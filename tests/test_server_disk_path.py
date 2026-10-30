"""The Server page's disk: a guest's sandbox has a disk of its own (guests/disk.py) and
guestctl passes AGENTKENNEL_DISK_PATH=/work; the page shows that disk, not the host's.
Elsewhere it is "/" as before."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def collector_disk(monkeypatch, tmp_path, value):
    monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(tmp_path / "k"))
    for k in ("AGENTKENNEL_DISK_PATH", "AGENTDECK_DISK_PATH"):
        monkeypatch.delenv(k, raising=False)
    if value is not None:
        monkeypatch.setenv("AGENTKENNEL_DISK_PATH", value)
    sys.modules.pop("status_server", None)
    try:
        return importlib.import_module("status_server").SERVER_COLLECTOR.disk_path
    finally:
        sys.modules.pop("status_server", None)


def test_a_sandbox_shows_its_own_disk(monkeypatch, tmp_path):
    assert collector_disk(monkeypatch, tmp_path, "/work") == "/work"


def test_elsewhere_the_root_disk(monkeypatch, tmp_path):
    assert collector_disk(monkeypatch, tmp_path, None) == "/"
