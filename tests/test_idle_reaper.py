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
import types

import pytest

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
        "/tmp/claude-1000/-home-ubuntu-pr/bbbbbbbb-188b/tasks/be0n7egae.output")
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


def _drop_socket(sock):
    try:
        os.unlink(f"/tmp/tmux-{os.getuid()}/{sock}")
    except OSError:
        pass


# ── last output on a REAL tmux (private socket) ─────────────────────────────
def test_activity_follows_output_of_a_detached_session(monkeypatch):
    # tmux 3.2a: #{session_activity} is client activity — it does NOT move when a
    # detached session prints (measured 2026-09-24: 6 s of output, value frozen).
    # A Claude working with no tab open must not look idle.
    sock = f"agentdeck-test-reaper-{os.getpid()}"
    monkeypatch.setenv("AGENTDECK_TMUX_SOCKET", sock)
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "probe",
                    "while true; do date; sleep 1; done"], check=True)
    try:
        time.sleep(4)
        last = reaper.session_activity("probe")
        assert last is not None and time.time() - last <= 2.5, (time.time(), last)
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"])
        _drop_socket(sock)


# ── which sessions it watches ───────────────────────────────────────────────
def test_watches_library_sessions_and_legacy_slots(monkeypatch):
    # library sessions are tmux `cs-<8 hex>`; anything else (a shell, a test) is not ours
    monkeypatch.setattr(reaper, "_tmux_session_names",
                        lambda: ["claude-terminal-3", "cs-aaaaaaaa", "cs-nothex!", "work", "cs-bbbbbbbb"])
    watched = reaper.watched_sessions()
    assert "cs-aaaaaaaa" in watched and "cs-bbbbbbbb" in watched
    assert "claude-terminal-3" in watched and "claude-terminal-12" in watched   # legacy, until migration
    assert "work" not in watched and "cs-nothex!" not in watched
    assert len(watched) == len(set(watched))


def test_sweep_uses_the_discovered_list(monkeypatch, tmp_path):
    now = 100_000
    t = FakeTmux({"cs-aaaaaaaa": dict(activity=now - IDLE * 2, attached=False, bg=False)})
    t.install(monkeypatch, tmp_path)
    monkeypatch.setattr(reaper, "watched_sessions", lambda: ["cs-aaaaaaaa"])
    assert reaper.sweep(now=now) == ["cs-aaaaaaaa"]


# ── config ──────────────────────────────────────────────────────────────────
def test_sessions_cover_terminals_1_through_12():
    assert reaper.SESSIONS[0] == "claude-terminal"
    assert "claude-terminal-12" in reaper.SESSIONS
    assert len(reaper.SESSIONS) == 12


def test_defaults_two_hours_and_one_day():
    assert reaper.IDLE_SECONDS == 7200
    assert reaper.BG_MAX_SECONDS == 86400


# ── never crash the live tmux server ────────────────────────────────────────
def test_no_display_message_anywhere():
    # tmux 3.2a segfaults the WHOLE server on `display-message -p` when the target
    # is missing (reproduced 2026-09-24; it took down every live terminal at 10:38).
    # A session can vanish between two calls, so this command must not be used.
    root = os.path.join(os.path.dirname(__file__), "..")
    for name in ("idle_reaper.py", "status_server.py", "library_cli.py", "tg_bridge.py"):
        src = open(os.path.join(root, name)).read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        assert '"display-message"' not in code and "'display-message'" not in code, name


def test_socket_env_is_honoured(monkeypatch):
    monkeypatch.setenv("AGENTDECK_TMUX_SOCKET", "agentdeck-test-x")
    seen = {}
    monkeypatch.setattr(reaper.subprocess, "run",
                        lambda args, **k: seen.setdefault("a", args) and subprocess.CompletedProcess(args, 1, "", ""))
    reaper._tmux(["list-sessions"])
    assert seen["a"][:3] == ["tmux", "-L", "agentdeck-test-x"]


# ── exact tmux targets on a REAL private server ────────────────────────────
def test_targets_are_exact_never_prefix(monkeypatch):
    # `-t claude-terminal` prefix-matches claude-terminal-5 in tmux; the reaper
    # must neither read nor kill the neighbour when "claude-terminal" is gone.
    sock = f"agentdeck-test-reaper-x-{os.getpid()}"
    monkeypatch.setenv("AGENTDECK_TMUX_SOCKET", sock)
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "claude-terminal-5",
                    "sleep 600"], check=True)
    try:
        assert reaper.get_pane_pid("claude-terminal") is None
        assert isinstance(reaper.get_pane_pid("claude-terminal-5"), int)
        assert reaper.unload("claude-terminal") is False
        alive = subprocess.run(["tmux", "-L", sock, "has-session", "-t", "=claude-terminal-5"])
        assert alive.returncode == 0
        assert reaper.unload("claude-terminal-5") is True
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
        _drop_socket(sock)


# ── hold marker: a pending timer (ScheduleWakeup / CronCreate) is work ─────
def test_held_library_session_is_kept_until_the_hold_expires(monkeypatch, tmp_path):
    now = 100_000
    reg = str(tmp_path / "reg" / "library.json")
    monkeypatch.setattr(reaper.library, "LIB_FILE", reg)
    t = FakeTmux({"cs-aaaaaaaa": dict(activity=now - IDLE * 3, attached=False, bg=False),
                  "cs-bbbbbbbb": dict(activity=now - IDLE * 3, attached=False, bg=False)})
    t.install(monkeypatch, tmp_path)
    reaper.library.set_hold("aaaaaaaa", now + 600, lib_file=reg)
    assert reaper.sweep(now=now) == ["cs-bbbbbbbb"]
    del t.sessions["cs-bbbbbbbb"]                  # the fake's unload doesn't remove it
    assert reaper.sweep(now=now + 600 + IDLE) == ["cs-aaaaaaaa"]


def test_session_held_only_for_library_names(monkeypatch, tmp_path):
    reg = str(tmp_path / "library.json")
    monkeypatch.setattr(reaper.library, "LIB_FILE", reg)
    reaper.library.set_hold("aaaaaaaa", 10**10, lib_file=reg)
    assert reaper.session_held("cs-aaaaaaaa", now=1) is True
    assert reaper.session_held("cs-bbbbbbbb", now=1) is False
    assert reaper.session_held("claude-terminal-3", now=1) is False


# ── archived terminals: unloaded as soon as idle (120 s), not after 2 h ────
def _archived(monkeypatch, ids):
    monkeypatch.setattr(reaper, "archived_ids", lambda: set(ids))


def test_archived_idle_terminal_goes_after_two_minutes(monkeypatch, tmp_path):
    now = 100_000
    t = FakeTmux({"cs-aaaaaaaa": dict(activity=now - 130, attached=False, bg=False),
                  "cs-bbbbbbbb": dict(activity=now - 130, attached=False, bg=False)})
    t.install(monkeypatch, tmp_path)
    _archived(monkeypatch, {"aaaaaaaa"})
    assert reaper.sweep(now=now) == ["cs-aaaaaaaa"]           # the live one keeps 2 h


def test_archived_terminal_is_kept_while_in_use(monkeypatch, tmp_path):
    now = 100_000
    t = FakeTmux({"cs-aaaaaaaa": dict(activity=now - 60, attached=False, bg=False),
                  "cs-bbbbbbbb": dict(activity=now - 1000, attached=True, bg=False),
                  "cs-cccccccc": dict(activity=now - 1000, attached=False, bg=True),
                  "cs-dddddddd": dict(activity=None, attached=False, bg=False)})
    t.install(monkeypatch, tmp_path)
    _archived(monkeypatch, {"aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd"})
    assert reaper.sweep(now=now) == []


def test_archived_held_terminal_is_kept(monkeypatch, tmp_path):
    now = 100_000
    t = FakeTmux({"cs-aaaaaaaa": dict(activity=now - 1000, attached=False, bg=False)})
    t.install(monkeypatch, tmp_path)
    _archived(monkeypatch, {"aaaaaaaa"})
    monkeypatch.setattr(reaper, "session_held", lambda s, n: True)
    assert reaper.sweep(now=now) == []


def test_closing_the_tab_of_an_archived_terminal_restarts_its_two_minutes(monkeypatch, tmp_path):
    now = 100_000
    t = FakeTmux({"cs-aaaaaaaa": dict(activity=now - 5000, attached=True, bg=False)})
    t.install(monkeypatch, tmp_path)
    _archived(monkeypatch, {"aaaaaaaa"})
    assert reaper.sweep(now=now) == []
    t.sessions["cs-aaaaaaaa"]["attached"] = False
    assert reaper.sweep(now=now + 60) == []
    assert reaper.sweep(now=now + 125) == ["cs-aaaaaaaa"]


def test_archived_window_default_is_120s():
    assert reaper.ARCHIVED_IDLE_SECONDS == 120


def test_archived_ids_read_the_registry(monkeypatch, tmp_path):
    lib = tmp_path / "library.json"
    monkeypatch.setenv("AGENTDECK_LIBRARY", str(lib))
    assert reaper.archived_ids() == set()                     # no registry yet
    with reaper.library.update(str(lib)) as L:
        a = reaper.library.create(L, "a", cwd="/", now=1, uuid="aaaaaaaa-1111-4111-8111-111111111111")
        reaper.library.create(L, "b", cwd="/", now=1, uuid="bbbbbbbb-1111-4111-8111-111111111111")
        a["archived"] = True
    assert reaper.archived_ids() == {"aaaaaaaa"}
    lib.write_text("{broken")
    assert reaper.archived_ids() == set()                     # never guess towards killing


# ── one number per terminal: the sweep syncs first (convo_sync) ─────────────
@pytest.fixture
def switched(monkeypatch, tmp_path):
    """A real private tmux server with terminal cs-a1a1a1a1 whose Claude now runs
    conversation b2b2b2b2 (Claude's pid file says so); temp registry, pid-file dir
    and reaper state."""
    import json as _json
    sock = f"agentdeck-test-reaper-sync-{os.getpid()}"
    reg = tmp_path / "reg" / "library.json"
    sessions = tmp_path / "claude-sessions"
    sessions.mkdir()
    for k, v in (("AGENTDECK_TMUX_SOCKET", sock), ("AGENTDECK_LIBRARY", str(reg)),
                 ("AGENTDECK_CLAUDE_SESSIONS", str(sessions)),
                 ("AGENTDECK_CLAUDE_PROJECTS", str(tmp_path / "projects"))):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(reaper, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(reaper.library, "LIB_FILE", str(reg))
    with reaper.library.update(str(reg)) as L:
        reaper.library.create(L, "Deploy", cwd=str(tmp_path), now=1,
                              uuid="a1a1a1a1-1111-4111-8111-111111111111")
    tmux = ["tmux", "-L", sock, "-f", "/dev/null"]
    subprocess.run([*tmux, "new-session", "-d", "-s", "cs-a1a1a1a1", "sleep", "600"], check=True)
    try:
        out = subprocess.run([*tmux, "list-panes", "-a", "-F", "#{pane_pid}"],
                             capture_output=True, text=True).stdout
        pid = int(out.split()[0])
        with open(f"/proc/{pid}/stat") as f:
            start = f.read().rsplit(")", 1)[1].split()[19]
        (sessions / f"{pid}.json").write_text(_json.dumps(
            {"pid": pid, "sessionId": "b2b2b2b2-2222-4222-8222-222222222222",
             "procStart": start, "kind": "interactive", "entrypoint": "cli",
             "tmux": "cs-a1a1a1a1:@0.%0"}))
        yield types.SimpleNamespace(reg=str(reg), names=lambda: subprocess.run(
            [*tmux, "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True).stdout.split())
    finally:
        subprocess.run([*tmux, "kill-server"], capture_output=True)


def test_sweep_syncs_first_and_carries_the_seen_attached_clock(switched):
    # a tab was on the terminal 100 s ago (state keyed by its tmux name); the
    # conversation switch renames it, and the clock must go with it — else the
    # renamed terminal looks idle since its last output and is unloaded
    now = time.time() + reaper.IDLE_SECONDS + 50
    reaper.save_state({"cs-a1a1a1a1": now - 100})
    assert reaper.sweep(now=now) == []
    assert switched.names() == ["cs-b2b2b2b2"]
    assert reaper.load_state() == {"cs-b2b2b2b2": now - 100}
    assert [e["id"] for e in reaper.library.load(switched.reg)["sessions"]] == ["b2b2b2b2"]


def test_dry_run_switches_nothing(switched):
    reaper.sweep(now=time.time(), dry_run=True)
    assert switched.names() == ["cs-a1a1a1a1"]
    assert [e["id"] for e in reaper.library.load(switched.reg)["sessions"]] == ["a1a1a1a1"]
