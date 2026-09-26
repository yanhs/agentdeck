#!/usr/bin/env python3
"""Smoke test of an installed AgentDeck, from outside, through Caddy (stdlib only).

    smoke.py BASE_URL --password PW [--first-run] [--new-terminal] [--expect-id ID]
             [--id-file PATH] [--insecure] [--connect HOST:PORT]

  --first-run      the login page must be the "set a password" form; POST /login
                   pass/pass2 sets it. Otherwise log in with user admin + PW.
  --new-terminal   POST /api/library/new (JSON, Origin = BASE_URL), open /sess/?arg=<id>,
                   then attach over the ttyd websocket and wait for terminal output
                   (proves ttyd -> open-session.sh -> tmux -> claude starts). The id is
                   written to --id-file.
  --expect-id ID   /api/library must still list ID (e.g. after a reboot).
  --insecure       https: don't verify the certificate (self-signed / a test CA).
  --connect H:P    open every connection to H:P instead of BASE's host — like curl
                   --connect-to: the URL, Host header and TLS name stay BASE's.

Always: /login 200, /api/library 200, /tasks/ 200, /api/server 200, and / without a
cookie redirects to /login. Exit 0 = all passed; every check prints one line.
"""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


FAILED = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--password", required=True)
    ap.add_argument("--first-run", action="store_true")
    ap.add_argument("--new-terminal", action="store_true")
    ap.add_argument("--expect-id")
    ap.add_argument("--id-file")
    ap.add_argument("--wait", type=int, default=60, help="seconds to wait for the login page")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--connect")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    if a.connect:
        host, _, port = a.connect.rpartition(":")
        real = socket.create_connection
        socket.create_connection = lambda _addr, *x, **k: real((host, int(port)), *x, **k)
    ctx = ssl.create_default_context()
    if a.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect,
                                     urllib.request.HTTPSHandler(context=ctx))

    def req(method, path, data=None, headers=None):
        r = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
        try:
            with op.open(r, timeout=20) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    # 0. wait until Caddy + status_server answer
    deadline = time.time() + a.wait
    code, body = 0, b""
    while time.time() < deadline:
        try:
            code, _, body = req("GET", "/login")
            if code == 200:
                break
        except OSError:
            pass
        time.sleep(2)
    check("GET /login 200", code == 200, f"got {code}")
    if code != 200:
        return 1
    if a.first_run:
        check("first run shows the set-password form", b'name="pass2"' in body)

    code, hdrs, _ = req("GET", "/")
    check("GET / without login -> /login", code == 302 and "/login" in hdrs.get("Location", ""),
          f"{code} {hdrs.get('Location')}")

    # 1. log in (first run: set the password)
    form = ({"pass": a.password, "pass2": a.password} if a.first_run
            else {"user": "admin", "pass": a.password})
    code, hdrs, _ = req("POST", "/login", urllib.parse.urlencode(form).encode(),
                        {"Content-Type": "application/x-www-form-urlencoded"})
    check("POST /login sets a session cookie", len(jar) > 0, f"status {code}")

    # 2. the library
    code, _, body = req("GET", "/api/library")
    check("GET /api/library 200", code == 200, f"got {code}")
    try:
        lib = json.loads(body)
    except ValueError:
        lib = None
    rows = lib if isinstance(lib, list) else (lib or {}).get("sessions") or (lib or {}).get("items") or []
    ids = [r.get("id") for r in rows if isinstance(r, dict)]
    if a.expect_id:
        check(f"terminal {a.expect_id} still listed", a.expect_id in ids, f"ids={ids}")

    if a.new_terminal:
        code, _, body = req("POST", "/api/library/new", json.dumps({"name": "smoke"}).encode(),
                            {"Content-Type": "application/json", "Origin": base})
        sid = ""
        try:
            sid = json.loads(body).get("id", "")
        except ValueError:
            pass
        check("POST /api/library/new creates a terminal", code == 200 and len(sid) == 8,
              f"{code} {body[:120]!r}")
        if a.id_file and sid:
            with open(a.id_file, "w") as f:
                f.write(sid)
        code, _, body = req("GET", f"/sess/?arg={sid}")
        check("GET /sess/?arg=<id> 200", code == 200, f"got {code}")
        if sid:
            out = ttyd_attach(base, jar, sid, ctx)
            check("terminal attaches and prints output", len(out) > 0,
                  f"{len(out)} bytes: {out[-160:]!r}")

    for path in ("/tasks/", "/api/server"):
        code, _, _ = req("GET", path)
        check(f"GET {path} 200", code == 200, f"got {code}")

    print("SMOKE", "OK" if not FAILED else f"FAILED: {', '.join(FAILED)}")
    return 0 if not FAILED else 1


def ttyd_attach(base, jar, sid, ctx, seconds=20, stay=False):
    """Open /sess/ws?arg=<id> like the browser does (over TLS for https) and collect
    terminal output: until 2000 bytes came, or — stay=True, an open tab — for the
    whole `seconds` (then the last 64 KiB)."""
    u = urllib.parse.urlparse(base)
    tls = u.scheme == "https"
    host, port = u.hostname, u.port or (443 if tls else 80)
    cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
    key = base64.b64encode(os.urandom(16)).decode()
    hostport = f"{host}:{port}" if u.port else host
    s = socket.create_connection((host, port), timeout=10)
    if tls:
        s = ctx.wrap_socket(s, server_hostname=host)
    s.sendall((f"GET /sess/ws?arg={sid} HTTP/1.1\r\nHost: {hostport}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: tty\r\n"
               f"Origin: {base}\r\nCookie: {cookie}\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    if b" 101 " not in head.split(b"\r\n", 1)[0]:
        return b""
    rest = head.split(b"\r\n\r\n", 1)[1]

    def send(payload: bytes, opcode=1):
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        hdr += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big")
        s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    send(json.dumps({"AuthToken": "", "columns": 120, "rows": 40}).encode(), opcode=2)
    buf, out = rest, b""
    end = time.time() + seconds
    s.settimeout(2)
    while time.time() < end and (stay or len(out) < 2000):
        try:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            pass
        while len(buf) >= 2:
            n = buf[1] & 0x7F
            off = 2
            if n == 126:
                if len(buf) < 4:
                    break
                n, off = int.from_bytes(buf[2:4], "big"), 4
            elif n == 127:
                if len(buf) < 10:
                    break
                n, off = int.from_bytes(buf[2:10], "big"), 10
            if len(buf) < off + n:
                break
            opcode = buf[0] & 0x0F
            payload, buf = buf[off:off + n], buf[off + n:]
            if opcode == 0x9:                # ping: answer, or ttyd drops a quiet tab
                send(payload, opcode=0xA)
            elif payload[:1] == b"0":        # ttyd OUTPUT message
                out = (out + payload[1:])[-65536:]
    s.close()
    return out


if __name__ == "__main__":
    sys.exit(main())
