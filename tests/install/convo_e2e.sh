#!/usr/bin/env bash
# End-to-end check of "one number per terminal" on a fresh server: a container with
# systemd as PID 1 (tests/install/Dockerfile.ubuntu-24.04), AgentDeck installed the way
# users do (`bash < install.sh`), the real Claude Code the installer puts there.
#
#   tests/install/convo_e2e.sh            the working tree (tracked files, like run.sh)
#   tests/install/convo_e2e.sh v1.7.0     a git ref instead (git archive) — to show the
#                                          same flow on a release without the fix
#
# The scenario (tests/install/convo_e2e.py drives it, from the host, over the dashboard
# URL and `docker exec` into the container's OWN tmux server — keys with send-keys,
# the screen with capture-pane):
#   0. new terminal A from the dashboard before anyone signed in: Claude shows its
#      login first (the bypass consent comes only after sign-in)
#   1. "sign in": a made-up ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL of a stand-in API
#      in the container (tests/install/fake_anthropic_api.py) in ~/.claude/oauth.env;
#      A is reopened (claude --session-id A…), the first-run screens are answered and
#      Claude's bypass-permissions consent (never pre-accepted) gets "Yes, I accept"
#   2. Claude relaunches ITSELF without the --session-id AgentDeck gave it — on 2.1.282
#      the consent alone doesn't; its "Try the new fullscreen renderer?" offer does
#      (forced on with CLAUDE_CODE_FORCE_FULLSCREEN_UPSELL=1 so it comes now; /tui
#      fullscreen is the fallback): the conversation gets a new number B, the command
#      line becomes `claude.exe --allow-dangerously-skip-permissions --permission-mode
#      bypassPermissions`, as on the owner's demo
#   3. the dashboard lists B (not A), tmux has cs-B, a tab attached meanwhile is on
#      cs-B, the old link /sess/?arg=A opens B
#   4. a task added from inside the pane — by Claude's own Bash tool running
#      tracker.py — carries B on the task board
#   5. unload + reopen resumes B: claude --resume <B uuid>, the prompt is on screen
#   6. /clear in the pane: a new number C; B stays in the list ("… (earlier)") and
#      opens as its own terminal; /resume B inside cs-C while B is open in cs-B
#      merges nothing
#
# Env: KEEP=1 leaves the container running for a look (docker rm -f <name>; docker rmi
#      <image> by hand). Without it the container and its image are removed.
set -Eeuo pipefail

REF="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DOCKERFILE="$HERE/Dockerfile.ubuntu-24.04"
LABEL="${REF:-worktree}"
TAG="agentdeck-install-test-convo"
NAME="$TAG-$$"
IMAGE="$TAG:$$"
WORK="$(mktemp -d)"
PW="e2e-pass-$$"

docker ps -a --format '{{.Names}}' | { grep -v "^agentdeck-install-test-" || true; } | sort > "$WORK/ps-before"

cleanup() {
  local rc=$?
  if [ "${KEEP:-}" = 1 ]; then
    echo "KEEP=1: container $NAME left running (docker rm -f $NAME; docker rmi $IMAGE)"
  else
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker rmi -f "$IMAGE" >/dev/null 2>&1 || true
  fi
  docker ps -a --format '{{.Names}}' | sort > "$WORK/ps-after"
  gone="$(comm -23 "$WORK/ps-before" "$WORK/ps-after")"
  if [ -n "$gone" ]; then
    echo "FAIL  containers disappeared during the test: $gone"; rc=1
  else
    echo "PASS  other containers untouched ($(wc -l < "$WORK/ps-before") before, all still there)"
  fi
  rm -rf "$WORK"
  exit "$rc"
}
trap cleanup EXIT

echo "=== build $IMAGE"
docker build -q -t "$IMAGE" -f "$DOCKERFILE" "$HERE" >/dev/null

echo "=== source: $LABEL"
mkdir -p "$WORK/src"
if [ -z "$REF" ]; then
  (cd "$REPO" && { git ls-files; echo install.sh; } | sort -u \
     | while read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done > "$WORK/files")
  tar -C "$REPO" -cf "$WORK/tree.tar" -T "$WORK/files"
  tar -C "$WORK/src" -xf "$WORK/tree.tar"
  COMMIT="$(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet HEAD -- . ':!tests/install/convo_e2e.*' || echo +changes)"
else
  git -C "$REPO" archive "$REF" | tar -C "$WORK/src" -xf -
  COMMIT="$(git -C "$REPO" rev-parse --short "$REF^{commit}")"
fi
echo "commit: $COMMIT"
(cd "$WORK/src" && git init -q -b master && git add -A \
   && git -c user.name=t -c user.email=t@t commit -qm tree \
   && git clone -q --bare "$WORK/src" "$WORK/agentdeck.git")

freeport() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }
PORT="$(freeport)"
BASE="http://127.0.0.1:$PORT"

echo "=== boot $NAME (systemd PID 1), dashboard -> $BASE"
docker run -d --name "$NAME" --hostname agentdeck-test --privileged --cgroupns=private \
  --tmpfs /run --tmpfs /run/lock -p "127.0.0.1:$PORT:8765" "$IMAGE" >/dev/null
for _ in $(seq 1 60); do
  st="$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)"
  case "$st" in running|degraded) break ;; esac
  sleep 1
done
docker cp "$WORK/agentdeck.git" "$NAME:/opt/agentdeck.git"
docker exec "$NAME" chown -R ubuntu:ubuntu /opt/agentdeck.git

echo "=== install: bash < install.sh --http, as user ubuntu"
docker exec -i -u ubuntu -w /home/ubuntu -e AGENTDECK_REPO_URL=/opt/agentdeck.git \
  -e AGENTDECK_PUBLIC_IP=203.0.113.7 "$NAME" bash -s -- --http \
  < "$WORK/src/install.sh" > "$WORK/install.log" 2>&1 \
  || { tail -40 "$WORK/install.log"; echo "FAIL  install.sh"; exit 1; }
tail -3 "$WORK/install.log"
docker exec -u ubuntu "$NAME" bash -lc 'claude --version'

echo "=== scenario"
docker cp "$HERE/fake_anthropic_api.py" "$NAME:/opt/fake_anthropic_api.py"
python3 "$HERE/convo_e2e.py" "$BASE" --container "$NAME" --password "$PW" \
  --label "$LABEL ($COMMIT)"
