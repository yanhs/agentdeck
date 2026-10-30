"""TDD: a guest's dashboard shows today's Claude spend (decision 30.10.2026).

In a guest's sandbox Claude goes through the host's turnstile (ANTHROPIC_BASE_URL) with the
guest's own pass (ANTHROPIC_AUTH_TOKEN). The dashboard asks the turnstile for that pass's
numbers and shows "today X of Y tokens":

    GET /api/quota -> {"available": false}                 not behind a turnstile
                   -> {"available": true, "spent_today", "limit", "enabled",
                       "unlimited_until", "resets_at"}     from GET <base>/guest/usage
                   -> {"available": false, "error": "..."} the turnstile did not answer well

The pass never goes to the page; the answer is cached for a few seconds.
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import pytest

TERMINAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TERMINAL_DIR))

PASS = "sbx-" + "p" * 40
USAGE = {"guest": "maria", "spent_today": 603374, "limit": 37_300_000, "enabled": True,
         "unlimited_until": 0, "resets_at": 1793430000}


class FakeTurnstile:
    def __init__(self):
        self.calls = []
        self.status, self.body = 200, json.dumps(USAGE).encode()
        me = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                me.calls.append((self.path, self.headers.get("authorization")))
                self.send_response(me.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(me.body)))
                self.end_headers()
                self.wfile.write(me.body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture
def q(tmp_path, monkeypatch):
    t = FakeTurnstile()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", t.base)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", PASS)
    monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(tmp_path / "k"))
    sys.modules.pop("status_server", None)
    ss = importlib.import_module("status_server")
    srv = HTTPServer(("127.0.0.1", 0), ss.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class Q:
        turnstile = t
        mod = ss
        base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield Q
    finally:
        srv.shutdown()
        srv.server_close()
        t.srv.shutdown()


def get(base, path="/api/quota"):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_a_guest_sees_today_and_the_limit(q):
    code, raw = get(q.base)
    assert code == 200
    d = json.loads(raw)
    assert d == {"available": True, "spent_today": 603374, "limit": 37_300_000, "enabled": True,
                 "unlimited_until": 0, "resets_at": 1793430000}
    assert q.turnstile.calls == [("/guest/usage", f"Bearer {PASS}")]
    assert PASS not in raw                                   # the pass never reaches the page


def test_the_answer_is_cached_for_a_while(q):
    get(q.base)
    get(q.base)
    assert len(q.turnstile.calls) == 1


@pytest.mark.parametrize("env", [{"ANTHROPIC_BASE_URL": None}, {"ANTHROPIC_AUTH_TOKEN": None},
                                 {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}],
                         ids=["no-base", "no-pass", "anthropic-itself"])
def test_not_behind_a_turnstile_there_is_nothing_to_show(q, monkeypatch, env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k)
        else:
            monkeypatch.setenv(k, v)
    q.mod.QUOTA.clear()
    assert json.loads(get(q.base)[1]) == {"available": False}
    assert q.turnstile.calls == []


@pytest.mark.parametrize("status,body", [(401, b'{"error":"x"}'), (502, b"oops"), (200, b"not json"),
                                         (200, b'{"spent_today":"lots","limit":1}'),
                                         (200, b'{"spent_today":1}')],
                         ids=["refused", "down", "garbage", "bad-number", "missing-limit"])
def test_a_bad_answer_hides_the_numbers(q, status, body):
    q.turnstile.status, q.turnstile.body = status, body
    d = json.loads(get(q.base)[1])
    assert d["available"] is False and d["error"]
    assert PASS not in json.dumps(d)


def test_the_proxies_route_quota_to_the_status_server():
    for f in ("Caddyfile", "docker/Caddyfile"):
        assert "handle /api/quota" in (TERMINAL_DIR / f).read_text(), f
    assert "location = /api/quota" in (TERMINAL_DIR / "nginx/agents-subdomain.conf").read_text()
