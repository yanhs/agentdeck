"""TDD: the dashboard knows how much of the Claude allowance is used (owner, 30.10.2026).

Like Claude's /usage: the 5-hour window and the week, for the owner (his subscription)
and for a guest (the guest's own limits at the host's turnstile). Shared contract:
pr/terminal-dev/.progress-usage-limits-disk.md.

    GET /api/quota -> {"available": false}
                   -> {"available": true, "source": "guest" | "subscription",
                       "five_hour": {"utilization", "resets_at"},
                       "seven_day": {"utilization", "resets_at"}}     utilization: 0..1 float

- guest (ANTHROPIC_BASE_URL is not anthropic.com, ANTHROPIC_AUTH_TOKEN is the pass):
  GET <base>/guest/usage with the pass. The pass never reaches the page.
- subscription (no turnstile): a tiny probe POST https://api.anthropic.com/v1/messages
  (claude-haiku-4-5, max_tokens 1) with CLAUDE_CODE_OAUTH_TOKEN (~/.claude/oauth.env or
  the env var); the numbers are its anthropic-ratelimit-unified-{5h,7d}-{utilization,reset}
  headers. At most one probe per 10 minutes, always in a background thread.

    GET /api/quota/terminals -> {"terminals": [{"id", "name", "tokens_today"}]}   (owner only)
  today's weighted tokens per terminal from its conversation JSONL (input 1, cache write
  1.25, cache read 0.1, output 5; each assistant message.id once), top 8, cached 5 min.

No test here ever calls the real network: the probe is injected (and the real probe
function is pointed at a local fake server).
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import pytest

TERMINAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TERMINAL_DIR))

PASS = "sbx-" + "p" * 40
OAUTH = "sk-ant-oat01-" + "t" * 40


def usage_body(r5, r7, u5=0.07, u7=0.09):
    """GET /guest/usage as the turnstile answers it (the shared contract)."""
    return {"guest": "maria", "enabled": True, "unlimited_until": 0,
            "five_hour": {"used": int(u5 * 5_000_000), "limit": 5_000_000,
                          "utilization": u5, "resets_at": r5},
            "seven_day": {"used": int(u7 * 35_000_000), "limit": 35_000_000,
                          "utilization": u7, "resets_at": r7}}


def probe_headers(r5, r7, u5="0.07", u7="0.09"):
    return {"anthropic-ratelimit-unified-5h-utilization": u5,
            "anthropic-ratelimit-unified-5h-reset": str(r5),
            "anthropic-ratelimit-unified-7d-utilization": u7,
            "anthropic-ratelimit-unified-7d-reset": str(r7),
            "anthropic-ratelimit-unified-status": "allowed"}


class FakeTurnstile:
    def __init__(self):
        self.calls = []
        self.status, self.body = 200, b"{}"
        me = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                me.calls.append((self.path, self.headers.get("authorization")))
                self.send_response(me.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(me.body)))
                self.end_headers()
                self.wfile.write(me.body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def answer(self, data, status=200):
        self.status = status
        self.body = data if isinstance(data, bytes) else json.dumps(data).encode()


class FakeProbe:
    """Stands in for the probe of Anthropic: records the tokens, answers (status, headers)."""

    def __init__(self):
        self.calls = []
        self.answer = (200, {})
        self.gate = None            # a threading.Event: hold the probe until it is set

    def __call__(self, token):
        self.calls.append(token)
        if self.gate is not None:
            self.gate.wait(10)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.fixture
def q(tmp_path, monkeypatch):
    t = FakeTurnstile()
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(tmp_path / "k"))
    monkeypatch.setenv("AGENTKENNEL_LIBRARY", str(tmp_path / "library.json"))
    monkeypatch.setenv("AGENTKENNEL_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    sys.modules.pop("status_server", None)
    ss = importlib.import_module("status_server")
    probe = FakeProbe()
    ss.QUOTA.probe = probe
    monkeypatch.setattr(ss, "QUOTA_PROBE_URL", "http://127.0.0.1:9/never-the-real-api")
    srv = HTTPServer(("127.0.0.1", 0), ss.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class Q:
        turnstile = t
        mod = ss
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        oauth_env = home / ".claude" / "oauth.env"

    Q.probe = probe

    def guest():
        monkeypatch.setenv("ANTHROPIC_BASE_URL", t.base)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", PASS)

    def owner(line=f'export CLAUDE_CODE_OAUTH_TOKEN="{OAUTH}"\n'):
        Q.oauth_env.write_text(line)
    Q.guest, Q.owner = guest, owner
    try:
        yield Q
    finally:
        srv.shutdown()
        srv.server_close()
        t.srv.shutdown()


def get(base, path="/api/quota"):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def settled(base, path="/api/quota", until=lambda d: d.get("available") or d.get("error"),
            seconds=8):
    """The first answer may come before the background fetch is done: ask until it is."""
    deadline = time.time() + seconds
    while True:
        code, raw = get(base, path)
        d = json.loads(raw)
        if until(d) or time.time() > deadline:
            return code, raw, d
        time.sleep(0.05)


# ── a guest: the turnstile's numbers ─────────────────────────────────────────
def test_a_guest_sees_both_windows(q):
    now = int(time.time())
    q.guest()
    q.turnstile.answer(usage_body(now + 3 * 3600, now + 4 * 86400))
    code, raw, d = settled(q.base)
    assert code == 200
    assert d == {"available": True, "source": "guest",
                 "five_hour": {"utilization": 0.07, "resets_at": now + 3 * 3600},
                 "seven_day": {"utilization": 0.09, "resets_at": now + 4 * 86400}}
    assert q.turnstile.calls == [("/guest/usage", f"Bearer {PASS}")]
    assert PASS not in raw                                   # the pass never reaches the page
    assert q.probe.calls == []                               # a guest never probes Anthropic


def test_a_guest_window_not_open_yet_says_zero(q):
    now = int(time.time())
    q.guest()
    q.turnstile.answer(usage_body(0, now + 86400, u5=0.0, u7=0.5))
    d = settled(q.base)[2]
    assert d["five_hour"] == {"utilization": 0.0, "resets_at": 0}
    assert d["seven_day"]["utilization"] == 0.5


def test_the_guest_answer_is_cached_for_a_while(q):
    now = int(time.time())
    q.guest()
    q.turnstile.answer(usage_body(now + 60, now + 600))
    settled(q.base)
    for _ in range(3):
        get(q.base)
    assert len(q.turnstile.calls) == 1


@pytest.mark.parametrize("status,body", [
    (401, b'{"error":"x"}'), (502, b"oops"), (200, b"not json"),
    (200, {"five_hour": {"utilization": 0.1, "resets_at": 1}}),
    (200, {"five_hour": {"utilization": "lots", "resets_at": 1},
           "seven_day": {"utilization": 0.1, "resets_at": 1}}),
    (200, {"five_hour": {"utilization": 0.1},
           "seven_day": {"utilization": 0.1, "resets_at": 1}}),
    (200, {"five_hour": {"utilization": True, "resets_at": 1},
           "seven_day": {"utilization": 0.1, "resets_at": 1}}),
    (200, {"spent_today": 1, "limit": 2, "resets_at": 3})],
    ids=["refused", "down", "garbage", "no-week", "bad-number", "no-reset", "bool", "old-shape"])
def test_a_bad_turnstile_answer_shows_nothing(q, status, body):
    q.guest()
    q.turnstile.answer(body, status)
    d = settled(q.base)[2]
    assert d["available"] is False and d["error"]
    assert PASS not in json.dumps(d)


def test_a_proxy_without_a_pass_shows_nothing(q, monkeypatch):
    q.owner()                                   # even with a subscription token on disk
    monkeypatch.setenv("ANTHROPIC_BASE_URL", q.turnstile.base)
    time.sleep(0.2)
    assert json.loads(get(q.base)[1]) == {"available": False}
    time.sleep(0.2)
    assert q.turnstile.calls == [] and q.probe.calls == []


# ── the owner: his subscription, from one tiny probe ─────────────────────────
def test_the_owner_sees_his_subscription(q):
    now = int(time.time())
    q.owner()
    q.probe.answer = (200, probe_headers(now + 7200, now + 5 * 86400))
    code, raw, d = settled(q.base)
    assert code == 200
    assert d == {"available": True, "source": "subscription",
                 "five_hour": {"utilization": 0.07, "resets_at": now + 7200},
                 "seven_day": {"utilization": 0.09, "resets_at": now + 5 * 86400}}
    assert q.probe.calls == [OAUTH]
    assert OAUTH not in raw                                  # the token never reaches the page
    assert q.turnstile.calls == []


def test_at_the_limit_the_refusal_still_carries_the_numbers(q):
    now = int(time.time())
    q.owner()
    q.probe.answer = (429, probe_headers(now + 600, now + 86400, u5="1.02", u7="0.4"))
    d = settled(q.base)[2]
    assert d["available"] is True
    assert d["five_hour"]["utilization"] == pytest.approx(1.02)


def test_at_most_one_probe_per_ten_minutes(q):
    now = int(time.time())
    clock = [1_000_000.0]
    q.mod.QUOTA.clock = lambda: clock[0]
    q.owner()
    q.probe.answer = (200, probe_headers(now + 7200, now + 86400))
    settled(q.base)
    for _ in range(5):                                       # the page polls every minute
        get(q.base)
    clock[0] += 599
    for _ in range(3):
        get(q.base)
    time.sleep(0.3)
    assert len(q.probe.calls) == 1
    clock[0] += 2                                            # 10 minutes are over
    q.probe.answer = (200, probe_headers(now + 7200, now + 86400, u5="0.5"))
    settled(q.base, until=lambda d: d.get("five_hour", {}).get("utilization") == 0.5)
    for _ in range(3):
        get(q.base)
    time.sleep(0.3)
    assert len(q.probe.calls) == 2


def test_the_page_never_waits_for_the_probe(q):
    now = int(time.time())
    q.owner()
    q.probe.gate = threading.Event()
    q.probe.answer = (200, probe_headers(now + 7200, now + 86400))
    try:
        t0 = time.time()
        code, raw = get(q.base)
        assert time.time() - t0 < 2
        assert code == 200 and json.loads(raw) == {"available": False}
        code, raw = get(q.base)                              # still running: not a 2nd probe
        assert json.loads(raw) == {"available": False}
    finally:
        q.probe.gate.set()
    assert settled(q.base)[2]["available"] is True
    assert len(q.probe.calls) == 1


@pytest.mark.parametrize("answer,word", [
    ((401, {}), "401"),
    ((200, {"anthropic-ratelimit-unified-5h-utilization": "0.1"}), "no usage"),
    ((200, probe_headers("soon", 1)), "no usage"),
    (OSError("network is unreachable"), "OSError")],
    ids=["refused", "half", "garbage", "offline"])
def test_a_failed_probe_shows_nothing(q, answer, word):
    q.owner()
    q.probe.answer = answer
    d = settled(q.base)[2]
    assert d["available"] is False and word in d["error"]
    assert OAUTH not in json.dumps(d)


def test_no_token_nothing_to_show(q):
    assert json.loads(get(q.base)[1]) == {"available": False}
    time.sleep(0.2)
    assert q.probe.calls == []


def test_the_token_may_come_from_the_environment(q, monkeypatch):
    now = int(time.time())
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-token")
    q.probe.answer = (200, probe_headers(now + 60, now + 600))
    assert settled(q.base)[2]["source"] == "subscription"
    assert q.probe.calls == ["env-token"]


def test_anthropic_itself_as_base_url_is_still_the_subscription(q, monkeypatch):
    now = int(time.time())
    q.owner()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", PASS)
    q.probe.answer = (200, probe_headers(now + 60, now + 600))
    assert settled(q.base)[2]["source"] == "subscription"
    assert q.probe.calls == [OAUTH] and q.turnstile.calls == []


@pytest.mark.parametrize("text,want", [
    (f"export CLAUDE_CODE_OAUTH_TOKEN={OAUTH}\n", OAUTH),
    (f"# the owner's token\nOTHER=1\nCLAUDE_CODE_OAUTH_TOKEN='{OAUTH}'\n", OAUTH),
    (f'  export   CLAUDE_CODE_OAUTH_TOKEN = "{OAUTH}"  \n', OAUTH),
    ("export CLAUDE_CODE_OAUTH_TOKEN=\n", "from-env"),
    ("", "from-env")],
    ids=["export", "plain-quoted", "spaces", "empty-value", "empty-file"])
def test_the_token_file(q, monkeypatch, text, want):
    q.oauth_env.write_text(text)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-env")
    assert q.mod.quota_oauth_token() == want


def test_the_real_probe_asks_for_one_token_of_haiku(q):
    """quota_probe() itself, against a local stand-in for api.anthropic.com."""
    seen = []
    now = int(time.time())

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            seen.append((self.path, dict(self.headers), json.loads(body)))
            code = 429 if len(seen) > 1 else 200
            self.send_response(code)
            for k, v in probe_headers(now + 60, now + 600).items():
                self.send_header(k, v)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1/messages"
        for want in (200, 429):                    # a 429 at the limit still has the numbers
            status, headers = q.mod.quota_probe(OAUTH, url=url)
            assert status == want
            assert q.mod.quota_from_headers(headers)["five_hour"] == {
                "utilization": 0.07, "resets_at": now + 60}
    finally:
        srv.shutdown()
    path, h, body = seen[0]
    h = {k.lower(): v for k, v in h.items()}
    assert path == "/v1/messages"
    assert h["authorization"] == f"Bearer {OAUTH}"
    assert h["anthropic-beta"] == "oauth-2025-04-20"
    assert h["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-haiku-4-5" and body["max_tokens"] == 1
    assert q.mod.QUOTA_PROBE_TTL == 600


# ── today by terminal (owner) ────────────────────────────────────────────────
def local_ts(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def assistant(mid, ts, i=0, cw=0, cr=0, out=0):
    return {"type": "assistant", "timestamp": iso(ts), "uuid": mid + "-u",
            "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": i, "cache_creation_input_tokens": cw,
                                  "cache_read_input_tokens": cr, "output_tokens": out}}}


def user(ts):
    return {"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": "hi"}}


class Tree:
    """A fake ~/.claude/projects and library.json."""

    def __init__(self, mod, tmp):
        self.mod, self.root, self.libfile = mod, tmp / "projects", tmp / "library.json"
        self.lib = {"sessions": []}
        self.n = 0

    def terminal(self, name, cwd="/home/u/work", trashed=False, archived=False):
        self.n += 1
        u = f"{self.n:08x}-0000-4000-8000-000000000000"
        e = {"id": u[:8], "uuid": u, "name": name, "cwd": cwd, "created": 1, "last_used": 1,
             "archived": archived}
        if trashed:
            e["trashed"] = 2
        self.lib["sessions"].append(e)
        self.libfile.write_text(json.dumps(self.lib))
        return e

    def path(self, e, sub=None):
        lib = self.mod.library
        p = Path(lib.transcript_file(str(self.root), e["cwd"], e["uuid"]))
        if sub:
            p = p.parent / e["uuid"] / "subagents" / sub
        return p

    def write(self, e, records, sub=None, mode="a"):
        p = self.path(e, sub)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, mode) as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return p


def W(i=0, cw=0, cr=0, out=0):
    return round(i + 1.25 * cw + 0.1 * cr + 5 * out)


def test_today_by_terminal_counts_weighted_tokens(q, tmp_path):
    tr = Tree(q.mod, tmp_path)
    now = local_ts(2026, 10, 30, 15)
    today, yesterday = local_ts(2026, 10, 30, 9), local_ts(2026, 10, 29, 22)
    a = tr.terminal("Appeals")
    same = assistant("msg_a1", today, i=10, cw=1000, cr=100_000, out=2000)
    tr.write(a, [user(yesterday), assistant("msg_a0", yesterday, out=999_999),   # yesterday
                 user(today), same, same, dict(same, uuid="other-line"),         # one message
                 assistant("msg_a2", today + 60, i=5, out=100)])
    tr.write(a, [assistant("msg_s1", today + 120, cr=50_000, out=1000)], sub="agent-x.jsonl")
    b = tr.terminal("AgentKennel", cwd="/home/u/pr/terminal")
    tr.write(b, [assistant("msg_b1", today, i=1, out=10)])
    c = tr.terminal("Quiet")                                 # nothing today
    tr.write(c, [assistant("msg_c1", yesterday, out=50)])
    tr.terminal("Never used")                                # no transcript at all
    d = tr.terminal("Thrown away", trashed=True)
    tr.write(d, [assistant("msg_d1", today, out=10_000_000)])
    got = q.mod.quota_terminal_tokens(str(tr.libfile), str(tr.root), now)
    want_a = W(i=10, cw=1000, cr=100_000, out=2000) + W(i=5, out=100) + W(cr=50_000, out=1000)
    assert got == [{"id": a["id"], "name": "Appeals", "tokens_today": want_a},
                   {"id": b["id"], "name": "AgentKennel", "tokens_today": W(i=1, out=10)}]


def test_today_is_from_local_midnight(q, tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        tr = Tree(q.mod, tmp_path)
        now = local_ts(2026, 10, 30, 8)                       # 15:00 UTC
        e = tr.terminal("LA")
        tr.write(e, [assistant("late", local_ts(2026, 10, 29, 23, 30), out=1000),  # 06:30Z
                     assistant("early", local_ts(2026, 10, 30, 0, 30), out=1)])
        got = q.mod.quota_terminal_tokens(str(tr.libfile), str(tr.root), now)
        assert got == [{"id": e["id"], "name": "LA", "tokens_today": 5}]
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_top_eight_largest_first(q, tmp_path):
    tr = Tree(q.mod, tmp_path)
    now = local_ts(2026, 10, 30, 15)
    for n in range(11):
        e = tr.terminal(f"T{n}")
        tr.write(e, [assistant(f"m{n}", local_ts(2026, 10, 30, 10), out=n + 1)])
    got = q.mod.quota_terminal_tokens(str(tr.libfile), str(tr.root), now)
    assert [g["name"] for g in got] == [f"T{n}" for n in range(10, 2, -1)]
    assert [g["tokens_today"] for g in got] == [5 * (n + 1) for n in range(10, 2, -1)]


def test_a_growing_conversation_is_read_on_without_counting_twice(q, tmp_path):
    tr = Tree(q.mod, tmp_path)
    now = local_ts(2026, 10, 30, 15)
    e = tr.terminal("Busy")
    t = local_ts(2026, 10, 30, 10)
    counter = q.mod.QuotaTerminals()
    tr.write(e, [assistant("m1", t, out=10)])
    assert counter.compute(str(tr.libfile), str(tr.root), now)[0]["tokens_today"] == 50
    tr.write(e, [assistant("m1", t, out=10), assistant("m2", t + 5, out=1)])
    assert counter.compute(str(tr.libfile), str(tr.root), now)[0]["tokens_today"] == 55
    p = tr.path(e)                                           # half a line being written
    with open(p, "a") as f:
        f.write(json.dumps(assistant("m3", t + 9, out=2))[:40])
    assert counter.compute(str(tr.libfile), str(tr.root), now)[0]["tokens_today"] == 55
    with open(p, "a") as f:
        f.write(json.dumps(assistant("m3", t + 9, out=2))[40:] + "\n")
    assert counter.compute(str(tr.libfile), str(tr.root), now)[0]["tokens_today"] == 65
    nxt = local_ts(2026, 10, 31, 9)                          # a new day starts from zero
    os.utime(p, (nxt, nxt))
    assert counter.compute(str(tr.libfile), str(tr.root), nxt) == []


def test_terminals_endpoint_for_the_owner(q, tmp_path):
    now = int(time.time())
    q.owner()
    q.probe.answer = (200, probe_headers(now + 60, now + 600))
    tr = Tree(q.mod, tmp_path)
    e = tr.terminal("Appeals")
    tr.write(e, [assistant("m1", now - 1, i=3, out=1)])
    code, raw, d = settled(q.base, "/api/quota/terminals", until=lambda d: d.get("terminals"))
    assert code == 200
    assert d == {"terminals": [{"id": e["id"], "name": "Appeals", "tokens_today": 8}]}


def test_terminals_are_counted_at_most_every_five_minutes_in_the_background(q, tmp_path):
    now = int(time.time())
    clock = [float(now)]
    q.owner()
    terms = q.mod.QUOTA_TERMS
    terms.clock = lambda: clock[0]
    runs = []
    real = terms.compute
    terms.compute = lambda *a: (runs.append(1), real(*a))[1]
    tr = Tree(q.mod, tmp_path)
    tr.write(tr.terminal("A"), [assistant("m1", now - 1, out=1)])
    settled(q.base, "/api/quota/terminals", until=lambda d: d.get("terminals"))
    for _ in range(4):
        get(q.base, "/api/quota/terminals")
    clock[0] += 299
    get(q.base, "/api/quota/terminals")
    time.sleep(0.3)
    assert len(runs) == 1
    clock[0] += 2
    get(q.base, "/api/quota/terminals")
    deadline = time.time() + 5
    while len(runs) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert len(runs) == 2


def test_terminals_are_the_owners_only(q, tmp_path):
    now = int(time.time())
    q.guest()
    q.turnstile.answer(usage_body(now + 60, now + 600))
    tr = Tree(q.mod, tmp_path)
    tr.write(tr.terminal("A"), [assistant("m1", now - 1, out=1)])
    time.sleep(0.2)
    assert json.loads(get(q.base, "/api/quota/terminals")[1]) == {"terminals": []}
    time.sleep(0.3)
    assert json.loads(get(q.base, "/api/quota/terminals")[1]) == {"terminals": []}


def test_the_proxies_route_quota_to_the_status_server():
    for f in ("Caddyfile", "docker/Caddyfile"):
        assert "handle /api/quota* {" in (TERMINAL_DIR / f).read_text(), f
    conf = (TERMINAL_DIR / "nginx/agents-subdomain.conf").read_text()
    assert "location /api/quota {" in conf and "location = /api/quota " not in conf
