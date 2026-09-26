#!/usr/bin/env bash
# End-to-end test of install.sh on a fresh "server": a container with systemd as PID 1
# and a sudo-capable user `ubuntu` (tests/install/Dockerfile.<distro>).
#
#   tests/install/run.sh ubuntu-22.04 | ubuntu-24.04 | debian-12   full install test
#   tests/install/run.sh fedora-40                                 unsupported: refusal only
#
# What a supported run does (the CURRENT working tree is tested, not GitHub):
#   1. build + boot the container (--privileged, systemd), dashboard on a free host port
#   2. install the way users do: `curl … | bash` → here `bash < install.sh` with
#      AGENTDECK_REPO_URL pointing at a bare repo made from the working tree
#   3. smoke test from the host (tests/install/smoke.py): login page → set the password →
#      /api/library → new terminal → /sess/?arg=<id> → websocket shows the terminal →
#      /tasks/ → /api/server; every agentdeck unit active; claude runs in cs-<id>
#   4. reboot (docker restart): services back, login works, the terminal is still listed
#   5. re-run ./install.sh --yes: nothing duplicated (units, guard hooks), still works
#   6. ./install.sh --uninstall: units gone, data kept; then --uninstall --purge
# Always tears down its own container + image; `docker ps -a` must be unchanged after.
#
# Env: KEEP=1 leaves the container running for a look (still removed on the next run).
set -Eeuo pipefail

DISTRO="${1:?usage: run.sh <ubuntu-22.04|ubuntu-24.04|debian-12|fedora-40>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DOCKERFILE="$HERE/Dockerfile.$DISTRO"
[ -f "$DOCKERFILE" ] || { echo "no $DOCKERFILE" >&2; exit 2; }

TAG="agentdeck-install-test-$DISTRO"
NAME="$TAG-$$"
IMAGE="$TAG:$$"
WORK="$(mktemp -d)"
PW="smoke-pass-$$"
FAILS=0

say()  { printf '\n=== %s\n' "$*"; }
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
expect() {  # expect "name" cmd...
  local name=$1; shift
  if "$@"; then pass "$name"; else fail "$name"; fi
}

# other containers (immappeal-*, sparkaide-*, 3x-ui, …) must all still be there afterwards;
# parallel runs of this harness (agentdeck-install-test-*) are left out of the comparison
docker ps -a --format '{{.Names}}' | grep -v "^agentdeck-install-test-" | sort > "$WORK/ps-before"

cleanup() {
  local rc=$?
  if [ "${KEEP:-}" = 1 ]; then
    echo "KEEP=1: container $NAME left running"
  else
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker rmi -f "$IMAGE" >/dev/null 2>&1 || true
  fi
  docker ps -a --format '{{.Names}}' | sort > "$WORK/ps-after"
  gone="$(comm -23 "$WORK/ps-before" "$WORK/ps-after")"
  if [ -n "$gone" ]; then
    echo "FAIL  containers disappeared during the test: $gone"; rc=1
  elif [ "${KEEP:-}" != 1 ] && grep -qx "$NAME" "$WORK/ps-after"; then
    echo "FAIL  test container $NAME was not removed"; rc=1
  else
    echo "PASS  other containers untouched ($(wc -l < "$WORK/ps-before") before, all still there); test container removed"
  fi
  rm -rf "$WORK"
  exit "$rc"
}
trap cleanup EXIT

say "build $IMAGE"
docker build -q -t "$IMAGE" -f "$DOCKERFILE" "$HERE" >/dev/null

# the working tree as it is now: tracked files + the installer and its tests
(cd "$REPO" && { git ls-files; echo install.sh; git ls-files -o --exclude-standard tests/install .github; } \
   | sort -u | while read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done > "$WORK/files")
tar -C "$REPO" -cf "$WORK/tree.tar" -T "$WORK/files"

# ── unsupported distro: refuse, change nothing ─────────────────────────────────
if [ "$DISTRO" = fedora-40 ]; then
  docker run -d --name "$NAME" "$IMAGE" >/dev/null
  docker cp "$REPO/install.sh" "$NAME:/tmp/install.sh"
  docker exec "$NAME" bash -c 'touch /tmp/marker && sleep 1'
  for who in tester root; do
    say "install.sh as $who on $DISTRO"
    set +e
    out="$(docker exec -u "$who" -e AGENTDECK_ALLOW_ROOT=1 "$NAME" bash -c 'cd /tmp && bash /tmp/install.sh --yes' 2>&1)"
    rc=$?
    set -e
    echo "$out"
    expect "exit code is non-zero ($rc)" test "$rc" -ne 0
    expect "says not supported" grep -q "not supported" <<<"$out"
    expect "points at the Docker sandbox" grep -q "docker compose up -d" <<<"$out"
  done
  changed="$(docker exec "$NAME" bash -c 'find /etc /usr /opt /var/lib /home /root -xdev -newer /tmp/marker 2>/dev/null')"
  expect "no files changed on the system" test -z "$changed"
  [ -z "$changed" ] || echo "$changed" | head -20
  say "RESULT $DISTRO: $([ $FAILS = 0 ] && echo PASS || echo "FAIL ($FAILS)")"
  [ "$FAILS" = 0 ]
  exit
fi

# ── supported distro: full install ───────────────────────────────────────────
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
BASE="http://127.0.0.1:$PORT"

wait_systemd() {
  local st=""
  for _ in $(seq 1 60); do
    st="$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)"
    case "$st" in running|degraded) return 0 ;; esac
    sleep 1
  done
  echo "systemd state: $st"; return 1
}

say "boot $NAME (systemd PID 1), dashboard -> $BASE"
docker run -d --name "$NAME" --hostname agentdeck-test --privileged --cgroupns=private \
  --tmpfs /run --tmpfs /run/lock \
  -p "127.0.0.1:$PORT:8765" "$IMAGE" >/dev/null
expect "systemd is up" wait_systemd
expect "PID 1 is systemd" test "$(docker exec "$NAME" cat /proc/1/comm)" = systemd

# a bare repo made from the working tree = what `git clone` of GitHub gives the installer
(
  mkdir -p "$WORK/src" && tar -C "$WORK/src" -xf "$WORK/tree.tar" && cd "$WORK/src" \
  && git init -q -b master && git add -A && git -c user.name=t -c user.email=t@t commit -qm tree \
  && git clone -q --bare "$WORK/src" "$WORK/agentdeck.git"
)
docker cp "$WORK/agentdeck.git" "$NAME:/opt/agentdeck.git"
docker exec "$NAME" chmod -R a+rX /opt/agentdeck.git

say "install: bash < install.sh (the curl | bash path), as user ubuntu, no flags"
set +e
docker exec -i -u ubuntu -w /home/ubuntu -e AGENTDECK_REPO_URL=/opt/agentdeck.git "$NAME" \
  bash < "$REPO/install.sh" 2>&1 | tee "$WORK/install1.log"
rc=${PIPESTATUS[0]}
set -e
expect "install.sh exit 0" test "$rc" = 0
expect "prints the URL" grep -Eq "http://[0-9.]+:8765" "$WORK/install1.log"
expect "says first visit sets the password" grep -qi "first visit sets the password" "$WORK/install1.log"
expect "says + New terminal / sign in to Claude" grep -qi "sign in to Claude" "$WORK/install1.log"
[ "$rc" = 0 ] || exit 1

UNITS_ACTIVE="agentdeck-status.service agentdeck-tasks.service agentdeck-sessions.service agentdeck-caddy.service agentdeck-reaper.timer"
units_active() {
  local u bad=0
  for u in $UNITS_ACTIVE; do
    st="$(docker exec "$NAME" systemctl is-active "$u" || true)"
    [ "$st" = active ] || { echo "  $u: $st"; bad=1; }
  done
  [ "$bad" = 0 ]
}
units_enabled() {
  local u bad=0
  for u in $UNITS_ACTIVE; do
    st="$(docker exec "$NAME" systemctl is-enabled "$u" || true)"
    [ "$st" = enabled ] || { echo "  $u: $st"; bad=1; }
  done
  [ "$bad" = 0 ]
}
running_as_ubuntu() {
  local u; for u in status tasks sessions caddy; do
    [ "$(docker exec "$NAME" systemctl show -p User --value "agentdeck-$u.service")" = ubuntu ] || return 1
  done
}

say "services"
expect "all agentdeck units active" units_active
expect "all agentdeck units enabled (start at boot)" units_enabled
expect "services run as ubuntu, not root" running_as_ubuntu
docker exec "$NAME" systemctl list-units 'agentdeck*' --all --no-legend --plain | tee "$WORK/units1"
expect "claude installed for the user" docker exec -u ubuntu "$NAME" bash -lc 'claude --version'
expect "ttyd + caddy + node in place" docker exec "$NAME" bash -c 'ttyd --version && caddy version && node --version'

say "smoke (first run)"
expect "smoke: first run + new terminal" python3 "$HERE/smoke.py" "$BASE" --password "$PW" \
  --first-run --new-terminal --id-file "$WORK/id"
SID="$(cat "$WORK/id" 2>/dev/null || true)"
tmux_has() {
  docker exec -u ubuntu -e TMUX_TMPDIR=/home/ubuntu/agentdeck/.sessions/tmux "$NAME" \
    tmux list-panes -a -F '#{session_name} #{pane_current_command}' \
    | tee "$WORK/panes" | grep -Eq "^cs-$SID (claude|node)"
}
expect "cs-$SID runs claude in the agents' own tmux server" tmux_has
default_has_no_agents() {  # the user's default tmux server knows nothing of the agents
  ! docker exec -u ubuntu "$NAME" tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -q '^cs-'
}
expect "agents are not on the user's default tmux server" default_has_no_agents
cat "$WORK/panes" 2>/dev/null || true
guards() { docker exec -u ubuntu "$NAME" bash -c 'grep -o guard_task_board.py ~/.claude/settings.json | wc -l'; }
G1="$(guards)"
expect "guard hooks installed into ~/.claude/settings.json" test "$G1" -ge 1

say "reboot"
docker restart "$NAME" >/dev/null
expect "systemd is up after reboot" wait_systemd
sleep 3
expect "all agentdeck units active after reboot" units_active
expect "smoke after reboot: login + terminal still listed" python3 "$HERE/smoke.py" "$BASE" \
  --password "$PW" --expect-id "$SID"

say "re-run: ./install.sh --yes (idempotent)"
set +e
docker exec -u ubuntu -w /home/ubuntu "$NAME" bash -c 'cd ~/agentdeck && ./install.sh --yes' 2>&1 \
  | tee "$WORK/install2.log"
rc=${PIPESTATUS[0]}
set -e
expect "re-run exit 0" test "$rc" = 0
docker exec "$NAME" systemctl list-units 'agentdeck*' --all --no-legend --plain > "$WORK/units2"
expect "same units after re-run" diff <(awk '{print $1}' "$WORK/units1") <(awk '{print $1}' "$WORK/units2")
expect "guard hooks not duplicated" test "$(guards)" = "$G1"
expect "one ttyd, one caddy, one status_server" docker exec "$NAME" bash -c \
  '[ "$(pgrep -xc ttyd)" = 1 ] && [ "$(pgrep -xc caddy)" = 1 ] && [ "$(pgrep -fc "[s]tatus_server.py")" = 1 ]'
expect "units active after re-run" units_active
expect "smoke after re-run" python3 "$HERE/smoke.py" "$BASE" --password "$PW" --expect-id "$SID"

say "uninstall (keeps data)"
# a tmux session of the user's own — uninstall must leave it alone
docker exec -u ubuntu "$NAME" tmux new-session -d -s mine
set +e
docker exec -u ubuntu "$NAME" bash -c 'cd ~/agentdeck && ./install.sh --uninstall --yes' 2>&1 | tee "$WORK/un.log"
rc=${PIPESTATUS[0]}
set -e
expect "uninstall exit 0" test "$rc" = 0
expect "no agentdeck units left" test -z "$(docker exec "$NAME" bash -c 'ls /etc/systemd/system/ | grep agentdeck; systemctl list-units "agentdeck*" --no-legend --plain' )"
expect "dashboard port closed" bash -c "! curl -s -o /dev/null --max-time 3 $BASE/login"
expect "data kept (.sessions/library.json)" docker exec -u ubuntu "$NAME" test -s /home/ubuntu/agentdeck/.sessions/library.json
expect "guard hooks removed" test "$(guards)" = 0
expect "the user's own tmux session survived" docker exec -u ubuntu "$NAME" tmux has-session -t =mine
agents_tmux_gone() {
  ! docker exec -u ubuntu -e TMUX_TMPDIR=/home/ubuntu/agentdeck/.sessions/tmux "$NAME" tmux list-sessions >/dev/null 2>&1
}
expect "the agents' tmux server is gone" agents_tmux_gone

say "uninstall --purge"
docker cp "$REPO/install.sh" "$NAME:/tmp/install.sh"
docker exec -u ubuntu "$NAME" bash /tmp/install.sh --uninstall --purge --yes 2>&1 | tail -5
expect "purge removes ~/agentdeck" docker exec "$NAME" test ! -e /home/ubuntu/agentdeck
expect "purge keeps home + the user's tmux session" docker exec -u ubuntu "$NAME" bash -c 'test -d /home/ubuntu && tmux has-session -t =mine'

say "RESULT $DISTRO: $([ $FAILS = 0 ] && echo PASS || echo "FAIL ($FAILS)")"
[ "$FAILS" = 0 ]
