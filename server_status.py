#!/usr/bin/env python3
"""Server status collector for the dashboard's "Server" page (GET /api/server).

One sample every few seconds, all from /proc: every process's CPU ticks and RSS,
its cgroup (which systemd unit or docker container it lives in) and its parent.
From that, each process lands in exactly one member of one group:

  Agents           every top-most Claude Code process (`claude`) with its whole
                   process tree (tests, builds, MCP servers it started), mapped to
                   its tmux terminal (cs-<id> -> name from the session library;
                   legacy claude-terminal-N -> name of its conversation)
  Sites & apps     docker containers (by cgroup), the owner's systemd services
                   (user units + a table of system ones), PM2 apps (ttyd terminals)
  Background jobs  anything else that is real work: python/node/npm/pytest/...
                   or anything using CPU, with who started it (a terminal, an ssh
                   session, or nobody)
  System           kernel threads, big system daemons, and "Other" for the rest

CPU % is the tick delta between two samples (100 % = one full core), summed
over a member's processes. The slow sources (docker ps, systemctl, pm2 pid files)
are cached for 15-30 s; tmux list-panes is one cheap call per sample.

SECURITY: command lines on this box can carry tokens. Nothing here ever sends a
command line or environment out: a process is labelled with its executable's
basename + the script/module basename (safe_label), and every free text that
comes from the system (unit descriptions, names, paths) goes through redact().
/proc/<pid>/environ is never read.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque

WORKING_PCT = 5.0          # a member using this much CPU (of one core) is "working"
DOCKER_TTL = 15.0
UNITS_TTL = 30.0
PM2_TTL = 30.0
HISTORY_LEN = 180          # 15 min at 5 s
INTERVAL = 5.0
GROUPS = (("agents", "Agents"), ("apps", "Sites & apps"),
          ("jobs", "Background jobs"), ("system", "System"))
HOME = os.path.expanduser("~")

# ── friendly names ──────────────────────────────────────────────────────────
# unit -> (name, what (None = the unit's own description), group)
FRIENDLY_UNITS = {
    "nginx.service": ("nginx", "Web server and reverse proxy for every site", "apps"),
    "postgresql@14-main.service": ("PostgreSQL 14", "Host database (Safin RAG and others)", "apps"),
    "mariadb.service": ("MariaDB", "MySQL-compatible database", "apps"),
    "php8.1-fpm.service": ("PHP-FPM", "Runs the PHP sites", "apps"),
    "secondwind-backend.service": ("SecondWind backend", None, "apps"),
    "secondwind-storefront.service": ("SecondWind storefront", None, "apps"),
    "svetlota-orders.service": ("Svetlota orders", None, "apps"),
    "ttyd.service": ("ttyd", "Browser terminal", "apps"),
    "postfix@-.service": ("Postfix", "Mail server", "apps"),
    "agents-status-server.service": ("Dashboard backend", "Serves this dashboard's APIs", "apps"),
    "claude-telegram-bot.service": ("Claude Telegram bot", None, "apps"),
    "claude-tg-bridge.service": ("Telegram bridge", "Telegram <-> terminal bridge", "apps"),
    "iai-embedder.service": ("IAI embedder", "Local text embedder for agent memory", "apps"),
    "iai-mcp-daemon.service": ("IAI memory daemon", "Consolidates agent memory between sessions", "apps"),
    "safin-rag.service": ("Safin RAG", None, "apps"),
    "tasks-tracker.service": ("Task board", "Live task board for the agents", "apps"),
    "lagratar-order-bot.service": ("La Gratar order bot", None, "apps"),
    "vpn-bot.service": ("VPN sales bot", None, "apps"),
    "kolobok-api.service": ("Kolobok VPN API", None, "apps"),
    "contact-api.service": ("Contact API", None, "apps"),
    "orchestra-web.service": ("Orchestra web", None, "apps"),
    "yacht-api.service": ("Yacht booking API", None, "apps"),
    "docker.service": ("Docker engine", "Runs the containers", "system"),
    "containerd.service": ("containerd", "Container runtime", "system"),
    "tailscaled.service": ("Tailscale", "Private network", "system"),
    "memguard.service": ("memguard", "Kills runaway processes when memory runs low", "system"),
    "dbus.service": ("D-Bus", "System message bus", "system"),
    "gpg-agent.service": ("gpg-agent", "Key agent", "system"),
}
# docker container name -> (display name, what); regex groups fill {1}, {2}
FRIENDLY_CONTAINERS = [
    (r"immappeal-prod-(blue|green)", "ImmAppeal production web + API ({1} slot)"),
    (r"immappeal-dev", "ImmAppeal web + API (dev)"),
    (r"immappeal-(\w+)-db", "ImmAppeal {1} database (Postgres + pgvector)"),
    (r"sparkaide-web", "SparkAide cleaning platform (Next.js)"),
    (r"sparkaide-db", "SparkAide database (Postgres)"),
    (r"secondwind-db", "SecondWind database (Postgres)"),
    (r"3x-ui", "VPN panel + Xray (Kolobok VPN)"),
]
_SKIP_UNITS = re.compile(r"^(pm2-\w+|user@\d+)\.service$")

# ── process labels (never a full command line) ──────────────────────────────
_INTERP = re.compile(r"^(python[\d.]*|node|nodejs|bun|deno|ruby|perl|php[\d.]*|java|bash|sh|zsh|dash)$")
_PKG = {"npm", "npx", "pnpm", "yarn", "uv", "pip", "pip3", "poetry", "cargo", "go", "make"}
_VERBS = {"run", "exec", "build", "start", "dev", "test", "serve", "install", "ci", "watch",
          "lint", "runserver", "migrate", "worker", "preview", "export", "generate", "x"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_WORD_OK = re.compile(r"[^A-Za-z0-9._+@-]")
_TOKEN = re.compile(r"(?:\b(?:sk|pk|rk|ghp|gho|ghu|ghs|github_pat|xox[abprs]|AKIA|ASIA|glpat)[-_]?"
                    r"[A-Za-z0-9_\-.]{8,}|\beyJ[A-Za-z0-9_\-.]{12,})")
_RUN = re.compile(r"[A-Za-z0-9_+=-]{24,}")     # a long run: a secret if it mixes letters+digits


def _long_secret(m):
    w = m.group(0)
    return "***" if re.search(r"\d", w) and re.search(r"[A-Za-z]", w) else w


def _has_long_secret(s):
    return any(_long_secret(m) == "***" for m in _RUN.finditer(s))
_URL_CRED = re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.I)
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{6,}")
_KV = re.compile(r"(?i)\b([\w-]*(?:token|secret|passw(?:or)?d|pwd|api[_-]?key|apikey|auth\w*|"
                 r"credential|private[_-]?key)[\w-]*)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")


def redact(text, limit=200):
    """Free text from the system with anything credential-like replaced by ***."""
    s = str(text or "")
    s = _URL_CRED.sub(r"\1***@", s)
    s = _BEARER.sub(r"\1 ***", s)
    s = _KV.sub(r"\1\2***", s)
    s = _TOKEN.sub("***", s)
    s = _RUN.sub(_long_secret, s)
    return s[:limit]


def _secretish(word):
    return bool(_TOKEN.search(word) or _has_long_secret(word) or "=" in word or "://" in word
                or ("@" in word and ":" in word))


def _clean(word):
    w = _WORD_OK.sub("", word)[:40]
    return "" if not w or _secretish(word) or _secretish(w) else w


def safe_label(argv, comm):
    """'python3 _cl_bulk_texts.py', 'pytest', 'node next build', 'ttyd' — an
    executable basename, plus for interpreters the script/module basename and
    well-known subcommand verbs. Never an option value, never a free argument."""
    comm_clean = re.sub(r"[^A-Za-z0-9._+@/:-]", "", comm or "")[:40] or "?"
    if not argv:
        return comm_clean
    if len(argv) == 1 and " " in argv[0]:            # process title: "next-server (v15)"
        argv = [argv[0].split()[0].rstrip(":")]
    exe = _clean(os.path.basename(argv[0]).lstrip("-"))
    if not exe:
        return _clean(comm or "") or "?"
    rest = argv[1:]
    words = [exe]
    if _INTERP.match(exe):
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "-m" and exe.startswith("python") and i + 1 < len(rest):
                mod = re.sub(r"[^\w.]", "", rest[i + 1])[:40]
                return mod if mod and not _secretish(mod) else exe
            if a in ("-c", "-e", "--eval") or (exe in _SHELLS and a.startswith("-") and "c" in a[1:]):
                return f"{exe} {a}" if a in ("-c", "-e") else f"{exe} -c"
            if a.startswith("-"):
                i += 1
                continue
            script = _clean(os.path.basename(a.rstrip("/")))
            if script:
                words.append(script)
                rest = rest[i + 1:]
                break
            return exe
        else:
            return exe
    elif exe not in _PKG:
        return exe
    for a in rest[:3]:                               # subcommand verbs only
        if a in _VERBS:
            words.append(a)
        else:
            break
    return " ".join(words)


def label_base(label):
    return label.split(" ", 1)[0]


# ── math ────────────────────────────────────────────────────────────────────
def cpu_pct(t1, t2, dt, hz):
    """% of one core used between two tick readings dt seconds apart."""
    if dt <= 0 or t2 <= t1:
        return 0.0
    return (t2 - t1) / hz / dt * 100.0


def pressure(cpu_pct, load1, cores, ram_available_pct):
    """(verdict, reason): ok / busy / overloaded."""
    cores = max(1, cores)
    if cpu_pct >= 90:
        return "overloaded", f"CPU at {cpu_pct:.0f}%"
    if load1 > 1.5 * cores:
        return "overloaded", f"load {load1:.1f} on {cores} cores"
    if ram_available_pct < 10:
        return "overloaded", f"only {ram_available_pct:.0f}% of RAM free"
    if cpu_pct >= 60:
        return "busy", f"CPU at {cpu_pct:.0f}%"
    if load1 > cores:
        return "busy", f"load {load1:.1f} on {cores} cores"
    if ram_available_pct < 25:
        return "busy", f"{ram_available_pct:.0f}% of RAM free"
    return "ok", "all normal"


def short_path(p):
    if not p:
        return ""
    if p == HOME or p.startswith(HOME + "/"):
        p = "~" + p[len(HOME):]
    return redact(p, 80)


# ── default sources (all injectable for tests) ─────────────────────────────
def _run(cmd, timeout=5):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout if r.returncode == 0 else ""


def default_tmux_panes():
    """[(session_name, pane_pid)] — list-panes only (display-message segfaults tmux 3.2a)."""
    sock = os.environ.get("AGENTDECK_TMUX_SOCKET")
    out = _run(["tmux", *(["-L", sock] if sock else []), "list-panes", "-a",
                "-F", "#{session_name}\t#{pane_pid}"], timeout=3)
    panes = []
    for line in out.splitlines():
        name, _, pid = line.partition("\t")
        if pid.isdigit():
            panes.append((name, int(pid)))
    return panes


def default_docker_ps():
    out = _run(["docker", "ps", "-a", "--no-trunc", "--format",
                "{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}"])
    res = {}
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) == 5:
            res[p[0]] = {"name": p[1], "state": p[2], "status": p[3], "image": p[4]}
    return res


def default_units():
    res = {}
    for scope, extra in (("system", []), ("user", ["--user"])):
        out = _run(["systemctl", *extra, "list-units", "--type=service", "--all", "--plain",
                    "--no-legend", "--no-pager"])
        for line in out.splitlines():
            p = line.split(None, 4)
            if len(p) >= 4 and p[0].endswith(".service"):
                res[p[0]] = {"active": p[2], "scope": scope, "desc": p[4] if len(p) > 4 else ""}
    return res


def default_pm2_names():
    d = os.path.join(HOME, ".pm2", "pids")
    res = {}
    try:
        for f in os.listdir(d):
            m = re.fullmatch(r"(.+)-\d+\.pid", f)
            if not m:
                continue
            try:
                with open(os.path.join(d, f)) as fh:
                    res[int(fh.read().strip())] = m.group(1)
            except (OSError, ValueError):
                pass
    except OSError:
        pass
    return res


_lib_cache = {"key": None, "val": ({}, {})}


def default_library_names():
    """({sid: name}, {uuid: name}) from the session library (cached by mtime)."""
    import library
    path = os.environ.get("AGENTDECK_LIBRARY") or library.LIB_FILE
    try:
        key = os.stat(path).st_mtime_ns
    except OSError:
        return {}, {}
    if _lib_cache["key"] != key:
        lib = library.load(path)
        by_sid = {e["id"]: e.get("name") or e["id"] for e in lib.get("sessions", [])}
        by_uuid = {e["uuid"]: e.get("name") or e["id"] for e in lib.get("sessions", [])
                   if e.get("uuid")}
        _lib_cache.update(key=key, val=(by_sid, by_uuid))
    return _lib_cache["val"]


# ── the collector ───────────────────────────────────────────────────────────
class _P:
    __slots__ = ("pid", "ppid", "comm", "ticks", "rss_mb", "start", "cpu", "key")


_UUID = re.compile(r"--(?:resume|session-id)=?\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                   r"[0-9a-f]{4}-[0-9a-f]{12})")
_DOCKER_CG = re.compile(r"docker[-/]([0-9a-f]{64})")
_LEGACY = re.compile(r"claude-terminal(?:-(\d+))?")
_JOB_EXES = re.compile(r"^(python[\d.]*|node|nodejs|npm|npx|pnpm|yarn|bun|deno|pytest|uvicorn|"
                       r"gunicorn|java|go|cargo|rustc|make|gcc|g\+\+|cc1\w*|ld|php[\d.]*|ruby|perl|"
                       r"ffmpeg|git|rsync|pg_dump|pg_restore|psql|chrome|chromium|headless_shell|"
                       r"playwright|esbuild|tsc|vite|next|next-server|webpack|curl|wget|tar|gzip|"
                       r"xz|zstd|docker|pip|pip3|uv|poetry)$")
_NOT_JOBS = {"tmux", "tmux:", "bash", "sh", "zsh", "dash", "sshd", "systemd", "init", "sleep",
             "login", "PM2", "sudo", "su", "cron", "agetty"}


class Collector:
    def __init__(self, proc_root="/proc", interval=INTERVAL, history_len=HISTORY_LEN,
                 tmux_panes=None, docker_ps=None, units=None, pm2_names=None,
                 library_names=None, disk_path="/", clock=time.time, hz=None, page_kb=None):
        self.root = proc_root
        self.interval = interval
        self.clock = clock
        self.hz = hz or os.sysconf("SC_CLK_TCK")
        self.page_kb = page_kb or os.sysconf("SC_PAGE_SIZE") // 1024
        self.disk_path = disk_path
        self.src_tmux = tmux_panes or default_tmux_panes
        self.src_docker = docker_ps or default_docker_ps
        self.src_units = units or default_units
        self.src_pm2 = pm2_names or default_pm2_names
        self.src_lib = library_names or default_library_names
        self._cache = {}                 # name -> (t, value)
        self._meta = {}                  # (pid, start, comm) -> {"argv", "cgroup"}
        self._prev_ticks = {}
        self._prev_t = None
        self._prev_cpu = None
        self.history = deque(maxlen=history_len)
        self.latest = None
        self._cost = deque(maxlen=12)    # (cpu seconds, wall ms) per sample
        self._lock = threading.Lock()
        self._thread = None

    # -- sources ----------------------------------------------------------
    def _cached(self, name, ttl, fn, default):
        now = self.clock()
        hit = self._cache.get(name)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        try:
            val = fn()
        except Exception:
            val = hit[1] if hit is not None else default
        self._cache[name] = (now, val)
        return val

    @staticmethod
    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    def _read(self, path):
        with open(path) as f:
            return f.read()

    def _meta_for(self, p):
        """argv + cgroup for a process, read once per (pid, start, comm)."""
        m = self._meta.get(p.key)
        if m is None:
            m = {}
            try:
                with open(f"{self.root}/{p.pid}/cmdline", "rb") as f:
                    raw = f.read()
                m["argv"] = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
            except OSError:
                m["argv"] = []
            try:
                m["cgroup"] = self._read(f"{self.root}/{p.pid}/cgroup")
            except OSError:
                m["cgroup"] = ""
            self._meta[p.key] = m
        return m

    def _label(self, p):
        m = self._meta_for(p)
        if "label" not in m:
            m["label"] = safe_label(m["argv"], p.comm)
        return m["label"]

    def _cwd(self, pid):
        try:
            return os.readlink(f"{self.root}/{pid}/cwd")
        except OSError:
            return ""

    def _read_procs(self):
        procs = {}
        for name in os.listdir(self.root):
            if not name.isdigit():
                continue
            try:
                with open(f"{self.root}/{name}/stat") as f:
                    s = f.read()
            except OSError:
                continue
            try:
                head, tail = s.rsplit(")", 1)
                fl = tail.split()
                p = _P()
                p.pid = int(name)
                p.comm = head.split("(", 1)[1]
                p.ppid = int(fl[1])
                p.ticks = int(fl[11]) + int(fl[12])
                p.start = int(fl[19])
                p.rss_mb = int(fl[21]) * self.page_kb / 1024.0
            except (IndexError, ValueError):
                continue
            p.key = (p.pid, p.start, p.comm)
            procs[p.pid] = p
        return procs

    def _read_host(self):
        cpus = {}
        for line in self._read(f"{self.root}/stat").splitlines():
            if line.startswith("cpu"):
                name, *vals = line.split()
                v = [int(x) for x in vals[:8]] + [0] * max(0, 8 - len(vals))
                cpus[name] = (sum(v[:8]), v[3] + v[4])      # total, idle+iowait
        mem = {}
        for line in self._read(f"{self.root}/meminfo").splitlines():
            k, _, v = line.partition(":")
            try:
                mem[k] = int(v.split()[0])
            except (IndexError, ValueError):
                pass
        load = [float(x) for x in self._read(f"{self.root}/loadavg").split()[:3]]
        uptime = float(self._read(f"{self.root}/uptime").split()[0])
        return cpus, mem, load, uptime

    # -- sampling ---------------------------------------------------------
    def sample(self):
        c0, w0 = time.thread_time(), time.perf_counter()
        now = self.clock()
        procs = self._read_procs()
        dt = (now - self._prev_t) if self._prev_t is not None else 0.0
        ticks = {}
        for p in procs.values():
            # by (pid, start), not comm: kworkers and prctl() rename themselves
            ticks[(p.pid, p.start)] = p.ticks
            if self._prev_t is None:
                p.cpu = 0.0
            else:
                p.cpu = cpu_pct(self._prev_ticks.get((p.pid, p.start), 0), p.ticks, dt, self.hz)
        self._prev_ticks = ticks
        self._prev_t = now
        live = {p.key for p in procs.values()}
        if len(self._meta) > 2 * len(live) + 100:
            self._meta = {k: v for k, v in self._meta.items() if k in live}

        host = self._host(dt)
        groups = self._classify(procs)
        point = {"t": round(now, 1), "cpu": host["cpu_pct"], "ram": host["ram_pct"],
                 "g": {g["id"]: g["cpu_pct"] for g in groups}}
        self._cost.append((time.thread_time() - c0, (time.perf_counter() - w0) * 1000))
        cpu_s = sum(c for c, _ in self._cost) / len(self._cost)
        snap = {
            "ts": round(now, 1),
            "interval": self.interval,
            "host": host,
            "groups": groups,
            "collector": {"sample_ms": round(self._cost[-1][1], 1),
                          "cpu_pct": round(cpu_s / self.interval * 100, 2),
                          "processes": len(procs)},
        }
        with self._lock:
            self.history.append(point)
            self.latest = snap
        return self.snapshot()

    def snapshot(self):
        with self._lock:
            if self.latest is None:
                return None
            snap = dict(self.latest)
            snap["history"] = {"interval": self.interval, "points": list(self.history)}
            return snap

    def _host(self, dt):
        try:
            cpus, mem, load, uptime = self._read_host()
        except (OSError, ValueError):
            cpus, mem, load, uptime = {}, {}, [0.0, 0.0, 0.0], 0.0
        prev, self._prev_cpu = self._prev_cpu, cpus

        def pct(name):
            if not prev or name not in prev or name not in cpus:
                return 0.0
            dtot = cpus[name][0] - prev[name][0]
            didle = cpus[name][1] - prev[name][1]
            return round(max(0.0, min(100.0, (dtot - didle) / dtot * 100)), 1) if dtot > 0 else 0.0
        cores = sorted((k for k in cpus if k != "cpu"), key=lambda k: int(k[3:]))
        total_mb = mem.get("MemTotal", 0) / 1024
        avail_mb = mem.get("MemAvailable", mem.get("MemFree", 0)) / 1024
        used_mb = total_mb - avail_mb
        try:
            st = os.statvfs(self.disk_path)
            dtot = st.f_blocks * st.f_frsize / 1e9
            dfree = st.f_bavail * st.f_frsize / 1e9
            dused = (st.f_blocks - st.f_bfree) * st.f_frsize / 1e9
        except OSError:
            dtot = dfree = dused = 0.0
        ncores = len(cores) or os.cpu_count() or 1
        cpu = pct("cpu")
        avail_pct = avail_mb / total_mb * 100 if total_mb else 100.0
        verdict, why = pressure(cpu, load[0], ncores, avail_pct)
        return {
            "hostname": redact(socket.gethostname(), 60),
            "cores": ncores,
            "load": load,
            "cpu_pct": cpu,
            "cpu_per_core": [pct(c) for c in cores],
            "ram_total_mb": round(total_mb),
            "ram_used_mb": round(used_mb),
            "ram_available_mb": round(avail_mb),
            "ram_pct": round(used_mb / total_mb * 100, 1) if total_mb else 0.0,
            "swap_total_mb": round(mem.get("SwapTotal", 0) / 1024),
            "swap_used_mb": round((mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / 1024),
            "disk_total_gb": round(dtot, 1),
            "disk_used_gb": round(dused, 1),
            "disk_free_gb": round(dfree, 1),
            "disk_pct": round(dused / (dused + dfree) * 100, 1) if dused + dfree > 0 else 0.0,
            "uptime_s": int(uptime),
            "pressure": verdict,
            "pressure_reason": why,
        }

    # -- classification ---------------------------------------------------
    def _classify(self, procs):
        kids = {}
        for p in procs.values():
            kids.setdefault(p.ppid, []).append(p.pid)
        owner = {}                                   # pid -> member key
        members = {}                                 # key -> member dict (without totals)

        def claim(root, key, stop=None):
            stack = [root]
            while stack:
                pid = stack.pop()
                if pid in owner or pid not in procs or (stop and stop(pid)):
                    continue
                owner[pid] = key
                stack.extend(kids.get(pid, ()))

        panes = self._safe(self.src_tmux, [])
        pane_of = {pid: name for name, pid in panes}
        docker = self._cached("docker", DOCKER_TTL, self.src_docker, {})
        units = self._cached("units", UNITS_TTL, self.src_units, {})
        pm2 = self._cached("pm2", PM2_TTL, self.src_pm2, {})
        by_sid, by_uuid = self._safe(self.src_lib, ({}, {}))

        cg_cache = {}

        def cgroup(pid):
            if pid not in cg_cache:
                cg_cache[pid] = self._meta_for(procs[pid])["cgroup"]
            return cg_cache[pid]

        def docker_id(pid):
            m = _DOCKER_CG.search(cgroup(pid))
            return m.group(1) if m else None

        def unit_of(pid):
            for part in reversed(cgroup(pid).strip().split("/")):
                if part.endswith(".service"):
                    return None if _SKIP_UNITS.match(part) else part
                if part.endswith(".scope") and part.startswith("session-"):
                    return None
            return None

        # 1. kernel threads
        for p in procs.values():
            if p.pid == 2 or p.ppid == 2:
                owner[p.pid] = "sys:kernel"
        members["sys:kernel"] = {"id": "sys:kernel", "group": "system", "name": "Kernel threads",
                                 "kind": "kernel", "what": "Linux kernel workers"}

        # 2. docker containers
        for p in procs.values():
            if p.pid in owner:
                continue
            cid = docker_id(p.pid)
            if cid:
                info = docker.get(cid) or {"name": cid[:12], "state": "running", "status": "",
                                           "image": ""}
                key = "docker:" + info["name"]
                owner[p.pid] = key
                members.setdefault(key, self._container_member(key, info))
        for cid, info in docker.items():
            key = "docker:" + info["name"]
            members.setdefault(key, self._container_member(key, info))

        # 3. agents: top-most claude processes and everything under them
        def is_claude(p):
            if p.comm == "claude":
                return True
            argv = self._meta_for(p)["argv"]
            return bool(argv) and os.path.basename(argv[0]) == "claude"
        for p in sorted(procs.values(), key=lambda p: p.start):
            if p.pid in owner or not is_claude(p):
                continue
            anc, top = procs.get(p.ppid), True
            seen = 0
            while anc is not None and anc.pid > 1 and seen < 64:
                if is_claude(anc) and anc.pid not in owner:
                    top = False
                    break
                anc = procs.get(anc.ppid)
                seen += 1
            if not top:
                continue
            key = f"agent:{p.pid}"
            claim(p.pid, key, stop=lambda pid: docker_id(pid) is not None)
            members[key] = self._agent_member(key, p, procs, pane_of, by_sid, by_uuid,
                                              unit_of(p.pid))

        # 4. systemd units (by cgroup)
        for p in procs.values():
            if p.pid in owner:
                continue
            u = unit_of(p.pid)
            if not u:
                continue
            fr = FRIENDLY_UNITS.get(u)
            scope = (units.get(u) or {}).get("scope") or (
                "user" if "/user@" in cgroup(p.pid) else "system")
            grp = fr[2] if fr else ("apps" if scope == "user" else "system")
            key = ("unit:" if grp == "apps" else "sysunit:") + u
            owner[p.pid] = key
            if key not in members:
                members[key] = self._unit_member(key, u, grp, units.get(u), fr)
        for u, info in units.items():
            fr = FRIENDLY_UNITS.get(u)
            grp = fr[2] if fr else ("apps" if info.get("scope") == "user" else "system")
            if grp != "apps" or ("unit:" + u) in members:
                continue
            if info.get("active") == "failed" or (fr and info.get("active") == "inactive"):
                m = self._unit_member("unit:" + u, u, grp, info, fr)
                m["stopped"] = True
                members["unit:" + u] = m

        # 5. PM2 apps: children of the PM2 daemon
        for god in procs.values():
            if not god.comm.startswith("PM2"):
                continue
            for cpid in kids.get(god.pid, ()):
                if cpid in owner:
                    continue
                lab = self._label(procs[cpid])
                if label_base(lab) == "ttyd":
                    key = "pm2:ttyd"
                    members.setdefault(key, {"id": key, "group": "apps", "kind": "pm2",
                                             "name": "Web terminals (ttyd)",
                                             "what": "Browser terminals behind this dashboard"})
                else:
                    name = _clean(pm2.get(cpid, "")) or lab
                    key = "pm2:" + name
                    members.setdefault(key, {"id": key, "group": "apps", "kind": "pm2",
                                             "name": name, "what": f"PM2 app ({lab})"})
                claim(cpid, key)

        # 6. background jobs among the rest; everything else is "Other"
        def candidate(p):
            if p.pid in owner or p.pid == 1:
                return False
            lab = self._label(p)
            base = label_base(lab)
            if base in _SHELLS:
                return " " in lab and not lab.endswith(" -c")        # bash some-script.sh
            if base in _NOT_JOBS or base.startswith("tmux"):
                return False
            return bool(_JOB_EXES.match(base)) or p.cpu >= 2 or p.rss_mb >= 200
        cands = {p.pid for p in procs.values() if candidate(p)}
        for pid in sorted(cands, key=lambda x: procs[x].start):
            if pid in owner:
                continue
            anc, root = procs.get(procs[pid].ppid), pid
            while anc is not None and anc.pid not in owner and anc.pid > 1:
                if anc.pid in cands:
                    root = anc.pid
                anc = procs.get(anc.ppid)
            if root in owner:
                continue
            key = f"job:{root}"
            claim(root, key)
            members[key] = self._job_member(key, procs[root], procs, pane_of, unit_of)
        for p in procs.values():
            if p.pid not in owner:
                owner[p.pid] = "sys:other"
        members["sys:other"] = {"id": "sys:other", "group": "system", "name": "Other",
                                "kind": "system", "what": ""}

        return self._totals(procs, owner, members)

    def _container_member(self, key, info):
        what = None
        for pat, desc in FRIENDLY_CONTAINERS:
            m = re.fullmatch(pat, info["name"])
            if m:
                what = desc
                for i, g in enumerate(m.groups(), 1):
                    what = what.replace("{%d}" % i, g)
                break
        img = redact(info.get("image", ""), 60)
        return {"id": key, "group": "apps", "kind": "container", "name": redact(info["name"], 60),
                "what": what or (f"Docker container ({img})" if img else "Docker container"),
                "detail": redact(info.get("status", ""), 60),
                "stopped": info.get("state") not in (None, "running", "restarting")}

    def _unit_member(self, key, unit, grp, info, fr):
        info = info or {}
        name = fr[0] if fr else unit[:-len(".service")]
        what = (fr[1] if fr and fr[1] else None) or info.get("desc") or unit
        return {"id": key, "group": grp, "kind": "service", "name": redact(name, 60),
                "what": redact(what, 120), "detail": unit,
                "stopped": info.get("active") in ("failed", "inactive")}

    def _agent_member(self, key, p, procs, pane_of, by_sid, by_uuid, unit):
        term, anc, hops = "", p, 0
        while anc is not None and hops < 8:
            if anc.pid in pane_of:
                term = pane_of[anc.pid]
                break
            anc = procs.get(anc.ppid)
            hops += 1
        name = ""
        if term.startswith("cs-"):
            name = by_sid.get(term[3:], "") or term[3:]
        if not name:
            m = _UUID.search(" ".join(self._meta_for(p)["argv"]))
            if m:
                name = by_uuid.get(m.group(1), "")
        if not name:
            lm = _LEGACY.fullmatch(term)
            if lm:
                name = f"Terminal {lm.group(1) or 1}"
            elif term:
                name = term
            else:
                name = "Claude (no terminal)"
        cwd = short_path(self._cwd(p.pid))
        what = "Claude Code" + (f" in {cwd}" if cwd else "")
        if not term:
            what += " · no terminal" + (f" · started by {unit}" if unit else "")
        return {"id": key, "group": "agents", "kind": "claude", "name": redact(name, 80),
                "what": what, "terminal": redact(term, 40), "agent_pid": p.pid}

    def _job_member(self, key, p, procs, pane_of, unit_of):
        own, anc, hops = "", procs.get(p.ppid), 0
        while anc is not None and hops < 16:
            if anc.pid in pane_of:
                own = pane_of[anc.pid]
                break
            anc = procs.get(anc.ppid)
            hops += 1
        if not own:
            cg = self._meta_for(p)["cgroup"]
            u = unit_of(p.pid)
            own = u or ("ssh session" if "/session-" in cg else "detached")
        cwd = short_path(self._cwd(p.pid))
        what = f"Started from {own}" + (f" · in {cwd}" if cwd else "")
        return {"id": key, "group": "jobs", "kind": "job", "name": self._label(p),
                "what": redact(what, 120), "owner": redact(own, 40)}

    def _totals(self, procs, owner, members):
        agg = {k: {"cpu": 0.0, "rss": 0.0, "n": 0, "labels": {}} for k in members}
        for pid, key in owner.items():
            p = procs[pid]
            a = agg[key]
            a["cpu"] += p.cpu
            a["rss"] += p.rss_mb
            a["n"] += 1
            m = members[key]
            if m["kind"] == "kernel" or (m["kind"] == "claude" and pid == m.get("agent_pid")):
                continue
            lab = self._label(p) if key != "sys:other" or p.cpu >= 0.5 or p.rss_mb >= 50 else None
            if lab is None:
                continue
            la = a["labels"].setdefault(lab, [0.0, 0.0, 0])
            la[0] += p.cpu
            la[1] += p.rss_mb
            la[2] += 1
        # small system units fold into "Other"
        for key in [k for k in members if k.startswith("sysunit:")]:
            a = agg[key]
            if a["cpu"] < 1.0 and a["rss"] < 150:
                o = agg["sys:other"]
                o["cpu"] += a["cpu"]
                o["rss"] += a["rss"]
                o["n"] += a["n"]
                for lab, v in a["labels"].items():
                    la = o["labels"].setdefault(lab, [0.0, 0.0, 0])
                    for i in range(3):
                        la[i] += v[i]
                del members[key]
        groups = {gid: [] for gid, _ in GROUPS}
        for key, m in members.items():
            a = agg[key]
            m = {k: v for k, v in m.items() if k not in ("group", "agent_pid", "stopped")} | {
                "cpu_pct": round(a["cpu"], 1), "rss_mb": round(a["rss"], 1), "count": a["n"]}
            src = members[key]
            if src.get("stopped") and a["n"] == 0:
                m["status"] = "stopped"
            elif a["cpu"] >= WORKING_PCT:
                m["status"] = "working"
            else:
                m["status"] = "idle" if src["kind"] == "claude" else "running"
            kids = sorted(a["labels"].items(), key=lambda kv: (-kv[1][0], -kv[1][1]))
            m["children"] = [{"name": lab, "cpu_pct": round(v[0], 1), "rss_mb": round(v[1], 1),
                              "count": v[2]} for lab, v in kids[:8]]
            if key == "sys:other":
                m["what"] = f"{a['n']} small processes: shells, daemons, tmux"
            elif src["kind"] == "claude" and m["children"] and m["children"][0]["cpu_pct"] >= 2:
                m["what"] += f" · running {m['children'][0]['name']}"
            groups[src["group"]].append(m)
        out = []
        for gid, title in GROUPS:
            ms = groups[gid]
            ms.sort(key=lambda m: (m["status"] == "stopped", m["id"] in ("sys:other",),
                                   -m["cpu_pct"], -m["rss_mb"]))
            out.append({"id": gid, "title": title,
                        "cpu_pct": round(sum(m["cpu_pct"] for m in ms), 1),
                        "rss_mb": round(sum(m["rss_mb"] for m in ms), 1),
                        "count": len(ms), "members": ms})
        return out

    # -- background thread --------------------------------------------------
    def start(self):
        """Sample every `interval` seconds in a daemon thread (idempotent)."""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop, name="server-status", daemon=True)
        self._thread.start()

    def _loop(self):
        try:
            self.sample()
            time.sleep(1.0)            # a quick second sample: CPU % on the first view
        except Exception:
            pass
        while True:
            try:
                self.sample()
            except Exception:
                pass
            time.sleep(self.interval)


if __name__ == "__main__":
    c = Collector()
    c.sample()
    time.sleep(2)
    print(json.dumps(c.sample(), indent=1))
