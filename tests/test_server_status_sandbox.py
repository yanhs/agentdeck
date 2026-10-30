"""TDD: the Server page inside a sandbox shows the sandbox (decision 30.10.2026).

A guest's sandbox is a small machine inside the owner's: /proc there still shows the whole
host (all its cores and RAM), while the sandbox's own limits live in its cgroup (v2):
memory.max / memory.current / memory.stat, cpu.max / cpu.stat. When the cgroup has limits
smaller than the host, the page shows those: "2 cores", "4 GB", and the CPU in use
against the 2 cores. Without limits (the owner's own server, a Docker install with no
limits) nothing changes.
"""
from __future__ import annotations

import pytest

from tests.test_server_status import Clock, FakeProc, make_collector

GiB = 1024 ** 3


def cgroup(tmp_path, mem_max="4294967296", mem_current=GiB + 512 * 1024 ** 2,
           inactive_file=512 * 1024 ** 2, cpu_max="200000 100000", usage_usec=10_000_000):
    d = tmp_path / "cgroup"
    d.mkdir(exist_ok=True)
    (d / "memory.max").write_text(f"{mem_max}\n")
    (d / "memory.current").write_text(f"{mem_current}\n")
    (d / "memory.stat").write_text(f"anon 123\nfile 456\ninactive_file {inactive_file}\nactive_file 9\n")
    (d / "cpu.max").write_text(f"{cpu_max}\n")
    (d / "cpu.stat").write_text(f"usage_usec {usage_usec}\nuser_usec 1\nsystem_usec 2\n")
    return d


def two_samples(tmp_path, cg, busy_usec=5_000_000, dt=5.0):
    """Sample, let the sandbox use `busy_usec` of CPU in `dt` seconds, sample again."""
    fp = FakeProc(tmp_path / "proc")
    fp.add(1, "tini", 0, cgroup="0::/")
    clock = Clock()
    c = make_collector(fp, clock, cgroup_root=str(cg))
    c.sample()
    clock.t += dt
    fp.busy_host(50, 50)                       # the host is busy too (its own numbers)
    if (cg / "cpu.stat").exists():             # (on a host there is no cgroup file at all)
        stat = (cg / "cpu.stat").read_text().split()
        (cg / "cpu.stat").write_text(f"usage_usec {int(stat[1]) + busy_usec}\n")
    return c.sample()["host"]


def test_inside_a_sandbox_the_page_shows_the_sandbox(tmp_path):
    h = two_samples(tmp_path, cgroup(tmp_path))
    assert h["scope"] == "sandbox"
    assert h["cores"] == 2
    assert h["cpu_pct"] == pytest.approx(50, abs=0.5)          # 1 of its 2 cores, not the host's
    assert h["cpu_per_core"] == []                             # the host's cores are not its own
    assert h["ram_total_mb"] == 4096
    assert h["ram_used_mb"] == 1024                            # current minus reclaimable cache
    assert h["ram_available_mb"] == 3072
    assert h["ram_pct"] == pytest.approx(25)
    assert h["load"] == [pytest.approx(1.0, abs=0.01)] * 3     # cores in use now, not the host's load


def test_a_fraction_of_a_core_is_kept(tmp_path):
    h = two_samples(tmp_path, cgroup(tmp_path, cpu_max="150000 100000"))
    assert h["cores"] == 1.5


def test_the_pressure_verdict_judges_the_sandbox(tmp_path):
    h = two_samples(tmp_path, cgroup(tmp_path), busy_usec=9_600_000)    # 1.92 of 2 cores
    assert h["cpu_pct"] == pytest.approx(96, abs=0.5)
    assert h["pressure"] == "overloaded"


@pytest.mark.parametrize("kw", [
    {"mem_max": "max", "cpu_max": "max 100000"},               # a container without limits
    {"mem_max": str(64 * GiB), "cpu_max": "6400000 100000"},   # limits above the host's 16 GB, 4 cores
], ids=["no-limits", "limits-above-the-host"])
def test_without_real_limits_it_is_the_server(tmp_path, kw):
    h = two_samples(tmp_path, cgroup(tmp_path, **kw))
    assert h["scope"] == "server"
    assert h["cores"] == 4 and h["ram_total_mb"] == 16384 and len(h["cpu_per_core"]) == 4


def test_on_a_host_there_is_no_cgroup_file_and_nothing_changes(tmp_path):
    h = two_samples(tmp_path, tmp_path / "nowhere")
    assert h["scope"] == "server" and h["cores"] == 4 and h["load"] == [1.5, 1.2, 1.0]


def test_only_memory_limited(tmp_path):
    h = two_samples(tmp_path, cgroup(tmp_path, cpu_max="max 100000"))
    assert h["scope"] == "sandbox"
    assert h["ram_total_mb"] == 4096
    assert h["cores"] == 4 and len(h["cpu_per_core"]) == 4    # CPU is the host's: no quota
