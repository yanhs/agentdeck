#!/usr/bin/env python3
"""AgentDeck services keeper.

A *service* is a program that must outlive a terminal and a conversation: a Tailscale
link, a published site's server, a bot. The agent puts a start script into
``<work folder>/.services/<name>.sh``; this keeper (started with AgentDeck — a systemd
unit on a host install, the entrypoint in Docker — independent of any terminal) runs it, writes its output to ``<name>.log``, restarts it after a crash (with a
growing pause), stops it — with all its child processes — when the file is removed or
renamed to ``<name>.sh.off``. At most ``max_services`` (env SERVICES_MAX, set by
guestctl) run at once — a guard against a runaway agent; the sandbox's memory/CPU/process
limits are the real bound. State goes to
``status.json``; a small HTTP API on :9876 (this machine only, unless SERVICES_BIND
says otherwise — a guest sandbox opens it to its host) serves it and the
stop/start/restart actions of the dashboard and the Telegram /services command.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
MAX_DEFAULT = 10
LOG_MAX = 1_000_000          # bytes; then <name>.log -> <name>.log.1


def group_pids(pgid: int) -> List[int]:
    """Live (non-zombie) processes of a process group, from /proc."""
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            rest = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(rest) > 2 and rest[0] != "Z" and int(rest[2]) == pgid:
            out.append(int(entry))
    return out


def start_ticks(pid: int) -> Optional[int]:
    """When the process started (clock ticks since boot): with the pid it names one
    process — a later process that gets the same number has a different start."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


class Keeper:
    def __init__(self, directory: Path, max_services: int = MAX_DEFAULT,
                 backoff=(5, 10, 30, 60), cwd: Optional[Path] = None) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max = max_services
        self.backoff = tuple(backoff) or (5,)
        work = work_folder()
        self.cwd = Path(cwd) if cwd else (work if work.is_dir() else self.dir)
        self._recs: Dict[str, dict] = {}
        self._restart_req: set = set()
        self._lock = threading.RLock()

    # -- files ------------------------------------------------------------- #
    def _scan(self):
        enabled = sorted(p.stem for p in self.dir.glob("*.sh") if NAME_RE.match(p.stem))
        disabled = sorted(p.name[:-len(".sh.off")] for p in self.dir.glob("*.sh.off")
                          if NAME_RE.match(p.name[:-len(".sh.off")]))
        return enabled, [d for d in disabled if d not in enabled]

    # -- processes ---------------------------------------------------------- #
    def _start(self, name: str) -> None:
        rec = self._recs[name]
        log_path = self.dir / f"{name}.log"
        try:
            if log_path.exists() and log_path.stat().st_size > LOG_MAX:
                log_path.replace(self.dir / f"{name}.log.1")
        except OSError:
            pass
        with open(log_path, "ab") as log:
            log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start {name}\n".encode())
            log.flush()
            rec["proc"] = subprocess.Popen(
                ["bash", str(self.dir / f"{name}.sh")], cwd=str(self.cwd),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)       # own process group: stop takes its children
        rec["since"] = time.time()
        rec["start_ticks"] = start_ticks(rec["proc"].pid)

    def _stop(self, name: str) -> None:
        rec = self._recs.get(name)
        proc = rec and rec.get("proc")
        if not proc:
            return
        pgid = proc.pid
        for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            end = time.time() + wait
            while time.time() < end and (proc.poll() is None or group_pids(pgid)):
                time.sleep(0.05)
            if proc.poll() is not None and not group_pids(pgid):
                break
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        rec["proc"] = None

    # -- the loop ----------------------------------------------------------- #
    def tick(self) -> None:
        now = time.time()
        with self._lock:
            enabled, disabled = self._scan()
            allowed = set(enabled[: self.max])
            for name in list(self._recs):
                if name not in allowed:
                    self._stop(name)
                    if name not in enabled and name not in disabled:
                        del self._recs[name]
            for name in enabled[: self.max]:
                rec = self._recs.setdefault(name, {"proc": None, "restarts": 0, "crashes": 0,
                                                   "last_exit": None, "next_start": 0.0,
                                                   "since": None})
                if name in self._restart_req:
                    self._restart_req.discard(name)
                    self._stop(name)
                    rec["next_start"] = 0.0
                proc = rec["proc"]
                if proc is not None and proc.poll() is None:
                    continue
                if proc is not None:                              # it exited
                    rec["last_exit"] = proc.returncode
                    rec["proc"] = None
                    pause = self.backoff[min(rec["crashes"], len(self.backoff) - 1)]
                    rec["crashes"] += 1
                    rec["next_start"] = now + pause
                    rec["restart_due"] = True
                if now >= rec["next_start"]:
                    if rec.pop("restart_due", False):
                        rec["restarts"] += 1
                    self._start(name)
            self._write_status(enabled, disabled)

    def status(self) -> Dict[str, dict]:
        with self._lock:
            enabled, disabled = self._scan()
            out = {}
            for i, name in enumerate(enabled):
                rec = self._recs.get(name, {})
                proc = rec.get("proc")
                if i >= self.max:
                    state = "skipped"
                elif proc is not None and proc.poll() is None:
                    state = "running"
                else:
                    state = "waiting"
                out[name] = {"state": state, "pid": proc.pid if proc else None,
                             "start_ticks": rec.get("start_ticks") if proc else None,
                             "since": rec.get("since"), "restarts": rec.get("restarts", 0),
                             "last_exit": rec.get("last_exit"), "log": f"{name}.log"}
                if state == "skipped":
                    out[name]["limit"] = self.max
            for name in disabled:
                out[name] = {"state": "stopped", "pid": None, "since": None,
                             "restarts": 0, "last_exit": None, "log": f"{name}.log"}
            return out

    def _write_status(self, enabled, disabled) -> None:
        data = {"updated": time.time(), "max": self.max, "services": self.status()}
        tmp = self.dir / "status.json.tmp"
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.dir / "status.json")

    # -- actions ------------------------------------------------------------ #
    def restart(self, name: str) -> bool:
        if not NAME_RE.match(name) or not (self.dir / f"{name}.sh").exists():
            return False
        with self._lock:
            self._restart_req.add(name)
        return True

    def disable(self, name: str) -> bool:
        src = self.dir / f"{name}.sh"
        if not NAME_RE.match(name) or not src.exists():
            return False
        src.replace(self.dir / f"{name}.sh.off")
        return True

    def enable(self, name: str) -> bool:
        src = self.dir / f"{name}.sh.off"
        if not NAME_RE.match(name) or not src.exists():
            return False
        src.replace(self.dir / f"{name}.sh")
        with self._lock:
            if name in self._recs:
                self._recs[name].update(next_start=0.0, crashes=0)
        return True

    def stop_all(self) -> None:
        with self._lock:
            for name in list(self._recs):
                self._stop(name)

    def clean_leftovers(self) -> None:
        """Stop services a previous keeper left running (so nothing runs twice).
        A recorded pid counts only if the process with that number started at the
        recorded moment and still leads its own group — after a container restart the
        number may belong to a stranger."""
        try:
            old = json.loads((self.dir / "status.json").read_text()).get("services", {})
        except (OSError, ValueError, AttributeError):
            return
        for name, st in old.items():
            if not isinstance(st, dict):
                continue
            pid, ticks = st.get("pid"), st.get("start_ticks")
            if not isinstance(pid, int) or not isinstance(ticks, int) or pid <= 1:
                continue
            try:
                if start_ticks(pid) != ticks or os.getpgid(pid) != pid:
                    continue
            except (OSError, ProcessLookupError):
                continue
            for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
                try:
                    os.killpg(pid, sig)
                except ProcessLookupError:
                    break
                end = time.time() + wait
                while time.time() < end and group_pids(pid):
                    time.sleep(0.05)
                if not group_pids(pid):
                    break


# ── the Telegram view (/services in the bridge) and a client for this machine ─
ACTIONS = ("restart", "stop", "start")


def _ago(seconds: float) -> str:
    minutes = int(max(0, seconds) // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{hours // 24} d"


def telegram_view(services: Optional[Dict], now: Optional[float] = None):
    """(text, rows of (label, callback_data)) for the /services message."""
    now = time.time() if now is None else now
    refresh = [("🔄 Refresh", "svc:refresh:")]
    if services is None:
        return ("The services keeper isn't running, so the list is unavailable. It starts "
                "with AgentDeck: the agentdeck-services unit on a server, the entrypoint in "
                "Docker.", [refresh])
    if not services:
        return ("No services yet.\n\nA service is a program that keeps running without an "
                "open terminal: a tunnel, a site's server, a bot. Ask the agent: \"make this "
                "a service\".", [refresh])
    running = sum(1 for st in services.values() if st.get("state") == "running")
    lines, rows = [f"Services (running {running} of {len(services)}):", ""], []
    for name, st in sorted(services.items()):
        state, restarts = st.get("state"), st.get("restarts") or 0
        tail = f", restarts: {restarts}" if restarts else ""
        if state == "running":
            since = st.get("since")
            lines.append(f"🟢 {name} — running" + (f" {_ago(now - since)}" if since else "") + tail)
        elif state == "waiting":
            code = st.get("last_exit")
            why = (f"crashed (exit {code}), restarting soon" if code is not None
                   else "starting")
            lines.append(f"🟠 {name} — {why}{tail}")
        elif state == "skipped":
            lines.append(f"⚪ {name} — not started: at most {st.get('limit') or MAX_DEFAULT} services")
        else:
            lines.append(f"⚪ {name} — stopped")
        if state == "stopped":
            rows.append([(f"▶ {name}", f"svc:start:{name}")])
        elif state != "skipped":
            rows.append([(f"🔁 {name}", f"svc:restart:{name}"), (f"⏹ {name}", f"svc:stop:{name}")])
    lines += ["", "Logs: .services/<name>.log"]
    return "\n".join(lines), rows + [refresh]


class Client:
    """The keeper's HTTP API on this machine (SERVICES_URL, else 127.0.0.1:SERVICES_PORT)."""

    def __init__(self, base: Optional[str] = None, timeout: float = 4) -> None:
        self.base = (base or os.environ.get("SERVICES_URL")
                     or f"http://127.0.0.1:{os.environ.get('SERVICES_PORT') or 9876}")
        self.timeout = timeout

    def status(self) -> Optional[Dict]:
        try:
            with urllib.request.urlopen(f"{self.base}/status", timeout=self.timeout) as r:
                data = json.loads(r.read())
        except (OSError, ValueError):
            return None
        services = data.get("services") if isinstance(data, dict) else None
        if not isinstance(services, dict):
            return None
        return {n: st for n, st in services.items()
                if isinstance(st, dict) and NAME_RE.match(str(n))}

    def act(self, action: str, name: str) -> bool:
        if action not in ACTIONS or not NAME_RE.match(name):
            return False
        req = urllib.request.Request(f"{self.base}/{action}/{name}", data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status == 200
        except (OSError, ValueError):
            return False


def make_server(bind: str, port: int, keeper: Keeper) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/status":
                return self._json(200, {"max": keeper.max, "services": keeper.status()})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            if len(parts) != 2:
                return self._json(404, {"error": "not found"})
            action, name = parts[0], urllib.parse.unquote(parts[1])
            fn = {"restart": keeper.restart, "stop": keeper.disable,
                  "start": keeper.enable}.get(action)
            if fn is None or not NAME_RE.match(name) or not fn(name):
                return self._json(404, {"error": "no such service"})
            self._json(200, {"ok": True})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


def work_folder() -> Path:
    """Where terminals start — the same rule as library_cli.WORKDIR: AGENTDECK_WORKDIR,
    else the folder above this AgentDeck checkout (Docker sets /work)."""
    return Path(os.environ.get("AGENTDECK_WORKDIR")
                or Path(__file__).resolve().parent.parent)


def default_dir() -> Path:
    """SERVICES_DIR, else <work folder>/.services."""
    if os.environ.get("SERVICES_DIR"):
        return Path(os.environ["SERVICES_DIR"])
    return work_folder() / ".services"


def default_bind() -> str:
    return os.environ.get("SERVICES_BIND") or "127.0.0.1"


def main() -> int:
    directory = default_dir()
    keeper = Keeper(directory, max_services=int(os.environ.get("SERVICES_MAX") or MAX_DEFAULT))
    keeper.clean_leftovers()
    server = make_server(default_bind(),
                         int(os.environ.get("SERVICES_PORT", "9876")), keeper)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def bye(*_):
        keeper.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    print(f"keeper: {directory}, max {keeper.max}, http :9876", flush=True)
    while True:
        try:
            keeper.tick()
        except Exception as exc:          # never die: the services depend on us
            print(f"keeper tick error: {exc!r}", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
