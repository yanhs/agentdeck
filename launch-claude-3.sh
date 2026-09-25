#!/bin/bash
# MIGRATED-BY migrate_library.py — slot 3 is now library topic f91c3981.
# The original script is kept next to this one as $(basename "$0").pre-library.
# Legacy tmux session still running -> attach to it exactly as before (its claude
# is not restarted). Otherwise -> the library opens the topic (open-session.sh
# loads cs-f91c3981, unloading an idle topic at the limit). No order gate: the
# library decides what loads.
SESSION="claude-terminal-3"
AGENT_ID="3"
LIB_ID="f91c3981"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

unset CLAUDE_CODE_SESSION CLAUDE_SESSION_ID CLAUDE_CODE CLAUDE_CODE_RUNNING CLAUDE_PARENT_SESSION ANTHROPIC_CLAUDE_CODE
for var in $(env | cut -d= -f1 | grep -i CLAUDE | grep -vx CLAUDE_BIN); do unset "$var"; done

TMUX_CMD=(tmux)
[ -n "${AGENTDECK_TMUX_SOCKET:-}" ] && TMUX_CMD=(tmux -L "$AGENTDECK_TMUX_SOCKET")

# exact name match in code (list-sessions), never display-message
if "${TMUX_CMD[@]}" list-sessions -F '#{session_name}' 2>/dev/null | grep -qxF -- "$SESSION"; then
  if [ "${DRY_RUN:-}" = "1" ]; then
    echo "attach =$SESSION"
    exit 0
  fi
  exec "${TMUX_CMD[@]}" attach-session -t "=$SESSION"
fi

exec bash "$HERE/open-session.sh" "$LIB_ID"
