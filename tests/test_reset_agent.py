"""TDD: × button on /agents/ unloads an agent from RAM.

`POST /api/terminal-status {"id": "...", "reset": true}` must:
  1. Kill the agent's tmux session (so the Claude process leaves RAM).
  2. PRESERVE the Claude conversation JSONL on disk — when the agent is
     re-added later via "+ Claude", launch-claude-*.sh runs `--resume`
     against the same UUID and the conversation continues.
  3. PRESERVE the agent's overrides entry in agents.json (project / task
     / locked) — re-adding via "+ Claude" must keep the same name, not
     come back blank.
"""
from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock


TERMINAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TERMINAL_DIR))


def _load(mod_name: str):
    sys.modules.pop(mod_name, None)
    return importlib.import_module(mod_name)


def _fake_launch_script(tmp: Path, basename: str, session_name: str, uuid: str) -> Path:
    p = tmp / basename
    p.write_text(
        "#!/bin/bash\n"
        f'SESSION="{session_name}"\n'
        f'AGENT_SESSION_ID="{uuid}"\n'
    )
    return p


def _call_reset(mod, body: dict):
    handler = mod.Handler.__new__(mod.Handler)
    handler.headers = {"Content-Length": str(len(json.dumps(body)))}
    raw = json.dumps(body).encode()
    handler.rfile = types.SimpleNamespace(read=lambda n: raw)

    captured = {}

    def _resp(code, data):
        captured["code"] = code
        captured["data"] = data

    handler._json_response = _resp  # type: ignore[attr-defined]
    handler.do_POST()
    return captured


def test_reset_kills_tmux_preserves_jsonl_AND_overrides(tmp_path, monkeypatch):
    """× freezes the agent (kill tmux) but preserves both the conversation JSONL
    AND the project/task overrides — so re-adding via "+ Claude" later
    restores the same name + history."""
    mod = _load("status_server")

    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _fake_launch_script(tmp_path, "launch-claude.sh", "claude-terminal", uuid)
    monkeypatch.setattr(mod, "__file__", str(tmp_path / "status_server.py"))

    fake_home = tmp_path / "home"
    proj_dir = fake_home / ".claude" / "projects" / "-home-ubuntu-pr"
    proj_dir.mkdir(parents=True)
    jsonl = proj_dir / f"{uuid}.jsonl"
    jsonl.write_text('{"role":"user","content":"hello"}\n')
    monkeypatch.setenv("HOME", str(fake_home))

    agents_file = tmp_path / "agents.json"
    agents_file.write_text(json.dumps({
        "1": {"project": "MyProj", "task": "fix bug", "locked": True},
        "2": {"project": "Other"},
    }))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(agents_file))

    killed = []
    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["tmux", "kill-session"]:
            killed.append(cmd[3])
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(mod.subprocess, "run", side_effect=fake_run):
        out = _call_reset(mod, {"id": "1", "reset": True})

    assert out["code"] == 200, out
    assert out["data"]["ok"] is True
    assert "claude-terminal" in killed, f"tmux not killed; got {killed}"
    # JSONL preserved on disk
    assert jsonl.exists()
    assert jsonl.read_text() == '{"role":"user","content":"hello"}\n'
    # Overrides PRESERVED — fix for "name gets wiped on re-add"
    saved = json.loads(agents_file.read_text())
    assert saved.get("1") == {"project": "MyProj", "task": "fix bug", "locked": True}, \
        f"agent #1 overrides must survive reset; got {saved.get('1')}"
    assert saved.get("2") == {"project": "Other"}


def test_reset_rejects_unknown_id(tmp_path, monkeypatch):
    mod = _load("status_server")
    agents_file = tmp_path / "agents.json"
    agents_file.write_text("{}")
    monkeypatch.setattr(mod, "AGENTS_FILE", str(agents_file))
    monkeypatch.setattr(mod, "__file__", str(tmp_path / "status_server.py"))

    with patch.object(mod.subprocess, "run",
                      return_value=MagicMock(returncode=0, stdout="", stderr="")):
        out = _call_reset(mod, {"id": "999", "reset": True})

    assert out["code"] == 400, out
    assert "error" in out["data"]


def test_reset_is_idempotent_when_tmux_already_dead(tmp_path, monkeypatch):
    """tmux kill-session returns non-zero if the session doesn't exist —
    reset must still respond 200 (idempotent unload)."""
    mod = _load("status_server")

    uuid = "deadbeef-0000-1111-2222-333344445555"
    _fake_launch_script(tmp_path, "launch-claude-2.sh", "claude-terminal-2", uuid)
    monkeypatch.setattr(mod, "__file__", str(tmp_path / "status_server.py"))
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "projects" / "-home-ubuntu-pr").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    agents_file = tmp_path / "agents.json"
    agents_file.write_text("{}")
    monkeypatch.setattr(mod, "AGENTS_FILE", str(agents_file))

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["tmux", "kill-session"]:
            return MagicMock(returncode=1, stdout="", stderr="no session")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(mod.subprocess, "run", side_effect=fake_run):
        out = _call_reset(mod, {"id": "2", "reset": True})

    assert out["code"] == 200, f"should be idempotent, got {out}"
    assert out["data"]["ok"] is True
