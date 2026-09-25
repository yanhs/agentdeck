"""tracker.py records which Claude session boarded a task: the first 8 hex of
CLAUDE_CODE_SESSION_ID, the same code the dashboard uses for a terminal."""
import json
import os
import subprocess
import sys
from pathlib import Path

TRACKER = Path(__file__).resolve().parents[1] / "tasks-dashboard" / "tracker.py"
SID = "943d9794-a508-4c54-8225-08b2ad8887da"


def run(state, *args, sid=SID):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["TRACKER_STATE"] = str(state)
    if sid is not None:
        env["CLAUDE_CODE_SESSION_ID"] = sid
    subprocess.run([sys.executable, str(TRACKER), *args], env=env, check=True,
                   capture_output=True, text=True)
    return {t["id"]: t for t in json.loads(state.read_text())["tasks"]}


def test_add_task_records_the_session(tmp_path):
    st = tmp_path / "state.json"
    t = run(st, "add-task", "x", "--title", "X", "--agent", "claude · terminal")["x"]
    assert t["session"] == "943d9794"
    assert t["agent"] == "claude · terminal"


def test_no_session_env_means_no_field(tmp_path):
    st = tmp_path / "state.json"
    assert "session" not in run(st, "add-task", "x", sid=None)["x"]


def test_garbage_session_env_is_ignored(tmp_path):
    st = tmp_path / "state.json"
    assert "session" not in run(st, "add-task", "x", sid="not-a-uuid")["x"]


def test_later_commands_fill_a_missing_session_but_never_replace_one(tmp_path):
    st = tmp_path / "state.json"
    run(st, "add-task", "old", sid=None)
    run(st, "add-item", "old", "step", sid=None)
    t = run(st, "set", "old", "0", "active")["old"]
    assert t["session"] == "943d9794"                        # filled by whoever works on it
    other = "aaaabbbb-0000-0000-0000-000000000000"
    for args in (("set", "old", "0", "done"), ("set-task", "old", "done"),
                 ("state", "old", "stopped"), ("add-item", "old", "more")):
        assert run(st, *args, sid=other)["old"]["session"] == "943d9794", args


def test_add_task_again_takes_the_task_over(tmp_path):
    st = tmp_path / "state.json"
    run(st, "add-task", "x")
    other = "aaaabbbb-0000-0000-0000-000000000000"
    assert run(st, "add-task", "x", sid=other)["x"]["session"] == "aaaabbbb"
