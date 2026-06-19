#!/usr/bin/env bash
# track-run.sh — tie a board item to a command so the board self-updates on completion.
#
# The harness has no "task finished" shell hook (it wakes the agent, not a script),
# so we make the JOB report its own status: wrap any long/background command and the
# board flips itself when the command exits. No manual `tracker.py set` afterwards,
# no stale board.
#
# Usage:
#   track-run.sh <task_id> <item_index> [--note "<note shown while active>"] -- <command...>
#
# Lifecycle:
#   start      -> item = active, task state = working
#   exit 0     -> item = done,   task state = stopped
#   exit != 0  -> item = blocked (note "exit N"), task state = blocked
# The wrapped command's exit code is preserved. Tracker errors never break the job.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
trk() { python3 "$DIR/tracker.py" "$@" >/dev/null 2>&1 || true; }

TASK="${1:?usage: track-run.sh <task_id> <item_index> [--note NOTE] -- <command...>}"
IDX="${2:?item index required}"
shift 2

NOTE=""
if [ "${1:-}" = "--note" ]; then NOTE="$2"; shift 2; fi
[ "${1:-}" = "--" ] && shift

if [ -n "$NOTE" ]; then trk set "$TASK" "$IDX" active --note "$NOTE"; else trk set "$TASK" "$IDX" active; fi
trk state "$TASK" working

"$@"; rc=$?

if [ "$rc" -eq 0 ]; then
  trk set "$TASK" "$IDX" done
  trk state "$TASK" stopped
else
  trk set "$TASK" "$IDX" blocked --note "exit $rc"
  trk state "$TASK" blocked
fi
exit "$rc"
