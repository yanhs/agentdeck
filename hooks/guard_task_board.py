#!/usr/bin/env python3
"""Claude Code PreToolUse hook: put the task on the board before editing files.

Why this exists: "put every non-trivial task on the shared board before you start" is an
easy rule to write into an agent's instructions and an easy rule for the agent to skip
whenever a task "feels small". A reminder that only lives in a document loses to momentum.
This hook blocks the first file edit of a session until the work is on the board, or until
the agent explicitly declares the change trivial (which leaves a visible trace in the
transcript).

One script, two roles, chosen by tool_name:
  Bash                         watches for a tracker.py call (or the NO_BOARD=1 escape) and
                               records that this session has satisfied the rule.
  Edit / Write / MultiEdit /   blocked while that record is absent.
  NotebookEdit

Exempt paths (bookkeeping, not project work): temp directories and ~/.claude/ (the agent's
own memory and settings; also lets a broken guard be repaired).

Configuration (environment):
  AGENTDECK_TRACKER      path to tracker.py (default: tasks-dashboard/tracker.py in this repo)
  AGENTDECK_BOARD_URL    board URL shown in the message (optional; omitted when unset)
  AGENTDECK_BOARD_MARKS  per-session marker directory (default: $TMPDIR/claude-board-marks)

Any internal error fails OPEN: a broken guard must never wedge the session.
"""
import json
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tracker_path() -> str:
    return os.environ.get("AGENTDECK_TRACKER") or os.path.join(REPO, "tasks-dashboard", "tracker.py")


def mark_dir() -> str:
    return os.environ.get("AGENTDECK_BOARD_MARKS") or os.path.join(
        tempfile.gettempdir(), "claude-board-marks")


def exempt_prefixes() -> list:
    prefixes = ["/tmp/", "/var/tmp/", os.path.join(tempfile.gettempdir(), "")]
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if home and home != "/":
        prefixes.append(os.path.join(home, ".claude", ""))
    return prefixes


def is_exempt(path: str) -> bool:
    if not path:
        return False
    return any(path.startswith(p) for p in exempt_prefixes())


def block_message(path: str) -> str:
    tracker = tracker_path()
    lines = [
        "BLOCKED: put the task on the board first, then edit.",
        "",
        f"  cd {os.path.dirname(tracker)}",
        f'  python3 {os.path.basename(tracker)} add-task <id> --title "..." --agent "claude"',
        f'  python3 {os.path.basename(tracker)} add-item <id> "step"        # one per step',
        f"  python3 {os.path.basename(tracker)} set-task <id> active",
        "",
        "Then keep it moving as you go: python3 tracker.py set <id> <item> active|done|blocked",
    ]
    url = os.environ.get("AGENTDECK_BOARD_URL", "").strip()
    if url:
        lines += ["", f"Board: {url}"]
    lines += [
        "",
        "Genuinely a one-line change? Say so instead of silently skipping. Run:",
        "  NO_BOARD=1 true   # trivial edit, no board entry needed",
        "and the guard stands down for the rest of this session.",
        "",
        f"First edit attempted: {path}",
    ]
    return "\n".join(lines)


def marker_path(session: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session or "unknown")
    return os.path.join(mark_dir(), safe)


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    tool = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}
    mark = marker_path(data.get("session_id") or "")

    if tool == "Bash":
        cmd = ti.get("command") or ""
        if "tracker.py" in cmd or re.search(r"\bNO_BOARD=1\b", cmd):
            os.makedirs(os.path.dirname(mark), exist_ok=True)
            open(mark, "w").close()
        return 0

    if tool not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return 0
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if is_exempt(path) or os.path.exists(mark):
        return 0
    print(block_message(path), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[guard_task_board] internal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
