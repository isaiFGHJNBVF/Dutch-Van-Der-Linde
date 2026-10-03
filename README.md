# Discord utility bot

Python Discord bot with slash commands, owner-only prefix controls, per-server
mention triggers, voice reconnect, message cleanup, and a spam-protect channel.

## Run it

- set the `DISCORD_BOT_TOKEN` Secret and start the **Discord Bot**
  workflow.
- For local development, copy `.env.example` to `.env` and put the bot token in
  `.env`. Do not commit `.env`.
- Alternatively, install the dependencies from `requirements.txt` with
  `python -m pip install -r requirements.txt`, then run `python main.py`.
- The bot stores trigger, whitelist, spam-channel, and 24/7 voice settings in
  `bot_data.json`. Keep that file if you want those settings after a restart.

## Discord setup

1. Create an application and bot in the Discord Developer Portal, then invite it
   to your server with the `bot` and `applications.commands` scopes.
2. Enable **Message Content Intent** and **Server Members Intent** in the
   application's Bot settings. The bot needs these intents for prefix commands,
   mention triggers, and voice-member operations.
3. Grant the bot the permissions it needs for the features you use:
   **View Channels**, **Send Messages**, **Read Message History**, **Manage
   Messages**, **Manage Channels**, **Manage Roles** (for timeouts), **Move
   Members**, **Connect**, and **Use Voice Activity**. Keep the bot's role above
   members it must move or timeout.
4. Global slash-command changes may take a short time to appear after the bot
   starts and syncs commands.

## Commands and access

- `/247` and `/leave` are available to all members.
- `/help` is visible to all members and lists slash commands only.
- Other slash commands default to members with **Manage Server** or
  **Administrator**. Bot owners are also allowed by the bot's runtime check.
  Discord controls command visibility separately: if an owner without Manage
  Server needs those commands to appear in the slash picker, a server
  administrator must grant that owner access in **Server Settings → Integrations
  → this bot → Commands**.
- Prefix commands work in server channels and direct messages. `.say` is limited
  to owners and users on the bot's whitelist. `.dm`, `.add wl`, `.remove wl`,
  `.list wl`, `.status`, `.custom_status`, and `.help` are owner-only. An
  unauthorized prefix command is silently ignored. Authorized prefix command
  messages are deleted after the bot processes them when Discord permits it;
  deletion of user messages in bot DMs may be blocked by Discord.

## Prefix and slash command reference

| Command | Access | Purpose |
| --- | --- | --- |
| `/247` | Everyone | Join the caller's voice channel and reconnect if the bot disconnects. |
| `/leave` | Everyone | Stop reconnecting and leave voice. |
| `/mass_move <channel1> <channel2>` | Manage Server / Administrator / bot owner | Move human members; bots are skipped. |
| `/addmembergif <member> <gif_link>` | Manage Server / Administrator / bot owner | Send the configured GIF when that member is mentioned. |
| `/addmemberemoji <member> <emoji>` | Manage Server / Administrator / bot owner | React when that member is mentioned. |
| `/removemembergif <member>` | Manage Server / Administrator / bot owner | Remove that member's GIF trigger. |
| `/removememberemoji <member>` | Manage Server / Administrator / bot owner | Remove that member's emoji trigger. |
| `/listmembergif` | Manage Server / Administrator / bot owner | List configured GIF triggers. |
| `/listmemberemoji` | Manage Server / Administrator / bot owner | List configured emoji triggers. |
| `/purge_all <amount>` | Manage Server / Administrator / bot owner | Delete up to the requested number of recent messages in this channel. |
| `/purge_bot <amount>` | Manage Server / Administrator / bot owner | Delete up to the requested number of recent bot messages in this channel. |
| `/purge_human <amount>` | Manage Server / Administrator / bot owner | Delete up to the requested number of recent human messages in this channel. |
| `/grab_spam <enabled>` | Manage Server / Administrator / bot owner | Create and enable the managed `spam-protect` channel, or turn it off and delete that managed channel. |
| `/help` | Everyone | List slash commands only. |
| `.help` | Bot owner | List every slash and prefix command. |
| `.say <content>` | Bot owner / whitelisted user | Send content in the current channel or DM. |
| `.dm <user mention or ID> <content>` | Bot owner | Send a direct message. |
| `.add wl <user mention or ID>` | Bot owner | Allow the user to use `.say`. |
| `.remove wl <user mention or ID>` | Bot owner | Remove the user's `.say` access. |
| `.list wl` | Bot owner | List whitelisted users with a non-notifying mention, ID, username, and display name. |
| `.status <dnd\|online\|idle>` | Bot owner | Change bot presence status. |
| `.custom_status <content>` | Bot owner | Set the bot's custom status text. |

Purge amounts are limited to 1–500. Spam protection times out anyone who posts
in the managed channel for one hour and attempts to delete that user's messages
from the previous ten minutes across the server's text channels. Discord
permissions and message-history limits can prevent some deletions.
