#!/bin/bash
unset CLAUDE_CODE_SESSION CLAUDE_SESSION_ID CLAUDE_CODE CLAUDE_CODE_RUNNING CLAUDE_PARENT_SESSION ANTHROPIC_CLAUDE_CODE
# Unset any other CLAUDE-named env vars so the child claude doesn't think it's
# nested. Grep the NAME only (cut first) — grepping whole lines would also match
# a var whose *value* contains "claude" (e.g. HOME under /tmp/claude-*).
for var in $(env | cut -d= -f1 | grep -i CLAUDE); do unset "$var"; done

SESSION="claude-terminal-2"
AGENT_ID="2"

# The conversation persists across restarts: first launch creates the session
# with --session-id, later launches continue it with --resume. NOTHING else is
# forced — no --model, no effort. The model/context (1M vs 200K) and effort are
# whatever you last had; pick them freely in the TUI with /model and /effort.
# The conversation persists across restarts AND closed terminals: each agent has a
# stable session id stored in .sessions/ (generated once), so a relaunch RESUMES it
# (claude --resume) instead of starting over. Nothing is hardcoded -- claude is found
# on PATH and agents start in $AGENTDECK_WORKDIR (default: the directory above this
# repo), so a fresh clone works on any machine out of the box.
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}"
WORKDIR="${AGENTDECK_WORKDIR:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
_SID_DIR="$(dirname "${BASH_SOURCE[0]:-$0}")/.sessions"; mkdir -p "$_SID_DIR"
_SID_FILE="$_SID_DIR/agent-$AGENT_ID.id"
[ -s "$_SID_FILE" ] || uuidgen > "$_SID_FILE"
AGENT_SESSION_ID="$(cat "$_SID_FILE")"
SESSION_FILE="$HOME/.claude/projects/$(printf '%s' "$WORKDIR" | sed 's#/#-#g')/$AGENT_SESSION_ID.jsonl"

if [ -f "$SESSION_FILE" ]; then
  CLAUDE_CMD="$CLAUDE_BIN --resume $AGENT_SESSION_ID --dangerously-skip-permissions"
else
  CLAUDE_CMD="$CLAUDE_BIN --session-id $AGENT_SESSION_ID --dangerously-skip-permissions"
fi

# DRY_RUN=1 prints the resolved claude command, then exits (used by tests).
if [ "${DRY_RUN:-}" = "1" ]; then
  echo "$CLAUDE_CMD"
  exit 0
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  exec tmux attach-session -t "$SESSION"
else
  # === Order gate: don't auto-spawn a brand-new tmux session if this agent
  # was not enabled in the dashboard list (`_order` in agents.json). Existing
  # tmux sessions still attach normally — the gate only blocks fresh starts.
  if ! python3 "$(dirname "${BASH_SOURCE[0]:-$0}")/_order_gate.py" "$AGENT_ID" >/dev/null 2>&1; then
    echo "Agent #$AGENT_ID is not in /agents/. Click '+ Claude' in the dashboard to enable it."
    sleep 5
    exit 0
  fi
  exec tmux new-session -s "$SESSION" -c "$WORKDIR" \; \
    set mouse on \; \
    send-keys "$CLAUDE_CMD" Enter
fi
