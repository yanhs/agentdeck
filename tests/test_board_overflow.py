"""The /tasks/ board must never scroll horizontally, on phone or desktop.

A long unbroken token in a task title, note, id or agent string is what breaks
it: flex children without min-width:0 refuse to shrink and text without
overflow-wrap refuses to break, so the card grows past the viewport. These
tests drive the real board page through Chromium with a deliberately
pathological state.json and assert the document never overflows sideways.
"""
import http.server
import json
import re
import socket
import threading
from contextlib import closing
from functools import partial
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "tasks-dashboard" / "static"

LONG = "https://agents.reimake.com/" + "verylongunbrokensegment" * 6  # ~150 chars, no spaces
STATE = {
    "updated": "2026-08-29T18:00:00+00:00",
    "tasks": [
        {
            "id": "svetlota-errcheck-" + "x" * 40,
            "title": "Светлота: проверить сайт на ошибки — " + LONG,
            "agent": "claude · " + "a" * 60,
            "status": "active",
            "activity": "working",
            "activity_note": LONG,
            "created": "2026-08-29T17:00:00+00:00",
            "updated": "2026-08-29T17:59:00+00:00",
            "items": [
                {"status": "done", "title": "short one", "note": ""},
                {"status": "active", "title": LONG, "note": "Fixed: " + LONG},
                {"status": "todo", "title": "нормальный пункт", "note": ""},
            ],
        },
        {
            "id": "plain", "title": "Обычная задача", "agent": "claude · x",
            "status": "done", "created": "2026-08-29T16:00:00+00:00",
            "updated": "2026-08-29T16:30:00+00:00",
            "items": [{"status": "done", "title": "готово", "note": ""}],
        },
    ],
}


def _free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def site():
    port = _free_port()
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(STATIC))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.RequestHandlerClass.log_message = lambda *a, **k: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/index.html"
    srv.shutdown()


def _overflow_at(site, width):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": width, "height": 800})
        pg.route(re.compile(r"/state\.json"),
                 lambda r: r.fulfill(status=200, content_type="application/json",
                                     body=json.dumps(STATE)))
        pg.route(re.compile(r"/events"), lambda r: r.abort())
        pg.goto(site)
        pg.wait_for_selector(".card")
        pg.wait_for_timeout(200)
        over = pg.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        b.close()
        return over


def test_no_horizontal_scroll_on_phone(site):
    assert _overflow_at(site, 390) <= 1


def test_no_horizontal_scroll_on_small_phone(site):
    assert _overflow_at(site, 320) <= 1


def test_no_horizontal_scroll_on_desktop(site):
    assert _overflow_at(site, 1440) <= 1
