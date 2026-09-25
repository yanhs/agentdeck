"""hooks/guard_task_board.py and hooks/guard_dont_stop.py — Claude Code guard hooks.

guard_task_board: the first file edit of a session is blocked until the work is on the
task board (a tracker.py call) or explicitly declared trivial (NO_BOARD=1).

guard_dont_stop: a turn may not end while this session has an open task on the board and
nothing is scheduled to wake the agent up again (background command, timer, monitor, agent).

Both hooks are run the way Claude Code runs them: hook JSON on stdin, in a subprocess, with
state and marker directories redirected to temp dirs via env.
"""
import json
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD_HOOK = os.path.join(REPO, "hooks", "guard_task_board.py")
STOP_HOOK = os.path.join(REPO, "hooks", "guard_dont_stop.py")

# A path that is never exempt: not a temp dir, not ~/.claude/.
PROJECT_FILE = "/srv/project/app.py"
TRACKER = "cd tasks-dashboard && python3 tracker.py"
CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def _env(tmp_path, **extra):
    env = dict(os.environ)
    env.pop("AGENTDECK_BOARD_URL", None)
    env["TRACKER_STATE"] = str(tmp_path / "state.json")
    env["AGENTDECK_BOARD_MARKS"] = str(tmp_path / "board-marks")
    env["AGENTDECK_STOP_MARKS"] = str(tmp_path / "stop-marks")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def run_hook(hook, payload, tmp_path, raw=None, **env):
    p = subprocess.run(
        [sys.executable, hook],
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True, text=True, env=_env(tmp_path, **env), timeout=30,
    )
    return p


def assert_clean(text):
    # The repo path is computed at run time and may legitimately sit under any home dir;
    # what must not appear is anything hardcoded for one owner.
    text = text.replace(REPO, "<repo>")
    assert not CYRILLIC.search(text), f"non-English text in hook output: {text!r}"
    assert "/home/ubuntu" not in text, text
    assert "reimake" not in text.lower(), text


# ── guard_task_board ─────────────────────────────────────────────────────────
def edit(path=PROJECT_FILE, session="s1", tool="Edit"):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {"hook_event_name": "PreToolUse", "session_id": session,
            "tool_name": tool, "tool_input": {key: path}}


def bash(cmd, session="s1", background=False):
    ti = {"command": cmd}
    if background:
        ti["run_in_background"] = True
    return {"hook_event_name": "PreToolUse", "session_id": session,
            "tool_name": "Bash", "tool_input": ti}


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_board_blocks_first_edit(tmp_path, tool):
    p = run_hook(BOARD_HOOK, edit(tool=tool), tmp_path)
    assert p.returncode == 2
    assert "tracker.py" in p.stderr
    assert "NO_BOARD=1" in p.stderr
    assert PROJECT_FILE in p.stderr
    assert_clean(p.stderr + p.stdout)


def test_board_message_uses_repo_tracker_path(tmp_path):
    p = run_hook(BOARD_HOOK, edit(), tmp_path)
    assert os.path.join(REPO, "tasks-dashboard") in p.stderr


def test_board_tracker_path_from_env(tmp_path):
    p = run_hook(BOARD_HOOK, edit(), tmp_path, AGENTDECK_TRACKER="/opt/board/tracker.py")
    assert "/opt/board" in p.stderr


def test_board_url_shown_only_when_set(tmp_path):
    p = run_hook(BOARD_HOOK, edit(), tmp_path)
    assert "Board:" not in p.stderr
    p = run_hook(BOARD_HOOK, edit(), tmp_path, AGENTDECK_BOARD_URL="https://board.example/tasks/")
    assert "Board: https://board.example/tasks/" in p.stderr


def test_board_passes_after_tracker_call(tmp_path):
    assert run_hook(BOARD_HOOK, bash(f"{TRACKER} add-task t1 --title x"), tmp_path).returncode == 0
    assert run_hook(BOARD_HOOK, edit(), tmp_path).returncode == 0


def test_board_passes_after_no_board_escape(tmp_path):
    assert run_hook(BOARD_HOOK, bash("NO_BOARD=1 true"), tmp_path).returncode == 0
    assert run_hook(BOARD_HOOK, edit(), tmp_path).returncode == 0


def test_board_ordinary_bash_does_not_unlock(tmp_path):
    run_hook(BOARD_HOOK, bash("ls -la"), tmp_path)
    assert run_hook(BOARD_HOOK, edit(), tmp_path).returncode == 2


def test_board_unlock_is_per_session(tmp_path):
    run_hook(BOARD_HOOK, bash("NO_BOARD=1 true", session="other"), tmp_path)
    assert run_hook(BOARD_HOOK, edit(session="s1"), tmp_path).returncode == 2


def test_board_exempt_paths(tmp_path):
    home = tmp_path / "home"
    for path in ["/tmp/scratch.txt", "/var/tmp/x.py",
                 str(tmp_path / "anything.py"),  # under $TMPDIR
                 str(home / ".claude" / "memory" / "notes.md")]:
        p = run_hook(BOARD_HOOK, edit(path=path), tmp_path, HOME=home, TMPDIR=tmp_path)
        assert p.returncode == 0, path


def test_board_other_home_is_not_exempt(tmp_path):
    """~/.claude/ is computed from $HOME, not hardcoded to one user."""
    p = run_hook(BOARD_HOOK, edit(path="/home/someone/.claude/x.md"), tmp_path,
                 HOME=tmp_path / "home")
    assert p.returncode == 2


def test_board_ignores_other_tools(tmp_path):
    p = run_hook(BOARD_HOOK, {"tool_name": "Read", "tool_input": {"file_path": PROJECT_FILE}},
                 tmp_path)
    assert p.returncode == 0


def test_board_bad_json_fails_open(tmp_path):
    p = run_hook(BOARD_HOOK, None, tmp_path, raw="{not json")
    assert p.returncode == 0


def test_board_unwritable_marker_dir_fails_open(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    p = run_hook(BOARD_HOOK, bash("NO_BOARD=1 true"), tmp_path,
                 AGENTDECK_BOARD_MARKS=blocker / "sub")
    assert p.returncode == 0


# ── guard_dont_stop ──────────────────────────────────────────────────────────
def board(tmp_path, tasks):
    (tmp_path / "state.json").write_text(json.dumps({"tasks": tasks}), encoding="utf-8")


def stop(session="s1"):
    return {"hook_event_name": "Stop", "session_id": session}


def tool(name, session="s1", **ti):
    return {"hook_event_name": "PreToolUse", "session_id": session,
            "tool_name": name, "tool_input": ti}


def decision(tmp_path, payload, **env):
    p = run_hook(STOP_HOOK, payload, tmp_path, **env)
    assert p.returncode == 0
    out = p.stdout.strip()
    return json.loads(out) if out else None


def own(tmp_path, task_id="mine-1", session="s1"):
    decision(tmp_path, bash(f"{TRACKER} add-task {task_id} --title work", session=session))


def test_stop_blocks_with_own_active_task_and_no_wake(tmp_path):
    board(tmp_path, [{"id": "mine-1", "status": "active"}])
    own(tmp_path)
    r = decision(tmp_path, stop())
    assert r and r["decision"] == "block"
    assert "mine-1" in r["reason"]
    assert "run_in_background" in r["reason"]
    assert os.path.join(REPO, "tasks-dashboard") in r["reason"]
    assert "Board:" not in r["reason"]
    assert_clean(r["reason"])


def test_stop_message_includes_board_url_when_set(tmp_path):
    board(tmp_path, [{"id": "mine-1", "status": "active"}])
    own(tmp_path)
    r = decision(tmp_path, stop(), AGENTDECK_BOARD_URL="https://board.example/tasks/")
    assert "Board: https://board.example/tasks/" in r["reason"]


def test_stop_task_moved_by_us_is_ours(tmp_path):
    board(tmp_path, [{"id": "handed", "status": "active"}])
    decision(tmp_path, bash(f"{TRACKER} set handed 2 done --note ok"))
    assert decision(tmp_path, stop())["decision"] == "block"


def test_stop_dict_shaped_tasks(tmp_path):
    board(tmp_path, {"mine-1": {"id": "mine-1", "status": "active"}})
    own(tmp_path)
    assert decision(tmp_path, stop())["decision"] == "block"


@pytest.mark.parametrize("wake", [
    bash("./long.sh", background=True),
    tool("CronCreate", cron="*/5 * * * *", prompt="go on"),
    tool("ScheduleWakeup", delaySeconds=300),
    tool("Monitor", command="tail -f x"),
    tool("Agent", prompt="do it"),
    tool("Task", prompt="do it"),
])
def test_stop_fresh_wake_releases(tmp_path, wake):
    board(tmp_path, [{"id": "mine-1", "status": "active"}])
    own(tmp_path)
    decision(tmp_path, wake)
    assert decision(tmp_path, stop()) is None


def test_stop_foreground_bash_is_not_a_wake(tmp_path):
    board(tmp_path, [{"id": "mine-1", "status": "active"}])
    own(tmp_path)
    decision(tmp_path, bash("./quick.sh"))
    assert decision(tmp_path, stop())["decision"] == "block"


def test_stop_stale_wake_does_not_release(tmp_path):
    board(tmp_path, [{"id": "mine-1", "status": "active"}])
    own(tmp_path)
    decision(tmp_path, tool("ScheduleWakeup", delaySeconds=60))
    marks = tmp_path / "stop-marks"
    wake = [f for f in marks.iterdir() if f.name.endswith(".wake")][0]
    old = wake.stat().st_mtime - 11 * 60
    os.utime(wake, (old, old))
    assert decision(tmp_path, stop())["decision"] == "block"


def test_stop_ignores_other_sessions_tasks(tmp_path):
    board(tmp_path, [{"id": "theirs", "status": "active"}])
    own(tmp_path, "theirs", session="other")
    assert decision(tmp_path, stop(session="s1")) is None


def test_stop_ignores_untouched_tasks_on_shared_board(tmp_path):
    board(tmp_path, [{"id": "someone-else", "status": "active"}])
    assert decision(tmp_path, stop()) is None


def test_stop_reports_only_own_tasks(tmp_path):
    board(tmp_path, [{"id": "mine-1", "status": "active"},
                     {"id": "foreign-a", "status": "active"}])
    own(tmp_path)
    reason = decision(tmp_path, stop())["reason"]
    assert "mine-1" in reason and "foreign-a" not in reason


@pytest.mark.parametrize("task", [
    {"id": "mine-1", "status": "done"},
    {"id": "mine-1", "status": "todo"},
    {"id": "mine-1", "status": "active", "activity": "stopped", "activity_note": "left: X"},
])
def test_stop_ignores_closed_or_stopped_tasks(tmp_path, task):
    board(tmp_path, [task])
    own(tmp_path)
    assert decision(tmp_path, stop()) is None


def test_stop_working_activity_still_holds(tmp_path):
    board(tmp_path, [{"id": "mine-1", "status": "active", "activity": "working"}])
    own(tmp_path)
    assert decision(tmp_path, stop())["decision"] == "block"


def test_stop_lets_through_after_two_consecutive_blocks(tmp_path):
    board(tmp_path, [{"id": "mine-1", "status": "active"}])
    own(tmp_path)
    assert decision(tmp_path, stop())["decision"] == "block"
    assert decision(tmp_path, stop())["decision"] == "block"
    assert decision(tmp_path, stop()) is None


def test_stop_new_wake_resets_block_counter(tmp_path):
    board(tmp_path, [{"id": "mine-1", "status": "active"}])
    own(tmp_path)
    decision(tmp_path, stop())
    decision(tmp_path, stop())
    decision(tmp_path, tool("ScheduleWakeup", delaySeconds=60))
    marks = tmp_path / "stop-marks"
    wake = [f for f in marks.iterdir() if f.name.endswith(".wake")][0]
    old = wake.stat().st_mtime - 11 * 60
    os.utime(wake, (old, old))
    assert decision(tmp_path, stop())["decision"] == "block"


def test_stop_missing_state_fails_open(tmp_path):
    own(tmp_path)
    assert decision(tmp_path, stop()) is None


def test_stop_unreadable_state_fails_open(tmp_path):
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    own(tmp_path)
    assert decision(tmp_path, stop()) is None


def test_stop_state_is_a_directory_fails_open(tmp_path):
    own(tmp_path)
    d = tmp_path / "statedir"
    d.mkdir()
    assert decision(tmp_path, stop(), TRACKER_STATE=d) is None


def test_stop_bad_json_fails_open(tmp_path):
    p = run_hook(STOP_HOOK, None, tmp_path, raw="{not json")
    assert p.returncode == 0 and p.stdout.strip() == ""


def test_stop_unwritable_marker_dir_fails_open(tmp_path):
    board(tmp_path, [{"id": "mine-1", "status": "active"}])
    blocker = tmp_path / "file"
    blocker.write_text("x")
    p = run_hook(STOP_HOOK, stop(), tmp_path, AGENTDECK_STOP_MARKS=blocker / "sub")
    assert p.returncode == 0 and p.stdout.strip() == ""


# ── both: nothing owner-specific in the sources ──────────────────────────────
@pytest.mark.parametrize("hook", [BOARD_HOOK, STOP_HOOK])
def test_hook_source_is_generic(hook):
    assert_clean(open(hook, encoding="utf-8").read())


def test_settings_example_is_valid_and_wires_both_hooks():
    path = os.path.join(REPO, "hooks", "settings.example.json")
    text = open(path, encoding="utf-8").read()
    assert_clean(text)
    hooks = json.loads(text)["hooks"]
    flat = json.dumps(hooks)
    assert "guard_task_board.py" in flat and "guard_dont_stop.py" in flat
    matchers = {e.get("matcher"): json.dumps(e) for e in hooks["PreToolUse"]}
    assert "guard_task_board.py" in matchers["Bash"] and "guard_dont_stop.py" in matchers["Bash"]
    assert "guard_task_board.py" in matchers["Edit|Write|MultiEdit|NotebookEdit"]
    assert "guard_dont_stop.py" in matchers["CronCreate|ScheduleWakeup|Monitor|Agent|Task"]
    assert "guard_dont_stop.py" in json.dumps(hooks["Stop"])
