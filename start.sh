#!/usr/bin/env bash
# Start the whole AgentDeck stack WITHOUT Docker, in one command:
#   1. status_server.py   — backend (all API ports, the session library API) + the login gate
#   2. the task board      — tasks-dashboard/server.py on :9308 (/tasks/)
#   3. ONE ttyd            — the session library on 127.0.0.1:3031 (/sess/ -> open-session.sh)
#   4. idle_reaper.py      — once a minute (a loop here; drop your cron line if you had one)
#   5. Caddy               — proxy + login, on :8765 (or a domain for HTTPS)
#
# The dashboard exposes live terminals, so it's behind a login. You SET the password
# the first time you open it in the browser (or pre-seed it: echo 'AGENTDECK_PASSWORD=...' >> .env).
# Optional: AGENTDECK_SITE=your-domain -> automatic HTTPS (default ":8765", http/local).
#
# Needs: python3, tmux, ttyd, the `claude` CLI, and `caddy`.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]:-$0}")"

[ -f .env ] && { set -a; . ./.env; set +a; }

# A UTF-8 locale so the tmux client renders Cyrillic / box-drawing instead of "?"
# (respects an already-set LANG, e.g. from your shell or .env).
export LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}"

if ! command -v caddy >/dev/null 2>&1; then
  echo "[agentdeck] need 'caddy' (one binary, no sudo): https://caddyserver.com/download" >&2
  exit 1
fi

mkdir -p .sessions
export AGENTDECK_PASSFILE="${AGENTDECK_PASSFILE:-$PWD/.sessions/.dashpass}"
# cookie-signing key in .sessions too, so logins survive restarts (kept out of the image/git)
export AGENTDECK_AUTH_SECRET="${AGENTDECK_AUTH_SECRET:-$PWD/.sessions/.agents_auth_secret}"
export TRACKER_STATE="${TRACKER_STATE:-$PWD/.sessions/tasks-state.json}"  # task board state in the volume
# OPTIONAL: pre-seed the password from an env var (else set it on first visit)
if [ -n "${AGENTDECK_PASSWORD:-}" ] && [ ! -s "$AGENTDECK_PASSFILE" ]; then
  python3 - "$AGENTDECK_PASSFILE" "$AGENTDECK_PASSWORD" <<'PY'
import sys, os, hashlib
p, pw = sys.argv[1], sys.argv[2]
salt = os.urandom(16); h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
os.umask(0o077); open(p, "w").write(f"{salt.hex()}${h.hex()}")
PY
fi
[ -s "$AGENTDECK_PASSFILE" ] && echo "[agentdeck] dashboard password is set" \
  || echo "[agentdeck] no password yet — open the dashboard and set one on first visit"

# Guard hooks (task board before edits, no silent mid-task stop): ON by default in Docker.
# Here they would go into YOUR ~/.claude/settings.json, which every Claude Code session on
# this machine reads — so only with AGENTDECK_GUARDS=1 (e.g. in .env); otherwise a hint.
if [ "${AGENTDECK_GUARDS:-}" = 1 ]; then
  python3 hooks/install_guards.py --tracker-state "$TRACKER_STATE" \
    --claude-md "$HOME/.claude/CLAUDE.md" \
    && echo "[agentdeck] guard hooks: on in ~/.claude/settings.json"
elif [ "${AGENTDECK_GUARDS:-}" != 0 ] && ! python3 hooks/install_guards.py --check; then
  echo "[agentdeck] tip: AGENTDECK_GUARDS=1 ./start.sh keeps agents on the task board (guard hooks; AGENTDECK_GUARDS=0 hides this)"
fi

pids=()
cleanup() { echo; echo "[agentdeck] stopping…"; kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "[agentdeck] backend: status_server.py"
python3 status_server.py & pids+=($!)
sleep 1

# task board (the 📋 Tasks button) on :9308, behind the same login
[ -f "$TRACKER_STATE" ] || echo '{"title":"Task Tracker","tasks":[]}' > "$TRACKER_STATE"
echo "[agentdeck] task board: tasks-dashboard/server.py"
python3 tasks-dashboard/server.py & pids+=($!)

# Session library: ONE ttyd for every topic (/sess/?arg=<8-hex id> or ?arg=shell ->
# open-session.sh). -a passes ?arg= to the script; -O: websocket Origin must equal Host.
echo "[agentdeck] sessions ttyd :3031 (/sess) -> open-session.sh"
ttyd -W -a -O -i lo -p 3031 --base-path /sess bash "$PWD/open-session.sh" & pids+=($!)

# idle reaper: unloads topics nobody uses, once a minute
echo "[agentdeck] idle reaper: idle_reaper.py once a minute"
( while true; do python3 idle_reaper.py >> .sessions/idle_reaper.log 2>&1; sleep 60; done ) & pids+=($!)

CADDYFILE=Caddyfile
# AGENTDECK_SITE=https://... (a port/IP, not a real domain) → self-signed HTTPS:
# generate a certificate on first run (kept in .sessions/) and serve with it.
case "${AGENTDECK_SITE:-}" in
  https://*)
    CD=.sessions/tls; mkdir -p "$CD"
    if [ ! -s "$CD/cert.pem" ]; then
      openssl req -x509 -newkey rsa:2048 -keyout "$CD/key.pem" -out "$CD/cert.pem" \
        -days 3650 -nodes -subj "/CN=agentdeck" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null
      echo "[agentdeck] generated a self-signed TLS certificate (first run)"
    fi
    CD="$CD" python3 -c 'import os;cd=os.environ["CD"];s=open("Caddyfile").read().replace("{$AGENTDECK_SITE::8765} {","{$AGENTDECK_SITE::8765} {\n\ttls "+cd+"/cert.pem "+cd+"/key.pem");open(".sessions/Caddyfile.gen","w").write(s)'
    CADDYFILE=.sessions/Caddyfile.gen
    ;;
esac
echo "[agentdeck] caddy -> ${AGENTDECK_SITE:-:8765}"
caddy run --config "$CADDYFILE" --adapter caddyfile & pids+=($!)
wait
