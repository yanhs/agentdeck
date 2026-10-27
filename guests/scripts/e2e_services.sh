#!/usr/bin/env bash
# End-to-end check of sandbox services on a THROWAWAY guest (default "probe"):
# a real container from the new image, the real sandbox network, the host's view.
#   scripts/e2e_services.sh [name]        KEEP=1 — leave the guest for a look afterwards
# Prints PASS/FAIL per check; exit code = number of failures.
set -uo pipefail
G="${1:-probe}"; C="sbx-$G"; IMG="${IMG:-agentdeck-guest:v1.9.0-svc}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
PASS=0; FAIL=0
ok()  { echo "PASS  $*"; PASS=$((PASS+1)); }
bad() { echo "FAIL  $*"; FAIL=$((FAIL+1)); }
x()   { docker exec "$C" "$@"; }
st()  { x curl -s --max-time 3 localhost:9876/status; }
# field <service> <key>  -> value from the keeper's status (or "None")
field() { st | python3 -c "import json,sys
try: d=json.load(sys.stdin)['services']
except Exception: d={}
print(d.get('$1',{}).get('$2'))"; }
# waitfor <seconds> <command...>  -> 0 as soon as the command succeeds
waitfor() { local n="$1"; shift; local i; for i in $(seq 1 "$((n*2))"); do "$@" >/dev/null 2>&1 && return 0; sleep 0.5; done; return 1; }
is() { [ "$(field "$1" "$2")" = "$3" ]; }
http_up() { [ "$(x curl -s -o /dev/null -w '%{http_code}' --max-time 2 "localhost:$1/")" = 200 ]; }
port_closed() { ! x curl -s -o /dev/null --max-time 1 "localhost:$1/"; }
restarted() { local r; r="$(field "$1" restarts)"; [ "$r" != None ] && [ "$r" -ge 1 ] 2>/dev/null; }
pid_changed() { local p; p="$(field "$1" pid)"; [ -n "$p" ] && [ "$p" != None ] && [ "$p" != "$2" ]; }

case "$G" in yan|g222222222) echo "refusing: $G is a real guest"; exit 99 ;; esac

# 0. the throwaway guest (Telegram id 1 = nobody; no Claude calls are made)
if ! docker inspect "$C" >/dev/null 2>&1; then
  python3 guestctl.py add "$G" --tg 1 --limit 1000000 --image "$IMG" || { echo "add failed"; exit 98; }
fi
IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$C")"
echo "guest $G at $IP, image $(docker inspect -f '{{.Config.Image}}' "$C")"

# 1. the keeper comes up with the sandbox, the limit and the agent's rule are in place
waitfor 30 st && ok "keeper answers on :9876" || bad "keeper does not answer"
[ "$(x printenv SERVICES_MAX)" = 10 ] && ok "SERVICES_MAX=10 in the sandbox" || bad "SERVICES_MAX"
x grep -q "Не больше 10 служб" /root/.claude/CLAUDE.md && ok "services rule in CLAUDE.md" || bad "services rule missing"
[ "$(x cat /proc/1/comm)" = docker-init ] && ok "init process is PID 1" || bad "no init: $(x cat /proc/1/comm)"

# 2. a service starts, writes its log, serves
x bash -c 'printf "%s\n" "echo web starting" "exec python3 -m http.server 8080 --bind 0.0.0.0 --directory /work/public" > /work/.services/web.sh'
waitfor 15 is web state running && ok "service starts" || bad "service not running: $(field web state)"
waitfor 10 http_up 8080 && ok "service serves :8080" || bad "service not serving"
x grep -q "web starting" /work/.services/web.log && ok "service log written" || bad "no log"

# 3. the host sees it the way the Telegram relay does, and the buttons work
python3 -I - "$G" "$IP" <<'PY' && ok "relay: /services text + restart button" || bad "relay view/button"
import sys, time
sys.path[:0] = ["tgrelay", "turnstile"]
import tgrelay as tr, turnstile as ts
guest, ip = sys.argv[1], sys.argv[2]
client = tr.KeeperClient(lambda g: f"http://{ip}:9876")
before = client.status(guest)["web"]["pid"]
class TG:
    calls = []
    def call(self, method, params, files=None):
        self.calls.append((method, params)); return {"ok": True, "result": {"message_id": 1}}
tg = TG()
book = ts.GuestBook(__import__("pathlib").Path("turnstile/data/guests.json"))
relay = tr.Relay(book, tg, services=client, background=False)
g = book.get(guest)
relay.route({"update_id": 1, "message": {"message_id": 1, "from": {"id": g.telegram_id},
             "chat": {"id": g.telegram_id}, "text": "/services"}})
text = [p["text"] for m, p in tg.calls if m == "sendMessage"][0]
print("   ", text.replace("\n", "\n    "))
assert "🟢 web — работает" in text, text
relay.route({"update_id": 2, "callback_query": {"id": "c1", "from": {"id": g.telegram_id},
             "data": "svc:restart:web", "message": {"message_id": 1, "chat": {"id": g.telegram_id}}}})
after = client.status(guest)["web"]["pid"]
assert after and after != before, (before, after)
PY

# 4. a crashing service is restarted, with its exit code shown
x bash -c 'printf "%s\n" "echo boom" "exit 3" > /work/.services/crash.sh'
waitfor 30 is crash last_exit 3 && ok "crash seen (exit 3)" || bad "crash not seen"
waitfor 30 restarted crash && ok "crash restarted ($(field crash restarts))" || bad "no restart"
x rm -f /work/.services/crash.sh
waitfor 10 is crash state None && ok "removed file = service gone" || bad "crash still listed"

# 5. stop / start by name (what the buttons call)
x curl -s -X POST --max-time 3 localhost:9876/stop/web >/dev/null
waitfor 10 is web state stopped && ok "stop -> stopped" || bad "stop"
waitfor 10 port_closed 8080 && ok "stopped = port closed" || bad "still serving after stop"
x curl -s -X POST --max-time 3 localhost:9876/start/web >/dev/null
waitfor 10 is web state running && ok "start -> running" || bad "start"

# 6. the keeper itself is killed hard (as by the OOM killer): it comes back and the
#    service does NOT run twice (a SIGTERM would let it stop its services itself)
waitfor 10 http_up 8080
KP="$(x ps -eo pid=,args= | awk '$2=="python3" && $3=="/opt/sbx/keeper.py" {print $1}')"
OLDWEB="$(field web pid)"
if [ -n "$KP" ]; then
  x kill -9 "$KP"
  waitfor 25 pid_changed web "$OLDWEB" && ok "keeper back, web restarted under it" \
    || bad "keeper did not come back cleanly"
  waitfor 10 http_up 8080
  N="$(x ps -eo args= | awk '$1=="python3" && $2=="-m" && $3=="http.server"' | wc -l)"
  [ "$N" = 1 ] && ok "web runs once" || bad "web runs $N times"
  # "once" alone would also pass if the OLD copy survived and the new one crash-loops
  # on the busy port — so: the old process is gone and the new one is not restarting
  x sh -c "[ ! -d /proc/$OLDWEB ] || grep -q '^State:.*Z' /proc/$OLDWEB/status" \
    && ok "old web process is gone (leftover stopped)" || bad "old web $OLDWEB still alive"
  sleep 6
  [ "$(field web state)" = running ] && [ "$(field web restarts)" = 0 ] \
    && ok "new web stays up (no crash loop)" || bad "new web: $(field web state), restarts $(field web restarts)"
else
  bad "keeper pid not found"
fi

# 7. survives a sandbox restart (the whole point: no terminal needed)
docker restart "$C" >/dev/null
waitfor 40 is web state running && waitfor 15 http_up 8080 && ok "back after sandbox restart" || bad "not back after restart"

# 8. walls: another sandbox can't reach this keeper; no zombie pile-up
python3 -I - "$IP" <<'PY' && ok "host reaches the keeper" || bad "host can't reach keeper"
import sys, urllib.request
urllib.request.urlopen(f"http://{sys.argv[1]}:9876/status", timeout=3).read()
PY
OTHER=172.31.250.11
x python3 -c "
import socket,sys
s=socket.socket(); s.settimeout(3)
try: s.connect(('$OTHER', 8765)); sys.exit(1)
except OSError: sys.exit(0)" && ok "other sandbox ($OTHER) unreachable" || bad "other sandbox reachable!"
Z="$(x ps -eo stat= | grep -c '^Z')"; [ "$Z" = 0 ] && ok "no zombies" || bad "$Z zombies"

echo "== $PASS passed, $FAIL failed"

# 9. clean up the throwaway guest (its volumes and empty public folder were made by this run)
if [ "${KEEP:-0}" != 1 ]; then
  KEY="$(python3 -c "import json;print(json.load(open('turnstile/data/sandboxes.json')).get('$G',{}).get('public_key',''))")"
  python3 guestctl.py remove "$G"
  for v in work claude sessions data; do docker volume rm "sbx-$G-$v" >/dev/null 2>&1; done
  [ -n "$KEY" ] && rmdir "public/$KEY" 2>/dev/null
  rm -f "turnstile/data/$G.dashboard-password"
  echo "cleaned up $G"
fi
exit "$FAIL"
