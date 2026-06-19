"""TDD: POST /api/terminal-status accepts per-agent model + effort.

Body shapes:
  {"id": "3", "model":  "sonnet"}   → agents.json[3].model  == "sonnet"
  {"id": "3", "effort": "xhigh"}    → agents.json[3].effort == "xhigh"
  {"id": "3", "model": ""}          → drop model key (clear override)
  {"id": "3", "effort": ""}         → drop effort key (clear override)

Validation:
  - model:  any non-empty string (Claude accepts shortnames + full ids)
  - effort: one of {low, medium, high, xhigh, max, auto}, else 400

The handler must persist the new value to agents.json and respond 200.
GET response (do_GET) must echo `model` / `effort` per agent so the
dashboard can render the current setting.
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


def _get(mod):
    h = mod.Handler.__new__(mod.Handler)
    out: dict = {}

    def _resp(code, data):
        out["code"] = code; out["data"] = data

    h._json_response = _resp  # type: ignore[attr-defined]
    h.do_GET()
    return out


def _stub_subprocess(mod):
    return patch.object(
        mod.subprocess, "run",
        side_effect=lambda *a, **kw: MagicMock(returncode=1, stdout="", stderr="")
    )


# --- POST persistence ----------------------------------------------------


def test_post_model_persists(tmp_path, monkeypatch):
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"_order": ["3"]}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    out = _post(mod, {"id": "3", "model": "sonnet"})

    assert out["code"] == 200, out
    saved = json.loads(af.read_text())
    assert saved["3"]["model"] == "sonnet"


# --- live apply via tmux send-keys (the actual user-facing fix) ---


def test_post_model_sends_slash_command_to_live_tmux(tmp_path, monkeypatch):
    """If the agent's tmux session is alive, POSTing model=sonnet must also
    inject `/model sonnet<Enter>` into the running claude TUI so the change
    takes effect immediately, not just on next launch."""
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"_order": ["3"]}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["tmux", "has-session"]:
            return MagicMock(returncode=0, stdout="", stderr="")  # alive
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(mod.subprocess, "run", side_effect=fake_run):
        out = _post(mod, {"id": "3", "model": "sonnet"})

    assert out["code"] == 200
    send_keys = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert any("/model sonnet" in " ".join(c) for c in send_keys), (
        f"expected `/model sonnet` send-keys; got: {send_keys}"
    )


def test_post_effort_sends_slash_command_to_live_tmux(tmp_path, monkeypatch):
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"_order": ["3"]}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["tmux", "has-session"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(mod.subprocess, "run", side_effect=fake_run):
        _post(mod, {"id": "3", "effort": "xhigh"})

    sk = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert any("/effort xhigh" in " ".join(c) for c in sk), sk


def test_post_does_not_send_slash_when_tmux_is_dead(tmp_path, monkeypatch):
    """No tmux session → no send-keys (would error / spawn nothing useful)."""
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"_order": ["3"]}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["tmux", "has-session"]:
            return MagicMock(returncode=1, stdout="", stderr="no session")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(mod.subprocess, "run", side_effect=fake_run):
        out = _post(mod, {"id": "3", "model": "sonnet"})

    assert out["code"] == 200, out  # still persists to disk
    sk = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert not sk, f"expected no send-keys when tmux dead; got: {sk}"


def test_post_clearing_does_not_send_slash(tmp_path, monkeypatch):
    """Clearing the override (model=\"\") only updates the file — no slash
    command, because there's no canonical default to switch back to."""
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"3": {"model": "sonnet"}, "_order": ["3"]}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["tmux", "has-session"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(mod.subprocess, "run", side_effect=fake_run):
        _post(mod, {"id": "3", "model": ""})

    sk = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert not sk, sk


def test_post_effort_persists(tmp_path, monkeypatch):
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"_order": ["3"]}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    out = _post(mod, {"id": "3", "effort": "xhigh"})

    assert out["code"] == 200, out
    saved = json.loads(af.read_text())
    assert saved["3"]["effort"] == "xhigh"


def test_post_both_in_one_request(tmp_path, monkeypatch):
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"_order": ["3"]}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    out = _post(mod, {"id": "3", "model": "opus", "effort": "max"})
    assert out["code"] == 200, out
    saved = json.loads(af.read_text())["3"]
    assert saved == {"model": "opus", "effort": "max"} or \
           (saved.get("model") == "opus" and saved.get("effort") == "max")


def test_post_does_not_clobber_project_or_locked(tmp_path, monkeypatch):
    """Setting model/effort must merge into the existing entry, not replace it."""
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({
        "3": {"project": "Foo", "locked": True, "task": "bar"},
        "_order": ["3"],
    }))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    out = _post(mod, {"id": "3", "effort": "low"})
    assert out["code"] == 200, out
    saved = json.loads(af.read_text())["3"]
    assert saved["project"] == "Foo"
    assert saved["locked"] is True
    assert saved["task"] == "bar"
    assert saved["effort"] == "low"


# --- clearing ------------------------------------------------------------


def test_post_empty_model_clears_override(tmp_path, monkeypatch):
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"3": {"model": "sonnet"}, "_order": ["3"]}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    out = _post(mod, {"id": "3", "model": ""})
    assert out["code"] == 200, out
    assert "model" not in json.loads(af.read_text())["3"]


def test_post_empty_effort_clears_override(tmp_path, monkeypatch):
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"3": {"effort": "xhigh"}, "_order": ["3"]}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    out = _post(mod, {"id": "3", "effort": ""})
    assert out["code"] == 200, out
    assert "effort" not in json.loads(af.read_text())["3"]


# --- validation ----------------------------------------------------------


@pytest.mark.parametrize("eff", ["low", "medium", "high", "xhigh", "max", "auto"])
def test_post_accepts_known_effort_levels(tmp_path, monkeypatch, eff):
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text("{}")
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    out = _post(mod, {"id": "3", "effort": eff})
    assert out["code"] == 200, out
    assert json.loads(af.read_text())["3"]["effort"] == eff


def test_post_rejects_bogus_effort(tmp_path, monkeypatch):
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text("{}")
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    out = _post(mod, {"id": "3", "effort": "ultra"})
    assert out["code"] == 400, out
    # Must NOT have written anything
    assert json.loads(af.read_text()) == {}


# --- GET exposes the new fields -----------------------------------------


def test_get_echoes_model_and_effort(tmp_path, monkeypatch):
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({
        "3": {"model": "sonnet", "effort": "xhigh"},
        "_order": ["3"],
    }))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    with _stub_subprocess(mod):
        out = _get(mod)

    assert out["code"] == 200
    entry = out["data"]["3"]
    assert entry.get("model") == "sonnet"
    assert entry.get("effort") == "xhigh"


def test_get_omits_model_effort_when_unset(tmp_path, monkeypatch):
    """Don't surface noisy defaults — UI uses absent keys to render placeholders."""
    mod = _load()
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"_order": ["3"]}))
    monkeypatch.setattr(mod, "AGENTS_FILE", str(af))

    with _stub_subprocess(mod):
        out = _get(mod)

    entry = out["data"]["3"]
    assert "model" not in entry or entry["model"] == ""
    assert "effort" not in entry or entry["effort"] == ""
