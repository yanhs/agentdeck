"""Telegram turnstile: one real bot token on the host, each guest sandbox sees only
its own user's updates and may only write to its own chat."""

import http.client
import json
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "turnstile"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import turnstile as ts  # noqa: E402

import tgrelay as tr  # noqa: E402

REAL = "8893376561:REAL-SECRET"
ANNA, BORIS = 1001, 2002


class FakeTelegram:
    """Stands in for api.telegram.org (records what the relay forwards)."""

    def __init__(self):
        self.calls = []
        self.downloads = []

    def call(self, method, params, files=None):
        self.calls.append((method, dict(params), dict(files or {})))
        if method == "getFile":
            return {"ok": True, "result": {"file_id": params["file_id"],
                                           "file_path": "photos/f1.jpg"}}
        if method == "getMe":
            return {"ok": True, "result": {"id": 1, "is_bot": True, "username": "gb"}}
        return {"ok": True, "result": {"message_id": 7}}

    def download(self, file_path):
        self.downloads.append(file_path)
        return b"JPEGDATA"


@pytest.fixture
def book(tmp_path):
    return ts.GuestBook(tmp_path / "guests.json")


@pytest.fixture
def passes(book):
    return {
        "anna": book.add("anna", daily_limit=10, telegram_id=ANNA),
        "boris": book.add("boris", daily_limit=10, telegram_id=BORIS),
    }


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def relay(book, tg, passes):
    return tr.Relay(book, tg)


def msg(uid, text="hi", update_id=1, **extra):
    m = {"message_id": 5, "from": {"id": uid}, "chat": {"id": uid, "type": "private"},
         "text": text}
    m.update(extra)
    return {"update_id": update_id, "message": m}


# --------------------------------------------------------------------------- #
# routing updates
# --------------------------------------------------------------------------- #
class TestRouting:
    def test_each_guest_gets_only_own_updates(self, relay):
        relay.route(msg(ANNA, "for anna", 1))
        relay.route(msg(BORIS, "for boris", 2))
        anna = relay.get_updates("anna", offset=0, limit=100, timeout=0)
        boris = relay.get_updates("boris", offset=0, limit=100, timeout=0)
        assert [u["message"]["text"] for u in anna] == ["for anna"]
        assert [u["message"]["text"] for u in boris] == ["for boris"]

    def test_callback_queries_route_by_sender(self, relay):
        relay.route({"update_id": 3, "callback_query": {
            "id": "cb1", "from": {"id": ANNA}, "data": "use:1"}})
        assert relay.get_updates("anna", 0, 100, 0)[0]["callback_query"]["id"] == "cb1"
        assert relay.get_updates("boris", 0, 100, 0) == []

    def test_stranger_is_told_once_and_nothing_is_queued(self, relay, tg):
        relay.route(msg(999, "let me in", 1))
        relay.route(msg(999, "please", 2))
        assert relay.get_updates("anna", 0, 100, 0) == []
        notices = [c for c in tg.calls if c[0] == "sendMessage"]
        assert len(notices) == 1
        assert notices[0][1]["chat_id"] == 999

    def test_offset_confirms_and_drops_older_updates(self, relay):
        relay.route(msg(ANNA, "a", 10))
        relay.route(msg(ANNA, "b", 11))
        assert len(relay.get_updates("anna", 0, 100, 0)) == 2
        after = relay.get_updates("anna", offset=11, limit=100, timeout=0)
        assert [u["update_id"] for u in after] == [11]
        assert relay.get_updates("anna", offset=12, limit=100, timeout=0) == []

    def test_long_poll_wakes_up_on_a_new_update(self, relay):
        got = []
        t = threading.Thread(target=lambda: got.extend(
            relay.get_updates("anna", 0, 100, timeout=5)))
        t.start()
        time.sleep(0.1)
        relay.route(msg(ANNA, "late", 20))
        t.join(timeout=3)
        assert [u["update_id"] for u in got] == [20]

    def test_disabled_guest_gets_nothing(self, relay, book):
        book.set_enabled("anna", False)
        relay.route(msg(ANNA, "x", 1))
        assert relay.get_updates("anna", 0, 100, 0) == []


# --------------------------------------------------------------------------- #
# methods a guest may call
# --------------------------------------------------------------------------- #
class TestMethods:
    def test_send_to_own_chat_is_forwarded(self, relay, tg):
        res = relay.call("anna", "sendMessage", {"chat_id": str(ANNA), "text": "hi"})
        assert res["ok"] is True
        assert tg.calls[-1][0] == "sendMessage"

    def test_send_to_someone_else_is_refused(self, relay, tg):
        res = relay.call("anna", "sendMessage", {"chat_id": str(BORIS), "text": "hey"})
        assert res["ok"] is False and res["error_code"] == 403
        assert not any(c[0] == "sendMessage" for c in tg.calls)

    def test_edit_and_delete_are_chat_checked_too(self, relay, tg):
        ok = relay.call("anna", "editMessageText",
                        {"chat_id": ANNA, "message_id": 1, "text": "x"})
        bad = relay.call("anna", "deleteMessage", {"chat_id": BORIS, "message_id": 1})
        assert ok["ok"] is True and bad["ok"] is False

    def test_callback_answer_only_for_own_buttons(self, relay, tg):
        relay.route({"update_id": 3, "callback_query": {
            "id": "cb-anna", "from": {"id": ANNA}, "data": "x"}})
        assert relay.call("anna", "answerCallbackQuery",
                          {"callback_query_id": "cb-anna"})["ok"] is True
        assert relay.call("boris", "answerCallbackQuery",
                          {"callback_query_id": "cb-anna"})["ok"] is False

    def test_files_only_from_own_messages(self, relay, tg):
        relay.route(msg(ANNA, update_id=4, photo=[{"file_id": "F-anna", "width": 1}]))
        got = relay.call("anna", "getFile", {"file_id": "F-anna"})
        assert got["result"]["file_path"] == "photos/f1.jpg"
        assert relay.call("boris", "getFile", {"file_id": "F-anna"})["ok"] is False
        assert relay.download("anna", "photos/f1.jpg") == b"JPEGDATA"
        assert relay.download("boris", "photos/f1.jpg") is None

    def test_commands_are_set_for_the_guest_chat_only(self, relay, tg):
        cmds = json.dumps([{"command": "list", "description": "terminals"}])
        relay.call("anna", "setMyCommands", {"commands": cmds})
        method, params, _ = tg.calls[-1]
        assert method == "setMyCommands"
        assert json.loads(params["scope"]) == {"type": "chat", "chat_id": ANNA}

    def test_webhook_and_logout_are_harmless_no_ops(self, relay, tg):
        for method in ("deleteWebhook", "setWebhook", "logOut", "close"):
            assert relay.call("anna", method, {})["ok"] is True
        assert tg.calls == []

    def test_unknown_method_is_refused(self, relay, tg):
        res = relay.call("anna", "banChatMember", {"chat_id": ANNA, "user_id": 5})
        assert res["ok"] is False and res["error_code"] == 403
        assert tg.calls == []


# --------------------------------------------------------------------------- #
# HTTP layer (what python-telegram-bot inside the sandbox talks to)
# --------------------------------------------------------------------------- #
@pytest.fixture
def server(relay):
    srv = tr.make_server("127.0.0.1", 0, relay)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def post(server, path, body=b"", ctype="application/x-www-form-urlencoded"):
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    c.request("POST", path, body=body, headers={"content-type": ctype})
    r = c.getresponse()
    return r.status, r.read()


class TestHttp:
    def test_bad_pass_is_401(self, server):
        status, body = post(server, "/botsbx-wrong/getMe")
        assert status == 401 and json.loads(body)["ok"] is False

    def test_form_encoded_send_message(self, server, passes, tg):
        form = urllib.parse.urlencode({"chat_id": ANNA, "text": "привет"}).encode()
        status, body = post(server, f"/bot{passes['anna']}/sendMessage", form)
        assert status == 200 and json.loads(body)["ok"] is True
        assert tg.calls[-1][1]["text"] == "привет"

    def test_json_body_get_updates(self, server, passes, relay):
        relay.route(msg(ANNA, "x", 9))
        status, body = post(server, f"/bot{passes['anna']}/getUpdates",
                            json.dumps({"offset": 0, "timeout": 0}).encode(),
                            "application/json")
        assert [u["update_id"] for u in json.loads(body)["result"]] == [9]

    def test_multipart_upload_is_chat_checked_and_forwarded(self, server, passes, tg):
        boundary = "XyZ"
        parts = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
            f"{ANNA}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
            f"filename=\"a.txt\"\r\nContent-Type: text/plain\r\n\r\nhello\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        status, _ = post(server, f"/bot{passes['anna']}/sendDocument", parts,
                         f"multipart/form-data; boundary={boundary}")
        assert status == 200
        method, params, files = tg.calls[-1]
        assert method == "sendDocument" and params["chat_id"] == str(ANNA)
        assert files["document"][0] == "a.txt" and files["document"][1] == b"hello"

    def test_file_download_route(self, server, passes, relay):
        relay.route(msg(ANNA, update_id=4, document={"file_id": "D1"}))
        relay.call("anna", "getFile", {"file_id": "D1"})
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        c.request("GET", f"/file/bot{passes['anna']}/photos/f1.jpg")
        r = c.getresponse()
        assert r.status == 200 and r.read() == b"JPEGDATA"

    def test_real_token_never_leaks(self, server, passes):
        status, body = post(server, f"/bot{passes['anna']}/getMe")
        assert REAL.encode() not in body


# --------------------------------------------------------------------------- #
# /start and /web: the guest learns its web address and password in Telegram
# --------------------------------------------------------------------------- #
INFO = {"anna": {"url": "https://sb-anna.reimake.com", "password": "pw-ANNA-1"}}


@pytest.fixture
def info_relay(book, tg, passes):
    return tr.Relay(book, tg, info_for=INFO.get)


def _sent(tg):
    return [c for c in tg.calls if c[0] == "sendMessage"]


@pytest.mark.parametrize("cmd", ["/start", "/web", "/web@Yanhs_guest_bot"])
def test_start_and_web_answer_with_address_and_password(info_relay, tg, cmd):
    info_relay.route(msg(ANNA, cmd, 30))
    assert info_relay.get_updates("anna", 0, 100, 0) == []   # handled here, not by the sandbox
    (method, params, _), = _sent(tg)
    assert params["chat_id"] == ANNA
    assert "https://sb-anna.reimake.com" in params["text"]
    assert "pw-ANNA-1" in params["text"]
    assert "https://sb-anna.reimake.com/change-password" in params["text"]


def test_other_messages_still_go_to_the_sandbox(info_relay, tg):
    info_relay.route(msg(ANNA, "сделай сайт", 31))
    assert [u["message"]["text"] for u in info_relay.get_updates("anna", 0, 100, 0)] == ["сделай сайт"]
    assert _sent(tg) == []


def test_guest_without_web_info_gets_a_plain_answer(info_relay, tg):
    info_relay.route(msg(BORIS, "/web", 32))
    (_, params, _), = _sent(tg)
    assert params["chat_id"] == BORIS and "ещё не" in params["text"]


def test_web_is_added_to_the_menu_the_sandbox_sets(info_relay, tg):
    cmds = json.dumps([{"command": "list", "description": "terminals"}])
    info_relay.call("anna", "setMyCommands", {"commands": cmds})
    sent = json.loads(tg.calls[-1][1]["commands"])
    assert [c["command"] for c in sent] == ["list", "web"]


# --------------------------------------------------------------------------- #
# the real-Telegram client keeps its connection (no new DNS + TLS per call) and
# logs how long each call took
# --------------------------------------------------------------------------- #
def test_telegram_client_reuses_one_connection(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    peers = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            peers.append(self.client_address[1])
            n = int(self.headers.get("content-length") or 0)
            self.rfile.read(n)
            body = b'{"ok": true, "result": {"message_id": 1}}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    lines = []
    api = tr.TelegramAPI("T", base=f"http://127.0.0.1:{srv.server_address[1]}",
                         log=lines.append)
    for _ in range(3):
        assert api.call("sendMessage", {"chat_id": 1, "text": "x"})["ok"] is True
    srv.shutdown()
    assert len(set(peers)) == 1            # one TCP connection for all three
    assert any("sendMessage" in l and "ms" in l for l in lines)


# --------------------------------------------------------------------------- #
# /services: the guest sees what runs in the sandbox without a terminal and can
# restart / stop / start it with buttons (answered here, the keeper does the work)
# --------------------------------------------------------------------------- #
class FakeServices:
    def __init__(self):
        now = time.time()
        self.state = {
            "anna": {
                "tailscale": {"state": "running", "pid": 10, "since": now - 3 * 3600,
                              "restarts": 0, "last_exit": None},
                "site": {"state": "waiting", "pid": None, "since": now - 60,
                         "restarts": 3, "last_exit": 1},
                "bot": {"state": "stopped", "pid": None, "since": None,
                        "restarts": 0, "last_exit": None},
            },
            "boris": {},
        }
        self.actions = []

    def status(self, guest):
        return self.state.get(guest)          # None = the keeper can't be reached

    def act(self, guest, action, svc):
        self.actions.append((guest, action, svc))
        return svc in (self.state.get(guest) or {})


@pytest.fixture
def services():
    return FakeServices()


@pytest.fixture
def svc_relay(book, tg, passes, services):
    # synchronous and without the pause before the redraw, so the tests can look at once
    return tr.Relay(book, tg, services=services, background=False, settle=0)


def _buttons(params):
    markup = params["reply_markup"]
    markup = json.loads(markup) if isinstance(markup, str) else markup
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


def press(uid, data, update_id=50, cb_id="cb1"):
    return {"update_id": update_id, "callback_query": {
        "id": cb_id, "from": {"id": uid}, "data": data,
        "message": {"message_id": 9, "chat": {"id": uid, "type": "private"}}}}


def test_services_command_lists_them_with_buttons(svc_relay, tg):
    svc_relay.route(msg(ANNA, "/services", 40))
    assert svc_relay.get_updates("anna", 0, 100, 0) == []     # not the sandbox's business
    (_, params, _), = _sent(tg)
    text = params["text"]
    assert params["chat_id"] == ANNA
    assert "tailscale" in text and "работает" in text
    assert "site" in text and "упала" in text
    assert "bot" in text and "остановлена" in text
    buttons = _buttons(params)
    assert "svc:restart:tailscale" in buttons and "svc:stop:tailscale" in buttons
    assert "svc:start:bot" in buttons and "svc:stop:bot" not in buttons
    assert "svc:refresh:" in buttons


def test_service_button_acts_and_refreshes_the_message(svc_relay, tg, services):
    svc_relay.route(press(ANNA, "svc:restart:tailscale"))
    assert services.actions == [("anna", "restart", "tailscale")]
    assert svc_relay.get_updates("anna", 0, 100, 0) == []
    methods = [c[0] for c in tg.calls]
    assert "answerCallbackQuery" in methods
    (_, edit, _), = [c for c in tg.calls if c[0] == "editMessageText"]
    assert edit["chat_id"] == ANNA and edit["message_id"] == 9
    assert "tailscale" in edit["text"]


def test_a_button_acts_only_on_the_pressers_own_sandbox(svc_relay, services):
    svc_relay.route(press(BORIS, "svc:stop:tailscale"))
    assert services.actions == [("boris", "stop", "tailscale")]


@pytest.mark.parametrize("data", ["svc:rm:tailscale", "svc:stop:../x", "svc:stop:Bad Name"])
def test_a_bad_button_does_nothing(svc_relay, tg, services, data):
    svc_relay.route(press(ANNA, data))
    assert services.actions == []
    assert "answerCallbackQuery" in [c[0] for c in tg.calls]      # spinner stops anyway


def test_other_buttons_still_go_to_the_sandbox(svc_relay):
    svc_relay.route(press(ANNA, "lib:open:abcd1234"))
    assert [u["callback_query"]["data"] for u in svc_relay.get_updates("anna", 0, 100, 0)] \
        == ["lib:open:abcd1234"]


def test_no_services_explains_what_they_are(svc_relay, tg):
    svc_relay.route(msg(BORIS, "/services", 41))
    (_, params, _), = _sent(tg)
    assert "Служб пока нет" in params["text"]


def test_keeper_out_of_reach_is_said_plainly(book, tg, passes):
    class Down(FakeServices):
        def status(self, guest):
            return None
    relay = tr.Relay(book, tg, services=Down(), background=False, settle=0)
    relay.route(msg(ANNA, "/services", 42))
    (_, params, _), = _sent(tg)
    assert "недоступ" in params["text"]


def test_services_is_added_to_the_menu(svc_relay, tg):
    cmds = json.dumps([{"command": "list", "description": "terminals"}])
    svc_relay.call("anna", "setMyCommands", {"commands": cmds})
    sent = json.loads(tg.calls[-1][1]["commands"])
    assert [c["command"] for c in sent] == ["list", "web", "services"]


def test_welcome_mentions_services():
    assert "/services" in tr.web_text({"url": "https://x"}, welcome=True)


def test_keeper_client_talks_to_the_sandbox_keeper():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits = []

    class H(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            hits.append(("GET", self.path))
            self._send(200, {"max": 5, "services": {"web": {"state": "running"}}})

        def do_POST(self):
            hits.append(("POST", self.path))
            self._send(200 if self.path == "/restart/web" else 404, {})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        client = tr.KeeperClient(lambda guest: f"http://127.0.0.1:{port}" if guest == "anna" else None)
        assert client.status("anna") == {"web": {"state": "running"}}
        assert client.act("anna", "restart", "web") is True
        assert client.act("anna", "restart", "nope") is False
        assert client.status("boris") is None                    # no address known
        dead = tr.KeeperClient(lambda guest: "http://127.0.0.1:1")
        assert dead.status("anna") is None and dead.act("anna", "stop", "web") is False
    finally:
        srv.shutdown()
    assert ("POST", "/restart/web") in hits


def test_a_slow_keeper_does_not_hold_up_other_guests(book, tg, passes):
    """Updates for everyone come through one polling loop: /services must not block it."""
    class Slow(FakeServices):
        def status(self, guest):
            time.sleep(1.5)
            return {}
    relay = tr.Relay(book, tg, services=Slow())
    started = time.time()
    relay.route(msg(ANNA, "/services", 60))
    relay.route(press(ANNA, "svc:restart:web", 61))
    relay.route(msg(BORIS, "привет", 62))
    assert time.time() - started < 0.5
    assert [u["message"]["text"] for u in relay.get_updates("boris", 0, 100, 0)] == ["привет"]


def test_a_skipped_service_names_the_limit():
    text, _ = tr.services_view({"extra": {"state": "skipped", "limit": 20}})
    assert "extra" in text and "20" in text
