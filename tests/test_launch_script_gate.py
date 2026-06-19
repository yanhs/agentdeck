"""Integration test: each launch-claude-*.sh must honor _order_gate.py.

We run the script in a shim environment:
  - PATH points at a fake tmux that always says "no session" + records calls
  - HOME/agents.json path is overridden via env so the gate reads our fixture
  - Script exits within a few seconds if blocked (gate sleeps 5s — we kill it)

Asserts:
  * id in _order → script proceeds far enough to call `tmux new-session`
  * id NOT in _order → script exits BEFORE any `tmux new-session` call
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest


TERMINAL = Path(__file__).resolve().parents[1]


def _fake_tmux(tmp: Path, call_log: Path) -> Path:
    """Create a fake `tmux` on PATH that logs args and refuses has-session."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo "$@" >> "{call_log}"
        case "$1" in
          has-session) exit 1 ;;
          # The launch script polls capture-pane until it sees "bypass
          # permissions". Echo that immediately to short-circuit the 60s loop.
          capture-pane) echo "bypass permissions"; exit 0 ;;
          # `new-session` is the line under test — fake tmux returns 0 so the
          # script terminates instead of attaching to a real TUI.
          *) exit 0 ;;
        esac
    """))
    tmux.chmod(0o755)
    return bin_dir


def _run_script(script: Path, agents_file: Path, tmp: Path):
    """Run a launch-claude-*.sh with mocked tmux. Returns (rc, calls, output)."""
    call_log = tmp / "tmux-calls.log"
    call_log.touch()
    bin_dir = _fake_tmux(tmp, call_log)
    # Stage the script + _order_gate.py + the test agents.json in the same dir
    # so the script picks up our fixture as agents.json.
    stage = tmp / "stage"
    stage.mkdir()
    (stage / "_order_gate.py").write_bytes((TERMINAL / "_order_gate.py").read_bytes())
    (stage / "agents.json").write_text(agents_file.read_text())
    target = stage / script.name
    target.write_bytes(script.read_bytes())
    target.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DRY_RUN"] = "0"
    # The script `exec`s tmux new-session at the end; our fake tmux exits 0
    # so the script terminates instead of attaching to a real TUI.
    proc = subprocess.run(
        ["bash", str(target)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return proc.returncode, call_log.read_text(), proc.stdout + proc.stderr


def test_launch_proceeds_when_agent_in_order(tmp_path):
    """Agent id is in _order → script reaches `tmux new-session`."""
    af = tmp_path / "agents.json"
    af.write_text('{"_order": ["1"]}')

    rc, calls, out = _run_script(TERMINAL / "launch-claude.sh", af, tmp_path)

    assert "new-session" in calls, f"expected new-session call, got:\n{calls}\n---\n{out}"
    assert "is not in /agents/" not in out


def test_launch_blocked_when_agent_NOT_in_order(tmp_path):
    """Agent id is NOT in _order → script exits before `tmux new-session`."""
    af = tmp_path / "agents.json"
    af.write_text('{"_order": ["2", "3"]}')  # 1 missing → must block

    rc, calls, out = _run_script(TERMINAL / "launch-claude.sh", af, tmp_path)

    assert rc == 0, f"gate must exit cleanly, got rc={rc}"
    assert "new-session" not in calls, (
        f"gate FAILED — tmux new-session was called:\n{calls}\n---\n{out}"
    )
    assert "is not in /agents/" in out, f"expected 'is not in /agents/' notice, got:\n{out}"


def test_launch_blocked_for_terminal3_when_only_terminal1_enabled(tmp_path):
    """Cross-check with a numbered script (launch-claude-3.sh)."""
    af = tmp_path / "agents.json"
    af.write_text('{"_order": ["1"]}')

    rc, calls, out = _run_script(TERMINAL / "launch-claude-3.sh", af, tmp_path)

    assert rc == 0
    assert "new-session" not in calls, calls
    assert "Agent #3" in out


def test_launch_proceeds_when_no_order_field(tmp_path):
    """No _order field → fail-open (back-compat with fresh installs)."""
    af = tmp_path / "agents.json"
    af.write_text('{}')

    rc, calls, out = _run_script(TERMINAL / "launch-claude-2.sh", af, tmp_path)

    assert "new-session" in calls, f"missing _order should allow launch:\n{calls}\n{out}"
