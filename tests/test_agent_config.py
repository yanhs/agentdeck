"""TDD: per-agent model/effort persistence in agents.json.

Each agent entry in agents.json may carry optional `model` and `effort`
fields. A small helper script (`_agent_config.py`) reads these so launch
scripts can shell out cheaply:

    python3 _agent_config.py 3 model    # → "sonnet"
    python3 _agent_config.py 3 effort   # → "xhigh"

If the field is unset, the helper prints an empty string (so the launch
script can `[ -n "$M" ] && CLAUDE_CMD="$CLAUDE_CMD --model $M"`).

Defaults (when nothing in agents.json):
  - model  → "" (use Claude's subscription default, no `--model` flag)
  - effort → "auto" (matches the historical /effort auto we sent)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


HELPER = Path(__file__).resolve().parents[1] / "_agent_config.py"


def _run(agent_id: str, field: str, agents_file: Path) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, str(HELPER), agent_id, field, "--agents-file", str(agents_file)],
        capture_output=True, text=True,
    )
    return p.returncode, p.stdout.rstrip("\n")


# --- model ----------------------------------------------------------------


def test_model_returned_when_set(tmp_path):
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"3": {"model": "sonnet"}}))
    rc, out = _run("3", "model", af)
    assert rc == 0
    assert out == "sonnet"


def test_model_blank_when_unset(tmp_path):
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"3": {"project": "Foo"}}))
    rc, out = _run("3", "model", af)
    assert rc == 0
    assert out == ""


def test_model_blank_when_agent_missing(tmp_path):
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"1": {"model": "opus"}}))
    rc, out = _run("3", "model", af)
    assert rc == 0
    assert out == ""


# --- effort ---------------------------------------------------------------


def test_effort_returned_when_set(tmp_path):
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"3": {"effort": "xhigh"}}))
    rc, out = _run("3", "effort", af)
    assert rc == 0
    assert out == "xhigh"


def test_effort_defaults_to_auto_when_unset(tmp_path):
    """If no effort recorded, fall back to "auto" — that's the historical
    behaviour the launch script encoded as `/effort auto`."""
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"3": {}}))
    rc, out = _run("3", "effort", af)
    assert rc == 0
    assert out == "auto"


def test_effort_defaults_to_auto_when_agent_missing(tmp_path):
    af = tmp_path / "agents.json"
    af.write_text("{}")
    rc, out = _run("3", "effort", af)
    assert rc == 0
    assert out == "auto"


# --- corner cases ---------------------------------------------------------


def test_missing_agents_file_returns_blank_model_auto_effort(tmp_path):
    """Fresh install: no file at all — same defaults."""
    af = tmp_path / "missing.json"
    assert _run("1", "model", af) == (0, "")
    assert _run("1", "effort", af) == (0, "auto")


def test_malformed_json_does_not_explode(tmp_path):
    af = tmp_path / "agents.json"
    af.write_text("not json")
    assert _run("1", "model", af) == (0, "")
    assert _run("1", "effort", af) == (0, "auto")


def test_unknown_field_errors(tmp_path):
    af = tmp_path / "agents.json"
    af.write_text("{}")
    rc, out = _run("1", "garbage", af)
    assert rc != 0, "asking for an unknown field should fail loud"


# --- validation of stored values -----------------------------------------


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max", "auto"])
def test_all_known_effort_levels_pass_through(tmp_path, effort):
    af = tmp_path / "agents.json"
    af.write_text(json.dumps({"3": {"effort": effort}}))
    assert _run("3", "effort", af) == (0, effort)
