"""Phone selection end to end: the dashboard page, a real ttyd and a real tmux.

Claude's fullscreen renderer turns on the alternate screen and any-event mouse
tracking (modes 1049, 1003, 1006), so tmux hands every mouse event to it. The app
here does the same and appends every byte it reads to a log. On an emulated phone
(Chromium, Pixel 7):
- a long press then a drag selects several lines in xterm.js itself, copies them,
  and sends the app nothing;
- a tap reaches the app as exactly one press and one release (not a double click);
- a swipe still scrolls (wheel reports, no press).
The same long press runs again with navigator.platform = MacIntel (iPadOS "desktop
site"), where xterm.js forces a selection with Alt instead of Shift: a drag selects
and a long press lifted without a drag types no cursor keys into the app.

ttyd is started with -W like production: ttyd 1.7 (the default install) is read-only
without it. Each "the app got nothing" check is followed by a tap that must reach the
app, so a read-only terminal fails the test instead of passing it.

The dashboard (web/index-lib.html, API faked by page.route) is served on ttyd's own
origin, so the terminal iframe at /sess/ is same-origin, as behind the real nginx.

Isolation: a private tmux server (`tmux -L`, its own short TMUX_TMPDIR) started with
the repo tmux.conf, ttyd on a free port on 127.0.0.1; both stopped by name and saved
PID. The live tmux server is never touched. Skipped when ttyd or tmux is missing.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.test_index_lib import FakeAPI, _free_port, row

pytestmark = pytest.mark.skipif(not (shutil.which("ttyd") and shutil.which("tmux")),
                                reason="needs ttyd and tmux")

REPO = Path(__file__).resolve().parent.parent
WEB_DIR = REPO / "web"
CONF = REPO / "tmux.conf"

APP = r'''
import os, select, signal, sys, tty
log = open(sys.argv[1], 'ab', buffering=0)
fd = sys.stdin.fileno()
tty.setraw(fd)
def draw(*_):
    rows = os.get_terminal_size(fd).lines
    os.write(1, ('\x1b[2J\x1b[H' + '\r\n'.join(
        'line %02d select me' % i for i in range(rows - 1))).encode())
os.write(1, b'\x1b[?1049h\x1b[?1003h\x1b[?1006h')
draw()
signal.signal(signal.SIGWINCH, draw)
while True:
    if select.select([fd], [], [], 1)[0]:
        b = os.read(fd, 4096)
        if not b:
            break
        log.write(b)
'''

SGR = re.compile(rb"\x1b\[<(\d+);\d+;\d+([Mm])")


def _wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"ttyd did not come up on {port}")


@pytest.fixture(scope="module")
def ttyd_server():
    tmp = tempfile.mkdtemp(prefix="adx.", dir="/tmp")        # short: socket path limit
    env = dict(os.environ, TMUX_TMPDIR=tmp)
    env.pop("TMUX", None)
    name = f"adx-touch-{os.getpid()}"
    app, log = os.path.join(tmp, "app.py"), os.path.join(tmp, "app.log")
    Path(app).write_text(APP)
    Path(log).write_bytes(b"")
    tmux = ["tmux", "-L", name, "-f", str(CONF)]
    ttyd = None
    try:
        subprocess.run(tmux + ["new-session", "-d", "-s", "t", "-x", "80", "-y", "30",
                               f"python3 {app} {log}"], env=env, check=True, timeout=10)
        port = _free_port()
        ttyd = subprocess.Popen(["ttyd", "-W", "-p", str(port), "-i", "lo", "-b", "/sess",
                                 "tmux", "-L", name, "attach", "-t", "t"],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _wait_port(port)
        yield {"origin": f"http://127.0.0.1:{port}", "log": log}
    finally:
        if ttyd:
            ttyd.terminate()
            try:
                ttyd.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ttyd.kill()
                ttyd.wait(timeout=5)
        subprocess.run(tmux + ["kill-server"], env=env, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(tmp, ignore_errors=True)


IPAD_PLATFORM = "Object.defineProperty(Navigator.prototype, 'platform', {get: () => 'MacIntel'});"


@pytest.fixture(scope="module")
def pw():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            yield p, b
        finally:
            b.close()


@pytest.fixture(scope="module")
def phone(pw, ttyd_server):
    """The dashboard on an emulated Pixel 7 with terminal dddd0004 open."""
    yield from _dashboard(pw, ttyd_server)


@pytest.fixture
def ipad(pw, ttyd_server):
    """The same, with a Mac navigator.platform (iPadOS asking for desktop sites)."""
    yield from _dashboard(pw, ttyd_server, init_script=IPAD_PLATFORM)


def _dashboard(pw, ttyd_server, init_script=None):
    p, b = pw
    origin = ttyd_server["origin"]
    api = FakeAPI()

    def static(route):
        path = route.request.url[len(origin):].split("?", 1)[0].lstrip("/")
        f = WEB_DIR / path
        if path and f.is_file() and f.resolve().parent == WEB_DIR.resolve():
            return route.fulfill(status=200, path=str(f))
        route.fulfill(status=404, body="")

    ctx = b.new_context(**p.devices["Pixel 7"])
    try:
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
        ctx.set_default_timeout(15000)
        if init_script:
            ctx.add_init_script(init_script)
        pg = ctx.new_page()
        # everything but /sess/ (ttyd itself, page and websocket) is the dashboard
        pg.route(re.compile("^" + re.escape(origin) + r"/(?!sess/)"), static)
        pg.route(re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/"), lambda r: r.abort())
        pg.route(re.compile(r"/api/library(/|\?|$)"), api.library)
        pg.route(re.compile(r"/api/terminal-status(\?|$)"),
                 lambda r: r.fulfill(status=200, content_type="application/json",
                                     body=json.dumps({})))
        pg.route(re.compile(r"/api/page-version"), lambda r: r.abort())
        pg.goto(origin + "/index-lib.html")
        pg.wait_for_selector(".card[data-sid]")
        pg.tap(row("dddd0004") + " .proj")
        pg.wait_for_selector("#wrap iframe[src^='/sess/']")
        pg.frame_locator("#wrap iframe").locator(".xterm.enable-mouse-events").wait_for()
        term = next(f for f in pg.frames if "/sess/" in f.url)
        term.wait_for_function("() => { const b = term.buffer.active || term.buffer;"
                               " return /line 05/.test(b.getLine(5).translateToString()); }")
        pg.wait_for_timeout(500)                       # the touch handler is attached
        yield {"page": pg, "term": term, "cdp": ctx.new_cdp_session(pg),
               "log": ttyd_server["log"]}
    finally:
        ctx.close()


def _pos(log):
    return os.path.getsize(log)


def _since(log, pos):
    with open(log, "rb") as f:
        f.seek(pos)
        return f.read()


def _events(data):
    """(button, 'M' press/motion | 'm' release) for each SGR mouse report."""
    return [(int(b), k.decode()) for b, k in SGR.findall(data)]


def _touch(cdp, kind, *pts):
    cdp.send("Input.dispatchTouchEvent", {
        "type": kind, "touchPoints": [{"x": x, "y": y, "id": i} for i, (x, y) in enumerate(pts)]})


def _geometry(ph):
    pg, term = ph["page"], ph["term"]
    fb = pg.query_selector("#wrap iframe").bounding_box()
    sb = term.evaluate("(() => { const r = document.querySelector('.xterm-screen')"
                       ".getBoundingClientRect(); return {x: r.x, y: r.y, h: r.height}; })()")
    ch = sb["h"] / term.evaluate("term.rows")
    return fb["x"] + sb["x"] + 30, fb["y"] + sb["y"] + ch * 3.5, ch


def _tap(ph):
    """Tap; the app must get exactly one press and one release."""
    pg, log = ph["page"], ph["log"]
    x, y, _ = _geometry(ph)
    p = _pos(log)
    _touch(ph["cdp"], "touchStart", (x, y))
    pg.wait_for_timeout(60)
    _touch(ph["cdp"], "touchEnd")
    pg.wait_for_timeout(800)
    ev = _events(_since(log, p))
    presses = [e for e in ev if e == (0, "M")]
    releases = [e for e in ev if e[1] == "m"]
    assert len(presses) == 1 and len(releases) == 1, ev
    pg.wait_for_timeout(700)                           # out of any double-click window


def test_tap_is_one_press_and_one_release(phone):
    _tap(phone)


def _long_press_drag_selects_and_sends_nothing(ph):
    pg, term, log, cdp = ph["page"], ph["term"], ph["log"], ph["cdp"]
    pg.evaluate("navigator.clipboard.writeText('untouched')")
    x, y, ch = _geometry(ph)
    p = _pos(log)
    _touch(cdp, "touchStart", (x, y))
    pg.wait_for_timeout(650)
    for i in range(1, 15):
        _touch(cdp, "touchMove", (x + i * 8, y + i * ch / 2))
        pg.wait_for_timeout(30)
    _touch(cdp, "touchEnd")
    pg.wait_for_timeout(800)
    assert _since(log, p) == b""
    sel = term.evaluate("term.getSelection()")
    assert len(sel.split("\n")) >= 5, sel
    assert "line 04" in sel and "line 09" in sel, sel
    assert pg.evaluate("navigator.clipboard.readText()") == sel
    assert pg.evaluate("!!document.getElementById('copyHint')")
    _tap(ph)                                           # and input does reach the app


def test_long_press_drag_selects_lines_and_sends_the_app_nothing(phone):
    _long_press_drag_selects_and_sends_nothing(phone)


def test_ipad_long_press_drag_selects_lines_and_sends_the_app_nothing(ipad):
    assert ipad["term"].evaluate("navigator.platform") == "MacIntel"
    _long_press_drag_selects_and_sends_nothing(ipad)


def test_ipad_long_press_and_lift_types_no_cursor_keys(ipad):
    """A short Alt click is xterm.js's "move the cursor here": it would type arrow
    keys into Claude (prompt history, input cursor)."""
    pg, log, cdp = ipad["page"], ipad["log"], ipad["cdp"]
    x, y, _ = _geometry(ipad)
    p = _pos(log)
    _touch(cdp, "touchStart", (x, y))
    pg.wait_for_timeout(600)                           # mousedown at 450, mouseup < 500 later
    _touch(cdp, "touchEnd")
    pg.wait_for_timeout(800)
    assert _since(log, p) == b""
    _tap(ipad)


def test_swipe_scrolls_with_wheel_reports_and_no_press(phone):
    pg, log, cdp = phone["page"], phone["log"], phone["cdp"]
    x, y, ch = _geometry(phone)
    y0 = y + ch * 15
    p = _pos(log)
    _touch(cdp, "touchStart", (x, y0))
    for i in range(1, 16):
        _touch(cdp, "touchMove", (x, y0 - i * 20))
        pg.wait_for_timeout(20)
    _touch(cdp, "touchEnd")
    pg.wait_for_timeout(800)
    ev = _events(_since(log, p))
    assert any(b in (64, 65) for b, _ in ev), ev
    assert not [e for e in ev if e[0] in (0, 1, 2)], ev
