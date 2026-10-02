from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


owner = [1377967442172579843, 930300673281654784]
PREFIX = "."
DATA_FILE = Path(os.getenv("BOT_DATA_PATH", "bot_data.json"))
SPAM_CHANNEL_TOPIC = "Managed spam-protect channel. Created by this bot."
SPAM_CHANNEL_NAME = "spam-protect"
MAX_PURGE_AMOUNT = 500
MAX_HISTORY_SCAN = 5_000
VOICE_RECONNECT_INTERVAL = 15

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("discord_bot")


def _empty_data() -> dict[str, Any]:
    return {"prefix_whitelist": [], "guilds": {}}


class BotStore:
    """Small JSON-backed store for trigger, whitelist, voice, and channel settings."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Could not read bot data file {path}: {exc}") from exc
            if not isinstance(data, dict):
                raise RuntimeError(f"Bot data file {path} must contain a JSON object.")
            self.data = data
            self.data.setdefault("prefix_whitelist", [])
            self.data.setdefault("guilds", {})
        else:
            self.data = _empty_data()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as exc:
            raise RuntimeError(f"Could not save bot data file {self.path}: {exc}") from exc

    def guild(self, guild_id: int) -> dict[str, Any]:
        guilds = self.data["guilds"]
        key = str(guild_id)
        if key not in guilds:
            guilds[key] = {
                "member_gifs": {},
                "member_emojis": {},
                "voice_247_channel_id": None,
                "spam_protect_channel_id": None,
            }
        return guilds[key]

    def whitelist(self) -> set[int]:
        return {int(user_id) for user_id in self.data["prefix_whitelist"]}


store = BotStore(DATA_FILE)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    case_insensitive=True,
    allowed_mentions=discord.AllowedMentions.none(),
    help_command=None,
)
voice_guard_tasks: dict[int, asyncio.Task[None]] = {}
current_presence_status = discord.Status.online


async def _respond(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = True,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral)


async def _is_admin_or_owner(interaction: discord.Interaction) -> bool:
    if interaction.user.id in owner:
        return True
    member = interaction.user
    return isinstance(member, discord.Member) and (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    )


def admin_or_owner() -> Callable[[app_commands.Command[Any, ..., Any]], Any]:
    return app_commands.check(_is_admin_or_owner)


def owner_only() -> Callable[[commands.Context[Any]], Any]:
    async def predicate(ctx: commands.Context[Any]) -> bool:
        return ctx.author.id in owner

    return commands.check(predicate)


async def _get_user(user_id: int) -> discord.User | None:
    cached = bot.get_user(user_id)
    if cached is not None:
        return cached
    try:
        return await bot.fetch_user(user_id)
    except discord.NotFound:
        return None


async def _set_voice_target(guild: discord.Guild, channel_id: int | None) -> None:
    store.guild(guild.id)["voice_247_channel_id"] = channel_id
    store.save()


async def _voice_guard(guild_id: int) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            guild = bot.get_guild(guild_id)
            if guild is None:
                await asyncio.sleep(VOICE_RECONNECT_INTERVAL)
                continue

            target_id = store.guild(guild_id).get("voice_247_channel_id")
            target = guild.get_channel(int(target_id)) if target_id else None
            if not isinstance(target, discord.VoiceChannel):
                await asyncio.sleep(VOICE_RECONNECT_INTERVAL)
                continue

            voice_client = guild.voice_client
            if voice_client and voice_client.is_connected():
                if voice_client.channel.id != target.id:
                    await voice_client.move_to(target)
            else:
                if voice_client:
                    try:
                        await voice_client.disconnect(force=True)
                    except (discord.DiscordException, OSError):
                        logger.debug("Could not clean up the previous voice connection.")
                await target.connect(reconnect=True, timeout=20)
                logger.info("Rejoined 24/7 voice channel %s in guild %s", target.id, guild.id)
        except asyncio.CancelledError:
            raise
        except (discord.DiscordException, OSError, asyncio.TimeoutError):
            logger.exception("The 24/7 voice connection could not be restored.")
        await asyncio.sleep(VOICE_RECONNECT_INTERVAL)


def _ensure_voice_guard(guild_id: int) -> None:
    task = voice_guard_tasks.get(guild_id)
    if task is None or task.done():
        voice_guard_tasks[guild_id] = asyncio.create_task(
            _voice_guard(guild_id),
            name=f"voice-guard-{guild_id}",
        )


def _guild_trigger_map(guild: discord.Guild, key: str) -> dict[str, str]:
    return store.guild(guild.id).setdefault(key, {})


def _parse_gif_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_emoji(value: str) -> discord.PartialEmoji:
    parsed = discord.PartialEmoji.from_str(value.strip())
    is_custom = parsed.id is not None
    has_unicode = any(ord(character) > 127 for character in value)
    if not parsed.name or not (is_custom or has_unicode):
        raise ValueError("Enter a Unicode emoji or a custom emoji such as `<:name:id>`.")
    return parsed


async def _purge_messages(
    channel: discord.TextChannel,
    amount: int,
    predicate: Callable[[discord.Message], bool],
) -> int:
    scan_limit = min(MAX_HISTORY_SCAN, max(100, amount * 25))
    selected: list[discord.Message] = []
    async for message in channel.history(limit=scan_limit):
        if predicate(message):
            selected.append(message)
            if len(selected) >= amount:
                break

    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=14)
    recent = [message for message in selected if message.created_at >= recent_cutoff]
    old = [message for message in selected if message.created_at < recent_cutoff]
    deleted = 0

    for index in range(0, len(recent), 100):
        batch = recent[index : index + 100]
        if len(batch) == 1:
            await batch[0].delete()
        else:
            await channel.delete_messages(batch)
        deleted += len(batch)

    for message in old:
        await message.delete()
        deleted += 1
    return deleted


async def _delete_recent_user_messages(
    guild: discord.Guild,
    user_id: int,
    after: datetime,
) -> int:
    deleted = 0
    for channel in guild.text_channels:
        try:
            matches: list[discord.Message] = []
            async for message in channel.history(limit=1_000, after=after):
                if message.author.id == user_id:
                    matches.append(message)
            if not matches:
                continue
            recent_cutoff = datetime.now(timezone.utc) - timedelta(days=14)
            recent = [message for message in matches if message.created_at >= recent_cutoff]
            old = [message for message in matches if message.created_at < recent_cutoff]
            for index in range(0, len(recent), 100):
                batch = recent[index : index + 100]
                if len(batch) == 1:
                    await batch[0].delete()
                else:
                    await channel.delete_messages(batch)
                deleted += len(batch)
            for message in old:
                await message.delete()
                deleted += 1
        except discord.Forbidden:
            logger.warning(
                "Missing message-history or delete permission in channel %s.",
                channel.id,
            )
        except discord.HTTPException:
            logger.exception("Could not remove recent messages in channel %s.", channel.id)
    return deleted


async def _apply_spam_timeout(message: discord.Message) -> None:
    guild = message.guild
    if guild is None or not isinstance(message.author, discord.Member):
        return
    try:
        await message.author.timeout(
            datetime.now(timezone.utc) + timedelta(hours=1),
            reason="Posted in the spam-protect channel.",
        )
    except discord.Forbidden:
        logger.warning(
            "Could not timeout user %s in guild %s; check role order and permissions.",
            message.author.id,
            guild.id,
        )
        return
    except discord.HTTPException:
        logger.exception("Could not apply the spam-protect timeout.")
        return

    removed = await _delete_recent_user_messages(
        guild,
        message.author.id,
        datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    logger.info(
        "Timed out user %s in guild %s; removed %s recent messages.",
        message.author.id,
        guild.id,
        removed,
    )


async def _delete_prefix_invocation(message: discord.Message) -> None:
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        logger.debug(
            "Could not delete a prefix-command message (channel %s).",
            message.channel.id,
        )


def _amount_option() -> app_commands.Range[int, 1, MAX_PURGE_AMOUNT]:
    return app_commands.Range[int, 1, MAX_PURGE_AMOUNT]


@bot.event
async def on_ready() -> None:
    if bot.user is None:
        return
    logger.info("Signed in as %s (ID: %s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        logger.info("Synced %s application commands.", len(synced))
    except discord.DiscordException:
        logger.exception("Could not sync slash commands.")
    for guild_key, config in store.data.get("guilds", {}).items():
        if config.get("voice_247_channel_id"):
            _ensure_voice_guard(int(guild_key))


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if message.guild is not None:
        config = store.guild(message.guild.id)
        if config.get("spam_protect_channel_id") == message.channel.id:
            await _apply_spam_timeout(message)
            return

    if message.content.startswith(PREFIX):
        context = await bot.get_context(message)
        if context.command is not None:
            user_id = message.author.id
            is_owner = user_id in owner
            is_allowed_say = (
                context.command.qualified_name == "say"
                and user_id in store.whitelist()
            )
            if not (is_owner or is_allowed_say):
                return
            try:
                await bot.process_commands(message)
            finally:
                await _delete_prefix_invocation(message)
            return

    if message.guild is not None:
        mentioned_ids = {member.id for member in message.mentions}
        for member_id in mentioned_ids:
            gif_url = _guild_trigger_map(message.guild, "member_gifs").get(str(member_id))
            if gif_url:
                try:
                    await message.channel.send(
                        gif_url,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    logger.exception("Could not send a configured member GIF.")
            emoji_value = _guild_trigger_map(message.guild, "member_emojis").get(
                str(member_id)
            )
            if emoji_value:
                try:
                    await message.add_reaction(_parse_emoji(emoji_value))
                except (ValueError, discord.HTTPException):
                    logger.exception("Could not react with a configured member emoji.")

    await bot.process_commands(message)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.CheckFailure):
        await _respond(interaction, "You don’t have permission to use this command.")
        return
    original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
    if isinstance(original, discord.Forbidden):
        message = (
            "I’m missing a Discord permission for that action. "
            "Check my role and the channel permissions."
        )
    elif isinstance(original, discord.HTTPException):
        message = "Discord rejected that action. Check the bot’s permissions and try again."
    else:
        message = "The command failed. Check the bot log for details."
    logger.error(
        "Slash command failed: %s",
        getattr(interaction.command, "qualified_name", "unknown"),
        exc_info=(type(original), original, original.__traceback__),
    )
    await _respond(interaction, message)


@bot.event
async def on_command_error(
    ctx: commands.Context[Any],
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing required value: `{error.param.name}`.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("I couldn’t find that user. Use a user mention or numeric ID.")
        return
    if isinstance(error, commands.CommandInvokeError):
        logger.error("Prefix command failed.", exc_info=error.original)
        await ctx.send("The command failed. Check the bot log for details.")
        return
    logger.error("Prefix command failed.", exc_info=error)
    await ctx.send("The command failed.")


# Public slash commands: all members can discover and run these.
@bot.tree.command(name="help", description="List all slash commands.")
async def slash_help(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="Bot command help",
        color=discord.Color.blurple(),
        description=(
            "Available slash commands depend on your server permissions. "
            "Admin slash commands require Manage Server or Administrator."
        ),
    )
    embed.add_field(
        name="Public slash commands",
        value="`/247` — join your voice channel and keep reconnecting\n"
        "`/leave` — stop 24/7 mode and leave voice\n"
        "`/help` — list slash commands",
        inline=False,
    )
    embed.add_field(
        name="Admin slash commands",
        value=(
            "`/mass_move` · `/addmembergif` · `/addmemberemoji` · "
            "`/removemembergif` · `/removememberemoji` · `/listmembergif` · "
            "`/listmemberemoji` · `/purge_all` · `/purge_bot` · `/purge_human` · "
            "`/grab_spam`"
        ),
        inline=False,
    )
    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="247", description="Join your voice channel and keep reconnecting.")
async def stay_in_voice(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _respond(interaction, "Use this command in a server.")
        return
    voice_state = member.voice
    if voice_state is None or not isinstance(voice_state.channel, discord.VoiceChannel):
        await _respond(interaction, "Join a voice channel first.")
        return
    channel = voice_state.channel
    voice_client = guild.voice_client
    if voice_client and voice_client.is_connected():
        await voice_client.move_to(channel)
    else:
        if voice_client:
            await voice_client.disconnect(force=True)
        await channel.connect(reconnect=True, timeout=20)
    await _set_voice_target(guild, channel.id)
    _ensure_voice_guard(guild.id)
    await interaction.response.send_message(
        f"I joined **{channel.name}** and will reconnect if disconnected.",
        ephemeral=True,
    )


@bot.tree.command(name="leave", description="Stop 24/7 mode and leave voice chat.")
async def leave_voice(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await _respond(interaction, "Use this command in a server.")
        return
    await _set_voice_target(guild, None)
    voice_client = guild.voice_client
    if voice_client:
        await voice_client.disconnect(force=True)
        await interaction.response.send_message("I left the voice channel.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "I’m not in a voice channel. Reconnect mode is off.",
            ephemeral=True,
        )


@bot.tree.command(name="mass_move", description="Move human members between voice channels.")
@app_commands.default_permissions(manage_guild=True)
@admin_or_owner()
async def mass_move(
    interaction: discord.Interaction,
    channel1: discord.VoiceChannel,
    channel2: discord.VoiceChannel,
) -> None:
    members = [member for member in channel1.members if not member.bot]
    moved = 0
    failed = 0
    for member in members:
        try:
            await member.move_to(channel2, reason=f"Mass move requested by {interaction.user}")
            moved += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
    await interaction.response.send_message(
        f"Moved **{moved}** human member(s) from **{channel1.name}** "
        f"to **{channel2.name}**."
        + (f" {failed} could not be moved." if failed else ""),
        ephemeral=True,
    )


@bot.tree.command(name="addmembergif", description="Set a GIF trigger for a member mention.")
@app_commands.default_permissions(manage_guild=True)
@admin_or_owner()
async def add_member_gif(
    interaction: discord.Interaction,
    member: discord.Member,
    gif_link: str,
) -> None:
    if len(gif_link) > 2_000 or not _parse_gif_url(gif_link):
        await _respond(interaction, "Enter a valid `http://` or `https://` GIF link.")
        return
    if interaction.guild is None:
        await _respond(interaction, "Use this command in a server.")
        return
    _guild_trigger_map(interaction.guild, "member_gifs")[str(member.id)] = gif_link
    store.save()
    await interaction.response.send_message(
        f"Saved the GIF trigger for {member.mention}.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="addmemberemoji", description="Set an emoji reaction for a member mention.")
@app_commands.default_permissions(manage_guild=True)
@admin_or_owner()
async def add_member_emoji(
    interaction: discord.Interaction,
    member: discord.Member,
    emoji: str,
) -> None:
    try:
        _parse_emoji(emoji)
    except ValueError as exc:
        await _respond(interaction, str(exc))
        return
    if interaction.guild is None:
        await _respond(interaction, "Use this command in a server.")
        return
    _guild_trigger_map(interaction.guild, "member_emojis")[str(member.id)] = emoji
    store.save()
    await interaction.response.send_message(
        f"Saved the emoji trigger for {member.mention}.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="removemembergif", description="Remove a member’s GIF trigger.")
@app_commands.default_permissions(manage_guild=True)
@admin_or_owner()
async def remove_member_gif(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if interaction.guild is None:
        await _respond(interaction, "Use this command in a server.")
        return
    removed = _guild_trigger_map(interaction.guild, "member_gifs").pop(
        str(member.id), None
    )
    store.save()
    await interaction.response.send_message(
        "Removed the GIF trigger." if removed else "That member has no GIF trigger.",
        ephemeral=True,
    )


@bot.tree.command(name="removememberemoji", description="Remove a member’s emoji trigger.")
@app_commands.default_permissions(manage_guild=True)
@admin_or_owner()
async def remove_member_emoji(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if interaction.guild is None:
        await _respond(interaction, "Use this command in a server.")
        return
    removed = _guild_trigger_map(interaction.guild, "member_emojis").pop(
        str(member.id), None
    )
    store.save()
    await interaction.response.send_message(
        "Removed the emoji trigger." if removed else "That member has no emoji trigger.",
        ephemeral=True,
    )


async def _list_member_triggers(
    interaction: discord.Interaction,
    key: str,
    title: str,
) -> None:
    guild = interaction.guild
    if guild is None:
        await _respond(interaction, "Use this command in a server.")
        return
    triggers = _guild_trigger_map(guild, key)
    if not triggers:
        await interaction.response.send_message(
            f"No {title.lower()} have been configured.",
            ephemeral=True,
        )
        return
    pages: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for user_id, value in sorted(triggers.items()):
        line = f"<@{user_id}> — `{value}`"
        if current and current_length + len(line) + 1 > 3_800:
            pages.append(current)
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + 1
    if current:
        pages.append(current)

    embeds = [
        discord.Embed(
            title=f"{title} ({index}/{len(pages)})" if len(pages) > 1 else title,
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        for index, lines in enumerate(pages, start=1)
    ]
    await interaction.response.send_message(
        embed=embeds[0],
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    for embed in embeds[1:]:
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


@bot.tree.command(name="listmembergif", description="List members with GIF triggers.")
@app_commands.default_permissions(manage_guild=True)
@admin_or_owner()
async def list_member_gifs(interaction: discord.Interaction) -> None:
    await _list_member_triggers(interaction, "member_gifs", "Member GIF triggers")


@bot.tree.command(name="listmemberemoji", description="List members with emoji triggers.")
@app_commands.default_permissions(manage_guild=True)
@admin_or_owner()
async def list_member_emojis(interaction: discord.Interaction) -> None:
    await _list_member_triggers(interaction, "member_emojis", "Member emoji triggers")


@bot.tree.command(name="purge_all", description="Delete recent messages from this channel.")
@app_commands.default_permissions(manage_guild=True)
@admin_or_owner()
async def purge_all(
    interaction: discord.Interaction,
    amount: _amount_option(),
) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        await _respond(interaction, "Use this command in a server text channel.")
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await _purge_messages(
        interaction.channel,
        amount,
        lambda _message: True,
    )
    await interaction.followup.send(f"Deleted **{deleted}** message(s).", ephemeral=True)


@bot.tree.command(name="purge_bot", description="Delete recent bot messages from this channel.")
@app_commands.default_permissions(manage_guild=True)
@admin_or_owner()
async def purge_bot(
    interaction: discord.Interaction,
    amount: _amount_option(),
) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        await _respond(interaction, "Use this command in a server text channel.")
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await _purge_messages(
        interaction.channel,
        amount,
        lambda message: message.author.bot,
    )
    await interaction.followup.send(f"Deleted **{deleted}** bot message(s).", ephemeral=True)


@bot.tree.command(name="purge_human", description="Delete recent human messages from this channel.")
@app_commands.default_permissions(manage_guild=True)
@admin_or_owner()
async def purge_human(
    interaction: discord.Interaction,
    amount: _amount_option(),
) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        await _respond(interaction, "Use this command in a server text channel.")
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await _purge_messages(
        interaction.channel,
        amount,
        lambda message: not message.author.bot,
    )
    await interaction.followup.send(
        f"Deleted **{deleted}** human message(s).",
        ephemeral=True,
    )


@bot.tree.command(name="grab_spam", description="Enable or disable the spam-protect channel.")
@app_commands.default_permissions(manage_guild=True)
@admin_or_owner()
async def grab_spam(
    interaction: discord.Interaction,
    enabled: bool,
) -> None:
    guild = interaction.guild
    if guild is None:
        await _respond(interaction, "Use this command in a server.")
        return
    config = store.guild(guild.id)
    channel_id = config.get("spam_protect_channel_id")
    managed = guild.get_channel(int(channel_id)) if channel_id else None

    if enabled:
        if managed is not None and not isinstance(managed, discord.TextChannel):
            await _respond(interaction, "The saved spam-protect channel is not a text channel.")
            return
        if not isinstance(managed, discord.TextChannel):
            existing = discord.utils.get(guild.text_channels, name=SPAM_CHANNEL_NAME)
            if existing is not None:
                if existing.topic != SPAM_CHANNEL_TOPIC:
                    await _respond(
                        interaction,
                        "A channel named `spam-protect` already exists and is not bot-managed. "
                        "Rename it first; I won’t take it over.",
                    )
                    return
                managed = existing
            else:
                managed = await guild.create_text_channel(
                    SPAM_CHANNEL_NAME,
                    topic=SPAM_CHANNEL_TOPIC,
                    reason=f"Spam protection enabled by {interaction.user}",
                )
                await managed.edit(position=0)
            config["spam_protect_channel_id"] = managed.id
            store.save()

        embed = discord.Embed(
            title="Dont Send Any Message Here",
            description=(
                "If you send any message here you will get timeout of 1 hours and "
                "your past 10mins of messages will get deleted"
            ),
            color=discord.Color.from_rgb(0, 0, 0),
        )
        await managed.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await interaction.response.send_message(
            f"Spam protection is enabled in {managed.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    if managed is None:
        config["spam_protect_channel_id"] = None
        store.save()
        await interaction.response.send_message(
            "Spam protection is already off; there is no managed channel to delete.",
            ephemeral=True,
        )
        return
    if not isinstance(managed, discord.TextChannel):
        await _respond(interaction, "The saved spam-protect channel is not a text channel.")
        return
    if managed.topic != SPAM_CHANNEL_TOPIC:
        await _respond(
            interaction,
            "The saved channel no longer has the bot’s management marker, so I left it untouched.",
        )
        return
    await managed.delete(reason=f"Spam protection disabled by {interaction.user}")
    config["spam_protect_channel_id"] = None
    store.save()
    await interaction.response.send_message(
        "Spam protection is off and the managed channel was deleted.",
        ephemeral=True,
    )


@bot.group(name="add", invoke_without_command=True)
async def add_group(ctx: commands.Context[Any]) -> None:
    await ctx.send("Use `.add wl <user mention or ID>`.")


@add_group.command(name="wl")
@owner_only()
async def add_whitelist(ctx: commands.Context[Any], user: discord.User) -> None:
    whitelist = store.data["prefix_whitelist"]
    if user.id in store.whitelist():
        await ctx.send(f"{user} is already whitelisted.")
        return
    whitelist.append(user.id)
    store.save()
    await ctx.send(f"Whitelisted {user.mention}.", allowed_mentions=discord.AllowedMentions.none())


@bot.group(name="remove", invoke_without_command=True)
async def remove_group(ctx: commands.Context[Any]) -> None:
    await ctx.send("Use `.remove wl <user mention or ID>`.")


@remove_group.command(name="wl")
@owner_only()
async def remove_whitelist(ctx: commands.Context[Any], user: discord.User) -> None:
    whitelist = store.data["prefix_whitelist"]
    if user.id not in store.whitelist():
        await ctx.send(f"{user} is not whitelisted.")
        return
    store.data["prefix_whitelist"] = [
        int(user_id) for user_id in whitelist if int(user_id) != user.id
    ]
    store.save()
    await ctx.send(f"Removed {user.mention} from the whitelist.", allowed_mentions=discord.AllowedMentions.none())


@bot.group(name="list", invoke_without_command=True)
async def list_group(ctx: commands.Context[Any]) -> None:
    await ctx.send("Use `.list wl`.")


@list_group.command(name="wl")
@owner_only()
async def list_whitelist(ctx: commands.Context[Any]) -> None:
    user_ids = sorted(store.whitelist())
    if not user_ids:
        await ctx.send("The prefix whitelist is empty.")
        return
    lines = []
    for user_id in user_ids:
        user = await _get_user(user_id)
        username = user.name if user else "Unknown user"
        display_name = user.global_name if user and user.global_name else username
        lines.append(
            f"<@{user_id}> — ID `{user_id}` — username `{username}` — "
            f"display name `{display_name}`"
        )
    chunks: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        if current and current_length + len(line) + 1 > 1_800:
            chunks.append(current)
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + 1
    if current:
        chunks.append(current)
    for chunk in chunks:
        await ctx.send(
            "\n".join(chunk),
            allowed_mentions=discord.AllowedMentions.none(),
        )


@bot.command(name="dm")
@owner_only()
async def dm_user(
    ctx: commands.Context[Any],
    user: discord.User,
    *,
    content: str,
) -> None:
    await user.send(content, allowed_mentions=discord.AllowedMentions.none())
    await ctx.send(f"Sent a DM to {user.mention}.", allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="say")
async def say(ctx: commands.Context[Any], *, content: str) -> None:
    if ctx.author.id not in owner and ctx.author.id not in store.whitelist():
        return
    await ctx.send(content, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="status")
@owner_only()
async def set_status(ctx: commands.Context[Any], status: str) -> None:
    global current_presence_status
    status = status.lower()
    statuses = {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
    }
    if status not in statuses:
        await ctx.send("Use `.status <dnd|online|idle>`.")
        return
    current_presence_status = statuses[status]
    await bot.change_presence(status=current_presence_status)
    await ctx.send(f"Bot status set to **{status}**.")


@bot.command(name="custom_status")
@owner_only()
async def set_custom_status(ctx: commands.Context[Any], *, content: str) -> None:
    if len(content) > 128:
        await ctx.send("Custom status text must be 128 characters or fewer.")
        return
    await bot.change_presence(
        status=current_presence_status,
        activity=discord.CustomActivity(name=content),
    )
    await ctx.send("Custom status updated.")


@bot.command(name="help")
@owner_only()
async def prefix_help(ctx: commands.Context[Any]) -> None:
    await ctx.send(
        "**Slash:** `/247`, `/leave`, `/help`; admin-only: `/mass_move`, "
        "`/addmembergif`, `/addmemberemoji`, `/removemembergif`, "
        "`/removememberemoji`, `/listmembergif`, `/listmemberemoji`, "
        "`/purge_all`, `/purge_bot`, `/purge_human`, `/grab_spam`.\n"
        "**Prefix:** `.help`, `.say <content>`, `.dm <user> <content>`, "
        "`.add wl <user>`, `.remove wl <user>`, `.list wl`, "
        "`.status <dnd|online|idle>`, `.custom_status <content>`.\n"
        "`.say` is for owners and whitelisted users. Whitelist, DM, and status "
        "commands are owner-only."
    )


def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Missing DISCORD_BOT_TOKEN. Add it as a Replit Secret or in a local .env file."
        )
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure as exc:
        raise SystemExit(
            "Discord rejected DISCORD_BOT_TOKEN. Replace it with the bot token from the "
            "Discord Developer Portal."
        ) from exc


if __name__ == "__main__":
    main()