"""The module runs on anyone's server: no names, addresses, paths or people of the
server it was first built on (they live in guests/.env now)."""

from pathlib import Path

GUESTS = Path(__file__).resolve().parents[1]
FORBIDDEN = ("reimake.com", "203.0.113.10", "immappeal", "/home/ubuntu", "111111111",
             "222222222", "10.0.3.1", "Yanhs_")
SKIP_DIRS = {"__pycache__", "data", "public", "sites", "build", ".pytest_cache"}


def files():
    for p in sorted(GUESTS.rglob("*")):
        if p.is_file() and not SKIP_DIRS & set(p.relative_to(GUESTS).parts) \
                and p.name != ".env" and p != Path(__file__).resolve():
            yield p


def test_nothing_of_one_server_is_left():
    found = []
    for p in files():
        text = p.read_text(encoding="utf-8", errors="replace").lower()
        found += [f"{p.relative_to(GUESTS)}: {w}" for w in FORBIDDEN if w.lower() in text]
    assert not found, "\n".join(found)
