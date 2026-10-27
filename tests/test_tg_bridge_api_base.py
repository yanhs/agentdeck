"""TG_BRIDGE_API_BASE: the bridge can talk to Telegram through a relay.

A guest sandbox must not hold the real bot token. The host runs a relay that holds
it and gives each sandbox only its own user's updates; the bridge inside the
sandbox points its Bot API base URL at that relay. Unset = api.telegram.org, as before.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TG_BRIDGE_TOKEN", "test:token")
os.environ.setdefault("TG_BRIDGE_OWNER", "1")
os.environ["TG_CONVO_LOG"] = "/tmp/test_tg_convo.log"

import tg_bridge as tb  # noqa: E402


def test_default_is_telegram(monkeypatch):
    monkeypatch.delenv("TG_BRIDGE_API_BASE", raising=False)
    bot = tb.build_app().bot
    assert bot.base_url == "https://api.telegram.org/bot" + tb.TOKEN
    assert bot.base_file_url == "https://api.telegram.org/file/bot" + tb.TOKEN


def test_relay_base_url_is_used(monkeypatch):
    monkeypatch.setenv("TG_BRIDGE_API_BASE", "http://172.31.250.1:8792/")
    bot = tb.build_app().bot
    assert bot.base_url == "http://172.31.250.1:8792/bot" + tb.TOKEN
    assert bot.base_file_url == "http://172.31.250.1:8792/file/bot" + tb.TOKEN
