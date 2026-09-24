"""TDD: session-library API on the status server (the port-3011 Handler).

    GET  /api/library                   -> {max_active, sessions: [{id, name, cwd, created,
                                            last_used, archived, active, attached, status}]}
                                           in library.display_order (loaded first, then recent);
                                           ?archived=1 also lists archived ones
    POST /api/library/new      {name}      -> the new entry (no tmux started)
    POST /api/library/rename   {id, name}
    POST /api/library/archive  {id, archived}
    POST /api/library/close    {id}        -> unload: kill tmux session cs-<id>, nothing else

`active` / `attached` come from tmux (sessions named cs-<id>); `status` is what the terminal
cards already show: off / idle / working, "working" measured the same way (CPU ticks of the
pane's process tree over SAMPLE_INTERVAL).

SAFETY: every tmux call in this file — ours and the server's — goes to a private socket
(AGENTDECK_TMUX_SOCKET=agentdeck-test-*), never the default socket where the live
claude-terminal*/cs-* sessions run; teardown kills only that private server. The registry
is a temp file (AGENTDECK_LIBRARY).
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import uuid as _uuid
from http.server import HTTPServer
from pathlib import Path

import pytest

TERMINAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TERMINAL_DIR))

import library  # noqa: E402

U1 = "aaaa1111-2222-4333-8444-555566667777"
U2 = "bbbb2222-3333-4444-8555-666677778888"
U3 = "c0ffee00-0000-4000-8000-000000000000"
ROW_KEYS = {"id", "name", "cwd", "created", "last_used", "archived", "active", "attached", "status"}


# ── fixture: the real Handler on a free port, private tmux socket, temp registry ──
def _tmux(sock, *args):
    assert sock.startswith("agentdeck-test-"), "tests must never touch the default tmux socket"
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    return subprocess.run(["tmux", "-L", sock, "-f", "/dev/null", *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture
def api(tmp_path, monkeypatch):
    sock = f"agentdeck-test-api-{os.getpid()}-{_uuid.uuid4().hex[:6]}"
    libfile = tmp_path / "library.json"
    monkeypatch.setenv("AGENTDECK_TMUX_SOCKET", sock)
    monkeypatch.setenv("AGENTDECK_LIBRARY", str(libfile))
    monkeypatch.delenv("TMUX", raising=False)
    sys.modules.pop("status_server", None)
    ss = importlib.import_module("status_server")
    srv = HTTPServer(("127.0.0.1", 0), ss.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    A = types.SimpleNamespace(base=f"http://127.0.0.1:{srv.server_address[1]}",
                              sock=sock, lib=str(libfile), ss=ss)
    try:
        yield A
    finally:
        srv.shutdown()
        srv.server_close()
        _tmux(sock, "kill-server")          # the private server only
        sock_file = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / sock
        if sock_file.is_socket():           # kill-server leaves the socket file behind
            sock_file.unlink()


def _seed(A, *entries):
    """entries: (uuid, name, last_used[, archived])"""
    with library.update(A.lib) as L:
        for u, name, last_used, *arch in entries:
            e = library.create(L, name, cwd="/home/ubuntu/pr", now=last_used, uuid=u)
            if arch and arch[0]:
                e["archived"] = True


def _start(A, name, cmd="sleep 600"):
    r = _tmux(A.sock, "new-session", "-d", "-s", name, "-x", "80", "-y", "24", cmd)
    assert r.returncode == 0, r.stderr


def _alive(A, name):
    return _tmux(A.sock, "has-session", "-t", "=" + name).returncode == 0


def _raw_req(A, method, path, data=None, headers=None):
    """-> (status, headers, parsed JSON body)"""
    hdrs = {"Content-Type": "application/json"} if headers is None else headers
    req = urllib.request.Request(A.base + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.headers, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, json.loads(e.read() or b"null")


def _req(A, method, path, body=None, raw=None, headers=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    code, _, out = _raw_req(A, method, path, data, headers)
    return code, out


def get(A, path="/api/library"):
    return _req(A, "GET", path)


def post(A, route, body=None, raw=None):
    return _req(A, "POST", "/api/library/" + route, body if body is not None else {}, raw)


def _by_id(listing):
    return {s["id"]: s for s in listing["sessions"]}


# ── GET /api/library ─────────────────────────────────────────────────────────
def test_get_empty_registry_no_tmux_server(api):
    code, data = get(api)
    assert code == 200
    assert data["max_active"] == library.MAX_ACTIVE and data["sessions"] == []


def test_get_carries_system_stats_like_the_terminal_status(api):
    # the library page shows CPU/RAM from this same poll instead of a second one
    data = get(api)[1]
    assert set(data) == {"max_active", "sessions", "_system"}
    assert set(data["_system"]) == {"cpu_pct", "ram_used_mb", "ram_total_mb", "ram_pct"}
    assert data["_system"]["ram_total_mb"] > 0


def test_get_lists_registry_in_display_order_with_tmux_state(api):
    _seed(api, (U1, "ImmAppeal деплой", 10), (U2, "Налоги", 20), (U3, "Старое", 5))
    _start(api, "cs-c0ffee00")                          # oldest, but loaded -> first
    code, data = get(api)
    assert code == 200 and data["max_active"] == library.MAX_ACTIVE
    assert [s["id"] for s in data["sessions"]] == ["c0ffee00", "bbbb2222", "aaaa1111"]
    for s in data["sessions"]:
        assert set(s) == ROW_KEYS
    by = _by_id(data)
    assert by["c0ffee00"]["active"] is True and by["c0ffee00"]["attached"] is False
    assert by["c0ffee00"]["status"] == "idle"
    assert by["aaaa1111"] == {"id": "aaaa1111", "name": "ImmAppeal деплой", "cwd": "/home/ubuntu/pr",
                              "created": 10, "last_used": 10, "archived": False,
                              "active": False, "attached": False, "status": "off"}


def test_status_working_is_the_terminal_cards_cpu_measure(api, monkeypatch):
    monkeypatch.setattr(api.ss, "SAMPLE_INTERVAL", 0.5)   # headroom on a loaded box
    _seed(api, (U1, "busy", 10), (U2, "quiet", 20))
    _start(api, "cs-aaaa1111", "while :; do :; done")      # burns CPU
    _start(api, "cs-bbbb2222", "sleep 600")
    by = _by_id(get(api)[1])
    assert by["aaaa1111"]["status"] == "working"
    assert by["bbbb2222"]["status"] == "idle"


def test_attached_true_while_a_client_views_the_session(api):
    _seed(api, (U1, "в браузере", 10), (U2, "никто не смотрит", 20))
    _start(api, "cs-aaaa1111")
    _start(api, "cs-bbbb2222")
    # a real tmux client attached to cs-aaaa1111, run from a helper session on the same socket
    _start(api, "viewer", f"env -u TMUX tmux -L {api.sock} attach -t =cs-aaaa1111")
    deadline = time.time() + 5
    while time.time() < deadline:
        # list-sessions -F, not display-message: that returns '' here in tmux 3.2a
        # (and segfaults the server on a missing target)
        r = _tmux(api.sock, "list-sessions", "-F", "#{session_name}\t#{session_attached}")
        if "cs-aaaa1111\t1" in r.stdout.splitlines():
            break
        time.sleep(0.1)
    by = _by_id(get(api)[1])
    assert by["aaaa1111"]["attached"] is True and by["aaaa1111"]["active"] is True
    assert by["bbbb2222"]["attached"] is False and by["bbbb2222"]["active"] is True
    assert set(by) == {"aaaa1111", "bbbb2222"}           # "viewer" is not a library session


def test_tmux_sessions_outside_the_registry_are_ignored(api):
    _seed(api, (U1, "есть в реестре", 10))
    for name in ("cs-deadbeef", "claude-terminal-3", "cs-AAAA1111", "cs-aaaa1111x"):
        _start(api, name)
    data = get(api)[1]
    assert [s["id"] for s in data["sessions"]] == ["aaaa1111"]
    assert data["sessions"][0]["active"] is False       # "cs-aaaa1111x" is not cs-aaaa1111


def test_archived_hidden_by_default_listed_with_query(api):
    _seed(api, (U1, "живая", 10), (U2, "в архиве", 20, True))
    assert [s["id"] for s in get(api)[1]["sessions"]] == ["aaaa1111"]
    by = _by_id(get(api, "/api/library?archived=1")[1])
    assert set(by) == {"aaaa1111", "bbbb2222"} and by["bbbb2222"]["archived"] is True


# ── POST /api/library/new ────────────────────────────────────────────────────
def test_new_creates_entry_without_starting_tmux(api, monkeypatch):
    monkeypatch.setenv("AGENTDECK_WORKDIR", "/srv/work")
    _seed(api, (U1, "старая", 10))
    t0 = int(time.time())
    code, e = post(api, "new", {"name": "ImmAppeal деплой"})
    assert code == 200
    assert set(e) == ROW_KEYS and library.valid_id(e["id"])
    assert e["name"] == "ImmAppeal деплой" and e["cwd"] == "/srv/work"
    assert e["archived"] is False and e["active"] is False and e["status"] == "off"
    assert t0 <= e["created"] == e["last_used"] <= int(time.time()) + 1
    stored = library.find(library.load(api.lib), e["id"])
    assert stored["uuid"].replace("-", "")[:8] == e["id"] and stored["name"] == "ImmAppeal деплой"
    assert not _alive(api, "cs-" + e["id"])
    # newest inactive first, the older one after it
    assert [s["id"] for s in get(api)[1]["sessions"]] == [e["id"], "aaaa1111"]


def test_new_default_cwd_is_the_folder_above_this_repo(api, monkeypatch):
    monkeypatch.delenv("AGENTDECK_WORKDIR", raising=False)
    code, e = post(api, "new", {"name": "x"})
    assert code == 200 and e["cwd"] == str(TERMINAL_DIR.parent)


def test_new_blank_or_missing_name_gets_a_dated_default(api):
    for body in ({}, {"name": "   "}):
        code, e = post(api, "new", body)
        assert code == 200 and e["name"].startswith("Тема ")


def test_new_collapses_whitespace_and_drops_control_chars(api):
    code, e = post(api, "new", {"name": "  Налоги\n2026 \x1b[31mкрасное\t "})
    assert code == 200 and e["name"] == "Налоги 2026 [31mкрасное"


def test_new_rejects_bad_names(api):
    for body in ({"name": 5}, {"name": ["a"]}, {"name": "я" * 201}):
        code, err = post(api, "new", body)
        assert code == 400 and "error" in err, body
    assert library.load(api.lib)["sessions"] == []


# ── POST /api/library/rename ─────────────────────────────────────────────────
def test_rename(api):
    _seed(api, (U1, "старое имя", 10))
    code, e = post(api, "rename", {"id": "aaaa1111", "name": " Новое \n имя "})
    assert code == 200 and e["id"] == "aaaa1111" and e["name"] == "Новое имя"
    assert set(e) == ROW_KEYS
    assert library.find(library.load(api.lib), "aaaa1111")["name"] == "Новое имя"


def test_rename_errors(api):
    _seed(api, (U1, "имя", 10))
    assert post(api, "rename", {"id": "aaaa1111", "name": "  "})[0] == 400
    assert post(api, "rename", {"id": "aaaa1111"})[0] == 400
    assert post(api, "rename", {"id": "aaaa1111", "name": "я" * 201})[0] == 400
    assert post(api, "rename", {"id": "deadbeef", "name": "x"})[0] == 404
    assert post(api, "rename", {"id": "../x", "name": "x"})[0] == 400
    assert library.find(library.load(api.lib), "aaaa1111")["name"] == "имя"


# ── POST /api/library/archive ────────────────────────────────────────────────
def test_archive_and_unarchive(api):
    _seed(api, (U1, "тема", 10))
    code, e = post(api, "archive", {"id": "aaaa1111"})             # default: archive
    assert code == 200 and e["archived"] is True
    assert library.find(library.load(api.lib), "aaaa1111")["archived"] is True
    code, e = post(api, "archive", {"id": "aaaa1111", "archived": False})
    assert code == 200 and e["archived"] is False
    assert library.find(library.load(api.lib), "aaaa1111")["archived"] is False


def test_archive_errors(api):
    _seed(api, (U1, "тема", 10))
    assert post(api, "archive", {"id": "aaaa1111", "archived": "yes"})[0] == 400
    assert post(api, "archive", {"id": "deadbeef"})[0] == 404
    assert post(api, "archive", {"id": "AAAA1111"})[0] == 400
    assert library.find(library.load(api.lib), "aaaa1111")["archived"] is False


# ── POST /api/library/close ──────────────────────────────────────────────────
def test_close_kills_only_its_own_session_and_keeps_the_entry(api, monkeypatch):
    monkeypatch.setattr(api.ss, "CLOSE_QUIET_SECONDS", 0)     # "no output for 30 min"
    _seed(api, (U1, "выгрузить", 10), (U2, "соседка", 20))
    _start(api, "cs-aaaa1111")
    _start(api, "cs-bbbb2222")
    code, e = post(api, "close", {"id": "aaaa1111"})
    assert code == 200 and e["id"] == "aaaa1111" and e["killed"] is True
    assert e["active"] is False and e["status"] == "off"
    assert not _alive(api, "cs-aaaa1111") and _alive(api, "cs-bbbb2222")
    assert library.find(library.load(api.lib), "aaaa1111") is not None     # topic stays
    by = _by_id(get(api)[1])
    assert by["aaaa1111"]["active"] is False and by["bbbb2222"]["active"] is True
    # idempotent: closing an unloaded topic is fine
    code, e = post(api, "close", {"id": "aaaa1111"})
    assert code == 200 and e["killed"] is False


def test_close_matches_the_session_name_exactly_not_by_prefix(api):
    _seed(api, (U1, "не загружена", 10))
    _start(api, "cs-aaaa1111x")                   # tmux would prefix-match "-t cs-aaaa1111"
    code, e = post(api, "close", {"id": "aaaa1111"})
    assert code == 200 and e["killed"] is False
    assert _alive(api, "cs-aaaa1111x")


def test_close_rejects_bad_and_unknown_ids_and_kills_nothing(api):
    _seed(api, (U1, "тема", 10))
    _start(api, "cs-aaaa1111")
    _start(api, "cs-deadbeef")                    # a session, but not a registry topic
    for body in ({"id": "; tmux kill-server"}, {"id": "../x"}, {"id": "AAAA1111"},
                 {"id": "aaaa111"}, {"id": 12345678}, {"id": None}, {}):
        code, err = post(api, "close", body)
        assert code == 400 and "error" in err, body
    code, err = post(api, "close", {"id": "deadbeef"})
    assert code == 404 and "error" in err
    assert _alive(api, "cs-aaaa1111") and _alive(api, "cs-deadbeef")


# ── malformed requests ───────────────────────────────────────────────────────
def test_malformed_requests(api):
    assert post(api, "new", raw=b"not json")[0] == 400
    assert post(api, "new", raw=b"[1, 2]")[0] == 400
    assert post(api, "explode", {"id": "aaaa1111"})[0] == 404
    assert _req(api, "POST", "/api/library", {})[0] == 404
    assert get(api, "/api/library/new")[0] == 404
    assert library.load(api.lib)["sessions"] == []


def test_new_with_empty_body_is_a_default_topic(api):
    code, e = post(api, "new", raw=b"")
    assert code == 200 and e["name"].startswith("Тема ")


# ── safety: the API only ever talks to the configured tmux socket ────────────
def test_every_tmux_call_goes_to_the_configured_socket(api, monkeypatch):
    real_run = subprocess.run
    calls = []

    def spy(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "tmux":
            calls.append(list(cmd))
        return real_run(cmd, *a, **kw)

    _seed(api, (U1, "a", 10), (U2, "b", 20))
    _start(api, "cs-aaaa1111")
    monkeypatch.setattr(api.ss.subprocess, "run", spy)
    get(api)
    code, e = post(api, "new", {"name": "c"})
    post(api, "rename", {"id": "aaaa1111", "name": "a2"})
    post(api, "archive", {"id": "bbbb2222"})
    post(api, "close", {"id": "aaaa1111"})
    assert calls, "the API made no tmux calls at all"
    for c in calls:
        assert c[1:3] == ["-L", api.sock], c
        assert "new-session" not in c, c          # the API never starts sessions


# ── CSRF: a page on another site must not be able to drive the library API ──
def test_post_requires_json_content_type(api):
    body = json.dumps({"name": "x"}).encode()
    for hdrs in ({}, {"Content-Type": "text/plain"},
                 {"Content-Type": "application/x-www-form-urlencoded"}):
        code, _, err = _raw_req(api, "POST", "/api/library/new", body, hdrs)
        assert code == 415 and "error" in err, hdrs
    code, _, e = _raw_req(api, "POST", "/api/library/new", body,
                          {"Content-Type": "application/json; charset=utf-8"})
    assert code == 200 and e["name"] == "x"
    assert len(library.load(api.lib)["sessions"]) == 1


def test_post_rejects_a_foreign_origin(api, monkeypatch):
    monkeypatch.delenv("AGENTDECK_ORIGIN", raising=False)
    body = json.dumps({"name": "x"}).encode()
    for origin in ("https://evil.example", "null", "http://agents.reimake.com",
                   "https://agents.reimake.com.evil.example"):
        code, _, err = _raw_req(api, "POST", "/api/library/new", body,
                                {"Content-Type": "application/json", "Origin": origin})
        assert code == 403 and "error" in err, origin
    assert library.load(api.lib)["sessions"] == []
    code, _, _ = _raw_req(api, "POST", "/api/library/new", body,
                          {"Content-Type": "application/json", "Origin": "https://agents.reimake.com"})
    assert code == 200


def test_allowed_origin_is_configurable(api, monkeypatch):
    monkeypatch.setenv("AGENTDECK_ORIGIN", "https://deck.test")
    body = json.dumps({"name": "x"}).encode()
    hdr = lambda o: {"Content-Type": "application/json", "Origin": o}
    assert _raw_req(api, "POST", "/api/library/new", body, hdr("https://deck.test"))[0] == 200
    assert _raw_req(api, "POST", "/api/library/new", body, hdr("https://agents.reimake.com"))[0] == 403


def test_library_responses_have_no_wildcard_cors(api):
    _seed(api, (U1, "a", 10))
    for method, path, data in (("GET", "/api/library", None),
                               ("POST", "/api/library/rename", json.dumps({"id": "aaaa1111", "name": "b"}).encode()),
                               ("POST", "/api/library/new", b"not json"),
                               ("OPTIONS", "/api/library/new", None)):
        req = urllib.request.Request(api.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                h = r.headers
        except urllib.error.HTTPError as e:
            h = e.headers
        assert h.get("Access-Control-Allow-Origin") is None, (method, path)


# ── close refuses a topic someone is using, unless forced ────────────────────
def test_close_refuses_a_session_with_recent_output(api):
    _seed(api, (U1, "свежий вывод", 10))
    _start(api, "cs-aaaa1111")                    # just started = output right now
    code, err = post(api, "close", {"id": "aaaa1111"})
    assert code == 409 and "error" in err
    assert _alive(api, "cs-aaaa1111")
    code, e = post(api, "close", {"id": "aaaa1111", "force": True})
    assert code == 200 and e["killed"] is True and not _alive(api, "cs-aaaa1111")


def test_close_refuses_an_attached_session_even_when_quiet(api, monkeypatch):
    monkeypatch.setattr(api.ss, "CLOSE_QUIET_SECONDS", 0)
    _seed(api, (U1, "смотрят", 10))
    _start(api, "cs-aaaa1111")
    _start(api, "viewer", f"env -u TMUX tmux -L {api.sock} attach -t =cs-aaaa1111")
    deadline = time.time() + 5
    while time.time() < deadline:
        r = _tmux(api.sock, "list-sessions", "-F", "#{session_name}\t#{session_attached}")
        if "cs-aaaa1111\t1" in r.stdout.splitlines():
            break
        time.sleep(0.1)
    code, err = post(api, "close", {"id": "aaaa1111"})
    assert code == 409 and _alive(api, "cs-aaaa1111")
    assert post(api, "close", {"id": "aaaa1111", "force": "yes"})[0] == 400   # force must be a bool
    code, e = post(api, "close", {"id": "aaaa1111", "force": True})
    assert code == 200 and e["killed"] is True


def test_close_quiet_detached_session_without_force(api, monkeypatch):
    monkeypatch.setattr(api.ss, "CLOSE_QUIET_SECONDS", 0)
    _seed(api, (U1, "тихая", 10))
    _start(api, "cs-aaaa1111")
    code, e = post(api, "close", {"id": "aaaa1111"})
    assert code == 200 and e["killed"] is True


def test_close_quiet_window_is_thirty_minutes(api):
    assert api.ss.CLOSE_QUIET_SECONDS == 30 * 60


# ── page auto-reload watches the file the page was served from ───────────────
def test_page_version_watches_index_lib_for_the_library_page(api, tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("a")
    (web / "index-lib.html").write_text("b")
    os.utime(web / "index.html", (1000, 1000))
    os.utime(web / "index-lib.html", (2000, 2000))
    monkeypatch.setattr(api.ss, "WEB_DIR", str(web))
    srv = HTTPServer(("127.0.0.1", 0), api.ss.LiveHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    try:
        def version(headers):
            with urllib.request.urlopen(urllib.request.Request(base, headers=headers), timeout=5) as r:
                return float(r.read())
        assert version({}) == 1000
        assert version({"Referer": "https://agents.reimake.com/"}) == 1000
        assert version({"Referer": "https://agents.reimake.com/index-lib.html"}) == 2000
        assert version({"Referer": "https://agents.reimake.com/index-lib.html?x=1#y"}) == 2000
        assert version({"Referer": "https://agents.reimake.com/../../etc/passwd"}) == 1000
    finally:
        srv.shutdown()
        srv.server_close()
