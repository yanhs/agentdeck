"""📎 File: any file from the browser lands in a PRIVATE folder on the server and the
terminal gets its PATH (owner 29.10.2026: «загрузить файл ... и вставить в терминал»).

A path, not a link: Claude runs on the same machine and reads the file directly, and a
public link would expose client documents. POST /api/upload?name=<original name> with the
raw bytes → <UPLOAD_DIR>/<YYYY-MM-DD>/<safe name> (0600, folder 0700) → {path, name, size}.
The handler checks the login cookie itself, not only the proxy gate.
"""
from __future__ import annotations

import http.client
import io
import json
import os
import socket
import stat
import sys
import threading
import time
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import status_server as ss  # noqa: E402


# ── the file name ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, want", [
    ("report.pdf", "report.pdf"),
    ("договор аренды.docx", "договор_аренды.docx"),   # letters kept, no spaces in a path
    ("../../etc/passwd", "passwd"),                     # no way out of the folder
    ("C:\\Users\\me\\photo 1.JPG", "photo_1.JPG"),       # a Windows path → its last part
    (".env", "env"),                                    # never a hidden file
    ("a;rm -rf ~;$(x)`y`.txt", "a_rm_-rf_x_y_.txt"),     # nothing a shell would act on
    ("", "file"), ("...", "file"), (None, "file"),
])
def test_upload_name_is_safe(raw, want):
    assert ss._upload_name(raw) == want


def test_upload_name_is_capped_and_keeps_the_extension():
    n = ss._upload_name("x" * 400 + ".tar.gz")
    assert len(n) <= 120 and n.endswith(".gz")


# ── saving ──────────────────────────────────────────────────────────────────
def test_save_upload_streams_into_a_private_dated_folder(tmp_path):
    data = os.urandom(3 * 1024 * 1024 + 7)               # several chunks
    res = ss.save_upload(io.BytesIO(data), len(data), "отчёт.pdf", dest_dir=str(tmp_path))
    p = Path(res["path"])
    assert p.read_bytes() == data
    assert p.name == "отчёт.pdf" and res["name"] == "отчёт.pdf" and res["size"] == len(data)
    assert p.parent.name == time.strftime("%Y-%m-%d") and p.parent.parent == tmp_path
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_same_name_twice_keeps_both(tmp_path):
    a = ss.save_upload(io.BytesIO(b"one"), 3, "a.txt", dest_dir=str(tmp_path))
    b = ss.save_upload(io.BytesIO(b"two"), 3, "a.txt", dest_dir=str(tmp_path))
    assert a["path"] != b["path"] and Path(b["path"]).name == "a_2.txt"
    assert Path(a["path"]).read_bytes() == b"one" and Path(b["path"]).read_bytes() == b"two"


def test_a_cut_upload_leaves_nothing(tmp_path):
    with pytest.raises(ValueError):
        ss.save_upload(io.BytesIO(b"short"), 100, "cut.bin", dest_dir=str(tmp_path))
    assert not [f for f in tmp_path.rglob("*") if f.is_file()]


def test_upload_defaults_are_private_inside_the_repo():
    assert ss.UPLOAD_DIR.endswith(os.path.join(".sessions", "uploads"))
    assert ss.UPLOAD_MAX == 100 * 1024 * 1024


# ── over HTTP ───────────────────────────────────────────────────────────────
def _free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "UPLOAD_DIR", str(tmp_path / "up"))
    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), ss.PasteHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(path, body, cookie=True, length=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Content-Type": "application/octet-stream",
             "Content-Length": str(len(body) if length is None else length)}
        if cookie:
            h["Cookie"] = f"{ss.AUTH_COOKIE}={ss._sign_token('admin', int(time.time()) + 60)}"
        c.request("POST", path, body=body, headers=h)
        r = c.getresponse()
        out = (r.status, r.read())
        c.close()
        return out

    post.port = port
    yield post, tmp_path / "up"
    srv.shutdown()


def test_http_upload_returns_the_path(server):
    post, up = server
    status, body = post("/api/upload?name=" + quote("план работ.txt"), b"hello")
    assert status == 200, body
    d = json.loads(body)
    assert d["name"] == "план_работ.txt" and d["size"] == 5
    assert Path(d["path"]).read_bytes() == b"hello" and up in Path(d["path"]).parents


def test_http_upload_needs_the_login(server):
    post, up = server
    status, _ = post("/api/upload?name=a.txt", b"hello", cookie=False)
    assert status == 401
    assert not up.exists() or not any(up.rglob("*.txt"))


def test_http_upload_too_big_is_refused_before_reading(server, monkeypatch):
    post, up = server
    monkeypatch.setattr(ss, "UPLOAD_MAX", 10)
    status, _ = post("/api/upload?name=a.bin", b"x" * 11)
    assert status == 413


def test_http_empty_upload_is_refused(server):
    post, _ = server
    assert post("/api/upload?name=a.bin", b"")[0] == 400


def test_image_paste_still_works(server, tmp_path, monkeypatch):
    """The old route keeps its own contract: raw image bytes, Content-Type image/*."""
    post, _ = server
    monkeypatch.setattr(ss, "PASTE_DIR", str(tmp_path / "paste"))
    c = http.client.HTTPConnection("127.0.0.1", post.port, timeout=5)
    c.request("POST", "/", body=b"\x89PNG\r\n", headers={"Content-Type": "image/png"})
    r = c.getresponse()
    d = json.loads(r.read())
    c.close()
    assert r.status == 200 and d["path"].startswith(str(tmp_path / "paste"))


# ── a real browser against the real server: the bytes arrive intact ─────────
WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def test_real_browser_to_real_server(tmp_path, monkeypatch):
    """The dashboard page and /api/upload on ONE real server (no Playwright route in the
    way, which hides a disk file's body): pick a file in the page → the same bytes on disk."""
    pw = pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(ss, "UPLOAD_DIR", str(tmp_path / "up"))

    class H(ss.PasteHandler):
        def do_GET(self):                        # the dashboard's files; any API → empty
            f = WEB_DIR / self.path.split("?", 1)[0].lstrip("/")
            if f.is_file() and f.parent == WEB_DIR:
                body, ctype = f.read_bytes(), "text/html; charset=utf-8"
            else:
                body, ctype = b"{}", "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    data = os.urandom(2 * 1024 * 1024 + 3)
    src = tmp_path / "фото отпуск.jpg"
    src.write_bytes(data)
    try:
        with pw.sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context()
            ctx.add_cookies([{"name": ss.AUTH_COOKIE, "url": f"http://127.0.0.1:{port}",
                              "value": ss._sign_token("admin", int(time.time()) + 60)}])
            pg = ctx.new_page()
            pg.route("**/fonts.g*/**", lambda r: r.abort())
            pg.goto(f"http://127.0.0.1:{port}/index-lib.html")
            pg.set_input_files("#tFileInput", str(src))
            pg.wait_for_function("() => (document.getElementById('_imgToast')||{}).textContent"
                                 "?.includes('✅')", timeout=15000)
            b.close()
    finally:
        srv.shutdown()
    saved = [f for f in (tmp_path / "up").rglob("*") if f.is_file()]
    assert [f.name for f in saved] == ["фото_отпуск.jpg"]
    assert saved[0].read_bytes() == data
