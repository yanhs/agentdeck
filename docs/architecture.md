# Architecture

[← back to the README](../README.md)

AgentKennel is plain Python and Bash around three programs you already know: `tmux` keeps
every agent alive, `ttyd` shows a tmux session in the browser, and Claude Code runs inside.

```
                   ┌───────────── Caddy (install.sh, Docker) or nginx ─────────────┐
 Browser ──TLS──▶  │ login check    ──▶  status_server.py  login, signed cookie    │
                   │ /              ──▶  web/index.html    dashboard UI            │
                   │ /api/library   ──▶  status_server.py  new, rename, archive …  │
                   │ /api/*         ──▶  status_server.py  status, paste, upload   │
                   │ /tasks/        ──▶  tasks-dashboard/  task board :9308        │
                   │ /sess/?arg=ID  ──▶  ttyd :3031        → open-session.sh       │
                   └───────────────────────────────┬───────────────────────────────┘
                                                   │  library_cli.py loads tmux "cs-<code>"
                                                   ▼
                                          ┌─────────────────┐
 Telegram ◀─▶ tg_bridge.py ─ send-keys ──▶│   claude (CLI)  │
                   ▲                      └────────┬────────┘
                   └───── transcript (.jsonl) ─────┘

 every minute:  idle_reaper.py ──▶ unloads idle terminals
                (a systemd timer with install.sh; Docker and ./start.sh run it themselves)
 always on:     services_keeper.py ──▶ runs <work folder>/.services/<name>.sh
```

## The pieces

- **`library.py`** — the session library: the list of terminals in
  `.sessions/library.json` (name, code, Claude session id, order, archived).
- **`convo_sync.py`** — keeps a terminal's code equal to the conversation live in it:
  reads the file Claude keeps per running process (`~/.claude/sessions/<pid>.json`), finds
  each terminal's Claude through the process tree (in the pane AgentKennel started — not in
  a pane split off later), and after a `/clear`, `/resume` or the permissions-prompt
  restart moves the registry entry and renames `cs-<old>` to `cs-<new>`. The status server
  (every list refresh), `library_cli.py` and the idle reaper run it before they act on a
  terminal.
- **`open-session.sh` + `library_cli.py`** — one `ttyd` serves every terminal. The page
  `/sess/?arg=<code>` checks the code, loads the terminal into `tmux` if needed (unloading
  an idle one at the limit), and attaches the tab. `/sess/?arg=shell` is the `cmd` line.
  `library_cli.py` also builds Claude's command line: `claude --resume <id>` (or
  `--session-id` for a new one) with `--dangerously-skip-permissions`, plus the effort the
  conversation ended at.
- **`bin/pane`** — what tmux runs in a new terminal: it enters the terminal's folder,
  loads your login shell's environment and starts claude with the arguments
  `library_cli.py` prepared (left in `.sessions/launch/` next to it). The tmux server
  keeps, as its own, the command line and working directory of the call that started it,
  and a careless `pkill -f grep` (or `bash`, `env`, `python`, the checkout's path) or
  `fuser -k <folder>` somewhere on the server must not match it and take every terminal
  down. So the server is started with nothing on that line —
  `tmux -f /dev/null new-session -d -s hold-<hex> tmux wait-for hold-<hex>`, from `/` —
  then `tmux.conf` is loaded into it, and only then the terminal is made
  (`-c <folder>`, `bin/pane <code>`).
- **The agents' tmux server** is their own, in `~/agentkennel/.sessions/tmux`; your own tmux
  sessions are never touched. To look at it over SSH:
  `TMUX_TMPDIR=~/agentkennel/.sessions/tmux tmux ls`.
- **`idle_reaper.py`** — unloads terminals nobody is using.
- **`services_keeper.py`** — starts every `<work folder>/.services/<name>.sh`, restarts it
  after a crash with a growing pause, keeps its log, and stops it with its child processes
  when the file is removed (at most 10 services). A small API on `127.0.0.1:9876`.
- **`status_server.py`** — the login gate, the library/status/buffer/paste/services APIs,
  the Server page's data and the Telegram setup page. One process; it listens on
  `127.0.0.1` ports 3011 (main API), 3014, 3045, 3046 (login) and 3047 (image paste, 📎 File uploads).
- **`web/index.html`** — the dashboard front-end; `web/server.html` is the Server page.
- **`tg_bridge.py`** — the Telegram ⇄ tmux bridge (+ `whisper_transcribe.py` for voice).
- **`tasks-dashboard/`** — the shared task board and its CLI, `tracker.py` (own README
  inside).
- **`hooks/`** — the Claude Code hooks: the board guards, `hold_on_timer.py` and the three
  safety guards.

## Project layout

```
install.sh               one-command install on your own server (systemd services, HTTPS)
https.sh                 HTTPS for the Docker sandbox in one command
start.sh                 the whole stack without Docker or systemd, behind Caddy
tmux.conf                the agents' tmux settings (mouse, copy); your ~/.tmux.conf applies after it
library.py               session library: the list of named terminals
convo_sync.py            a terminal keeps the code of the conversation live in it (/clear, /resume)
library_cli.py           loads/unloads terminals in tmux (used by ttyd and the bridge)
bin/pane                 what tmux runs in a new terminal (keeps the tmux server's command line neutral)
open-session.sh          the one ttyd entry point: /sess/?arg=<code>
sessions.pm2.config.js   PM2 app for that ttyd
idle_reaper.py           unloads idle terminals (every minute)
services_keeper.py       keeps services running after their terminal is unloaded
migrate_library.py       moves old numbered slots into the library
hooks/hold_on_timer.py   Claude Code hook: a pending timer keeps its terminal loaded
hooks/guard_*.py         Claude Code hooks: board before edits, no silent mid-task stop,
                         signals only to your own processes, no mail or secrets without you
status_server.py         login gate + library/status/buffer/paste APIs + Telegram page
web/                     dashboard front-end
tg_bridge.py             Telegram ⇄ tmux bridge
whisper_transcribe.py    voice-note transcription helper
Caddyfile, docker/       the proxy for install.sh / start.sh, and the Docker image's files
nginx/                   a reverse-proxy + login template for the manual setup
tasks-dashboard/         shared live task board
tests/                   pytest suite
AGENTS.md                an example set of operating rules for agents
```
