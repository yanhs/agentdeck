#!/bin/bash
# Tests Claude-session persistence for all agent launchers (agents 1-8).
# Each launcher must create its session on the first launch (--session-id) and
# resume it on every later launch (--resume), with a session id unique per agent.
# Launch is "default mode": no --model and no /effort are applied — a restart
# just resumes the session and leaves model/effort at Claude's defaults.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS=(launch-claude.sh launch-claude-2.sh launch-claude-3.sh launch-claude-4.sh \
         launch-claude-5.sh launch-claude-6.sh launch-claude-7.sh launch-claude-8.sh)
fail=0
ids=""

for s in "${SCRIPTS[@]}"; do
  script="$DIR/$s"
  if [ ! -f "$script" ]; then echo "FAIL: $s — missing"; fail=1; continue; fi

  sid=$(grep -oP 'AGENT_SESSION_ID="\K[^"]+' "$script")
  if [ -z "$sid" ]; then echo "FAIL: $s — no AGENT_SESSION_ID"; fail=1; continue; fi
  ids="$ids $sid"

  # first launch (no session file) -> must use --session-id
  T=$(mktemp -d)
  out=$(HOME="$T" DRY_RUN=1 bash "$script" 2>&1)
  rm -rf "$T"
  if ! echo "$out" | grep -q -- "--session-id $sid" || echo "$out" | grep -q -- "--resume"; then
    echo "FAIL: $s — first launch not --session-id: $out"; fail=1; continue
  fi

  # later launch (session file exists) -> must use --resume
  T=$(mktemp -d)
  mkdir -p "$T/.claude/projects/-home-ubuntu-pr"
  echo '{}' > "$T/.claude/projects/-home-ubuntu-pr/$sid.jsonl"
  out=$(HOME="$T" DRY_RUN=1 bash "$script" 2>&1)
  rm -rf "$T"
  if ! echo "$out" | grep -q -- "--resume $sid" || echo "$out" | grep -q -- "--session-id"; then
    echo "FAIL: $s — later launch not --resume: $out"; fail=1; continue
  fi

  # --dangerously-skip-permissions kept. Default mode: NOTHING else is forced —
  # no --model (so 1M/200K is your free choice in the TUI), no effort env, no
  # /effort send-keys. Model/context/effort come from the resumed session.
  if ! echo "$out" | grep -q -- "--dangerously-skip-permissions"; then
    echo "FAIL: $s — --dangerously-skip-permissions missing: $out"; fail=1; continue
  fi
  if echo "$out" | grep -q -- "--model"; then
    echo "FAIL: $s — --model must not be forced (free choice in TUI): $out"; fail=1; continue
  fi
  if echo "$out" | grep -qi -- "EFFORT_LEVEL"; then
    echo "FAIL: $s — effort must not be forced: $out"; fail=1; continue
  fi
  if echo "$out" | grep -q -- "/effort"; then
    echo "FAIL: $s — /effort send-keys must not be used: $out"; fail=1; continue
  fi
  echo "PASS: $s  ($sid)"
done

# every agent must have its own distinct session id
dupes=$(echo $ids | tr ' ' '\n' | sort | uniq -d)
if [ -n "$dupes" ]; then
  echo "FAIL: duplicate session ids: $dupes"; fail=1
else
  echo "PASS: all session ids unique"
fi

[ "$fail" -eq 0 ] && echo "--- all tests passed ---" || echo "--- TESTS FAILED ---"
exit $fail
