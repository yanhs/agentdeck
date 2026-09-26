# Changelog

All notable changes to AgentDeck. Newest first.

## Unreleased

- **Guard hooks on by default in Docker.** The container start merges
  `hooks/guard_task_board.py` + `hooks/guard_dont_stop.py` into the agents'
  `~/.claude/settings.json` (idempotent; other settings and your own hooks kept) and writes a
  short default `~/.claude/CLAUDE.md` with the board commands if none exists — every task an
  agent takes on goes on the board, and it keeps working while the task is open. Opt out:
  `AGENTDECK_GUARDS=0`. `start.sh` leaves your own `~/.claude` alone unless
  `AGENTDECK_GUARDS=1`. New `hooks/install_guards.py` (install / `--remove` / `--check`).
- **A terminal waiting on its own timer stays loaded.** `install_guards.py` now also wires `hooks/hold_on_timer.py` (PostToolUse on `ScheduleWakeup`/`CronCreate`/`Monitor`), so the idle reaper no longer unloads an agent before its timer fires.

## v1.6.0 — archive that frees memory, Telegram menu with the archive

### Dashboard
- **Archive unloads the terminal** right away, unless it is working (printing, a timer, a
  background task); a busy one unloads by itself once it goes quiet. The dashboard says which.
- **Clicking an archived terminal restores and opens it.** The Restore button is gone; Delete
  stays. The task board's terminal links restore an archived terminal the same way.

### Telegram bridge
- **`/archive`**: archived terminals as buttons (newest first); a tap restores and opens it,
  like a click on the dashboard. `/archive <text>` searches the archive.
- **`/use`** buttons list the terminals outside the archive, loaded first; `/list` shows how
  many are archived.
- **After a pick the bot re-reads the session by itself**: a second message "📺 «name» · id"
  with the terminal's screen once Claude is up, or its last reply if it can't load.
- A topic still running in an old numbered terminal is served there (no "already open
  elsewhere" refusal); `/read` on an unloaded terminal shows its last reply from the transcript.
- The menu says "terminal" everywhere, like the dashboard.

### Repo
- The nginx example has no numbered `/terminalN` routes any more (one ttyd on `/sess/`).
- README gallery: fewer task-board shots; new archive and Telegram screenshots.

## v1.5.1 — terminal ⇄ tasks links, fixes from a fresh-user Docker install test

Tested before release: the full suite, and a fresh install from GitHub with Docker following
the README step by step (password, new terminal, cmd, Tasks, Server, theme, Password and
Telegram pages, logout/login, data surviving `docker compose up -d --force-recreate`).

### Links
- **A terminal's code opens its tasks** from the terminal list too (it already did from the
  top bar); the rest of the row still opens the terminal.
- **A task's terminal code opens that terminal** from the task board — inside the dashboard it
  switches to the terminal, on its own it opens `/?open=<code>`.
- **Tasks, Server and Command line** rows take one line, without captions.

### Docker
- **Server tab works in Docker.** Both Caddyfiles now route `/api/server` to the backend
  (it was only in the nginx config, so the tab showed 404). A test derives every backend path
  from `nginx/agents-subdomain.conf` and checks both Caddyfiles route it, so they can't drift.
- **HTTPS certificates persist.** The image sets `XDG_DATA_HOME=/data` and
  `XDG_CONFIG_HOME=/data/config`, so Caddy keeps its Let's Encrypt certificates in the
  `caddy-data` volume (it used to stay empty and certificates were lost on re-create).
- **Agents' work persists.** `/work` is a named volume (`agentdeck-work`); replace it with
  `./my-project:/work` to work on your own files.
- **Pasted images and Telegram files** no longer point at the author's server: defaults are
  `.sessions/paste` and `.sessions/tgfiles` inside the repo (the persisted volume in Docker),
  no public URL (the path is handed back instead), whisper runs with the bridge's own Python,
  and a bot-started agent runs in `$AGENTDECK_WORKDIR` (or `~`). Override with
  `AGENTDECK_PASTE_DIR` / `AGENTDECK_PASTE_URL` and `TG_FILES_DIR` / `TG_FILES_URL` /
  `TG_WHISPER_PY` / `TG_AGENT_CWD`.
- **ttyd for ARM too.** The Dockerfile downloads the ttyd build for the machine's CPU
  (x86_64 or aarch64).
- **No fixed container name** in `docker-compose.yml`, so two installs (`-p`) don't clash.
- **Claude Code version pinned** with a build argument (`CLAUDE_CODE_VERSION`, default
  `2.1.282`); `--build-arg CLAUDE_CODE_VERSION=latest` takes the newest.

### Dashboard
- **Tab title** is "AgentDeck" (it showed the author's domain).
- **After logout** the browser shows the login page instead of a cached, empty dashboard:
  HTML is sent with `Cache-Control: no-cache` (Caddy and nginx).
- **No typed command line when a terminal opens.** The Claude pane starts with its command
  directly, instead of showing the long `for v in … exec claude …` line (twice) first.
- **Login page** says 🛰 AgentDeck and asks only for the password when the dashboard has its
  own password (Docker / `start.sh`). The Username field stays where it's checked (htpasswd).
- **The ⋯ menu** (Telegram, Password, Theme, Logout) is available on the empty dashboard,
  not only while a terminal is open.

### Docs
- README: the Docker quick start needs one free TCP port (8765) open in the firewall — and
  Docker-published ports bypass `ufw`; `AGENTDECK_PORT=9000 docker compose up -d`; the working
  self-signed HTTPS command (`AGENTDECK_SITE=https://:8765`); for a real domain edit `ports:`
  to `80:80` + `443:443` (both free); the first terminal asks for a colour theme, then how to
  log in; voice messages don't work in the Docker image (no faster-whisper / ffmpeg).
- `.env.example`: the dashboard login lines are commented out ("Docker: leave unset") —
  compose loads `.env` automatically, and the old example set a fixed password and domain.

## v1.5.0 — server page, light theme, guard hooks

- **Server page.** A live view of the machine (`web/server.html`, `GET /api/server`, collected
  by `server_status.py` every 5 s for under 1% of one core): CPU, memory, disk and load gauges,
  a 15-minute CPU chart by group, a "who uses the CPU right now" bar, and rows for every agent
  (with the tests and builds it started), site, container, service and background job, each
  with CPU and memory. Command lines are never shown in full; anything that looks like a
  secret is masked. Opens as a closable **Server** tab next to Tasks.
- **Light theme.** A Dark / Light switch in the dashboard's ⋯ menu (remembered in `localStorage` `agentdeck-theme`, applied before first paint); the embedded Tasks and Server pages follow it live. The terminal stays dark.
- **Guard hooks.** `hooks/guard_task_board.py` blocks the first file edit of a session until
  the task is on the board (or the agent runs `NO_BOARD=1 true` for a trivial change).
  `hooks/guard_dont_stop.py` blocks ending a turn while this session's board task is still
  active and no background command, timer, monitor or sub-agent will resume it. Both give way
  if they hit an error of their own. Settings are environment variables. Copy the wiring from
  `hooks/settings.example.json`. Tests: `tests/test_guard_hooks.py`.
- **Terminal id → its tasks.** Clicking the 8-character code in the top bar opens the Tasks tab
  filtered to that terminal (`/tasks/?session=<code>`, shown as a removable chip).
- **Task board inside the dashboard** uses the dashboard's colours and a slightly smaller type.
- **RAM readout** in GB with one decimal (`13.8/23.5G`).
- **Leaner repo.** The numbered-slot launch scripts (`launch-claude*.sh`, `_order_gate.py`) are
  no longer shipped; `migrate_library.py` still upgrades an old install that has them.

## v1.4.0 — named terminals instead of numbered slots

The fixed row of numbered terminals (1–8, later 1–12) is gone. Terminals are now a
**session library**: as many named terminals as you like, each one a Claude conversation,
with only a limited number loaded in memory at a time.

### Session library

- **Named terminals.** Each terminal has a name and an 8-character code. The code is the
  start of the Claude session id, so the dashboard row, the tmux session (`cs-<code>`), the
  logs and the transcript file (`<uuid>.jsonl`) all share it. No slot numbers anywhere.
- **＋ New terminal** starts a new Claude conversation (name optional — a dated default is
  used). **✎** renames it.
- **Archive / Restore / Delete.** Archive hides a terminal from the list; **Show archived**
  brings the archive back so you can restore it. Only an archived terminal can be deleted;
  its transcript is moved to `.sessions/trash/`, never erased.
- **Your own order.** Drag rows to arrange them (on touch screens, use ▲ ▼). Loaded terminals stay at the
  top; the rest follow, greyed.
- **Search** by name or code, archived terminals included. Enter opens the first match.
- **Memory limit.** At most `AGENTDECK_MAX_ACTIVE` terminals (default 12) run at once.
  Opening one more unloads the least recently used terminal that is idle and has no tab
  open. If all of them are busy, it says so and you try again later. Unloading loses nothing: the
  conversation stays on disk and `claude --resume` picks it up on the next click.
- **One web terminal for all.** A single `ttyd` serves every terminal at
  `/sess/?arg=<code>` (`open-session.sh` → `library_cli.py`), instead of one `ttyd` and one
  launch script per slot. It refuses to start a second Claude on a conversation that is
  already running somewhere else.
- **Timers keep a terminal loaded.** `hooks/hold_on_timer.py` (a Claude Code hook) marks a
  terminal as busy while a `ScheduleWakeup`, `CronCreate` or `Monitor` it set is pending, so
  unloading does not kill the timer.
- **Library API** in `status_server.py`: `GET /api/library` and
  `POST /api/library/{new,rename,archive,close,delete,reorder}`.

### Idle reaper

- New `idle_reaper.py` (run from cron once a minute) unloads terminals nobody is using:
  no screen output for **2 hours** (archived terminals: **2 minutes**).
- It never unloads a terminal that has a browser tab open, holds a timer, or runs a
  background task (a background task stops protecting it after 24 h of silence).
- "Idle" is measured by the last screen output, not by a CPU sample.

### Dashboard

- **cmd** — one plain `bash` command line in the browser, shared by every tab; not counted
  toward the memory limit. `exit` closes it; press **cmd** again for a new one.
- **Tasks tab.** The **T** button opens the task board inside the dashboard as a tab,
  pinned above the terminal list; ✕ closes it.
- **Account menu (⋯)** holds Telegram, Password and Logout. **Esc** is the first button above
  the terminal.
- **English-only** dashboard, Telegram bridge replies, and terminal messages.

### Task board

- Redesigned: created/updated dates and times, search across tasks, steps and notes,
  status filters (All / Active / Blocked / Pending / Done), an agent filter, and sorting
  (recently updated, recently created, title). Follows the system light/dark theme.
- Each task shows the agent's session code, recorded by `tracker.py` from the Claude
  session it runs in — the same code the terminal list shows.

### Telegram bridge

- Works with named terminals: `/use <part of a name or code>` (several matches → buttons),
  `/list` (loaded terminals first), `/new <name>`. An unloaded terminal is loaded before
  your message is typed in, and text is typed only when Claude is actually running there.
- `/use N` still reaches a terminal migrated from slot N.
- The **Telegram** setup page sees a bridge that runs as the systemd service
  (`claude-tg-bridge`, or `TG_SYSTEMD_UNIT`) and never starts a second one on the same token.

### Login

- With `AGENTDECK_PASSFILE` set, the dashboard keeps its own password file. If that file is
  still empty but an nginx htpasswd exists, the first correct login copies the password
  into it — nobody has to set it again. **Password** in the account menu changes it.

### Moving from numbered slots

- `migrate_library.py --dry-run` shows what will happen; `--apply` turns each old slot
  with a real conversation into a named terminal (name from `agents.json`), and replaces
  each old `launch-claude-N.sh` with a small script that opens that terminal (the original
  is kept as `.pre-library`). Terminals that are running are **not** touched or restarted.
  Nothing is deleted. `--swap-dashboard` / `--rollback-dashboard` switch the page.

### Docker and `./start.sh`

- Both now run the session library: one `ttyd` on `/sess/`, the library API, the task board
  on `/tasks/`, and the idle reaper once a minute. The old numbered `/terminalN` routes are
  gone from both Caddyfiles.
- `AGENTDECK_ORIGIN` is optional: when unset, a library change is accepted only from the
  dashboard's own address (the request's scheme + Host, as the proxy forwards them).

### nginx

- New routes in `nginx/agents-subdomain.conf`: `/sess/` (the terminal, WebSocket),
  `/api/library`, `/telegram` and `/change-password` (the last two used to fall through to
  the dashboard page).

682 tests.

## v1.3.0 — set your password in the browser

- The first visit sets the dashboard password (entered twice); a **Password** button
  changes it later.
- Dashboard, APIs and terminals all stay behind the login. Works with `docker compose up`
  and `./start.sh`; an existing nginx + htpasswd setup is untouched.

## v1.2.0 — secure by default

- Every install path requires a login: `docker compose up` and `./start.sh` refuse to start
  without `AGENTDECK_PASSWORD`.
- Set `AGENTDECK_SITE` to your domain and Caddy fetches a TLS certificate for it.
- Older releases (v1.0.0–v1.1.1) shipped an unauthenticated dashboard and were removed.
