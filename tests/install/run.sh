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
#      AGENTKENNEL_REPO_URL pointing at a bare repo made from the working tree
#   3. smoke test from the host (tests/install/smoke.py): login page → set the password →
#      /api/library → new terminal → /sess/?arg=<id> → websocket shows the terminal →
#      /tasks/ → /api/server; every agentkennel unit active; claude runs in cs-<id>
#   4. reboot (docker restart): services back, login works, the terminal is still listed
#   5. re-run ./install.sh --yes: nothing duplicated (units, guard hooks), still works
#   6. ./install.sh --uninstall: units gone, data kept; then --uninstall --purge
# Always tears down its own container + image; `docker ps -a` must be unchanged after.
#
# Env: KEEP=1 leaves the container running for a look (still removed on the next run).
#      SCENARIO= which HTTPS case the install meets (a public certificate can't be issued
#      in a container, so each case is made reachable on purpose; no Let's Encrypt traffic):
#        cert-timeout (default)  80/443 free, but the ACME server is unreachable
#                                (AGENTKENNEL_ACME_CA=https://127.0.0.1:9/…, 20 s wait) →
#                                falls back to HTTPS with a self-signed certificate on :8765
#        port80-busy             something else listens on 80 → self-signed on :8765
#        internal                80/443 free, AGENTKENNEL_TLS_INTERNAL=1 (Caddy's own CA in place
#                                of Let's Encrypt) → https://<name> on 443, 80 redirects, no :8765
#        internal-alt            443 taken → https://<name>:8443 with Caddy's own CA
#        http                    ./install.sh --http → plain http on :8765
#        upgrade                 not a fresh install: AgentDeck as it was before v2.1.0
#                                (OLD_REF, default ba949b9, from `git archive`) is installed
#                                with --http, gets a password, a terminal and a board task;
#                                then the working tree's installer runs the curl | bash way.
#                                Checked: the agentdeck-* units are gone, agentkennel-* run
#                                and start at boot, the same password logs in, the terminal
#                                and its tmux session (same tmux server, same pane pid) and
#                                the task survived, nothing is duplicated, a re-run is
#                                idempotent, a reboot and --uninstall leave no old name.
#      The smoke test (login, new terminal over the websocket, board) runs over that URL.
set -Eeuo pipefail

DISTRO="${1:?usage: run.sh <ubuntu-22.04|ubuntu-24.04|debian-12|fedora-40>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DOCKERFILE="$HERE/Dockerfile.$DISTRO"
[ -f "$DOCKERFILE" ] || { echo "no $DOCKERFILE" >&2; exit 2; }

TAG="agentkennel-install-test-$DISTRO"
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

# the server's other containers (whatever else runs there) must all still be there afterwards;
# parallel runs of this harness (agentkennel-install-test-*) are left out of the comparison
docker ps -a --format '{{.Names}}' | { grep -v "^agentkennel-install-test-" || true; } | sort > "$WORK/ps-before"

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
    out="$(docker exec -u "$who" -e AGENTKENNEL_ALLOW_ROOT=1 "$NAME" bash -c 'cd /tmp && bash /tmp/install.sh --yes' 2>&1)"
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
freeport() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }
PORT="$(freeport)" H80="$(freeport)" H443="$(freeport)" HALT="$(freeport)"
SCENARIO="${SCENARIO:-cert-timeout}"
IP=203.0.113.7                    # a documentation address: no real lookup, no real name
NAME_SSLIP="203-0-113-7.sslip.io"
# env for every installer run of this scenario (re-runs too) + how the smoke test reaches it
INSTALL_ENV=(-e AGENTKENNEL_REPO_URL=/opt/agentkennel.git -e AGENTKENNEL_PUBLIC_IP=$IP)
INSTALL_ARGS=""
SMOKE_ARGS=()
case "$SCENARIO" in
  cert-timeout)
    INSTALL_ENV+=(-e AGENTKENNEL_ACME_CA=https://127.0.0.1:9/directory -e AGENTKENNEL_CERT_WAIT=20)
    BASE="https://127.0.0.1:$PORT"; SMOKE_ARGS=(--insecure) ;;
  port80-busy)
    BASE="https://127.0.0.1:$PORT"; SMOKE_ARGS=(--insecure) ;;
  internal)
    INSTALL_ENV+=(-e AGENTKENNEL_TLS_INTERNAL=1)
    BASE="https://$NAME_SSLIP"; SMOKE_ARGS=(--insecure --connect "127.0.0.1:$H443") ;;
  internal-alt)
    INSTALL_ENV+=(-e AGENTKENNEL_TLS_INTERNAL=1)
    BASE="https://$NAME_SSLIP:8443"; SMOKE_ARGS=(--insecure --connect "127.0.0.1:$HALT") ;;
  http|upgrade)
    INSTALL_ARGS="--http"; BASE="http://127.0.0.1:$PORT" ;;
  *) echo "unknown SCENARIO=$SCENARIO" >&2; exit 2 ;;
esac
smoke() { python3 "$HERE/smoke.py" "$BASE" "${SMOKE_ARGS[@]}" "$@"; }
# curl the dashboard from the host the way the smoke test does
dash_curl() {
  case "$SCENARIO" in
    internal) curl -sk --connect-to "$NAME_SSLIP:443:127.0.0.1:$H443" "$@" ;;
    internal-alt) curl -sk --connect-to "$NAME_SSLIP:8443:127.0.0.1:$HALT" "$@" ;;
    *) curl -sk "$@" ;;
  esac
}
say "scenario: $SCENARIO — dashboard expected at $BASE"

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
docker run -d --name "$NAME" --hostname agentkennel-test --privileged --cgroupns=private \
  --tmpfs /run --tmpfs /run/lock \
  -p "127.0.0.1:$PORT:8765" -p "127.0.0.1:$H80:80" -p "127.0.0.1:$H443:443" \
  -p "127.0.0.1:$HALT:8443" "$IMAGE" >/dev/null
expect "systemd is up" wait_systemd
expect "PID 1 is systemd" test "$(docker exec "$NAME" cat /proc/1/comm)" = systemd

# ── SCENARIO=upgrade: AgentDeck (before v2.1.0) installed, then this installer ─────
if [ "$SCENARIO" = upgrade ]; then
  OLD_REF="${OLD_REF:-ba949b9}"
  OLD_DIR=/home/ubuntu/agentdeck            # the old default folder: it must be kept
  OLD_TMUX="$OLD_DIR/.sessions/tmux"
  as_ubuntu() { docker exec -u ubuntu -w /home/ubuntu -e HOME=/home/ubuntu "$NAME" bash -c "$1"; }
  agents_tmux() { docker exec -u ubuntu -e TMUX_TMPDIR="$OLD_TMUX" "$NAME" tmux "$@"; }

  say "the old version: $OLD_REF (git archive) as the bare repo's first commit"
  mkdir -p "$WORK/src"
  git -C "$REPO" archive "$OLD_REF" | tar -C "$WORK/src" -xf -
  (cd "$WORK/src" && git init -q -b master && git add -A \
     && git -c user.name=t -c user.email=t@t commit -qm "old: $OLD_REF" \
     && git clone -q --bare "$WORK/src" "$WORK/agentkennel.git")
  docker cp "$WORK/agentkennel.git" "$NAME:/opt/agentkennel.git"
  docker exec "$NAME" chown -R ubuntu:ubuntu /opt/agentkennel.git

  say "install the OLD version (bash < install.sh, its own variable names)"
  set +e
  docker exec -i -u ubuntu -w /home/ubuntu -e AGENTDECK_REPO_URL=/opt/agentkennel.git \
    -e AGENTDECK_PUBLIC_IP=$IP "$NAME" bash -s -- --http < "$WORK/src/install.sh" 2>&1 \
    | tee "$WORK/install-old.log" | tail -n 12
  rc=${PIPESTATUS[0]}
  set -e
  expect "old install.sh exit 0" test "$rc" = 0
  [ "$rc" = 0 ] || exit 1
  expect "old units are agentdeck-*" docker exec "$NAME" systemctl is-active agentdeck-status.service agentdeck-sessions.service
  SETUP="$(grep -o '/login?setup=[A-Za-z0-9_-]*' "$WORK/install-old.log" | tail -1)"
  expect "old: password + a new terminal" smoke --password "$PW" --first-run --setup "$SETUP" \
    --new-terminal --id-file "$WORK/id"
  SID="$(cat "$WORK/id" 2>/dev/null || true)"
  expect "old: a task on the board" as_ubuntu \
    "TRACKER_STATE=$OLD_DIR/.sessions/tasks-state.json python3 $OLD_DIR/tasks-dashboard/tracker.py add-task upgrade-check --title 'survives the upgrade' --agent harness"
  guards() { as_ubuntu 'grep -o guard_task_board.py ~/.claude/settings.json | wc -l'; }
  G0="$(guards)"
  pane_of() { agents_tmux list-panes -t "=cs-$SID" -F '#{pane_pid}' 2>/dev/null | head -n 1; }
  # list-*, never display-message (it has crashed a tmux server before)
  server_of() { agents_tmux list-panes -t "=cs-$SID" -F '#{pid}' 2>/dev/null | head -n 1; }
  PANE0="$(pane_of)"; SERVER0="$(server_of)"
  expect "old: cs-$SID runs (pane pid ${PANE0:-none}, tmux server ${SERVER0:-none})" test -n "$PANE0" -a -n "$SERVER0"
  docker exec "$NAME" systemctl list-units 'agentdeck*' --all --no-legend --plain | tee "$WORK/units-old"

  say "the new version: the working tree pushed on top (what a git pull gets)"
  (cd "$WORK/src" && git rm -rq . && tar -C "$WORK/src" -xf "$WORK/tree.tar" && git add -A \
     && git -c user.name=t -c user.email=t@t commit -qm "new: working tree" \
     && git push -q "$WORK/agentkennel.git" master)
  docker cp "$WORK/agentkennel.git/." "$NAME:/opt/agentkennel.git/"
  docker exec "$NAME" chown -R ubuntu:ubuntu /opt/agentkennel.git

  say "UPGRADE: bash < install.sh (the curl | bash path) with the new variable names"
  set +e
  docker exec -i -u ubuntu -w /home/ubuntu -e AGENTKENNEL_REPO_URL=/opt/agentkennel.git \
    -e AGENTKENNEL_PUBLIC_IP=$IP "$NAME" bash -s < "$REPO/install.sh" 2>&1 | tee "$WORK/install-up.log"
  rc=${PIPESTATUS[0]}
  set -e
  L="$WORK/install-up.log"
  expect "upgrade exit 0" test "$rc" = 0
  expect "kept the old folder ($OLD_DIR)" grep -q "updating $OLD_DIR" "$L"
  expect "says the services moved" grep -q "moved the services to their new names" "$L"
  expect "kept the mode (--http from the old record)" grep -q "HTTP only (you chose --http at the last install)" "$L"
  expect "the code is the new one" as_ubuntu "test -f $OLD_DIR/envcompat.py"

  UNITS_ACTIVE="agentkennel-status.service agentkennel-tasks.service agentkennel-sessions.service agentkennel-caddy.service agentkennel-reaper.timer agentkennel-services.service"
  units_state() {  # expect active + enabled for every new unit
    local u bad=0
    for u in $UNITS_ACTIVE; do
      a="$(docker exec "$NAME" systemctl is-active "$u" || true)"
      e="$(docker exec "$NAME" systemctl is-enabled "$u" || true)"
      [ "$a" = active ] && [ "$e" = enabled ] || { echo "  $u: $a / $e"; bad=1; }
    done
    [ "$bad" = 0 ]
  }
  old_units_gone() {  # no file, nothing loaded or running under an old name
    local files listed
    files="$(docker exec "$NAME" bash -c 'ls /etc/systemd/system /etc/systemd/system/*.wants 2>/dev/null | grep agentdeck || true')"
    listed="$(docker exec "$NAME" systemctl list-units 'agentdeck*' --all --no-legend --plain)"
    echo "  files: ${files:-none}"; echo "  units: ${listed:-none}"
    [ -z "$files" ] && ! awk '$2 == "loaded" || $3 == "active" || $3 == "activating"' <<<"$listed" | grep -q .
  }
  expect "agentkennel-* active and enabled" units_state
  expect "no agentdeck-* unit file, none loaded or active" old_units_gone
  expect "the same password logs in, the terminal is still listed" smoke --password "$PW" --expect-id "$SID"
  PANE1="$(pane_of)"; SERVER1="$(server_of)"
  expect "tmux session cs-$SID survived: same tmux server ($SERVER0 → $SERVER1)" test -n "$SERVER1" -a "$SERVER1" = "$SERVER0"
  expect "same pane, same claude (pane pid $PANE0 → $PANE1)" test -n "$PANE1" -a "$PANE1" = "$PANE0"
  board_has_task() {
    as_ubuntu "grep -q '\"upgrade-check\"' $OLD_DIR/.sessions/tasks-state.json" \
      && docker exec "$NAME" curl -fsS http://127.0.0.1:9308/state.json | grep -q '"upgrade-check"'
  }
  expect "the board task is still there (file and /tasks/)" board_has_task
  expect "the new record: ~/.config/agentkennel/install.env, the old one gone" as_ubuntu \
    "grep -qx 'AGENTKENNEL_DIR=$OLD_DIR' ~/.config/agentkennel/install.env && test ! -e ~/.config/agentdeck/install.env"
  expect "guard hooks not duplicated ($G0)" test "$(guards)" = "$G0"
  marks_once() {
    local m n; for m in environment safety tdd links services; do
      n="$(as_ubuntu "grep -cE '<!-- agent(deck|kennel)-$m -->' ~/.claude/CLAUDE.md" || true)"
      [ "$n" = 1 ] || { echo "  $m: $n"; return 1; }
    done
  }
  expect "every CLAUDE.md block once (old marks count)" marks_once
  expect "a new terminal works in the surviving tmux server" smoke --password "$PW" \
    --new-terminal --id-file "$WORK/id2"
  expect "the units set both variable names for children" docker exec "$NAME" bash -c \
    'systemctl show -p Environment agentkennel-status.service | grep -q AGENTKENNEL_WORKDIR= && systemctl show -p Environment agentkennel-status.service | grep -q AGENTDECK_WORKDIR='

  say "re-run: ./install.sh --yes (idempotent)"
  docker exec "$NAME" systemctl list-units 'agentkennel*' --all --no-legend --plain | awk '{print $1}' > "$WORK/units-a"
  set +e
  as_ubuntu "cd $OLD_DIR && AGENTKENNEL_PUBLIC_IP=$IP ./install.sh --yes" 2>&1 | tee "$WORK/install-re.log" | tail -n 8
  rc=${PIPESTATUS[0]}
  set -e
  expect "re-run exit 0" test "$rc" = 0
  expect "re-run moves nothing" bash -c "! grep -q 'moved the services' '$WORK/install-re.log'"
  expect "re-run: unit files unchanged" grep -q "unit files unchanged" "$WORK/install-re.log"
  docker exec "$NAME" systemctl list-units 'agentkennel*' --all --no-legend --plain | awk '{print $1}' > "$WORK/units-b"
  expect "re-run: same units" diff "$WORK/units-a" "$WORK/units-b"
  expect "re-run: guard hooks not duplicated" test "$(guards)" = "$G0"
  expect "re-run: every CLAUDE.md block once" marks_once
  expect "re-run: cs-$SID still the same pane ($PANE0)" test "$(pane_of)" = "$PANE0"
  expect "re-run: one ttyd, one caddy, one status_server" docker exec "$NAME" bash -c \
    '[ "$(pgrep -xc ttyd)" = 1 ] && [ "$(pgrep -xc caddy)" = 1 ] && [ "$(pgrep -fc "[s]tatus_server.py")" = 1 ]'
  expect "re-run: login + terminal" smoke --password "$PW" --expect-id "$SID"

  say "reboot after the upgrade"
  docker restart "$NAME" >/dev/null
  expect "systemd is up after reboot" wait_systemd
  sleep 3
  expect "after reboot: agentkennel-* active and enabled" units_state
  expect "after reboot: no agentdeck-* unit at all" old_units_gone
  expect "after reboot: login + the terminal still listed" smoke --password "$PW" --expect-id "$SID"

  say "uninstall"
  set +e
  as_ubuntu "cd $OLD_DIR && ./install.sh --uninstall --yes" 2>&1 | tail -n 4
  rc=${PIPESTATUS[0]}
  set -e
  expect "uninstall exit 0" test "$rc" = 0
  expect "no unit of either name left" test -z "$(docker exec "$NAME" bash -c 'ls /etc/systemd/system | grep -E "agent(deck|kennel)"; systemctl list-units "agentdeck*" "agentkennel*" --no-legend --plain')"
  expect "data kept" as_ubuntu "test -s $OLD_DIR/.sessions/library.json && grep -q upgrade-check $OLD_DIR/.sessions/tasks-state.json"
  expect "purge deletes the folder the OLD installer cloned (old marker)" as_ubuntu \
    "bash $OLD_DIR/install.sh --uninstall --purge --yes >/dev/null 2>&1; test ! -e $OLD_DIR"

  say "RESULT $DISTRO (upgrade from $OLD_REF): $([ $FAILS = 0 ] && echo PASS || echo "FAIL ($FAILS)")"
  [ "$FAILS" = 0 ]
  exit
fi

# a bare repo made from the working tree = what `git clone` of GitHub gives the installer
(
  mkdir -p "$WORK/src" && tar -C "$WORK/src" -xf "$WORK/tree.tar" && cd "$WORK/src" \
  && git init -q -b master && git add -A && git -c user.name=t -c user.email=t@t commit -qm tree \
  && git clone -q --bare "$WORK/src" "$WORK/agentkennel.git"
)
docker cp "$WORK/agentkennel.git" "$NAME:/opt/agentkennel.git"
# owned by the installing user: docker cp keeps the host uid, and git refuses to clone a
# repo owned by someone else ("dubious ownership") — e.g. uid 1001 on a GitHub runner
docker exec "$NAME" chown -R ubuntu:ubuntu /opt/agentkennel.git

# something else on a port the installer wants (root, like nginx would be)
hold_port() {
  docker exec -d "$NAME" perl -MIO::Socket::INET -e \
    "my \$s = IO::Socket::INET->new(LocalPort => $1, Listen => 5, ReuseAddr => 1) or die; sleep 1e6"
  sleep 1
}
case "$SCENARIO" in port80-busy) hold_port 80 ;; internal-alt) hold_port 443 ;; esac

say "install: bash < install.sh (the curl | bash path), as user ubuntu${INSTALL_ARGS:+, $INSTALL_ARGS}"
set +e
docker exec -i -u ubuntu -w /home/ubuntu "${INSTALL_ENV[@]}" "$NAME" \
  bash -s -- $INSTALL_ARGS < "$REPO/install.sh" 2>&1 | tee "$WORK/install1.log"
rc=${PIPESTATUS[0]}
set -e
L1="$WORK/install1.log"
expect "install.sh exit 0" test "$rc" = 0
case "$SCENARIO" in
  cert-timeout)
    expect "waited for the certificate" grep -q "Getting a certificate for $NAME_SSLIP (up to 20 s)" "$L1"
    expect "says Let's Encrypt couldn't reach it" grep -q "Let's Encrypt couldn't reach this server on port 80 within 20 s" "$L1"
    expect "says how to retry" grep -q "install.sh --https" "$L1"
    expect "prints the self-signed URL" grep -q "https://$IP:8765" "$L1" ;;
  port80-busy)
    expect "names the program on port 80" grep -q "it's used by perl" "$L1"
    expect "prints the self-signed URL" grep -q "https://$IP:8765" "$L1" ;;
  internal)
    expect "HTTPS is on at https://<name>" grep -q "Dashboard: https://$NAME_SSLIP — HTTPS is on" "$L1" ;;
  internal-alt)
    expect "HTTPS is on at https://<name>:8443" grep -q "Dashboard: https://$NAME_SSLIP:8443 — HTTPS is on" "$L1"
    expect "says 443 is taken" grep -q "Port 443 is used by perl" "$L1" ;;
  http)
    expect "warns: HTTP only, as requested" grep -q "HTTP only, as requested (--http)" "$L1"
    expect "prints the URL" grep -q "http://$IP:8765" "$L1" ;;
esac
case "$SCENARIO" in cert-timeout|port80-busy)
  expect "explains the browser warning" grep -q "your browser will warn once" "$L1"
  expect "says it is still encrypted" grep -q "the connection and your password are still encrypted" "$L1" ;;
esac
expect "prints the one-time setup link" grep -q "one-time link" "$L1"
expect "says + New terminal / sign in to Claude" grep -qi "sign in to Claude" "$L1"
expect "says terminals open in ~/projects" grep -q "New terminals open in ~/projects" "$L1"
[ "$rc" = 0 ] || exit 1

say "transport ($SCENARIO)"
listening() { docker exec "$NAME" ss -ltnH "sport = :$1" | grep -q .; }
case "$SCENARIO" in
  cert-timeout|port80-busy)
    # 403 = up, no password yet (only the setup link can set one)
    expect ":8765 answers https (self-signed)" test "$(dash_curl -o /dev/null -w '%{http_code}' "$BASE/login")" = 403
    expect ":8765 serves no plain http" bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/login)\" != 200 ]"
    expect "Caddy holds no port 80 / 443" bash -c "! docker exec $NAME ss -ltnpH | grep caddy | grep -Eq ':(80|443) '" ;;
  internal|internal-alt)
    expect "the dashboard port :8765 is not served at all" bash -c "! docker exec $NAME ss -ltnH 'sport = :8765' | grep -q ."
    loc="$(curl -s -o /dev/null -w '%{redirect_url}' --connect-to "$NAME_SSLIP:80:127.0.0.1:$H80" "http://$NAME_SSLIP/login")"
    want="https://$NAME_SSLIP/login"; [ "$SCENARIO" = internal-alt ] && want="https://$NAME_SSLIP:8443/login"
    expect "http on 80 redirects to $want (got $loc)" test "$loc" = "$want" ;;
  http)
    expect ":8765 answers plain http" test "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/login")" = 403 ;;
esac
expect "~/projects exists, owned by ubuntu" docker exec "$NAME" bash -c '[ "$(stat -c %U /home/ubuntu/projects)" = ubuntu ]'

UNITS_ACTIVE="agentkennel-status.service agentkennel-tasks.service agentkennel-sessions.service agentkennel-caddy.service agentkennel-reaper.timer"
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
    [ "$(docker exec "$NAME" systemctl show -p User --value "agentkennel-$u.service")" = ubuntu ] || return 1
  done
}

say "services"
expect "all agentkennel units active" units_active
expect "all agentkennel units enabled (start at boot)" units_enabled
expect "services run as ubuntu, not root" running_as_ubuntu
docker exec "$NAME" systemctl list-units 'agentkennel*' --all --no-legend --plain | tee "$WORK/units1"
expect "claude installed for the user" docker exec -u ubuntu "$NAME" bash -lc 'claude --version'
expect "ttyd + caddy + node in place" docker exec "$NAME" bash -c 'ttyd --version && caddy version && node --version'

say "smoke (first run)"
# the one-time link, taken from what the installer printed — the way a user gets it
SETUP="$(grep -o '/login?setup=[A-Za-z0-9_-]*' "$WORK/install1.log" | tail -1)"
expect "installer prints the one-time setup link" test -n "$SETUP"
expect "smoke: first run + new terminal" smoke --password "$PW" \
  --first-run --setup "$SETUP" --new-terminal --id-file "$WORK/id"
SID="$(cat "$WORK/id" 2>/dev/null || true)"
tmux_has() {
  docker exec -u ubuntu -e TMUX_TMPDIR=/home/ubuntu/agentkennel/.sessions/tmux "$NAME" \
    tmux list-panes -a -F '#{session_name} #{pane_current_command} #{pane_current_path}' \
    | tee "$WORK/panes" | grep -Eq "^cs-$SID (claude|node) "
}
starts_in_projects() { grep -Eq "^cs-$SID .* /home/ubuntu/projects$" "$WORK/panes"; }
expect "cs-$SID runs claude in the agents' own tmux server" tmux_has
expect "the new terminal starts in ~/projects" starts_in_projects
default_has_no_agents() {  # the user's default tmux server knows nothing of the agents
  ! docker exec -u ubuntu "$NAME" tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -q '^cs-'
}
expect "agents are not on the user's default tmux server" default_has_no_agents
cat "$WORK/panes" 2>/dev/null || true
guards() { docker exec -u ubuntu "$NAME" bash -c 'grep -o guard_task_board.py ~/.claude/settings.json | wc -l'; }
G1="$(guards)"
expect "guard hooks installed into ~/.claude/settings.json" test "$G1" -ge 1

say "📎 File upload"
# through the install's own proxy; the agents' user must be able to read what landed
upload_ok() {
  local out path sha
  out="$(python3 "$HERE/upload_check.py" "$BASE" "$PW" "${SMOKE_ARGS[@]}")" || { echo "$out"; return 1; }
  echo "$out"
  path="$(sed -n 's/^PATH=\(.*\) SHA=.*/\1/p' <<<"$out")"; sha="${out##*SHA=}"
  docker exec -u ubuntu "$NAME" sha256sum "$path" | grep -q "^$sha "
}
expect "📎 File: an upload lands intact and the agents' user can read it" upload_ok

say "reboot"
docker restart "$NAME" >/dev/null
expect "systemd is up after reboot" wait_systemd
# the other program on 80 / 443 comes back at boot too
case "$SCENARIO" in port80-busy) hold_port 80 ;; internal-alt) hold_port 443 ;; esac
sleep 3
expect "all agentkennel units active after reboot" units_active
expect "smoke after reboot: login + terminal still listed" smoke \
  --password "$PW" --expect-id "$SID"

say "re-run: ./install.sh --yes (idempotent)"
set +e
docker exec -u ubuntu -w /home/ubuntu "${INSTALL_ENV[@]}" "$NAME" \
  bash -c 'cd ~/agentkennel && ./install.sh --yes' 2>&1 | tee "$WORK/install2.log"
rc=${PIPESTATUS[0]}
set -e
expect "re-run exit 0" test "$rc" = 0
expect "re-run keeps the mode (same URL)" grep -qF "AgentKennel is running:  $(sed -n 's/^AgentKennel is running:  //p' "$WORK/install1.log" | tail -n 1)" "$WORK/install2.log"
docker exec "$NAME" systemctl list-units 'agentkennel*' --all --no-legend --plain > "$WORK/units2"
expect "same units after re-run" diff <(awk '{print $1}' "$WORK/units1") <(awk '{print $1}' "$WORK/units2")
expect "guard hooks not duplicated" test "$(guards)" = "$G1"
expect "one ttyd, one caddy, one status_server" docker exec "$NAME" bash -c \
  '[ "$(pgrep -xc ttyd)" = 1 ] && [ "$(pgrep -xc caddy)" = 1 ] && [ "$(pgrep -fc "[s]tatus_server.py")" = 1 ]'
expect "units active after re-run" units_active
expect "smoke after re-run" smoke --password "$PW" --expect-id "$SID"

say "uninstall (keeps data)"
# a tmux session of the user's own — uninstall must leave it alone
docker exec -u ubuntu "$NAME" tmux new-session -d -s mine
set +e
docker exec -u ubuntu "$NAME" bash -c 'cd ~/agentkennel && ./install.sh --uninstall --yes' 2>&1 | tee "$WORK/un.log"
rc=${PIPESTATUS[0]}
set -e
expect "uninstall exit 0" test "$rc" = 0
expect "no agentkennel units left" test -z "$(docker exec "$NAME" bash -c 'ls /etc/systemd/system/ | grep agentkennel; systemctl list-units "agentkennel*" --no-legend --plain' )"
dash_closed() { ! dash_curl -o /dev/null --max-time 3 "$BASE/login"; }
expect "dashboard closed" dash_closed
expect "data kept (.sessions/library.json)" docker exec -u ubuntu "$NAME" test -s /home/ubuntu/agentkennel/.sessions/library.json
expect "guard hooks removed" test "$(guards)" = 0
expect "the user's own tmux session survived" docker exec -u ubuntu "$NAME" tmux has-session -t =mine
agents_tmux_gone() {
  ! docker exec -u ubuntu -e TMUX_TMPDIR=/home/ubuntu/agentkennel/.sessions/tmux "$NAME" tmux list-sessions >/dev/null 2>&1
}
expect "the agents' tmux server is gone" agents_tmux_gone

say "uninstall --purge"
docker cp "$REPO/install.sh" "$NAME:/tmp/install.sh"
docker exec -u ubuntu "$NAME" bash /tmp/install.sh --uninstall --purge --yes 2>&1 | tail -5
expect "purge removes ~/agentkennel" docker exec "$NAME" test ! -e /home/ubuntu/agentkennel
expect "purge keeps home + the user's tmux session" docker exec -u ubuntu "$NAME" bash -c 'test -d /home/ubuntu && tmux has-session -t =mine'

say "RESULT $DISTRO: $([ $FAILS = 0 ] && echo PASS || echo "FAIL ($FAILS)")"
[ "$FAILS" = 0 ]
