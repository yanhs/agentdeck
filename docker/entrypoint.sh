#!/usr/bin/env bash
# Start the AgentDeck stack inside the container — the same layout as production
# (nginx/agents-subdomain.conf): backend (status_server: all API ports + the session
# library API) + the task board + ONE ttyd for the session library (/sess/) + the idle
# reaper (once a minute) + Caddy (login gate + automatic HTTPS) in front.
# The dashboard password is set on first visit (no env needed) and stored in the volume.
set -uo pipefail
cd /app

# A UTF-8 locale so the tmux client renders Cyrillic / box-drawing instead of "?".
# (The Dockerfile sets this as ENV too; this also covers a non-rebuilt image. ttyd runs a
# non-login bash that sources no profile, so the locale must be a real env var on the tree.)
export LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}"

# the login password + the cookie-signing key both live in the persisted sessions volume,
# so they survive a container recreate and the key is never baked into the image
export AGENTDECK_PASSFILE="${AGENTDECK_PASSFILE:-/app/.sessions/.dashpass}"
export AGENTDECK_AUTH_SECRET="${AGENTDECK_AUTH_SECRET:-/app/.sessions/.agents_auth_secret}"
# task board state lives in the volume too (survives recreate; author's tasks not baked in)
export TRACKER_STATE="${TRACKER_STATE:-/app/.sessions/tasks-state.json}"

# OPTIONAL: pre-seed the password from an env var (otherwise set it on first visit)
if [ -n "${AGENTDECK_PASSWORD:-}" ] && [ ! -s "$AGENTDECK_PASSFILE" ]; then
  python3 - "$AGENTDECK_PASSFILE" "$AGENTDECK_PASSWORD" <<'PY'
import sys, os, hashlib
path, pw = sys.argv[1], sys.argv[2]
salt = os.urandom(16)
h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
os.umask(0o077); open(path, "w").write(f"{salt.hex()}${h.hex()}")
PY
  echo "[agentdeck] dashboard password seeded from AGENTDECK_PASSWORD"
fi
[ -s "$AGENTDECK_PASSFILE" ] && echo "[agentdeck] dashboard password is set" \
  || echo "[agentdeck] no password yet — open the dashboard and set one on first visit"

# ~/.claude.json (claude's home-level config) is NOT inside the ~/.claude volume on its own —
# keep it persisted: store it in the volume and symlink it back, so a restart doesn't lose the
# login/onboarding (otherwise claude reports "configuration file not found").
[ -f /root/.claude.json ] && [ ! -L /root/.claude.json ] && mv -f /root/.claude.json /root/.claude/.claude.json
ln -sfn /root/.claude/.claude.json /root/.claude.json

echo "[agentdeck] backend: status_server.py"
python3 status_server.py &
sleep 1

# task board (the 📋 Tasks button in the dashboard) on :9308, behind the same login
[ -f "$TRACKER_STATE" ] || echo '{"title":"Task Tracker","tasks":[]}' > "$TRACKER_STATE"
echo "[agentdeck] task board: tasks-dashboard/server.py"
python3 tasks-dashboard/server.py &

# Session library: ONE ttyd for every topic. The dashboard opens /sess/?arg=<8-hex id>
# (or ?arg=shell); -a hands ?arg= to open-session.sh, which validates it and attaches the
# tab to the topic's tmux session (loading it first). -O: the websocket's Origin must equal
# its Host (Caddy passes Host through). Localhost only — Caddy fronts it behind the login.
echo "[agentdeck] sessions ttyd :3031 (/sess) -> open-session.sh"
ttyd -W -a -O -i lo -p 3031 --base-path /sess bash /app/open-session.sh &

# idle reaper: unloads topics nobody uses (no cron in the container — a loop instead)
echo "[agentdeck] idle reaper: idle_reaper.py once a minute"
( while true; do python3 idle_reaper.py >> /app/.sessions/idle_reaper.log 2>&1; sleep 60; done ) &

CADDYFILE=/app/docker/Caddyfile
# AGENTDECK_SITE=https://... (a port or IP, no real domain) → self-signed HTTPS:
# generate a certificate on first run (kept in the volume) and serve with it.
case "${AGENTDECK_SITE:-}" in
  https://*)
    CD=/app/.sessions/tls; mkdir -p "$CD"
    if [ ! -s "$CD/cert.pem" ]; then
      openssl req -x509 -newkey rsa:2048 -keyout "$CD/key.pem" -out "$CD/cert.pem" \
        -days 3650 -nodes -subj "/CN=agentdeck" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null
      echo "[agentdeck] generated a self-signed TLS certificate (first run)"
    fi
    CD="$CD" python3 -c 'import os;cd=os.environ["CD"];p="/app/docker/Caddyfile";s=open(p).read().replace("{$AGENTDECK_SITE::8765} {","{$AGENTDECK_SITE::8765} {\n\ttls "+cd+"/cert.pem "+cd+"/key.pem");open("/tmp/Caddyfile","w").write(s)'
    CADDYFILE=/tmp/Caddyfile
    ;;
esac
echo "[agentdeck] caddy -> ${AGENTDECK_SITE:-:8765}  (domain=auto-HTTPS, https://:port=self-signed)"
exec caddy run --config "$CADDYFILE" --adapter caddyfile
