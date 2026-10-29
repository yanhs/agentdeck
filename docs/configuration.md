# Configuration

[← back to the README](../README.md)

AgentKennel needs no config file to start. Everything below is optional.

With `install.sh`, put settings in `~/agentkennel/.env` (the systemd units read it, and your
values win), then run the installer again or `sudo systemctl restart 'agentkennel-*'`. With
Docker, put them in a `.env` next to `docker-compose.yml`.

| What | Where | Notes |
|---|---|---|
| Terminals (names, codes, order, archive) | `.sessions/library.json` | written by the dashboard; `AGENTKENNEL_LIBRARY` moves it |
| A terminal's start, for `bin/pane` | `.sessions/launch/<code>` of the checkout | read once and removed; always next to `bin/pane` (`AGENTKENNEL_LIBRARY` does not move it: a pane's environment is the tmux server's) |
| How many terminals stay loaded | `AGENTKENNEL_MAX_ACTIVE` env | default `12` |
| Working directory | `AGENTKENNEL_WORKDIR` env | where new terminals start; `install.sh`: `~/projects` (set it when installing to change it; re-runs keep it), Docker: `/work`, otherwise the directory above the repo |
| Dashboard port (Docker) | `AGENTKENNEL_PORT` env | default `8765` |
| Your own domain | `--https your-domain.com` or `AGENTKENNEL_SITE` env | its A record must point at the server |
| Dashboard address (for POST checks) | `AGENTKENNEL_ORIGIN` env | default: the request's own scheme + Host (as the proxy forwards them); set e.g. `https://agents.example.com` behind a proxy without `X-Forwarded-Proto`; library changes from any other origin are refused |
| Dashboard password | `.sessions/.dashpass` (`AGENTKENNEL_PASSFILE`) | set once through the one-time setup link; change it with **⋯ → Password**; `AGENTKENNEL_PASSWORD` pre-seeds it |
| Idle reaper timings | `REAPER_IDLE_SECONDS`, `REAPER_ARCHIVED_IDLE_SECONDS`, `REAPER_BG_MAX_SECONDS` | defaults 7200 / 120 / 86400 |
| Guard hooks (task board before edits, no silent mid-task stop, safety guards) | `AGENTKENNEL_GUARDS` env | Docker: on by default, `0` turns them off; `install.sh`: on by default, `0` skips them; `start.sh`: `1` installs them into `~/.claude/settings.json` |
| This server's public IPs, for `guard_secrets.py` | `AGENTKENNEL_PUBLIC_IPS` env or `~/.claude/agentkennel-public-ips` | needed in Docker, where the network interfaces are private |
| Claude Code version | `CLAUDE_CODE_VERSION` | pinned by the installer and the Dockerfile |
| Telegram bridge | **⋯ → Telegram** in the dashboard, or `.env` | see [Telegram](telegram.md) |
| Pasted images (Ctrl+V / ⌘V) | `AGENTKENNEL_PASTE_DIR`, `AGENTKENNEL_PASTE_URL` env | default `.sessions/paste`, no public URL; the terminal gets the file's path either way |
| Uploaded files (📎 File / drop) | `AGENTKENNEL_UPLOAD_DIR`, `AGENTKENNEL_UPLOAD_MAX_MB` env | default `.sessions/uploads/<date>/`, private (0700/0600), 100 MB per file; never served on the web |
| Pretty project names | `PROJECT_MAP` in `status_server.py` | optional, cosmetic |
| Friendly names on the Server page | `~/.config/agentkennel/server-names.json` (`AGENTKENNEL_SERVER_NAMES`) | optional, cosmetic; see below |
| Reverse proxy / TLS (manual setup) | `nginx/agents-subdomain.conf` | a template: swap in your domain and paths, see [Manual setup](manual-setup.md) |

**Server page names.** The Server page names AgentKennel's own units and a few common
ones (nginx, PostgreSQL, Docker, …); everything else shows its systemd description or
Docker image. To name your own, write `server-names.json`:
`{"units": {"shop-api.service": ["Shop API", null, "apps"]}, "containers": [["shop-(\\w+)", "Shop web ({1})"]]}`.
A unit is `["Name", "what it does" or null for the unit's own description, "apps" | "system" | "jobs"]`;
a container is `["<regex for the whole name>", "description"]`, where `{1}`, `{2}` are the
regex groups. Your units win over the built-in ones and your container patterns are tried
first. It is read when the backend starts; a broken file is ignored with one line in its log.
