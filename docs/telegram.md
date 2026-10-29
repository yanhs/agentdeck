# Telegram: drive the terminals from your phone

[← back to the README](../README.md)

The bridge (`tg_bridge.py`) is a Telegram bot that types your messages into the selected
terminal and sends the screen back. It is **owner-only**: every handler is filtered by your
numeric Telegram user id, so messages or button taps from anyone else are ignored.

<p align="center">
  <img src="screenshots/telegram-live.gif" width="300" alt="The Telegram bridge streaming an agent's reply in real time, then showing an interactive prompt as inline buttons">
</p>

## The easy way: the dashboard's Telegram page

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts and copy
   the token it gives you.
2. Find your numeric Telegram **user id** (not your `@username`): message
   [@userinfobot](https://t.me/userinfobot) (it replies with your `Id`),
   [@RawDataBot](https://t.me/RawDataBot) (the `message.from.id` field) or
   [@myidbot](https://t.me/myidbot) (send `/getid`).
3. In the dashboard open **⋯ → Telegram**, paste the token and your user id, and press
   **Save & start**. The bot starts right away and comes back after a restart. The
   installer never handles the token.

If the bridge already runs as a systemd service (below), the Telegram page shows it as
running and never starts a second copy.

**Docker:** the image has no `faster-whisper` or `ffmpeg`, so voice notes get an error
reply; text and files work. Files you send are saved in `/work/tg-uploads` and the reply
gives that path.

## Commands

| Command | What it does |
|---|---|
| `/use` | pick a terminal from buttons (loaded ones first, archived ones left out) |
| `/use <text>` | pick a terminal by part of its name or its code (several matches → buttons; a match only in the archive → a button that restores it) |
| `/list` | all terminals, loaded ones first; the current one is marked; how many are archived |
| `/archive` | archived terminals as buttons: a tap restores the terminal and picks it |
| `/archive <text>` | only the archived terminals whose name or code matches |
| `/new <name>` | start a new terminal and select it |
| `/read` | re-read the current terminal's screen (after every pick this happens by itself) |
| `/esc` | interrupt the agent (Escape) |
| `/enter` | send Enter |
| `/compact` | compact the agent's conversation |
| `/restart` | restart Claude in the current terminal, on the same conversation (asks first if it is busy) |
| `/services` | your services with their state, and buttons to restart, stop or start each |

Plain text is typed into the selected terminal (an unloaded one is loaded first). Voice
notes are transcribed locally with [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper).
Files and images are saved and handed to the agent as a path (and a link, if you set
`TG_FILES_URL`). The agent's progress and final answer stream back, and Claude's
multiple-choice questions arrive as inline buttons.

## Manual setup with `.env` (no dashboard page)

```dotenv
TG_BRIDGE_TOKEN=123456:ABC-DEF...your-token   # required — the bridge won't start without it
TG_BRIDGE_OWNER=123456789                     # your numeric user id (default 0 = nobody allowed)

# optional — all have working defaults:
# TG_FILES_DIR=/path/to/uploads               # where uploaded files are saved (default .sessions/tgfiles)
# TG_FILES_URL=https://example.com/files      # public base URL of that folder (default: none — the reply gives the local path)
# TG_WHISPER_PY=/path/to/venv/bin/python      # a Python that has faster-whisper (default: the bridge's own python)
# TG_AGENT_CWD=/path/to/projects              # where an agent started from the bot runs (default $AGENTDECK_WORKDIR or ~)
# TG_BRIDGE_API_BASE=https://relay.example.com  # a Bot API relay instead of api.telegram.org
```

`TG_BRIDGE_OWNER` defaults to `0`, which matches no real user, so the bot ignores everyone
until you set it.

```bash
pip install python-telegram-bot      # the only third-party package the bridge needs
```

Voice transcription is optional: voice notes are handed to a separate Python
(`TG_WHISPER_PY`) that has `faster-whisper` installed, with `ffmpeg` on the system.

```bash
# development
python3 tg_bridge.py

# production — systemd user service (template: claude-tg-bridge.service)
cp claude-tg-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-tg-bridge.service
loginctl enable-linger "$USER"     # keep it running without an active login
```
