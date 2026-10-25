"""The login page never renders inside a terminal pane.

25.10.2026: the session cookie expired while the dashboard was open. The terminal iframe
reloaded into /login, the password was typed THERE, and the post-login redirect to "/"
put a whole second dashboard inside the pane (two sidebars, one nested in the other).
The login/setup pages now move the whole tab to /login when they find themselves framed.
"""
import importlib
import socket
import sys
import threading
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def ss(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDECK_PASSFILE", str(tmp_path / ".dashpass"))
    monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(tmp_path / ".secret"))
    sys.modules.pop("status_server", None)
    m = importlib.import_module("status_server")
    monkeypatch.setattr(m, "HTPASSWD_FILE", str(tmp_path / "none"))
    yield m
    sys.modules.pop("status_server", None)


def test_login_and_setup_pages_break_out_of_frames(ss):
    ss._pw_store("pw-123456")
    assert "window.top!==window.self" in ss.login_html()
    assert "window.top!==window.self" in ss.login_html(error=True)
    assert "window.top!==window.self" in ss._setup_form()


def _free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_framed_login_takes_over_the_whole_tab(ss):
    """Real browser: a dashboard-like page frames /login → the TAB ends up on /login."""
    pw = pytest.importorskip("playwright.sync_api")
    ss._pw_store("pw-123456")

    class H(ss.AuthHandler):
        def do_GET(self):
            if self.path == "/dash":
                return self._html('<h1 id="dash">dashboard</h1><iframe src="/login"></iframe>')
            return super().do_GET()

    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pw.sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page()
            page.goto(f"http://127.0.0.1:{port}/dash")
            page.wait_for_url(f"http://127.0.0.1:{port}/login", timeout=5000)
            assert page.locator("input[name=pass]").count() == 1
            assert page.locator("iframe").count() == 0
            b.close()
    finally:
        srv.shutdown()
