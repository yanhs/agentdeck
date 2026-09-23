"""Decision logic for the idle-terminal reaper.

The risky part is WHEN to unload; the tmux side (kill-session) is trivial and
exercised by the live dry-run. These tests pin `decide()`:
  - a working terminal is never unloaded,
  - a terminal with an open tab (attached) is never unloaded (owner's choice),
  - a freshly-seen idle terminal is not unloaded until the clock has run,
  - and one idle past the threshold IS unloaded.
"""
import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    "idle_reaper", os.path.join(os.path.dirname(__file__), "..", "idle_reaper.py"))
reaper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reaper)
decide = reaper.decide

IDLE = 1200  # 20 min


def test_working_terminal_is_kept_and_clock_resets():
    new_last, unload = decide(now=10_000, last_active=1, attached=False,
                              working=True, idle_seconds=IDLE)
    assert unload is False
    assert new_last == 10_000            # clock bumped to now


def test_attached_terminal_is_kept_even_if_long_idle():
    # Open tab, no CPU, last activity ages ago -> still kept (owner's choice).
    new_last, unload = decide(now=10_000, last_active=1, attached=True,
                              working=False, idle_seconds=IDLE)
    assert unload is False
    assert new_last == 10_000


def test_first_sight_seeds_clock_and_does_not_unload():
    new_last, unload = decide(now=500, last_active=None, attached=False,
                              working=False, idle_seconds=IDLE)
    assert unload is False
    assert new_last == 500               # start counting from now


def test_idle_but_within_window_is_kept():
    new_last, unload = decide(now=1000 + IDLE - 1, last_active=1000,
                              attached=False, working=False, idle_seconds=IDLE)
    assert unload is False
    assert new_last == 1000              # clock NOT reset while merely idle


def test_idle_past_window_is_unloaded():
    new_last, unload = decide(now=1000 + IDLE, last_active=1000,
                              attached=False, working=False, idle_seconds=IDLE)
    assert unload is True


def test_working_beats_a_stale_clock():
    # Even if it looks long-idle, catching it working keeps it and resets.
    _, unload = decide(now=1000 + IDLE * 5, last_active=1000, attached=False,
                       working=True, idle_seconds=IDLE)
    assert unload is False


def test_sessions_cover_terminals_1_through_12():
    assert reaper.SESSIONS[0] == "claude-terminal"
    assert "claude-terminal-12" in reaper.SESSIONS
    assert len(reaper.SESSIONS) == 12


def test_default_idle_is_two_hours():
    # A paused-mid-task terminal must survive well past a short break.
    assert reaper.IDLE_SECONDS == 7200
