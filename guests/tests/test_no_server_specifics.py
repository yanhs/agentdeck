"""The module runs on anyone's server: no names, addresses, paths or people of the
server it was first built on (they live in guests/.env now). Addresses and people's
Telegram ids are checked by their sha256 — this file is public, so it must not carry
them either."""

import hashlib
import re
from pathlib import Path

GUESTS = Path(__file__).resolve().parents[1]
FORBIDDEN = ("reimake.com", "immappeal", "/home/ubuntu", "10.0.3.1", "Yanhs_")
# sha256 of the first server's public IP and of two people's Telegram ids
FORBIDDEN_SHA256 = {
    "4c79f7ef9e280dccc58f137d5fd16544d95e422b57fb1b73b56be763d44772c9",
    "0f2a190883b96307248b343765007a685260b5f01bd566a64308c7d536cd0afb",
    "408e6c42f124388f55e5d9a16f9c1fd205c6ef9fe8c372ec8de1d09c4fb55e34",
}
CANDIDATE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])|(?<!\d)\d{6,}(?!\d)")
SKIP_DIRS = {"__pycache__", "data", "public", "sites", "build", ".pytest_cache"}


def files():
    for p in sorted(GUESTS.rglob("*")):
        if p.is_file() and not SKIP_DIRS & set(p.relative_to(GUESTS).parts) \
                and p.name != ".env" and p != Path(__file__).resolve():
            yield p


def hashed_hits(text):
    return sorted({m for m in CANDIDATE.findall(text)
                   if hashlib.sha256(m.encode()).hexdigest() in FORBIDDEN_SHA256})


def test_candidates_are_checked_by_fingerprint():
    probe = "1.2.3.4 id 123456789 and 98765432100"
    assert CANDIDATE.findall(probe) == ["1.2.3.4", "123456789", "98765432100"]
    known = next(iter(FORBIDDEN_SHA256))
    assert hashed_hits(probe) == [] and len(known) == 64


def test_nothing_of_one_server_is_left():
    found = []
    for p in files():
        text = p.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        found += [f"{p.relative_to(GUESTS)}: {w}" for w in FORBIDDEN if w.lower() in low]
        found += [f"{p.relative_to(GUESTS)}: sha256-listed value" for _ in hashed_hits(text)]
    assert not found, "\n".join(found)
