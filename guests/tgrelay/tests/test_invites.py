"""Invitations: a stranger writes to the guest bot -> the owner gets "Пустить?" with buttons
in the same bot -> on "Пустить" the sandbox is created and the person gets their address
and password. Only the owner's button counts; nothing is created twice."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "turnstile"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import turnstile as ts  # noqa: E402

import tgrelay as tr  # noqa: E402

OWNER, MASHA, IVAN, ANNA = 4242, 5550001, 5550002, 1001


class FakeTelegram:
    def __init__(self):
        self.calls = []

    def call(self, method, params, files=None):
        self.calls.append((method, dict(params)))
        return {"ok": True, "result": {"message_id": 77}}

    def to(self, chat, method="sendMessage"):
        return [p for m, p in self.calls if m == method and p.get("chat_id") == chat]


class FakeProvisioner:
    def __init__(self, ok=True):
        self.ok, self.made = ok, []

    def provision(self, name, tid):
        self.made.append((name, tid))
        return (True, "") if self.ok else (False, "docker: no space left")


@pytest.fixture
def book(tmp_path):
    b = ts.GuestBook(tmp_path / "guests.json")
    b.add("anna", daily_limit=10, telegram_id=ANNA)
    return b


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def prov():
    return FakeProvisioner()


def make(book, tg, prov, tmp_path, owner=OWNER):
    inv = tr.Invitations(tmp_path / "requests.json", owner_tid=owner, provisioner=prov,
                         taken=lambda: set(book.names()), background=False)
    info = {"masha": {"url": "https://sb-masha.example.test", "password": "pw-M"}}
    return tr.Relay(book, tg, invites=inv, info_for=info.get), inv


@pytest.fixture
def relay(book, tg, prov, tmp_path):
    return make(book, tg, prov, tmp_path)[0]


def hello(uid, text="/start", username="Masha", first="Маша", update_id=1):
    who = {"id": uid, "first_name": first}
    if username:
        who["username"] = username
    return {"update_id": update_id, "message": {"message_id": 3, "from": who,
                                                "chat": {"id": uid}, "text": text}}


def press(uid, data, update_id=50):
    return {"update_id": update_id, "callback_query": {
        "id": "cb", "from": {"id": uid}, "data": data,
        "message": {"message_id": 77, "chat": {"id": uid}}}}


def buttons(params):
    markup = json.loads(params["reply_markup"])
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


def test_a_stranger_becomes_a_request_to_the_owner(relay, tg, tmp_path):
    relay.route(hello(MASHA))
    (ask,) = tg.to(OWNER)
    assert "Маша" in ask["text"] and "@Masha" in ask["text"] and str(MASHA) in ask["text"]
    assert buttons(ask) == [f"inv:ok:{MASHA}", f"inv:no:{MASHA}"]
    (told,) = tg.to(MASHA)
    assert "владельц" in told["text"]
    saved = json.loads((tmp_path / "requests.json").read_text())
    assert saved[str(MASHA)]["state"] == "pending"


def test_writing_again_does_not_bother_the_owner_twice(relay, tg):
    relay.route(hello(MASHA, update_id=1))
    relay.route(hello(MASHA, "можно?", update_id=2))
    relay.route(hello(MASHA, "ау", update_id=3))
    assert len(tg.to(OWNER)) == 1
    assert len(tg.to(MASHA)) == 2            # "sent" + one "still waiting" (then quiet)


def test_owner_lets_in_and_the_sandbox_is_made(relay, tg, prov, book):
    relay.route(hello(MASHA))
    relay.route(press(OWNER, f"inv:ok:{MASHA}"))
    assert prov.made == [("masha", MASHA)]
    welcome = tg.to(MASHA)[-1]["text"]
    assert "https://sb-masha.example.test" in welcome and "pw-M" in welcome
    done = tg.to(OWNER, "editMessageText")[-1]["text"]
    assert "✅" in done and "masha" in done


def test_only_the_owners_button_counts(relay, tg, prov):
    relay.route(hello(MASHA))
    relay.route(press(ANNA, f"inv:ok:{MASHA}"))           # a guest forging the button
    relay.route(press(MASHA, f"inv:ok:{MASHA}"))          # the stranger herself
    assert prov.made == []
    assert relay.get_updates("anna", 0, 100, 0) == []     # not passed to anna's sandbox


def test_a_double_press_makes_one_sandbox(relay, prov):
    relay.route(hello(MASHA))
    relay.route(press(OWNER, f"inv:ok:{MASHA}", 50))
    relay.route(press(OWNER, f"inv:ok:{MASHA}", 51))
    assert len(prov.made) == 1


def test_owner_says_no(relay, tg, prov):
    relay.route(hello(MASHA))
    relay.route(press(OWNER, f"inv:no:{MASHA}"))
    assert prov.made == []
    assert "не дал" in tg.to(MASHA)[-1]["text"]
    assert "✖" in tg.to(OWNER, "editMessageText")[-1]["text"]
    relay.route(hello(MASHA, "ну пожалуйста", update_id=9))
    assert len(tg.to(OWNER)) == 1                         # no new request after a "no"


def test_a_failed_sandbox_is_reported_and_can_be_retried(book, tg, tmp_path):
    bad = FakeProvisioner(ok=False)
    relay, _ = make(book, tg, bad, tmp_path)
    relay.route(hello(MASHA))
    relay.route(press(OWNER, f"inv:ok:{MASHA}"))
    edit = tg.to(OWNER, "editMessageText")[-1]
    assert "🚨" in edit["text"] and "no space" in edit["text"]
    assert f"inv:ok:{MASHA}" in buttons(edit)             # the buttons stay for a retry
    bad.ok = True
    relay.route(press(OWNER, f"inv:ok:{MASHA}", 52))
    assert len(bad.made) == 2


@pytest.mark.parametrize("username,expected", [
    ("Masha", "masha"), ("Masha_P", "masha-p"), (None, f"g{IVAN}"),
    ("anna", f"g{IVAN}"),                                  # taken by a guest already
    ("x" * 40, "x" * 31),
])
def test_the_sandbox_name(book, tg, prov, tmp_path, username, expected):
    relay, _ = make(book, tg, prov, tmp_path)
    relay.route(hello(IVAN, username=username))
    relay.route(press(OWNER, f"inv:ok:{IVAN}"))
    assert prov.made == [(expected, IVAN)]


def test_requests_survive_a_restart(book, tg, prov, tmp_path):
    relay, _ = make(book, tg, prov, tmp_path)
    relay.route(hello(MASHA))
    relay2, _ = make(book, tg, prov, tmp_path)            # the relay restarted
    relay2.route(press(OWNER, f"inv:ok:{MASHA}"))
    assert prov.made == [("masha", MASHA)]


def test_without_an_owner_strangers_are_just_told_no(book, tg, prov, tmp_path):
    relay = tr.Relay(book, tg)                            # invitations off
    relay.route(hello(MASHA))
    assert tg.to(OWNER) == []
    assert tg.to(MASHA)[0]["text"] == "Это закрытый бот. Доступ выдаёт владелец."
