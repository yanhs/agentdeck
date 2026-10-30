"""The CPU/RAM line above the terminal list (`_system` of GET /api/library and
/api/terminal-status) in a guest's sandbox shows the sandbox, not the host (owner,
30.10.2026: "you changed the Server page, but not the line next to the terminals").

The same cgroup (v2) limits as the Server page (server_status.read_cgroup): memory.max /
memory.current / memory.stat, cpu.max / cpu.stat. Without limits — the host, as before.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GiB = 1024 ** 3


@pytest.fixture
def ss(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(tmp_path / "k"))
    sys.modules.pop("status_server", None)
    mod = importlib.import_module("status_server")
    yield mod
    sys.modules.pop("status_server", None)


def cgroup(tmp_path, mem_max=str(4 * GiB), current=GiB + 512 * 1024 ** 2,
           inactive=512 * 1024 ** 2, cpu_max="100000 100000", usage=10_000_000):
    d = tmp_path / "cg"
    d.mkdir(exist_ok=True)
    (d / "memory.max").write_text(f"{mem_max}\n")
    (d / "memory.current").write_text(f"{current}\n")
    (d / "memory.stat").write_text(f"anon 1\ninactive_file {inactive}\n")
    (d / "cpu.max").write_text(f"{cpu_max}\n")
    (d / "cpu.stat").write_text(f"usage_usec {usage}\n")
    return d


def test_in_a_sandbox_the_line_shows_its_own_memory_and_cpu(ss, tmp_path, monkeypatch):
    cg = cgroup(tmp_path)
    monkeypatch.setattr(ss, "CGROUP_ROOT", str(cg))
    t = [1000.0]
    monkeypatch.setattr(ss, "_stats_clock", lambda: t[0])
    ss.get_system_stats()                                   # the first sample: no CPU yet
    t[0] += 4.0
    (cg / "cpu.stat").write_text(f"usage_usec {10_000_000 + 2_000_000}\n")   # 0.5 of 1 core
    s = ss.get_system_stats()
    assert s["ram_total_mb"] == 4096 and s["ram_used_mb"] == 1024 and s["ram_pct"] == 25
    assert s["cpu_pct"] == 50


def test_on_a_host_nothing_changes(ss, tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "CGROUP_ROOT", str(tmp_path / "nowhere"))
    s = ss.get_system_stats()
    with open("/proc/meminfo") as f:
        total_kb = int(next(l for l in f if l.startswith("MemTotal:")).split()[1])
    assert s["ram_total_mb"] == total_kb // 1024


def test_limits_above_the_host_are_no_limits(ss, tmp_path, monkeypatch):
    cg = cgroup(tmp_path, mem_max=str(1024 * GiB), cpu_max=f"{(os.cpu_count() + 4) * 100000} 100000")
    monkeypatch.setattr(ss, "CGROUP_ROOT", str(cg))
    s = ss.get_system_stats()
    assert s["ram_total_mb"] != 1024 * 1024
