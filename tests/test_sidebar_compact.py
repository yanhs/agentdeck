"""Visual/computed-style contract for the dashboard's left column.

The sidebar must stay compact: narrow, with small non-bold text and small
buttons, and it must not overflow horizontally at that width. These are
computed-style assertions driven through a real browser, because the rules
live in a <style> block inside web/index.html and only a layout engine can
tell us whether the header still fits once everything shrinks.
"""
import http.server
import json
import socket
import threading
from contextlib import closing
from functools import partial
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Trimmed snapshot of a real /api/terminal-status payload (long project names
# and a long task string on purpose — that is what stresses the narrow column).
API_PAYLOAD = {
    "1": {"active": True, "working": True, "path": "terminal", "project": "Terminal",
          "task": "agents.reimake.com сделай левую колонку уже", "locked": True},
    "2": {"active": True, "working": False, "path": "terminal2",
          "project": "app - news, seo, encription, magiclink",
          "task": "давай делай проверку законов через eCFR API", "locked": True},
    "3": {"active": False, "working": False, "path": "terminal3",
          "project": "Instagram infuencers", "task": "", "locked": True},
    "4": {"active": False, "working": False, "path": "terminal4",
          "project": "Term - TG", "task": "", "locked": True},
    "5": {"active": True, "working": False, "path": "terminal5",
          "project": "app - design, outreach, docker", "task": "", "locked": True},
    "6": {"active": True, "working": False, "path": "terminal6",
          "project": "app - pipeline", "task": "", "locked": True},
    "7": {"active": False, "working": False, "path": "terminal7",
          "project": "UpWork", "task": "", "locked": True},
    "_order": ["6", "2", "5", "1", "3", "4", "7"],
}


def _free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def site():
    port = _free_port()
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB_DIR))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.RequestHandlerClass.log_message = lambda *a, **k: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/index.html"
    srv.shutdown()


@pytest.fixture(scope="module")
def page(site):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1440, "height": 900})
        # The dashboard is auth-gated in production; here we stub its only data
        # source so the agent cards render exactly as they do live.
        pg.route("**/api/terminal-status",
                 lambda route: route.fulfill(status=200, content_type="application/json",
                                             body=json.dumps(API_PAYLOAD)))
        pg.route("**/api/page-version", lambda route: route.abort())
        pg.goto(site)
        pg.wait_for_selector(".card .proj")
        pg.wait_for_timeout(300)
        yield pg
        browser.close()


def px(page, selector, prop):
    return float(page.eval_on_selector(
        selector, f"el => parseFloat(getComputedStyle(el).{prop})"))


def test_sidebar_is_narrow(page):
    """~a third narrower than the original 260px."""
    assert px(page, ".sidebar", "width") <= 178


def test_header_font_is_small_and_not_bold(page):
    assert px(page, ".sidebar-header", "fontSize") <= 12.0
    assert px(page, ".sidebar-header", "fontWeight") <= 400


def test_project_name_is_small_and_not_bold(page):
    assert px(page, ".card .proj", "fontSize") <= 12.5
    assert px(page, ".card .proj", "fontWeight") <= 400


def test_task_line_is_small(page):
    assert px(page, ".card .task", "fontSize") <= 11.0


def test_add_buttons_are_small_and_not_bold(page):
    assert px(page, ".add-btn", "fontSize") <= 9.0
    assert px(page, ".add-btn", "fontWeight") <= 400
    assert px(page, ".add-btn", "height") <= 18


def test_card_buttons_are_small(page):
    assert px(page, ".card-btn", "width") <= 19
    assert px(page, ".card-btn", "height") <= 19


def test_header_does_not_overflow_at_that_width(page):
    """Everything in the header row must still fit side by side."""
    overflow = page.eval_on_selector(
        ".sidebar-header", "el => el.scrollWidth - el.clientWidth")
    assert overflow <= 1, f"sidebar header overflows by {overflow}px"


def test_sidebar_does_not_scroll_horizontally(page):
    overflow = page.eval_on_selector(
        ".agent-list", "el => el.scrollWidth - el.clientWidth")
    assert overflow <= 1, f"agent list overflows by {overflow}px"


def test_card_buttons_stay_inside_the_column(page):
    """The ⇲ ✎ × cluster must not be pushed past the sidebar's right edge."""
    bad = page.evaluate("""() => {
      const right = document.querySelector('.sidebar').getBoundingClientRect().right;
      return [...document.querySelectorAll('.card-btn')]
        .filter(b => b.getBoundingClientRect().right > right + 0.5).length;
    }""")
    assert bad == 0, f"{bad} card buttons stick out of the sidebar"
