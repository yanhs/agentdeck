"""TDD for the redesigned task board (tasks-dashboard/static/index-next.html).

Drives the page through Chromium against the REAL server.py handler (same /state.json
and /events SSE), running in-process on a free port with a temp TRACKER_STATE. The
page carries <base href="/tasks/"> like production, so the test server strips the
/tasks prefix the way nginx does. server.py itself is not modified: the test only
points the module's INDEX at index-next.html.

Covers: sections + counts, Done collapsed + paginated, instant search with
highlight (title / id / agent / step / note), status + agent filters, sort, expand
with steps / times / progress / duration, SSE update that keeps UI state, 400 px
without horizontal scroll, light + dark, and the real 290-task state rendering fast.
Screenshots land in tests/artifacts/tasks-next-*.png.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = ROOT / "tasks-dashboard"
NEXT = TASKS_DIR / "static" / "index-next.html"
REAL_STATE = TASKS_DIR / "state.json"
ART = Path(__file__).resolve().parent / "artifacts"


# ---------------------------------------------------------------- fixture data
def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def make_state(now: datetime) -> dict:
    ago = lambda **kw: iso(now - timedelta(**kw))  # noqa: E731
    tasks = [
        {
            "id": "pipeline-quality", "title": "Improve brief pipeline quality",
            "agent": "claude · appeals", "status": "active", "activity": "working",
            "session": "5e1f00ab",
            "created": ago(hours=3), "updated": ago(minutes=5),
            "items": [
                {"title": "Read code", "status": "done", "note": "", "updated": ago(hours=2, minutes=50)},
                {"title": "Red tests", "status": "done", "note": "10 red", "updated": ago(hours=2)},
                {"title": "Wire citation auditor", "status": "active",
                 "note": "zebrafish-note lives here", "updated": ago(minutes=5)},
                {"title": "Deploy dev", "status": "todo", "note": "", "updated": ago(hours=3)},
            ],
        },
        {
            "id": "svetlota-fonts", "title": "Svetlota font loading",
            "agent": "claude · svetlota", "status": "active",
            "created": ago(days=1), "updated": ago(hours=1),
            "items": [{"title": "Measure CLS", "status": "active", "note": "", "updated": ago(hours=1)}],
        },
        {
            "id": "vpn-cert", "title": "Rotate VPN certificate",
            "agent": "claude · vpn", "status": "blocked",
            "created": ago(days=2), "updated": ago(hours=5),
            "items": [{"title": "Wait for DNS", "status": "blocked", "note": "registrar down", "updated": ago(hours=5)}],
        },
        {
            "id": "gmail-sync", "title": "Gmail sync worker",
            "agent": "claude · gmail", "status": "active", "activity": "blocked",
            "activity_note": "needs OAuth consent",
            "created": ago(days=3), "updated": ago(hours=6), "items": [],
        },
        {
            "id": "yacht-landing", "title": "Yacht landing copy",
            "agent": "claude · yacht", "status": "paused",
            "created": ago(days=4), "updated": ago(days=1), "items": [],
        },
    ]
    for i in range(120):
        tasks.append({
            "id": f"done-{i:03d}", "title": f"Archived job number {i:03d}",
            "agent": "claude · terminal" if i % 2 else "claude · appeals",
            "status": "done",
            "created": ago(days=10, minutes=i * 7 + 90),
            "updated": ago(days=10, minutes=i * 7),
            "items": [{"title": "only step", "status": "done", "note": "", "updated": ago(days=10, minutes=i * 7)}],
        })
    return {"title": "Claude Task Tracker", "updated": ago(minutes=5), "tasks": tasks}


# ---------------------------------------------------------------- server harness
def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Board:
    def __init__(self, state_path: Path):
        self.state_path = state_path
        old = os.environ.get("TRACKER_STATE")
        os.environ["TRACKER_STATE"] = str(state_path)
        sys.path.insert(0, str(TASKS_DIR))
        sys.modules.pop("server", None)
        srv = importlib.import_module("server")
        if old is None:
            os.environ.pop("TRACKER_STATE", None)
        else:
            os.environ["TRACKER_STATE"] = old
        srv.INDEX = NEXT  # module-level override for this in-process copy only

        class Prefixed(srv.H):  # emulate nginx `location /tasks/ { proxy_pass .../; }`
            def do_GET(self):
                if self.path.startswith("/tasks"):
                    self.path = self.path[len("/tasks"):] or "/"
                return super().do_GET()

        self.port = _free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Prefixed)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}/tasks/"

    def write(self, state: dict):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False))
        os.replace(tmp, self.state_path)

    def close(self):
        self.httpd.shutdown()


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    d = tmp_path_factory.mktemp("board")
    b = Board(d / "state.json")
    b.write(make_state(datetime.now(timezone.utc)))
    yield b
    b.close()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        br = p.chromium.launch()
        yield br
        br.close()


@pytest.fixture
def page(browser, board):
    board.write(make_state(datetime.now(timezone.utc)))
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(board.url)
    pg.wait_for_selector(".task")
    yield pg
    ctx.close()
    assert not errors, errors


def visible_ids(pg) -> list[str]:
    return pg.eval_on_selector_all(
        ".task", "els => els.filter(e => e.offsetParent !== null).map(e => e.dataset.id)")


def group_ids(pg, group: str) -> list[str]:
    return pg.eval_on_selector_all(
        f'section.group[data-group="{group}"] .task', "els => els.map(e => e.dataset.id)")


# ---------------------------------------------------------------- tests
def test_file_exists_and_live_index_untouched():
    assert NEXT.exists(), "static/index-next.html must exist"
    live = (TASKS_DIR / "server.py").read_text()
    assert 'INDEX = BASE / "static" / "index.html"' in live  # live route unchanged


def test_sections_and_counts(page):
    for g in ("active", "blocked", "pending", "done"):
        assert page.locator(f'section.group[data-group="{g}"]').count() == 1, g
    cnt = lambda g: page.locator(f'section.group[data-group="{g}"] .group-count').inner_text().strip()  # noqa: E731
    assert cnt("active") == "2"
    assert cnt("blocked") == "2"  # status=blocked + activity=blocked
    assert cnt("pending") == "1"
    assert cnt("done") == "120"
    assert set(group_ids(page, "blocked")) == {"vpn-cert", "gmail-sync"}
    # status filter chips carry counts too
    assert page.locator('.status-filter [data-status="all"] .n').inner_text() == "125"
    assert page.locator('.status-filter [data-status="done"] .n').inner_text() == "120"


def test_done_collapsed_and_paginated(page):
    done = page.locator('section.group[data-group="done"]')
    head = done.locator(".group-head")
    assert head.get_attribute("aria-expanded") == "false"
    assert done.locator(".task").count() == 0 or not done.locator(".task").first.is_visible()
    head.click()
    assert head.get_attribute("aria-expanded") == "true"
    assert done.locator(".task").count() == 50
    more = done.locator("button.more")
    assert "70" in more.inner_text()
    more.click()
    assert done.locator(".task").count() == 100
    more.click()
    assert done.locator(".task").count() == 120
    assert done.locator("button.more").count() == 0


@pytest.mark.parametrize("query,expect", [
    ("font loading", {"svetlota-fonts"}),           # title
    ("vpn-cert", {"vpn-cert"}),                      # id
    ("svetlota", {"svetlota-fonts"}),                # agent
    ("citation auditor", {"pipeline-quality"}),      # step title
    ("zebrafish", {"pipeline-quality"}),             # step note
    ("oauth consent", {"gmail-sync"}),               # activity note
    ("number 007", {"done-007"}),                    # done tasks are searched too
    ("5e1f00", {"pipeline-quality"}),                # agent's 8-hex session id
])
def test_search_instant_with_highlight(page, query, expect):
    page.fill("#q", query)  # no Enter: instant
    page.wait_for_timeout(150)
    assert set(visible_ids(page)) == expect
    assert page.locator(".task mark").count() >= 1
    assert page.locator(".task mark").first.inner_text().lower() in query.lower()


def test_search_empty_state_and_clear(page):
    page.fill("#q", "nothing-matches-this-xyz")
    page.wait_for_timeout(150)
    assert visible_ids(page) == []
    assert page.locator(".empty").is_visible()
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert page.input_value("#q") == ""
    assert "pipeline-quality" in visible_ids(page)


def test_slash_focuses_search(page):
    page.click("h1")
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "q"


def test_status_filter(page):
    page.click('.status-filter [data-status="blocked"]')
    page.wait_for_timeout(100)
    assert set(visible_ids(page)) == {"vpn-cert", "gmail-sync"}
    page.click('.status-filter [data-status="done"]')
    page.wait_for_timeout(100)
    ids = visible_ids(page)
    assert len(ids) == 50 and all(i.startswith("done-") for i in ids)  # opened + paginated
    page.click('.status-filter [data-status="all"]')
    assert "pipeline-quality" in visible_ids(page)


def test_agent_filter_derived_from_data(page):
    opts = page.eval_on_selector_all("#agent option", "os => os.map(o => o.value)")
    assert "" in opts and "claude · vpn" in opts and "claude · terminal" in opts
    page.select_option("#agent", "claude · vpn")
    page.wait_for_timeout(100)
    assert visible_ids(page) == ["vpn-cert"]
    # counts follow the agent facet
    assert page.locator('.status-filter [data-status="all"] .n').inner_text() == "1"


def test_sort(page):
    # default: recently updated first
    assert page.input_value("#sort") == "updated"
    assert group_ids(page, "active") == ["pipeline-quality", "svetlota-fonts"]
    page.select_option("#sort", "title")
    page.wait_for_timeout(100)
    assert group_ids(page, "active") == ["pipeline-quality", "svetlota-fonts"]  # I < S
    assert group_ids(page, "blocked") == ["gmail-sync", "vpn-cert"]           # G < R
    page.select_option("#sort", "created")
    page.wait_for_timeout(100)
    assert group_ids(page, "blocked") == ["vpn-cert", "gmail-sync"]  # 2d ago newer than 3d


def test_row_shows_relative_time_with_exact_hover(page):
    row = page.locator('.task[data-id="pipeline-quality"]')
    t = row.locator(".task-row time.updated")
    assert t.inner_text().strip() in ("5m ago", "4m ago", "6m ago")
    assert t.get_attribute("datetime")
    assert t.get_attribute("title")  # exact local time on hover
    assert row.locator(".progress").get_attribute("aria-valuenow") == "50"
    assert "2/4" in row.inner_text()


def test_session_id_shown_next_to_agent(page):
    """Owner, 2026-09-25: the agent's 8-hex session id (the start of the Claude
    session uuid, the same code the dashboard shows) sits next to the agent name."""
    row = page.locator('.task[data-id="pipeline-quality"]')
    sess = row.locator(".task-row .t-sess")
    assert sess.inner_text().strip() == "5e1f00ab"
    assert "session" in (sess.get_attribute("title") or "").lower()
    # a task recorded before this change has no id: nothing, not "undefined"
    assert page.locator('.task[data-id="svetlota-fonts"] .t-sess').count() == 0
    row.locator(".task-row").click()
    det = row.locator(".task-detail")
    assert "5e1f00ab" in det.inner_text()
    assert "session" in det.inner_text().lower()


def test_expand_shows_steps_times_and_duration(page):
    row = page.locator('.task[data-id="pipeline-quality"]')
    btn = row.locator(".task-row")
    assert btn.get_attribute("aria-expanded") == "false"
    btn.click()
    assert btn.get_attribute("aria-expanded") == "true"
    det = row.locator(".task-detail")
    assert det.is_visible()
    steps = det.locator(".step")
    assert steps.count() == 4
    assert [steps.nth(i).get_attribute("data-status") for i in range(4)] == ["done", "done", "active", "todo"]
    assert "10 red" in det.locator(".step-note").first.inner_text()
    assert steps.nth(0).locator("time").get_attribute("title")
    # step title gets real width (regression: stray grid-area squeezed it to one letter per line)
    assert steps.nth(0).locator(".step-title").bounding_box()["width"] > 300
    assert steps.nth(0).bounding_box()["height"] < 60
    txt = det.inner_text().lower()  # labels are CSS-uppercased
    assert "created" in txt and "updated" in txt and "open for" in txt
    # done task shows duration (created -> last update = 90 min)
    page.click('section.group[data-group="done"] .group-head')
    d = page.locator('.task[data-id="done-000"]')
    assert "1h 30m" in d.locator(".task-row").inner_text()


def test_sse_update_keeps_state(page, board):
    page.locator('.task[data-id="pipeline-quality"] .task-row').click()
    page.click('section.group[data-group="done"] .group-head')
    page.select_option("#sort", "title")
    page.set_viewport_size({"width": 1280, "height": 500})
    page.evaluate("window.scrollTo(0, 400)")
    page.wait_for_timeout(100)
    y0 = page.evaluate("window.scrollY")
    assert y0 > 300

    st = make_state(datetime.now(timezone.utc))
    st["tasks"][0]["items"].append({"title": "Fresh step from SSE", "status": "todo", "note": "",
                                     "updated": iso(datetime.now(timezone.utc))})
    st["tasks"][1]["title"] = "Svetlota font loading v2"
    board.write(st)
    page.wait_for_function("document.body.innerText.includes('Fresh step from SSE')", timeout=8000)

    row = page.locator('.task[data-id="pipeline-quality"]')
    assert row.locator(".task-row").get_attribute("aria-expanded") == "true"
    assert row.locator(".step").count() == 5
    assert page.locator('section.group[data-group="done"] .group-head').get_attribute("aria-expanded") == "true"
    assert page.input_value("#sort") == "title"
    assert abs(page.evaluate("window.scrollY") - y0) < 5
    assert "v2" in page.locator('.task[data-id="svetlota-fonts"]').inner_text()


def test_sse_update_keeps_search_and_focus(page, board):
    page.fill("#q", "svetlota")
    page.wait_for_timeout(100)
    st = make_state(datetime.now(timezone.utc))
    st["tasks"][1]["title"] = "Svetlota renamed live"
    board.write(st)
    page.wait_for_function("document.body.innerText.includes('renamed live')", timeout=8000)
    assert page.input_value("#q") == "svetlota"
    assert page.evaluate("document.activeElement.id") == "q"
    assert visible_ids(page) == ["svetlota-fonts"]


def test_desktop_toolbar_is_one_line(page):
    js = ("els => els.filter(e => e.getClientRects().length)"   # hidden ones take no room
          ".map(e => Math.round(e.getBoundingClientRect().top))")
    ys = page.eval_on_selector_all(".bar2 > *", js)
    assert len(ys) >= 4 and len(set(ys)) == 1, ys
    # with the terminal chip shown (?session=) it still fits one line at 1280
    page.goto(page.url.split("?")[0] + "?session=5e1f00ab")
    page.wait_for_selector("#sess-chip:not([hidden])")
    ys = page.eval_on_selector_all(".bar2 > *", js)
    assert len(set(ys)) == 1, ys


def test_mobile_400_no_horizontal_scroll(browser, board):
    ctx = browser.new_context(viewport={"width": 400, "height": 860}, color_scheme="light")
    pg = ctx.new_page()
    pg.goto(board.url)
    pg.wait_for_selector(".task")
    pg.locator('.task[data-id="pipeline-quality"] .task-row').click()
    pg.click('section.group[data-group="done"] .group-head')
    pg.wait_for_timeout(150)
    assert pg.locator('.task[data-id="pipeline-quality"] .step-title').first.bounding_box()["width"] > 200
    sw, cw = pg.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw, (sw, cw)
    ctx.close()


def test_dark_mode_follows_system(browser, board):
    bgs = {}
    for scheme in ("light", "dark"):
        ctx = browser.new_context(viewport={"width": 900, "height": 600}, color_scheme=scheme)
        pg = ctx.new_page()
        pg.goto(board.url)
        pg.wait_for_selector(".task")
        bgs[scheme] = pg.evaluate("getComputedStyle(document.body).backgroundColor")
        ctx.close()
    assert bgs["light"] != bgs["dark"]


def test_real_state_renders_fast(browser, tmp_path):
    if not REAL_STATE.exists():
        pytest.skip("no real state.json")
    sp = tmp_path / "state.json"
    shutil.copy(REAL_STATE, sp)
    b = Board(sp)
    try:
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        pg = ctx.new_page()
        pg.goto(b.url)
        pg.wait_for_selector(".task")
        ms = pg.evaluate("""() => { const t = performance.now();
            window.__board.render(window.__board.state); return performance.now() - t; }""")
        assert ms < 150, ms
        t0 = time.time()
        pg.fill("#q", "deploy")
        pg.wait_for_timeout(50)
        assert time.time() - t0 < 1.0
        assert pg.locator(".task").count() > 0
        ctx.close()
    finally:
        b.close()


def test_screenshots(browser, tmp_path):
    """Desktop + mobile, light + dark, on the REAL board (fallback: fixture)."""
    ART.mkdir(exist_ok=True)
    sp = tmp_path / "state.json"
    if REAL_STATE.exists():
        shutil.copy(REAL_STATE, sp)
    else:
        sp.write_text(json.dumps(make_state(datetime.now(timezone.utc))))
    b = Board(sp)
    try:
        for scheme in ("light", "dark"):
            for name, vp in (("desktop", {"width": 1366, "height": 900}), ("mobile", {"width": 400, "height": 860})):
                ctx = browser.new_context(viewport=vp, color_scheme=scheme, device_scale_factor=1)
                pg = ctx.new_page()
                pg.goto(b.url)
                pg.wait_for_selector(".task")
                pg.locator(".task .task-row").first.click()
                pg.wait_for_timeout(250)
                pg.screenshot(path=str(ART / f"tasks-next-{name}-{scheme}.png"), full_page=False)
                if name == "desktop" and scheme == "light":
                    pg.fill("#q", "deploy")
                    pg.wait_for_timeout(200)
                    pg.screenshot(path=str(ART / "tasks-next-desktop-search.png"))
                ctx.close()
    finally:
        b.close()
    for f in ("desktop-light", "desktop-dark", "mobile-light", "mobile-dark"):
        assert (ART / f"tasks-next-{f}.png").stat().st_size > 5000


def test_t_mark_logo_and_favicon(page):
    """Owner: the notebook/clipboard icon is replaced by a bold "T" mark in the header and the tab."""
    href = page.get_attribute('link[rel="icon"]', "href")
    assert href and href.startswith("data:image/svg+xml")
    from urllib.parse import unquote
    assert ">T<" in unquote(href)
    logo = page.locator("header .logo")
    assert logo.is_visible()
    assert logo.inner_text().strip() == "T" or ">T<" in logo.inner_html()
    html = page.content()
    assert "📋" not in html and "📓" not in html


# ---- ?session=<8 hex>: the dashboard opens the board filtered to one terminal
def _goto(browser, board, query):
    board.write(make_state(datetime.now(timezone.utc)))
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(board.url + query)
    pg.wait_for_selector("#empty, .task", state="attached")
    pg.wait_for_timeout(200)
    return ctx, pg, errors


def test_session_param_filters_to_that_terminal_with_a_removable_chip(browser, board):
    ctx, pg, errors = _goto(browser, board, "?session=5e1f00ab")
    try:
        assert set(visible_ids(pg)) == {"pipeline-quality"}
        chip = pg.locator("#sess-chip")
        assert chip.is_visible()
        assert "5e1f00ab" in chip.inner_text()
        assert pg.locator('.status-filter [data-status="all"] .n').inner_text() == "1"
        chip.locator("button").click()
        pg.wait_for_timeout(150)
        assert not chip.is_visible()
        assert len(visible_ids(pg)) > 1
        assert "session=" not in pg.url
        assert not errors, errors
    finally:
        ctx.close()


def test_session_param_with_no_tasks_says_so(browser, board):
    ctx, pg, errors = _goto(browser, board, "?session=deadbeef")
    try:
        assert visible_ids(pg) == []
        assert "deadbeef" in pg.inner_text("#empty")
        assert not errors, errors
    finally:
        ctx.close()


def test_session_param_ignores_garbage(browser, board):
    ctx, pg, errors = _goto(browser, board, "?session=<b>x</b>")
    try:
        assert not pg.locator("#sess-chip").is_visible()
        assert len(visible_ids(pg)) > 1
    finally:
        ctx.close()


# ---- inside the dashboard (an iframe) the board wears the dashboard's colours
DECK_BG = "rgb(9, 9, 11)"          # dashboard body #09090b
DECK_TEXT = "rgb(228, 228, 231)"   # dashboard text #e4e4e7


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_embedded_board_uses_the_dashboard_palette(browser, board, scheme):
    board.write(make_state(datetime.now(timezone.utc)))
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
    pg = ctx.new_page()
    try:
        pg.set_content(f'<iframe id="f" src="{board.url}" style="width:1200px;height:800px"></iframe>')
        fr = pg.frame_locator("#f")
        fr.locator(".task").first.wait_for()
        frame = pg.frames[1]
        css = lambda sel, prop: frame.eval_on_selector(sel, f"e => getComputedStyle(e).{prop}")  # noqa: E731
        assert css("body", "backgroundColor") == DECK_BG
        assert css("body", "color") == DECK_TEXT
    finally:
        ctx.close()


def test_standalone_board_keeps_its_own_theme(page):
    assert page.eval_on_selector("body", "e => getComputedStyle(e).backgroundColor") != DECK_BG


def test_embedded_board_text_matches_the_dashboard_size(browser, board):
    """Owner: the board's type looked big next to the dashboard (11-12 px). Inside
    the dashboard the board renders ~15% smaller; a task title ends up <= 12 px
    tall per line-box font size as seen on screen."""
    board.write(make_state(datetime.now(timezone.utc)))
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    try:
        pg.goto(board.url)
        pg.wait_for_selector(".task")
        alone = pg.eval_on_selector(".t-title", "e => e.getBoundingClientRect().height")
        pg.set_content(f'<iframe id="f" src="{board.url}" style="width:1200px;height:800px"></iframe>')
        pg.frame_locator("#f").locator(".task").first.wait_for()
        fr = pg.frames[1]
        emb = fr.eval_on_selector(".t-title", "e => e.getBoundingClientRect().height")
        # measured from the parent too (what the eye sees)
        box = pg.frame_locator("#f").locator(".t-title").first.bounding_box()
        assert alone * 0.88 <= box["height"] <= alone * 0.95, (box, alone, emb)
        pg.screenshot(path=str(ART / "tasks-embedded-small.png"))
    finally:
        ctx.close()


# ---- light dashboard theme: the embedded board follows localStorage agentdeck-theme
def _lum(s):
    import re as _re
    r, g, b = [float(x) for x in _re.findall(r"[\d.]+", s)[:3]]
    f = lambda c: (c / 255) / 12.92 if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _embedded(browser, board, theme, scheme="dark"):
    """A same-origin parent (like the dashboard) holding the board in an iframe."""
    board.write(make_state(datetime.now(timezone.utc)))
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
    pg = ctx.new_page()
    pg.goto(board.url)
    pg.wait_for_selector(".task")
    if theme is None:
        pg.evaluate("localStorage.removeItem('agentdeck-theme')")
    else:
        pg.evaluate("t => localStorage.setItem('agentdeck-theme', t)", theme)
    pg.set_content(f'<iframe id="f" src="{board.url}" style="width:1200px;height:800px"></iframe>')
    pg.frame_locator("#f").locator(".task").first.wait_for()
    return ctx, pg, pg.frames[1]


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_embedded_board_light_deck_palette(browser, board, scheme):
    ctx, pg, fr = _embedded(browser, board, "light", scheme)
    try:
        assert fr.evaluate("document.documentElement.dataset.theme") == "deck-light"
        bg = fr.evaluate("getComputedStyle(document.body).backgroundColor")
        assert bg == "rgb(241, 241, 243)", bg                  # = the dashboard's light main area
        assert _lum(fr.evaluate("getComputedStyle(document.body).color")) < 0.05
        # same size tweak as the dark deck
        assert fr.evaluate("getComputedStyle(document.documentElement).zoom") == "0.92"
        pg.screenshot(path=str(ART / "tasks-embedded-light.png"))
    finally:
        ctx.close()


def test_embedded_board_defaults_to_dark_deck(browser, board):
    ctx, pg, fr = _embedded(browser, board, None, "light")
    try:
        assert fr.evaluate("document.documentElement.dataset.theme") == "deck"
        assert fr.evaluate("getComputedStyle(document.body).backgroundColor") == DECK_BG
    finally:
        ctx.close()


def test_embedded_board_switches_live(browser, board):
    ctx, pg, fr = _embedded(browser, board, "dark")
    try:
        assert fr.evaluate("getComputedStyle(document.body).backgroundColor") == DECK_BG
        pg.evaluate("localStorage.setItem('agentdeck-theme', 'light')")   # what the dashboard does
        fr.wait_for_function("document.documentElement.dataset.theme === 'deck-light'", timeout=3000)
        assert fr.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(241, 241, 243)"
        pg.evaluate("localStorage.setItem('agentdeck-theme', 'dark')")
        fr.wait_for_function("document.documentElement.dataset.theme === 'deck'", timeout=3000)
    finally:
        ctx.close()


def test_standalone_board_ignores_the_dashboard_theme(browser, board):
    ctx = browser.new_context(viewport={"width": 900, "height": 600}, color_scheme="dark")
    pg = ctx.new_page()
    try:
        pg.goto(board.url)
        pg.evaluate("localStorage.setItem('agentdeck-theme', 'light')")
        pg.reload()
        pg.wait_for_selector(".task")
        assert pg.evaluate("document.documentElement.dataset.theme") in (None, "", "undefined") \
            or pg.evaluate("document.documentElement.dataset.theme") is None
        assert _lum(pg.evaluate("getComputedStyle(document.body).backgroundColor")) < 0.1
    finally:
        ctx.close()


def test_live_board_copy_matches_next():
    assert (TASKS_DIR / "static" / "index.html").read_text() == NEXT.read_text()
