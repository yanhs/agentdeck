#!/usr/bin/env python3
"""Render the README demo animation (docs/demo.gif + docs/demo.mp4) from STAGED data.

Reuses tests/make_screenshots.py: web/ served locally, /api/* answered with English
demo data, mock Claude Code terminals, the REAL task board on a temp state file, the
Server payload and the Telegram mock. Nothing live is recorded.

    python3 tests/make_demo.py              # frames -> docs/demo.gif + docs/demo.mp4
    python3 tests/make_demo.py --keep       # also keep the frame PNGs (path printed)

Each frame is a 2x screenshot; ffmpeg assembles them (concat with per-frame
durations), scales to OUT_W with lanczos and builds the GIF with palettegen/paletteuse.
Every page is checked for Cyrillic; any hit fails the run.
"""
from __future__ import annotations

import base64
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_screenshots as ms  # noqa: E402

ROOT = ms.ROOT
DOCS = ROOT / "docs"
W, H = 1200, 720           # CSS px of every frame (captured at 2x)
OUT_W = 1200               # final width of the GIF / MP4
FPS = 13
STEP = 1 / FPS             # duration of one animated frame

# ─────────────────────────────────────────────────────────── overlay ──
# Caption pill + a fake mouse cursor / tap ring, in the dashboard's dark palette.
OVERLAY_CSS = """
#demo-cap{position:fixed;left:22px;bottom:22px;z-index:2147483646;display:flex;align-items:center;gap:11px;
  padding:11px 18px 11px 15px;border-radius:12px;background:rgba(12,12,15,.92);
  border:1px solid rgba(255,255,255,.12);box-shadow:0 10px 30px rgba(0,0,0,.55);
  font:600 19px/1.2 'Inter',-apple-system,system-ui,sans-serif;color:#fafafa;letter-spacing:-.01em;
  pointer-events:none;transition:none}
#demo-cap i{width:9px;height:9px;border-radius:50%;background:#818cf8;box-shadow:0 0 0 4px rgba(129,140,248,.22)}
#demo-cap small{font-weight:500;color:#a1a1aa;font-size:17px}
#demo-cur{position:fixed;z-index:2147483647;width:22px;height:22px;pointer-events:none;left:-50px;top:-50px}
#demo-ring{position:fixed;z-index:2147483645;border-radius:50%;pointer-events:none;
  border:2px solid rgba(129,140,248,.9);background:rgba(129,140,248,.18);display:none}
"""
CURSOR_SVG = ('<svg viewBox="0 0 22 22" width="22" height="22"><path d="M3 2l14 9.5-6.2 1.3 3.6 6.9-2.6 1.3'
              '-3.6-6.9L3 18.6z" fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg>')

OVERLAY_JS = """([css, svg]) => {
  if (document.getElementById('demo-cap')) return;
  const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
  const cap = document.createElement('div'); cap.id = 'demo-cap'; cap.style.display = 'none';
  document.body.appendChild(cap);
  const cur = document.createElement('div'); cur.id = 'demo-cur'; cur.innerHTML = svg;
  document.body.appendChild(cur);
  const ring = document.createElement('div'); ring.id = 'demo-ring'; document.body.appendChild(ring);
}"""


class Film:
    """Collects (png, duration) pairs and turns them into a GIF and an MP4."""

    def __init__(self, workdir: Path):
        self.dir = workdir
        self.frames: list[tuple[Path, float]] = []
        self.stage = None          # the page that composes phone shots

    # ── capture ──
    def shot(self, pg, dur: float) -> None:
        p = self.dir / f"f{len(self.frames):04d}.png"
        pg.screenshot(path=str(p))
        self.frames.append((p, dur))

    def hold(self, dur: float) -> None:
        """Extend the last frame."""
        p, d = self.frames[-1]
        self.frames[-1] = (p, d + dur)

    @property
    def seconds(self) -> float:
        return sum(d for _, d in self.frames)

    # ── assemble ──
    def build(self, gif: Path, mp4: Path) -> None:
        lst = self.dir / "frames.txt"
        lines = []
        for p, d in self.frames:
            lines += [f"file '{p.name}'", f"duration {d:.4f}"]
        lines.append(f"file '{self.frames[-1][0].name}'")     # concat quirk: last file repeated
        lst.write_text("\n".join(lines) + "\n")
        scale = f"scale={OUT_W}:-2:flags=lanczos"
        src = ["-f", "concat", "-safe", "0", "-i", str(lst)]
        run(["ffmpeg", "-y", "-loglevel", "error", *src,
             "-vf", f"fps={FPS},{scale},format=yuv420p",
             "-c:v", "libx264", "-preset", "slow", "-crf", "22", "-tune", "stillimage",
             "-movflags", "+faststart", "-an", str(mp4)])
        # one palette for the whole film (built from the lossless frames, not the MP4);
        # diff_mode=rectangle re-encodes only the part of each frame that changed
        run(["ffmpeg", "-y", "-loglevel", "error", *src, "-lavfi",
             f"fps={FPS},{scale},split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];"
             "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
             "-loop", "0", str(gif)])


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, cwd=None)


# ─────────────────────────────────────────────────────────── helpers ──
def overlay(pg) -> None:
    pg.evaluate(OVERLAY_JS, [OVERLAY_CSS, CURSOR_SVG])


def caption(pg, text: str, sub: str = "") -> None:
    pg.evaluate("""([t, s]) => { const c = document.getElementById('demo-cap');
        c.innerHTML = '<i></i><span></span>' + (s ? '<small></small>' : '');
        c.querySelector('span').textContent = t; if (s) c.querySelector('small').textContent = s;
        c.style.display = 'flex'; }""", [text, sub])


def cursor_to(pg, film: Film, sel: str | tuple[float, float], steps: int = 8, frame=None) -> tuple[float, float]:
    """Glide the fake cursor to the centre-left of `sel` (or an x,y), one frame per step."""
    if isinstance(sel, tuple):
        tx, ty = sel
    else:
        box = (frame or pg).locator(sel).first.bounding_box()
        w = box["width"]
        tx, ty = box["x"] + (w / 2 if w < 80 else min(w * 0.35, 90)), box["y"] + box["height"] / 2
    start = pg.evaluate("() => { const c = document.getElementById('demo-cur');"
                        " return [parseFloat(c.style.left), parseFloat(c.style.top)]; }")
    sx, sy = start if start[0] > -40 else (tx + 160, ty + 120)
    for i in range(1, steps + 1):
        f = 0.5 - 0.5 * math.cos(math.pi * i / steps)          # ease in-out
        x, y = sx + (tx - sx) * f, sy + (ty - sy) * f
        pg.evaluate("([x, y]) => { const c = document.getElementById('demo-cur');"
                    " c.style.left = (x - 3) + 'px'; c.style.top = (y - 2) + 'px'; }", [x, y])
        film.shot(pg, STEP)
    return tx, ty


def ring(pg, film: Film, x: float, y: float, frames: int = 3) -> None:
    """A click/tap ring that grows and fades."""
    for i in range(frames):
        r = 14 + 9 * i
        pg.evaluate("([x, y, r, o]) => { const g = document.getElementById('demo-ring');"
                    " Object.assign(g.style, {display: 'block', left: (x - r) + 'px', top: (y - r) + 'px',"
                    " width: 2 * r + 'px', height: 2 * r + 'px', opacity: o}); }",
                    [x, y, r, 1 - i / frames])
        film.shot(pg, STEP)
    pg.evaluate("() => { document.getElementById('demo-ring').style.display = 'none'; }")


def hide_cursor(pg) -> None:
    pg.evaluate("() => { const c = document.getElementById('demo-cur'); c.style.left = '-50px'; c.style.top = '-50px'; }")


# spinner glyphs Claude Code cycles through
SPIN = ["&#x2736;", "&#x2738;", "&#x2739;", "&#x273A;", "&#x2739;", "&#x2738;"]

NEW_AUTH_LINES = [
    ('<span class="dot">&#9679;</span> <span class="tool">Write</span><span class="arg">(tests/test_sessions_api.py)</span>',
     '  <span class="dim">&#9151;  Wrote 64 lines to tests/test_sessions_api.py</span>'),
    ('<span class="dot">&#9679;</span> <span class="tool">Bash</span><span class="arg">(pytest tests/test_sessions_api.py -q)</span>',
     '  <span class="dim">&#9151;  </span><span class="ok">6 passed</span><span class="dim"> in 0.93s</span>'),
]


def term_spin(fr, i: int, secs: int, tokens: str) -> None:
    fr.evaluate("""([g, s, t]) => { const ls = document.querySelectorAll('body > div:first-child > .l');
        const last = ls[ls.length - 1];
        last.innerHTML = '<span class="or">' + g + ' Wiring up the device list&hellip;</span> ' +
          '<span class="dim">(' + s + 's &middot; &darr; ' + t + ' tokens &middot; esc to interrupt)</span>'; }""",
                [SPIN[i % len(SPIN)], secs, tokens])


def term_add(fr, a: str, b: str) -> None:
    """Insert a tool call (two lines) just above the spinner line, like Claude Code does."""
    fr.evaluate("""([a, b]) => { const box = document.querySelector('body > div:first-child');
        const ls = box.querySelectorAll('.l'); const spin = ls[ls.length - 1], gap = ls[ls.length - 2];
        for (const h of [a, b]) { const d = document.createElement('div'); d.className = 'l'; d.innerHTML = h;
          box.insertBefore(d, gap); } }""", [a, b])


# ─────────────────────────────────────────────────────────── stage ──
STAGE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
html,body{margin:0;width:%(w)dpx;height:%(h)dpx;overflow:hidden;background:#09090b;
  font-family:'Inter',-apple-system,system-ui,sans-serif}
body{background:radial-gradient(900px 520px at 72%% 40%%,#17172a 0%%,#09090b 70%%)}
.dev{position:absolute;top:50%%;transform:translateY(-50%%);border-radius:34px;padding:9px;background:#1c1c21;
  box-shadow:0 0 0 1px rgba(255,255,255,.10),0 30px 70px rgba(0,0,0,.6)}
.dev img{display:block;border-radius:26px}
.side{position:absolute;left:70px;top:0;bottom:0;width:420px;display:flex;flex-direction:column;justify-content:center;
  color:#a1a1aa;font-size:17px;line-height:1.55}
.side b{display:block;color:#fafafa;font-size:30px;line-height:1.2;font-weight:700;letter-spacing:-.02em;margin-bottom:14px}
.side ul{margin:10px 0 0;padding:0 0 0 18px}.side li{margin:3px 0}
.side li::marker{color:#818cf8}
</style></head><body><div class="side" id="side"></div></body></html>"""


def stage_show(stage, images: list[tuple[str, int, int]], side_html: str) -> None:
    """Place phone screenshots (data URL, css w, css h) on the right of the stage."""
    stage.evaluate("""([imgs, side, W]) => {
        document.querySelectorAll('.dev').forEach(n => n.remove());
        document.getElementById('side').innerHTML = side;
        let x = W - 70;
        for (let i = imgs.length - 1; i >= 0; i--) {
          const [src, w, h] = imgs[i];
          const d = document.createElement('div'); d.className = 'dev';
          x -= w + 18; d.style.left = x + 'px';
          const im = document.createElement('img'); im.src = src; im.width = w; im.height = h;
          d.appendChild(im); document.body.appendChild(d); x -= 40;
        } }""", [images, side_html, W])
    stage.wait_for_function("() => [...document.images].every(i => i.complete)")


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


# ─────────────────────────────────────────────────────────── telegram ──
TG_EXTRA_CSS = """
html,body{height:100%;background:#c2dfae}
.phone{height:100vh;display:flex;flex-direction:column}
.chat{flex:1;justify-content:flex-end;overflow:hidden}
.kbtn.hit{background:rgba(85,168,240,.35);color:#0f4f8f}
.bub.in,.bub.out{animation:none}
"""


def tg_in(text: str, t: str, kb: list[str] | None = None) -> str:
    import html as h
    k = ""
    if kb:
        k = '<div class="kb">' + "".join(f'<div class="kbtn">{h.escape(b)}</div>' for b in kb) + "</div>"
    body = h.escape(text).replace("\n", "<br>")
    return (f'<div class="row in"><div class="col"><div class="bub in"><span class="txt">{body}</span>'
            f'<span class="tm">{t}</span></div>{k}</div></div>')


def tg_out(text: str, t: str) -> str:
    import html as h
    return (f'<div class="row out"><div class="bub out"><span class="txt">{h.escape(text)}</span>'
            f'<span class="tm">{t} <b>&#10003;&#10003;</b></span></div></div>')


TG_STEPS = [
    tg_out("/use", "14:02"),
    tg_in("Current: «Auth: refresh tokens» · 3f9a1c07. Pick a terminal:\n🗄 3 in the archive — /archive", "14:02",
          kb=["🟢 Auth: refresh tokens · 3f9a1c07 ⚙️", "🟢 Docs: API reference · 8b2e4d10",
              "🟢 Data pipeline backfill · c41d9e2a ⚙️", "🟢 Landing page redesign · 5d07b3f8 ⚙️",
              "⚪️ Scraper rate limits · a9e61c34"]),
    tg_in("✅ Current terminal: «Data pipeline backfill» · c41d9e2a", "14:02"),
    tg_in("📺 «Data pipeline backfill» · c41d9e2a\n\n"
          "● Bash(python3 backfill.py --month 11)\n"
          "  ⎿ month 11 of 18 · 2.4M rows · 0 errors\n"
          "● Bash(python3 verify_counts.py --month 11)\n"
          "  ⎿ row counts match the source (2,401,877)\n\n"
          "✶ Backfilling month 12… (3m 08s · esc to interrupt)", "14:02"),
]


# ─────────────────────────────────────────────────────────── main ──
def main() -> int:
    from playwright.sync_api import sync_playwright

    keep = "--keep" in sys.argv
    for tool in ("ffmpeg",):
        if not shutil.which(tool):
            print(f"{tool} is required"); return 2
    work = Path(tempfile.mkdtemp(prefix="agentdeck-demo-"))
    frames = work / "frames"
    frames.mkdir()
    state_path = work / "state.json"
    state = ms.board_state()
    state_path.write_text(json.dumps(state, ensure_ascii=False))
    base = ms.start_server(state_path)
    print("serving", base, "frames in", frames)
    film = Film(frames)

    with sync_playwright() as p:
        b = p.chromium.launch()

        # ── beats 1-4: desktop dashboard ─────────────────────────────
        ctx, pg, errs = ms.open_dashboard(b, base, width=W, height=H)
        pg.click('#list .card[data-sid="3f9a1c07"] .proj')
        pg.wait_for_selector("#wrap iframe")
        pg.frame_locator("#wrap iframe").locator(".box").wait_for()
        ms.settle(pg)
        overlay(pg)
        term = next(f for f in pg.frames if "arg=3f9a1c07" in f.url)
        ms.check_english(pg, "beat1")

        # beat 1 — one agent working: spinner ticks, tool calls land
        caption(pg, "Many Claude Code agents, one dashboard")
        secs, tok, i = 38, 1.2, 0
        for step in range(30):                       # ~4.6 s at 1/FPS * 2
            if step in (9, 20):
                term_add(term, *NEW_AUTH_LINES[0 if step == 9 else 1])
                tok += 0.9
            term_spin(term, i, secs + step // 7, f"{tok + step * 0.03:.1f}k")
            i += 1
            film.shot(pg, STEP * 2)

        # beat 2 — click another terminal, its session appears
        caption(pg, "Switch terminals", "· the others keep working")
        film.shot(pg, 0.4)
        x, y = cursor_to(pg, film, '#list .card[data-sid="8b2e4d10"] .proj', steps=9)
        ring(pg, film, x, y)
        pg.click('#list .card[data-sid="8b2e4d10"] .proj')
        pg.wait_for_function("() => [...document.querySelectorAll('#wrap iframe')].some(f => f.src.includes('8b2e4d10') && f.getBoundingClientRect().width > 0)")
        next(f for f in pg.frames if "arg=8b2e4d10" in f.url).locator(".box").wait_for()
        pg.wait_for_timeout(500)
        film.shot(pg, 2.6)
        ms.check_english(pg, "beat2")

        # beat 3 — the Tasks tab, a task moving
        caption(pg, "A task board per terminal")
        x, y = cursor_to(pg, film, "#tasksBtn", steps=8)
        ring(pg, film, x, y)
        pg.click("#tasksBtn")
        pg.wait_for_selector("#list .tasks-row.sel")
        tfr = pg.frame_locator("#wrap iframe")
        tfr.locator(".task").first.wait_for()
        tfr.locator('.task[data-id="auth-rotation"] .task-row').click()
        hide_cursor(pg)
        ms.settle(pg)
        overlay(pg)
        film.shot(pg, 1.6)
        auth = next(t for t in state["tasks"] if t["id"] == "auth-rotation")
        auth["items"][4].update(status="done", note="6 passed")
        auth["items"][5].update(status="active", note="deploying")
        state_path.write_text(json.dumps(state, ensure_ascii=False))
        pg.wait_for_timeout(5600)                   # SSE or the 5 s refresh picks it up
        film.shot(pg, 2.2)
        ms.check_english(pg, "beat3")

        # beat 4 — the Server tab: who uses the CPU
        caption(pg, "What is using your server")
        x, y = cursor_to(pg, film, "#serverBtn", steps=8)
        ring(pg, film, x, y)
        pg.click("#serverBtn")
        pg.wait_for_selector("#list .server-row.sel")
        sfr = pg.frame_locator("#wrap iframe")
        sfr.locator(".row[data-id]").first.wait_for()
        hide_cursor(pg)
        ms.settle(pg)
        film.shot(pg, 1.4)
        sfr.locator('.row[data-id="agent:41822"] .main').click()
        srv = next(f for f in pg.frames if "server" in f.url)
        for k in range(1, 7):                         # scroll the agents into view
            srv.evaluate("y => window.scrollTo(0, y)", 40 * k)
            film.shot(pg, STEP)
        pg.wait_for_timeout(300)
        film.shot(pg, 2.2)
        ms.check_english(pg, "beat4")
        ctx.close()
        ms.PROBLEMS.extend(f"desktop: pageerror {e}" for e in errs)

        # ── stage for the phone beats ──
        sctx = b.new_context(viewport={"width": W, "height": H}, device_scale_factor=ms.SCALE)
        stage = sctx.new_page()
        stage.set_content(STAGE_HTML % {"w": W, "h": H})
        stage.evaluate("() => document.fonts.ready")
        overlay(stage)

        # beat 5 — the dashboard on a phone: the agent keeps going, you type the next step
        PW, PH, K = 390, 740, 0.86
        mctx, mpg, merrs = ms.open_dashboard(b, base, width=PW, height=PH, mobile=True, tasks_open=False)
        mpg.tap('#list .card[data-sid="3f9a1c07"] .proj')
        mpg.wait_for_selector("#wrap iframe")
        mpg.frame_locator("#wrap iframe").locator(".box").wait_for()
        ms.settle(mpg)
        mterm = next(f for f in mpg.frames if "arg=3f9a1c07" in f.url)
        ms.check_english(mpg, "beat5")
        side = ("<b>Same dashboard,<br>in your pocket</b>Read what the agent did, "
                "type or dictate the next step. The terminal runs on your server, not in the tab.")
        caption(stage, "From your phone")
        dw, dh = int(PW * K), int(PH * K)

        def phone(dur: float) -> None:
            stage_show(stage, [(data_url(mpg.screenshot()), dw, dh)], side)
            film.shot(stage, dur)

        for k in range(5):
            term_spin(mterm, k, 44 + k // 3, f"{3.1 + k * 0.1:.1f}k")
            phone(STEP * 2)
        box = mpg.locator("#mobText").bounding_box()
        img = stage.locator(".dev img").bounding_box()
        ring(stage, film, img["x"] + (box["x"] + 50) * K, img["y"] + (box["y"] + box["height"] / 2) * K)
        msg = "Also rate-limit /auth/sessions"
        for n in range(4, len(msg) + 4, 4):
            mpg.evaluate("t => { const m = document.getElementById('mobText'); m.value = t; m.focus(); }", msg[:n])
            term_spin(mterm, 5 + n, 46, f"{3.6 + n * 0.02:.1f}k")
            phone(STEP * 1.5)
        phone(1.3)
        mctx.close()
        ms.PROBLEMS.extend(f"mobile: pageerror {e}" for e in merrs)

        # beat 6 — Telegram: pick a terminal, its screen arrives
        TW, TH = 420, 760
        tctx = b.new_context(viewport={"width": TW, "height": TH}, device_scale_factor=ms.SCALE)
        tpg = tctx.new_page()
        tpg.set_content(ms.tg_html())
        tpg.add_style_tag(content=TG_EXTRA_CSS)
        tpg.evaluate("() => { document.querySelector('.status span').textContent = '14:02'; }")
        side = ("<b>A Telegram bot,<br>same terminals</b>For when the browser is too much:"
                "<ul><li>pick one from a list</li><li>&#x1F4FA; its screen comes back as text</li>"
                "<li>reply, or send a voice note</li></ul>")
        caption(stage, "…or from Telegram")
        tdw, tdh = int(TW * 0.83), int(TH * 0.83)

        def tg_frame(n_msgs: int, dur: float, hit: int | None = None) -> None:
            tpg.evaluate("([h, hit]) => { const c = document.querySelector('.chat'); c.innerHTML = h;"
                         " if (hit !== null) { const k = c.querySelectorAll('.kbtn'); k[hit].classList.add('hit'); } }",
                         ["".join(TG_STEPS[:n_msgs]), hit])
            tpg.wait_for_timeout(120)
            stage_show(stage, [(data_url(tpg.screenshot()), tdw, tdh)], side)
            film.shot(stage, dur)

        tg_frame(1, 0.6)
        tg_frame(2, 1.5)
        kb = tpg.locator(".kbtn").nth(2).bounding_box()
        img = stage.locator(".dev img").bounding_box()
        ring(stage, film, img["x"] + (kb["x"] + kb["width"] / 2) * tdw / TW,
             img["y"] + (kb["y"] + kb["height"] / 2) * tdh / TH)
        tg_frame(2, 0.5, hit=2)
        ms.check_english(tpg, "beat6-picker")
        tg_frame(3, 0.8, hit=2)
        tg_frame(4, 3.2, hit=2)
        ms.check_english(tpg, "beat6-screen")
        ms.check_english(stage, "stage")
        tctx.close()
        sctx.close()
        b.close()

    if ms.PROBLEMS:
        print("PROBLEMS:"); [print("  -", x) for x in ms.PROBLEMS]
        return 1
    print(f"{len(film.frames)} frames, {film.seconds:.1f} s")
    DOCS.mkdir(exist_ok=True)
    gif, mp4 = DOCS / "demo.gif", DOCS / "demo.mp4"
    film.build(gif, mp4)
    for f in (gif, mp4):
        print(f"wrote {f} ({f.stat().st_size / 1e6:.2f} MB)")
    if keep:
        print("frames kept in", frames)
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
