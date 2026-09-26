"""Run-wide safety net (see tests/test_isolation_guard.py).

Every test — and every subprocess it starts — gets TMUX_TMPDIR pointed at a private,
throwaway directory, so a bare `tmux` anywhere in the suite talks to its own server and can
never list, kill or type into the live terminals. After each test we also check that the
live session registry still exists and stop the run loudly if a test deleted it."""
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMUX_DIR = tempfile.mkdtemp(prefix="pytest-tmux-")
os.environ["TMUX_TMPDIR"] = _TMUX_DIR
os.environ.pop("TMUX", None)          # not "inside" the live server either

_LIVE_LIBRARY = Path(__file__).resolve().parents[1] / ".sessions" / "library.json"
_LIVE_AT_START = _LIVE_LIBRARY.exists()


@pytest.fixture(autouse=True)
def _live_registry_survives(request):
    yield
    if _LIVE_AT_START and not _LIVE_LIBRARY.exists():
        pytest.exit(f"{request.node.nodeid} deleted the live session registry "
                    f"{_LIVE_LIBRARY} — stopping the run", returncode=3)


def pytest_unconfigure(config):
    shutil.rmtree(_TMUX_DIR, ignore_errors=True)
