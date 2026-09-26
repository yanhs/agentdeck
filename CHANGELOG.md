# Changelog

All notable changes to AgentDeck. Newest first.

## Unreleased

### Terminals
- **One number per terminal: a terminal and its conversation always share one number.**
  Claude can change the conversation under a running terminal: its "allow bypass
  permissions?" prompt, answered yes on a fresh install, restarts Claude without the
  session id AgentDeck gave it; `/clear` starts a new conversation; `/resume` switches to
  another. The dashboard used to keep the old number while the task board (and Claude
  itself) used the new one, and a restart opened an empty conversation under the old
  number. Now the terminal takes the new conversation's number: the new `convo_sync.py`
  reads the file Claude keeps per running process (`~/.claude/sessions/<pid>.json`), finds
  each terminal's Claude through the process tree (not by the file's tmux name, which goes
  stale after a rename), moves the registry entry and renames tmux `cs-<old>` to
  `cs-<new>` — an open tab stays attached, the process is not touched, a pending-timer hold
  moves along, and a restart resumes the live conversation. It runs on every list refresh,
  before close / archive / delete / rename, in `library_cli.py ensure` and `active`, in the
  idle reaper, and before the Telegram bridge reads a chat's terminal. The old number: when
  it never held a conversation (the permissions prompt), it disappears and old links
  (`/?open=`, the task board, `/sess/?arg=`, `/use`) still land on the terminal; when it
  has messages (`/clear` after work), it stays in the list as its own unloaded terminal,
  "… (earlier)", and opens on its own. A conversation open in two terminals at once
  (`/resume` of one that another terminal has open) is left as it is and logged — nothing
  is merged or killed. The open page and a Telegram chat follow their terminal to its new
  number, but not back to an earlier conversation picked on purpose. The pending-timer
  hook and `library_cli.py hold -` use the conversation live now, not the number the pane
  was started with. A Claude on a terminal of its own inside a pane (`script -c claude`)
  is not mistaken for the terminal's, and the npm package's `claude.exe` counts as Claude.
  Claude's permissions prompt itself is left as it is.
- **A careless `pkill -f grep` elsewhere on the server can no longer take down every
  terminal:** a tmux server keeps, as its own command line, the command line of the tmux
  call that started it, and that used to be `tmux … new-session … -c <folder> bash -lic
  'for v in $(env | … grep -i CLAUDE) … exec claude --resume …'` — so `pkill -f` with
  `grep`, `claude`, `bash` or `env` in its pattern (even inside a `… | grep` meant as a
  pipe) matched the server and killed all terminals at once. The server also keeps the
  working directory of that call, so a terminal's folder there would make `fuser -k
  <folder>` or `lsof +D <folder> | xargs kill` ("free this folder") do the same. Now,
  when no server runs, AgentDeck starts one first with a line that says nothing — `tmux
  -f /dev/null new-session -d -s hold-<hex> tmux wait-for hold-<hex>`, called from `/`:
  no grep/claude/bash/env, no `agentdeck`, no path of the checkout or of a folder —
  loads `tmux.conf` into it, makes the terminal on it and lets the placeholder go. The
  terminal's own `new-session` (`-c <folder> <repo>/bin/pane <code>`, or `… bin/pane
  shell` for `cmd`) runs on a server that is already up, so its line is not the server's,
  and tmux's new windows in the terminal still open in its folder. Every tmux call that
  makes a session is made from `/`. The new launcher `bin/pane` (a name with no such
  word either) checks the code, enters the folder (one it can't enter: home, tmux's own
  rule), loads the login shell's environment, unsets `CLAUDE*`, sources
  `~/.claude/oauth.env`, exports `AGENTDECK_SESSION` and execs claude with the arguments
  `library_cli.py ensure` prepared for this start (`--resume`/`--session-id`, the
  ultracode/max restore). The start is left in `<checkout>/.sessions/launch/<code>`, read
  once, and found from the launcher's own path — never from the environment, which in a
  pane is the tmux server's (whoever started it first), not the dashboard's: a moved
  registry (`AGENTDECK_LIBRARY`) cannot send it elsewhere. It is used only if nobody else
  could have written it (not a link, not writable by others) and runs only a program
  named `claude` (`claude.exe`: the npm build), so `CLAUDE_BIN` must name a file called
  that, as `install.sh` sets it. A start tmux did not take — refused, or tmux hung for
  15 s (now a message and exit 1, not a traceback) — is removed at once. A
  Telegram-started old numbered slot is made the same way under a neutral name and
  renamed to `claude-terminal-N`. A server that is already running keeps its line and
  working directory until it next starts. (`pkill -f claude` still ends every Claude
  itself — that is what it asks for; the conversations stay resumable.)
- **The old numbered terminals' buttons reach only their own terminal.** tmux reads a
  bare `-t claude-terminal` as a prefix when no session has exactly that name — and slot
  #1's `claude-terminal` is the prefix of every slot. With #1 not running and one other
  slot up, the × (unload) of #1 killed that other terminal, and `/compact`, a model or
  effort change for #1 were typed into it; the status list showed #1 running with the
  other's screen. Every tmux target there is exact now (`=name`, `=name:`).

## v1.7.0 — HTTPS by default, copy & paste like a desktop terminal, ultracode survives restarts

### Install on your own server (`install.sh`)
- **One command; agents get the whole server.**
  `curl -fsSL https://raw.githubusercontent.com/yanhs/agentdeck/master/install.sh | bash` (or
  `./install.sh` from a clone) on Ubuntu 22.04 / 24.04 or Debian 12 (x86_64, aarch64):
  apt basics, ttyd + Caddy binaries, Node.js 22 when needed, Claude Code (the Dockerfile's
  pinned version), the repo in `~/agentdeck`, guard hooks merged into `~/.claude`, and
  systemd services running as you (status server, task board, the sessions ttyd, Caddy for
  the login and HTTPS, an idle-reaper timer) that start at boot. Options: `--https [domain]`,
  `--http`, `--uninstall [--purge]`, `--check`, `--yes`, `--telegram`.
  Refuses root (unless `AGENTDECK_ALLOW_ROOT=1`) and unsupported systems without changing
  anything; re-running upgrades and repairs, and restarts never kill running agents. The
  agents get a tmux server of their own (`TMUX_TMPDIR=~/agentdeck/.sessions/tmux`), so
  `--uninstall` stops only that one and never your own tmux sessions; `--purge` deletes only
  a folder the installer cloned itself (recorded in `~/.config/agentdeck/install.env`, with a
  marker inside) and refuses anything else.
- **HTTPS by default, never a password in clear text.** The one-liner serves
  `https://<your-ip-with-dashes>.sslip.io` with a free Let's Encrypt certificate when ports
  80 and 443 are free (80 redirects; `:8765` isn't served); with 443 taken, the same
  certificate on `:8443` (or the next free port up to 8453); with 80 taken, no public IP, or
  no certificate within ~120 s (a cloud firewall…) — HTTPS with a self-signed certificate on
  `:8765` (the browser warns once). It waits for the certificate itself and says in plain
  words which case you got, why, and how to get a trusted one (`install.sh --https`, with a
  Caddy log excerpt when Let's Encrypt failed). `--https` insists on a trusted certificate
  (it stops if port 80 is busy); `--https your-domain.com` (or
  `AGENTDECK_SITE=your-domain.com`) uses your own domain instead of sslip.io. Plain http only
  with an explicit `--http`. The mode is recorded in `~/.config/agentdeck/install.env` and
  kept on re-runs; an install from the earlier http-by-default installer moves to HTTPS on
  its next run. Test-only switches: `AGENTDECK_TLS_INTERNAL=1`, `AGENTDECK_ACME_CA`,
  `AGENTDECK_CERT_WAIT`.
- **New terminals open in `~/projects`,** not the whole home (Claude asks to trust that
  folder only). The installer creates it; `AGENTDECK_WORKDIR=/path` at install time picks
  another one, recorded and kept on re-runs. The Docker sandbox keeps `/work`.
- **Installer tests.** `tests/test_install_sh.py` (preflight, OS/arch, units, every HTTPS
  case and message, uninstall and purge rules — fast; it runs a copy of install.sh in a tmp
  sandbox where every outside command is a shim and any write outside the sandbox fails the
  test) and `tests/install/run.sh <distro>` (systemd containers: install, login, new
  terminal, reboot, re-run, uninstall); `SCENARIO=` picks the HTTPS case it meets
  (`cert-timeout`, `port80-busy`, `internal`, `internal-alt`, `http`), and `smoke.py` logs in
  and attaches to a terminal over https too (`--insecure`, `--connect`).
  `.github/workflows/install.yml` runs them on every push, plus `./install.sh --yes` on real
  Ubuntu 22.04 / 24.04 VMs.

### Terminals
- **Terminals keep ultracode (and max effort) across a restart:** AgentDeck reads from the
  terminal's transcript the effort it ended at and relaunches with it; other levels already
  persist through Claude Code's own default. The newest evidence wins: an `/effort` or
  `/model` output, the ultracode on/off notice Claude Code records whatever switched it
  (`/config`, Alt+P, Remote Control), and the effort each reply ran at.
  (`--settings '{"ultracode":true}'` or `--effort max` on the `--resume` launch only; the
  transcript is read backwards and the scan stops once the newest evidence settles it; any
  read problem means a plain `--resume`.)
- **Terminals in folders with emoji or very long paths resume their conversation** instead
  of starting a new one: the transcript folder name now follows Claude Code's own rule
  (UTF-16 code units; names over 200 characters are cut and get a hash suffix).
- **Mouse selection copies like a desktop terminal on a fresh install.** AgentDeck now ships
  its own `tmux.conf` (mouse mode, copy into tmux's buffer on mouse release — what the
  dashboard's copy reads — a 50,000-line scrollback, right click left to the browser); it
  used to live only in the author's `~/.tmux.conf`. Every tmux server AgentDeck starts reads
  it (`tmux -f <repo>/tmux.conf`, the Telegram bridge too); one that was already running
  gets it once. Your own `~/.tmux.conf` is sourced at the end, so it still applies on top.
- **"Copied · Ctrl+Shift+V to paste".** After a mouse selection in a terminal actually
  reaches the clipboard, a small chip in the terminal's top-right corner confirms it for
  ~1.5 s (⌘V on a Mac). After 5 copies in a browser it just says "Copied". Terminals only,
  not the Tasks or Server tabs.
- **A terminal waiting on its own timer stays loaded.** `install_guards.py` now also wires
  `hooks/hold_on_timer.py` (PostToolUse on `ScheduleWakeup`/`CronCreate`/`Monitor`), so the
  idle reaper no longer unloads an agent before its timer fires.

### Docker
- **`./https.sh` — HTTPS in one command, no domain needed.** Uses a free `<ip>.sslip.io` name
  (or `./https.sh your-domain.com`), writes `AGENTDECK_SITE` + a ports override into `.env`,
  checks 80/443 are free, waits for the Let's Encrypt certificate; `--off` reverts. The
  placeholder `you@example.com` ACME email is now dropped (Let's Encrypt refuses it).
- **Guard hooks on by default in Docker.** The container start merges
  `hooks/guard_task_board.py` + `hooks/guard_dont_stop.py` into the agents'
  `~/.claude/settings.json` (idempotent; other settings and your own hooks kept) and writes a
  short default `~/.claude/CLAUDE.md` with the board commands if none exists — every task an
  agent takes on goes on the board, and it keeps working while the task is open. Opt out:
  `AGENTDECK_GUARDS=0`. `start.sh` leaves your own `~/.claude` alone unless
  `AGENTDECK_GUARDS=1`. New `hooks/install_guards.py` (install / `--remove` / `--check`).

### README
- **Full control or a sandbox.** "Try it in a minute" is the one-line install (full
  control of the server, best on a dedicated VPS); "Try it in a sandbox (server control
  isn't available in this mode)" is Docker, with its limits spelled out. Quick start has
  both, in that order.
- **Leads with a demo animation** (`docs/demo.gif`, `docs/demo.mp4`), then **Why AgentDeck**
  — including "Sessions survive restarts" (browser close, AgentDeck restart, server reboot;
  ultracode included) and "Copy & paste like a desktop terminal" — a one-minute try, and an
  honest comparison table. Social preview image `docs/social-preview.png`.

### Tests
- The whole test run gets a private tmux server (`TMUX_TMPDIR` set in `tests/conftest.py`),
  so no test can list, kill or type into running terminals, and the run stops at once if a
  test deletes the session registry (`.sessions/library.json`).

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
