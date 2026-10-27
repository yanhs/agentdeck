"""Browser contract for the Services block of web/server.html (AgentDeck 2.0).

The page's /api/server and /api/services are faked by page.route() (see
test_server_page.py for the static site and the browser). Screenshots go to
tests/artifacts/ (untracked).
"""
from __future__ import annotations

import json
import re

import pytest

from tests.test_server_page import FakeServerAPI, browser, shot, site  # noqa: F401

NOW = 1_790_000_000.0


def services():
    return {
        "web": {"state": "running", "pid": 101, "since": NOW - 3 * 3600, "restarts": 0,
                "last_exit": None, "log": "web.log"},
        "tunnel": {"state": "waiting", "pid": None, "since": None, "restarts": 4,
                   "last_exit": 1, "log": "tunnel.log"},
        "old-bot": {"state": "stopped", "pid": None, "since": None, "restarts": 0,
                    "last_exit": None, "log": "old-bot.log"},
    }


class FakeServices:
    def __init__(self):
        self.available = True
        self.services = services()
        self.posts = []

    def handle(self, route):
        req = route.request
        path = re.sub(r"^https?://[^/]+", "", req.url).split("?")[0]
        if req.method == "POST":
            self.posts.append((path, req.headers.get("content-type", "")))
            action, name = path.split("/")[-2:]
            st = self.services.get(name)
            if st is None:
                return route.fulfill(status=404, content_type="application/json",
                                     body='{"error": "no such service"}')
            st["state"] = {"stop": "stopped", "start": "running", "restart": "running"}[action]
            return route.fulfill(status=200, content_type="application/json", body='{"ok": true}')
        if path.startswith("/api/services/log/"):
            name = path.rsplit("/", 1)[1]
            lines = [f"{name}: line {i}" for i in range(30)] + ["<b>not html</b>"]
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"name": name, "lines": lines}))
        body = {"available": self.available,
                "services": self.services if self.available else {},
                "dir": "/home/me/.services", "now": NOW}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def svc():
    return FakeServices()


@pytest.fixture
def page(browser, site, svc):  # noqa: F811
    ctx, pg = _open_with(browser, site, svc)
    yield pg
    assert pg.errors == []
    ctx.close()


def _open_with(browser, site, svc, width=1280, height=900, mobile=False):  # noqa: F811
    """test_server_page._open, with /api/services faked on the context first."""
    ctx = browser.new_context(viewport={"width": width, "height": height},
                              is_mobile=mobile, has_touch=mobile)
    ctx.set_default_timeout(10000)
    ctx.route(re.compile(r"/api/services(/|\?|$)"), svc.handle)
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda msg: pg.errors.append(msg.text) if msg.type == "error"
          and "Failed to load resource" not in msg.text else None)
    pg.route(re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/"), lambda r: r.abort())
    pg.route(re.compile(r"/api/server(\?|$)"), FakeServerAPI().handle)
    pg.goto(site + "?poll=300")
    pg.wait_for_selector(".row[data-id]")
    return ctx, pg


def rows(pg):
    pg.wait_for_selector("#services .svc[data-name]")
    return pg.eval_on_selector_all(
        "#services .svc[data-name]",
        "els => els.map(e => [e.dataset.name, e.dataset.state, e.innerText])")


def test_lists_every_service_with_its_state(page):
    got = {n: (s, t) for n, s, t in rows(page)}
    assert set(got) == {"web", "tunnel", "old-bot"}
    assert got["web"][0] == "running" and "running" in got["web"][1].lower()
    assert "3 h" in got["web"][1]
    assert got["tunnel"][0] == "waiting" and "exit 1" in got["tunnel"][1]
    assert "restarts: 4" in got["tunnel"][1].lower()
    assert got["old-bot"][0] == "stopped" and "stopped" in got["old-bot"][1].lower()
    assert "1 of 3" in page.inner_text("#svcSum")
    shot(page, "server-services-desktop.png")


def test_buttons_match_the_state(page):
    rows(page)
    btns = lambda n: page.eval_on_selector_all(  # noqa: E731
        f'#services .svc[data-name="{n}"] button[data-act]', "els => els.map(e => e.dataset.act)")
    assert btns("web") == ["log", "restart", "stop"]
    assert btns("old-bot") == ["log", "start"]


def test_stop_posts_json_and_redraws(page, svc):
    rows(page)
    page.click('#services .svc[data-name="web"] button[data-act="stop"]')
    page.wait_for_selector('#services .svc[data-name="web"][data-state="stopped"]')
    assert svc.posts[0][0] == "/api/services/stop/web"
    assert svc.posts[0][1].startswith("application/json")
    assert page.eval_on_selector_all('#services .svc[data-name="web"] button[data-act]',
                                     "els => els.map(e => e.dataset.act)") == ["log", "start"]


def test_start_a_stopped_one(page, svc):
    rows(page)
    page.click('#services .svc[data-name="old-bot"] button[data-act="start"]')
    page.wait_for_selector('#services .svc[data-name="old-bot"][data-state="running"]')
    assert svc.posts == [("/api/services/start/old-bot", "application/json")]


def test_log_opens_under_the_row_as_text(page):
    rows(page)
    page.click('#services .svc[data-name="web"] button[data-act="log"]')
    page.wait_for_selector('#services .svc[data-name="web"] pre.svc-log')
    text = page.inner_text('#services .svc[data-name="web"] pre.svc-log')
    assert "web: line 29" in text and "<b>not html</b>" in text
    shot(page, "server-services-log.png")
    page.click('#services .svc[data-name="web"] button[data-act="log"]')
    page.wait_for_selector('#services .svc[data-name="web"] pre.svc-log', state="detached")


def test_no_services_explains_what_they_are(browser, site, svc):  # noqa: F811
    svc.services = {}
    ctx, pg = _open_with(browser, site, svc)
    pg.wait_for_selector("#svcEmpty:not([hidden])")
    assert "no services yet" in pg.inner_text("#svcEmpty").lower()
    assert pg.errors == []
    ctx.close()


def test_keeper_down_says_so(browser, site, svc):  # noqa: F811
    svc.available = False
    ctx, pg = _open_with(browser, site, svc)
    pg.wait_for_selector("#svcEmpty:not([hidden])")
    assert "keeper isn't running" in pg.inner_text("#svcEmpty").lower()
    assert pg.errors == []
    ctx.close()


def test_phone_width_no_sideways_scroll(browser, site, svc):  # noqa: F811
    ctx, pg = _open_with(browser, site, svc, width=390, height=844, mobile=True)
    rows(pg)
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(pg, "server-services-phone.png")
    assert pg.errors == []
    ctx.close()


def test_light_theme(browser, site, svc):  # noqa: F811
    ctx, pg = _open_with(browser, site, svc)
    pg.evaluate("localStorage.setItem('agentdeck-theme', 'light')")
    pg.reload()
    rows(pg)
    shot(pg, "server-services-light.png")
    assert pg.errors == []
    ctx.close()
