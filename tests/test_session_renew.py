"""Sliding session: while the dashboard is in use the login cookie keeps renewing.

25.10.2026: the 30-day cookie counted from the LOGIN, so a panel used every day still
signed you out once a month (and the expired terminal pane nested a second dashboard).
GET /login?renew=1 with a valid cookie answers 204 with a fresh 30-day cookie; without a
valid cookie it answers 401 and sets nothing. /login* is reachable without a session in
every setup (nginx `location = /login`, Caddy `handle /login*`), so no proxy change.
"""
import http.client
import importlib
import socket
import sys
import threading
import time
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDECK_PASSFILE", str(tmp_path / ".dashpass"))
    monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(tmp_path / ".secret"))
    sys.modules.pop("status_server", None)
    m = importlib.import_module("status_server")
    monkeypatch.setattr(m, "HTPASSWD_FILE", str(tmp_path / "none"))
    m._pw_store("pw-123456")
    port = _free_port()
    s = ThreadingHTTPServer(("127.0.0.1", port), m.AuthHandler)
    threading.Thread(target=s.serve_forever, daemon=True).start()

    def get(path, cookie=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = f"{m.AUTH_COOKIE}={cookie}"
        c.request("GET", path, headers=h)
        r = c.getresponse()
        r.read()
        c.close()
        return r

    yield m, get
    s.shutdown()
    sys.modules.pop("status_server", None)


def _exp(set_cookie, m):
    tok = set_cookie.split(";")[0].split("=", 1)[1]
    assert m._verify_token(tok) == "admin"
    return int(tok.split(".")[1])


def test_valid_cookie_gets_a_fresh_30_day_one(srv):
    m, get = srv
    old = m._sign_token("admin", int(time.time()) + 3600)       # one hour left
    r = get("/login?renew=1", cookie=old)
    assert r.status == 204
    sc = r.getheader("Set-Cookie")
    assert sc and sc.startswith(f"{m.AUTH_COOKIE}=")
    assert "HttpOnly" in sc and "Path=/" in sc and f"Max-Age={m.AUTH_TTL}" in sc
    assert _exp(sc, m) >= int(time.time()) + m.AUTH_TTL - 5
    assert r.getheader("Cache-Control") == "no-store"


def test_renewal_keeps_the_user(srv):
    m, get = srv
    r = get("/login?renew=1", cookie=m._sign_token("yanhs", int(time.time()) + 60))
    tok = r.getheader("Set-Cookie").split(";")[0].split("=", 1)[1]
    assert m._verify_token(tok) == "yanhs"


@pytest.mark.parametrize("cookie", [None, "garbage", "admin.1.deadbeef"])
def test_no_valid_cookie_no_renewal(srv, cookie):
    m, get = srv
    r = get("/login?renew=1", cookie=cookie)
    assert r.status == 401
    assert r.getheader("Set-Cookie") is None


def test_expired_cookie_is_not_revived(srv):
    m, get = srv
    r = get("/login?renew=1", cookie=m._sign_token("admin", int(time.time()) - 1))
    assert r.status == 401 and r.getheader("Set-Cookie") is None


def test_secure_flag_behind_https(srv):
    m, get = srv
    tok = m._sign_token("admin", int(time.time()) + 60)
    assert "Secure" in get("/login?renew=1", cookie=tok,
                           headers={"X-Forwarded-Proto": "https"}).getheader("Set-Cookie")
    assert "Secure" not in get("/login?renew=1", cookie=tok).getheader("Set-Cookie")


def test_plain_login_page_unchanged(srv):
    m, get = srv
    r = get("/login", cookie=m._sign_token("admin", int(time.time()) + 60))
    assert r.status == 200 and r.getheader("Set-Cookie") is None
