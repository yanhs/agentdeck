"""Run-wide safety net (see tests/test_isolation_guard.py).

Every test — and every subprocess it starts — gets TMUX_TMPDIR pointed at a private,
throwaway directory, so a bare `tmux` anywhere in the suite talks to its own server and can
never list, kill or type into the live terminals. After each test we also check that the
live session registry still exists and stop the run loudly if a test deleted it.

A new pane's start is left in <the pane launcher's checkout>/.sessions/launch/
(library_cli.LAUNCHER, bin/pane). The run points AGENTDECK_LAUNCHER at a copy of the
launcher in a throwaway checkout, so nothing a test starts is ever written into this
checkout's own .sessions (a live AgentDeck may run from it)."""
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMUX_DIR = tempfile.mkdtemp(prefix="pytest-tmux-")
os.environ["TMUX_TMPDIR"] = _TMUX_DIR
os.environ.pop("TMUX", None)          # not "inside" the live server either

_REPO = Path(__file__).resolve().parents[1]
_LIVE_LIBRARY = _REPO / ".sessions" / "library.json"
_LIVE_AT_START = _LIVE_LIBRARY.exists()

# a neutral prefix: the path lands on the command line of the tmux servers tests start
_PANE_CHECKOUT = tempfile.mkdtemp(prefix="deck-")
os.makedirs(os.path.join(_PANE_CHECKOUT, "bin"))
if (_REPO / "bin" / "pane").exists():         # missing: ensure refuses "not executable"
    shutil.copy2(_REPO / "bin" / "pane", os.path.join(_PANE_CHECKOUT, "bin", "pane"))
os.environ["AGENTDECK_LAUNCHER"] = os.path.join(_PANE_CHECKOUT, "bin", "pane")


@pytest.fixture(autouse=True)
def _live_registry_survives(request):
    yield
    if _LIVE_AT_START and not _LIVE_LIBRARY.exists():
        pytest.exit(f"{request.node.nodeid} deleted the live session registry "
                    f"{_LIVE_LIBRARY} — stopping the run", returncode=3)


def pytest_unconfigure(config):
    shutil.rmtree(_TMUX_DIR, ignore_errors=True)
    shutil.rmtree(_PANE_CHECKOUT, ignore_errors=True)
