"""Browser contract for web/index-lib.html — the dashboard with a session library.

The left column is no longer twelve numbered slots but a library of named
topics (sessions): search, «＋ New topic», loaded ones first with a status dot,
unloaded ones greyed, each row = name + 8-hex code + ✎ + «Archive» (one click).
«Show archived» at the bottom lists archived topics greyed, each with «Delete»;
one click on an archived row restores it and opens it (no Restore button).
Archive unloads the terminal (the server says unloaded true|false + reason).
A click opens the one terminal endpoint `/sess/?arg=<id>` in the iframe.
All UI text is English (owner, 2026-09-25) — topic names are user data.

Everything runs against a static copy of web/ with the API faked by
page.route(): no tmux, no status_server, no live session is touched.
Screenshots land in tests/artifacts/.
"""
import http.server
import json
import re
import socket
import threading
from contextlib import closing
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
PAGE = WEB_DIR / "index-lib.html"
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"

ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def base_sessions():
    # Deliberately NOT in display order: the page itself must put loaded first,
    # then most recently used.
    return [
        {"id": "aaaa0001", "name": "ImmAppeal деплой", "cwd": "/home/ubuntu/pr",
         "created": 1, "last_used": 100, "archived": False,
         "active": False, "attached": False, "status": "off"},
        {"id": "bbbb0002", "name": "Светлота сайт", "cwd": "/home/ubuntu/pr",
         "created": 2, "last_used": 50, "archived": False,
         "active": True, "attached": False, "status": "working"},
        {"id": "cccc0003", "name": "Старая тема", "cwd": "/home/ubuntu/pr",
         "created": 3, "last_used": 10, "archived": False,
         "active": False, "attached": False, "status": "off"},
        {"id": "dddd0004", "name": "TG мост", "cwd": "/home/ubuntu/pr",
         "created": 4, "last_used": 200, "archived": False,
         "active": True, "attached": True, "status": "idle"},
    ]


class FakeAPI:
    """In-memory stand-in for status_server's /api/library* (contract part B)."""

    def __init__(self, with_system=False):
        self.sessions = base_sessions()
        self.calls = []            # [(method, path, body)]
        self.urls = []             # full request URLs (to check the query)
        self.with_system = with_system
        self.fail_new = False
        self.fail_reorder = False
        self.fail_shell_close = False
        self.fail_archive = False
        self.busy = set()          # ids the fake server reports as working on Archive
        self.shell = None          # top-level "shell" of GET /api/library (None = absent)
        self._n = 0

    def gets(self):
        return [c for c in self.calls if c[0] == "GET" and c[1] == "/api/library"]

    def posts(self, path=None):
        return [c for c in self.calls if c[0] == "POST" and (path is None or c[1] == path)]

    def _json(self, route, obj, status=200):
        route.fulfill(status=status, content_type="application/json",
                      body=json.dumps(obj, ensure_ascii=False))

    def library(self, route):
        req = route.request
        path = urlparse(req.url).path
        body = None
        if req.method == "POST":
            try:
                body = json.loads(req.post_data or "{}")
            except ValueError:
                body = {}
        self.calls.append((req.method, path, body))
        self.urls.append(req.url)

        if req.method == "GET" and path == "/api/library":
            q = urlparse(req.url).query
            with_arch = "archived=1" in q
            out = {"max_active": 12,
                   "sessions": [s for s in self.sessions if with_arch or not s["archived"]]}
            if self.with_system:
                out["_system"] = {"cpu_pct": 42, "ram_pct": 50,
                                  "ram_used_mb": 7000, "ram_total_mb": 16000}
            if self.shell is not None:
                out["shell"] = self.shell
            return self._json(route, out)
        if req.method != "POST":
            return self._json(route, {"error": "method"}, 405)

        if path == "/api/library/new":
            if self.fail_new:
                return self._json(route, {"error": "диск полон"}, 500)
            self._n += 1
            sid = f"eeee000{self._n}"
            e = {"id": sid, "name": (body.get("name") or "").strip() or "Terminal 24.09 10:00",
                 "cwd": "/home/ubuntu/pr", "created": 999, "last_used": 999,
                 "archived": False, "active": False, "attached": False, "status": "off"}
            self.sessions.append(e)
            return self._json(route, e)
        if path == "/api/library/shell-close":
            if self.fail_shell_close:
                return self._json(route, {"error": "tmux gone"}, 500)
            was = bool(self.shell and self.shell.get("active"))
            self.shell = {"active": False, "attached": False, "status": "off"}
            return self._json(route, {"ok": True, "killed": was})
        if path == "/api/library/reorder":
            sid = (body.get("ids") or [None])[0]
        else:
            sid = (body or {}).get("id")
        e = next((s for s in self.sessions if s["id"] == sid), None)
        if e is None:
            return self._json(route, {"error": "unknown id"}, 404)
        if path == "/api/library/rename":
            e["name"] = body["name"]
            return self._json(route, e)
        if path == "/api/library/archive":
            if self.fail_archive:
                return self._json(route, {"error": "disk full"}, 500)
            e["archived"] = bool(body.get("archived"))
            if not e["archived"]:
                return self._json(route, e)
            loaded = e["active"]
            busy = loaded and sid in self.busy
            if loaded and not busy:
                e["active"], e["status"] = False, "off"
            return self._json(route, dict(
                e, unloaded=loaded and not busy, unload_pending=busy,
                reason="working" if busy else "idle" if loaded else "not-loaded"))
        if path == "/api/library/reorder":
            if self.fail_reorder:
                return self._json(route, {"error": "disk full"}, 500)
            for i, x in enumerate(body["ids"]):
                next(s for s in self.sessions if s["id"] == x)["pos"] = i
            return self._json(route, {"ok": True})
        if path == "/api/library/close":
            e["active"] = False
            return self._json(route, e)
        if path == "/api/library/delete":
            if not e["archived"] or e["active"]:
                return self._json(route, {"error": "topic is not archived"}, 409)
            self.sessions.remove(e)
            return self._json(route, {"ok": True, "trashed": f"/x/trash/{sid}.jsonl"})
        return self._json(route, {"error": "no such route"}, 404)


@pytest.fixture(scope="module")
def site():
    port = _free_port()
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB_DIR))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.RequestHandlerClass.log_message = lambda *a, **k: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/index-lib.html"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _open(browser, site, api, width=1440, height=900, mobile=False,
          user_agent=None, init_script=None):
    kw = {"user_agent": user_agent} if user_agent else {}
    ctx = browser.new_context(viewport={"width": width, "height": height},
                              is_mobile=mobile, has_touch=mobile, **kw)
    if init_script:
        ctx.add_init_script(init_script)
    ctx.set_default_timeout(10000)
    pg = ctx.new_page()
    pg.route(re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/"), lambda r: r.abort())
    pg.route(re.compile(r"/api/library(/|\?|$)"), api.library)
    pg.route(re.compile(r"/api/terminal-status(\?|$)"),
             lambda r: r.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"_system": {"cpu_pct": 12, "ram_pct": 40,
                                                              "ram_used_mb": 6400,
                                                              "ram_total_mb": 16000}})))
    pg.route(re.compile(r"/api/page-version"), lambda r: r.abort())
    # the terminal endpoint: a tiny stand-in page instead of ttyd
    pg.route(re.compile(r"/sess/"),
             lambda r: r.fulfill(status=200, content_type="text/html",
                                 body="<html><body style='background:#000;color:#0f0'>"
                                      "ttyd stub</body></html>"))
    pg.goto(site)
    pg.wait_for_selector(".card[data-sid]")
    return ctx, pg


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def page(browser, site, api):
    ctx, pg = _open(browser, site, api)
    yield pg
    ctx.close()


def row_ids(page):
    return page.eval_on_selector_all(
        "#list .card[data-sid]",
        "els => els.filter(e => e.offsetParent !== null).map(e => e.dataset.sid)")


def row(sid):
    return f'#list .card[data-sid="{sid}"]'


def shot(page, name):
    ARTIFACTS.mkdir(exist_ok=True)
    page.screenshot(path=str(ARTIFACTS / name), full_page=False)


# ── the file itself ─────────────────────────────────────────────────────────
def test_page_exists_and_old_page_untouched():
    assert PAGE.is_file()
    # after the migration (2026-09-24) index.html IS the library page, and the
    # old slot dashboard is kept as index-legacy.html for rollback
    assert (WEB_DIR / "index.html").read_text(encoding="utf-8") == PAGE.read_text(encoding="utf-8")
    assert "CLAUDE_IDS" in (WEB_DIR / "index-legacy.html").read_text(encoding="utf-8")


def test_slot_machinery_is_gone_from_the_source():
    src = PAGE.read_text(encoding="utf-8")
    for needle in ("CLAUDE_IDS", "OO_IDS", "OO_CONFIG", "agent-order",
                   "+ Claude", "Add a Claude agent", "'#' + id", "launch-claude"):
        assert needle not in src, f"slot leftover in index-lib.html: {needle!r}"
    # the slot-order key of /api/terminal-status (not e.g. «display_order» in prose)
    assert not re.search(r"\b_order\b", src), "slot _order handling left in the page"
    assert not re.search(r"['\"`]/terminal\d*['\"`/?]", src), "iframe still points at /terminalN"


def test_existing_features_are_kept_in_the_source():
    src = PAGE.read_text(encoding="utf-8")
    for fn in ("function enableIframeTouchScroll", "function enableClipboardCopy",
               "function enablePasteImage", "function suppressContextMenu",
               "function _pasteImageFromClipboard", "function _uploadAndInsertImage",
               "function sendToTerminal", "/api/tmux-buffer/", "/api/paste-image",
               "/api/page-version"):
        assert fn in src, f"lost feature: {fn}"


# ── rendering ───────────────────────────────────────────────────────────────
def test_rows_show_name_and_short_code(page):
    shot(page, "index-lib-desktop.png")
    assert sorted(row_ids(page)) == ["aaaa0001", "bbbb0002", "cccc0003", "dddd0004"]
    assert page.inner_text(row("bbbb0002") + " .proj") == "Светлота сайт"
    assert page.inner_text(row("bbbb0002") + " .sid") == "bbbb0002"
    # the code is small — smaller than the name
    name_px = float(page.eval_on_selector(row("bbbb0002") + " .proj",
                                          "e => parseFloat(getComputedStyle(e).fontSize)"))
    code_px = float(page.eval_on_selector(row("bbbb0002") + " .sid",
                                          "e => parseFloat(getComputedStyle(e).fontSize)"))
    assert code_px < name_px


def test_loaded_first_then_most_recent(page):
    assert row_ids(page) == ["dddd0004", "bbbb0002", "aaaa0001", "cccc0003"]


def test_status_dots_and_greyed_unloaded(page):
    dot = lambda sid: page.get_attribute(row(sid) + " .dot", "class")
    assert "working" in dot("bbbb0002")
    assert "idle" in dot("dddd0004")
    assert "off" in dot("aaaa0001") and "off" in dot("cccc0003")
    for sid in ("aaaa0001", "cccc0003"):
        assert "unloaded" in page.get_attribute(row(sid), "class")
    for sid in ("bbbb0002", "dddd0004"):
        assert "unloaded" not in page.get_attribute(row(sid), "class")
    # greyed = visibly dimmer name than a loaded row
    lum = """e => { const [r,g,b] = getComputedStyle(e).color.match(/\\d+/g).map(Number);
                    const o = parseFloat(getComputedStyle(e.closest('.card')).opacity);
                    return (0.2126*r + 0.7152*g + 0.0722*b) * o; }"""
    assert page.eval_on_selector(row("aaaa0001") + " .proj", lum) < \
        page.eval_on_selector(row("bbbb0002") + " .proj", lum) * 0.8


def test_names_are_text_not_html(browser, site):
    api = FakeAPI()
    api.sessions[0]["name"] = "<img src=x onerror=\"window.__xss=1\"><b>жирно</b>"
    api.sessions.append({"id": "BAD'); x(", "name": "плохой код", "active": True,
                         "status": "idle", "last_used": 1, "archived": False})
    ctx, pg = _open(browser, site, api)
    try:
        assert pg.inner_text(row("aaaa0001") + " .proj").startswith("<img")
        assert pg.evaluate("window.__xss") is None
        assert "плохой код" not in pg.inner_text("#list")   # invalid id → row skipped
    finally:
        ctx.close()


# ── search ──────────────────────────────────────────────────────────────────
def test_search_filters_by_name_and_code(page):
    page.fill("#search", "свет")
    assert row_ids(page) == ["bbbb0002"]
    page.fill("#search", "IMMAPPEAL")                  # case-insensitive
    assert row_ids(page) == ["aaaa0001"]
    page.fill("#search", "cccc")                       # code prefix
    assert row_ids(page) == ["cccc0003"]
    page.fill("#search", "нет такого")
    assert row_ids(page) == []
    page.fill("#search", "")
    assert len(row_ids(page)) == 4


def test_search_enter_opens_the_top_match(page):
    page.fill("#search", "тем")                        # «Старая тема» only
    page.press("#search", "Enter")
    page.wait_for_selector("#wrap iframe")
    assert page.get_attribute("#wrap iframe", "src") == "/sess/?arg=cccc0003"


def test_search_survives_a_poll(page, api):
    page.fill("#search", "свет")
    n = len(api.gets())
    page.wait_for_timeout(4600)                        # one poll period (4 s)
    assert len(api.gets()) > n, "the list is not polled every ~4 s"
    assert row_ids(page) == ["bbbb0002"]
    assert page.input_value("#search") == "свет"


# ── open ────────────────────────────────────────────────────────────────────
def test_click_opens_the_session_endpoint(page):
    page.click(row("cccc0003") + " .proj")
    page.wait_for_selector("#wrap iframe")
    src = page.get_attribute("#wrap iframe", "src")
    assert src == "/sess/?arg=cccc0003"
    assert page.get_attribute("#tOpen", "href") == "/sess/?arg=cccc0003"
    assert page.is_visible("#topbar")
    assert page.inner_text("#tNum") == "cccc0003"
    assert "Старая тема" in page.inner_text("#tInfo")
    assert "sel" in page.get_attribute(row("cccc0003"), "class")
    shot(page, "index-lib-opened.png")


def test_buttons_do_not_open_the_row(page):
    page.click(row("aaaa0001") + " .card-btn.edit")
    assert page.query_selector("#wrap iframe") is None


def test_poll_moves_a_freshly_loaded_session_up(page, api):
    api.sessions[2].update(active=True, status="working", last_used=300)  # cccc0003
    page.wait_for_function(
        "() => document.querySelector('#list .card[data-sid]').dataset.sid === 'cccc0003'",
        timeout=6000)
    assert "working" in page.get_attribute(row("cccc0003") + " .dot", "class")
    assert "unloaded" not in page.get_attribute(row("cccc0003"), "class")


# ── new theme ───────────────────────────────────────────────────────────────
def test_new_theme_posts_then_opens_it(page, api):
    assert page.inner_text("#newBtn").strip() == "＋ New terminal"
    page.click("#newBtn")
    page.wait_for_selector("#newName:visible")
    page.fill("#newName", "Разбор логов")
    page.press("#newName", "Enter")
    page.wait_for_selector("#wrap iframe")
    assert api.posts("/api/library/new")[-1][2] == {"name": "Разбор логов"}
    assert page.get_attribute("#wrap iframe", "src") == "/sess/?arg=eeee0001"
    page.wait_for_selector(row("eeee0001"))
    assert page.inner_text(row("eeee0001") + " .proj") == "Разбор логов"
    assert "sel" in page.get_attribute(row("eeee0001"), "class")
    assert not page.is_visible("#newName")


def test_new_theme_escape_cancels(page, api):
    page.click("#newBtn")
    page.fill("#newName", "передумал")
    page.press("#newName", "Escape")
    assert not page.is_visible("#newName")
    assert api.posts("/api/library/new") == []
    assert page.query_selector("#wrap iframe") is None


def test_new_theme_error_is_shown_and_nothing_opens(page, api):
    api.fail_new = True
    page.click("#newBtn")
    page.fill("#newName", "не выйдет")
    page.press("#newName", "Enter")
    page.wait_for_function(
        "() => (document.getElementById('_imgToast')||{}).textContent?.includes('диск полон')",
        timeout=3000)
    assert page.query_selector("#wrap iframe") is None


# ── rename ──────────────────────────────────────────────────────────────────
def test_rename_posts_and_shows_new_name(page, api):
    page.click(row("aaaa0001") + " .card-btn.edit")
    inp = row("aaaa0001") + " input.proj-edit"
    page.wait_for_selector(inp)
    assert page.input_value(inp) == "ImmAppeal деплой"
    page.fill(inp, "ImmAppeal прод")
    page.press(inp, "Enter")
    assert page.inner_text(row("aaaa0001") + " .proj") == "ImmAppeal прод"
    assert api.posts("/api/library/rename") == [
        ("POST", "/api/library/rename", {"id": "aaaa0001", "name": "ImmAppeal прод"})]


def test_rename_escape_or_empty_changes_nothing(page, api):
    page.click(row("aaaa0001") + " .card-btn.edit")
    inp = row("aaaa0001") + " input.proj-edit"
    page.fill(inp, "что-то")
    page.press(inp, "Escape")
    assert page.inner_text(row("aaaa0001") + " .proj") == "ImmAppeal деплой"
    page.click(row("aaaa0001") + " .card-btn.edit")
    page.fill(inp, "   ")
    page.press(inp, "Enter")
    assert page.inner_text(row("aaaa0001") + " .proj") == "ImmAppeal деплой"
    assert api.posts("/api/library/rename") == []


def test_poll_does_not_clobber_a_rename_in_progress(page, api):
    page.click(row("aaaa0001") + " .card-btn.edit")
    inp = row("aaaa0001") + " input.proj-edit"
    page.fill(inp, "набираю медленно")
    n = len(api.gets())
    page.wait_for_timeout(4600)
    assert len(api.gets()) > n
    assert page.input_value(inp) == "набираю медленно"
    assert api.posts("/api/library/rename") == []


# ── archive ─────────────────────────────────────────────────────────────────
def archived_session():
    return {"id": "ffff0005", "name": "Old archived work", "cwd": "/home/ubuntu/pr",
            "created": 5, "last_used": 5, "archived": True,
            "active": False, "attached": False, "status": "off"}


def test_archive_is_one_click(page, api):
    btn = row("cccc0003") + " .arch-btn"
    assert page.inner_text(btn).strip() == "Archive"
    page.click(btn)
    page.wait_for_selector(row("cccc0003"), state="detached")
    assert api.posts("/api/library/archive") == [
        ("POST", "/api/library/archive", {"id": "cccc0003", "archived": True})]
    assert page.query_selector("#wrap iframe") is None
    page.wait_for_timeout(600)                         # no delayed second request
    assert len(api.posts("/api/library/archive")) == 1


def test_archive_request_is_json(page, api):
    seen = []
    page.on("request", lambda r: seen.append(r.headers.get("content-type"))
            if r.url.endswith("/api/library/archive") else None)
    page.click(row("cccc0003") + " .arch-btn")
    page.wait_for_selector(row("cccc0003"), state="detached")
    assert seen and all(ct and ct.startswith("application/json") for ct in seen)


def test_show_archived_toggle_lists_archived_rows(page, api):
    api.sessions.append(archived_session())
    assert page.query_selector(row("ffff0005")) is None
    tog = "#showArchived"
    assert page.is_visible(tog)
    assert "Show archived" in page.inner_text(tog)
    n = len(api.urls)
    page.click(tog)
    page.wait_for_selector(row("ffff0005"))
    assert any(u.endswith("/api/library?archived=1") for u in api.urls[n:])
    cls = page.get_attribute(row("ffff0005"), "class")
    assert "archived" in cls
    # archived rows are greyed, after the live ones, with Delete and no Restore/Archive
    assert row_ids(page)[-1] == "ffff0005"
    assert not page.is_visible(row("ffff0005") + " .arch-btn")
    assert page.is_visible(row("ffff0005") + " .del-btn")
    assert "Restore" not in page.inner_text(row("ffff0005"))
    page.mouse.move(900, 450)                          # not hovering the row
    page.wait_for_timeout(200)
    op = float(page.eval_on_selector(row("ffff0005"), "e => getComputedStyle(e).opacity"))
    assert op < 0.8
    # still searchable
    page.fill("#search", "old arch")
    assert row_ids(page) == ["ffff0005"]
    page.fill("#search", "")
    shot(page, "index-lib-archived.png")
    # toggle off hides them again
    page.click(tog)
    page.wait_for_selector(row("ffff0005"), state="detached")


def test_click_on_an_archived_row_restores_and_opens_it(page, api):
    api.sessions.append(archived_session())
    page.click("#showArchived")
    page.wait_for_selector(row("ffff0005"))
    page.click(row("ffff0005") + " .proj")
    page.wait_for_function(
        "() => { const c = document.querySelector('#list .card[data-sid=\"ffff0005\"]');"
        " return c && !c.classList.contains('archived'); }", timeout=3000)
    page.wait_for_function("() => (document.querySelector('#wrap iframe')||{}).getAttribute"
                           " && document.querySelector('#wrap iframe').getAttribute('src')"
                           " === '/sess/?arg=ffff0005'", timeout=3000)
    assert api.posts("/api/library/archive") == [
        ("POST", "/api/library/archive", {"id": "ffff0005", "archived": False})]
    assert page.inner_text("#tNum") == "ffff0005"
    assert page.inner_text(row("ffff0005") + " .arch-btn").strip() == "Archive"
    assert not page.is_visible(row("ffff0005") + " .del-btn")
    # and it stays when the archived view is switched off
    page.click("#showArchived")
    page.wait_for_timeout(300)
    assert "ffff0005" in row_ids(page)


def test_show_archived_is_remembered(browser, site):
    api = FakeAPI()
    api.sessions.append(archived_session())
    ctx, pg = _open(browser, site, api)
    try:
        pg.click("#showArchived")
        pg.wait_for_selector(row("ffff0005"))
        pg.reload()
        pg.wait_for_selector(row("ffff0005"))
        assert "archived" in pg.get_attribute(row("ffff0005"), "class")
    finally:
        ctx.close()


def test_show_archived_is_instant_from_the_last_poll(browser, site):
    api = FakeAPI()
    api.sessions.append(archived_session())
    ctx, pg = _open(browser, site, api)
    try:
        # from now on the API is unreachable: the toggle must work from the last poll
        pg.unroute(re.compile(r"/api/library(/|\?|$)"))
        pg.route(re.compile(r"/api/library(/|\?|$)"), lambda r: r.abort())
        pg.click("#showArchived")
        pg.wait_for_selector(row("ffff0005"), timeout=500)
    finally:
        ctx.close()


def test_archive_survives_the_toggle_being_on(page, api):
    api.sessions.append(archived_session())
    page.click("#showArchived")
    page.wait_for_selector(row("ffff0005"))
    page.click(row("cccc0003") + " .arch-btn")
    page.wait_for_function(
        "() => document.querySelector('#list .card[data-sid=\"cccc0003\"]')"
        "?.classList.contains('archived')", timeout=3000)
    assert not page.is_visible(row("cccc0003") + " .arch-btn")
    assert page.is_visible(row("cccc0003") + " .del-btn")


# ── Archive unloads; a click on an archived row restores + opens (owner 2026-09-25) ──
def _toast_has(pg, text, timeout=3000):
    pg.wait_for_function("t => ((document.getElementById('_imgToast')||{}).textContent||'')"
                         ".includes(t)", arg=text, timeout=timeout)


def test_archive_toast_says_unloaded(page, api):
    page.click(row("bbbb0002") + " .arch-btn")               # loaded, not busy
    _toast_has(page, "Archived and unloaded")


def test_archive_toast_says_busy_terminal_unloads_later(page, api):
    api.busy.add("bbbb0002")
    page.click(row("bbbb0002") + " .arch-btn")
    _toast_has(page, "Archived — it's busy, it will unload when it finishes")


def test_archiving_the_open_terminal_goes_back_to_the_placeholder(page, api):
    page.click(row("dddd0004") + " .proj")
    page.wait_for_selector("#wrap iframe")
    page.click(row("dddd0004") + " .arch-btn")
    page.wait_for_selector("#wrap iframe", state="detached", timeout=3000)
    assert page.is_visible("#ph")
    assert api.posts("/api/library/archive") == [
        ("POST", "/api/library/archive", {"id": "dddd0004", "archived": True})]


def test_archiving_another_terminal_keeps_the_viewer(page, api):
    page.click(row("dddd0004") + " .proj")
    page.wait_for_selector("#wrap iframe")
    page.click(row("cccc0003") + " .arch-btn")
    page.wait_for_selector(row("cccc0003"), state="detached")
    assert page.get_attribute("#wrap iframe", "src") == "/sess/?arg=dddd0004"


def test_archived_row_found_by_search_restores_and_opens_on_click(page, api):
    api.sessions.append(archived_session())
    page.fill("#search", "old arch")
    page.wait_for_selector(row("ffff0005"), timeout=3000)
    page.click(row("ffff0005") + " .proj")
    page.wait_for_function("() => (document.querySelector('#wrap iframe')||{}).getAttribute"
                           " && document.querySelector('#wrap iframe').getAttribute('src')"
                           " === '/sess/?arg=ffff0005'", timeout=3000)
    assert api.posts("/api/library/archive") == [
        ("POST", "/api/library/archive", {"id": "ffff0005", "archived": False})]
    assert next(s for s in api.sessions if s["id"] == "ffff0005")["archived"] is False


def test_failed_restore_does_not_open_and_says_so(page, api):
    api.sessions.append(archived_session())
    api.fail_archive = True
    page.click("#showArchived")
    page.wait_for_selector(row("ffff0005"))
    page.click(row("ffff0005") + " .proj")
    _toast_has(page, "Not restored")
    assert page.query_selector("#wrap iframe") is None
    assert "archived" in page.get_attribute(row("ffff0005"), "class")


# ── delete (archived rows only) ────────────────────────────────────────────
def test_live_rows_have_no_delete_button(page):
    assert page.query_selector(row("cccc0003") + " .del-btn") is None or \
        not page.is_visible(row("cccc0003") + " .del-btn")


def test_delete_is_one_click_on_an_archived_row(page, api):
    api.sessions.append(archived_session())
    page.click("#showArchived")
    page.wait_for_selector(row("ffff0005"))
    btn = row("ffff0005") + " .del-btn"
    assert page.is_visible(btn) and page.inner_text(btn).strip() == "Delete"
    color = page.eval_on_selector(btn, "e => getComputedStyle(e).color")
    r, g, b = [int(x) for x in re.findall(r"\d+", color)[:3]]
    assert r > g + 40 and r > b + 40, color                # red
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.click(btn)
    page.wait_for_selector(row("ffff0005"), state="detached")
    assert dialogs == []                                   # no confirm
    assert api.posts("/api/library/delete") == [
        ("POST", "/api/library/delete", {"id": "ffff0005"})]
    page.wait_for_function("() => ((document.getElementById('_imgToast')||{}).textContent||'')"
                           ".includes('Deleted — transcript moved to trash')")
    assert page.query_selector("#wrap iframe") is None
    page.wait_for_timeout(400)
    assert page.query_selector(row("ffff0005")) is None      # a poll does not bring it back
    assert len(api.posts("/api/library/delete")) == 1
    shot(page, "index-lib-deleted.png")


def test_delete_error_keeps_the_row(page, api):
    s = archived_session()
    s["active"] = True                                     # fake API answers 409
    api.sessions.append(s)
    page.click("#showArchived")
    page.wait_for_selector(row("ffff0005"))
    page.click(row("ffff0005") + " .del-btn")
    page.wait_for_function("() => ((document.getElementById('_imgToast')||{}).textContent||'')"
                           ".includes('Not deleted')")
    assert page.query_selector(row("ffff0005")) is not None


def test_delete_button_takes_the_place_of_archive(page, api):
    api.sessions.append(archived_session())
    page.click("#showArchived")
    page.wait_for_selector(row("ffff0005"))
    assert not page.is_visible(row("ffff0005") + " .arch-btn")
    boxes = [page.eval_on_selector(row("ffff0005") + " " + sel,
                                   "e => { const r = e.getBoundingClientRect();"
                                   " return [r.top, r.bottom, r.left, r.right]; }")
             for sel in (".sid", ".del-btn")]
    (st, sb, sl, sr), (dt, db, dl, dr) = boxes
    assert abs((st + sb) / 2 - (dt + db) / 2) < 4          # same line as the code
    assert dl >= sr                                        # to its right, no overlap


# ── search also covers the archive ─────────────────────────────────────────
def test_search_shows_archived_matches_with_toggle_off(page, api):
    api.sessions.append(archived_session())
    assert page.inner_text("#showArchived").strip() == "Show archived"   # toggle off
    page.fill("#search", "old arch")
    page.wait_for_selector(row("ffff0005"), timeout=3000)
    assert row_ids(page) == ["ffff0005"]
    assert "archived" in page.get_attribute(row("ffff0005"), "class")
    assert not page.is_visible(row("ffff0005") + " .arch-btn")
    assert page.is_visible(row("ffff0005") + " .del-btn")
    page.wait_for_timeout(4500)                        # survives a poll while searching
    assert row_ids(page) == ["ffff0005"]
    # the toggle itself was not switched on
    assert page.inner_text("#showArchived").strip() == "Show archived"


def test_clearing_search_hides_archived_again(page, api):
    api.sessions.append(archived_session())
    page.fill("#search", "old")
    page.wait_for_selector(row("ffff0005"), timeout=3000)
    page.fill("#search", "")
    page.wait_for_selector(row("ffff0005"), state="detached", timeout=3000)
    assert len(row_ids(page)) == 4
    page.wait_for_timeout(4500)                        # and a later poll keeps it hidden
    assert page.query_selector(row("ffff0005")) is None


def test_search_enter_skips_archived_matches(page, api):
    s = archived_session()
    s["name"] = "Старая тема archived"
    s["last_used"] = 10**6                             # would sort first if it counted
    api.sessions.append(s)
    page.fill("#search", "старая")
    page.wait_for_selector(row("ffff0005"), timeout=3000)
    page.press("#search", "Enter")
    page.wait_for_selector("#wrap iframe")
    assert "arg=cccc0003" in page.get_attribute("#wrap iframe", "src")


# ── manual order: drag-and-drop (desktop), ▲▼ (touch) ─────────────────────
def test_initial_order_follows_pos(browser, site):
    api = FakeAPI()
    for s, p in zip(api.sessions, (1, 0, 0, 1)):      # aaaa, bbbb, cccc, dddd
        s["pos"] = p
    ctx, pg = _open(browser, site, api)
    try:
        # loaded first (bbbb pos 0, dddd pos 1), then the rest (cccc pos 0, aaaa pos 1)
        assert row_ids(pg) == ["bbbb0002", "dddd0004", "cccc0003", "aaaa0001"]
    finally:
        ctx.close()


def test_drag_a_row_reorders_and_posts_the_order(page, api):
    assert row_ids(page) == ["dddd0004", "bbbb0002", "aaaa0001", "cccc0003"]
    assert page.get_attribute(row("cccc0003"), "draggable") == "true"
    page.drag_and_drop(row("cccc0003"), row("aaaa0001"),
                       target_position={"x": 30, "y": 3})      # upper edge = before it
    page.wait_for_function("() => [...document.querySelectorAll('#list .card[data-sid]')]"
                           ".map(e => e.dataset.sid).join() === "
                           "'dddd0004,bbbb0002,cccc0003,aaaa0001'", timeout=3000)
    assert api.posts("/api/library/reorder") == [
        ("POST", "/api/library/reorder",
         {"ids": ["dddd0004", "bbbb0002", "cccc0003", "aaaa0001"]})]
    assert page.query_selector("#wrap iframe") is None        # a drag is not a click
    page.wait_for_timeout(4500)                               # survives the next poll
    assert row_ids(page) == ["dddd0004", "bbbb0002", "cccc0003", "aaaa0001"]


def test_drag_down_puts_the_row_after_the_target(page, api):
    page.drag_and_drop(row("dddd0004"), row("bbbb0002"),
                       target_position={"x": 30, "y": 25})     # lower half = after it
    page.wait_for_function("() => document.querySelector('#list .card[data-sid]')"
                           ".dataset.sid === 'bbbb0002'", timeout=3000)
    assert api.posts("/api/library/reorder")[0][2]["ids"][:2] == ["bbbb0002", "dddd0004"]


def test_reorder_error_reverts_with_a_toast(page, api):
    api.fail_reorder = True
    page.drag_and_drop(row("cccc0003"), row("aaaa0001"), target_position={"x": 30, "y": 3})
    page.wait_for_function("() => ((document.getElementById('_imgToast')||{}).textContent||'')"
                           ".includes('Not reordered')", timeout=3000)
    assert row_ids(page) == ["dddd0004", "bbbb0002", "aaaa0001", "cccc0003"]


def test_touch_move_buttons_reorder(browser, site):
    api = FakeAPI()
    ctx, pg = _open(browser, site, api, width=400, height=800, mobile=True)
    try:
        dn = row("aaaa0001") + " .mv-btn.down"
        assert pg.is_visible(dn)
        pg.click(dn)
        pg.wait_for_function("() => [...document.querySelectorAll('#list .card[data-sid]')]"
                             ".map(e => e.dataset.sid).join() === "
                             "'dddd0004,bbbb0002,cccc0003,aaaa0001'", timeout=3000)
        assert api.posts("/api/library/reorder")[0][2] == {
            "ids": ["dddd0004", "bbbb0002", "cccc0003", "aaaa0001"]}
        assert pg.query_selector("#wrap iframe") is None
    finally:
        ctx.close()


def test_move_buttons_hidden_on_desktop(page):
    assert not page.is_visible(row("aaaa0001") + " .mv-btn.down")


# ── English only ────────────────────────────────────────────────────────────
CYR = re.compile(r"[\u0400-\u04FF]")

SCAN_JS = """() => {
  const out = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n; while ((n = w.nextNode())) {
    const p = n.parentElement;
    if (p && (p.closest('script,style'))) continue;
    if (n.textContent.trim()) out.push(n.textContent.trim());
  }
  for (const e of document.querySelectorAll('*'))
    for (const a of ['title', 'placeholder', 'aria-label', 'value', 'alt'])
      if (e.getAttribute(a)) out.push(a + '=' + e.getAttribute(a));
  out.push('title=' + document.title);
  return out;
}"""


def english_api():
    api = FakeAPI()
    names = ["Deploy", "Site work", "Old work", "Bridge"]
    for s, nm in zip(api.sessions, names):
        s["name"] = nm
    api.sessions.append(archived_session())
    return api


def _cyrillic(pg):
    return [t for t in pg.evaluate(SCAN_JS) if CYR.search(t)]


def _exercise(pg):
    bad = _cyrillic(pg)
    pg.click("#showArchived")
    pg.wait_for_selector(row("ffff0005"))
    bad += _cyrillic(pg)
    pg.click(row("bbbb0002") + " .proj")
    pg.wait_for_selector("#wrap iframe")
    bad += _cyrillic(pg)
    pg.click(row("cccc0003") + " .arch-btn")                  # toast
    pg.wait_for_function("() => (document.getElementById('_imgToast')||{}).textContent")
    bad += _cyrillic(pg)
    pg.click(row("ffff0005") + " .proj")                      # restore + open toast
    pg.wait_for_timeout(300)
    bad += _cyrillic(pg)
    pg.click(row("aaaa0001") + " .card-btn.edit")
    bad += _cyrillic(pg)
    pg.keyboard.press("Escape")
    pg.click("#newBtn")
    pg.wait_for_selector("#newName:visible")
    bad += _cyrillic(pg)
    pg.keyboard.press("Escape")
    pg.fill("#search", "zzz nothing")
    bad += _cyrillic(pg)
    return bad


def test_no_cyrillic_in_visible_ui_desktop(browser, site):
    ctx, pg = _open(browser, site, english_api())
    try:
        bad = _exercise(pg)
        assert not bad, f"Russian UI text: {sorted(set(bad))}"
    finally:
        ctx.close()


def test_no_cyrillic_in_visible_ui_phone(browser, site):
    ctx, pg = _open(browser, site, english_api(), width=400, height=800, mobile=True)
    try:
        bad = _exercise(pg)
        assert not bad, f"Russian UI text: {sorted(set(bad))}"
    finally:
        ctx.close()


def test_empty_library_text_is_english(browser, site):
    api = FakeAPI()
    api.sessions = [archived_session()]
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    try:
        pg.route(re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/"), lambda r: r.abort())
        pg.route(re.compile(r"/api/library(/|\?|$)"), api.library)
        pg.route(re.compile(r"/api/(terminal-status|page-version)"), lambda r: r.abort())
        pg.goto(site)
        pg.wait_for_selector("#libEmpty:visible")
        assert not _cyrillic(pg), _cyrillic(pg)
        assert pg.is_visible("#showArchived")        # reachable even with no live topics
    finally:
        ctx.close()


def test_no_cyrillic_in_page_strings_outside_comments():
    src = PAGE.read_text(encoding="utf-8")
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(^|[\s;{}(),])//.*$", r"\1", src)
    lines = [l.strip() for l in src.splitlines() if CYR.search(l)]
    assert not lines, "Cyrillic outside comments:\n" + "\n".join(lines[:20])


# ── wording: "terminal", never "topic" (owner, 2026-09-25) ─────────────────
TOPIC = re.compile(r"\btopics?\b", re.I)


def test_ui_says_terminal_not_topic_desktop(browser, site):
    ctx, pg = _open(browser, site, english_api())
    try:
        seen = pg.evaluate(SCAN_JS)
        _exercise(pg)
        seen += pg.evaluate(SCAN_JS)
        bad = [t for t in seen if TOPIC.search(t)]
        assert not bad, f"'topic' in UI text: {sorted(set(bad))}"
        assert pg.inner_text("#newBtn").strip() == "＋ New terminal"
    finally:
        ctx.close()


def test_no_topic_word_in_page_strings_outside_comments():
    src = PAGE.read_text(encoding="utf-8")
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(^|[\s;{}(),])//.*$", r"\1", src)
    lines = [l.strip() for l in src.splitlines() if TOPIC.search(l)]
    assert not lines, "'topic' outside comments:\n" + "\n".join(lines[:20])


# ── no slot numbers ─────────────────────────────────────────────────────────
def test_no_slot_numbers_in_the_dom(page):
    page.click(row("dddd0004") + " .proj")
    page.wait_for_selector("#wrap iframe")
    text = page.inner_text("body")
    assert not re.search(r"#\s*\d", text), "a '#N' label is on the page"
    assert "Claude" not in page.inner_text(".sidebar")
    ids = page.eval_on_selector_all("[id]", "els => els.map(e => e.id)")
    assert not [i for i in ids if re.fullmatch(r"[cpdt]\d+", i)], "slot-numbered element ids"
    srcs = page.eval_on_selector_all("iframe", "els => els.map(e => e.getAttribute('src'))")
    assert all(s.startswith("/sess/?arg=") for s in srcs)
    # every row's visible text = name + 8-hex code + controls, nothing else numeric
    for sid in row_ids(page):
        t = page.inner_text(row(sid) + " .sid")
        assert ID_RE.match(t)


# ── keeps the rest of the dashboard ─────────────────────────────────────────
def test_topbar_tasks_and_stats_are_kept(page):
    assert page.get_attribute('.sidebar a[href="/tasks/"]', "title")
    page.wait_for_function(
        "() => document.getElementById('cpuVal').textContent === '12%'", timeout=6000)
    assert page.inner_text("#ramVal") == "6.4/16.0G"          # GB, one decimal (owner, 2026-09-25)
    page.click(row("dddd0004") + " .proj")
    for bid in ("tPasteImg", "tOpen", "tEsc", "tMenu"):
        assert page.is_visible("#" + bid), bid
    page.click("#tMenu")                     # Telegram / Password / Logout live in the menu
    for bid in ("tTg", "tChpw", "tLogout"):
        assert page.is_visible("#" + bid), bid


def test_system_stats_come_from_library_when_present(browser, site):
    api = FakeAPI(with_system=True)
    ctx, pg = _open(browser, site, api)
    try:
        pg.wait_for_function(
            "() => document.getElementById('cpuVal').textContent === '42%'", timeout=3000)
    finally:
        ctx.close()


def test_sidebar_stays_compact(page):
    w = page.eval_on_selector(".sidebar", "e => e.getBoundingClientRect().width")
    assert w <= 178
    for sel in (".sidebar-header", ".lib-tools", ".agent-list"):
        over = page.eval_on_selector(sel, "e => e.scrollWidth - e.clientWidth")
        assert over <= 1, f"{sel} overflows by {over}px"
    bad = page.evaluate("""() => {
      const right = document.querySelector('.sidebar').getBoundingClientRect().right;
      return [...document.querySelectorAll('.card-btn, .arch-btn, #newBtn, #search')]
        .filter(b => b.getBoundingClientRect().right > right + 0.5).length;
    }""")
    assert bad == 0


# ── phone ───────────────────────────────────────────────────────────────────
def test_works_at_400px(browser, site):
    api = FakeAPI()
    ctx, pg = _open(browser, site, api, width=400, height=800, mobile=True)
    try:
        shot(pg, "index-lib-mobile.png")
        over = pg.evaluate("() => document.documentElement.scrollWidth - innerWidth")
        assert over <= 0, f"page scrolls sideways by {over}px"
        for sel in ("#newBtn", "#search", "#mobInput", '.sidebar a[href="/tasks/"]'):
            box = pg.eval_on_selector(sel, "e => { const r = e.getBoundingClientRect();"
                                           " return [r.left, r.right, r.width]; }")
            assert box[2] > 0 and box[0] >= 0 and box[1] <= 400.5, (sel, box)
        # every row control is on screen
        bad = pg.evaluate("""() => [...document.querySelectorAll(
              '#list .card .card-btn, #list .card .arch-btn, #list .card .dot, #list .card .sid')]
            .filter(e => { const r = e.getBoundingClientRect();
                           return r.width === 0 || r.right > innerWidth + 0.5; }).length""")
        assert bad == 0, f"{bad} row controls are off-screen or hidden at 400px"
        # at least two rows fit in the (short) phone sidebar
        visible_rows = pg.evaluate("""() => {
          const l = document.getElementById('list').getBoundingClientRect();
          return [...document.querySelectorAll('#list .card')]
            .filter(c => c.getBoundingClientRect().bottom <= l.bottom + 1).length; }""")
        assert visible_rows >= 2
        pg.fill("#search", "мост")
        assert row_ids(pg) == ["dddd0004"]
        pg.tap(row("dddd0004") + " .proj")
        pg.wait_for_selector("#wrap iframe")
        assert pg.get_attribute("#wrap iframe", "src") == "/sess/?arg=dddd0004"
        shot(pg, "index-lib-mobile-opened.png")
        over = pg.eval_on_selector(".topbar", "e => e.scrollWidth - e.clientWidth")
        assert over <= 1, f"phone topbar overflows by {over}px (Logout cut off)"
        pg.fill("#search", "")
        pg.tap("#newBtn")
        pg.wait_for_selector("#newName:visible")
        shot(pg, "index-lib-mobile-new.png")
        box = pg.eval_on_selector("#newName", "e => e.getBoundingClientRect().right")
        assert box <= 400.5
    finally:
        ctx.close()


# ── cmd: the one plain command line (tmux cmd-shell) ────────────────────────
SHELL_ON = {"active": True, "attached": False, "status": "idle"}


def shell_row(page):
    return page.query_selector("#list .shell-row")


def test_cmd_button_sits_next_to_new_terminal(page):
    assert page.inner_text("#cmdBtn").strip() == "cmd"
    nb = page.eval_on_selector("#newBtn", "e => e.getBoundingClientRect().toJSON()")
    cb = page.eval_on_selector("#cmdBtn", "e => e.getBoundingClientRect().toJSON()")
    assert abs(nb["top"] - cb["top"]) < 2 and cb["left"] >= nb["right"] - 0.5
    over = page.eval_on_selector(".sidebar", "e => e.getBoundingClientRect().right")
    assert cb["right"] <= over + 0.5


def test_cmd_opens_the_shell_endpoint_without_any_post(page, api):
    page.click("#cmdBtn")
    page.wait_for_selector("#wrap iframe")
    assert page.get_attribute("#wrap iframe", "src") == "/sess/?arg=shell"
    assert page.get_attribute("#tOpen", "href") == "/sess/?arg=shell"
    assert api.posts() == []
    assert "Command line" in page.inner_text("#tInfo")
    # no topic row is selected
    assert page.query_selector_all("#list .card[data-sid].sel") == []


def test_no_shell_row_while_the_shell_is_not_loaded(page, api):
    assert shell_row(page) is None
    api.shell = {"active": False, "attached": False, "status": "off"}
    page.wait_for_timeout(4600)
    assert shell_row(page) is None


def test_shell_row_pinned_on_top_with_status_dot(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON, status="working")
    ctx, pg = _open(browser, site, api)
    try:
        pg.wait_for_selector("#list .shell-row")
        shot(pg, "index-lib-shell-row.png")
        first = pg.evaluate("() => document.querySelector('#list').firstElementChild.className")
        assert "shell-row" in first
        assert pg.inner_text("#list .shell-row .proj") == "Command line"
        assert "working" in pg.get_attribute("#list .shell-row .dot", "class")
        assert pg.get_attribute("#list .shell-row", "draggable") != "true"
        for sub in (".card-btn", ".arch-btn", ".del-btn", ".mv-btn"):
            assert pg.query_selector_all("#list .shell-row " + sub) == [], sub
        # topic rows are unchanged and still ordered after it
        assert row_ids(pg) == ["dddd0004", "bbbb0002", "aaaa0001", "cccc0003"]
        # it stays on top across polls, and the status follows the poll
        api.shell = dict(SHELL_ON)
        pg.wait_for_timeout(4600)
        first = pg.evaluate("() => document.querySelector('#list').firstElementChild.className")
        assert "shell-row" in first
        assert "idle" in pg.get_attribute("#list .shell-row .dot", "class")
        # unloaded -> the row goes away
        api.shell = {"active": False, "attached": False, "status": "off"}
        pg.wait_for_timeout(4600)
        assert shell_row(pg) is None
    finally:
        ctx.close()


def test_click_shell_row_reopens_it_and_selects_it(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open(browser, site, api)
    try:
        pg.wait_for_selector("#list .shell-row")
        pg.click(row("cccc0003") + " .proj")
        pg.wait_for_selector("#wrap iframe")
        pg.click("#list .shell-row .proj")
        pg.wait_for_function("() => document.querySelector('#wrap iframe')"
                             ".getAttribute('src') === '/sess/?arg=shell'")
        assert "sel" in pg.get_attribute("#list .shell-row", "class")
        assert "sel" not in pg.get_attribute(row("cccc0003"), "class")
        assert api.posts() == []
    finally:
        ctx.close()


def test_shell_row_hidden_by_an_unrelated_search(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open(browser, site, api)
    try:
        pg.wait_for_selector("#list .shell-row")
        pg.fill("#search", "свет")
        assert shell_row(pg) is None or not shell_row(pg).is_visible()
        pg.fill("#search", "cmd")
        assert shell_row(pg).is_visible()
        pg.fill("#search", "")
        assert shell_row(pg).is_visible()
    finally:
        ctx.close()


def test_cmd_and_shell_row_fit_at_400px(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open(browser, site, api, width=400, height=800, mobile=True)
    try:
        pg.wait_for_selector("#list .shell-row")
        shot(pg, "index-lib-mobile-shell.png")
        over = pg.evaluate("() => document.documentElement.scrollWidth - innerWidth")
        assert over <= 0
        for sel in ("#newBtn", "#cmdBtn", "#search", '.sidebar a[href="/tasks/"]', "#sysStats"):
            box = pg.eval_on_selector(sel, "e => { const r = e.getBoundingClientRect();"
                                           " return [r.left, r.right, r.width]; }")
            assert box[2] > 0 and box[0] >= 0 and box[1] <= 400.5, (sel, box)
        assert pg.eval_on_selector("#search", "e => e.getBoundingClientRect().width") >= 60
        for sel in ("#list .shell-row .dot", "#list .shell-row .proj"):
            r = pg.eval_on_selector(sel, "e => e.getBoundingClientRect().right")
            assert r <= 400.5
        pg.tap("#cmdBtn")
        pg.wait_for_selector("#wrap iframe")
        assert pg.get_attribute("#wrap iframe", "src") == "/sess/?arg=shell"
        over = pg.eval_on_selector(".topbar", "e => e.scrollWidth - e.clientWidth")
        assert over <= 1
    finally:
        ctx.close()


# ── ✕ closes the command line ───────────────────────────────────────────────
def test_shell_row_close_button_closes_it_without_opening(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open(browser, site, api)
    try:
        pg.wait_for_selector("#list .shell-row")
        btn = "#list .shell-row .shell-close"
        assert pg.get_attribute(btn, "title") == "Close command line"
        assert pg.inner_text(btn).strip() == "✕"
        pg.click(btn)
        pg.wait_for_function("() => !document.querySelector('#list .shell-row')")
        assert pg.query_selector("#wrap iframe") is None           # the click did not open it
        posts = api.posts("/api/library/shell-close")
        assert len(posts) == 1 and posts[0][2] == {}
        pg.wait_for_timeout(4600)                                  # a poll does not bring it back
        assert shell_row(pg) is None
    finally:
        ctx.close()


def test_closing_the_open_shell_clears_the_frame(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open(browser, site, api)
    try:
        pg.wait_for_selector("#list .shell-row")
        pg.click("#list .shell-row .proj")
        pg.wait_for_selector("#wrap iframe")
        pg.click("#list .shell-row .shell-close")
        pg.wait_for_function("() => !document.querySelector('#list .shell-row')")
        assert pg.query_selector("#wrap iframe") is None           # no dead terminal left showing
        assert pg.is_visible("#ph")
    finally:
        ctx.close()


def test_shell_close_error_brings_the_row_back_with_a_toast(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    api.fail_shell_close = True
    ctx, pg = _open(browser, site, api)
    try:
        pg.wait_for_selector("#list .shell-row")
        pg.click("#list .shell-row .shell-close")
        pg.wait_for_selector("#_imgToast")
        pg.wait_for_function("() => /command line/i.test(document.getElementById('_imgToast').textContent)")
        pg.wait_for_selector("#list .shell-row")
    finally:
        ctx.close()


def test_shell_close_button_fits_at_400px(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open(browser, site, api, width=400, height=800, mobile=True)
    try:
        pg.wait_for_selector("#list .shell-row")
        r = pg.eval_on_selector("#list .shell-row .shell-close",
                                "e => { const b = e.getBoundingClientRect(); return [b.width, b.right]; }")
        assert r[0] > 0 and r[1] <= 400.5
        pg.tap("#list .shell-row .shell-close")
        pg.wait_for_function("() => !document.querySelector('#list .shell-row')")
    finally:
        ctx.close()


# ── the shell ended (`exit`): its frame gives way to the placeholder ────────
def test_shell_frame_replaced_by_placeholder_when_the_shell_ends(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open(browser, site, api)
    try:
        pg.wait_for_selector("#list .shell-row")
        pg.click("#cmdBtn")
        pg.wait_for_selector("#wrap iframe")
        api.shell = {"active": False, "attached": False, "status": "off"}
        pg.wait_for_function("() => !document.querySelector('#wrap iframe')", timeout=6000)
        assert pg.is_visible("#ph")
        assert shell_row(pg) is None
        # a topic's frame is never touched by the shell's state
        pg.click(row("cccc0003") + " .proj")
        pg.wait_for_selector("#wrap iframe")
        pg.wait_for_timeout(4600)
        assert pg.get_attribute("#wrap iframe", "src") == "/sess/?arg=cccc0003"
    finally:
        ctx.close()


def test_starting_shell_frame_survives_polls_before_it_is_up(page, api):
    page.click("#cmdBtn")                                # shell not running yet
    page.wait_for_selector("#wrap iframe")
    page.wait_for_timeout(4600)                           # polls still say inactive
    assert page.get_attribute("#wrap iframe", "src") == "/sess/?arg=shell"
    api.shell = dict(SHELL_ON)                            # it came up ...
    page.wait_for_selector("#list .shell-row")
    api.shell = {"active": False, "attached": False, "status": "off"}   # ... and was exited
    page.wait_for_function("() => !document.querySelector('#wrap iframe')", timeout=6000)
    assert page.is_visible("#ph")


# ── header: Esc first; Telegram / Password / Logout in one menu ─────────────
def _topbar_order(pg):
    return pg.evaluate("""() => [...document.querySelectorAll('#topbar > .btn, #topbar > .acct > .btn')]
        .filter(e => e.offsetParent !== null).map(e => e.id)""")


def _menu_open(pg):
    return pg.evaluate("() => !document.getElementById('tMenuList').hidden")


def test_esc_is_first_and_account_links_are_in_one_menu(page):
    page.click(row("dddd0004") + " .proj")
    page.wait_for_selector("#topbar:visible")
    assert _topbar_order(page) == ["tEsc", "tPasteImg", "tOpen", "tMenu"]
    for bid in ("tTg", "tChpw", "tLogout"):                # hidden until the menu opens
        assert not page.is_visible("#" + bid), bid
    assert page.get_attribute("#tTg", "href") == "/telegram"
    assert page.get_attribute("#tChpw", "href") == "/change-password"
    assert page.get_attribute("#tLogout", "href") == "/logout"
    assert page.get_attribute("#tMenu", "aria-haspopup") == "menu"
    assert page.get_attribute("#tMenu", "aria-expanded") == "false"
    shot(page, "index-lib-topbar.png")


def test_account_menu_opens_and_closes(page):
    page.click(row("dddd0004") + " .proj")
    page.click("#tMenu")
    assert _menu_open(page) and page.get_attribute("#tMenu", "aria-expanded") == "true"
    shot(page, "index-lib-account-menu.png")
    items = page.eval_on_selector_all("#tMenuList [role=menuitem]", "els => els.map(e => e.id)")
    assert items == ["tTg", "tChpw", "tTheme", "tLogout"]   # + theme switch (owner, 2026-09-25)
    for bid in items:
        assert page.is_visible("#" + bid)
    # the menu is drawn above the terminal, not clipped under it
    hit = page.evaluate("""() => { const r = document.getElementById('tLogout').getBoundingClientRect();
        return document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2).id; }""")
    assert hit == "tLogout"
    page.click("#tMenu")                                    # toggles shut
    assert not _menu_open(page)
    page.click("#tMenu")
    page.click(".sidebar-header")                           # outside click
    assert not _menu_open(page)
    page.click("#tMenu")
    page.keyboard.press("Escape")
    assert not _menu_open(page)
    assert page.evaluate("document.activeElement.id") == "tMenu"


def test_account_menu_keyboard(page):
    page.click(row("dddd0004") + " .proj")
    page.focus("#tMenu")
    page.keyboard.press("Enter")
    assert _menu_open(page)
    assert page.evaluate("document.activeElement.id") == "tTg"   # focus moves into the menu
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.id") == "tChpw"
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.id") == "tTheme"
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")                            # wraps
    assert page.evaluate("document.activeElement.id") == "tTg"
    page.keyboard.press("ArrowUp")
    assert page.evaluate("document.activeElement.id") == "tLogout"
    page.keyboard.press("Tab")                                  # leaving the menu closes it
    assert not _menu_open(page)


def test_account_menu_at_400px(browser, site):
    api = FakeAPI()
    ctx, pg = _open(browser, site, api, width=400, height=800, mobile=True)
    try:
        pg.tap(row("dddd0004") + " .proj")
        pg.wait_for_selector("#topbar:visible")
        assert _topbar_order(pg)[0] == "tEsc"
        over = pg.eval_on_selector(".topbar", "e => e.scrollWidth - e.clientWidth")
        assert over <= 1
        pg.tap("#tMenu")
        shot(pg, "index-lib-mobile-account-menu.png")
        for bid in ("tTg", "tChpw", "tLogout"):
            b = pg.eval_on_selector("#" + bid, "e => { const r = e.getBoundingClientRect();"
                                               " return [r.left, r.right, r.width]; }")
            assert b[2] > 0 and b[0] >= 0 and b[1] <= 400.5, (bid, b)
        over = pg.evaluate("() => document.documentElement.scrollWidth - innerWidth")
        assert over <= 0
        pg.tap("#tInfo")
        assert not _menu_open(pg)
    finally:
        ctx.close()


# ── Task board as an in-page tab (T button) ─────────────────────────────────
TASKS_LINK = '.sidebar a[href="/tasks/"]'


def _open_tasks_ctx(browser, site, api, **kw):
    ctx, pg = _open(browser, site, api, **kw)
    pg.route(re.compile(r"/tasks/(\?|$)"),
             lambda r: r.fulfill(status=200, content_type="text/html",
                                 body="<html><body>task board stub</body></html>"))
    return ctx, pg


def _first_row_class(pg):
    return pg.evaluate("() => document.querySelector('#list').firstElementChild.className")


def test_T_opens_the_board_in_the_viewer_not_a_new_window(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open_tasks_ctx(browser, site, api)
    try:
        pg.evaluate("() => { try { localStorage.removeItem('lib-tasks-open'); } catch {} }")
        assert pg.get_attribute(TASKS_LINK, "target") is None
        assert pg.inner_text(TASKS_LINK).strip() == "T"
        n_pages = len(ctx.pages)
        pg.click(TASKS_LINK)
        pg.wait_for_selector("#wrap iframe")
        assert pg.get_attribute("#wrap iframe", "src") == "/tasks/"
        assert len(ctx.pages) == n_pages                            # no new window/tab
        assert pg.url.endswith("/index-lib.html")                  # the dashboard stayed
        # pinned row at the very top, above the command line
        pg.wait_for_selector("#list .tasks-row")
        assert "tasks-row" in _first_row_class(pg)
        second = pg.evaluate("() => document.querySelector('#list').children[1].className")
        assert "shell-row" in second
        assert pg.inner_text("#list .tasks-row .proj") == "Tasks"
        assert "sel" in pg.get_attribute("#list .tasks-row", "class")
        assert pg.get_attribute("#list .tasks-row", "draggable") != "true"
        for sub in (".card-btn", ".arch-btn", ".del-btn", ".mv-btn", ".edit"):
            assert pg.query_selector_all("#list .tasks-row " + sub) == [], sub
        assert pg.get_attribute("#list .tasks-row .tasks-close", "title") == "Close task board"
        shot(pg, "index-lib-tasks-tab.png")
    finally:
        ctx.close()


def test_tasks_row_has_the_green_dot_like_the_other_rows(browser, site):
    """Owner, 2026-09-25: the Tasks row gets the same green "loaded" dot as every
    loaded terminal, in the same place (after the close button)."""
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open_tasks_ctx(browser, site, api)
    try:
        pg.click(TASKS_LINK)
        pg.wait_for_selector("#list .tasks-row .card-btns .dot")
        assert pg.get_attribute("#list .tasks-row .dot", "class") == "dot idle"
        order = pg.evaluate("() => [...document.querySelector('#list .tasks-row .card-btns')"
                            ".children].map(e => e.className)")
        assert order == ["tasks-close", "dot idle"]
        # same look as the command line's loaded dot
        css = "e => getComputedStyle(e).backgroundColor"
        assert pg.eval_on_selector("#list .tasks-row .dot", css) == "rgb(74, 222, 128)"
        pg.wait_for_selector("#list .shell-row .dot.idle")
        assert (pg.eval_on_selector("#list .tasks-row .dot", "e => e.getBoundingClientRect().width")
                == pg.eval_on_selector("#list .shell-row .dot", "e => e.getBoundingClientRect().width"))
    finally:
        ctx.close()


def test_clicking_the_terminal_id_in_the_top_bar_shows_its_tasks(browser, site):
    """Owner, 2026-09-25: the 8-hex id in the top bar opens the Tasks tab filtered
    to that terminal (/tasks/?session=<id>)."""
    api = FakeAPI()
    ctx, pg = _open_tasks_ctx(browser, site, api)
    pg.route(re.compile(r"/tasks/\?session="),
             lambda r: r.fulfill(status=200, content_type="text/html",
                                 body="<html><body>filtered board stub</body></html>"))
    try:
        pg.click(row("cccc0003") + " .proj")
        pg.wait_for_function("() => document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/sess/?arg=cccc0003'")
        assert pg.inner_text("#tNum") == "cccc0003"
        assert "task" in (pg.get_attribute("#tNum", "title") or "").lower()
        pg.click("#tNum")
        pg.wait_for_function("() => document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/tasks/?session=cccc0003'")
        pg.wait_for_selector("#list .tasks-row.sel")
        assert pg.inner_text("#tNum") == "tasks"
        # on the board itself the label is not a link any more
        pg.click("#tNum")
        pg.wait_for_timeout(200)
        assert pg.get_attribute("#wrap iframe", "src") == "/tasks/?session=cccc0003"
        assert not (pg.get_attribute("#tNum", "title") or "")
    finally:
        ctx.close()


def test_tasks_row_stays_while_a_terminal_is_open_and_reshows_the_board(browser, site):
    api = FakeAPI()
    ctx, pg = _open_tasks_ctx(browser, site, api)
    try:
        pg.click(TASKS_LINK)
        pg.wait_for_selector("#list .tasks-row")
        pg.click(row("cccc0003") + " .proj")
        pg.wait_for_function("() => document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/sess/?arg=cccc0003'")
        assert pg.query_selector("#list .tasks-row") is not None
        assert "sel" not in pg.get_attribute("#list .tasks-row", "class")
        pg.wait_for_timeout(4600)                                    # survives a poll
        assert "tasks-row" in _first_row_class(pg)
        pg.click("#list .tasks-row .proj")
        pg.wait_for_function("() => document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/tasks/'")
        assert "sel" in pg.get_attribute("#list .tasks-row", "class")
        assert "sel" not in pg.get_attribute(row("cccc0003"), "class")
    finally:
        ctx.close()


def test_tasks_close_removes_the_row_and_returns_to_the_placeholder(browser, site):
    api = FakeAPI()
    ctx, pg = _open_tasks_ctx(browser, site, api)
    try:
        pg.click(TASKS_LINK)
        pg.wait_for_selector("#list .tasks-row")
        pg.click("#list .tasks-row .tasks-close")
        pg.wait_for_function("() => !document.querySelector('#list .tasks-row')")
        assert pg.query_selector("#wrap iframe") is None
        assert pg.is_visible("#ph")
        # closing while a terminal is shown leaves the terminal alone
        pg.click(TASKS_LINK)
        pg.wait_for_selector("#list .tasks-row")
        pg.click(row("cccc0003") + " .proj")
        pg.wait_for_function("() => document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/sess/?arg=cccc0003'")
        pg.click("#list .tasks-row .tasks-close")
        pg.wait_for_function("() => !document.querySelector('#list .tasks-row')")
        assert pg.get_attribute("#wrap iframe", "src") == "/sess/?arg=cccc0003"
    finally:
        ctx.close()


def test_tasks_tab_is_remembered_across_reload(browser, site):
    api = FakeAPI()
    ctx, pg = _open_tasks_ctx(browser, site, api)
    try:
        pg.click(TASKS_LINK)
        pg.wait_for_selector("#list .tasks-row")
        pg.reload()
        pg.wait_for_selector(".card[data-sid]")
        pg.wait_for_selector("#list .tasks-row")
        assert "tasks-row" in _first_row_class(pg)
        pg.click("#list .tasks-row .tasks-close")
        pg.wait_for_function("() => !document.querySelector('#list .tasks-row')")
        pg.reload()
        pg.wait_for_selector(".card[data-sid]")
        pg.wait_for_timeout(300)
        assert pg.query_selector("#list .tasks-row") is None
    finally:
        ctx.close()


def test_tasks_tab_at_400px(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open_tasks_ctx(browser, site, api, width=400, height=800, mobile=True)
    try:
        pg.tap(TASKS_LINK)
        pg.wait_for_selector("#list .tasks-row")
        shot(pg, "index-lib-mobile-tasks-tab.png")
        assert pg.get_attribute("#wrap iframe", "src") == "/tasks/"
        over = pg.evaluate("() => document.documentElement.scrollWidth - innerWidth")
        assert over <= 0
        for sel in ("#list .tasks-row .proj", "#list .tasks-row .tasks-close"):
            b = pg.eval_on_selector(sel, "e => { const r = e.getBoundingClientRect();"
                                         " return [r.width, r.right]; }")
            assert b[0] > 0 and b[1] <= 400.5, (sel, b)
        pg.tap("#list .tasks-row .tasks-close")
        pg.wait_for_function("() => !document.querySelector('#list .tasks-row')")
    finally:
        ctx.close()


# ── Server status as an in-page tab (gauge button next to T) ────────────────
SERVER_LINK = '.sidebar a[href="/server.html"]'


def _open_server_ctx(browser, site, api, **kw):
    ctx, pg = _open_tasks_ctx(browser, site, api, **kw)
    pg.route(re.compile(r"/server\.html(\?|$)"),
             lambda r: r.fulfill(status=200, content_type="text/html",
                                 body="<html><body>server page stub</body></html>"))
    return ctx, pg


def test_server_link_is_in_both_pages_next_to_T():
    for name in ("index-lib.html", "index.html"):
        html = (WEB_DIR / name).read_text(encoding="utf-8")
        m = re.search(r'<div class="header-add">(.*?)</div>', html, re.S)
        assert m, name
        block = m.group(1)
        assert block.index('href="/tasks/"') < block.index('href="/server.html"'), name


def test_server_button_opens_the_page_in_the_viewer(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open_server_ctx(browser, site, api)
    try:
        pg.evaluate("() => { try { localStorage.removeItem('lib-server-open');"
                    " localStorage.removeItem('lib-tasks-open'); } catch {} }")
        assert pg.get_attribute(SERVER_LINK, "target") is None
        assert pg.get_attribute(SERVER_LINK, "aria-label")
        n_pages = len(ctx.pages)
        pg.click(SERVER_LINK)
        pg.wait_for_selector("#wrap iframe")
        assert pg.get_attribute("#wrap iframe", "src") == "/server.html"
        assert len(ctx.pages) == n_pages
        pg.wait_for_selector("#list .server-row")
        assert "server-row" in _first_row_class(pg)
        assert pg.inner_text("#list .server-row .proj") == "Server"
        assert "sel" in pg.get_attribute("#list .server-row", "class")
        assert pg.inner_text("#tNum") == "server"
        assert pg.get_attribute("#list .server-row .server-close", "title") == "Close server status"
        order = pg.evaluate("() => [...document.querySelector('#list .server-row .card-btns')"
                            ".children].map(e => e.className)")
        assert order == ["server-close", "dot idle"]
        # with the task board open too: Tasks, Server, Command line
        pg.click(TASKS_LINK)
        pg.wait_for_selector("#list .tasks-row")
        classes = pg.evaluate("() => [...document.querySelector('#list').children]"
                              ".slice(0, 3).map(e => e.className)")
        assert "tasks-row" in classes[0] and "server-row" in classes[1] and "shell-row" in classes[2]
        assert "sel" not in pg.get_attribute("#list .server-row", "class")
        pg.click("#list .server-row .proj")
        pg.wait_for_function("() => document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/server.html'")
        assert "sel" in pg.get_attribute("#list .server-row", "class")
        shot(pg, "index-lib-server-tab.png")
    finally:
        ctx.close()


def test_server_close_and_remembered_across_reload(browser, site):
    api = FakeAPI()
    ctx, pg = _open_server_ctx(browser, site, api)
    try:
        pg.click(SERVER_LINK)
        pg.wait_for_selector("#list .server-row")
        pg.reload()
        pg.wait_for_selector(".card[data-sid]")
        pg.wait_for_selector("#list .server-row")
        pg.click("#list .server-row .server-close")
        pg.wait_for_function("() => !document.querySelector('#list .server-row')")
        assert pg.query_selector("#wrap iframe") is None
        assert pg.is_visible("#ph")
        pg.reload()
        pg.wait_for_selector(".card[data-sid]")
        pg.wait_for_timeout(300)
        assert pg.query_selector("#list .server-row") is None
    finally:
        ctx.close()


def test_server_tab_at_400px(browser, site):
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open_server_ctx(browser, site, api, width=400, height=800, mobile=True)
    try:
        for sel in (TASKS_LINK, SERVER_LINK, "#sysStats", "#cmdBtn", "#newBtn"):
            b = pg.eval_on_selector(sel, "e => { const r = e.getBoundingClientRect();"
                                         " return [r.left, r.right, r.width]; }")
            assert b[2] > 0 and b[0] >= 0 and b[1] <= 400.5, (sel, b)
        pg.tap(SERVER_LINK)
        pg.wait_for_selector("#list .server-row")
        shot(pg, "index-lib-mobile-server-tab.png")
        assert pg.get_attribute("#wrap iframe", "src") == "/server.html"
        over = pg.evaluate("() => document.documentElement.scrollWidth - innerWidth")
        assert over <= 0
        pg.tap("#list .server-row .server-close")
        pg.wait_for_function("() => !document.querySelector('#list .server-row')")
    finally:
        ctx.close()


# ── Dark / Light theme (switch in the ⋯ menu; owner, 2026-09-25) ────────────
THEME_KEY = "agentdeck-theme"
# the dark look as it was before the palette became variables — must not move
DARK = {
    ("body", "backgroundColor"): "rgb(9, 9, 11)",
    ("body", "color"): "rgb(228, 228, 231)",
    (".sidebar", "backgroundColor"): "rgb(12, 12, 15)",
    (".sidebar", "borderRightColor"): "rgba(255, 255, 255, 0.06)",
    ("#list .card[data-sid] .proj", "color"): "rgb(212, 212, 216)",
    ("#list .card[data-sid] .sid", "color"): "rgb(82, 82, 91)",
    ("#search", "backgroundColor"): "rgb(24, 24, 27)",
    ("#newBtn", "color"): "rgb(199, 210, 254)",
    (".topbar", "backgroundColor"): "rgb(12, 12, 15)",
    ("#tOpen", "color"): "rgb(161, 161, 170)",
    ("#tOpen", "borderTopColor"): "rgba(255, 255, 255, 0.2)",
    (".card.sel", "backgroundColor"): "rgba(99, 102, 241, 0.07)",
    (".card.sel", "borderLeftColor"): "rgb(129, 140, 248)",
    (".dot.working", "backgroundColor"): "rgb(251, 191, 36)",
    (".dot.idle", "backgroundColor"): "rgb(74, 222, 128)",
    (".dot.off", "backgroundColor"): "rgb(63, 63, 70)",
    ("#cpuStat", "color"): "rgb(82, 82, 91)",
    ("#cpuVal", "color"): "rgb(161, 161, 170)",
    ("#tMenuList", "backgroundColor"): "rgb(17, 17, 20)",
}


def _rgb(s):
    nums = [float(x) for x in re.findall(r"[\d.]+", s)]
    return nums[:3], (nums[3] if len(nums) > 3 else 1.0)


def _lum(s):
    (r, g, b), _ = _rgb(s)
    f = lambda c: (c / 255) / 12.92 if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _css(pg, sel, prop):
    return pg.eval_on_selector(sel, f"e => getComputedStyle(e).{prop}")


def _open_themed(browser, site, api, theme=None, **kw):
    """Dashboard with a terminal open (so the topbar and its ⋯ menu are shown)."""
    ctx, pg = _open(browser, site, api, **kw)
    if theme is not None:
        pg.evaluate(f"t => localStorage.setItem('{THEME_KEY}', t)", theme)
        pg.reload()
        pg.wait_for_selector(".card[data-sid]")
    pg.click(row("dddd0004") + " .proj")
    pg.wait_for_selector("#topbar:visible")
    return ctx, pg


def test_theme_item_is_in_the_account_menu(page):
    page.click(row("dddd0004") + " .proj")
    page.click("#tMenu")
    assert page.get_attribute("#tTheme", "role") == "menuitem"
    assert page.inner_text("#tTheme").strip() == "Light theme"     # names the one you'd switch to
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"


def test_theme_switch_toggles_html_and_is_remembered(browser, site):
    api = FakeAPI()
    ctx, pg = _open_themed(browser, site, api)
    try:
        pg.click("#tMenu")
        pg.click("#tTheme")
        assert pg.evaluate("document.documentElement.dataset.theme") == "light"
        assert pg.evaluate(f"localStorage.getItem('{THEME_KEY}')") == "light"
        assert pg.is_visible("#tTheme")                               # menu stays open
        assert pg.inner_text("#tTheme").strip() == "Dark theme"
        pg.reload()
        pg.wait_for_selector(".card[data-sid]")
        assert pg.evaluate("document.documentElement.dataset.theme") == "light"
        pg.click(row("dddd0004") + " .proj")
        pg.click("#tMenu")
        assert pg.inner_text("#tTheme").strip() == "Dark theme"
        pg.click("#tTheme")
        assert pg.evaluate("document.documentElement.dataset.theme") == "dark"
        assert pg.evaluate(f"localStorage.getItem('{THEME_KEY}')") == "dark"
    finally:
        ctx.close()


def test_theme_switch_by_keyboard(browser, site):
    api = FakeAPI()
    ctx, pg = _open_themed(browser, site, api)
    try:
        pg.focus("#tMenu")
        pg.keyboard.press("Enter")
        pg.keyboard.press("ArrowDown")
        pg.keyboard.press("ArrowDown")
        assert pg.evaluate("document.activeElement.id") == "tTheme"
        pg.keyboard.press("Enter")
        assert pg.evaluate("document.documentElement.dataset.theme") == "light"
        pg.keyboard.press(" ")
        assert pg.evaluate("document.documentElement.dataset.theme") == "dark"
    finally:
        ctx.close()


def test_theme_is_applied_before_first_paint(browser, site):
    """The <head> script sets data-theme before <body> exists — no dark flash."""
    src = PAGE.read_text(encoding="utf-8")
    head = src[:src.index("</head>")]
    assert THEME_KEY in head and "dataset.theme" in head
    api = FakeAPI()
    ctx = browser.new_context(viewport={"width": 1200, "height": 800})
    ctx.add_init_script(f"""try {{ localStorage.setItem('{THEME_KEY}', 'light'); }} catch (e) {{}}
      window.__themeAtBody = 'unset';
      new MutationObserver((recs, obs) => {{
        if (document.body) {{ window.__themeAtBody = document.documentElement.dataset.theme || '';
                              obs.disconnect(); }}
      }}).observe(document, {{ childList: true, subtree: true }});""")
    pg = ctx.new_page()
    try:
        pg.route(re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/"), lambda r: r.abort())
        pg.route(re.compile(r"/api/library(/|\?|$)"), api.library)
        pg.route(re.compile(r"/api/page-version"), lambda r: r.abort())
        pg.goto(site)
        pg.wait_for_selector(".card[data-sid]")
        assert pg.evaluate("window.__themeAtBody") == "light"
    finally:
        ctx.close()


def test_theme_survives_broken_storage(browser, site):
    api = FakeAPI()
    ctx = browser.new_context(viewport={"width": 1200, "height": 800})
    # reading/writing the theme key throws (blocked site data): the page still works, dark
    ctx.add_init_script("""for (const m of ['getItem', 'setItem']) {
        const orig = Storage.prototype[m];
        Storage.prototype[m] = function (k, ...a) {
          if (k === 'agentdeck-theme') throw new Error('blocked');
          return orig.call(this, k, ...a); }; }""")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.route(re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/"), lambda r: r.abort())
        pg.route(re.compile(r"/api/library(/|\?|$)"), api.library)
        pg.route(re.compile(r"/api/page-version"), lambda r: r.abort())
        pg.goto(site)
        pg.wait_for_selector(".card[data-sid]")
        assert pg.evaluate("document.documentElement.dataset.theme") == "dark"
        pg.click(row("dddd0004") + " .proj")
        pg.click("#tMenu")
        pg.click("#tTheme")                          # still switches for this visit
        assert pg.evaluate("document.documentElement.dataset.theme") == "light"
        assert errors == [], errors
    finally:
        ctx.close()


def test_dark_look_is_unchanged(browser, site):
    api = FakeAPI()
    ctx, pg = _open_themed(browser, site, api)
    try:
        pg.click("#tMenu")
        got = {k: _css(pg, *k) for k in DARK}
        assert got == DARK
    finally:
        ctx.close()


def test_light_theme_colours(browser, site):
    api = FakeAPI()
    ctx, pg = _open_themed(browser, site, api, theme="light")
    try:
        pg.click("#tMenu")
        bg = _css(pg, "body", "backgroundColor")
        assert _lum(bg) > 0.8, bg                    # light, but a muted grey, not stark white
        assert _lum(_css(pg, "body", "color")) < 0.05
        for sel in (".sidebar", ".topbar", "#tMenuList", "#search"):
            assert _lum(_css(pg, sel, "backgroundColor")) > 0.75, sel
        # the sidebar sits a touch deeper than the main area
        assert _lum(_css(pg, ".sidebar", "backgroundColor")) < _lum(bg)
        side = _css(pg, ".sidebar", "backgroundColor")
        # text that must stay readable on the light sidebar
        for sel in ("#list .card[data-sid] .proj", "#newBtn", "#tOpen", "#tTheme", "#cpuVal",
                    '#list .card[data-sid="dddd0004"] .sid'):
            c = _css(pg, sel, "color")
            assert _contrast(c, side) >= 4.5, (sel, c)    # WCAG AA
        for sel in ("#tLogout",):
            assert _contrast(_css(pg, sel, "color"), "rgb(255, 255, 255)") >= 4.0, sel
        # status dots: working / idle / off all visible on white and different
        dots = {s: _css(pg, f".dot.{s}", "backgroundColor") for s in ("working", "idle", "off")}
        assert len(set(dots.values())) == 3
        for s, c in dots.items():
            assert _contrast(c, side) >= 1.6, (s, c)
        # the selected row still stands out
        assert _css(pg, ".card.sel", "borderLeftColor") != side
        # the terminal keeps its dark frame
        assert _css(pg, "#wrap iframe", "backgroundColor") == "rgb(0, 0, 0)"
        shot(pg, "theme-light-desktop.png")
    finally:
        ctx.close()


def test_light_theme_at_390px(browser, site):
    api = FakeAPI()
    ctx, pg = _open(browser, site, api, width=390, height=844, mobile=True)
    try:
        pg.evaluate(f"localStorage.setItem('{THEME_KEY}', 'light')")
        pg.reload()
        pg.wait_for_selector(".card[data-sid]")
        pg.tap(row("dddd0004") + " .proj")
        pg.wait_for_selector("#topbar:visible")
        assert _lum(_css(pg, "#mobInput", "backgroundColor")) > 0.75
        assert _lum(_css(pg, "#mobText", "backgroundColor")) > 0.75
        pg.tap("#tMenu")
        b = pg.eval_on_selector("#tTheme", "e => { const r = e.getBoundingClientRect();"
                                           " return [r.left, r.right, r.width]; }")
        assert b[2] > 0 and b[0] >= 0 and b[1] <= 390.5, b
        over = pg.evaluate("() => document.documentElement.scrollWidth - innerWidth")
        assert over <= 0
        shot(pg, "theme-light-mobile.png")
    finally:
        ctx.close()


def test_open_server_tab_follows_the_switch_live(browser, site):
    """The real server.html in the viewer changes colour when the menu switch is
    pressed — no reload (the storage event reaches the same-origin iframe)."""
    from tests.test_server_page import FakeServerAPI
    api, srv = FakeAPI(), FakeServerAPI()
    ctx, pg = _open(browser, site, api)
    try:
        pg.route(re.compile(r"/api/server(\?|$)"), srv.handle)
        pg.click(SERVER_LINK)
        pg.wait_for_selector("#wrap iframe")
        fr = pg.frame_locator("#wrap iframe")
        fr.locator(".row[data-id]").first.wait_for()
        frame = next(f for f in pg.frames if f.url.endswith("/server.html"))
        dark_bg = frame.evaluate("getComputedStyle(document.body).backgroundColor")
        assert dark_bg == "rgb(9, 9, 11)"
        pg.click("#tMenu")
        pg.click("#tTheme")
        frame.wait_for_function("getComputedStyle(document.body).backgroundColor !== 'rgb(9, 9, 11)'",
                                timeout=3000)
        assert _lum(frame.evaluate("getComputedStyle(document.body).backgroundColor")) > 0.85
        pg.click("#tTheme")
        frame.wait_for_function("getComputedStyle(document.body).backgroundColor === 'rgb(9, 9, 11)'",
                                timeout=3000)
    finally:
        ctx.close()


def test_pinned_rows_are_one_line_without_subtitles(browser, site):
    """Owner, 2026-09-25: Tasks / Server / Command line take one line each — no
    'board' / 'status' / 'bash' captions under them."""
    api = FakeAPI()
    api.shell = dict(SHELL_ON)
    ctx, pg = _open_server_ctx(browser, site, api)
    try:
        pg.click(TASKS_LINK)
        pg.wait_for_selector("#list .tasks-row")
        pg.click(SERVER_LINK)
        pg.wait_for_selector("#list .server-row")
        pg.wait_for_selector("#list .shell-row")
        term_h = pg.eval_on_selector(row("cccc0003"), "e => e.getBoundingClientRect().height")
        for cls in ("tasks-row", "server-row", "shell-row"):
            sel = f"#list .{cls}"
            assert pg.query_selector_all(sel + " .card-row2") == [], cls
            assert pg.query_selector_all(sel + " .sid") == [], cls
            h = pg.eval_on_selector(sel, "e => e.getBoundingClientRect().height")
            assert h < term_h * 0.75, (cls, h, term_h)
            # name, ✕ and dot on one line
            tops = pg.eval_on_selector_all(
                sel + " .proj, " + sel + " .card-btns > *",
                "els => els.map(e => Math.round(e.getBoundingClientRect().top + e.getBoundingClientRect().height / 2))")
            assert max(tops) - min(tops) <= 4, (cls, tops)
    finally:
        ctx.close()


# ── terminal code <-> its tasks (owner, 2026-09-25) ────────────────────────
# The 8-hex code under a row's name opens that terminal's tasks (like the id in
# the top bar); the task board sends a task's code back to open the terminal.
def _tasks_stubbed(browser, site, api, url=None, **kw):
    ctx, pg = _open_tasks_ctx(browser, site, api, **kw)
    pg.route(re.compile(r"/tasks/\?session="),
             lambda r: r.fulfill(status=200, content_type="text/html",
                                 body="<html><body>filtered board stub</body></html>"))
    return ctx, pg


def _src(pg):
    f = pg.query_selector("#wrap iframe")
    return f.get_attribute("src") if f else None


def test_clicking_the_code_in_the_list_shows_that_terminals_tasks(browser, site):
    api = FakeAPI()
    ctx, pg = _tasks_stubbed(browser, site, api)
    try:
        sid = row("cccc0003") + " .sid"
        assert pg.eval_on_selector(sid, "e => getComputedStyle(e).cursor") == "pointer"
        assert pg.get_attribute(sid, "title") == "Show this terminal's tasks"
        pg.hover(sid)
        assert "underline" in pg.eval_on_selector(sid, "e => getComputedStyle(e).textDecorationLine")
        pg.click(sid)
        pg.wait_for_function("() => (document.querySelector('#wrap iframe')||{}).getAttribute"
                             " && document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/tasks/?session=cccc0003'")
        assert pg.inner_text("#tNum") == "tasks"
        pg.wait_for_selector("#list .tasks-row.sel")
        # the rest of the row still opens the terminal
        pg.click(row("aaaa0001") + " .proj")
        pg.wait_for_function("() => document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/sess/?arg=aaaa0001'")
        assert api.posts("/api/library/reorder") == []           # a click is not a drag
    finally:
        ctx.close()


def test_clicking_the_code_of_an_archived_row_shows_its_tasks(browser, site):
    api = FakeAPI()
    api.sessions.append(archived_session())
    ctx, pg = _tasks_stubbed(browser, site, api)
    try:
        pg.click("#showArchived")
        pg.wait_for_selector(row("ffff0005"))
        pg.click(row("ffff0005") + " .sid")
        pg.wait_for_function("() => (document.querySelector('#wrap iframe')||{}).getAttribute"
                             " && document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/tasks/?session=ffff0005'")
        assert api.posts("/api/library/archive") == []
    finally:
        ctx.close()


def test_code_click_on_a_phone_shows_tasks_and_arrows_still_work(browser, site):
    api = FakeAPI()
    ctx, pg = _tasks_stubbed(browser, site, api, width=400, height=800, mobile=True)
    try:
        pg.click(row("cccc0003") + " .sid")
        pg.wait_for_function("() => (document.querySelector('#wrap iframe')||{}).getAttribute"
                             " && document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/tasks/?session=cccc0003'")
        pg.click(row("aaaa0001") + " .mv-btn.down")
        pg.wait_for_function("() => [...document.querySelectorAll('#list .card[data-sid]')]"
                             ".map(e => e.dataset.sid).join() === "
                             "'dddd0004,bbbb0002,cccc0003,aaaa0001'", timeout=3000)
    finally:
        ctx.close()


def _board_frame(pg):
    pg.click(TASKS_LINK)
    pg.wait_for_selector("#wrap iframe")
    pg.wait_for_function("() => window.frames.length > 0")
    fr = pg.query_selector("#wrap iframe").content_frame()
    fr.wait_for_selector("body")
    return fr


def test_board_message_from_the_same_origin_opens_the_terminal(browser, site):
    api = FakeAPI()
    ctx, pg = _tasks_stubbed(browser, site, api)
    try:
        fr = _board_frame(pg)
        # cccc0003 is hidden by the search filter: still opens
        pg.fill("#search", "свет")
        fr.evaluate("() => parent.postMessage({type: 'agentdeck:open-terminal', id: 'cccc0003'},"
                    " location.origin)")
        pg.wait_for_function("() => document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/sess/?arg=cccc0003'")
        assert pg.inner_text("#tNum") == "cccc0003"
    finally:
        ctx.close()


def test_board_message_for_an_unknown_terminal_only_toasts(browser, site):
    api = FakeAPI()
    ctx, pg = _tasks_stubbed(browser, site, api)
    try:
        fr = _board_frame(pg)
        fr.evaluate("() => parent.postMessage({type: 'agentdeck:open-terminal', id: '12345678'},"
                    " location.origin)")
        pg.wait_for_function("() => ((document.getElementById('_imgToast')||{}).textContent||'')"
                             ".includes('12345678')", timeout=3000)
        assert "not found" in pg.inner_text("#_imgToast").lower()
        assert _src(pg) == "/tasks/"
        # garbage ids and other message types are ignored outright
        fr.evaluate("() => { parent.postMessage({type: 'agentdeck:open-terminal', id: 'x\\');'},"
                    " location.origin); parent.postMessage({type: 'other', id: 'cccc0003'},"
                    " location.origin); }")
        pg.wait_for_timeout(300)
        assert _src(pg) == "/tasks/"
    finally:
        ctx.close()


def test_board_message_for_an_archived_terminal_restores_and_opens_it(browser, site):
    api = FakeAPI()
    api.sessions.append(archived_session())
    ctx, pg = _tasks_stubbed(browser, site, api)
    try:
        fr = _board_frame(pg)
        fr.evaluate("() => parent.postMessage({type: 'agentdeck:open-terminal', id: 'ffff0005'},"
                    " location.origin)")
        pg.wait_for_function("() => document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/sess/?arg=ffff0005'", timeout=3000)
        assert api.posts("/api/library/archive") == [
            ("POST", "/api/library/archive", {"id": "ffff0005", "archived": False})]
    finally:
        ctx.close()


def test_foreign_origin_message_is_ignored(browser, site):
    api = FakeAPI()
    ctx, pg = _tasks_stubbed(browser, site, api)
    try:
        _board_frame(pg)
        pg.evaluate("() => window.dispatchEvent(new MessageEvent('message', {origin:"
                    " 'https://evil.example', data: {type: 'agentdeck:open-terminal',"
                    " id: 'cccc0003'}}))")
        pg.wait_for_timeout(300)
        assert _src(pg) == "/tasks/"
        # the same message from our own origin works (the check is the origin)
        pg.evaluate("() => window.dispatchEvent(new MessageEvent('message', {origin:"
                    " location.origin, data: {type: 'agentdeck:open-terminal', id: 'cccc0003'}}))")
        pg.wait_for_function("() => document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/sess/?arg=cccc0003'")
    finally:
        ctx.close()


def test_open_param_on_load_opens_that_terminal_and_is_removed(browser, site):
    api = FakeAPI()
    ctx, pg = _open(browser, site + "?open=cccc0003", api)
    try:
        pg.wait_for_function("() => (document.querySelector('#wrap iframe')||{}).getAttribute"
                             " && document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/sess/?arg=cccc0003'")
        assert "open=" not in pg.url
        assert pg.url.endswith("/index-lib.html")
    finally:
        ctx.close()


def test_open_param_for_an_archived_terminal_restores_and_opens_it(browser, site):
    api = FakeAPI()
    api.sessions.append(archived_session())
    ctx, pg = _open(browser, site + "?open=ffff0005", api)
    try:
        pg.wait_for_function("() => (document.querySelector('#wrap iframe')||{}).getAttribute"
                             " && document.querySelector('#wrap iframe').getAttribute('src')"
                             " === '/sess/?arg=ffff0005'", timeout=3000)
        assert api.posts("/api/library/archive") == [
            ("POST", "/api/library/archive", {"id": "ffff0005", "archived": False})]
        assert "open=" not in pg.url
    finally:
        ctx.close()


def test_open_param_with_garbage_or_unknown_id_does_nothing(browser, site):
    for q in ("?open=%3Cb%3E", "?open=12345678"):
        api = FakeAPI()
        ctx, pg = _open(browser, site + q, api)
        try:
            pg.wait_for_timeout(500)
            assert pg.query_selector("#wrap iframe") is None, q
            assert "open=" not in pg.url, q
        finally:
            ctx.close()


# ── the ⋯ account menu is reachable with no terminal open (fresh-user test) ──
def test_account_menu_on_the_empty_dashboard(page):
    # nothing picked yet: Password / Theme / Telegram / Logout must still be reachable
    assert page.is_visible("#ph")
    assert page.is_visible("#tMenu")
    for bid in ("tEsc", "tPasteImg", "tOpen"):              # terminal-only buttons stay hidden
        assert not page.is_visible("#" + bid), bid
    page.click("#tMenu")
    assert _menu_open(page)
    for bid in ("tTg", "tChpw", "tTheme", "tLogout"):
        assert page.is_visible("#" + bid), bid
    shot(page, "index-lib-empty-account-menu.png")
    page.keyboard.press("Escape")
    assert not _menu_open(page)


def test_account_menu_stays_after_a_terminal_is_closed(page):
    page.click(row("dddd0004") + " .proj")
    page.wait_for_selector("#tEsc:visible")
    page.evaluate("showPlaceholder()")
    assert page.is_visible("#tMenu") and not page.is_visible("#tEsc")
    page.click(row("dddd0004") + " .proj")                 # and the terminal buttons come back
    page.wait_for_selector("#tEsc:visible")
    assert _topbar_order(page) == ["tEsc", "tPasteImg", "tOpen", "tMenu"]


# ── "Copied" chip after a mouse selection is copied (owner, 2026-09-26) ─────
# The terminal stub here has an .xterm element, so enableClipboardCopy binds its
# mousedown/mouseup on it; /api/tmux-buffer/ is faked: the read at mousedown
# (the anchor) returns the previous buffer, later reads the new selection.
HINT_KEY = "agentdeck-copy-hint"
HINT_PC = "Copied · Ctrl+Shift+V to paste"
HINT_MAC = "Copied · ⌘V to paste"
MAC_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
XTERM_STUB = ("<html><body style='margin:0;background:#000;color:#0f0'>"
              "<div class='xterm' style='width:100vw;height:100vh;font:14px monospace'>"
              "ubuntu@server:~$ ls -la<br>total 42</div></body></html>")


class FakeBuffer:
    def __init__(self, new_text="ls -la", old_text="previous"):
        self.new_text, self.old_text, self.reads = new_text, old_text, 0

    def __call__(self, route):
        self.reads += 1
        text = self.old_text if self.reads == 1 else self.new_text
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"text": text}))


def _copy_page(browser, site, buf=None, **kw):
    ctx, pg = _open(browser, site, FakeAPI(), **kw)
    ctx.grant_permissions(["clipboard-read", "clipboard-write"],
                          origin=site.rsplit("/", 1)[0])
    pg.route(re.compile(r"/sess/"),
             lambda r: r.fulfill(status=200, content_type="text/html", body=XTERM_STUB))
    pg.route(re.compile(r"/api/tmux-buffer/"), buf or FakeBuffer())
    return ctx, pg


def _open_terminal(pg, sid="dddd0004"):
    pg.click(row(sid) + " .proj")
    pg.wait_for_selector("#wrap iframe[src^='/sess/']")
    pg.frame_locator("#wrap iframe").locator(".xterm").wait_for()
    pg.wait_for_timeout(250)                              # enableClipboardCopy polls every 100ms


def _select(pg, drag=True):
    box = pg.query_selector("#wrap iframe").bounding_box()
    x, y = box["x"] + 40, box["y"] + 60
    pg.mouse.move(x, y)
    pg.mouse.down()
    pg.mouse.move(x + (120 if drag else 0), y + (20 if drag else 0), steps=5)
    pg.mouse.up()


def _hint_shown(pg):
    return pg.evaluate("!!document.getElementById('copyHint')?.classList.contains('show')")


def _wait_hint(pg, timeout=3000):
    pg.wait_for_function("document.getElementById('copyHint')?.classList.contains('show')",
                         timeout=timeout)


def test_copied_chip_after_a_selection_is_copied(browser, site):
    ctx, pg = _copy_page(browser, site)
    try:
        _open_terminal(pg)
        pg.mouse.move(700, 400)                           # hovering alone shows nothing
        pg.wait_for_timeout(300)
        assert not _hint_shown(pg)
        _select(pg)
        _wait_hint(pg)
        pg.wait_for_timeout(300)                          # past the fade-in
        h = pg.query_selector("#copyHint")
        assert h.inner_text().strip() == HINT_PC
        assert float(h.evaluate("e => getComputedStyle(e).opacity")) > 0.5
        assert h.evaluate("e => getComputedStyle(e).pointerEvents") == "none"
        hb, wb = h.bounding_box(), pg.query_selector("#wrap").bounding_box()
        assert hb["x"] + hb["width"] > wb["x"] + wb["width"] - 40      # top-right
        assert hb["y"] < wb["y"] + 40
        assert pg.evaluate("navigator.clipboard.readText()") == "ls -la"
        assert pg.evaluate(f"localStorage.getItem('{HINT_KEY}')") == "1"
        shot(pg, "index-lib-copied-chip.png")
        pg.wait_for_function("!document.getElementById('copyHint').classList.contains('show')",
                             timeout=2500)               # ~1.5 s, then it fades
    finally:
        ctx.close()


def test_copied_chip_light_theme(browser, site):
    ctx, pg = _copy_page(browser, site, init_script=(
        "try { localStorage.setItem('agentdeck-theme', 'light'); } catch (e) {}"))
    try:
        _open_terminal(pg)
        _select(pg)
        _wait_hint(pg)
        pg.wait_for_timeout(300)
        bg = pg.eval_on_selector("#copyHint", "e => getComputedStyle(e).backgroundColor")
        assert bg == "rgb(247, 247, 248)", bg            # --menu-bg of the light palette
        shot(pg, "index-lib-copied-chip-light.png")
    finally:
        ctx.close()


def test_copied_chip_shows_on_every_copy(browser, site):
    ctx, pg = _copy_page(browser, site)
    try:
        _open_terminal(pg)
        for _ in range(2):
            _select(pg)
            _wait_hint(pg)
            pg.wait_for_function(
                "!document.getElementById('copyHint').classList.contains('show')", timeout=2500)
    finally:
        ctx.close()


def test_no_chip_on_a_plain_click(browser, site):
    ctx, pg = _copy_page(browser, site)
    try:
        _open_terminal(pg)
        _select(pg, drag=False)
        pg.wait_for_timeout(1200)
        assert not _hint_shown(pg)
    finally:
        ctx.close()


def test_no_chip_when_nothing_was_copied(browser, site):
    ctx, pg = _copy_page(browser, site, buf=FakeBuffer(new_text="", old_text=""))
    try:
        _open_terminal(pg)
        _select(pg)
        pg.wait_for_timeout(1500)                        # the buffer poll gives up at ~700 ms
        assert not _hint_shown(pg)
    finally:
        ctx.close()


def test_no_chip_on_the_tasks_tab(browser, site):
    ctx, pg = _copy_page(browser, site)
    pg.route(re.compile(r"/tasks/"),
             lambda r: r.fulfill(status=200, content_type="text/html", body=XTERM_STUB))
    try:
        pg.click("#tasksBtn")
        pg.wait_for_selector("#wrap iframe[src^='/tasks/']")
        pg.frame_locator("#wrap iframe").locator(".xterm").wait_for()
        pg.wait_for_timeout(250)
        _select(pg)
        pg.wait_for_timeout(1200)
        assert not _hint_shown(pg)
    finally:
        ctx.close()


def test_after_the_limit_the_chip_just_says_copied(browser, site):
    ctx, pg = _copy_page(browser, site,
                         init_script=f"try {{ localStorage.setItem('{HINT_KEY}', '5'); }} catch (e) {{}}")
    try:
        _open_terminal(pg)
        _select(pg)
        _wait_hint(pg)
        assert pg.inner_text("#copyHint").strip() == "Copied"
        assert pg.evaluate(f"localStorage.getItem('{HINT_KEY}')") == "5"
    finally:
        ctx.close()


def test_copied_chip_mac_wording(browser, site):
    ctx, pg = _copy_page(browser, site, user_agent=MAC_UA)
    try:
        _open_terminal(pg)
        _select(pg)
        _wait_hint(pg)
        assert pg.inner_text("#copyHint").strip() == HINT_MAC
    finally:
        ctx.close()


def test_copied_chip_survives_broken_storage(browser, site):
    ctx, pg = _copy_page(browser, site, init_script="""for (const m of ['getItem', 'setItem']) {
        const orig = Storage.prototype[m];
        Storage.prototype[m] = function (k, ...a) {
          if (k === 'agentdeck-copy-hint') throw new Error('blocked');
          return orig.call(this, k, ...a); }; }""")
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        _open_terminal(pg)
        _select(pg)
        _wait_hint(pg)
        assert pg.inner_text("#copyHint").strip() == HINT_PC
        assert errors == [], errors
    finally:
        ctx.close()


# ── one number per terminal: the page follows a terminal's number ──────────
# When the conversation live in a terminal changes (Claude's consent relaunch,
# /clear, /resume), the server gives the terminal the new conversation's number:
# the row carries `prev_id` (the number it had), `switched_at`, and — when the old
# number never was a conversation — `aliases` (old numbers that lead to it).
def _src_is(pg, sid, timeout=10000):
    pg.wait_for_function("id => ((document.querySelector('#wrap iframe') || {}).getAttribute"
                         " && document.querySelector('#wrap iframe').getAttribute('src'))"
                         " === '/sess/?arg=' + id", arg=sid, timeout=timeout)


def _after_next_poll(pg, api):
    n = len(api.gets())
    for _ in range(100):
        pg.wait_for_timeout(100)
        if len(api.gets()) > n:
            pg.wait_for_timeout(200)          # let the page apply it
            return
    raise AssertionError("no poll within 10 s")


def test_open_link_with_a_vanished_number_opens_its_terminal(browser, site):
    api = FakeAPI()
    s = next(x for x in api.sessions if x["id"] == "dddd0004")
    s.update(aliases=["9999aaaa"], prev_id="9999aaaa", switched_at=1000)
    ctx, pg = _open(browser, site + "?open=9999aaaa", api)
    try:
        _src_is(pg, "dddd0004")
        assert pg.inner_text("#tNum") == "dddd0004"
        assert "sel" in pg.get_attribute(row("dddd0004"), "class")
        # the task board's "open this terminal" with the old number, too
        pg.click(row("cccc0003") + " .proj")
        _src_is(pg, "cccc0003")
        pg.evaluate("() => window.dispatchEvent(new MessageEvent('message', {origin:"
                    " location.origin, data: {type: 'agentdeck:open-terminal', id: '9999aaaa'}}))")
        _src_is(pg, "dddd0004")
    finally:
        ctx.close()


def test_open_terminal_follows_a_switch_and_not_back(page, api):
    page.click(row("dddd0004") + " .proj")
    _src_is(page, "dddd0004")
    # /clear in it: the terminal goes on as eeee0009; dddd0004 stays as an earlier row
    old = next(s for s in api.sessions if s["id"] == "dddd0004")
    api.sessions.append(dict(old, id="eeee0009", prev_id="dddd0004", switched_at=2000))
    old.update(name=old["name"] + " (earlier)", active=False, attached=False, status="off")
    _src_is(page, "eeee0009")
    assert page.inner_text("#tNum") == "eeee0009"
    assert page.get_attribute("#tOpen", "href") == "/sess/?arg=eeee0009"
    assert "sel" in page.get_attribute(row("eeee0009"), "class")
    # opening the earlier conversation on purpose: the page does not jump back
    page.click(row("dddd0004") + " .proj")
    _src_is(page, "dddd0004")
    _after_next_poll(page, api)
    _after_next_poll(page, api)
    assert page.get_attribute("#wrap iframe", "src") == "/sess/?arg=dddd0004"
    assert page.inner_text("#tNum") == "dddd0004"


def test_open_terminal_follows_when_its_number_is_taken_over(page, api):
    # the consent case: the old number simply was never a conversation — gone
    page.click(row("bbbb0002") + " .proj")
    _src_is(page, "bbbb0002")
    s = next(x for x in api.sessions if x["id"] == "bbbb0002")
    s.update(id="eeee0010", aliases=["bbbb0002"], prev_id="bbbb0002", switched_at=3000)
    _src_is(page, "eeee0010")
    assert page.inner_text("#tNum") == "eeee0010"
    assert page.query_selector(row("bbbb0002")) is None
    assert "sel" in page.get_attribute(row("eeee0010"), "class")


def test_open_terminal_follows_two_switches_seen_in_one_poll(page, api):
    # review 2026-09-26: the tab was in the background (browsers slow its timers to
    # one a minute) while the terminal went on twice: /clear -> eeee0009, work,
    # /clear -> ffff000a. Following one step re-armed the "seen" mark with the second
    # switch, so the page stopped on eeee0009 — an earlier row, which opening then
    # loads anew next to the terminal — instead of the terminal, ffff000a.
    page.click(row("dddd0004") + " .proj")
    _src_is(page, "dddd0004")
    old = next(s for s in api.sessions if s["id"] == "dddd0004")
    mid = dict(old, id="eeee0009", prev_id="dddd0004", switched_at=2000,
               name=old["name"] + " (earlier)", active=False, attached=False, status="off")
    new = dict(old, id="ffff000a", prev_id="eeee0009", switched_at=2001)
    old.update(name=old["name"] + " (earlier)", active=False, attached=False, status="off")
    api.sessions.extend([mid, new])                  # both in the same poll
    _src_is(page, "ffff000a")
    _after_next_poll(page, api)
    assert page.get_attribute("#wrap iframe", "src") == "/sess/?arg=ffff000a"
    assert page.inner_text("#tNum") == "ffff000a"
    # picking the middle one on purpose afterwards sticks
    page.click(row("eeee0009") + " .proj")
    _src_is(page, "eeee0009")
    _after_next_poll(page, api)
    _after_next_poll(page, api)
    assert page.get_attribute("#wrap iframe", "src") == "/sess/?arg=eeee0009"
