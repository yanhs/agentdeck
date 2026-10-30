#!/usr/bin/env python3
"""End-to-end check of what a guest sees about their own sandbox (30.10.2026): the usage
chip "5h X% / wk Y%" (GET /api/quota -> the turnstile's GET /guest/usage, 5-hour window and
week) and the Server page showing the sandbox's own cores and memory. On THROWAWAY containers only: a network
e2esv-net, a turnstile (this checkout's code, a made-up guest) and a sandbox from IMAGE
with --memory 1g --cpus 1. Nothing of the server's own is touched. Screenshots go to
OUT (optional). Prints PASS/FAIL; exit code = failures.

    python3 guests/scripts/e2e_sandbox_view.py IMAGE [OUT_DIR]
"""
import http.cookiejar
import json
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NET, GATE, SBX, PORT = "e2esv-net", "e2esv-gate", "sbx-e2esv", 18291
SPENT, WEEK = 6_000_000, 140_000_000      # 5 h limit = a seventh = 20M -> 30 %
fails = 0


def check(ok, label):
    global fails
    print(("PASS  " if ok else "FAIL  ") + label, flush=True)
    fails += 0 if ok else 1


def docker(*a, check_=True):
    return subprocess.run(["docker", *a], capture_output=True, text=True, check=check_)


def cleanup():
    docker("rm", "-f", "-v", SBX, GATE, check_=False)
    docker("network", "rm", NET, check_=False)


def main(image, out=None):
    sys.path[:0] = [str(REPO / "guests"), str(REPO / "guests" / "turnstile")]
    import turnstile as ts
    tmp = Path(tempfile.mkdtemp(prefix="e2esv-"))
    book = ts.GuestBook(tmp / "guests.json")
    gpass = book.add("e2esv", WEEK)
    # a made-up spend: input tokens weigh 1 (turnstile.weighted)
    ts.Ledger(tmp / "usage.db").record("e2esv", "claude-opus-x", {"input_tokens": SPENT})
    (tmp / "oauth.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=not-a-real-token\n")
    for f in tmp.iterdir():
        f.chmod(0o644)
    pw = secrets.token_urlsafe(12)
    cleanup()
    try:
        docker("network", "create", NET)
        docker("run", "-d", "--name", GATE, "--network", NET, "-v", f"{REPO}:/r:ro",
               "-v", f"{tmp}:/data", "-e", "TURNSTILE_BIND=0.0.0.0", "-e", "TURNSTILE_PORT=8790",
               "-e", "TURNSTILE_DATA=/data", "-e", "TURNSTILE_OAUTH_ENV=/data/oauth.env",
               "--entrypoint", "python3", image, "-I", "/r/guests/turnstile/turnstile.py", "serve")
        docker("run", "-d", "--name", SBX, "--network", NET, "--memory", "1g", "--cpus", "1",
               "-p", f"127.0.0.1:{PORT}:8765", "-e", f"AGENTKENNEL_PASSWORD={pw}",
               "-e", "AGENTKENNEL_SITE=:8765", "-e", f"ANTHROPIC_BASE_URL=http://{GATE}:8790",
               "-e", f"ANTHROPIC_AUTH_TOKEN={gpass}", image)
        base = f"http://127.0.0.1:{PORT}"
        for _ in range(120):
            try:
                urllib.request.urlopen(base + "/login", timeout=3)
                break
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(1)
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        op.open(base + "/login", data=urllib.parse.urlencode({"pass": pw}).encode(), timeout=10)
        check(any(c.name == "agents_session" for c in jar), "signed in to the throwaway sandbox")

        q = {}
        for _ in range(10):                     # the turnstile may still be starting
            q = json.loads(op.open(base + "/api/quota", timeout=10).read())
            if q.get("available"):
                break
            time.sleep(1)
        fh, wk = q.get("five_hour") or {}, q.get("seven_day") or {}
        check(q.get("available") is True and q.get("source") == "guest"
              and abs(fh.get("utilization", 0) - SPENT / (WEEK // 7)) < 0.01
              and abs(wk.get("utilization", 0) - SPENT / WEEK) < 0.01 and fh.get("resets_at", 0) > time.time(),
              f"/api/quota inside the sandbox: {q}")
        check(gpass not in json.dumps(q), "the pass is not in the answer")

        h = {}
        for _ in range(10):                     # the collector needs two samples for CPU
            h = json.loads(op.open(base + "/api/server", timeout=10).read()).get("host", {})
            if h:
                break
            time.sleep(1)
        check(h.get("scope") == "sandbox" and h.get("cores") == 1 and h.get("ram_total_mb") == 1024,
              f"/api/server: scope={h.get('scope')} cores={h.get('cores')} ram={h.get('ram_total_mb')} MB")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--font-render-hinting=none"])
            ctx = b.new_context(viewport={"width": 1280, "height": 800})
            ctx.add_cookies([{"name": c.name, "value": c.value, "url": base} for c in jar])
            ctx.route("https://fonts.googleapis.com/**", lambda r: r.abort())
            ctx.route("https://fonts.gstatic.com/**", lambda r: r.abort())
            pg = ctx.new_page()
            pg.goto(base + "/")
            pg.wait_for_selector("#quotaChip:not([hidden])", timeout=30000)
            text = " ".join(pg.inner_text("#quotaChip").split())
            check(text == "5h 30% wk 4%", f"the dashboard shows: {text!r}")
            check(pg.inner_text("#newBtn").strip() == "＋ New", "the button says ＋ New")
            if out:
                pg.screenshot(path=str(Path(out) / "guest-dashboard.png"))
                pg.locator(".side-top").screenshot(path=str(Path(out) / "guest-token-line.png"))
            pg.goto(base + "/server.html")
            pg.wait_for_function("() => document.querySelector('#host').textContent.includes('sandbox')",
                                 timeout=20000)
            check("of 1 cores" in pg.inner_text("#g-load .gs") and "5m" not in pg.inner_text("#g-load .gs"),
                  f"the Server page: {pg.inner_text('#host')!r}, {pg.inner_text('#g-cpu .gs')!r}, "
                  f"{pg.inner_text('#g-ram .gs')!r}, {pg.inner_text('#g-load .gs')!r}")
            if out:
                pg.screenshot(path=str(Path(out) / "guest-server-page.png"))
            b.close()
    except Exception as e:
        check(False, f"the check itself stopped: {e!r}")
    finally:
        cleanup()
    print(f"{'OK' if not fails else 'FAILED'}: {fails} failure(s)")
    return fails


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
