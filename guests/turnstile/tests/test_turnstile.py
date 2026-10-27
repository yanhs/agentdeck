"""Turnstile: guests reach the Claude API with a personal pass; the owner's login
stays on the server. Tests run against a fake upstream on localhost."""

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import turnstile as ts  # noqa: E402

OWNER = "sk-ant-oat01-OWNER-SECRET"


# --------------------------------------------------------------------------- #
# fake upstream (stands in for api.anthropic.com)
# --------------------------------------------------------------------------- #
SSE_BODY = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"m1","model":"claude-opus-x",'
    b'"usage":{"input_tokens":100,"cache_creation_input_tokens":20,'
    b'"cache_read_input_tokens":1000,"output_tokens":1,'
    b'"server_tool_use":{"web_search_requests":0}}}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":50}}\n\n'
    b"event: message_stop\n"
    b'data: {"type":"message_stop"}\n\n'
)
JSON_BODY = json.dumps(
    {
        "id": "m2",
        "model": "claude-sonnet-x",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
).encode()


class FakeUpstream:
    def __init__(self):
        self.requests = []
        up = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                up.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                stream = b'"stream": true' in body or b'"stream":true' in body
                payload = SSE_BODY if stream else JSON_BODY
                self.send_response(200)
                self.send_header(
                    "content-type", "text/event-stream" if stream else "application/json"
                )
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def upstream():
    up = FakeUpstream()
    yield up
    up.close()


@pytest.fixture
def book(tmp_path):
    return ts.GuestBook(tmp_path / "guests.json")


@pytest.fixture
def ledger(tmp_path):
    return ts.Ledger(tmp_path / "usage.db")


@pytest.fixture
def gate(upstream, book, ledger):
    server = ts.make_server(
        "127.0.0.1",
        0,
        book=book,
        ledger=ledger,
        owner_token=lambda: OWNER,
        upstream=("http", "127.0.0.1", upstream.port),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def call(gate, path="/v1/messages?beta=true", body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", gate.server_address[1], timeout=10)
    data = json.dumps(body if body is not None else {"model": "m", "messages": []})
    conn.request("POST", path, body=data, headers=headers or {})
    resp = conn.getresponse()
    return resp.status, dict(resp.getheaders()), resp.read()


# --------------------------------------------------------------------------- #
# guest book
# --------------------------------------------------------------------------- #
class TestGuestBook:
    def test_add_returns_a_pass_and_stores_only_its_hash(self, book, tmp_path):
        p = book.add("maria", daily_limit=1000, telegram_id=42)
        text = (tmp_path / "guests.json").read_text()
        assert p.startswith("sbx-") and len(p) > 30
        assert p not in text
        assert book.lookup(p).name == "maria"
        assert book.lookup(p).telegram_id == 42

    def test_unknown_pass_is_none(self, book):
        book.add("maria", daily_limit=1000)
        assert book.lookup("sbx-nope") is None
        assert book.lookup("") is None

    def test_reloads_from_disk(self, book, tmp_path):
        p = book.add("maria", daily_limit=1000)
        again = ts.GuestBook(tmp_path / "guests.json")
        assert again.lookup(p).name == "maria"

    def test_disable_and_remove(self, book):
        p = book.add("maria", daily_limit=1000)
        book.set_enabled("maria", False)
        assert book.lookup(p).enabled is False
        book.remove("maria")
        assert book.lookup(p) is None

    def test_duplicate_name_rejected(self, book):
        book.add("maria", daily_limit=1000)
        with pytest.raises(ValueError):
            book.add("maria", daily_limit=1000)


# --------------------------------------------------------------------------- #
# usage accounting
# --------------------------------------------------------------------------- #
def test_weighted_uses_the_owners_price_weights():
    """Same weights as the owner's weekly measure (1% of the week ~ 8.7M):
    input 1, cache write 1.25, cache read 0.1, output 5."""
    u = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 1000,
    }
    assert ts.weighted(u) == 100 + 50 * 5 + 25 + 100


def test_usage_parser_reads_sse_across_chunk_boundaries():
    p = ts.UsageParser()
    for i in range(0, len(SSE_BODY), 7):  # tiny chunks split lines and JSON
        p.feed(SSE_BODY[i : i + 7])
    assert p.model == "claude-opus-x"
    assert p.usage["input_tokens"] == 100
    assert p.usage["output_tokens"] == 50
    assert p.usage["cache_read_input_tokens"] == 1000


def test_usage_parser_reads_plain_json():
    p = ts.UsageParser()
    p.feed(JSON_BODY)
    p.close()
    assert p.model == "claude-sonnet-x"
    assert p.usage == {"input_tokens": 10, "output_tokens": 5}


def test_ledger_sums_today_per_guest(ledger):
    ledger.record("maria", "m", {"input_tokens": 10, "output_tokens": 5})
    ledger.record("maria", "m", {"input_tokens": 1, "output_tokens": 1})
    ledger.record("ivan", "m", {"input_tokens": 999})
    assert ledger.spent_today("maria") == (10 + 25) + (1 + 5)
    assert ledger.spent_today("ivan") == 999
    assert ledger.spent_today("nobody") == 0


# --------------------------------------------------------------------------- #
# the gate itself
# --------------------------------------------------------------------------- #
class TestGate:
    def test_unknown_pass_is_refused_and_upstream_not_called(self, gate, upstream):
        status, _, _ = call(gate, headers={"authorization": "Bearer sbx-wrong"})
        assert status == 401
        assert upstream.requests == []

    def test_no_pass_is_refused(self, gate, upstream):
        status, _, _ = call(gate)
        assert status == 401
        assert upstream.requests == []

    def test_guest_pass_is_swapped_for_owner_login(self, gate, upstream, book):
        p = book.add("maria", daily_limit=10**9)
        status, _, body = call(
            gate,
            headers={
                "authorization": f"Bearer {p}",
                "anthropic-beta": "fine-grained-tool-streaming-2025-05-14",
            },
        )
        assert status == 200
        assert json.loads(body)["model"] == "claude-sonnet-x"
        sent = {k.lower(): v for k, v in upstream.requests[0]["headers"].items()}
        assert sent["authorization"] == f"Bearer {OWNER}"
        assert "x-api-key" not in sent
        betas = sent["anthropic-beta"].split(",")
        assert "oauth-2025-04-20" in betas
        assert "fine-grained-tool-streaming-2025-05-14" in betas
        assert p not in json.dumps(sent)

    def test_pass_in_x_api_key_also_works(self, gate, upstream, book):
        p = book.add("maria", daily_limit=10**9)
        status, _, _ = call(gate, headers={"x-api-key": p})
        assert status == 200
        sent = {k.lower(): v for k, v in upstream.requests[0]["headers"].items()}
        assert "x-api-key" not in sent

    def test_only_message_endpoints_are_forwarded(self, gate, upstream, book):
        p = book.add("maria", daily_limit=10**9)
        status, _, _ = call(gate, path="/v1/files", headers={"x-api-key": p})
        assert status == 403
        assert upstream.requests == []
        status, _, _ = call(
            gate, path="/v1/messages/count_tokens", headers={"x-api-key": p}
        )
        assert status == 200

    def test_streamed_usage_is_recorded_for_the_guest(self, gate, book, ledger):
        p = book.add("maria", daily_limit=10**9)
        status, _, body = call(
            gate,
            body={"model": "m", "stream": True, "messages": []},
            headers={"x-api-key": p},
        )
        assert status == 200
        assert b"message_stop" in body  # stream passed through intact
        assert ledger.spent_today("maria") == 100 + 50 * 5 + 25 + 100

    def test_guest_over_daily_limit_gets_429_and_upstream_not_called(
        self, gate, upstream, book, ledger
    ):
        p = book.add("maria", daily_limit=100)
        ledger.record("maria", "m", {"input_tokens": 100})
        status, _, body = call(gate, headers={"x-api-key": p})
        assert status == 429
        err = json.loads(body)
        assert err["type"] == "error"
        assert err["error"]["type"] == "rate_limit_error"
        assert upstream.requests == []

    def test_disabled_guest_is_refused(self, gate, upstream, book):
        p = book.add("maria", daily_limit=10**9)
        book.set_enabled("maria", False)
        status, _, _ = call(gate, headers={"x-api-key": p})
        assert status == 403
        assert upstream.requests == []

    def test_owner_login_never_reaches_the_guest(self, gate, book):
        p = book.add("maria", daily_limit=10**9)
        _, headers, body = call(gate, headers={"x-api-key": p})
        assert OWNER not in json.dumps(headers)
        assert OWNER.encode() not in body


# --------------------------------------------------------------------------- #
# "no limit until <time>": lifts the daily limit for a while, ends by itself
# --------------------------------------------------------------------------- #
class TestUnlimitedUntil:
    def test_guest_over_the_limit_passes_while_unlimited(self, gate, upstream, book, ledger):
        import time
        p = book.add("maria", daily_limit=100)
        ledger.record("maria", "m", {"input_tokens": 10_000})
        book.set_unlimited_until("maria", time.time() + 3600)
        status, _, _ = call(gate, headers={"x-api-key": p})
        assert status == 200 and len(upstream.requests) == 1

    def test_limit_is_back_by_itself_when_the_time_is_up(self, gate, upstream, book, ledger):
        import time
        p = book.add("maria", daily_limit=100)
        ledger.record("maria", "m", {"input_tokens": 10_000})
        book.set_unlimited_until("maria", time.time() - 1)       # already over
        status, _, _ = call(gate, headers={"x-api-key": p})
        assert status == 429 and upstream.requests == []

    def test_unlimited_until_survives_a_reload(self, book, tmp_path):
        import time
        book.add("maria", daily_limit=100)
        until = time.time() + 86400
        book.set_unlimited_until("maria", until)
        assert ts.GuestBook(tmp_path / "guests.json").get("maria").unlimited_until == until


# --------------------------------------------------------------------------- #
# spending alert: the owner hears when a guest burns more than 1% of the week in
# an hour (a runaway loop, a service calling Claude in circles)
# --------------------------------------------------------------------------- #
class TestSpendWatch:
    def _watch(self, ledger, sent, now, **kw):
        return ts.SpendWatch(ledger, notify=sent.append, clock=lambda: now[0],
                             background=False, **kw)

    def test_spent_since_counts_only_the_window(self, ledger):
        ledger.record("maria", "m", {"input_tokens": 10}, ts_=1000.0)
        ledger.record("maria", "m", {"input_tokens": 7}, ts_=5000.0)
        assert ledger.spent_since("maria", 2000.0) == 7

    def test_over_one_percent_in_an_hour_alerts_once(self, ledger):
        sent, now = [], [100_000.0]
        watch = self._watch(ledger, sent, now)
        ledger.record("maria", "m", {"input_tokens": ts.WEEK_PERCENT - 10}, ts_=now[0] - 600)
        watch.check("maria")
        assert sent == []
        ledger.record("maria", "m", {"input_tokens": 20}, ts_=now[0])
        watch.check("maria")
        watch.check("maria")
        assert len(sent) == 1
        assert "maria" in sent[0] and "%" in sent[0]

    def test_alerts_again_only_after_the_cooldown(self, ledger):
        sent, now = [], [100_000.0]
        watch = self._watch(ledger, sent, now)
        ledger.record("maria", "m", {"input_tokens": 2 * ts.WEEK_PERCENT}, ts_=now[0])
        watch.check("maria")
        now[0] += 1800
        ledger.record("maria", "m", {"input_tokens": 2 * ts.WEEK_PERCENT}, ts_=now[0])
        watch.check("maria")
        assert len(sent) == 1
        now[0] += 3600
        ledger.record("maria", "m", {"input_tokens": 2 * ts.WEEK_PERCENT}, ts_=now[0])
        watch.check("maria")
        assert len(sent) == 2

    def test_guests_are_watched_separately(self, ledger):
        sent, now = [], [100_000.0]
        watch = self._watch(ledger, sent, now)
        ledger.record("maria", "m", {"input_tokens": 2 * ts.WEEK_PERCENT}, ts_=now[0])
        ledger.record("ivan", "m", {"input_tokens": 100}, ts_=now[0])
        watch.check("maria")
        watch.check("ivan")
        assert len(sent) == 1 and "maria" in sent[0]

    def test_a_failing_notify_never_breaks_the_request(self, ledger):
        now = [100_000.0]

        def boom(text):
            raise RuntimeError("telegram down")
        watch = ts.SpendWatch(ledger, notify=boom, clock=lambda: now[0], background=False)
        ledger.record("maria", "m", {"input_tokens": 2 * ts.WEEK_PERCENT}, ts_=now[0])
        watch.check("maria")          # no exception

    def test_the_gate_checks_after_each_paid_request(self, upstream, book, ledger):
        seen = []

        class Spy:
            def check(self, guest):
                seen.append(guest)
        server = ts.make_server("127.0.0.1", 0, book=book, ledger=ledger,
                                owner_token=lambda: OWNER,
                                upstream=("http", "127.0.0.1", upstream.port), watch=Spy())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            p = book.add("maria", daily_limit=10**9)
            assert call(server, headers={"x-api-key": p})[0] == 200
        finally:
            server.shutdown()
        assert seen == ["maria"]
