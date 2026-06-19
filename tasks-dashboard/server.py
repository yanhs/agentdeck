#!/usr/bin/env python3
"""Tiny shared task-tracker server (stdlib only).

Serves a live dashboard + an SSE stream of state.json. ALL Claude agents share one
state.json (mutated via tracker.py); every browser tab on /events is pushed the new
state instantly, so one dashboard shows every agent's progress at once.

Run: TASKS_PORT=9308 python3 server.py   (proxied by nginx at /tasks/)
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "state.json"
INDEX = BASE / "static" / "index.html"
PORT = int(os.getenv("TASKS_PORT", "9308"))


def state_text():
    """state.json as one compact line (safe for SSE 'data:' framing)."""
    try:
        return json.dumps(json.loads(STATE.read_text()), separators=(",", ":"))
    except Exception:
        return '{"tasks":[]}'


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            try:
                self._send(200, INDEX.read_text(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "no index", "text/plain")
        elif path == "/state.json":
            self._send(200, state_text(), "application/json")
        elif path == "/healthz":
            self._send(200, "ok", "text/plain")
        elif path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")  # tell nginx not to buffer
            self.end_headers()
            last, ticks = None, 0
            try:
                while True:
                    cur = state_text()
                    if cur != last:
                        self.wfile.write(("data: " + cur + "\n\n").encode())
                        self.wfile.flush()
                        last = cur
                    else:
                        ticks += 1
                        if ticks % 12 == 0:               # keepalive comment ~every 12s
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"[tracker] serving on 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
