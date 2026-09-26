"""hooks/hold_on_timer.py — a Claude Code PostToolUse hook.

When Claude inside a library session sets a timer (ScheduleWakeup, CronCreate,
Monitor), the session must stay loaded until the timer fires: unloading it
(LRU at the limit, or idle_reaper) would kill the timer with the process. The
hook writes a hold marker for $AGENTDECK_SESSION; outside a library session it
does nothing, and it never fails the tool call.
"""
import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "hold_on_timer.py")


@pytest.fixture
def reg(tmp_path):
    return str(tmp_path / "reg" / "library.json")


def hold_file(reg, sid="aaaaaaaa"):
    return os.path.join(os.path.dirname(reg), f"hold-{sid}")


def run(reg, payload, session="aaaaaaaa"):
    env = {k: v for k, v in os.environ.items() if k != "AGENTDECK_SESSION"}
    env["AGENTDECK_LIBRARY"] = reg
    if session is not None:
        env["AGENTDECK_SESSION"] = session
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, HOOK], input=data, capture_output=True,
                          text=True, env=env, timeout=30)


def until(reg, sid="aaaaaaaa"):
    return int(open(hold_file(reg, sid)).read().strip())


def test_schedule_wakeup_holds_for_the_delay_plus_ten_minutes(reg):
    t = time.time()
    r = run(reg, {"tool_name": "ScheduleWakeup", "tool_input": {"delaySeconds": 1200}})
    assert r.returncode == 0, r.stderr
    assert t + 1800 - 5 <= until(reg) <= time.time() + 1800 + 5


@pytest.mark.parametrize("tool", ["CronCreate", "Monitor"])
def test_cron_and_monitor_hold_six_hours(reg, tool):
    t = time.time()
    r = run(reg, {"tool_name": tool, "tool_input": {"cron": "*/5 * * * *"}})
    assert r.returncode == 0, r.stderr
    assert t + 6 * 3600 - 5 <= until(reg) <= time.time() + 6 * 3600 + 5


def test_schedule_wakeup_without_a_delay_holds_six_hours(reg):
    t = time.time()
    assert run(reg, {"tool_name": "ScheduleWakeup", "tool_input": {}}).returncode == 0
    assert until(reg) >= t + 6 * 3600 - 5


@pytest.mark.parametrize("payload", [
    {"tool_name": "Bash", "tool_input": {"command": "sleep 1"}},
    {"tool_name": "Edit"},
    "not json at all",
    "",
    "[1,2]",
])
def test_other_tools_and_garbage_do_nothing(reg, payload):
    r = run(reg, payload)
    assert r.returncode == 0
    assert not os.path.exists(hold_file(reg))


@pytest.mark.parametrize("session", [None, "", "../x", "AAAAAAAA", "aaaaaaaa; id"])
def test_outside_a_library_session_it_is_a_no_op(reg, session):
    r = run(reg, {"tool_name": "ScheduleWakeup", "tool_input": {"delaySeconds": 60}},
            session=session)
    assert r.returncode == 0
    assert not os.listdir(os.path.dirname(reg)) if os.path.isdir(os.path.dirname(reg)) else True


def test_a_later_hold_is_not_shortened(reg):
    run(reg, {"tool_name": "CronCreate", "tool_input": {}})
    long_hold = until(reg)
    run(reg, {"tool_name": "ScheduleWakeup", "tool_input": {"delaySeconds": 60}})
    assert until(reg) == long_hold


# ── one number per terminal: the hold goes to the conversation live now ─────
# AGENTDECK_SESSION is exported once, when the pane starts. After Claude's consent
# relaunch, /clear or /resume the terminal's number is the new conversation's
# (convo_sync renames it); the hook payload's session_id is that conversation.
UB = "b2b2b2b2-2222-4222-8222-222222222222"


def test_hold_goes_to_the_hook_payload_session(reg):
    r = run(reg, {"session_id": UB, "tool_name": "CronCreate", "tool_input": {}},
            session="a1a1a1a1")
    assert r.returncode == 0, r.stderr
    assert os.path.exists(hold_file(reg, "b2b2b2b2"))
    assert not os.path.exists(hold_file(reg, "a1a1a1a1"))


@pytest.mark.parametrize("bad", ["../../etc/passwd", "", 12345678, None, "B2B2B2B2-x"])
def test_a_malformed_payload_session_falls_back_to_the_pane_variable(reg, bad):
    r = run(reg, {"session_id": bad, "tool_name": "CronCreate", "tool_input": {}},
            session="a1a1a1a1")
    assert r.returncode == 0, r.stderr
    assert os.listdir(os.path.dirname(reg)) == ["hold-a1a1a1a1"]


def test_payload_session_outside_a_library_pane_is_a_no_op(reg):
    # a Claude that AgentDeck did not start (no AGENTDECK_SESSION) holds nothing
    r = run(reg, {"session_id": UB, "tool_name": "CronCreate", "tool_input": {}}, session=None)
    assert r.returncode == 0
    assert not os.path.exists(os.path.dirname(reg))
