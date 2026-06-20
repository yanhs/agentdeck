"""TDD: the task-board server must read state from TRACKER_STATE (the env the tracker
CLI already uses), so the entrypoint can point it at the persistent volume instead of the
hardcoded tasks-dashboard/state.json — which otherwise bakes the author's tasks into the
image and resets on every container recreate.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

TASKS_DIR = Path(__file__).resolve().parents[1] / "tasks-dashboard"
sys.path.insert(0, str(TASKS_DIR))


def _load_server():
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def test_server_honors_tracker_state(tmp_path, monkeypatch):
    sp = tmp_path / "tasks-state.json"
    monkeypatch.setenv("TRACKER_STATE", str(sp))
    assert str(_load_server().STATE) == str(sp)


def test_server_reads_state_from_configured_path(tmp_path, monkeypatch):
    sp = tmp_path / "tasks-state.json"
    sp.write_text(json.dumps({"title": "T", "tasks": [{"id": "x", "title": "Hi"}]}))
    monkeypatch.setenv("TRACKER_STATE", str(sp))
    line = _load_server().state_text()
    assert '"id":"x"' in line and '"Hi"' in line


def test_server_missing_state_is_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKER_STATE", str(tmp_path / "nope.json"))
    assert _load_server().state_text() == '{"tasks":[]}'   # graceful empty board


def test_server_defaults_next_to_module(monkeypatch):
    monkeypatch.delenv("TRACKER_STATE", raising=False)
    srv = _load_server()
    assert srv.STATE.name == "state.json"
    assert "tasks-dashboard" in str(srv.STATE)   # host behaviour unchanged
