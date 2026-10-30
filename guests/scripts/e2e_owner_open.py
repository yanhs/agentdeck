#!/usr/bin/env python3
"""End-to-end check of Open (the owner signs in to a guest's dashboard, 29.10.2026) on
THROWAWAY sandboxes made from real guest images. Nothing of the server's own is touched:
containers sbx-e2eopen<N>, a copy of this guests module in a temp folder, an nginx of its
own on 127.0.0.1 high ports with a self-signed certificate, the owner panel
(status_server.Handler) on a free port. A real Chromium clicks Open on web/server.html.

    python3 guests/scripts/e2e_owner_open.py IMAGE [IMAGE ...]

The vhost the browser goes through is guestctl.dashboard_vhost() itself; only its ports
and certificate are swapped for local ones (the panel's 3011 for the test panel's port).
Prints PASS/FAIL; exit code = failures."""

import hashlib
import importlib
import json
import os
import secrets
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

GUESTS = Path(__file__).resolve().parents[1]
REPO = GUESTS.parent
DOMAIN = "e2e.test"
TLS_PORT, OWNER_PORT, PROXY_PORT, FIRST_PORT = 18443, 18080, 18444, 18190
fails = 0


def check(ok, label):
    global fails
    print(("PASS  " if ok else "FAIL  ") + label, flush=True)
    fails += 0 if ok else 1


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_http(url, seconds=120):
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except OSError:
            time.sleep(1)
    return None


def connect_proxy(stop):
    """CONNECT <anything>.e2e.test:443 -> our nginx; nothing else is let through."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PROXY_PORT))
    srv.listen(32)
    srv.settimeout(0.5)

    def pipe(a, b):
        try:
            while True:
                r, _, _ = select.select([a, b], [], [], 30)
                if not r:
                    return
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            pass
        finally:
            a.close()
            b.close()

    def one(c):
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 8192:
            chunk = c.recv(4096)
            if not chunk:
                return c.close()
            head += chunk
        line = head.split(b"\r\n", 1)[0].decode(errors="replace").split()
        if len(line) > 1 and line[0] == "CONNECT" and line[1].rsplit(":", 1)[0].endswith("." + DOMAIN):
            up = socket.create_connection(("127.0.0.1", TLS_PORT))
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            return pipe(c, up)
        # Playwright sends even 127.0.0.1 through the proxy ("<-loopback>"): the owner's
        # page goes on to our nginx as it came (nginx takes the absolute-form request line)
        if len(line) > 1 and line[1].startswith(f"http://127.0.0.1:{OWNER_PORT}/"):
            up = socket.create_connection(("127.0.0.1", OWNER_PORT))
            up.sendall(head)
            return pipe(c, up)
        c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        c.close()

    while not stop.is_set():
        try:
            c, _ = srv.accept()
        except socket.timeout:
            continue
        threading.Thread(target=one, args=(c,), daemon=True).start()
    srv.close()


def https_block(vhost):
    return vhost[vhost.index("server {", vhost.index("listen 443") - 40):]


def main(images):
    tmp = Path(tempfile.mkdtemp(prefix="e2e-owner-open-"))
    gcopy = tmp / "guests"
    shutil.copytree(GUESTS, gcopy, ignore=shutil.ignore_patterns(
        "__pycache__", ".env", "data", "public", "sites", "tests", "build"))
    env_file = gcopy / ".env"
    env_file.write_text(f"GUESTS_DOMAIN={DOMAIN}\nGUEST_BOT_TOKEN=123456789:AAH{'x' * 32}\n"
                        "OWNER_TG_ID=42\nGUESTS_LANG=en\n")
    tg = tmp / "telegram.json"
    tg.write_text(json.dumps({"enabled": True, "token": "999:" + "y" * 35, "owner": 42}))
    os.environ.update(GUESTS_DIR=str(gcopy), GUESTS_ENV_FILE=str(env_file),
                      AGENTDECK_TG_CONF=str(tg), AGENTDECK_AUTH_SECRET=str(tmp / "panel_key"))
    os.environ.pop("AGENTDECK_ORIGIN", None)
    sys.path[:0] = [str(gcopy), str(gcopy / "turnstile"), str(REPO)]
    gc = importlib.import_module("guestctl")
    ts = importlib.import_module("turnstile")

    names, nginx, panel, stop = [], None, None, threading.Event()
    try:
        # ── the sandboxes: real images, real dashboards ──
        reg, passwords = {}, {}
        (gcopy / "turnstile" / "data").mkdir(parents=True, exist_ok=True)
        (gcopy / "nginx" / "sites").mkdir(parents=True, exist_ok=True)
        book = ts.GuestBook(gcopy / "turnstile" / "data" / "guests.json")
        for i, image in enumerate(images, 1):
            name, port = f"e2eopen{i}", FIRST_PORT + i
            pw = secrets.token_urlsafe(12)
            subprocess.run(["docker", "rm", "-f", f"sbx-{name}"], capture_output=True)
            r = subprocess.run(["docker", "run", "-d", "--name", f"sbx-{name}", "--memory", "1g",
                                "--pids-limit", "1024", "-p", f"127.0.0.1:{port}:8765",
                                "-e", f"AGENTDECK_PASSWORD={pw}", "-e", "AGENTDECK_SITE=:8765",
                                "-e", f"AGENTDECK_ORIGIN=https://sb-{name}.{DOMAIN}",
                                "-e", "TZ=America/Los_Angeles", image], capture_output=True, text=True)
            check(r.returncode == 0, f"{name}: sandbox from {image} started {r.stderr.strip()[-200:]}")
            names.append(name)
            passwords[name] = pw
            reg[name] = {"port": port, "telegram_id": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            book.add(name, 1_000_000, 1)
            gc_vhost = gc.dashboard_vhost(name, port)
            (gcopy / "nginx" / "sites" / f"sb-{name}.{DOMAIN}.conf").write_text(gc_vhost)
        (gcopy / "turnstile" / "data" / "sandboxes.json").write_text(json.dumps(reg))
        for name in names:
            code = wait_http(f"http://127.0.0.1:{reg[name]['port']}/login")
            check(code == 200, f"{name}: its dashboard answers ({code})")

        # ── the owner panel ──
        ss = importlib.import_module("status_server")
        panel_port = free_port()
        panel = HTTPServer(("127.0.0.1", panel_port), ss.Handler)
        threading.Thread(target=panel.serve_forever, daemon=True).start()

        # ── nginx: the guests' real vhosts on local ports + the owner's page ──
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", f"/CN={DOMAIN}", "-addext", f"subjectAltName=DNS:*.{DOMAIN}",
                        "-keyout", str(tmp / "key.pem"), "-out", str(tmp / "cert.pem")],
                       capture_output=True, check=True)
        servers = []
        for name in names:
            host = f"sb-{name}.{DOMAIN}"
            block = https_block(gc.dashboard_vhost(name, reg[name]["port"]))
            check(block.count("127.0.0.1:3011") == 1, f"{name}: the vhost sends one path to the panel")
            block = (block.replace("listen 443 ssl;", f"listen 127.0.0.1:{TLS_PORT} ssl;")
                     .replace("listen [::]:443 ssl;\n", "")
                     .replace(f"/etc/letsencrypt/live/{host}/fullchain.pem", str(tmp / "cert.pem"))
                     .replace(f"/etc/letsencrypt/live/{host}/privkey.pem", str(tmp / "key.pem"))
                     .replace("127.0.0.1:3011", f"127.0.0.1:{panel_port}"))
            servers.append(block)
        servers.append(
            "server {\n"
            f"    listen 127.0.0.1:{OWNER_PORT};\n"
            f"    location / {{ root {REPO / 'web'}; default_type text/html; try_files /server.html =404; }}\n"
            f"    location /api/ {{ proxy_pass http://127.0.0.1:{panel_port}; proxy_set_header Host $http_host; }}\n"
            "}\n")
        for d in ("cb", "px", "fc", "uw", "sc", "logs"):
            (tmp / d).mkdir(exist_ok=True)
        (tmp / "nginx.conf").write_text(
            f"pid {tmp}/nginx.pid;\nerror_log {tmp}/logs/error.log;\nevents {{}}\nhttp {{\n"
            f"  access_log {tmp}/logs/access.log;\n  client_body_temp_path {tmp}/cb;\n"
            f"  proxy_temp_path {tmp}/px;\n  fastcgi_temp_path {tmp}/fc;\n"
            f"  uwsgi_temp_path {tmp}/uw;\n  scgi_temp_path {tmp}/sc;\n"
            + "\n".join(servers) + "}\n")
        nginx = subprocess.Popen(["/usr/sbin/nginx", "-p", str(tmp), "-c", str(tmp / "nginx.conf"),
                                  "-g", "daemon off;"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=connect_proxy, args=(stop,), daemon=True).start()
        check(wait_http(f"http://127.0.0.1:{OWNER_PORT}/", 15) == 200, "the owner's page is served")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(proxy={"server": f"http://127.0.0.1:{PROXY_PORT}"})
            for name in names:
                host = f"https://sb-{name}.{DOMAIN}"
                dashpass = ["docker", "exec", f"sbx-{name}", "cat", "/app/.sessions/.dashpass"]
                before = hashlib.sha256(subprocess.run(dashpass, capture_output=True).stdout).hexdigest()
                ctx = browser.new_context(ignore_https_errors=True)
                ctx.route("https://fonts.googleapis.com/**", lambda r: r.abort())
                ctx.route("https://fonts.gstatic.com/**", lambda r: r.abort())
                entries, entry_responses = [], []
                ctx.on("request", lambda req: entries.append(req.url) if "/_owner-entry" in req.url else None)
                # Response.headers leaves cookies out; header_value() is read after the click
                ctx.on("response", lambda res: entry_responses.append(res) if "/_owner-entry" in res.url else None)
                page = ctx.new_page()
                page_errors = []
                page.on("pageerror", lambda e: page_errors.append(str(e)))
                page.on("console", lambda m: page_errors.append(m.text) if m.type == "error" else None)
                page.goto(f"http://127.0.0.1:{OWNER_PORT}/?poll=1000")
                btn = f'#guests .guest[data-name="{name}"] button[data-act="open"]'
                try:
                    page.wait_for_selector(btn, timeout=30000)
                except Exception:
                    # say what the panel saw: the check must fail loudly, not just time out
                    with urllib.request.urlopen(f"http://127.0.0.1:{OWNER_PORT}/api/guests", timeout=10) as r:
                        print("GET /api/guests:", r.read().decode()[:3000])
                    print("page errors:", page_errors[:10])
                    print("guests block:", page.inner_text("#guests")[:800] if page.query_selector("#guests") else "-")
                    page.screenshot(path=str(tmp.parent / "e2e_owner_open_fail.png"), full_page=True)
                    raise
                with page.expect_popup() as pop:
                    page.click(btn)
                tab = pop.value
                tab.wait_for_url(host + "/", timeout=20000)
                tab.wait_for_load_state()
                check(tab.url == host + "/", f"{name}: Open lands on the guest's dashboard ({tab.url})")
                check(tab.locator('input[name="pass"]').count() == 0 and "login" not in tab.title().lower(),
                      f"{name}: signed in, no password asked (title: {tab.title()!r})")
                # what the entry link set (the dashboard may re-issue it at once on its renew)
                sc = (entry_responses[0].header_value("set-cookie") or "") if entry_responses else ""
                check(sc.startswith("agents_session=owner.") and all(
                      f in sc for f in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=43200")),
                      f"{name}: the entry link set the owner's cookie (HttpOnly, Secure, 12 h)")
                cookie = next((c for c in ctx.cookies(host) if c["name"] == "agents_session"), None)
                check(bool(cookie) and cookie["value"].startswith("owner.") and cookie["httpOnly"],
                      f"{name}: the browser keeps the owner's login on the guest's address")
                if cookie:
                    print(f"NOTE  {name}: the dashboard's own renewal: secure={cookie['secure']}, "
                          f"{(cookie['expires'] - time.time()) / 86400:.1f} days")
                check(len(entries) == 1, f"{name}: one entry link was used ({len(entries)})")
                again = ctx.new_page()
                resp = again.goto(entries[0]) if entries else None
                check(resp is not None and resp.status == 403, f"{name}: the link works only once")
                again.close()
                tab.close()
                ctx.close()
                # the guest's own password still works, and its file is untouched
                fresh = browser.new_context(ignore_https_errors=True)
                lp = fresh.new_page()
                lp.goto(host + "/login")
                lp.fill('input[name="pass"]', passwords[name])
                lp.click('button[type="submit"]')
                lp.wait_for_url(host + "/", timeout=15000)
                check(lp.locator('input[name="pass"]').count() == 0, f"{name}: the guest's password still signs in")
                own = next((c for c in fresh.cookies(host) if c["name"] == "agents_session"), None)
                if own:
                    print(f"NOTE  {name}: the guest's own password login: secure={own['secure']}")
                fresh.close()
                after = hashlib.sha256(subprocess.run(dashpass, capture_output=True).stdout).hexdigest()
                check(before == after, f"{name}: the guest's password file did not change")
            browser.close()
    except Exception:
        import traceback
        traceback.print_exc()
        check(False, "the check itself stopped (traceback above)")
    finally:
        stop.set()
        if panel:
            panel.shutdown()
            panel.server_close()
        if nginx:
            nginx.terminate()
            nginx.wait(10)
        for name in names:   # -v: the image's anonymous volumes go with the container
            subprocess.run(["docker", "rm", "-f", "-v", f"sbx-{name}"], capture_output=True)
        log = (tmp / "logs" / "error.log")
        if fails and log.exists():
            print("nginx error.log:\n" + log.read_text()[-1500:])
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{'OK' if not fails else 'FAILED'}: {fails} failure(s)")
    return fails


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1:]))
