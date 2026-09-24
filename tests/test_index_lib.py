"""Browser contract for web/index-lib.html — the dashboard with a session library.

The left column is no longer twelve numbered slots but a library of named
topics (sessions): search, «＋ Новая тема», loaded ones first with a status dot,
unloaded ones greyed, each row = name + 8-hex code + ✎ + «в архив». A click
opens the one terminal endpoint `/sess/?arg=<id>` in the iframe.

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
        self.with_system = with_system
        self.fail_new = False
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

        if req.method == "GET" and path == "/api/library":
            out = {"max_active": 12,
                   "sessions": [s for s in self.sessions if not s["archived"]]}
            if self.with_system:
                out["_system"] = {"cpu_pct": 42, "ram_pct": 50,
                                  "ram_used_mb": 7000, "ram_total_mb": 16000}
            return self._json(route, out)
        if req.method != "POST":
            return self._json(route, {"error": "method"}, 405)

        if path == "/api/library/new":
            if self.fail_new:
                return self._json(route, {"error": "диск полон"}, 500)
            self._n += 1
            sid = f"eeee000{self._n}"
            e = {"id": sid, "name": (body.get("name") or "").strip() or "Тема 24.09 10:00",
                 "cwd": "/home/ubuntu/pr", "created": 999, "last_used": 999,
                 "archived": False, "active": False, "attached": False, "status": "off"}
            self.sessions.append(e)
            return self._json(route, e)
        sid = (body or {}).get("id")
        e = next((s for s in self.sessions if s["id"] == sid), None)
        if e is None:
            return self._json(route, {"error": "unknown id"}, 404)
        if path == "/api/library/rename":
            e["name"] = body["name"]
            return self._json(route, e)
        if path == "/api/library/archive":
            e["archived"] = bool(body.get("archived"))
            return self._json(route, e)
        if path == "/api/library/close":
            e["active"] = False
            return self._json(route, e)
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


def _open(browser, site, api, width=1440, height=900, mobile=False):
    ctx = browser.new_context(viewport={"width": width, "height": height},
                              is_mobile=mobile, has_touch=mobile)
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
    # the live dashboard keeps its slots until the migration is done
    assert "CLAUDE_IDS" in (WEB_DIR / "index.html").read_text(encoding="utf-8")


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
    assert page.inner_text("#newBtn").strip() == "＋ Новая тема"
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
def test_archive_needs_a_second_tap_then_hides_the_row(page, api):
    btn = row("cccc0003") + " .arch-btn"
    assert page.inner_text(btn).strip() == "в архив"
    page.click(btn)                                   # first tap only arms it
    assert api.posts("/api/library/archive") == []
    assert page.inner_text(btn).strip() != "в архив"
    page.click(btn)
    page.wait_for_selector(row("cccc0003"), state="detached")
    assert api.posts("/api/library/archive") == [
        ("POST", "/api/library/archive", {"id": "cccc0003", "archived": True})]
    assert page.query_selector("#wrap iframe") is None


def test_archive_arm_expires(page, api):
    btn = row("cccc0003") + " .arch-btn"
    page.click(btn)
    page.wait_for_timeout(3300)
    assert page.inner_text(btn).strip() == "в архив"
    page.click(btn)
    assert api.posts("/api/library/archive") == []


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
    assert page.inner_text("#ramVal") == "6400/16000M"
    page.click(row("dddd0004") + " .proj")
    for bid in ("tPasteImg", "tOpen", "tEsc", "tTg", "tChpw", "tLogout"):
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
