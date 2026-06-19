"""TDD: launch-claude-*.sh must NOT auto-spawn a fresh agent on boot
unless that agent is enabled in the dashboard list (`_order`).

The shared check lives in `_order_gate.py`. Each launch script will shell
out to it before creating a brand-new tmux session.

Contract:
  - id in `_order`  → exit 0  (script proceeds, starts/attaches tmux)
  - id not in `_order` → exit 1  (script prints message, exits cleanly)
  - agents.json missing  → exit 0  (back-compat fail-open: fresh install)
  - `_order` field missing → exit 0  (same)
  - agents.json malformed → exit 0  (don't break the world)

This is intentional fail-open: the gate is a "do less on reboot" hint, not
a security boundary. If the file is unreadable we keep behaving as before.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


GATE = Path(__file__).resolve().parents[1] / "_order_gate.py"


def _run(agent_id: str, agents_file: Path) -> int:
    """Invoke the gate as a subprocess; return its exit code."""
    return subprocess.run(
        [sys.executable, str(GATE), agent_id, "--agents-file", str(agents_file)],
        capture_output=True,
    ).returncode


def test_id_in_order_passes(tmp_path):
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"_order": ["1", "3", "8"]}))
    assert _run("3", af) == 0


def test_id_not_in_order_blocks(tmp_path):
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"_order": ["1", "3", "8"]}))
    assert _run("5", af) == 1
    assert _run("2", af) == 1


def test_missing_file_fails_open(tmp_path):
    """No agents.json yet → allow launch (fresh install)."""
    assert _run("1", tmp_path / "does-not-exist.json") == 0


def test_no_order_field_fails_open(tmp_path):
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"1": {"project": "Foo"}}))  # no _order key
    assert _run("1", af) == 0


def test_malformed_json_fails_open(tmp_path):
    af = tmp_path / "agents.json"
    af.write_text("{not json")
    assert _run("1", af) == 0


def test_order_with_int_ids_matches_string_arg(tmp_path):
    """If the JSON stored numbers (legacy), string lookup must still work."""
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"_order": [1, 3, 8]}))
    assert _run("3", af) == 0
    assert _run("4", af) == 1


def test_orchestra_string_ids(tmp_path):
    """Orchestra-style ids ('9', '10') behave the same."""
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"_order": ["9", "10"]}))
    assert _run("9", af) == 0
    assert _run("10", af) == 0
    assert _run("11", af) == 1
