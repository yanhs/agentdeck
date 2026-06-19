"""TDD: a card-level "compact" action sends `/compact` into the agent's
running tmux session.

`/compact` is the claude TUI slash command that summarises the
conversation so far to reclaim context-window space. The dashboard fires
it via:

    POST /api/terminal-status {"id": "3", "action": "compact"}

Contract:
  - tmux session alive → send `/compact<Enter>` to that session, 200 OK
  - tmux session dead  → 200 OK, nothing sent (idempotent "no-op")
  - unknown action     → 400
  - unknown id         → 400 (handled by the existing id check)
"""
from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load():
    sys.modules.pop("status_server", None)
    return importlib.import_module("status_server")


def _post(mod, body):
    h = mod.Handler.__new__(mod.Handler)
    h.headers = {"Content-Length": str(len(json.dumps(body)))}
    raw = json.dumps(body).encode()
    h.rfile = types.SimpleNamespace(read=lambda n: raw)
    out: dict = {}

    def _resp(code, data):
        out["code"] = code; out["data"] = data

    h._json_response = _resp  # type: ignore[attr-defined]
    h.do_POST()
    return out


def _spy_run(calls, *, alive: bool):
    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["tmux", "has-session"]:
            return MagicMock(returncode=0 if alive else 1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")
    return fake_run


def test_compact_sends_slash_command_when_tmux_alive(tmp_path, monkeypatch):
    mod = _load()
    af = tmp_path / "agents.json"; af.write_text("{}")
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    calls: list[list[str]] = []
    with patch.object(mod.subprocess, "run", side_effect=_spy_run(calls, alive=True)):
        out = _post(mod, {"id": "3", "action": "compact"})

    assert out["code"] == 200, out
    sk = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert any("/compact" in " ".join(c) for c in sk), (
        f"expected /compact send-keys, got: {sk}"
    )


def test_compact_is_noop_when_tmux_dead(tmp_path, monkeypatch):
    mod = _load()
    af = tmp_path / "agents.json"; af.write_text("{}")
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    calls: list[list[str]] = []
    with patch.object(mod.subprocess, "run", side_effect=_spy_run(calls, alive=False)):
        out = _post(mod, {"id": "3", "action": "compact"})

    assert out["code"] == 200, out
    sk = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert not sk, sk


def test_compact_does_not_modify_agents_json(tmp_path, monkeypatch):
    """The /compact action is purely runtime — nothing should be persisted."""
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"3": {"project": "Foo"}}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    with patch.object(mod.subprocess, "run", side_effect=_spy_run([], alive=True)):
        _post(mod, {"id": "3", "action": "compact"})

    assert json.loads(af.read_text()) == {"3": {"project": "Foo"}}


def test_unknown_action_returns_400(tmp_path, monkeypatch):
    mod = _load()
    af = tmp_path / "agents.json"; af.write_text("{}")
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    with patch.object(mod.subprocess, "run", side_effect=_spy_run([], alive=True)):
        out = _post(mod, {"id": "3", "action": "self_destruct"})

    assert out["code"] == 400, out
    assert "error" in out["data"]
