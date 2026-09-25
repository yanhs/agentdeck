"""Browser contract for web/server.html — the dashboard's "Server" page.

Runs against a static copy of web/ with GET /api/server faked by page.route():
no collector, no status_server. Screenshots land in tests/artifacts/ (untracked).
"""
from __future__ import annotations

import copy
import http.server
import json
import re
import socket
import threading
from contextlib import closing
from functools import partial
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
PAGE = WEB_DIR / "server.html"
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"


def _free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def m(mid, name, kind, status, cpu, rss, what="", children=(), **kw):
    d = {"id": mid, "name": name, "kind": kind, "status": status, "cpu_pct": cpu,
         "rss_mb": rss, "count": 1 + len(children), "what": what,
         "children": [{"name": n, "cpu_pct": c, "rss_mb": r, "count": 1} for n, c, r in children]}
    d.update(kw)
    return d


def snapshot(cpu=62.0, t=1_790_000_000.0):
    groups = [
        ("agents", "Agents", [
            m("agent:500", "ImmAppeal deploy", "claude", "working", 115.0, 670,
              "Claude Code in ~/pr/Appeals/dev · running pytest",
              [("pytest", 100.0, 200), ("node mcp-server.js", 5.0, 60)], terminal="cs-b36f2fd1"),
            m("agent:601", "Landing page copy", "claude", "idle", 1.0, 300,
              "Claude Code in ~/pr", terminal="claude-terminal-3"),
        ]),
        ("apps", "Sites & apps", [
            m("docker:immappeal-dev", "immappeal-dev", "container", "working", 50.0, 1000,
              "ImmAppeal web + API (dev)", [("next-server", 40.0, 500), ("python3 api.py", 10, 300)],
              detail="Up 2 hours"),
            m("unit:nginx.service", "nginx", "service", "running", 2.0, 25,
              "Web server and reverse proxy for every site", [("nginx", 2.0, 25)]),
            m("unit:vpn-bot.service", "VPN sales bot", "service", "stopped", 0, 0,
              "MakeBlitz VPN Sales Bot"),
        ] + [m(f"unit:svc{i}.service", f"Service {i}", "service", "running", 0.1 * i, 20 + i,
               f"Small service {i}") for i in range(14)]),
        ("jobs", "Background jobs", [
            m("job:1400", "node next build", "job", "working", 200.0, 900,
              "Started from ssh session · in ~/pr/site", owner="ssh session"),
        ]),
        ("system", "System", [
            m("sys:kernel", "Kernel threads", "kernel", "running", 3.0, 0, "Linux kernel workers"),
            m("sys:other", "Other", "system", "running", 2.0, 600,
              "80 small processes: shells, daemons, tmux", [("sshd", 0.5, 10)]),
        ]),
    ]
    gs = []
    for gid, title, ms in groups:
        gs.append({"id": gid, "title": title, "count": len(ms), "members": ms,
                   "cpu_pct": round(sum(x["cpu_pct"] for x in ms), 1),
                   "rss_mb": round(sum(x["rss_mb"] for x in ms), 1)})
    pts = []
    for i in range(180):
        pts.append({"t": t - (179 - i) * 5, "cpu": 40 + (i % 20), "ram": 60,
                    "g": {"agents": 100 + i % 7, "apps": 60, "jobs": 150 if i > 150 else 0,
                          "system": 10}})
    return {
        "ts": t, "interval": 5,
        "host": {"hostname": "demo-box", "cores": 8, "load": [6.5, 5.2, 4.8], "cpu_pct": cpu,
                 "cpu_per_core": [90, 80, 70, 60, 50, 40, 30, 20],
                 "ram_total_mb": 24000, "ram_used_mb": 15000, "ram_available_mb": 9000,
                 "ram_pct": 62.5, "swap_total_mb": 0, "swap_used_mb": 0,
                 "disk_total_gb": 208, "disk_used_gb": 152, "disk_free_gb": 56, "disk_pct": 73.1,
                 "uptime_s": 6735266, "pressure": "busy", "pressure_reason": "CPU at 62%"},
        "groups": gs,
        "history": {"interval": 5, "points": pts},
        "collector": {"sample_ms": 40, "cpu_pct": 0.8, "processes": 400},
    }


class FakeServerAPI:
    def __init__(self):
        self.snap = snapshot()
        self.fail = False
        self.calls = 0

    def handle(self, route):
        self.calls += 1
        if self.fail:
            return route.fulfill(status=502, content_type="text/html", body="bad gateway")
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(self.snap, ensure_ascii=False))


@pytest.fixture(scope="module")
def site():
    port = _free_port()
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB_DIR))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.RequestHandlerClass.log_message = lambda *a, **k: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/server.html"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _open(browser, site, api, width=1280, height=900, mobile=False, query="?poll=300"):
    ctx = browser.new_context(viewport={"width": width, "height": height},
                              is_mobile=mobile, has_touch=mobile)
    ctx.set_default_timeout(10000)
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    # (fonts are aborted and the 502 is on purpose: resource-load noise is not a page error)
    pg.on("console", lambda msg: pg.errors.append(msg.text) if msg.type == "error"
          and "Failed to load resource" not in msg.text else None)
    pg.route(re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/"), lambda r: r.abort())
    pg.route(re.compile(r"/api/server(\?|$)"), api.handle)
    pg.goto(site + query)
    pg.wait_for_selector(".row[data-id]")
    return ctx, pg


@pytest.fixture
def api():
    return FakeServerAPI()


@pytest.fixture
def page(browser, site, api):
    ctx, pg = _open(browser, site, api)
    yield pg
    assert pg.errors == []
    ctx.close()


def shot(pg, name):
    ARTIFACTS.mkdir(exist_ok=True)
    pg.screenshot(path=str(ARTIFACTS / name), full_page=True)


def test_page_exists_and_is_self_contained():
    html = PAGE.read_text(encoding="utf-8")
    assert "<title>" in html
    for src in re.findall(r'<script[^>]+src="([^"]+)"', html):
        assert src.startswith("https://cdn.jsdelivr.net/"), src
    assert "/api/server" in html
    assert "innerHTML" not in html          # every value goes in as text


def test_header_gauges(page):
    shot(page, "server-desktop.png")
    assert page.inner_text("#g-cpu .gv").strip() == "62%"
    assert page.inner_text("#g-ram .gv").strip() == "63%"
    assert page.inner_text("#g-disk .gv").strip() == "73%"
    load = page.inner_text("#g-load")
    assert "6.5" in load and "8" in load
    assert page.inner_text("#host").strip() == "demo-box"
    pill = page.inner_text("#pressure").lower()
    assert "busy" in pill and "cpu at 62%" in pill
    assert len(page.query_selector_all("#g-cpu .core")) == 8


def test_history_chart_has_areas_and_ram_line(page):
    paths = page.eval_on_selector_all("#chart path.area", "els => els.map(e => e.getAttribute('d'))")
    assert len(paths) == 4 and all(p and len(p) > 50 for p in paths)
    assert page.get_attribute("#chart path.ram", "d")


def test_cpu_now_stacked_bar(page):
    segs = page.eval_on_selector_all(
        "#now .seg", "els => els.map(e => [e.dataset.group, parseFloat(e.style.width)])")
    assert segs, "no segments"
    assert {g for g, _ in segs} >= {"agents", "apps", "jobs"}
    assert sum(w for _, w in segs) <= 100.01
    # the machine is 8 cores: 200 % of one core = 25 % of the machine
    job = [w for g, w in segs if g == "jobs"]
    assert sum(job) == pytest.approx(25, abs=0.2)


def test_groups_in_order_with_rows(page):
    titles = page.eval_on_selector_all("section.group h2 .gt", "els => els.map(e => e.textContent)")
    assert titles == ["Agents", "Sites & apps", "Background jobs", "System"]
    a = page.query_selector('.row[data-id="agent:500"]')
    assert "ImmAppeal deploy" in a.inner_text()
    assert "running pytest" in a.inner_text()
    assert "working" in a.query_selector(".dot").get_attribute("class")
    idle = page.query_selector('.row[data-id="agent:601"] .dot').get_attribute("class")
    assert "idle" in idle
    stopped = page.query_selector('.row[data-id="unit:vpn-bot.service"]')
    assert "stopped" in stopped.get_attribute("class")
    # CPU shown as a share of the whole machine: 115 % of one core / 8 cores
    assert page.inner_text('.row[data-id="agent:500"] .cpu .v').strip() == "14%"
    assert page.inner_text('.row[data-id="agent:500"] .mem .v').strip() == "670 MB"
    assert page.inner_text('.row[data-id="docker:immappeal-dev"] .mem .v').strip() == "1.0 GB"


def test_long_groups_are_trimmed_with_show_more(page):
    apps = page.query_selector('section.group[data-group="apps"]')
    visible = [r for r in apps.query_selector_all(".row") if r.is_visible()]
    assert len(visible) <= 12
    btn = apps.query_selector(".more")
    assert btn and btn.is_visible() and re.search(r"\d+ more", btn.inner_text())
    btn.click()
    visible = [r for r in apps.query_selector_all(".row") if r.is_visible()]
    assert len(visible) == 17


def test_row_expands_to_child_processes(page):
    row = '.row[data-id="agent:500"]'
    assert not page.is_visible(row + " .kids")
    page.click(row + " .main")
    page.wait_for_selector(row + " .kids", state="visible")
    kids = page.inner_text(row + " .kids")
    assert "pytest" in kids and "node mcp-server.js" in kids
    page.click(row + " .main")
    page.wait_for_selector(row + " .kids", state="hidden")


def test_live_refresh_updates_in_place(browser, site, api):
    ctx, pg = _open(browser, site, api)
    try:
        pg.click('.row[data-id="agent:500"] .main')
        pg.evaluate("() => { document.querySelector('.row[data-id=\"agent:500\"]').__keep = 1; }")
        snap = copy.deepcopy(api.snap)
        snap["host"]["cpu_pct"] = 91.0
        snap["groups"][0]["members"][0]["cpu_pct"] = 400.0
        api.snap = snap
        pg.wait_for_function("() => document.querySelector('#g-cpu .gv').textContent.trim() === '91%'")
        pg.wait_for_function("() => document.querySelector('.row[data-id=\"agent:500\"] .cpu .v')"
                             ".textContent.trim() === '50%'")
        assert pg.evaluate("() => document.querySelector('.row[data-id=\"agent:500\"]').__keep") == 1
        assert pg.is_visible('.row[data-id="agent:500"] .kids')        # still expanded
        # a member that went away is removed, a new one appears
        snap = copy.deepcopy(snap)
        snap["groups"][2]["members"] = [m("job:77", "python3 burn.py", "job", "working", 80, 50,
                                          "Started from cmd-shell", owner="cmd-shell")]
        api.snap = snap
        pg.wait_for_selector('.row[data-id="job:77"]')
        pg.wait_for_selector('.row[data-id="job:1400"]', state="detached")
        assert pg.errors == []
    finally:
        ctx.close()


def test_api_failure_shows_offline_and_keeps_data(browser, site, api):
    ctx, pg = _open(browser, site, api)
    try:
        api.fail = True
        pg.wait_for_selector("#stale", state="visible")
        assert "offline" in pg.inner_text("#stale").lower() or "retry" in pg.inner_text("#stale").lower()
        assert pg.query_selector('.row[data-id="agent:500"]') is not None
        api.fail = False
        pg.wait_for_selector("#stale", state="hidden")
    finally:
        ctx.close()


def test_names_are_text_not_html(browser, site, api):
    snap = copy.deepcopy(api.snap)
    snap["groups"][0]["members"][0]["name"] = '<img src=x onerror="window.__pwn=1">'
    snap["groups"][0]["members"][0]["what"] = "<b>bold</b>"
    api.snap = snap
    ctx, pg = _open(browser, site, api)
    try:
        pg.wait_for_timeout(300)
        assert pg.evaluate("() => window.__pwn") is None
        assert pg.query_selector('.row[data-id="agent:500"] img') is None
        assert "<b>bold</b>" in pg.inner_text('.row[data-id="agent:500"]')
    finally:
        ctx.close()


def test_phone_390_has_no_sideways_scroll(browser, site, api):
    ctx, pg = _open(browser, site, api, width=390, height=844, mobile=True)
    try:
        pg.click('.row[data-id="agent:500"] .main')
        pg.click('section.group[data-group="apps"] .more')
        shot(pg, "server-phone.png")
        over = pg.evaluate("() => document.documentElement.scrollWidth - innerWidth")
        assert over <= 0, f"page scrolls sideways by {over}px"
        bad = pg.evaluate("""() => [...document.querySelectorAll('.row, .gauge, #chart, #now, .kids')]
            .filter(e => { const r = e.getBoundingClientRect();
                           return r.width > 0 && (r.left < -0.5 || r.right > innerWidth + 0.5); })
            .map(e => e.className || e.id)""")
        assert bad == []
        assert pg.errors == []
    finally:
        ctx.close()


def test_english_only(page):
    text = page.inner_text("body")
    assert not re.search(r"[А-Яа-яЁё]", text)


def test_standalone_has_a_way_back_but_not_inside_the_dashboard(browser, site, api):
    ctx, pg = _open(browser, site, api)
    try:
        assert pg.is_visible("#back")
        assert pg.get_attribute("#back", "href") == "/"
        pg.set_content(f'<iframe src="{site}?poll=300" style="width:1000px;height:700px"></iframe>')
        fr = pg.frame_locator("iframe")
        fr.locator(".row[data-id]").first.wait_for()
        assert not fr.locator("#back").is_visible()
    finally:
        ctx.close()


def test_login_redirect_shows_a_login_link(browser, site):
    """Behind nginx an expired session turns /api/server into the login page (HTML)."""
    class LoginAPI(FakeServerAPI):
        def handle(self, route):
            route.fulfill(status=200, content_type="text/html", body="<form>login</form>")
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.route(re.compile(r"/api/server(\?|$)"), LoginAPI().handle)
    try:
        pg.goto(site + "?poll=300")
        pg.wait_for_selector("#stale", state="visible")
        assert pg.get_attribute("#stale a", "href") == "/login"
    finally:
        ctx.close()


def test_cpu_no_process_holds_is_shown_as_short_lived(page):
    """Host CPU 62 % but the members only add up to less: the gap (processes that
    lived between two samples, interrupts) gets its own striped segment."""
    segs = page.eval_on_selector_all(
        "#now .seg", "els => els.map(e => [e.dataset.group, parseFloat(e.style.width)])")
    tracked = sum(w for g, w in segs if g != "untracked")
    short = [w for g, w in segs if g == "untracked"]
    assert len(short) == 1
    assert tracked + short[0] == pytest.approx(62, abs=0.3)
    assert "Short-lived" in page.inner_text("#nowLeg")


# ── Dark / Light: follows the dashboard's localStorage 'agentdeck-theme' ─────
def _lum(s):
    r, g, b = [float(x) for x in re.findall(r"[\d.]+", s)[:3]]
    f = lambda c: (c / 255) / 12.92 if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _themed(browser, site, api, theme, **kw):
    ctx, pg = _open(browser, site, api, **kw)
    if theme is not None:
        pg.evaluate("t => localStorage.setItem('agentdeck-theme', t)", theme)
        pg.reload()
        pg.wait_for_selector(".row[data-id]")
    return ctx, pg


def test_dark_is_the_default_and_unchanged(browser, site, api):
    ctx, pg = _themed(browser, site, api, None)
    try:
        css = lambda s, p: pg.eval_on_selector(s, f"e => getComputedStyle(e).{p}")  # noqa: E731
        assert css("body", "backgroundColor") == "rgb(9, 9, 11)"
        assert css("body", "color") == "rgb(228, 228, 231)"
        assert css(".gauge", "backgroundColor") == "rgb(17, 17, 20)"
        # chart areas keep the dark group colours
        fills = pg.eval_on_selector_all("#chart path.area", "els => els.map(e => getComputedStyle(e).fill)")
        assert "rgb(129, 140, 248)" in fills and "rgb(45, 212, 191)" in fills
        shot(pg, "theme-dark-server.png")
        assert pg.errors == []
    finally:
        ctx.close()


def test_light_theme(browser, site, api):
    ctx, pg = _themed(browser, site, api, "light")
    try:
        css = lambda s, p: pg.eval_on_selector(s, f"e => getComputedStyle(e).{p}")  # noqa: E731
        assert pg.evaluate("document.documentElement.dataset.theme") == "light"
        bg = css("body", "backgroundColor")
        assert _lum(bg) > 0.85, bg
        assert _lum(css("body", "color")) < 0.05
        card = css(".gauge", "backgroundColor")
        assert _lum(card) > 0.85
        for s in (".gl", ".gs", ".what", "h3", ".m .v", "#updated"):
            assert _contrast(css(s, "color"), card) >= 4.5, s    # WCAG AA
        # the ring track and the bars are visible on white, not dark zinc
        trk = pg.eval_on_selector(".ring .trk", "e => getComputedStyle(e).stroke")
        assert _lum(trk) > 0.6, trk
        fills = pg.eval_on_selector_all("#chart path.area", "els => els.map(e => getComputedStyle(e).fill)")
        assert len(set(fills)) == 4 and all(f not in ("none", "") for f in fills), fills
        grid = pg.eval_on_selector("#chart .grid", "e => getComputedStyle(e).stroke") \
            if pg.query_selector("#chart .grid") else None
        if grid:
            assert _lum(grid) > 0.5, grid
        shot(pg, "theme-light-server.png")
        assert pg.errors == []
    finally:
        ctx.close()


def test_light_theme_phone_no_sideways_scroll(browser, site, api):
    ctx, pg = _themed(browser, site, api, "light", width=390, height=844, mobile=True)
    try:
        over = pg.evaluate("() => document.documentElement.scrollWidth - innerWidth")
        assert over <= 0
        shot(pg, "theme-light-server-mobile.png")
    finally:
        ctx.close()


def test_theme_switches_live_from_another_page(browser, site, api):
    ctx, pg = _themed(browser, site, api, "dark")
    try:
        other = ctx.new_page()
        other.goto(site.replace("server.html", "no-such-page"))      # any same-origin page
        other.evaluate("localStorage.setItem('agentdeck-theme', 'light')")
        pg.wait_for_function("document.documentElement.dataset.theme === 'light'", timeout=3000)
        assert _lum(pg.evaluate("getComputedStyle(document.body).backgroundColor")) > 0.85
        other.evaluate("localStorage.setItem('agentdeck-theme', 'dark')")
        pg.wait_for_function("getComputedStyle(document.body).backgroundColor === 'rgb(9, 9, 11)'",
                             timeout=3000)
    finally:
        ctx.close()
