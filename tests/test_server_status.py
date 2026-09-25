"""TDD: server_status.py — the collector behind the dashboard's "Server" page.

Everything here runs against a synthetic /proc tree in a temp dir (stat, cmdline,
cgroup, cwd for each fake process + /proc/stat, meminfo, loadavg, uptime) and fake
tmux / docker / systemd / pm2 / library sources: no real process, tmux server,
container or unit is looked at, let alone touched.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import threading
import time
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

TERMINAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TERMINAL_DIR))

import server_status as ss  # noqa: E402

HZ = 100
PAGE_KB = 4
USER_UNIT = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/{}"
SYS_UNIT = "0::/system.slice/{}"
PM2_CG = SYS_UNIT.format("pm2-ubuntu.service")
DOCKER_ID = "ab" * 32

SECRET = "sk-ant-api03-Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0LzXcVbNm"
GH_TOKEN = "ghp_1234567890abcdefABCDEF1234567890abcd"


class FakeProc:
    """A /proc look-alike: write_proc(pid, ...) then tick() to move CPU counters."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.procs = {}
        self.cpu = [[1000, 0, 500, 10000, 0, 0, 0, 0, 0, 0] for _ in range(4)]
        self.mem = {"MemTotal": 16 * 1024 * 1024, "MemAvailable": 8 * 1024 * 1024,
                    "MemFree": 1024 * 1024, "SwapTotal": 0, "SwapFree": 0}
        self.load = (1.5, 1.2, 1.0)
        self._write_host()

    def add(self, pid, comm, ppid, ticks=0, rss_mb=10, argv=None, cgroup=PM2_CG,
            cwd="/home/ubuntu/pr", start=1000):
        self.procs[pid] = dict(comm=comm, ppid=ppid, ticks=ticks, rss_mb=rss_mb,
                               argv=[comm] if argv is None else argv, cgroup=cgroup,
                               cwd=cwd, start=start)
        self._write(pid)

    def remove(self, pid):
        d = self.root / str(pid)
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
        del self.procs[pid]

    def tick(self, pid, dticks):
        self.procs[pid]["ticks"] += dticks
        self._write(pid)

    def busy_host(self, busy, idle):
        """every core: +busy user ticks, +idle idle ticks"""
        for c in self.cpu:
            c[0] += busy
            c[3] += idle
        self._write_host()

    def _write(self, pid):
        p = self.procs[pid]
        d = self.root / str(pid)
        d.mkdir(exist_ok=True)
        rss_pages = p["rss_mb"] * 1024 // PAGE_KB
        utime = p["ticks"]
        fields = ["S", p["ppid"], pid, pid, 0, -1, 0, 0, 0, 0, 0, utime, 0, 0, 0, 20, 0, 1, 0,
                  p["start"], 1 << 20, rss_pages]
        (d / "stat").write_text(f"{pid} ({p['comm']}) " + " ".join(map(str, fields)) + " 0 0 0\n")
        (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in p["argv"]))
        (d / "comm").write_text(p["comm"] + "\n")
        (d / "cgroup").write_text(p["cgroup"] + "\n")
        (d / "environ").write_bytes(b"SECRET_TOKEN=" + SECRET.encode() + b"\0")
        cwd = d / "cwd"
        if cwd.is_symlink():
            cwd.unlink()
        cwd.symlink_to(p["cwd"])

    def _write_host(self):
        tot = [sum(c[i] for c in self.cpu) for i in range(10)]
        lines = ["cpu  " + " ".join(map(str, tot))]
        lines += [f"cpu{i} " + " ".join(map(str, c)) for i, c in enumerate(self.cpu)]
        lines += ["intr 0", "ctxt 0", "btime 1700000000"]
        (self.root / "stat").write_text("\n".join(lines) + "\n")
        (self.root / "meminfo").write_text(
            "".join(f"{k}: {v} kB\n" for k, v in self.mem.items()))
        (self.root / "loadavg").write_text("%.2f %.2f %.2f 3/300 999\n" % self.load)
        (self.root / "uptime").write_text("90061.50 100.00\n")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make_collector(fp, clock, **kw):
    lib_by_sid = {"b36f2fd1": "ImmAppeal deploy"}
    lib_by_uuid = {"aaaa1111-2222-4333-8444-555566667777": "TG bridge work"}
    args = dict(
        proc_root=str(fp.root), clock=clock, hz=HZ, page_kb=PAGE_KB, disk_path="/",
        tmux_panes=lambda: [("cs-b36f2fd1", 500), ("claude-terminal-3", 600),
                            ("cmd-shell", 700)],
        docker_ps=lambda: {DOCKER_ID: {"name": "immappeal-dev", "state": "running",
                                       "status": "Up 2 hours", "image": "immappeal:f5010d7"},
                           "cd" * 32: {"name": "old-thing", "state": "exited",
                                       "status": "Exited (0) 3 days ago", "image": "busybox"}},
        units=lambda: {"safin-rag.service": {"active": "active", "scope": "user",
                                             "desc": "Safin RAG API (FastAPI + pgvector + Claude)"},
                       "nginx.service": {"active": "active", "scope": "system",
                                         "desc": "A high performance web server"},
                       "cron.service": {"active": "active", "scope": "system",
                                        "desc": "Regular background program processing daemon"},
                       "vpn-bot.service": {"active": "failed", "scope": "user",
                                           "desc": "MakeBlitz VPN Sales Bot"}},
        library_names=lambda: (lib_by_sid, lib_by_uuid),
    )
    args.update(kw)
    return ss.Collector(**args)


@pytest.fixture
def world(tmp_path):
    """A small server: two agents (one with a busy pytest child), a docker
    container, a user service, nginx, a pm2 ttyd, an orphan script, noise."""
    fp = FakeProc(tmp_path / "proc")
    fp.add(1, "systemd", 0, cgroup="0::/init.scope", argv=["/sbin/init"])
    fp.add(2, "kthreadd", 0, argv=[], rss_mb=0, cgroup="0::/")
    fp.add(3, "kworker/0:1", 2, argv=[], rss_mb=0, cgroup="0::/")
    # pm2 + one ttyd app
    fp.add(90, "PM2 v6.0.14: God", 1, argv=["PM2 v6.0.14: God Daemon (/home/ubuntu/.pm2)"])
    fp.add(91, "ttyd", 90, argv=["/usr/bin/ttyd", "-p", "3031", "-c", "user:hunter2pass", "bash"])
    # tmux server and its panes
    fp.add(400, "tmux: server", 1, argv=["tmux"])
    # library terminal: the pane IS claude, with a pytest grandchild burning CPU
    fp.add(500, "claude", 400, argv=["/home/ubuntu/.local/bin/claude", "--resume",
                                      "bbbb2222-3333-4444-8555-666677778888"],
           cwd="/home/ubuntu/pr/Appeals/dev", rss_mb=400)
    fp.add(501, "bash", 500, argv=["/bin/bash", "-c", f"ANTHROPIC_API_KEY={SECRET} pytest -q"])
    fp.add(502, "python3", 501, argv=["python3", "-m", "pytest", "-q", "tests/", f"--token={SECRET}"],
           rss_mb=200)
    fp.add(503, "node", 500, argv=["node", "/home/ubuntu/.npm/_npx/x/mcp-server.js",
                                    f"--api-key={GH_TOKEN}"], rss_mb=60)
    # legacy numbered terminal: bash pane -> claude (idle)
    fp.add(600, "bash", 400, argv=["-bash"], rss_mb=5)
    fp.add(601, "claude", 600, argv=["/home/ubuntu/.local/bin/claude", "--resume",
                                      "aaaa1111-2222-4333-8444-555566667777"], rss_mb=300)
    # the command line: a shell running a heavy script (a job owned by cmd-shell)
    fp.add(700, "bash", 400, argv=["-bash"], rss_mb=5)
    fp.add(701, "python3", 700, argv=["python3", "/home/ubuntu/pr/x/_cl_bulk_texts.py",
                                       "--db", "postgres://app:s3cretpw@localhost/db"], rss_mb=150)
    # a claude outside tmux (e.g. an SDK-run agent), reparented to init
    fp.add(800, "claude", 1, argv=["/home/ubuntu/.local/bin/claude", "-p", "do stuff"],
           cwd="/tmp/scratch", rss_mb=250)
    # docker container processes (containerd-shim is outside, in system.slice)
    fp.add(1000, "node", 1, cgroup=f"0::/system.slice/docker-{DOCKER_ID}.scope",
           argv=["next-server (v15.1.0)"], rss_mb=500)
    fp.add(1001, "python3", 1000, cgroup=f"0::/system.slice/docker-{DOCKER_ID}.scope",
           argv=["python3", "api.py"], rss_mb=300)
    fp.add(1002, "claude", 1001, cgroup=f"0::/system.slice/docker-{DOCKER_ID}.scope",
           argv=["claude", "-p"], rss_mb=200)
    # services
    fp.add(1100, "python3", 1, cgroup=USER_UNIT.format("safin-rag.service"),
           argv=["/home/ubuntu/pr/safin/.venv/bin/python3", "-m", "uvicorn", "app:app"], rss_mb=120)
    fp.add(1200, "nginx", 1, cgroup=SYS_UNIT.format("nginx.service"),
           argv=["nginx: master process /usr/sbin/nginx -g daemon on;"], rss_mb=5)
    fp.add(1201, "nginx", 1200, cgroup=SYS_UNIT.format("nginx.service"),
           argv=["nginx: worker process"], rss_mb=20)
    fp.add(1300, "cron", 1, cgroup=SYS_UNIT.format("cron.service"), argv=["/usr/sbin/cron", "-f"],
           rss_mb=2)
    # an orphan build nobody owns (ssh session), heavy
    fp.add(1400, "node", 1, cgroup="0::/user.slice/user-1000.slice/session-5.scope",
           argv=["node", "/home/ubuntu/pr/site/node_modules/.bin/next", "build"], rss_mb=900)
    # tiny noise
    fp.add(1500, "sshd", 1, cgroup=SYS_UNIT.format("ssh.service"), argv=["sshd: /usr/sbin/sshd"],
           rss_mb=4)
    fp.add(1501, "sleep", 1, cgroup="0::/user.slice/user-1000.slice/session-5.scope",
           argv=["sleep", "100"], rss_mb=1)
    clock = Clock()
    c = make_collector(fp, clock)
    c.sample()
    # 5 seconds later: pytest 1 core (500 ticks), mcp 5%, claude(500) 10%,
    # docker node 50%, the orphan build 2 cores, bulk script 80%, legacy claude 1%
    clock.t += 5
    fp.tick(502, 500)
    fp.tick(503, 25)
    fp.tick(500, 50)
    fp.tick(1000, 250)
    fp.tick(1400, 1000)
    fp.tick(701, 400)
    fp.tick(601, 5)
    fp.tick(1201, 10)
    fp.busy_host(busy=300, idle=200)      # 4 cores x 500 ticks, 60% busy
    snap = c.sample()
    return fp, clock, c, snap


def group(snap, gid):
    return next(g for g in snap["groups"] if g["id"] == gid)


def member(snap, gid, pred):
    g = group(snap, gid)
    ms = [m for m in g["members"] if pred(m)]
    assert len(ms) == 1, [m["name"] for m in g["members"]]
    return ms[0]


# ── host ────────────────────────────────────────────────────────────────────
def test_host_cpu_ram_load_uptime(world):
    fp, clock, c, snap = world
    h = snap["host"]
    assert h["cores"] == 4
    assert h["cpu_pct"] == pytest.approx(60, abs=0.5)
    assert [round(x) for x in h["cpu_per_core"]] == [60, 60, 60, 60]
    assert h["load"] == [1.5, 1.2, 1.0]
    assert h["ram_total_mb"] == 16384
    assert h["ram_available_mb"] == 8192
    assert h["ram_used_mb"] == 8192
    assert h["ram_pct"] == pytest.approx(50)
    assert h["uptime_s"] == 90061
    assert h["disk_total_gb"] > 0 and 0 <= h["disk_pct"] <= 100


def test_first_sample_has_zero_cpu_not_garbage(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(1, "systemd", 0, ticks=123456, cgroup="0::/init.scope")
    snap = make_collector(fp, Clock()).sample()
    assert snap["host"]["cpu_pct"] == 0
    assert all(m["cpu_pct"] == 0 for g in snap["groups"] for m in g["members"])


@pytest.mark.parametrize("cpu,load,avail,verdict", [
    (20, 1.0, 50, "ok"),
    (70, 2.0, 50, "busy"),
    (20, 4.5, 50, "busy"),         # load above the 4 cores
    (20, 1.0, 20, "busy"),         # little RAM left
    (95, 3.0, 50, "overloaded"),
    (20, 7.0, 50, "overloaded"),   # load > 1.5 x cores
    (20, 1.0, 5, "overloaded"),
])
def test_pressure_verdict(cpu, load, avail, verdict):
    v, why = ss.pressure(cpu_pct=cpu, load1=load, cores=4, ram_available_pct=avail)
    assert v == verdict
    assert isinstance(why, str) and why


# ── CPU delta math ──────────────────────────────────────────────────────────
def test_cpu_percent_from_tick_deltas():
    # 250 ticks at 100 Hz over 5 s = 2.5 s of CPU in 5 s = 50 % of one core
    assert ss.cpu_pct(1000, 1250, 5.0, HZ) == pytest.approx(50)
    assert ss.cpu_pct(1000, 2000, 5.0, HZ) == pytest.approx(200)   # two cores
    assert ss.cpu_pct(1000, 900, 5.0, HZ) == 0                     # pid reused: never negative
    assert ss.cpu_pct(0, 10, 0, HZ) == 0                           # no interval


def test_process_born_between_samples_counts_all_its_ticks(world):
    fp, clock, c, _ = world
    fp.add(1600, "python3", 1, cgroup="0::/user.slice/user-1000.slice/session-5.scope",
           argv=["python3", "burn.py"], ticks=300, start=5000)
    clock.t += 5
    snap = c.sample()
    m = member(snap, "jobs", lambda m: m["name"] == "python3 burn.py")
    assert m["cpu_pct"] == pytest.approx(60, abs=0.5)


def test_renamed_process_is_not_counted_as_new(world):
    """kworkers (and prctl) change comm: the same (pid, start) keeps its ticks."""
    fp, clock, c, _ = world
    fp.procs[3]["comm"] = "kworker/0:1-events"
    fp.tick(3, 1_000_000)          # huge lifetime total, but only 10 ticks new...
    clock.t += 5
    c.sample()
    fp.tick(3, 10)
    clock.t += 5
    snap = c.sample()
    k = member(snap, "system", lambda m: m["id"] == "sys:kernel")
    assert k["cpu_pct"] == pytest.approx(2, abs=0.2)
    fp.procs[3]["comm"] = "kworker/0:1-mm"
    fp.tick(3, 5)
    clock.t += 5
    snap = c.sample()
    k = member(snap, "system", lambda m: m["id"] == "sys:kernel")
    assert k["cpu_pct"] == pytest.approx(1, abs=0.2)


def test_dead_process_disappears(world):
    fp, clock, c, _ = world
    fp.remove(1400)
    clock.t += 5
    snap = c.sample()
    assert not [m for m in group(snap, "jobs")["members"] if "next" in m["name"]]


# ── grouping ────────────────────────────────────────────────────────────────
def test_group_order_and_titles(world):
    snap = world[3]
    assert [(g["id"], g["title"]) for g in snap["groups"]] == [
        ("agents", "Agents"), ("apps", "Sites & apps"),
        ("jobs", "Background jobs"), ("system", "System")]


def test_library_terminal_agent_with_its_children(world):
    snap = world[3]
    a = member(snap, "agents", lambda m: m.get("terminal") == "cs-b36f2fd1")
    assert a["name"] == "ImmAppeal deploy"
    assert a["kind"] == "claude"
    assert a["status"] == "working"
    # claude 10 % + pytest 100 % + mcp node 5 %; bash 0
    assert a["cpu_pct"] == pytest.approx(115, abs=0.5)
    assert a["rss_mb"] == pytest.approx(400 + 10 + 200 + 60, abs=1)
    assert "~/pr/Appeals/dev" in a["what"]
    names = [ch["name"] for ch in a["children"]]
    assert names[0] == "pytest"                      # busiest first
    assert "node mcp-server.js" in names
    # its children are not listed anywhere else
    assert not [m for m in group(snap, "jobs")["members"] if "pytest" in m["name"]]


def test_legacy_terminal_is_named_by_its_conversation(world):
    snap = world[3]
    a = member(snap, "agents", lambda m: m.get("terminal") == "claude-terminal-3")
    assert a["name"] == "TG bridge work"
    assert a["status"] == "idle"
    assert a["cpu_pct"] == pytest.approx(1, abs=0.2)


def test_claude_outside_tmux_is_still_an_agent(world):
    snap = world[3]
    a = member(snap, "agents", lambda m: m["id"].endswith(":800"))
    assert a["terminal"] == ""
    assert "no terminal" in a["what"].lower()
    assert "/tmp/scratch" in a["what"]


def test_claude_inside_a_container_belongs_to_the_container(world):
    snap = world[3]
    assert not [m for m in group(snap, "agents")["members"] if m["id"].endswith(":1002")]
    d = member(snap, "apps", lambda m: m["name"] == "immappeal-dev")
    assert d["kind"] == "container"
    assert d["status"] == "working"                 # 50 % of a core
    assert d["cpu_pct"] == pytest.approx(50, abs=0.5)
    assert d["rss_mb"] == pytest.approx(1000, abs=1)
    assert "ImmAppeal" in d["what"]


def test_stopped_container_is_listed_as_stopped(world):
    d = member(world[3], "apps", lambda m: m["name"] == "old-thing")
    assert d["status"] == "stopped"
    assert d["cpu_pct"] == 0


def test_services_are_discovered_by_cgroup_with_friendly_names(world):
    snap = world[3]
    s = member(snap, "apps", lambda m: m["id"] == "unit:safin-rag.service")
    assert s["kind"] == "service"
    assert s["name"] == "Safin RAG"
    assert s["what"] == "Safin RAG API (FastAPI + pgvector + Claude)"
    n = member(snap, "apps", lambda m: m["id"] == "unit:nginx.service")
    assert n["name"] == "nginx"
    assert n["cpu_pct"] == pytest.approx(2, abs=0.2)
    # a known-but-failed unit shows up as stopped
    v = member(snap, "apps", lambda m: m["id"] == "unit:vpn-bot.service")
    assert v["status"] == "stopped"


def test_pm2_ttyd_apps_are_web_terminals_without_their_password(world):
    snap = world[3]
    t = member(snap, "apps", lambda m: m["kind"] == "pm2")
    assert t["name"] == "Web terminals (ttyd)"
    assert "hunter2pass" not in json.dumps(snap)


def test_command_line_job_is_owned_by_its_terminal(world):
    snap = world[3]
    j = member(snap, "jobs", lambda m: m["name"] == "python3 _cl_bulk_texts.py")
    assert j["owner"] == "cmd-shell"
    assert j["cpu_pct"] == pytest.approx(80, abs=0.5)
    assert j["status"] == "working"


def test_orphan_build_is_a_background_job(world):
    snap = world[3]
    j = member(snap, "jobs", lambda m: m["name"] == "node next build")
    assert j["cpu_pct"] == pytest.approx(200, abs=0.5)
    assert j["rss_mb"] == pytest.approx(900, abs=1)


def test_system_has_kernel_and_other(world):
    snap = world[3]
    names = [m["name"] for m in group(snap, "system")["members"]]
    assert "Kernel threads" in names
    assert "Other" in names
    other = member(snap, "system", lambda m: m["name"] == "Other")
    assert other["count"] >= 2           # cron, sshd, sleep, init ... folded together


def test_every_process_is_counted_once(world):
    fp, clock, c, snap = world
    total = sum(m["rss_mb"] for g in snap["groups"] for m in g["members"])
    assert total == pytest.approx(sum(p["rss_mb"] for p in fp.procs.values()), abs=2)
    for g in snap["groups"]:
        assert g["cpu_pct"] == pytest.approx(sum(m["cpu_pct"] for m in g["members"]), abs=0.2)


def test_member_ids_are_unique_and_stable(world):
    fp, clock, c, snap = world
    ids = [m["id"] for g in snap["groups"] for m in g["members"]]
    assert len(ids) == len(set(ids))
    clock.t += 5
    ids2 = [m["id"] for g in c.sample()["groups"] for m in g["members"]]
    assert set(ids) == set(ids2)


# ── security ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("argv,comm,label", [
    (["python3", "/home/ubuntu/pr/x/_cl_bulk_texts.py", "--key", SECRET], "python3",
     "python3 _cl_bulk_texts.py"),
    (["/usr/bin/python3.11", "-m", "pytest", "-k", "foo"], "python3.11", "pytest"),
    (["python3", "-u", "-m", "uvicorn", "app:app", "--port", "9020"], "python3", "uvicorn"),
    (["python3", "-c", f"import os; os.environ['K']='{SECRET}'"], "python3", "python3 -c"),
    (["node", "/x/node_modules/.bin/next", "build"], "node", "node next build"),
    (["next-server (v15.1.0)"], "next-server", "next-server"),
    (["npm", "run", "build"], "npm", "npm run build"),
    (["npm", "exec", f"--token={SECRET}"], "npm", "npm exec"),
    (["/bin/bash", "-c", f"curl -H 'Authorization: Bearer {SECRET}' x"], "bash", "bash -c"),
    (["/bin/bash", "/home/ubuntu/pr/terminal/launch-claude-3.sh"], "bash", "bash launch-claude-3.sh"),
    (["curl", "https://user:pw@example.com/"], "curl", "curl"),
    (["/usr/bin/ttyd", "-c", "user:hunter2pass", "bash"], "ttyd", "ttyd"),
    ([], "kworker/0:1", "kworker/0:1"),
    ([f"/opt/{SECRET}/run"], "run", "run"),
    ([f"{GH_TOKEN}"], "x", "x"),
    (["postgres: 14/main: checkpointer"], "postgres", "postgres"),
])
def test_label_is_basename_plus_script_only(argv, comm, label):
    assert ss.safe_label(argv, comm) == label


@pytest.mark.parametrize("text,bad", [
    (f"token={SECRET}", SECRET),
    (f"postgres://app:s3cretpw@localhost/db", "s3cretpw"),
    (f"Authorization: Bearer {GH_TOKEN}", GH_TOKEN),
    (f"path/{GH_TOKEN}/x", GH_TOKEN),
    ("password: hunter2pass", "hunter2pass"),
    ("AWS AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
])
def test_redact_free_text(text, bad):
    out = ss.redact(text)
    assert bad not in out
    assert len(out) <= max(len(text), 200)


def test_redact_keeps_ordinary_text():
    s = "Safin RAG API (FastAPI + pgvector + Claude) ~/pr/Appeals/dev immappeal:f5010d7"
    assert ss.redact(s) == s


def test_snapshot_never_leaks_cmdlines_or_environment(world):
    fp, clock, c, snap = world
    blob = json.dumps(snap)
    for bad in (SECRET, GH_TOKEN, "s3cretpw", "hunter2pass", "--token", "--api-key",
                "ANTHROPIC_API_KEY", "SECRET_TOKEN", "--resume", "tests/",
                "bbbb2222-3333-4444-8555-666677778888"):
        assert bad not in blob, bad


def test_collector_never_reads_environ(world, monkeypatch):
    fp, clock, c, _ = world
    real_open = open
    seen = []

    def spy(path, *a, **k):
        seen.append(str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", spy)
    clock.t += 5
    c.sample()
    assert seen and not [p for p in seen if p.endswith("/environ")]


# ── history ─────────────────────────────────────────────────────────────────
def test_history_ring_buffer(world):
    fp, clock, c, snap = world
    h = snap["history"]
    assert h["interval"] == 5
    pt = h["points"][-1]
    assert set(pt) >= {"t", "cpu", "ram", "g"}
    assert pt["cpu"] == pytest.approx(60, abs=0.5)
    assert set(pt["g"]) == {"agents", "apps", "jobs", "system"}
    assert pt["g"]["jobs"] == pytest.approx(280, abs=1)
    small = make_collector(fp, clock, history_len=3)
    for _ in range(6):
        clock.t += 5
        small.sample()
    pts = small.snapshot()["history"]["points"]
    assert len(pts) == 3
    assert pts[0]["t"] < pts[-1]["t"]


def test_snapshot_is_json_and_has_collector_cost(world):
    snap = world[2].snapshot()
    json.dumps(snap)
    assert snap["collector"]["sample_ms"] >= 0
    assert "cpu_pct" in snap["collector"]


# ── slow sources are cached ────────────────────────────────────────────────
def test_slow_sources_are_cached(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(1, "systemd", 0, cgroup="0::/init.scope")
    calls = {"docker": 0, "units": 0}

    def docker():
        calls["docker"] += 1
        return {}

    def units():
        calls["units"] += 1
        return {}
    clock = Clock()
    c = make_collector(fp, clock, docker_ps=docker, units=units)
    for _ in range(3):              # 3 samples within 10 s
        c.sample()
        clock.t += 4
    assert calls == {"docker": 1, "units": 1}
    clock.t += 60
    c.sample()
    assert calls == {"docker": 2, "units": 2}


def test_broken_sources_do_not_break_the_snapshot(tmp_path):
    fp = FakeProc(tmp_path / "proc")
    fp.add(1, "systemd", 0, cgroup="0::/init.scope")

    def boom():
        raise RuntimeError("docker not running")
    c = make_collector(fp, Clock(), docker_ps=boom, units=boom, tmux_panes=boom,
                       library_names=boom)
    snap = c.sample()
    assert [g["id"] for g in snap["groups"]] == ["agents", "apps", "jobs", "system"]


# ── real /proc: cheap enough ────────────────────────────────────────────────
def test_real_proc_sample_is_cheap():
    c = ss.Collector(docker_ps=lambda: {}, units=lambda: {}, tmux_panes=lambda: [],
                     library_names=lambda: ({}, {}))
    c.sample()
    t0 = time.process_time()
    for _ in range(3):
        c.sample()
    per = (time.process_time() - t0) / 3
    # at one sample per 5 s, 2 % of a core = 100 ms of CPU per sample
    assert per < 0.1, f"{per * 1000:.0f} ms CPU per sample"
    json.dumps(c.snapshot())


# ── the API on the dashboard backend (port-3011 Handler) ───────────────────
@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTDECK_TMUX_SOCKET", f"agentdeck-test-srv-{os.getpid()}")
    sys.modules.pop("status_server", None)
    srv_mod = importlib.import_module("status_server")
    fp = FakeProc(tmp_path / "proc")
    fp.add(1, "systemd", 0, cgroup="0::/init.scope")
    col = make_collector(fp, Clock())
    col.sample()
    monkeypatch.setattr(srv_mod, "SERVER_COLLECTOR", col)
    srv = HTTPServer(("127.0.0.1", 0), srv_mod.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_server_returns_the_snapshot(api):
    with urllib.request.urlopen(api + "/api/server", timeout=5) as r:
        assert r.status == 200
        assert r.headers["Content-Type"].startswith("application/json")
        assert r.headers.get("Access-Control-Allow-Origin") is None    # same-origin only
        assert "no-store" in (r.headers.get("Cache-Control") or "")
        data = json.loads(r.read())
    assert {"host", "groups", "history", "ts"} <= set(data)


def test_nginx_puts_api_server_behind_the_login():
    conf = (TERMINAL_DIR / "nginx" / "agents-subdomain.conf").read_text()
    m = re.search(r"location\s+(=\s*)?/api/server\s*\{([^}]*)\}", conf)
    assert m, "no nginx route for /api/server"
    body = m.group(2)
    assert "127.0.0.1:3011" in body
    assert "auth_request off" not in body
    # the server-level auth_request covers every location that doesn't switch it off
    assert re.search(r"^\s*auth_request /__auth;", conf, re.M)
