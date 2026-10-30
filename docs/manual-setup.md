# Advanced: manual setup behind your own nginx

[← back to the README](../README.md)

Most people should use the one-line installer or the Docker sandbox from the
[README](../README.md#-quick-start). This page is for running the pieces yourself — for
example on a server that already has nginx and a certificate for your domain.

## Requirements

- Linux, **Python 3.11+**
- [`tmux`](https://github.com/tmux/tmux) and [`ttyd`](https://github.com/tsl0922/ttyd)
- The **Claude Code CLI** (`claude`) — https://docs.claude.com/claude-code
- a reverse proxy for TLS, the login gate and WebSockets: `nginx` (this page) or
  [Caddy](https://caddyserver.com/download) (`./start.sh`)
- *(optional, for the Telegram bridge)* `python-telegram-bot`
- *(optional, for Telegram voice notes)* `faster-whisper`, `ffmpeg`

## The short way: `./start.sh` (Caddy, no systemd)

```bash
git clone https://github.com/yanhs/agentkennel.git && cd agentkennel
./start.sh                       # → http://<server>:8765; AGENTKENNEL_SITE=your-domain for HTTPS
```

It starts the status server, the task board, one ttyd, the idle reaper, the services keeper
and Caddy, and
prints the one-time setup link for the password. Guard hooks are not installed into your
own `~/.claude/settings.json` unless you ask: `AGENTKENNEL_GUARDS=1 ./start.sh`.

## Behind your own nginx

> `claude` is found on your `PATH`, agents start in `$AGENTKENNEL_WORKDIR` (default: the
> directory above the repo), and every terminal's conversation is kept under `.sessions/`.
> There is no agent list to edit by hand: terminals are created, renamed, archived and
> trashed in the dashboard (**＋ New**), which writes `.sessions/library.json`.

```bash
git clone https://github.com/yanhs/agentkennel.git && cd agentkennel

# 1) the backend — ONE process serves every dashboard API
#    (library, status, login, tmux-buffer, page-version, paste-image, upload, /telegram).
#    AGENTKENNEL_PASSFILE: the dashboard keeps its own password (so ⋯ → Password works).
#    AGENTKENNEL_ORIGIN is only needed when the proxy doesn't pass the original Host +
#    X-Forwarded-Proto; unset = the request's own scheme + Host.
export AGENTKENNEL_PASSFILE=$PWD/.sessions/.dashpass
mkdir -p .sessions
AGENTKENNEL_ORIGIN=https://agents.example.com python3 status_server.py &

# 2) ONE ttyd for every terminal: /sess/?arg=<code> runs open-session.sh <code>
#    (-a passes ?arg= to the script, -O checks the WebSocket origin)
ttyd -W -a -O -i lo -p 3031 --base-path /sess bash ./open-session.sh &
#    or with PM2:  pm2 start sessions.pm2.config.js && pm2 save

# 3) the task board (the T button)
python3 tasks-dashboard/server.py &

# 4) the idle reaper — once a minute from cron (crontab -e):
#    * * * * * cd /path/to/agentkennel && python3 idle_reaper.py >> idle_reaper.log 2>&1

# 5) the services keeper — programs that outlive a terminal (<work folder>/.services/)
python3 services_keeper.py &

# 6) nginx in front — TLS + the cookie login gate + one origin.
#    Browser terminals are WebSockets, so a reverse proxy is required.
#    nginx/agents-subdomain.conf is a full vhost template: /sess/, /api/library,
#    /api/*, /api/upload, /telegram, /change-password, /tasks/ and the dashboard.
#    Uploads need `client_max_body_size 0;` in /__auth (the login probe sees every
#    request's body size) and 101m on /api/upload — both are in the template.
```

Copy `nginx/agents-subdomain.conf` to `/etc/nginx/sites-available/`, replace
`agents.example.com` and `/home/deck/agentkennel` with your domain and checkout, point
`ssl_certificate` / `ssl_certificate_key` at your certificate (the comments in the file say
where), enable it and reload nginx.

### The first password

With `AGENTKENNEL_PASSFILE` set and the file still empty, only a one-time link can set the
password, so a stranger who finds the server first can't. Print it on the server, with the
same `AGENTKENNEL_PASSFILE` in the environment:

```bash
python3 status_server.py --setup-link     # → /login?setup=<token>
```

and open `https://agents.example.com/login?setup=<token>`. Setting the password deletes
the token. To pre-seed it instead, put the password in the file the way `start.sh` does
with `AGENTKENNEL_PASSWORD`.

Without `AGENTKENNEL_PASSFILE`, the login checks an nginx-style htpasswd file instead
(`/etc/nginx/.htpasswd_agents`, or `AGENTKENNEL_HTPASSWD`):

```bash
sudo htpasswd -c /etc/nginx/.htpasswd_agents <your-username>
```

If the passfile is set but empty and that htpasswd file exists, the first correct login
copies the password into the passfile.

### Running it for real

Run `status_server.py`, the ttyd and `tg_bridge.py` as systemd services —
`claude-tg-bridge.service` is a template. `install.sh` writes the same set of units
(`agentkennel-status`, `-tasks`, `-sessions`, `-services`, `-caddy`, the `agentkennel-reaper`
timer); reading them is a quick way to see the exact command lines.

## Hooks

**Keep a terminal loaded while a timer is pending.** `hooks/hold_on_timer.py` is a
`PostToolUse` hook: when an agent sets a `ScheduleWakeup`, the terminal is marked busy
until 10 minutes after the wake-up time (a `CronCreate` or `Monitor`: for 6 hours), so
neither the memory limit nor the idle reaper unloads it and kills the timer.

**Task-board hooks** (on by default in Docker and with `install.sh`; opt out with
`AGENTKENNEL_GUARDS=0`), plain Python with no dependencies:

- `guard_task_board.py` blocks the first file edit of a session until the work is on the
  task board (any `tracker.py` call). For a genuinely trivial change the agent runs
  `NO_BOARD=1 true` instead, which leaves a visible trace. Temp directories and
  `~/.claude/` are never blocked.
- `guard_dont_stop.py` stops an agent from ending its turn in the middle of a task it put
  on the board with nothing scheduled to wake it up again. It lets the turn end once there
  is a background command, a timer (`CronCreate` / `ScheduleWakeup`), a `Monitor` or a
  sub-agent from the last 10 minutes, or once the task is closed or marked `stopped`. Only
  tasks this session created or moved count, and it never blocks more than twice in a row.

The three safety guards are described in the README's
[Safety](../README.md#-safety-what-agents-never-do-without-you) section.

**Where they go.** In Docker the container start merges the hooks into the agents'
`~/.claude/settings.json` (your other settings and hooks are kept) and writes a short
default `~/.claude/CLAUDE.md` if there is none; `install.sh` does the same in your own
`~/.claude` (`--uninstall` takes them out again). With `./start.sh` they would go into
your own `~/.claude/settings.json`, which every Claude Code session on the machine reads,
so `start.sh` only prints a hint; run `AGENTKENNEL_GUARDS=1 ./start.sh` (or
`python3 hooks/install_guards.py`) to install them, `python3 hooks/install_guards.py
--remove` to take them out. Or copy the `hooks` block from `hooks/settings.example.json`
into one project's `.claude/settings.json` and replace `/path/to/agentkennel` with where you
cloned the repo.

**Settings** (environment variables): `TRACKER_STATE` (the board file, same default as
`tracker.py`), `AGENTKENNEL_TRACKER` (path to `tracker.py`), `AGENTKENNEL_BOARD_URL` (a board
link to show in the messages) and `AGENTKENNEL_BOARD_MARKS` / `AGENTKENNEL_STOP_MARKS`
(per-session marker folders, default under `$TMPDIR`). If a hook hits an error of its own,
it lets the action through.

All other settings: [Configuration](configuration.md).
