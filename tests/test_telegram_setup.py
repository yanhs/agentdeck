"""TDD for the dashboard's Telegram-bot setup (status_server tg_* helpers).

The dashboard lets you configure the optional Telegram bridge from the browser:
enter a bot token + your Telegram user id -> the config is saved to the volume and
the bridge process is (re)started. On container restart it auto-starts if enabled.
These tests cover the config store, status, start, and autostart logic — without
actually spawning the bridge or talking to Telegram.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

TERMINAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TERMINAL_DIR))


def _load():
    sys.modules.pop("status_server", None)
    return importlib.import_module("status_server")


def test_config_roundtrip(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "TG_CONF", str(tmp_path / "telegram.json"))
    assert mod.tg_load() == {}                       # nothing saved yet
    mod.tg_save({"token": "T", "owner": "1", "enabled": True})
    assert mod.tg_load() == {"token": "T", "owner": "1", "enabled": True}


def test_running_zero_without_pidfile(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "TG_PID", str(tmp_path / "tg.pid"))
    assert mod.tg_running() == 0


def test_render_shows_status_and_saved_values(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "TG_CONF", str(tmp_path / "telegram.json"))
    monkeypatch.setattr(mod, "TG_PID", str(tmp_path / "tg.pid"))
    mod.tg_save({"token": "123:ABC", "owner": "777", "enabled": True})
    html = mod.tg_render()
    assert "stopped" in html                         # no live pid -> stopped
    assert "123:ABC" in html and "777" in html       # form is pre-filled
    assert 'action="/telegram"' in html


def test_start_passes_token_and_owner_via_env(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "TG_PID", str(tmp_path / "tg.pid"))
    fake = MagicMock()
    fake.pid = os.getpid()                            # pretend the bridge = a live pid
    with patch.object(mod.subprocess, "Popen", return_value=fake) as pop, \
         patch.object(mod.os, "makedirs"):
        assert mod.tg_start("TOKEN", "777") is True
        env = pop.call_args.kwargs["env"]
        assert env["TG_BRIDGE_TOKEN"] == "TOKEN"
        assert env["TG_BRIDGE_OWNER"] == "777"
    assert mod.tg_running() == os.getpid()            # pidfile written + process alive


def test_autostart_starts_only_when_enabled(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "TG_CONF", str(tmp_path / "telegram.json"))
    monkeypatch.setattr(mod, "tg_running", lambda: 0)

    # enabled -> tg_start called with the saved creds
    mod.tg_save({"token": "T", "owner": "1", "enabled": True})
    called = {}
    monkeypatch.setattr(mod, "tg_start", lambda t, o: called.update(t=t, o=o))
    mod.tg_autostart()
    assert called == {"t": "T", "o": "1"}

    # disabled -> tg_start must NOT be called
    mod.tg_save({"token": "T", "owner": "1", "enabled": False})
    monkeypatch.setattr(mod, "tg_start",
                        lambda *a: pytest.fail("must not start when disabled"))
    mod.tg_autostart()
