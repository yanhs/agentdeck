"""Tests must never reach the live tmux server or the live .sessions.

2026-09-26 07:25: an unshimmed `tmux kill-session` in a test (install.sh --uninstall)
closed every live terminal, and `--uninstall --purge` deleted the live .sessions.
tmux finds its server by UID (/tmp/tmux-<uid>/default), not by HOME, so a changed HOME
doesn't protect it; TMUX_TMPDIR does — conftest.py points it at a private dir for the
whole run, so even a bare `tmux` in any test talks to a throwaway server."""
import os
import subprocess
import tempfile


def test_tmux_is_pointed_at_a_private_dir():
    d = os.environ.get("TMUX_TMPDIR", "")
    assert d and d.startswith(tempfile.gettempdir()) and "pytest-tmux" in d, d


def test_bare_tmux_sees_no_live_sessions():
    r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                       capture_output=True, text=True)
    live = [s for s in r.stdout.split() if s.startswith(("cs-", "claude-terminal", "cmd-shell"))]
    assert live == [], live
