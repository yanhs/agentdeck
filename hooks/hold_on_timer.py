#!/usr/bin/env python3
"""Claude Code PostToolUse hook: a timer set inside a library session holds it.

When Claude in a topic-session (tmux cs-<id>, env AGENTDECK_SESSION=<id>) sets a
timer, the session must stay loaded until the timer fires — unloading it (LRU
eviction at the limit, or idle_reaper) kills the claude process and the timer
with it. This hook writes the hold marker `.sessions/hold-<id>`
(library_cli.py hold) for:

    ScheduleWakeup   delaySeconds + 600 s (10 min slack for the wake-up itself)
    CronCreate       6 hours
    Monitor          6 hours

<id> is the conversation live NOW — the hook payload's session_id (first 8 hex).
AGENTDECK_SESSION is exported once, when the pane starts: after Claude's consent
relaunch, /clear or /resume the terminal's number is the new conversation's
(convo_sync renames it), so the variable is the old number then. It is only the
fallback when the payload has no usable session_id, and it tells a library pane
from any other Claude: outside a library session (no or bad AGENTDECK_SESSION) the
hook does nothing. It never fails the tool call: every error ends in exit 0. Wired in by
hooks/install_guards.py (PostToolUse, matcher ScheduleWakeup|CronCreate|Monitor;
see hooks/settings.example.json) — on by default in Docker.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

SLACK = 600
DEFAULT_HOLD = 6 * 3600
TOOLS = ("ScheduleWakeup", "CronCreate", "Monitor")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def hold_seconds(tool, tool_input):
    """Seconds to hold for this tool call, or None when it sets no timer."""
    if tool not in TOOLS:
        return None
    if tool == "ScheduleWakeup":
        d = (tool_input or {}).get("delaySeconds") if isinstance(tool_input, dict) else None
        if isinstance(d, (int, float)) and not isinstance(d, bool) and d >= 0:
            return int(d) + SLACK
    return DEFAULT_HOLD


def session_of(event, pane):
    """The terminal's number now: the payload's conversation, else the pane's."""
    import library  # noqa: E402
    u = event.get("session_id")
    if isinstance(u, str) and _UUID.fullmatch(u):
        return library.id_from_uuid(u)
    return pane


def main():
    import library  # noqa: E402  (cheap; only reached with a session id)
    pane = os.getenv("AGENTDECK_SESSION", "")
    if not library.valid_id(pane):
        return
    try:
        event = json.loads(sys.stdin.read() or "null")
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    sid = session_of(event, pane)
    secs = hold_seconds(event.get("tool_name"), event.get("tool_input"))
    if secs is None:
        return
    import library_cli  # noqa: E402
    library_cli.hold(sid, str(secs))


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # a broken hook must never break the tool call
        print(f"hold_on_timer: {ex}", file=sys.stderr)
    sys.exit(0)
