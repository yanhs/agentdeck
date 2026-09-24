"""Decision logic for the idle-terminal reaper.

The risky part is WHEN to unload; the tmux side (kill-session) is trivial and
exercised by the live dry-run. These tests pin it down:
  - a terminal with an open tab (attached) is never unloaded (owner's choice),
  - "idle" is measured from the terminal's last screen output (tmux
    session_activity) — a working Claude animates its spinner every second,
    an idle one prints nothing. The old per-minute 0.3 s CPU sample misread an
    idle Claude's background ticks as work ~3-4% of the time, so over two hours
    the clock almost never ran out (2026-09-24),
  - a terminal with a live background task (a shell writing into Claude's
    tasks/*.output) is kept past the idle window, but not forever: a task that
    has been silent for BG_MAX_SECONDS is a stuck loop, not work.
"""
import importlib.util
import os
import subprocess
import time

_spec = importlib.util.spec_from_file_location(
    "idle_reaper", os.path.join(os.path.dirname(__file__), "..", "idle_reaper.py"))
reaper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reaper)
decide = reaper.decide

IDLE = 7200      # 2 h
BG_MAX = 86400   # 24 h


def _decide(now, last_active, attached=False, bg_jobs=False):
    return decide(now=now, last_active=last_active, attached=attached,
                  bg_jobs=bg_jobs, idle_seconds=IDLE, bg_max_seconds=BG_MAX)


# ── decide() ────────────────────────────────────────────────────────────────
def test_attached_terminal_is_kept_even_if_long_idle():
    assert _decide(now=10**6, last_active=1, attached=True) is False


def test_recent_output_is_kept():
    assert _decide(now=1000 + IDLE - 1, last_active=1000) is False


def test_silent_past_window_is_unloaded():
    assert _decide(now=1000 + IDLE, last_active=1000) is True


def test_background_task_keeps_it_past_the_window():
    assert _decide(now=1000 + IDLE * 3, last_active=1000, bg_jobs=True) is False


def test_background_task_silent_past_bg_max_is_unloaded():
    # e.g. a `while pgrep -f X; do sleep 5; done` that matches itself forever
    assert _decide(now=1000 + BG_MAX, last_active=1000, bg_jobs=True) is True


def test_unknown_activity_is_kept():
    # tmux gave no timestamp — never guess towards killing
    assert _decide(now=10**6, last_active=None) is False


def test_last_active_takes_latest_of_output_and_attach():
    assert reaper.last_active_of(100, 500) == 500
    assert reaper.last_active_of(900, 500) == 900
    assert reaper.last_active_of(100, None) == 100
    assert reaper.last_active_of(None, None) is None


# ── sweep() with tmux faked out ─────────────────────────────────────────────
class FakeTmux:
    def __init__(self, sessions):
        self.sessions = sessions          # name -> dict(activity, attached, bg)
        self.killed = []

    def install(self, monkeypatch, tmp_path):
        monkeypatch.setattr(reaper, "STATE_FILE", str(tmp_path / "state.json"))
        monkeypatch.setattr(reaper, "SESSIONS", list(self.sessions))
        monkeypatch.setattr(reaper, "watched_sessions", lambda: list(self.sessions))
        monkeypatch.setattr(reaper, "session_exists", lambda s: s in self.sessions)
        monkeypatch.setattr(reaper, "session_attached", lambda s: self.sessions[s]["attached"])
        monkeypatch.setattr(reaper, "session_activity", lambda s: self.sessions[s]["activity"])
        monkeypatch.setattr(reaper, "session_has_bg_jobs", lambda s: self.sessions[s]["bg"])
        monkeypatch.setattr(reaper, "unload", lambda s: self.killed.append(s) or True)


def test_sweep_unloads_silent_and_keeps_the_rest(monkeypatch, tmp_path):
    now = 100_000
    t = FakeTmux({
        "silent":   dict(activity=now - IDLE - 60, attached=False, bg=False),
        "fresh":    dict(activity=now - 60,        attached=False, bg=False),
        "watched":  dict(activity=now - IDLE * 5,  attached=True,  bg=False),
        "bg":       dict(activity=now - IDLE - 60, attached=False, bg=True),
    })
    t.install(monkeypatch, tmp_path)
    assert reaper.sweep(now=now) == ["silent"]
    assert t.killed == ["silent"]


def test_closing_a_tab_restarts_the_clock(monkeypatch, tmp_path):
    # Output is old, but the owner had the tab open a minute ago: the glance
    # counts as use, so closing the tab must not unload it on the next tick.
    now = 100_000
    t = FakeTmux({"s": dict(activity=now - IDLE * 3, attached=True, bg=False)})
    t.install(monkeypatch, tmp_path)
    assert reaper.sweep(now=now) == []
    t.sessions["s"]["attached"] = False
    assert reaper.sweep(now=now + 60) == []
    assert reaper.sweep(now=now + IDLE) == ["s"]


def test_dry_run_kills_nothing(monkeypatch, tmp_path):
    now = 100_000
    t = FakeTmux({"s": dict(activity=now - IDLE * 2, attached=False, bg=False)})
    t.install(monkeypatch, tmp_path)
    assert reaper.sweep(now=now, dry_run=True) == ["s"]
    assert t.killed == []


# ── background-task detection on real processes ────────────────────────────
def test_task_output_path_pattern():
    assert reaper.is_task_output(
        "/tmp/claude-1000/-home-ubuntu-pr/bef2d270-188b/tasks/be0n7egae.output")
    assert not reaper.is_task_output("/tmp/claude-1000/x/tasks/notes.txt")
    assert not reaper.is_task_output("/home/ubuntu/pr/run.output")
    assert not reaper.is_task_output("pipe:[123]")


def test_detects_a_live_background_task_in_the_tree(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    out = open(tasks / "bxyz.output", "w")
    # parent shell -> child `sleep` whose stdout is a Claude task output file
    parent = subprocess.Popen(["bash", "-c", "sleep 30 & wait"], stdout=out)
    try:
        time.sleep(0.3)
        assert reaper.tree_has_task_output(parent.pid) is True
    finally:
        parent.kill()
        subprocess.run(["pkill", "-P", str(parent.pid)])
        out.close()


def test_plain_tree_has_no_background_task():
    p = subprocess.Popen(["sleep", "30"], stdout=subprocess.DEVNULL)
    try:
        assert reaper.tree_has_task_output(p.pid) is False
    finally:
        p.kill()


# ── which sessions it watches ───────────────────────────────────────────────
def test_watches_library_sessions_and_legacy_slots(monkeypatch):
    # library sessions are tmux `cs-<8 hex>`; anything else (a shell, a test) is not ours
    monkeypatch.setattr(reaper, "_tmux_session_names",
                        lambda: ["claude-terminal-3", "cs-3beb2a44", "cs-nothex!", "work", "cs-bef2d270"])
    watched = reaper.watched_sessions()
    assert "cs-3beb2a44" in watched and "cs-bef2d270" in watched
    assert "claude-terminal-3" in watched and "claude-terminal-12" in watched   # legacy, until migration
    assert "work" not in watched and "cs-nothex!" not in watched
    assert len(watched) == len(set(watched))


def test_sweep_uses_the_discovered_list(monkeypatch, tmp_path):
    now = 100_000
    t = FakeTmux({"cs-3beb2a44": dict(activity=now - IDLE * 2, attached=False, bg=False)})
    t.install(monkeypatch, tmp_path)
    monkeypatch.setattr(reaper, "watched_sessions", lambda: ["cs-3beb2a44"])
    assert reaper.sweep(now=now) == ["cs-3beb2a44"]


# ── config ──────────────────────────────────────────────────────────────────
def test_sessions_cover_terminals_1_through_12():
    assert reaper.SESSIONS[0] == "claude-terminal"
    assert "claude-terminal-12" in reaper.SESSIONS
    assert len(reaper.SESSIONS) == 12


def test_defaults_two_hours_and_one_day():
    assert reaper.IDLE_SECONDS == 7200
    assert reaper.BG_MAX_SECONDS == 86400
