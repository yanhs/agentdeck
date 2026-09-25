#!/usr/bin/env python3
"""Claude Code hook: do not end a turn mid-task without something that brings you back.

Why this exists: an agent can end its turn with "continuing..." and then simply go quiet.
With no background command and no timer pending, nothing ever wakes it up again, and the
work stalls until a human notices. A promise to continue is not a mechanism.

The rule: while this session has an open task on the task board, the turn may not end
unless something is scheduled to resume the agent: a background command
(run_in_background), a timer (CronCreate / ScheduleWakeup), a Monitor, or a sub-agent.
Otherwise the agent should close the task on the board.

How it works (wire it to PreToolUse for Bash and CronCreate|ScheduleWakeup|Monitor|Agent|Task,
and to Stop):
  PreToolUse  a background Bash / CronCreate / ScheduleWakeup / Monitor / Agent / Task call
              leaves a "wake is pending" marker; a Bash call to tracker.py records which
              task ids this session created or moved ("our tasks").
  Stop        blocked when one of OUR tasks is active on the board and no fresh wake
              marker exists.

Details:
  - Only this session's tasks count. The board is shared by many agents and projects; a
    guard that holds a session responsible for everyone else's open tasks fires on every
    stop, stops meaning anything, and then nobody notices the real silent stop it was
    built to catch.
  - A task whose activity is "stopped" is exempt: explicitly parking a task with a note on
    what is left is the opposite of a silent stop.
  - The wake marker is fresh for 10 minutes.
  - At most 2 consecutive blocks, then the stop is allowed: a broken guard must never
    wedge the session. A new wake resets the counter.
  - Any internal error, a missing or unreadable board: allow.

Configuration (environment):
  TRACKER_STATE          board state file (default: tasks-dashboard/state.json in this repo,
                         the same default tracker.py uses)
  AGENTDECK_TRACKER      path to tracker.py shown in the message (default: next to the state)
  AGENTDECK_BOARD_URL    board URL shown in the message (optional; omitted when unset)
  AGENTDECK_STOP_MARKS   per-session marker directory (default: $TMPDIR/claude-stop-marks)
"""
import json
import os
import re
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAKE_TTL = 600          # seconds a wake marker counts as fresh
MAX_BLOCKS = 2          # consecutive blocks before the stop is let through
WAKE_TOOLS = ("CronCreate", "ScheduleWakeup", "Monitor", "Agent", "Task", "Workflow", "TaskCreate")
ACTIVE = ("active", "in_progress", "working")

# Board commands that show a task is ours: we created it or are moving it.
# `set <id> <item>` and `set-task <id> <status>` take the id as the first argument.
TRACKER_CMD = re.compile(
    r"tracker\.py\s+(?:add-task|set-task|add-item|rm-task|state|set)\s+([A-Za-z0-9._-]+)"
)


def state_path() -> str:
    return os.environ.get("TRACKER_STATE") or os.path.join(REPO, "tasks-dashboard", "state.json")


def tracker_path() -> str:
    return os.environ.get("AGENTDECK_TRACKER") or os.path.join(REPO, "tasks-dashboard", "tracker.py")


def mark_dir() -> str:
    return os.environ.get("AGENTDECK_STOP_MARKS") or os.path.join(
        tempfile.gettempdir(), "claude-stop-marks")


def block_message(tasks: list) -> str:
    tracker = tracker_path()
    lines = [
        "Do not stop yet: this session has an open task on the board and nothing is",
        "scheduled to bring you back to it. Ending the turn with \"continuing...\" and no",
        "way to resume is a silent stop.",
        "",
        "Do ONE of these, then carry on:",
        "  1. Run the next step as a background command (run_in_background: true);",
        "     you are resumed automatically when it finishes.",
        "  2. Set a timer: CronCreate (or ScheduleWakeup in /loop mode).",
        "  3. Is the work really finished? Close the task on the board:",
        f"     cd {os.path.dirname(tracker)}",
        f"     python3 {os.path.basename(tracker)} set-task <id> done",
        "",
        f"Open tasks: {', '.join(tasks[:6])}",
    ]
    url = os.environ.get("AGENTDECK_BOARD_URL", "").strip()
    if url:
        lines.append(f"Board: {url}")
    return "\n".join(lines)


def mark_path(session: str, kind: str) -> str:
    safe = "".join(c for c in (session or "none") if c.isalnum() or c in "-_")[:80]
    return os.path.join(mark_dir(), f"{safe}.{kind}")


def active_tasks() -> list:
    """Ids of active, not-stopped tasks. Missing or unreadable board: none."""
    try:
        with open(state_path()) as f:
            data = json.load(f)
    except Exception:
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if isinstance(tasks, dict):
        tasks = list(tasks.values())
    out = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("status", "")).lower() not in ACTIVE:
            continue
        if str(t.get("activity", "")).lower() == "stopped":
            continue
        out.append(t.get("id") or t.get("title") or "?")
    return out


def remember_tasks(session: str, command: str) -> None:
    ids = TRACKER_CMD.findall(command or "")
    if not ids:
        return
    path = mark_path(session, "tasks")
    try:
        known = our_tasks(session)
        known.update(ids)
        with open(path, "w") as f:
            f.write("\n".join(sorted(known)))
    except Exception:
        pass


def our_tasks(session: str) -> set:
    path = mark_path(session, "tasks")
    try:
        return set(open(path).read().split()) if os.path.exists(path) else set()
    except Exception:
        return set()


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    session = data.get("session_id") or ""
    os.makedirs(mark_dir(), exist_ok=True)
    tool = data.get("tool_name") or ""
    event = data.get("hook_event_name") or ""

    # --- something that will bring us back: background command, timer, agent ---
    if event == "PreToolUse":
        ti = data.get("tool_input") or {}
        if tool == "Bash":
            remember_tasks(session, ti.get("command") or "")
        wakes_us = tool in WAKE_TOOLS or (tool == "Bash" and ti.get("run_in_background") is True)
        if wakes_us:
            try:
                with open(mark_path(session, "wake"), "w") as f:
                    f.write(str(time.time()))
                blocks = mark_path(session, "blocks")
                if os.path.exists(blocks):
                    os.remove(blocks)
            except Exception:
                pass
        return 0

    # --- an attempt to end the turn ---
    if event != "Stop":
        return 0

    mine = our_tasks(session)
    tasks = [t for t in active_tasks() if t in mine]
    if not tasks:
        return 0

    wake = mark_path(session, "wake")
    try:
        if os.path.exists(wake) and time.time() - os.path.getmtime(wake) < WAKE_TTL:
            return 0
    except Exception:
        return 0

    blocks_file = mark_path(session, "blocks")
    try:
        count = int(open(blocks_file).read().strip()) if os.path.exists(blocks_file) else 0
    except Exception:
        count = 0
    if count >= MAX_BLOCKS:
        return 0
    try:
        with open(blocks_file, "w") as f:
            f.write(str(count + 1))
    except Exception:
        pass

    print(json.dumps({"decision": "block", "reason": block_message(tasks)}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # the guard failing must never stop the work
