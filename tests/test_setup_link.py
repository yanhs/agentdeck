"""First run: only the holder of the setup link may set the dashboard password.

29.10.2026: with no password yet, /login showed "Set a password" to ANYONE, and the first
POST owned the dashboard (a shell with your sudo). A fresh sslip.io certificate lands in
the public certificate logs within minutes, and bots read those logs — the window between
the install and your first visit was enough. Now the password can be set only through
/login?setup=<token>: a random token kept next to the passfile (<passfile>.setup, 0600),
printed by the installer / the container's log (`status_server.py --setup-link`), and
deleted once the password is set.
"""
import http.client
import importlib
import os
import socket
import stat
import subprocess
import sys
import threading
import urllib.parse
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


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
    port = _free_port()
    s = ThreadingHTTPServer(("127.0.0.1", port), m.AuthHandler)
    threading.Thread(target=s.serve_forever, daemon=True).start()

    def req(method, path, form=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        c.request(method, path, body=body, headers=hdrs)
        r = c.getresponse()
        data = r.read().decode()
        c.close()
        return r, data

    yield m, req, tmp_path
    s.shutdown()
    sys.modules.pop("status_server", None)


def _token(m):
    link = m.setup_link_path()
    assert link.startswith("/login?setup=")
    return link.split("=", 1)[1]


def test_login_without_the_link_does_not_offer_the_setup_form(srv):
    m, req, _ = srv
    r, body = req("GET", "/login")
    assert r.status == 403
    assert 'name="pass2"' not in body
    assert "setup link" in body and "--setup-link" in body     # says where to get it


def test_post_without_the_token_cannot_set_the_password(srv):
    m, req, _ = srv
    r, _ = req("POST", "/login", {"pass": "attacker-1", "pass2": "attacker-1"})
    assert r.status == 403 and r.getheader("Set-Cookie") is None
    assert not m._pw_is_set()
    r, _ = req("POST", "/login", {"pass": "attacker-1", "pass2": "attacker-1", "setup": "guess"})
    assert r.status == 403 and not m._pw_is_set()


def test_the_link_shows_the_form_and_sets_the_password_once(srv):
    m, req, tmp = srv
    tok = _token(m)
    r, body = req("GET", "/login?setup=" + tok)
    assert r.status == 200 and 'name="pass2"' in body
    assert f'name="setup" value="{tok}"' in body              # carried into the POST
    r, _ = req("POST", "/login", {"pass": "owner-pw-1", "pass2": "owner-pw-1", "setup": tok})
    assert r.status == 302 and r.getheader("Set-Cookie")
    assert m._pw_is_set() and m._pw_verify("owner-pw-1")
    assert not (tmp / ".dashpass.setup").exists()              # the link is spent
    assert m.setup_link_path() == ""
    r, body = req("GET", "/login?setup=" + tok)                # now an ordinary login
    assert 'name="pass2"' not in body


def test_a_mismatch_keeps_the_token_in_the_form(srv):
    m, req, _ = srv
    tok = _token(m)
    r, body = req("POST", "/login", {"pass": "owner-pw-1", "pass2": "other", "setup": tok})
    assert "must match" in body and f'name="setup" value="{tok}"' in body
    assert not m._pw_is_set()


def test_the_token_is_stable_private_and_unguessable(srv):
    m, _, tmp = srv
    a = _token(m)
    assert _token(m) == a                                      # same link until used
    f = tmp / ".dashpass.setup"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert len(a) >= 24


def test_no_link_once_a_password_is_set(srv):
    m, req, tmp = srv
    m._pw_store("pw-123456")
    assert m.setup_link_path() == ""
    assert not (tmp / ".dashpass.setup").exists()
    r, body = req("GET", "/login")
    assert r.status == 200 and 'name="pass2"' not in body       # the normal login


def test_cli_prints_the_link(tmp_path):
    env = dict(os.environ, AGENTDECK_PASSFILE=str(tmp_path / ".dashpass"),
               AGENTDECK_AUTH_SECRET=str(tmp_path / ".secret"),
               AGENTDECK_HTPASSWD=str(tmp_path / "none"))   # no legacy nginx login here
    p = subprocess.run([sys.executable, str(ROOT / "status_server.py"), "--setup-link"],
                       env=env, capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    tok = (tmp_path / ".dashpass.setup").read_text().strip()
    assert p.stdout.strip() == "/login?setup=" + tok
    (tmp_path / ".dashpass").write_text("x$y")                 # password set → no link
    p = subprocess.run([sys.executable, str(ROOT / "status_server.py"), "--setup-link"],
                       env=env, capture_output=True, text=True, timeout=20)
    assert p.returncode == 1 and p.stdout.strip() == ""
