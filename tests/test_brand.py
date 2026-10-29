"""The brand a user sees is AgentKennel (renamed from AgentDeck, 29.10.2026).

A fresh install shows three pages before the dashboard: the setup-locked page (no
password yet, no link), the setup form (the one-time link) and the login page. Each must
say AgentKennel and never the old name. Since v2.1.0 the technical identifiers follow
(units agentkennel-*, the AGENTKENNEL_* variables, ~/agentkennel, the compose service);
the old ones are still accepted (tests/test_envcompat.py, tests/test_install_sh.py)."""
import http.client
import importlib
import socket
import sys
import threading
import urllib.parse
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_fresh_install_pages_say_agentkennel(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTKENNEL_PASSFILE", str(tmp_path / ".dashpass"))
    monkeypatch.setenv("AGENTKENNEL_AUTH_SECRET", str(tmp_path / ".secret"))
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
        return r.status, data

    try:
        pages = {}
        pages["setup-locked"] = req("GET", "/login")
        tok = m.setup_link_path().split("=", 1)[1]
        pages["setup"] = req("GET", "/login?setup=" + tok)
        st, _ = req("POST", "/login", {"pass": "owner-pw-1", "pass2": "owner-pw-1", "setup": tok})
        assert st == 302
        pages["login"] = req("GET", "/login")
        assert pages["setup-locked"][0] == 403 and "No password yet" in pages["setup-locked"][1]
        assert 'name="pass2"' in pages["setup"][1]
        assert 'name="pass"' in pages["login"][1] and 'name="pass2"' not in pages["login"][1]
        for name, (_, body) in pages.items():
            assert "🐾 AgentKennel" in body, name
            assert "<title>AgentKennel" in body, name
            assert "AgentDeck" not in body, name
    finally:
        s.shutdown()
        sys.modules.pop("status_server", None)
