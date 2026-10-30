"""Invitations, the guest bot's side (decision 27.10.2026): a stranger writes to the guest
bot -> the request is RECORDED (guests/invites.py RequestBook) and the person is told it
went to the owner. "Пустить?" is asked by the owner's own bot (tg_bridge + invites.Desk,
see guests/tests/test_invites_desk.py); the guest bot can't create sandboxes and ignores
any invitation button."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "turnstile"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import turnstile as ts  # noqa: E402

import invites  # noqa: E402
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


@pytest.fixture
def book(tmp_path):
    b = ts.GuestBook(tmp_path / "guests.json")
    b.add("anna", week_limit=10, telegram_id=ANNA)
    return b


@pytest.fixture
def tg():
    return FakeTelegram()


def make(book, tg, tmp_path, owner=OWNER):
    inv = tr.Invitations(invites.RequestBook(tmp_path / "requests.json"), owner_tid=owner)
    return tr.Relay(book, tg, invites=inv), inv


@pytest.fixture
def relay(book, tg, tmp_path):
    return make(book, tg, tmp_path)[0]


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


def test_a_stranger_becomes_a_recorded_request(relay, tg, tmp_path):
    relay.route(hello(MASHA))
    assert tg.to(OWNER) == []                    # the guest bot doesn't ask the owner
    (told,) = tg.to(MASHA)
    assert "владельц" in told["text"]
    saved = json.loads((tmp_path / "requests.json").read_text())
    assert saved[str(MASHA)]["state"] == "pending"
    assert saved[str(MASHA)]["first_name"] == "Маша" and saved[str(MASHA)]["username"] == "Masha"


def test_writing_again_says_waiting_once_then_quiet(relay, tg):
    relay.route(hello(MASHA, update_id=1))
    relay.route(hello(MASHA, "можно?", update_id=2))
    relay.route(hello(MASHA, "ау", update_id=3))
    assert len(tg.to(MASHA)) == 2            # "sent" + one "still waiting" (then quiet)


@pytest.mark.parametrize("presser", [OWNER, ANNA, MASHA])
def test_invitation_buttons_do_nothing_in_the_guest_bot(relay, tg, tmp_path, presser):
    relay.route(hello(MASHA))
    before = list(tg.calls)
    for data in (f"inv:ok:{MASHA}", f"ginv:ok:{MASHA}"):
        relay.route(press(presser, data))
    saved = json.loads((tmp_path / "requests.json").read_text())
    assert saved[str(MASHA)]["state"] == "pending"
    sent = [c for c in tg.calls[len(before):] if c[0] != "answerCallbackQuery"]
    assert all(c[1]["chat_id"] == presser for c in sent)  # nobody else hears of it
    if presser == ANNA:     # a guest's own tap goes to her own sandbox, like any button
        assert len(relay.get_updates("anna", 0, 100, 0)) == 2


def test_a_refused_person_is_told_no(relay, tg, tmp_path):
    relay.route(hello(MASHA))
    invites.RequestBook(tmp_path / "requests.json").reject(MASHA)   # the owner said no
    relay.route(hello(MASHA, "ну пожалуйста", update_id=9))
    assert tg.to(MASHA)[-1]["text"] == "Это закрытый бот. Доступ выдаёт владелец."


def test_the_owner_writing_to_the_guest_bot_is_ignored(relay, tg, tmp_path):
    relay.route(hello(OWNER))
    assert tg.calls == [] and not (tmp_path / "requests.json").exists()


def test_without_an_owner_strangers_are_just_told_no(book, tg):
    relay = tr.Relay(book, tg)                            # invitations off
    relay.route(hello(MASHA))
    assert tg.to(OWNER) == []
    assert tg.to(MASHA)[0]["text"] == "Это закрытый бот. Доступ выдаёт владелец."
