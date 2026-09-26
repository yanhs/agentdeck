"""hooks/install_guards.py — turns the guard hooks on (or off) in a Claude Code settings file.

The Docker entrypoint runs it on every start so the agents in the container have the task
board guards by default (opt-out: AGENTDECK_GUARDS=0). The settings file lives in a
persisted volume the user may have edited, so the merge must be idempotent, keep every other
key and every hook the user added, and never overwrite a file it cannot parse.

Also installs a short default ~/.claude/CLAUDE.md telling agents how to use the board —
only when none exists.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "hooks", "install_guards.py")
HOOKS = os.path.join(REPO, "hooks")
EXAMPLE = os.path.join(HOOKS, "settings.example.json")


def run(tmp_path, *args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(
        [sys.executable, INSTALL, "--settings", str(tmp_path / "settings.json"),
         "--hooks-dir", HOOKS, *args],
        capture_output=True, text=True, env=e, timeout=30)


def settings(tmp_path):
    return json.loads((tmp_path / "settings.json").read_text())


def commands(s, event=None):
    out = []
    for ev, groups in (s.get("hooks") or {}).items():
        if event and ev != event:
            continue
        for g in groups:
            for h in g.get("hooks", []):
                out.append(h.get("command", ""))
    return out


def guard_commands(s):
    return [c for c in commands(s) if "guard_task_board.py" in c or "guard_dont_stop.py" in c]


# ── install ──────────────────────────────────────────────────────────────────
def test_installs_into_a_missing_file_same_shape_as_the_example(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    s = settings(tmp_path)
    ex = json.load(open(EXAMPLE))
    # derived from settings.example.json: same events, same matchers, same hook count
    for ev, groups in ex["hooks"].items():
        assert [g.get("matcher") for g in s["hooks"][ev]] == [g.get("matcher") for g in groups]
    assert len(guard_commands(s)) == len(commands(ex))
    assert not any("/path/to/agentdeck" in c for c in commands(s))
    for c in guard_commands(s):
        script = c.split()[-1]
        assert os.path.isfile(script), c          # points at the real hook scripts
    assert any("guard_dont_stop.py" in c for c in commands(s, "Stop"))


def test_board_state_is_baked_into_the_hook_command(tmp_path):
    r = run(tmp_path, "--tracker-state", "/app/.sessions/tasks-state.json")
    assert r.returncode == 0, r.stderr
    for c in guard_commands(settings(tmp_path)):
        assert c.startswith("TRACKER_STATE=/app/.sessions/tasks-state.json "), c


def test_idempotent(tmp_path):
    run(tmp_path)
    first = (tmp_path / "settings.json").read_text()
    for _ in range(3):
        assert run(tmp_path).returncode == 0
    assert (tmp_path / "settings.json").read_text() == first


def test_keeps_other_keys_and_the_users_own_hooks(tmp_path):
    user = {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "python3 /mine/audit.py"}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(user))
    assert run(tmp_path).returncode == 0
    run(tmp_path)
    s = settings(tmp_path)
    assert s["model"] == "opus" and s["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert commands(s).count("python3 /mine/audit.py") == 1
    assert commands(s, "SessionStart") == ["echo hi"]
    assert guard_commands(s)
    # remove takes ours out and leaves theirs
    assert run(tmp_path, "--remove").returncode == 0
    s = settings(tmp_path)
    assert guard_commands(s) == []
    assert commands(s, "PreToolUse") == ["python3 /mine/audit.py"]
    assert commands(s, "SessionStart") == ["echo hi"]
    assert "Stop" not in s["hooks"]                 # emptied groups/events are dropped
    assert s["model"] == "opus"


def test_moving_the_state_path_replaces_not_duplicates(tmp_path):
    run(tmp_path, "--tracker-state", "/a/state.json")
    run(tmp_path, "--tracker-state", "/b/state.json")
    cs = guard_commands(settings(tmp_path))
    assert len(cs) == len(commands(json.load(open(EXAMPLE))))
    assert all(c.startswith("TRACKER_STATE=/b/state.json ") for c in cs)


def test_remove_on_a_missing_file_creates_nothing(tmp_path):
    assert run(tmp_path, "--remove").returncode == 0
    assert not (tmp_path / "settings.json").exists()


def test_unparseable_settings_are_left_alone(tmp_path):
    (tmp_path / "settings.json").write_text("{ not json")
    r = run(tmp_path)
    assert r.returncode == 0                       # never breaks the container start
    assert (tmp_path / "settings.json").read_text() == "{ not json"
    assert "not valid JSON" in r.stderr or "not valid JSON" in r.stdout


def test_check_reports_state(tmp_path):
    assert run(tmp_path, "--check").returncode == 1
    run(tmp_path)
    assert run(tmp_path, "--check").returncode == 0
    run(tmp_path, "--remove")
    assert run(tmp_path, "--check").returncode == 1


# ── default CLAUDE.md ────────────────────────────────────────────────────────
def test_claude_md_written_only_when_missing(tmp_path):
    md = tmp_path / "CLAUDE.md"
    r = run(tmp_path, "--claude-md", str(md), "--tracker", "/app/tasks-dashboard/tracker.py")
    assert r.returncode == 0, r.stderr
    text = md.read_text()
    assert "python3 /app/tasks-dashboard/tracker.py add-task" in text
    assert "add-item" in text and "set-task" in text and " set " in text
    assert "run_in_background" in text or "background" in text
    assert "ScheduleWakeup" in text or "timer" in text
    assert text.isascii() and len(text.splitlines()) < 40
    md.write_text("my own rules\n")
    run(tmp_path, "--claude-md", str(md))
    assert md.read_text() == "my own rules\n"


def test_remove_never_touches_claude_md(tmp_path):
    md = tmp_path / "CLAUDE.md"
    run(tmp_path, "--claude-md", str(md))
    before = md.read_text()
    run(tmp_path, "--remove", "--claude-md", str(md))
    assert md.read_text() == before


# ── end to end: the installed command really guards ─────────────────────────
def test_installed_command_blocks_then_allows(tmp_path):
    state = tmp_path / "state.json"
    run(tmp_path, "--tracker-state", str(state))
    s = settings(tmp_path)
    edit_cmd = next(c for g in s["hooks"]["PreToolUse"] if "Edit" in (g.get("matcher") or "")
                    for c in [h["command"] for h in g["hooks"]])
    bash_cmd = next(c for g in s["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"
                    for c in [h["command"] for h in g["hooks"]] if "guard_task_board" in c)
    env = dict(os.environ, AGENTDECK_BOARD_MARKS=str(tmp_path / "m"))

    def sh(cmd, payload):
        return subprocess.run(["sh", "-c", cmd], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=30)

    ed = {"session_id": "e2e", "tool_name": "Edit",
          "tool_input": {"file_path": "/srv/x.py"}}
    r = sh(edit_cmd, ed)
    assert r.returncode == 2 and "BLOCKED" in r.stderr
    sh(bash_cmd, {"session_id": "e2e", "tool_name": "Bash",
                  "tool_input": {"command": "python3 tracker.py add-task t1 --title x"}})
    assert sh(edit_cmd, ed).returncode == 0
