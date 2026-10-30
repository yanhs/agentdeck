"""guests/scripts/move_when_idle.py — a timer's job: move a guest onto its own disk and the
new image, but only while the guest is idle (no open terminal, no Claude request for a
while), then check the result and tell the owner either way (30.10.2026).

Exit codes: 0 done, 75 still waiting (the timer tries again), 1 failed (told, gives up).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import move_when_idle as m  # noqa: E402


class World:
    def __init__(self, terminals=(), last_request_min=60.0, move_rc=0, after=None):
        self.terminals, self.last = list(terminals), last_request_min
        self.move_rc = move_rc
        self.after = after or {"running": True, "image": "img:new", "disk": True, "login": 200}
        self.moved, self.told = [], []

    def live_terminals(self, name):
        return self.terminals

    def minutes_since_request(self, name):
        return self.last

    def move(self, name, gb, image):
        self.moved.append((name, gb, image))
        return self.move_rc, "moved" if self.move_rc == 0 else "rsync: no space"

    def state(self, name):
        return dict(self.after)

    def tell(self, text):
        self.told.append(text)


def run(w, dry=False):
    return m.run("anna", gb=20, image="img:new", quiet_min=30, world=w, dry=dry)


def test_an_open_terminal_waits_quietly():
    w = World(terminals=["cs-1"])
    assert run(w) == 75
    assert w.moved == [] and w.told == []


def test_a_recent_request_waits_quietly():
    w = World(last_request_min=5)
    assert run(w) == 75 and w.moved == [] and w.told == []


def test_idle_moves_checks_and_reports_success():
    w = World()
    assert run(w) == 0
    assert w.moved == [("anna", 20, "img:new")]
    assert len(w.told) == 1 and "✅" in w.told[0] and "anna" in w.told[0]


@pytest.mark.parametrize("after", [
    {"running": False, "image": "img:new", "disk": True, "login": 200},
    {"running": True, "image": "img:old", "disk": True, "login": 200},
    {"running": True, "image": "img:new", "disk": False, "login": 200},
    {"running": True, "image": "img:new", "disk": True, "login": 502},
], ids=["not-running", "old-image", "no-disk", "page-down"])
def test_a_result_that_is_not_right_is_reported_as_a_failure(after):
    w = World(after=after)
    assert run(w) == 1
    assert len(w.told) == 1 and "🚨" in w.told[0]


def test_a_failed_move_is_reported_with_why():
    w = World(move_rc=1)
    assert run(w) == 1
    assert "🚨" in w.told[0] and "rsync: no space" in w.told[0]


def test_a_dry_run_says_what_it_would_do_and_changes_nothing():
    w = World()
    assert run(w, dry=True) == 0
    assert w.moved == [] and w.told == []
