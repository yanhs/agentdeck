"""Invitations, decision 27.10.2026: the guest bot (open to everyone) only records a
request; the admin's OWN bot (AgentDeck's Telegram bridge) asks "Let in?", creates the
sandbox and tells the person — through the guest bot — their address and password.

guests/invites.py: RequestBook (requests.json, shared by the two processes under a file
lock) and Desk (the admin's side: what to announce, what a button does).
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

GUESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GUESTS))

import invites  # noqa: E402

MASHA, IVAN = 5550001, 5550002


class FakeProv:
    def __init__(self, ok=True):
        self.ok, self.made = ok, []

    def provision(self, name, tid):
        self.made.append((name, tid))
        return (True, "") if self.ok else (False, "docker: no space left")


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setenv("GUESTS_LANG", "en")


@pytest.fixture
def book(tmp_path):
    return invites.RequestBook(tmp_path / "requests.json", cooldown=600)


def desk(book, prov=None, taken=(), told=None):
    told = [] if told is None else told
    return invites.Desk(book, provisioner=prov or FakeProv(),
                        tell_guest=lambda tid, text: told.append((tid, text)),
                        info_for=lambda name: {"url": f"https://sb-{name}.example.com",
                                               "password": "pw-123"},
                        taken=lambda: set(taken))


# ── the book: what the guest bot writes ───────────────────────────────────────
def test_a_stranger_becomes_a_pending_request(book):
    assert book.ask(MASHA, "Masha", "masha_k", now=100) == "new"
    rec = book.get(MASHA)
    assert rec["state"] == "pending" and rec["first_name"] == "Masha" and rec["username"] == "masha_k"


def test_asking_again_is_waiting_then_quiet(book):
    book.ask(MASHA, "Masha", "", now=100)
    assert book.ask(MASHA, "Masha", "", now=110) == "waiting"
    assert book.ask(MASHA, "Masha", "", now=120) == "quiet"        # within the cooldown
    assert book.ask(MASHA, "Masha", "", now=800) == "waiting"


def test_a_refused_person_stays_refused(book):
    book.ask(MASHA, "Masha", "", now=100)
    assert book.reject(MASHA) is not None
    assert book.ask(MASHA, "Masha", "", now=200) == "rejected"


def test_an_approved_person_who_writes_again_was_removed_since(book):
    """The guest bot asks the book only for people who are not guests (any more)."""
    book.ask(MASHA, "Masha", "", now=100)
    assert book.claim(MASHA)
    book.finish(MASHA, ok=True, name="masha")
    assert book.ask(MASHA, "Masha", "", now=900) == "new"
    assert book.get(MASHA)["state"] == "pending"


def test_two_processes_share_the_book(tmp_path):
    a = invites.RequestBook(tmp_path / "requests.json")
    b = invites.RequestBook(tmp_path / "requests.json")
    a.ask(MASHA, "Masha", "", now=1)
    assert b.get(MASHA)["state"] == "pending"
    assert b.claim(MASHA) and not a.claim(MASHA)


def test_the_book_file_is_private(book, tmp_path):
    book.ask(MASHA, "Masha", "", now=1)
    assert (tmp_path / "requests.json").stat().st_mode & 0o077 == 0


def test_parallel_asks_lose_nothing(tmp_path):
    books = [invites.RequestBook(tmp_path / "requests.json") for _ in range(8)]
    ts = [threading.Thread(target=b.ask, args=(1000 + i, f"p{i}", "", 1))
          for i, b in enumerate(books)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(json.loads((tmp_path / "requests.json").read_text())) == 8


# ── the desk: the admin's bot ─────────────────────────────────────────────────
def test_a_new_request_is_announced_once(book):
    d = desk(book)
    book.ask(MASHA, "Masha", "masha_k", now=1)
    [(tid, text, buttons)] = d.announcements()
    assert tid == str(MASHA)
    assert "Masha (@masha_k)" in text and str(MASHA) in text and '"masha-k"' in text
    assert buttons == [("✅ Let in", f"ginv:ok:{MASHA}"), ("✖ Refuse", f"ginv:no:{MASHA}")]
    book.mark_announced(MASHA, 77)
    assert d.announcements() == []


def test_let_in_makes_the_sandbox_and_tells_the_person(book):
    told, prov = [], FakeProv()
    d = desk(book, prov, told=told)
    book.ask(MASHA, "Masha", "masha_k", now=1)
    text, buttons = d.decide("ok", str(MASHA))
    assert prov.made == [("masha-k", MASHA)]
    assert '"masha-k" created' in text and buttons is None
    assert book.get(MASHA)["state"] == "approved"
    [(tid, welcome)] = told
    assert tid == MASHA and "https://sb-masha-k.example.com" in welcome and "pw-123" in welcome


def test_a_double_press_makes_one_sandbox(book):
    prov = FakeProv()
    d = desk(book, prov)
    book.ask(MASHA, "Masha", "", now=1)
    d.decide("ok", str(MASHA))
    text, buttons = d.decide("ok", str(MASHA))
    assert len(prov.made) == 1 and "Already decided" in text and buttons is None


def test_refuse(book):
    told, prov = [], FakeProv()
    d = desk(book, prov, told=told)
    book.ask(IVAN, "Ivan", "", now=1)
    text, buttons = d.decide("no", str(IVAN))
    assert "Refused: Ivan" in text and buttons is None and prov.made == []
    assert told == [(IVAN, "The owner did not give access.")]


def test_a_failed_sandbox_is_reported_and_can_be_retried(book):
    told = []
    d = desk(book, FakeProv(ok=False), told=told)
    book.ask(MASHA, "Masha", "", now=1)
    text, buttons = d.decide("ok", str(MASHA))
    assert "no space left" in text and buttons and buttons[0][1] == f"ginv:ok:{MASHA}"
    assert book.get(MASHA)["state"] == "pending"
    assert told and "Couldn't create" in told[0][1]
    d2 = desk(book, FakeProv(ok=True))
    assert "created" in d2.decide("ok", str(MASHA))[0]


@pytest.mark.parametrize("data", [("ok", "abc"), ("ok", "999"), ("maybe", str(MASHA))])
def test_bad_buttons_do_nothing(book, data):
    prov = FakeProv()
    d = desk(book, prov)
    book.ask(MASHA, "Masha", "", now=1)
    text, _ = d.decide(*data)
    assert prov.made == [] and "Already decided" in text


@pytest.mark.parametrize("username,taken,expected", [
    ("masha_k", (), "masha-k"), ("", (), f"g{MASHA}"), ("masha_k", ("masha-k",), f"g{MASHA}"),
    ("Bad.Name!", (), f"g{MASHA}")])
def test_the_sandbox_name(book, username, taken, expected):
    prov = FakeProv()
    d = desk(book, prov, taken=taken)
    book.ask(MASHA, "Masha", username, now=1)
    d.decide("ok", str(MASHA))
    assert prov.made == [(expected, MASHA)]


def test_creating_text_only_for_a_pending_request(book):
    d = desk(book)
    book.ask(MASHA, "Masha", "", now=1)
    assert d.creating_text(str(MASHA)) == "⏳ Creating a sandbox for Masha…"
    d.decide("no", str(MASHA))
    assert d.creating_text(str(MASHA)) is None
    assert d.creating_text("nope") is None


def test_the_guest_bot_cannot_create_sandboxes():
    """The open bot holds no way to run guestctl: provisioning lives only here."""
    src = (GUESTS / "tgrelay" / "tgrelay.py").read_text()
    assert "guestctl" not in src and "Provisioner" not in src and "ginv:" not in src
