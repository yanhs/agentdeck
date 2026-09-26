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
import shutil
import subprocess
import sys
import tempfile
import time
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
    # `=name`: exactly that session (test_reset_never_reaches_another_slot)
    assert "=claude-terminal" in killed, f"tmux not killed; got {killed}"
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


# ── the old numbered slots: a tmux target is exact, never a prefix ─────────────
# tmux takes a bare `-t <name>` as a prefix when no session has that exact name, and
# slot #1's session is plain `claude-terminal` — the prefix of every other slot. With #1
# not running and one other slot up, `kill-session -t claude-terminal` (reset of #1)
# killed that other terminal; `has-session` + `send-keys` typed /compact, /model or
# /effort into it. These run a real tmux server of their own (a private TMUX_TMPDIR: the
# legacy code calls bare `tmux`) and signal nothing they did not start.
class _PrivateTmux:
    def __init__(self, monkeypatch):
        self.dir = tempfile.mkdtemp(prefix="rst-")      # short: a socket path is limited
        monkeypatch.setenv("TMUX_TMPDIR", self.dir)
        monkeypatch.delenv("TMUX", raising=False)

    def __call__(self, *a):
        return subprocess.run(["tmux", *a], capture_output=True, text=True, timeout=10)

    def names(self):
        return sorted(self("list-sessions", "-F", "#{session_name}").stdout.split())

    def close(self):
        self("kill-server")
        shutil.rmtree(self.dir, ignore_errors=True)


def test_reset_never_reaches_another_slot(tmp_path, monkeypatch):
    mod = _load("status_server")
    tm = _PrivateTmux(monkeypatch)
    try:
        for name in ("claude-terminal-10", "other"):
            assert tm("new-session", "-d", "-s", name, "cat").returncode == 0
        assert mod.reset_agent("1", "claude-terminal") == {"tmux_killed": False}
        assert mod.reset_agent("1", "claude-terminal-1") == {"tmux_killed": False}
        assert tm.names() == ["claude-terminal-10", "other"]
        assert tm("new-session", "-d", "-s", "claude-terminal", "cat").returncode == 0
        assert mod.reset_agent("1", "claude-terminal") == {"tmux_killed": True}
        assert tm.names() == ["claude-terminal-10", "other"]
    finally:
        tm.close()


def _post(mod, body):
    h = mod.Handler.__new__(mod.Handler)
    raw = json.dumps(body).encode()
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = types.SimpleNamespace(read=lambda n: raw)
    out = {}
    h._json_response = lambda code, data: out.update(code=code, data=data)
    h.do_POST()
    return out


def test_slash_commands_never_reach_another_slot(tmp_path, monkeypatch):
    """/compact, /model, /effort for a slot that is not running go nowhere — not into
    the one other slot that happens to run."""
    mod = _load("status_server")
    agents_file = tmp_path / "agents.json"
    agents_file.write_text("{}")
    monkeypatch.setattr(mod, "AGENTS_FILE", str(agents_file))
    typed = tmp_path / "typed.txt"
    tm = _PrivateTmux(monkeypatch)
    try:
        assert tm("new-session", "-d", "-s", "claude-terminal-10",
                  f"cat > '{typed}'").returncode == 0
        for body in ({"id": "1", "action": "compact"}, {"id": "1", "model": "sonnet"},
                     {"id": "1", "effort": "high"}):
            out = _post(mod, body)
            assert out["code"] == 200, out
        assert out["data"] == {"ok": True}
        assert _post(mod, {"id": "1", "action": "compact"})["data"]["sent"] is False
        time.sleep(0.3)
        assert not typed.exists() or typed.read_text() == "", typed.read_text()
        # the slot itself still gets them
        assert _post(mod, {"id": "10", "action": "compact"})["data"]["sent"] is True
        deadline = time.time() + 5
        while time.time() < deadline and "/compact" not in (typed.read_text()
                                                             if typed.exists() else ""):
            time.sleep(0.05)
        assert "/compact" in typed.read_text()
    finally:
        tm.close()


def test_a_slot_that_is_not_running_shows_as_not_running(monkeypatch):
    """The legacy status poll asked `has-session -t claude-terminal`: slot #1 showed as
    running whenever exactly one other slot ran (and with that slot's screen)."""
    mod = _load("status_server")
    tm = _PrivateTmux(monkeypatch)
    try:
        assert tm("new-session", "-d", "-s", "claude-terminal-10", "cat").returncode == 0
        calls = []
        real = subprocess.run

        def spy(cmd, *a, **kw):
            if cmd[:1] == ["tmux"]:
                calls.append(list(cmd))
            return real(cmd, *a, **kw)
        monkeypatch.setattr(mod.subprocess, "run", spy)
        mod.get_pane_pid("claude-terminal")
        mod.parse_pane("claude-terminal")
        for c in calls:
            if "-t" in c:
                t = c[c.index("-t") + 1]
                assert t.startswith("="), c
        assert mod.get_pane_pid("claude-terminal") is None
        assert mod.parse_pane("claude-terminal") == ("", "")
        assert mod.get_pane_pid("claude-terminal-10") is not None
    finally:
        tm.close()
