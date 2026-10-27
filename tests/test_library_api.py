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
ROW_KEYS = {"id", "name", "cwd", "created", "last_used", "archived", "active", "attached", "status", "legacy_path", "pos"}


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
    # Claude's live-process files (convo_sync) and transcripts: temp, never the real ones
    (tmp_path / "claude-sessions").mkdir()
    monkeypatch.setenv("AGENTDECK_CLAUDE_SESSIONS", str(tmp_path / "claude-sessions"))
    monkeypatch.setenv("AGENTDECK_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.delenv("TMUX", raising=False)
    sys.modules.pop("status_server", None)
    ss = importlib.import_module("status_server")
    srv = HTTPServer(("127.0.0.1", 0), ss.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    A = types.SimpleNamespace(base=f"http://127.0.0.1:{srv.server_address[1]}",
                              sock=sock, lib=str(libfile), ss=ss,
                              claude_sessions=tmp_path / "claude-sessions")
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
    assert set(data) == {"max_active", "sessions", "_system", "shell"}
    assert set(data["_system"]) == {"cpu_pct", "ram_used_mb", "ram_total_mb", "ram_pct"}
    assert data["_system"]["ram_total_mb"] > 0


def test_get_lists_registry_in_display_order_with_tmux_state(api):
    _seed(api, (U1, "ImmAppeal деплой", 10), (U2, "Налоги", 20), (U3, "Старое", 5))
    _start(api, "cs-c0ffee00")                          # loaded, but the list doesn't jump
    code, data = get(api)
    assert code == 200 and data["max_active"] == library.MAX_ACTIVE
    # by place: each new one went on top — c0ffee00 (seeded last), bbbb, aaaa
    assert [s["id"] for s in data["sessions"]] == ["c0ffee00", "bbbb2222", "aaaa1111"]
    for s in data["sessions"]:
        assert set(s) == ROW_KEYS
    by = _by_id(data)
    assert by["c0ffee00"]["active"] is True and by["c0ffee00"]["attached"] is False
    assert by["c0ffee00"]["status"] == "idle"
    assert by["aaaa1111"] == {"id": "aaaa1111", "name": "ImmAppeal деплой", "cwd": "/home/ubuntu/pr",
                              "created": 10, "last_used": 10, "archived": False,
                              "active": False, "attached": False, "status": "off", "legacy_path": None,
                              "pos": by["aaaa1111"]["pos"]}
    assert isinstance(by["aaaa1111"]["pos"], int)       # every terminal has a place


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
        assert code == 200 and e["name"].startswith("Terminal ")


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
    assert code == 200 and e["name"].startswith("Terminal ")


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
    own = api.base                                   # http://127.0.0.1:<port> = own scheme+Host
    for origin in ("https://evil.example", "null", "", own.replace("http://", "https://"),
                   own + ".evil.example", own + "/", "https://agents.reimake.com"):
        code, _, err = _raw_req(api, "POST", "/api/library/new", body,
                                {"Content-Type": "application/json", "Origin": origin})
        assert code == 403 and "error" in err, origin
    assert library.load(api.lib)["sessions"] == []
    code, _, _ = _raw_req(api, "POST", "/api/library/new", body,
                          {"Content-Type": "application/json", "Origin": own})
    assert code == 200


def test_default_origin_is_the_requests_own_scheme_and_host(api, monkeypatch):
    """No AGENTDECK_ORIGIN (a fresh install, e.g. docker behind Caddy): the only accepted
    Origin is the request's own scheme (X-Forwarded-Proto from the proxy, else http) + Host."""
    monkeypatch.delenv("AGENTDECK_ORIGIN", raising=False)
    body = json.dumps({"name": "x"}).encode()

    def req(origin, host, proto=None):
        h = {"Content-Type": "application/json", "Origin": origin, "Host": host}
        if proto:
            h["X-Forwarded-Proto"] = proto
        return _raw_req(api, "POST", "/api/library/new", body, h)[0]

    assert req("https://deck.example:8443", "deck.example:8443", "https") == 200
    assert req("http://deck.example:8443", "deck.example:8443", "https") == 403
    assert req("https://deck.example:8443", "deck.example:8443") == 403     # no proto -> http
    assert req("http://10.0.0.5:8765", "10.0.0.5:8765") == 200
    assert req("http://10.0.0.5:8765", "10.0.0.5:8766") == 403
    assert req("https://other.example", "deck.example", "https") == 403
    assert req("https://deck.example", "deck.example", "https,http") == 403  # odd proto -> refuse
    assert len(library.load(api.lib)["sessions"]) == 2


def test_default_origin_refuses_without_a_host(api, monkeypatch):
    monkeypatch.delenv("AGENTDECK_ORIGIN", raising=False)
    ss = api.ss
    assert ss.lib_origin_ok("http://x", {"Host": ""}) is False
    assert ss.lib_origin_ok("http://x", {}) is False
    assert ss.lib_origin_ok("http://x", {"Host": "x"}) is True
    assert "agents.reimake.com" not in repr(getattr(ss, "DEFAULT_ORIGIN", ""))


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


# ── topics still running in a legacy numbered terminal (after migration) ────
def test_topic_running_in_legacy_terminal_shows_active_with_its_path(api):
    # Migration leaves busy claude-terminal-N sessions untouched; their topic must
    # not look unloaded, and opening it must go to that terminal (a second Claude
    # on the same uuid is refused anyway).
    _seed(api, (U1, "PIPE", 100), (U2, "other", 50))
    _start(api, "claude-terminal-2", f"bash -c 'sleep 600; : --resume {U1}'")
    _start(api, "claude-terminal", f"bash -c 'sleep 600; : --session-id {U2}'")
    rows = {r["id"]: r for r in get(api)[1]["sessions"]}
    a, b = rows[library.id_from_uuid(U1)], rows[library.id_from_uuid(U2)]
    assert a["active"] is True and a["legacy_path"] == "/terminal2/"
    assert b["active"] is True and b["legacy_path"] == "/terminal/"


def test_library_session_has_no_legacy_path(api):
    _seed(api, (U1, "PIPE", 100))
    _start(api, "cs-" + library.id_from_uuid(U1))
    row = get(api)[1]["sessions"][0]
    assert row["active"] is True and row.get("legacy_path") is None


# ── transcripts on disk (Claude's projects dir, temp) ───────────────────────
@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("AGENTDECK_CLAUDE_PROJECTS", str(root))
    d = root / "-home-ubuntu-pr"
    d.mkdir(parents=True)
    return d


def test_the_old_delete_route_is_gone(api, projects):
    # the archived-only Delete (transcript into .sessions/trash/) was replaced by the
    # trash (2026-10-23); its one caller was the page's Delete button
    _seed(api, (U1, "old", 10, True))
    (projects / f"{U1}.jsonl").write_text("talk")
    code, err = post(api, "delete", {"id": "aaaa1111"})
    assert code == 404 and "error" in err
    assert library.find(library.load(api.lib), "aaaa1111") is not None
    assert (projects / f"{U1}.jsonl").read_text() == "talk"


# ── speed: one legacy scan per request, no pgrep per process ─────────────────
def test_listing_spawns_no_pgrep_and_scans_legacy_once(api, monkeypatch):
    _seed(api, (U1, "PIPE", 100), (U2, "live", 50))
    _start(api, "claude-terminal-2", f"bash -c 'sleep 600; : --resume {U1}'")
    _start(api, "cs-bbbb2222", "bash -c 'sleep 600'")
    real_run = subprocess.run
    pgreps = []

    def spy(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "pgrep":
            pgreps.append(list(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(api.ss.subprocess, "run", spy)
    real_legacy = api.ss.lib_legacy
    scans = []
    monkeypatch.setattr(api.ss, "lib_legacy", lambda *a, **k: scans.append(1) or real_legacy(*a, **k))
    for path in ("/api/library", "/api/library?archived=1"):
        scans.clear()
        code, data = get(api, path)
        assert code == 200
        assert len(scans) == 1, path
    rows = _by_id(data)
    assert rows["aaaa1111"]["legacy_path"] == "/terminal2/"   # still found without pgrep
    assert rows["bbbb2222"]["active"] is True
    assert pgreps == []


def test_child_map_matches_the_process_tree():
    ss = importlib.import_module("status_server")
    p = subprocess.Popen(["bash", "-c", "sleep 30 & wait"])
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not ss.child_map().get(p.pid):
            time.sleep(0.05)
        kids = ss.child_map().get(p.pid, [])
        assert len(kids) == 1
        with open(f"/proc/{kids[0]}/cmdline", "rb") as f:
            assert f.read().startswith(b"sleep")
    finally:
        p.kill()
        p.wait()


# ── POST /api/library/reorder {ids} ──────────────────────────────────────────
def test_reorder_rewrites_the_display_order(api):
    _seed(api, (U1, "a", 10), (U2, "b", 20), (U3, "c", 30))
    c3 = library.id_from_uuid(U3)
    assert [s["id"] for s in get(api)[1]["sessions"]] == [c3, "bbbb2222", "aaaa1111"]
    code, out = post(api, "reorder", {"ids": ["aaaa1111", c3, "bbbb2222"]})
    assert code == 200 and out == {"ok": True}
    assert [s["id"] for s in get(api)[1]["sessions"]] == ["aaaa1111", c3, "bbbb2222"]
    assert get(api)[1]["sessions"][0]["pos"] == 0


def test_reorder_validates_ids(api):
    _seed(api, (U1, "a", 10), (U2, "b", 20))
    before = [e["pos"] for e in library.load(api.lib)["sessions"]]
    for body in ({}, {"ids": "aaaa1111"}, {"ids": []}, {"ids": ["aaaa1111", "../x"]},
                 {"ids": ["aaaa1111", 5]}, {"ids": ["aaaa1111", "deadbeef"]},
                 {"ids": ["aaaa1111", "aaaa1111"]}):
        code, err = post(api, "reorder", body)
        assert code == 400 and "error" in err, body
    assert [e["pos"] for e in library.load(api.lib)["sessions"]] == before


def test_reorder_has_the_same_csrf_checks(api, monkeypatch):
    monkeypatch.delenv("AGENTDECK_ORIGIN", raising=False)
    _seed(api, (U1, "a", 10), (U2, "b", 20))
    before = [e["pos"] for e in library.load(api.lib)["sessions"]]
    body = json.dumps({"ids": ["aaaa1111", "bbbb2222"]}).encode()
    assert _raw_req(api, "POST", "/api/library/reorder", body, {"Content-Type": "text/plain"})[0] == 415
    assert _raw_req(api, "POST", "/api/library/reorder", body,
                    {"Content-Type": "application/json", "Origin": "https://evil.example"})[0] == 403
    assert [e["pos"] for e in library.load(api.lib)["sessions"]] == before


# ── Archive unloads at once unless the terminal is working (owner 2026-09-25:
#    "When I press Archive, the terminal should unload"; top rule: never
#    interrupt work — printing in the last ~15 s, a live background task, an
#    unexpired hold marker or CPU status 'working' keep it loaded, and
#    idle_reaper unloads it once quiet). An open tab does NOT keep it. ──
@pytest.fixture
def quiet(api, monkeypatch):
    """A loaded terminal that is not working: last output 60 s ago, no CPU."""
    monkeypatch.setattr(api.ss, "lib_last_output", lambda name: time.time() - 60)
    monkeypatch.setattr(api.ss, "sample_working", lambda pids: {k: False for k in pids})
    return api


def test_archive_unloads_a_loaded_quiet_terminal_at_once(quiet):
    api = quiet
    _seed(api, (U1, "в архив", 10), (U2, "соседка", 20))
    _start(api, "cs-aaaa1111")
    _start(api, "cs-bbbb2222")
    code, e = post(api, "archive", {"id": "aaaa1111"})
    assert code == 200, e
    assert e["archived"] is True and e["active"] is False
    assert e["unloaded"] is True and e["unload_pending"] is False
    assert not _alive(api, "cs-aaaa1111")
    assert _alive(api, "cs-bbbb2222")                        # only its own session


def test_archive_unloads_even_with_a_tab_open(quiet, monkeypatch):
    api = quiet
    _seed(api, (U1, "во вкладке", 10))
    _start(api, "cs-aaaa1111")
    real = api.ss.lib_live
    monkeypatch.setattr(api.ss, "lib_live",
                        lambda: {k: dict(v, attached=True) for k, v in real().items()})
    code, e = post(api, "archive", {"id": "aaaa1111"})
    assert code == 200 and e["unloaded"] is True
    assert not _alive(api, "cs-aaaa1111")


def test_archive_keeps_a_terminal_that_printed_in_the_last_seconds(quiet, monkeypatch):
    api = quiet
    _seed(api, (U1, "печатает", 10))
    _start(api, "cs-aaaa1111")
    monkeypatch.setattr(api.ss, "lib_last_output", lambda name: time.time() - 5)
    code, e = post(api, "archive", {"id": "aaaa1111"})
    assert code == 200 and e["archived"] is True
    assert e["unloaded"] is False and e["unload_pending"] is True and e["reason"] == "printing"
    assert e["active"] is True and _alive(api, "cs-aaaa1111")
    assert library.find(library.load(api.lib), "aaaa1111")["archived"] is True


def test_archive_busy_window_is_about_fifteen_seconds(quiet, monkeypatch):
    api = quiet
    assert api.ss.ARCHIVE_BUSY_SECONDS == 15
    _seed(api, (U1, "затихла", 10))
    _start(api, "cs-aaaa1111")
    monkeypatch.setattr(api.ss, "lib_last_output", lambda name: time.time() - 20)
    assert post(api, "archive", {"id": "aaaa1111"})[1]["unloaded"] is True


def test_archive_unknown_output_time_counts_as_working(quiet, monkeypatch):
    api = quiet
    _seed(api, (U1, "неясно", 10))
    _start(api, "cs-aaaa1111")
    monkeypatch.setattr(api.ss, "lib_last_output", lambda name: None)
    e = post(api, "archive", {"id": "aaaa1111"})[1]
    assert e["unloaded"] is False and e["reason"] == "printing"
    assert _alive(api, "cs-aaaa1111")


def test_archive_keeps_a_terminal_with_a_hold_marker(quiet):
    api = quiet
    _seed(api, (U1, "таймер", 10))
    _start(api, "cs-aaaa1111")
    library.set_hold("aaaa1111", time.time() + 600, api.lib)
    e = post(api, "archive", {"id": "aaaa1111"})[1]
    assert e["unloaded"] is False and e["unload_pending"] is True and e["reason"] == "hold"
    assert _alive(api, "cs-aaaa1111")


def test_archive_ignores_an_expired_hold_marker(quiet):
    api = quiet
    _seed(api, (U1, "старый таймер", 10))
    _start(api, "cs-aaaa1111")
    library.set_hold("aaaa1111", time.time() - 5, api.lib)
    assert post(api, "archive", {"id": "aaaa1111"})[1]["unloaded"] is True


def test_archive_keeps_a_terminal_with_a_background_task(quiet, monkeypatch):
    api = quiet
    _seed(api, (U1, "фон", 10))
    _start(api, "cs-aaaa1111")
    monkeypatch.setattr(api.ss.idle_reaper, "tree_has_task_output", lambda pid: True)
    e = post(api, "archive", {"id": "aaaa1111"})[1]
    assert e["unloaded"] is False and e["reason"] == "background-task"
    assert _alive(api, "cs-aaaa1111")


def test_archive_keeps_a_terminal_whose_status_is_working(quiet, monkeypatch):
    api = quiet
    _seed(api, (U1, "CPU", 10))
    _start(api, "cs-aaaa1111")
    monkeypatch.setattr(api.ss, "sample_working", lambda pids: {k: True for k in pids})
    e = post(api, "archive", {"id": "aaaa1111"})[1]
    assert e["unloaded"] is False and e["reason"] == "working" and e["status"] == "working"
    assert _alive(api, "cs-aaaa1111")


def test_archive_of_an_unloaded_terminal_has_nothing_pending(api):
    _seed(api, (U1, "выгружена", 10))
    code, e = post(api, "archive", {"id": "aaaa1111"})
    assert code == 200 and e["unload_pending"] is False and e["active"] is False
    assert e["unloaded"] is False and e["reason"] == "not-loaded"


def test_restore_never_unloads(api, monkeypatch):
    _seed(api, (U1, "вернуть", 10, True))
    _start(api, "cs-aaaa1111")
    monkeypatch.setattr(api.ss, "lib_last_output", lambda name: time.time() - 1000)
    code, e = post(api, "archive", {"id": "aaaa1111", "archived": False})
    assert code == 200 and e["archived"] is False
    assert _alive(api, "cs-aaaa1111")


def test_archive_quiet_window_is_the_delete_window(api):
    assert api.ss.DELETE_QUIET_SECONDS == 120


# ── one number per terminal (convo_sync): the listing follows the conversation ──
UNEW = "0badc0de-1111-4222-8333-444455556666"


def _pane_pid(A, name):
    r = _tmux(A.sock, "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}")
    for line in r.stdout.splitlines():
        n, _, pid = line.partition("\t")
        if n == name:
            return int(pid)
    return None


def _claude_runs(A, name, u):
    """Claude's pid file (~/.claude/sessions/<pid>.json) for the pane of `name`:
    the conversation live in it is `u` (as after the consent relaunch or /clear)."""
    pid = _pane_pid(A, name)
    with open(f"/proc/{pid}/stat") as f:
        start = f.read().rsplit(")", 1)[1].split()[19]
    (A.claude_sessions / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": u, "procStart": start, "kind": "interactive",
         "entrypoint": "cli", "tmux": name + ":@0.%0", "cwd": "/home/ubuntu/pr"}))


def test_listing_follows_a_conversation_switch(api):
    _seed(api, (U1, "consent", 10), (U2, "other", 20))
    _start(api, "cs-aaaa1111")
    _claude_runs(api, "cs-aaaa1111", UNEW)     # never had a transcript: the consent case
    code, data = get(api)
    assert code == 200
    by = _by_id(data)
    assert set(by) == {"0badc0de", "bbbb2222"}
    row = by["0badc0de"]
    assert row["active"] is True and row["name"] == "consent"
    assert row["prev_id"] == "aaaa1111" and row["aliases"] == ["aaaa1111"]
    assert isinstance(row["switched_at"], int)
    assert set(by["bbbb2222"]) == ROW_KEYS           # untouched rows: the same keys as ever
    assert _alive(api, "cs-0badc0de") and not _alive(api, "cs-aaaa1111")


def test_close_by_a_vanished_number_acts_on_the_renamed_terminal(api, monkeypatch):
    monkeypatch.setattr(api.ss, "CLOSE_QUIET_SECONDS", 0)
    _seed(api, (U1, "consent", 10))
    _start(api, "cs-aaaa1111")
    _claude_runs(api, "cs-aaaa1111", UNEW)
    # no listing in between: close itself syncs first, then maps the old number
    code, e = post(api, "close", {"id": "aaaa1111"})
    assert code == 200 and e["id"] == "0badc0de" and e["killed"] is True
    assert not _alive(api, "cs-0badc0de") and not _alive(api, "cs-aaaa1111")


def test_rename_and_archive_by_a_vanished_number(api):
    _seed(api, (U1, "consent", 10))
    _start(api, "cs-aaaa1111")
    _claude_runs(api, "cs-aaaa1111", UNEW)
    get(api)                                          # the switch happens here
    code, e = post(api, "rename", {"id": "aaaa1111", "name": "renamed"})
    assert code == 200 and e["id"] == "0badc0de" and e["name"] == "renamed"
    code, e = post(api, "archive", {"id": "aaaa1111", "archived": True})
    assert code == 200 and e["id"] == "0badc0de" and e["archived"] is True


def test_a_broken_sync_never_breaks_the_listing(api, monkeypatch):
    _seed(api, (U1, "a", 10))
    import convo_sync

    def boom(**kw):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(convo_sync, "sync", boom)
    code, data = get(api)
    assert code == 200 and [s["id"] for s in data["sessions"]] == ["aaaa1111"]


# ── POST /api/library/restart: ⟳ — Claude starts again in the same conversation ──
# (owner 2026-10-23: new launch settings such as CLAUDE_CODE_SCROLL_SPEED take effect
# only on a fresh start, and a hung Claude needs reviving.) Top rule: never interrupt
# work — a busy terminal (lib_busy_reason) is refused with 409 + reason unless
# force=true; an open tab is not a reason. The terminal comes back loaded through the
# real `library_cli ensure` (what opening it does), with a fake claude in a temp HOME.
from tests.test_library_cli import _procs_with_home, wait_for  # noqa: E402

import library_cli  # noqa: E402  (claude_processes: the one-claude-per-conversation test)

RESTART_FAKE_CLAUDE = r"""#!/bin/bash
# stands in for the claude CLI: records each start, then idles looking like claude
echo "$AGENTDECK_SESSION|$*" >> "$HOME/claude-calls.log"
echo "start $$" >> "$HOME/lifecycle.log"
if [ -f "$HOME/dying" ]; then             # after /clear: see DYING_CLAUDE below
  exec -a claude /bin/bash "$HOME/dying.sh" --resume 00000000-0000-4000-8000-000000000000
fi
if [ -f "$HOME/stubborn" ]; then          # a hung Claude: ignores the hangup and SIGTERM
  exec -a claude /bin/bash -c 'trap "" HUP TERM; while :; do sleep 0.2; done' claude "$@"
fi
exec -a claude /bin/bash -c 'sleep 600; :' claude "$@"
"""


@pytest.fixture
def rs(api, tmp_path, monkeypatch):
    """api + a fake claude (CLAUDE_BIN, temp HOME) that `library_cli ensure` starts,
    and the terminal quiet — last output 60 s ago, no CPU — unless a test says otherwise."""
    home, work, bindir = tmp_path / "home", tmp_path / "work", tmp_path / "bin"
    for d in (home, work, bindir):
        d.mkdir()
    claude = bindir / "claude"
    claude.write_text(RESTART_FAKE_CLAUDE)
    claude.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_BIN", str(claude))
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    monkeypatch.setenv("AGENTDECK_WORKDIR", str(work))
    monkeypatch.setenv("OPEN_SESSION_PAUSE", "0")
    monkeypatch.setattr(api.ss, "lib_last_output", lambda name: time.time() - 60)
    monkeypatch.setattr(api.ss, "sample_working", lambda pids: {k: False for k in pids})
    api.home, api.work, api.procs = home, work, []
    try:
        yield api
    finally:
        for p in api.procs:                      # helpers this test started (saved Popens)
            p.kill()
            p.wait(timeout=5)
        _tmux(api.sock, "kill-server")
        # fake claudes that outlived their tmux server all carry HOME=<this test's home>
        for pid in _procs_with_home(home):
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def _rs_seed(A, u=U1, name="перезапуск", archived=False):
    """A topic in A.work with a transcript, so every start is `--resume <uuid>`."""
    with library.update(A.lib) as L:
        e = library.create(L, name, cwd=str(A.work), now=int(time.time()), uuid=u)
        e["archived"] = archived
    t = Path(library.transcript_file(str(A.home / ".claude" / "projects"), str(A.work), u))
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text("{}\n")
    return e["id"]


def _claudes(u):
    return sorted(p for p, _ in library_cli.claude_processes(u))


def _ensure(A, sid):
    """What opening the terminal in a tab does: `library_cli ensure <id>`."""
    r = subprocess.run([sys.executable, str(TERMINAL_DIR / "library_cli.py"), "ensure", sid],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    pids = wait_for(lambda: _claudes(U1) if len(_claudes(U1)) == 1 else None, timeout=15)
    assert pids, "the fake claude never started"
    return pids


def _starts(A):
    p = A.home / "claude-calls.log"
    return p.read_text().splitlines() if p.exists() else []


def test_restart_starts_claude_again_in_the_same_conversation(rs):
    sid = _rs_seed(rs)
    old = _ensure(rs, sid)
    code, e = post(rs, "restart", {"id": sid})
    assert code == 200, e
    assert set(e) == ROW_KEYS | {"restarted"}
    assert e["restarted"] is True and e["id"] == sid
    assert e["active"] is True and e["archived"] is False
    # loaded again with no tab open: one claude, a new process, the same conversation
    assert _alive(rs, "cs-" + sid)
    new = wait_for(lambda: _claudes(U1) if len(_claudes(U1)) == 1 else None, timeout=15)
    assert new and new != old
    starts = _starts(rs)
    assert len(starts) == 2
    assert all(f"--resume {U1}" in s for s in starts), starts


@pytest.mark.parametrize("reason", ["printing", "hold", "background-task", "working"])
def test_restart_refuses_a_busy_terminal_without_force(rs, monkeypatch, reason):
    sid = _rs_seed(rs)
    _start(rs, "cs-" + sid)
    pane = _pane_pid(rs, "cs-" + sid)
    if reason == "printing":
        monkeypatch.setattr(rs.ss, "lib_last_output", lambda name: time.time() - 5)
    elif reason == "hold":
        library.set_hold(sid, time.time() + 600, rs.lib)
    elif reason == "background-task":
        monkeypatch.setattr(rs.ss.idle_reaper, "tree_has_task_output", lambda pid: True)
    else:
        monkeypatch.setattr(rs.ss, "sample_working", lambda pids: {k: True for k in pids})
    code, out = post(rs, "restart", {"id": sid})
    assert code == 409, out
    assert out["reason"] == reason
    assert isinstance(out["error"], str) and "force" in out["error"]
    assert _pane_pid(rs, "cs-" + sid) == pane                 # untouched
    assert _starts(rs) == []


def test_restart_force_restarts_a_busy_terminal(rs, monkeypatch):
    sid = _rs_seed(rs)
    old = _ensure(rs, sid)
    monkeypatch.setattr(rs.ss, "lib_last_output", lambda name: time.time() - 1)
    assert post(rs, "restart", {"id": sid})[1]["reason"] == "printing"
    code, e = post(rs, "restart", {"id": sid, "force": True})
    assert code == 200 and e["restarted"] is True, e
    new = wait_for(lambda: _claudes(U1) if len(_claudes(U1)) == 1 else None, timeout=15)
    assert new and new != old and len(_starts(rs)) == 2


def test_restart_with_a_tab_open_is_not_refused(rs):
    sid = _rs_seed(rs)
    old = _ensure(rs, sid)
    _start(rs, "viewer", f"env -u TMUX tmux -L {rs.sock} attach -t =cs-{sid}")
    assert wait_for(lambda: get(rs)[1]["sessions"][0]["attached"], timeout=5)
    code, e = post(rs, "restart", {"id": sid})
    assert code == 200 and e["restarted"] is True, e
    new = wait_for(lambda: _claudes(U1) if len(_claudes(U1)) == 1 else None, timeout=15)
    assert new and new != old


def test_restart_with_a_reconnecting_tab_ends_with_one_claude(rs):
    # an open ttyd tab: open-session.sh attaches; when the session dies the attach
    # returns and ttyd runs open-session.sh again — its ensure races ours
    sid = _rs_seed(rs)
    old = _ensure(rs, sid)
    stop = threading.Event()

    def tab():
        while not stop.is_set():
            p = subprocess.Popen(["script", "-qfc", f"bash {TERMINAL_DIR / 'open-session.sh'} {sid}",
                                  "/dev/null"], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rs.procs.append(p)
            while p.poll() is None and not stop.is_set():
                time.sleep(0.05)
    t = threading.Thread(target=tab, daemon=True)
    t.start()
    try:
        attached = lambda: _by_id(get(rs)[1])[sid]["attached"]   # noqa: E731
        assert wait_for(attached, timeout=10)
        seen = {tuple(old)}
        for n in range(3):
            code, e = post(rs, "restart", {"id": sid})
            assert code == 200 and e["restarted"] is True, e
            assert wait_for(attached, timeout=15), "the tab did not reattach"
            time.sleep(1.0)                      # let a late ensure from the tab land
            now = _claudes(U1)
            assert len(now) == 1, f"restart {n + 1}: {len(now)} claudes on one conversation"
            assert tuple(now) not in seen
            seen.add(tuple(now))
            assert len(_starts(rs)) == n + 2, _starts(rs)
    finally:
        stop.set()
        t.join(timeout=10)


def test_restart_kills_a_claude_that_ignores_the_hangup(rs, monkeypatch):
    monkeypatch.setattr(rs.ss, "RESTART_EXIT_SECONDS", 1)
    monkeypatch.setattr(rs.ss, "RESTART_TERM_SECONDS", 1)
    sid = _rs_seed(rs)
    (rs.home / "stubborn").write_text("")
    old = _ensure(rs, sid)
    (rs.home / "stubborn").unlink()              # the new start is a normal claude
    sent = []
    real_kill = rs.ss.os.kill
    monkeypatch.setattr(rs.ss.os, "kill", lambda pid, sig: (sent.append((pid, sig)), real_kill(pid, sig)))
    code, e = post(rs, "restart", {"id": sid})
    assert code == 200 and e["restarted"] is True, e
    import signal
    assert (old[0], signal.SIGTERM) in sent and (old[0], signal.SIGKILL) in sent
    assert {p for p, _ in sent} == set(old)      # only the old claude was signalled
    new = wait_for(lambda: _claudes(U1) if len(_claudes(U1)) == 1 else None, timeout=15)
    assert new and new != old


# Claude's shutdown removes its pid file first, then flushes (and runs SessionEnd
# hooks) for a while: the claude is still alive, but no longer found by that file
DYING_CLAUDE = r"""trap 'rm -f "$(cat "$HOME/dying-pidfile")"; sleep 3; echo "exit $$" >> "$HOME/lifecycle.log"; exit 0' HUP
while :; do sleep 0.2; done
"""


def test_restart_waits_for_the_old_claude_by_pid_not_by_its_pid_file(rs):
    # after /clear or /resume the command line names another conversation: only the
    # pid file links the claude to this one, and it goes first when the claude exits
    sid = _rs_seed(rs)
    name = "cs-" + sid
    (rs.home / "dying.sh").write_text(DYING_CLAUDE)
    (rs.home / "dying").write_text("")
    r = subprocess.run([sys.executable, str(TERMINAL_DIR / "library_cli.py"), "ensure", sid],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr

    def dying_pid():
        p = _pane_pid(rs, name)
        return p if p and "dying.sh" in " ".join(library_cli._cmdline(p)) else None
    pid = wait_for(dying_pid, timeout=15)
    assert pid, "the fake claude never started"
    time.sleep(0.3)                              # its trap is set
    (rs.home / "dying").unlink()                 # the new start is a normal claude
    _claude_runs(rs, name, U1)
    (rs.home / "dying-pidfile").write_text(str(rs.claude_sessions / f"{pid}.json"))
    assert _claudes(U1) == [pid]
    code, e = post(rs, "restart", {"id": sid})
    assert code == 200 and e["restarted"] is True, e
    log = lambda: (rs.home / "lifecycle.log").read_text().splitlines()   # noqa: E731
    assert wait_for(lambda: len(log()) >= 3, timeout=15), log()
    assert log()[:2] == [f"start {pid}", f"exit {pid}"], \
        f"the new claude started before the old one exited: {log()}"
    assert len(_starts(rs)) == 2


@pytest.mark.parametrize("renamed", [False, True])
def test_restart_refuses_when_the_conversation_switches_meanwhile(rs, monkeypatch, renamed):
    # /clear, /resume or a consent relaunch lands while ⟳ is being checked; another
    # syncer (a tab's ensure, the reaper) may rename the terminal too. Restarting the
    # number pressed would bring back the conversation it left: refuse instead
    sid = _rs_seed(rs)
    name = "cs-" + sid
    old = _ensure(rs, sid)

    done = []

    def switch(pids):                            # the busy sample: once, in the window
        if done:
            return {k: False for k in pids}
        done.append(1)
        _claude_runs(rs, name, UNEW)
        if renamed:
            assert _tmux(rs.sock, "rename-session", "-t", "=" + name, "cs-0badc0de").returncode == 0
        return {k: False for k in pids}
    monkeypatch.setattr(rs.ss, "sample_working", switch)
    code, out = post(rs, "restart", {"id": sid})
    assert code == 409 and out["reason"] == "changed", out
    now = "cs-0badc0de" if renamed else name
    assert _alive(rs, now) and _pane_pid(rs, now) == old[0]      # untouched
    assert renamed != _alive(rs, name)
    assert len(_starts(rs)) == 1


def test_restart_refuses_when_the_conversation_runs_in_another_claude(rs):
    sid = _rs_seed(rs)
    _start(rs, "cs-" + sid)
    p = subprocess.Popen(["bash", "-c", f"exec -a claude /bin/bash -c 'sleep 600; :' claude --resume {U1}"])
    rs.procs.append(p)
    assert wait_for(lambda: _claudes(U1), timeout=5)
    code, out = post(rs, "restart", {"id": sid, "force": True})
    assert code == 409 and out["reason"] == "elsewhere", out
    assert _alive(rs, "cs-" + sid) and _starts(rs) == []


def test_restart_errors(rs):
    sid = _rs_seed(rs)
    _rs_seed(rs, u=U2, name="в архиве", archived=True)
    _start(rs, "cs-bbbb2222")
    assert post(rs, "restart", {"id": "deadbeef"})[0] == 404
    assert post(rs, "restart", {"id": "AAAA1111"})[0] == 400
    assert post(rs, "restart", {"id": sid, "force": "yes"})[0] == 400
    code, out = post(rs, "restart", {"id": sid})                   # not loaded
    assert code == 409 and out["reason"] == "not-loaded"
    code, out = post(rs, "restart", {"id": "bbbb2222", "force": True})
    assert code == 409 and out["reason"] == "archived"
    assert _alive(rs, "cs-bbbb2222") and _starts(rs) == []


def test_restart_has_the_same_csrf_checks(rs, monkeypatch):
    monkeypatch.delenv("AGENTDECK_ORIGIN", raising=False)
    sid = _rs_seed(rs)
    _start(rs, "cs-" + sid)
    body = json.dumps({"id": sid, "force": True}).encode()
    assert _raw_req(rs, "POST", "/api/library/restart", body, {"Content-Type": "text/plain"})[0] == 415
    assert _raw_req(rs, "POST", "/api/library/restart", body,
                    {"Content-Type": "application/json", "Origin": "https://evil.example"})[0] == 403
    assert _pane_pid(rs, "cs-" + sid) is not None and _starts(rs) == []


# ── ⟳ asks once (owner 2026-10-23: "два диалога подряд неудобно"): POST
# /api/library/restart-check {id} answers whether the terminal is busy — {"busy": reason
# | null} — and touches nothing, so the page can show ONE question: a plain one, or a
# red one naming what it is doing. Its own route: a server that predates it answers
# 404 instead of taking the request for a restart (review 2026-10-23).
def test_restart_check_reports_an_idle_terminal_and_touches_nothing(rs):
    sid = _rs_seed(rs)
    old = _ensure(rs, sid)
    code, out = post(rs, "restart-check", {"id": sid})
    assert code == 200 and out == {"busy": None}, out
    assert _claudes(U1) == old and len(_starts(rs)) == 1


@pytest.mark.parametrize("busy,reason", [
    ({"lib_last_output": lambda name: time.time() - 1}, "printing"),
    ({"lib_last_output": lambda name: None}, "printing"),
    ({"held": True}, "hold"),
    ({"task": True}, "background-task"),
    ({"sample_working": lambda pids: {k: True for k in pids}}, "working"),
])
def test_restart_check_names_why_a_terminal_is_busy(rs, monkeypatch, busy, reason):
    sid = _rs_seed(rs)
    old = _ensure(rs, sid)
    for k in ("lib_last_output", "sample_working"):
        if k in busy:
            monkeypatch.setattr(rs.ss, k, busy[k])
    if busy.get("held"):
        monkeypatch.setattr(rs.ss.library, "held", lambda *a, **k: True)
    if busy.get("task"):
        monkeypatch.setattr(rs.ss.idle_reaper, "tree_has_task_output", lambda pid: True)
    code, out = post(rs, "restart-check", {"id": sid})
    assert code == 200 and out == {"busy": reason}, out
    assert _claudes(U1) == old and len(_starts(rs)) == 1


def test_restart_check_refuses_what_restart_refuses(rs):
    sid = _rs_seed(rs)
    code, out = post(rs, "restart-check", {"id": sid})                # not loaded
    assert code == 409 and out["reason"] == "not-loaded"
    assert post(rs, "restart-check", {"id": "deadbeef"})[0] == 404
    assert post(rs, "restart-check", {"id": "AAAA1111"})[0] == 400
    _ensure(rs, sid)
    p = subprocess.Popen(["bash", "-c", f"exec -a claude /bin/bash -c 'sleep 600; :' claude --resume {U1}"])
    rs.procs.append(p)
    assert wait_for(lambda: len(_claudes(U1)) == 2, timeout=5)
    code, out = post(rs, "restart-check", {"id": sid})
    assert code == 409 and out["reason"] == "elsewhere", out
    assert len(_starts(rs)) == 1


def test_restart_check_has_the_same_csrf_checks(rs):
    sid = _rs_seed(rs)
    body = json.dumps({"id": sid}).encode()
    assert _raw_req(rs, "POST", "/api/library/restart-check", body,
                    {"Content-Type": "text/plain"})[0] == 415
    assert _raw_req(rs, "POST", "/api/library/restart-check", body,
                    {"Content-Type": "application/json", "Origin": "https://evil.example"})[0] == 403


# ── the trash (owner 2026-10-23). Archive = needed, just not now; Trash = not needed,
# kept just in case, never erased by itself. POST trash {id, force?} moves a terminal
# there from the list or the archive — a loaded one is unloaded first, like ⟳ unloads:
# its exact tmux session killed, its claude waited for, under the ensure lock; a busy
# one (lib_busy_reason) only with force=true; never while its conversation runs in
# another claude. trash-check asks first and touches nothing; untrash puts it back
# where it was; empty-trash {ids} erases only the trashed ones among ids that run
# nowhere — the transcript and its own folder, nothing else. ──
def _trash_flag(A, sid):
    e = library.find(library.load(A.lib), sid)
    return None if e is None else e.get("trashed")


def _fake_claude_on(u):
    """A process that looks like `claude --resume u` (one process, no children)."""
    return subprocess.Popen(["bash", "-c", "exec -a claude python3 -c 'import time; "
                             f"time.sleep(600)' --resume {u}"])


def test_trash_of_an_unloaded_terminal_hides_it_everywhere_but_the_trash(api, projects):
    _seed(api, (U1, "в корзину", 10), (U2, "соседка", 20))
    (projects / f"{U1}.jsonl").write_text("talk")
    t0 = int(time.time())
    code, e = post(api, "trash", {"id": "aaaa1111"})
    assert code == 200, e
    assert e["id"] == "aaaa1111" and e["unloaded"] is False and e["active"] is False
    assert isinstance(e["trashed"], int) and e["trashed"] >= t0
    assert _trash_flag(api, "aaaa1111") == e["trashed"]
    assert library.find(library.load(api.lib), "aaaa1111")["archived"] is False
    assert [s["id"] for s in get(api)[1]["sessions"]] == ["bbbb2222"]
    assert [s["id"] for s in get(api, "/api/library?archived=1")[1]["sessions"]] == ["bbbb2222"]
    code, data = get(api, "/api/library?trash=1")
    assert code == 200 and [s["id"] for s in data["sessions"]] == ["aaaa1111"]
    assert set(data["sessions"][0]) == ROW_KEYS | {"trashed"}
    assert data["sessions"][0]["trashed"] == e["trashed"]
    assert (projects / f"{U1}.jsonl").read_text() == "talk"     # nothing erased
    assert set(_by_id(get(api)[1])["bbbb2222"]) == ROW_KEYS     # other rows: as ever


def test_trash_listing_is_most_recently_trashed_first(api):
    _seed(api, (U1, "a", 10), (U2, "b", 20, True), (U3, "c", 30))
    with library.update(api.lib) as L:
        library.trash(L, "aaaa1111", now=300)
        library.trash(L, "bbbb2222", now=100)
        library.trash(L, "c0ffee00", now=200)
    rows = get(api, "/api/library?trash=1")[1]["sessions"]
    assert [s["id"] for s in rows] == ["aaaa1111", "c0ffee00", "bbbb2222"]
    assert [s["archived"] for s in rows] == [False, False, True]


def test_untrash_puts_it_back_where_it_was(api):
    _seed(api, (U1, "live", 10), (U2, "archived", 20, True))
    assert post(api, "trash", {"id": "aaaa1111"})[0] == 200
    assert post(api, "trash", {"id": "bbbb2222"})[0] == 200
    code, e = post(api, "untrash", {"id": "bbbb2222"})
    assert code == 200 and e["id"] == "bbbb2222" and e["archived"] is True and "trashed" not in e
    assert [s["id"] for s in get(api)[1]["sessions"]] == []                    # still archived
    assert [s["id"] for s in get(api, "/api/library?archived=1")[1]["sessions"]] == ["bbbb2222"]
    code, e = post(api, "untrash", {"id": "aaaa1111"})
    assert code == 200 and e["archived"] is False
    assert [s["id"] for s in get(api)[1]["sessions"]] == ["aaaa1111"]
    assert get(api, "/api/library?trash=1")[1]["sessions"] == []
    assert post(api, "untrash", {"id": "aaaa1111"})[0] == 200                 # not trashed: no-op


def test_trash_and_untrash_bad_and_unknown_ids(api):
    _seed(api, (U1, "a", 10))
    for route in ("trash", "trash-check", "untrash"):
        for body in ({"id": "../x"}, {"id": "AAAA1111"}, {"id": 1}, {}):
            assert post(api, route, body)[0] == 400, (route, body)
        assert post(api, route, {"id": "deadbeef"})[0] == 404, route
    assert post(api, "trash", {"id": "aaaa1111", "force": "yes"})[0] == 400
    assert _trash_flag(api, "aaaa1111") is None


def test_trash_check_of_an_unloaded_terminal_is_not_busy(api):
    _seed(api, (U1, "a", 10))
    code, out = post(api, "trash-check", {"id": "aaaa1111"})
    assert code == 200 and out == {"busy": None}
    assert _trash_flag(api, "aaaa1111") is None


def test_trash_unloads_a_loaded_idle_terminal_and_it_does_not_come_back(rs):
    sid = _rs_seed(rs)
    _ensure(rs, sid)
    code, out = post(rs, "trash-check", {"id": sid})
    assert code == 200 and out == {"busy": None}
    assert _alive(rs, "cs-" + sid) and _trash_flag(rs, sid) is None          # touched nothing
    code, e = post(rs, "trash", {"id": sid})
    assert code == 200 and e["unloaded"] is True and e["active"] is False, e
    assert isinstance(e["trashed"], int)
    assert not _alive(rs, "cs-" + sid)
    assert wait_for(lambda: _claudes(U1) == [], timeout=10), _claudes(U1)    # its claude exited
    # opening it (a tab, a link, the bridge) does not load it again
    r = subprocess.run([sys.executable, str(TERMINAL_DIR / "library_cli.py"), "ensure", sid],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "trash" in r.stderr
    assert len(_starts(rs)) == 1 and not _alive(rs, "cs-" + sid)


@pytest.mark.parametrize("reason", ["printing", "hold", "background-task", "working"])
def test_trash_of_a_busy_terminal_is_refused_without_force(rs, monkeypatch, reason):
    sid = _rs_seed(rs)
    _start(rs, "cs-" + sid)
    pane = _pane_pid(rs, "cs-" + sid)
    if reason == "printing":
        monkeypatch.setattr(rs.ss, "lib_last_output", lambda name: time.time() - 5)
    elif reason == "hold":
        library.set_hold(sid, time.time() + 600, rs.lib)
    elif reason == "background-task":
        monkeypatch.setattr(rs.ss.idle_reaper, "tree_has_task_output", lambda pid: True)
    else:
        monkeypatch.setattr(rs.ss, "sample_working", lambda pids: {k: True for k in pids})
    code, out = post(rs, "trash-check", {"id": sid})
    assert code == 200 and out == {"busy": reason}, out
    code, out = post(rs, "trash", {"id": sid})
    assert code == 409 and out["reason"] == reason and "force" in out["error"], out
    assert _pane_pid(rs, "cs-" + sid) == pane and _trash_flag(rs, sid) is None   # untouched


def test_trash_force_unloads_a_busy_terminal(rs, monkeypatch):
    sid = _rs_seed(rs)
    _ensure(rs, sid)
    monkeypatch.setattr(rs.ss, "lib_last_output", lambda name: time.time() - 1)
    assert post(rs, "trash", {"id": sid})[1]["reason"] == "printing"
    code, e = post(rs, "trash", {"id": sid, "force": True})
    assert code == 200 and e["unloaded"] is True, e
    assert not _alive(rs, "cs-" + sid) and _trash_flag(rs, sid)
    assert wait_for(lambda: _claudes(U1) == [], timeout=10)
    assert len(_starts(rs)) == 1                                  # nothing started again


def test_trash_kills_a_claude_that_ignores_the_hangup(rs, monkeypatch):
    monkeypatch.setattr(rs.ss, "RESTART_EXIT_SECONDS", 1)
    monkeypatch.setattr(rs.ss, "RESTART_TERM_SECONDS", 1)
    sid = _rs_seed(rs)
    (rs.home / "stubborn").write_text("")
    old = _ensure(rs, sid)
    code, e = post(rs, "trash", {"id": sid})
    assert code == 200 and e["unloaded"] is True, e
    assert wait_for(lambda: _claudes(U1) == [], timeout=10)
    assert _trash_flag(rs, sid) and old


@pytest.mark.parametrize("loaded", [False, True])
def test_trash_refuses_while_the_conversation_runs_in_another_claude(rs, loaded):
    sid = _rs_seed(rs)
    if loaded:
        _start(rs, "cs-" + sid)
    p = _fake_claude_on(U1)
    rs.procs.append(p)
    assert wait_for(lambda: _claudes(U1), timeout=5)
    code, out = post(rs, "trash-check", {"id": sid})
    assert code == 409 and out["reason"] == "elsewhere", out
    code, out = post(rs, "trash", {"id": sid, "force": True})
    assert code == 409 and out["reason"] == "elsewhere", out
    assert _trash_flag(rs, sid) is None and p.poll() is None
    assert _alive(rs, "cs-" + sid) == loaded


def test_trash_refuses_a_conversation_running_in_a_legacy_terminal(api):
    _seed(api, (U1, "old", 10))
    _start(api, "claude-terminal-3", f"bash -c 'sleep 600; : --resume {U1}'")
    code, out = post(api, "trash", {"id": "aaaa1111", "force": True})
    assert code == 409 and out["reason"] == "elsewhere", out
    assert _trash_flag(api, "aaaa1111") is None and _alive(api, "claude-terminal-3")


def test_trash_by_a_vanished_number_acts_on_the_renamed_terminal(quiet):
    api = quiet
    _seed(api, (U1, "consent", 10))
    _start(api, "cs-aaaa1111")
    _claude_runs(api, "cs-aaaa1111", UNEW)
    code, e = post(api, "trash", {"id": "aaaa1111"})
    assert code == 200 and e["id"] == "0badc0de" and e["unloaded"] is True, e
    assert not _alive(api, "cs-0badc0de") and not _alive(api, "cs-aaaa1111")
    assert _trash_flag(api, "0badc0de")


def test_trash_again_is_a_no_op(api):
    _seed(api, (U1, "a", 10))
    first = post(api, "trash", {"id": "aaaa1111"})[1]["trashed"]
    code, e = post(api, "trash", {"id": "aaaa1111"})
    assert code == 200 and e["trashed"] == first
    assert post(api, "trash-check", {"id": "aaaa1111"}) == (200, {"busy": None})


def test_restart_refuses_a_trashed_terminal(api):
    _seed(api, (U1, "a", 10))
    with library.update(api.lib) as L:
        library.trash(L, "aaaa1111", now=5)
    _start(api, "cs-aaaa1111")
    for route in ("restart", "restart-check"):
        code, out = post(api, route, {"id": "aaaa1111", "force": True} if route == "restart"
                         else {"id": "aaaa1111"})
        assert code == 409 and out["reason"] == "trashed", (route, out)
    assert _alive(api, "cs-aaaa1111")


def test_trash_routes_have_the_same_csrf_checks(api, monkeypatch):
    monkeypatch.delenv("AGENTDECK_ORIGIN", raising=False)
    _seed(api, (U1, "a", 10))
    with library.update(api.lib) as L:
        library.trash(L, "aaaa1111", now=5)
    _seed(api, (U2, "b", 20))
    for route, body in (("trash", {"id": "bbbb2222", "force": True}), ("trash-check", {"id": "bbbb2222"}),
                        ("untrash", {"id": "aaaa1111"}), ("empty-trash", {"ids": ["aaaa1111"]})):
        raw = json.dumps(body).encode()
        assert _raw_req(api, "POST", "/api/library/" + route, raw,
                        {"Content-Type": "text/plain"})[0] == 415, route
        assert _raw_req(api, "POST", "/api/library/" + route, raw,
                        {"Content-Type": "application/json", "Origin": "https://evil.example"})[0] == 403, route
    assert _trash_flag(api, "aaaa1111") == 5 and _trash_flag(api, "bbbb2222") is None


# ── Empty trash: erased for good — only what was trashed AND shown, never a running one ──
def _talk(projects, u, sidecar=True):
    (projects / f"{u}.jsonl").write_text("talk")
    if sidecar:
        (projects / u / "subagents").mkdir(parents=True)
        (projects / u / "subagents" / "a.jsonl").write_text("sub")


def test_empty_trash_erases_only_trashed_entries_among_the_ids(api, projects):
    _seed(api, (U1, "binned", 10), (U2, "binned, not shown", 20), (U3, "live", 30))
    for u in (U1, U2, U3):
        _talk(projects, u)
    (projects / f"{U1}.jsonl.bak").write_text("keep")                      # only looks alike
    with library.update(api.lib) as L:
        library.trash(L, "aaaa1111", now=5)
        library.trash(L, "bbbb2222", now=6)
    code, out = post(api, "empty-trash", {"ids": ["aaaa1111", "c0ffee00", "deadbeef", "aaaa1111"]})
    assert code == 200, out
    assert out["erased"] == ["aaaa1111"]
    assert out["skipped"] == [{"id": "c0ffee00", "reason": "not-trashed"},
                              {"id": "deadbeef", "reason": "unknown"}]
    assert not (projects / f"{U1}.jsonl").exists() and not (projects / U1).exists()
    assert (projects / f"{U1}.jsonl.bak").read_text() == "keep"
    for u in (U2, U3):                                                       # untouched
        assert (projects / f"{u}.jsonl").read_text() == "talk"
        assert (projects / u / "subagents" / "a.jsonl").exists()
    assert [e["id"] for e in library.load(api.lib)["sessions"]] == ["bbbb2222", "c0ffee00"]
    assert [s["id"] for s in get(api, "/api/library?trash=1")[1]["sessions"]] == ["bbbb2222"]


def test_empty_trash_erases_an_archived_trashed_one_without_a_transcript(api, projects):
    _seed(api, (U1, "binned", 10, True))
    with library.update(api.lib) as L:
        library.trash(L, "aaaa1111", now=5)
    code, out = post(api, "empty-trash", {"ids": ["aaaa1111"]})
    assert code == 200 and out == {"erased": ["aaaa1111"], "skipped": []}
    assert library.load(api.lib)["sessions"] == []


def test_empty_trash_skips_a_conversation_running_anywhere(api, projects):
    _seed(api, (U1, "loaded", 10), (U2, "in another claude", 20), (U3, "in an old terminal", 30))
    for u in (U1, U2, U3):
        _talk(projects, u, sidecar=False)
    with library.update(api.lib) as L:
        for sid in ("aaaa1111", "bbbb2222", "c0ffee00"):
            library.trash(L, sid, now=5)
    _start(api, "cs-aaaa1111")                                              # still loaded
    p = _fake_claude_on(U2)
    _start(api, "claude-terminal-3", f"bash -c 'sleep 600; : --resume {U3}'")
    try:
        assert wait_for(lambda: library_cli.claude_processes(U2), timeout=5)
        code, out = post(api, "empty-trash", {"ids": ["aaaa1111", "bbbb2222", "c0ffee00"]})
        assert code == 200 and out["erased"] == [], out
        assert out["skipped"] == [{"id": s, "reason": "running"}
                                  for s in ("aaaa1111", "bbbb2222", "c0ffee00")]
        for u in (U1, U2, U3):
            assert (projects / f"{u}.jsonl").read_text() == "talk"
        assert len(library.load(api.lib)["sessions"]) == 3
        assert p.poll() is None and _alive(api, "cs-aaaa1111")              # nothing interrupted
    finally:
        p.kill()
        p.wait(timeout=5)


def test_empty_trash_validates_its_ids(api, projects):
    _seed(api, (U1, "binned", 10))
    with library.update(api.lib) as L:
        library.trash(L, "aaaa1111", now=5)
    for body in ({}, {"ids": "aaaa1111"}, {"ids": ["../x"]}, {"ids": ["AAAA1111"]}, {"ids": [1]},
                 {"ids": None}):
        assert post(api, "empty-trash", body)[0] == 400, body
    assert post(api, "empty-trash", {"ids": []}) == (200, {"erased": [], "skipped": []})
    assert len(library.load(api.lib)["sessions"]) == 1


def _move_cwd(api, sid, cwd):
    with library.update(api.lib) as L:
        library.find(L, sid)["cwd"] = cwd


def test_empty_trash_erases_where_claude_ran_when_the_folder_is_gone(api, projects, tmp_path):
    # review 2026-10-23: the terminal's folder was renamed/removed, so the pane ran in
    # WORKDIR (library_cli.effective_cwd) and the transcript is under its slug — that
    # is the one to erase, not a "nothing there" under the old folder's slug
    _seed(api, (U1, "folder gone", 10), (U2, "no folder at all", 20))
    _move_cwd(api, "aaaa1111", str(tmp_path / "renamed-away"))
    _move_cwd(api, "bbbb2222", "")
    here = projects.parent / library.cwd_slug(library_cli.WORKDIR)
    here.mkdir(exist_ok=True)
    for u in (U1, U2):
        (here / f"{u}.jsonl").write_text("talk")
    (here / U1).mkdir()
    with library.update(api.lib) as L:
        library.trash(L, "aaaa1111", now=5)
        library.trash(L, "bbbb2222", now=6)
    code, out = post(api, "empty-trash", {"ids": ["aaaa1111", "bbbb2222"]})
    assert code == 200 and out == {"erased": ["aaaa1111", "bbbb2222"], "skipped": []}, out
    for u in (U1, U2):
        assert not (here / f"{u}.jsonl").exists()
    assert not (here / U1).exists()
    assert library.load(api.lib)["sessions"] == []


def test_empty_trash_keeps_one_whose_transcript_is_still_on_disk_elsewhere(api, projects):
    # never "erased" while its conversation is still on disk: the entry stays in the
    # trash (to restore or try again), and nothing is removed outside its own folders
    _seed(api, (U1, "copy elsewhere", 10))
    (projects / f"{U1}.jsonl").write_text("talk")
    other = projects.parent / "-somewhere-else"
    other.mkdir()
    (other / f"{U1}.jsonl").write_text("talk too")
    with library.update(api.lib) as L:
        library.trash(L, "aaaa1111", now=5)
    code, out = post(api, "empty-trash", {"ids": ["aaaa1111"]})
    assert code == 200 and out == {"erased": [], "skipped": [{"id": "aaaa1111",
                                                             "reason": "left-on-disk"}]}, out
    assert (other / f"{U1}.jsonl").read_text() == "talk too"
    assert (projects / f"{U1}.jsonl").read_text() == "talk"          # nothing half-erased
    assert [s["id"] for s in get(api, "/api/library?trash=1")[1]["sessions"]] == ["aaaa1111"]
    assert "left-on-disk" in api.ss.EMPTY_TRASH_REASONS


def test_archive_refuses_a_trashed_terminal(api):
    # review 2026-10-23: a stale row (a 2nd tab, or before the next poll) must not
    # move a trashed terminal between the lists — Restore puts it back where it was
    _seed(api, (U1, "archived, then binned", 10, True), (U2, "binned", 20))
    with library.update(api.lib) as L:
        library.trash(L, "aaaa1111", now=5)
        library.trash(L, "bbbb2222", now=6)
    for sid, flag in (("aaaa1111", False), ("bbbb2222", True)):
        code, out = post(api, "archive", {"id": sid, "archived": flag})
        assert code == 409 and out["reason"] == "trashed", (sid, out)
    L = library.load(api.lib)
    assert library.find(L, "aaaa1111")["archived"] is True
    assert library.find(L, "bbbb2222")["archived"] is False
    assert post(api, "untrash", {"id": "aaaa1111"})[1]["archived"] is True
    assert post(api, "untrash", {"id": "bbbb2222"})[1]["archived"] is False
