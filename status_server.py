#!/usr/bin/env python3
"""Agent status server.

GET  /  — live status + auto-detected project/task
POST /  — update manual overrides  { "id": "1", "project": "...", "task": "..." }
"""
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
import json
import signal
import subprocess
import sys
import threading
import time
import os
import re
import hmac
import hashlib
import secrets
import unicodedata
from urllib.parse import urlparse, parse_qs
from pathlib import Path

import library   # session registry (library.py next to this file)
import convo_sync   # a terminal takes the number of the conversation live in it
import idle_reaper   # background-task detection (tree_has_task_output), pure /proc walk
import library_cli   # claude_processes: which claudes run a conversation (Restart)
import server_status   # the Server page's collector (GET /api/server)
import services_keeper   # the Server page's Services block (GET/POST /api/services)

SESSIONS = [
    {"id": "1",  "session": "claude-terminal",    "path": "terminal"},
    {"id": "2",  "session": "claude-terminal-2",   "path": "terminal2"},
    {"id": "3",  "session": "claude-terminal-3",   "path": "terminal3"},
    {"id": "4",  "session": "claude-terminal-4",   "path": "terminal4"},
    {"id": "5",  "session": "claude-terminal-5",   "path": "terminal5"},
    {"id": "6",  "session": "claude-terminal-6",   "path": "terminal6"},
    {"id": "7",  "session": "claude-terminal-7",   "path": "terminal7"},
    {"id": "8",  "session": "claude-terminal-8",   "path": "terminal8"},
    {"id": "9",  "session": "claude-terminal-9",   "path": "terminal9"},
    {"id": "10", "session": "claude-terminal-10",  "path": "terminal10"},
    {"id": "11", "session": "claude-terminal-11",  "path": "terminal11"},
    {"id": "12", "session": "claude-terminal-12",  "path": "terminal12"},
]

AGENTS_FILE = os.path.join(os.path.dirname(__file__), "agents.json")
SAMPLE_INTERVAL = 0.15
_prev_cpu = None
CPU_TICK_THRESHOLD = 2

# Optional: pretty display names for auto-detected project folders. Any folder
# not listed here simply shows its directory name, so this map is just for looks.
# Map your own "<folder>": "<Display Name>" entries here.
PROJECT_MAP = {
    "my-app": "My App",
    "docs-site": "Docs Site",
    "terminal": "Terminal",
    "orchestra": "Agent Orchestra",
}

ANSI_RE = re.compile(r'\x1b[\[\(][0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b.|\x0f|\x0e')
SEP_PROJECT_RE = re.compile(r'[─━═]{3,}\s+(.+?)\s+[─━═]{3,}')
PATH_PROJECT_RE = re.compile(r'(?:/home/ubuntu/pr|~/pr|/var/www)/([a-zA-Z0-9_-]+)')
SKIP_DIRS = frozenset({
    "tgimg", "tgfiles", "static", "node_modules",
    ".git", ".cache", ".claude", "pr",
})


def get_system_stats():
    global _prev_cpu
    # CPU
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = [int(v) for v in parts]
        idle = vals[3] + vals[4]  # idle + iowait
        total = sum(vals)
        cpu_pct = 0
        if _prev_cpu:
            d_total = total - _prev_cpu[0]
            d_idle = idle - _prev_cpu[1]
            cpu_pct = round((1 - d_idle / d_total) * 100) if d_total else 0
        _prev_cpu = (total, idle)
    except Exception:
        cpu_pct = 0
    # RAM
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                mem[k.strip()] = int(v.split()[0])
        total_mb = mem["MemTotal"] // 1024
        avail_mb = mem.get("MemAvailable", mem["MemFree"]) // 1024
        used_mb = total_mb - avail_mb
        ram_pct = round(used_mb / total_mb * 100) if total_mb else 0
    except Exception:
        total_mb = used_mb = ram_pct = 0
    return {"cpu_pct": cpu_pct, "ram_used_mb": used_mb, "ram_total_mb": total_mb, "ram_pct": ram_pct}


def strip_ansi(s):
    return ANSI_RE.sub("", s)


def is_junk(line):
    """Return True if line is UI chrome, not meaningful content."""
    if not line:
        return True
    # Pure box-drawing / whitespace
    if re.match(r'^[\s─━═╭╮╰╯│┃┌┐└┘├┤┬┴┼╱╲░▒▓▘▝▖▗▀▄█▌▐]+$', line):
        return True
    # Claude Code prompt / status bar
    junk_markers = [
        "bypass permissions", "shift+tab", "esc to interrupt",
        "ctrl+t", "ctrl+c", "ctrl+r", "\u276f",
    ]
    low = line.lower()
    for m in junk_markers:
        if m in low:
            return True
    # Just a prompt char
    if line in (">", "$", "%", "\u276f", "❯"):
        return True
    # Rating prompt
    if re.match(r'^\d+:\s*\w+\s+\d+:', line):
        return True
    # Path-only line like ~/pr
    if re.match(r'^~[/\w]*$', line.strip()):
        return True
    return False


# Claude Code's own notices for the person at the keyboard — never the agent's
# reply. A hook's output is a block: a head line ("● Ran 1 stop hook",
# "⎿  Stop hook error: …") and its wrapped/indented text, up to the next
# bullet, unindented line, spinner/timing line or tool summary.
_HOOK_HEAD_RE = re.compile(
    r"^(?:●\s*)?Ran \d+ [\w:-]+ hooks?$"
    r"|^(?:⎿\s*)?[\w:-]+ hook (?:error|feedback|blocking error|output)\b",
    re.IGNORECASE)
_UPDATE_RE = re.compile(
    r"^[✔✓✗✘]?\s*(?:Update installed\b|Auto-update failed\b)"
    r"|^Updated to latest\. Got \d+"
    r"|^code\.claude\.com/docs/\S*changelog for details$",
    re.IGNORECASE)
_BLOCK_END_RE = re.compile(
    r"^(?:Ran|Read|Wrote|Edited|Updated|Searched|Listed|Did|Fetched|Called|Created|"
    r"Running|Reading|Searching|Writing|Editing)\s+\d")
_SPINNER_CHARS = "✻✶✳✽✺✷✸✹✼❋✢❂⊹∗*·"


def drop_program_notices(lines):
    """`lines` (ANSI-stripped, indentation kept) without hook output blocks and
    the program's update notices. A dropped block leaves one blank line."""
    out, in_hook = [], False
    for line in lines:
        s = line.strip()
        if _HOOK_HEAD_RE.search(s):
            in_hook = True
            continue
        if in_hook:
            if not s or (line[:1] in (" ", "\t") and s[0] not in "●" + _SPINNER_CHARS
                         and not _BLOCK_END_RE.match(s)):
                continue
            in_hook = False
            if out and out[-1].strip():
                out.append("")
        if _UPDATE_RE.search(s):
            continue
        out.append(line)
    return out


def load_agents():
    try:
        with open(AGENTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_agents(data):
    with open(AGENTS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def reset_agent(agent_id, session_name):
    """Unload an agent from RAM by killing its tmux session.

    Both the Claude conversation JSONL and the agent's overrides entry
    (project / task / locked) are intentionally left untouched, so that
    when the user re-adds the agent via "+ Claude":
      - launch-claude-*.sh resumes the same Claude session id
      - the agent card immediately shows the same name (instead of blank)

    Idempotent: missing tmux session is fine.
    """
    out = {"tmux_killed": False}

    # `=name`: exactly this session. A bare name is a prefix to tmux when no session
    # has it exactly — and slot #1's `claude-terminal` is the prefix of every slot.
    r = subprocess.run(
        ["tmux", "kill-session", "-t", "=" + session_name],
        capture_output=True, text=True,
    )
    out["tmux_killed"] = r.returncode == 0

    return out


def get_pane_pid(session):
    r = subprocess.run(
        ["tmux", "list-panes", "-t", f"={session}:", "-F", "#{pane_pid}"],   # exact
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return int(r.stdout.strip().split("\n")[0])


def get_child_pids(pid):
    r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return [int(p) for p in r.stdout.strip().split("\n") if p.strip()]


def read_cpu_ticks(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split(")")
            fields = parts[-1].strip().split()
            return int(fields[11]) + int(fields[12])
    except (FileNotFoundError, PermissionError, IndexError, ValueError):
        return 0


def child_map():
    """{ppid: [pid, ...]} for every process, from one pass over /proc/*/stat —
    instead of spawning `pgrep -P` once per process."""
    kids = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        kids.setdefault(ppid, []).append(int(name))
    return kids


def tree_cpu_ticks(pane_pid, children=None):
    """CPU ticks of a pane's process, its children and grandchildren — the
    quantity whose growth over SAMPLE_INTERVAL makes a terminal "working".
    `children`: a child_map() to use instead of pgrep per process."""
    kids = (lambda p: children.get(p, [])) if children is not None else get_child_pids
    ticks = read_cpu_ticks(pane_pid)
    for cpid in kids(pane_pid):
        ticks += read_cpu_ticks(cpid)
        for gpid in kids(cpid):
            ticks += read_cpu_ticks(gpid)
    return ticks


def is_working(ticks1, ticks2):
    return ticks2 - ticks1 > CPU_TICK_THRESHOLD


def sample_working(pane_pids):
    """{key: pane_pid} -> {key: working}, one shared SAMPLE_INTERVAL sleep —
    the same measure the terminal cards use (GET /)."""
    pids = {k: p for k, p in pane_pids.items() if p}
    if not pids:
        return {}
    kids = child_map()
    first = {k: tree_cpu_ticks(p, kids) for k, p in pids.items()}
    time.sleep(SAMPLE_INTERVAL)
    kids = child_map()
    return {k: is_working(t1, tree_cpu_ticks(pids[k], kids)) for k, t1 in first.items()}


def get_cwd(session):
    # list-panes, not display-message: in tmux 3.2a `display-message -p` on a
    # target that has just vanished segfaults the whole server (2026-09-24).
    r = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}"],
        capture_output=True, text=True,
    )
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        name, _, path = line.partition("\t")
        if name == session:
            return path
    return ""


def detect_project_from_cwd(cwd):
    if not cwd:
        return ""
    parts = cwd.rstrip("/").split("/")
    for p in reversed(parts):
        if p.lower() in PROJECT_MAP:
            return PROJECT_MAP[p.lower()]
    for p in reversed(parts):
        if p and p not in ("pr", "home", "ubuntu", ""):
            return p
    return ""


def detect_project_from_pane(text):
    """Find the project by weighting recent mentions more heavily."""
    matches = [m for m in PATH_PROJECT_RE.findall(text) if m not in SKIP_DIRS]
    if not matches:
        return ""
    # Weight recent mentions: last 1/4 of text counts 4x
    total = len(text)
    cutoff = total * 3 // 4
    recent_text = text[cutoff:]
    recent_matches = [m for m in PATH_PROJECT_RE.findall(recent_text) if m not in SKIP_DIRS]
    from collections import Counter
    counts = Counter(matches)
    # Boost recent mentions
    for m in recent_matches:
        counts[m] += 3
    top = counts.most_common(1)[0][0]
    return PROJECT_MAP.get(top.lower(), top)


def parse_pane(session):
    """Capture pane content, extract project name and last meaningful activity."""
    # Deep capture for project detection, shallow for task
    r_deep = subprocess.run(
        ["tmux", "capture-pane", "-t", f"={session}:", "-p", "-S", "-300"],   # exact
        capture_output=True, text=True,
    )
    if r_deep.returncode != 0:
        return "", ""

    full_text = strip_ansi(r_deep.stdout)
    lines = full_text.split("\n")

    # 1) Project: most-mentioned path in scrollback
    project = detect_project_from_pane(full_text)
    task = ""

    # 2) Fallback: separator line  ──── name ────
    if not project:
        for line in lines:
            clean = line.strip()
            if not clean:
                continue
            dash_count = sum(1 for c in clean if c in "─━═")
            if dash_count < len(clean) * 0.6:
                continue
            m = SEP_PROJECT_RE.search(clean)
            if m:
                name = m.group(1).strip()
                low = name.lower().replace(" ", "-")
                project = PROJECT_MAP.get(low, name)

    # Find last user prompt (line starting with ❯)
    for line in reversed(lines):
        clean = strip_ansi(line).strip()
        if not clean:
            continue
        # Match prompt line: ❯ <user text>
        if clean.startswith("\u276f") or clean.startswith("❯"):
            prompt_text = clean.lstrip("❯\u276f ").strip()
            if prompt_text and len(prompt_text) > 1:
                if len(prompt_text) > 100:
                    prompt_text = prompt_text[:97] + "..."
                task = prompt_text
                break

    return project, task


# ── Telegram bridge: configure + start/stop from the dashboard ────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
TG_CONF = os.path.join(_HERE, ".sessions", "telegram.json")
TG_PID = os.path.join(_HERE, ".sessions", "tg_bridge.pid")
TG_SCRIPT = os.path.join(_HERE, "tg_bridge.py")


def tg_load():
    try:
        return json.load(open(TG_CONF))
    except (OSError, ValueError):
        return {}


def tg_save(cfg):
    os.makedirs(os.path.dirname(TG_CONF), exist_ok=True)
    old = os.umask(0o077)
    try:
        json.dump(cfg, open(TG_CONF, "w"))
    finally:
        os.umask(old)


TG_SYSTEMD_UNIT = os.environ.get("TG_SYSTEMD_UNIT", "claude-tg-bridge")


def tg_systemd_active():
    """The bridge runs as a systemd user service on this server (not started by
    this page): then the page must neither report it stopped nor start a second
    one on the same token (two pollers fight over Telegram updates)."""
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", TG_SYSTEMD_UNIT],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def tg_running():
    try:
        pid = int(open(TG_PID).read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return 0


def tg_stop():
    pid = tg_running()
    if pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    try:
        os.remove(TG_PID)
    except OSError:
        pass


def tg_start(token, owner):
    tg_stop()
    os.makedirs("/work/tg-uploads", exist_ok=True)
    env = {**os.environ, "TG_BRIDGE_TOKEN": token, "TG_BRIDGE_OWNER": str(owner),
           "TG_FILES_DIR": "/work/tg-uploads", "TG_FILES_URL": "",
           "TG_AGENT_CWD": os.environ.get("AGENTDECK_WORKDIR", "/work")}
    try:
        p = subprocess.Popen(["python3", TG_SCRIPT], env=env, cwd=_HERE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        open(TG_PID, "w").write(str(p.pid))
        return True
    except Exception:
        return False


def tg_autostart():
    """Called at startup: if the bridge was configured + enabled, bring it back up."""
    cfg = tg_load()
    if tg_systemd_active():
        return
    if cfg.get("enabled") and cfg.get("token") and cfg.get("owner") and not tg_running():
        tg_start(cfg["token"], cfg["owner"])


def tg_action(action, token, owner):
    """Start/stop from the setup page; refused when systemd owns the bridge."""
    if tg_systemd_active():
        return (f"The bot is managed by the systemd service {TG_SYSTEMD_UNIT} on this "
                "server — nothing changed here.")
    if action == "stop":
        tg_stop()
        cfg = tg_load(); cfg["enabled"] = False; tg_save(cfg)
        return "Bot stopped."
    if not token or not owner:
        return "Enter both the bot token and your Telegram user id."
    tg_save({"token": token, "owner": owner, "enabled": True})
    return ("Bot started — open Telegram and message it."
            if tg_start(token, owner)
            else "Could not start the bot — check the container logs.")


def tg_render(msg=""):
    # built lazily so _AUTH_HEAD (defined further down) is resolved at request time
    page = (_AUTH_HEAD.replace("min(92vw,340px)", "min(92vw,420px)") +
      '<form class="card" method="POST" action="/telegram">'
      '<h1>📲 Telegram bot</h1>'
      '<p class="sub">Drive your agents from Telegram. __STATUS__</p>'
      '<label for="t">Bot token <span style="color:#6e7681">(from @BotFather)</span></label>'
      '<input id="t" name="token" value="__TOKEN__" placeholder="123456:ABC-DEF...">'
      '<label for="o">Your Telegram user id <span style="color:#6e7681">(from @userinfobot)</span></label>'
      '<input id="o" name="owner" value="__OWNER__" placeholder="123456789" inputmode="numeric">'
      '<button type="submit" name="action" value="start">Save &amp; start</button>'
      '<button type="submit" name="action" value="stop" style="margin-top:8px;background:#30363d">Stop</button>'
      '__MSG__'
      '<p style="text-align:center;margin-top:14px"><a href="/">&larr; back to dashboard</a></p>'
      '</form></body></html>')
    cfg = tg_load()
    if tg_systemd_active():
        # managed outside this page: no Save&start / Stop (a second bridge on the
        # same token would fight the live one for updates)
        page = page.replace(
            '<button type="submit" name="action" value="start">Save &amp; start</button>', ''
        ).replace(
            '<button type="submit" name="action" value="stop" style="margin-top:8px;background:#30363d">Stop</button>',
            f'<p class="sub">Managed by the systemd service <code>{TG_SYSTEMD_UNIT}</code> on this '
            'server — start/stop it there, not here.</p>')
        status = '<b style="color:#3fb950">running</b> (systemd)'
    else:
        status = ('<b style="color:#3fb950">running</b>' if tg_running()
                  else '<b style="color:#8b949e">stopped</b>')
    return (page.replace("__STATUS__", "Status: " + status)
            .replace("__TOKEN__", (cfg.get("token") or "").replace('"', ""))
            .replace("__OWNER__", str(cfg.get("owner") or ""))
            .replace("__MSG__", f'<p class="ok">{msg}</p>' if msg else ''))


# ── Session library API: /api/library (see library.py) ──────────────────────
# Topics live in the registry; the loaded ones are tmux sessions cs-<id>. All tmux
# calls here go to AGENTDECK_TMUX_SOCKET when set (`tmux -L <name>` — tests use a
# private socket), and /close targets exactly "=cs-<id>" after the id is validated
# and found in the registry, so nothing else can ever be killed from here.
LIB_ROUTE = "/api/library"
LIB_NAME_MAX = 200
LIB_BODY_MAX = 64 * 1024
# /close unloads only a topic nobody is using: not attached and no screen output
# for this long — anything else needs {"force": true}
CLOSE_QUIET_SECONDS = 30 * 60
# CSRF: POSTs must be JSON (a cross-site <form> cannot send that without a CORS
# preflight, and we answer no preflight) and, when the browser says where the
# request comes from, come from the dashboard itself.
# The dashboard's origin: $AGENTDECK_ORIGIN when set (e.g. https://agents.example.com —
# needed behind a proxy that rewrites Host or doesn't pass X-Forwarded-Proto, like the
# nginx config in nginx/); otherwise the request's own scheme + Host, as the proxy
# forwards them (Caddy keeps Host and sets X-Forwarded-Proto). No Host -> refused.


def lib_allowed_origin(headers=None):
    """The one Origin library writes are accepted from ('' = none acceptable)."""
    configured = os.environ.get("AGENTDECK_ORIGIN")
    if configured:
        return configured
    headers = headers or {}
    host = (headers.get("Host") or "").strip()
    proto = (headers.get("X-Forwarded-Proto") or "http").strip().lower()
    if not host or proto not in ("http", "https") or not re.fullmatch(r"[A-Za-z0-9.:\[\]-]+", host):
        return ""
    return f"{proto}://{host}"


def lib_origin_ok(origin, headers):
    allowed = lib_allowed_origin(headers)
    return bool(allowed) and origin == allowed
_LIB_ROW_KEYS = ("id", "name", "cwd", "created", "last_used", "archived", "pos")
# only on a terminal whose conversation changed under it (convo_sync / library.switch):
# the numbers that now lead here, the number it had before and when it changed
_LIB_SWITCH_KEYS = ("aliases", "prev_id", "switched_at")


class LibError(Exception):
    def __init__(self, code, msg, **extra):
        super().__init__(msg)
        self.code = code
        self.extra = extra          # more fields of the error reply (e.g. reason)


def lib_path():
    return os.environ.get("AGENTDECK_LIBRARY") or library.LIB_FILE


def lib_workdir():
    """Where a new topic starts: $AGENTDECK_WORKDIR, else the folder above this
    repo — the same default the launch-claude*.sh scripts use."""
    return os.environ.get("AGENTDECK_WORKDIR") or os.path.dirname(_HERE)


def lib_tmux(*args):
    sock = os.environ.get("AGENTDECK_TMUX_SOCKET")
    return subprocess.run(["tmux", *(["-L", sock] if sock else []), *args],
                          capture_output=True, text=True)


def lib_sync(lock="try"):
    """One number per terminal: a terminal whose Claude now runs another conversation
    (consent relaunch, /clear, /resume) takes that conversation's number before
    anything reads or acts on it (convo_sync.sync). lock="try": the page's poll
    skips a round while an `ensure` runs; True: wait (before an unload). Never
    raises: a failure is logged and the caller goes on with what is there."""
    try:
        return convo_sync.sync(run=lib_tmux, lib_file=lib_path(), lock=lock)
    except Exception as e:  # noqa: BLE001 — must never break the listing or a POST
        convo_sync.log(f"sync failed: {str(e)[:200]}")
        return []


def lib_canonical(sid):
    """The current number of terminal `sid`: after syncing, an old number that
    became an alias (the consent relaunch) maps to the terminal that has it."""
    lib_sync(lock=True)
    e = library.find_or_alias(library.load(lib_path()), sid)
    return e["id"] if e is not None else sid


def lib_live():
    """{id: {"attached": bool, "pane_pid": int|None}} for every cs-<id> session.
    One tmux call; no running tmux server simply means nothing is loaded."""
    r = lib_tmux("list-panes", "-a", "-F",
                 "#{session_name}\t#{session_attached}\t#{pane_pid}")
    live = {}
    if r.returncode != 0:
        return live
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        sid = library.id_from_tmux(parts[0])
        if sid is None or sid in live:          # not a library session / 2nd pane
            continue
        live[sid] = {"attached": parts[1] not in ("", "0"),
                     "pane_pid": int(parts[2]) if parts[2].isdigit() else None}
    return live


_LEGACY_TMUX = re.compile(r"claude-terminal(?:-(\d+))?")
_RESUME_UUID = re.compile(rb"--(?:resume|session-id)\x00?\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _uuids_under(pid, depth=4, children=None):
    """uuids after --resume/--session-id in the cmdline of pid and its descendants."""
    children = child_map() if children is None else children
    found, stack = set(), [(pid, 0)]
    while stack:
        p, d = stack.pop()
        try:
            with open(f"/proc/{p}/cmdline", "rb") as f:
                found.update(m.decode() for m in _RESUME_UUID.findall(f.read()))
        except OSError:
            continue
        if d < depth:
            stack.extend((c, d + 1) for c in children.get(p, []))
    return found


def lib_legacy():
    """{uuid: "/terminalN/"} for Claude conversations still running in the old
    numbered terminals (migration leaves busy ones untouched until they unload)."""
    r = lib_tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}")
    out, children = {}, None
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        name, _, pid = line.partition("\t")
        m = _LEGACY_TMUX.fullmatch(name)
        if not m or not pid.isdigit():
            continue
        path = f"/terminal{m.group(1) or ''}/"
        children = child_map() if children is None else children
        for u in _uuids_under(int(pid), children=children):
            out.setdefault(u, path)
    return out


def lib_last_output(name):
    """Latest #{window_activity} (moves on pane output) of session `name`, by
    exact name; None when unknown. list-windows + filtering, never
    display-message (segfaults tmux 3.2a)."""
    r = lib_tmux("list-windows", "-a", "-F", "#{session_name}\t#{window_activity}")
    last = None
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        n, _, act = line.rpartition("\t")
        if n == name and act.isdigit():
            last = max(last or 0, int(act))
    return last


def lib_status(active, working):
    """Same words as the terminal cards: off / idle / working."""
    return "off" if not active else ("working" if working else "idle")


def lib_shell_live():
    """The dashboard's command line (tmux cmd-shell, exact name):
    {"attached": bool, "pane_pid": int|None}, or None when it is not running."""
    r = lib_tmux("list-panes", "-a", "-F",
                 "#{session_name}\t#{session_attached}\t#{pane_pid}")
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == library.SHELL_TMUX:
            return {"attached": parts[1] not in ("", "0"),
                    "pane_pid": int(parts[2]) if parts[2].isdigit() else None}
    return None


_SHELL_KEY = "\0shell"        # never a valid id: cannot collide with a topic's key


def lib_rows(entries, live=None, legacy_paths=None, working=None):
    """Registry entries -> API rows with tmux state (samples CPU once for all).
    live / legacy_paths / working: pass what the caller already has (one scan
    and one CPU sample per request)."""
    live = lib_live() if live is None else live
    if working is None:
        working = sample_working({e["id"]: live[e["id"]]["pane_pid"]
                                  for e in entries if e["id"] in live})
    legacy_paths = lib_legacy() if legacy_paths is None else legacy_paths
    rows = []
    for e in entries:
        row = {k: e.get(k) for k in _LIB_ROW_KEYS}
        row.update({k: e[k] for k in _LIB_SWITCH_KEYS if e.get(k) is not None})
        if library.is_trashed(e):
            row["trashed"] = e["trashed"]           # only in the trash listing (?trash=1)
        row["archived"] = bool(e.get("archived"))
        info = live.get(e["id"])
        legacy = legacy_paths.get(e.get("uuid")) if info is None else None
        row["legacy_path"] = legacy
        row["active"] = info is not None or legacy is not None
        row["attached"] = bool(info and info["attached"])
        row["status"] = lib_status(info is not None, working.get(e["id"], False))
        rows.append(row)
    return rows


def lib_listing(include_archived=False, trash=False):
    """The list (never a trashed terminal); trash=True: the trash itself instead."""
    lib_sync()                                      # the page polls this every 4 s
    lib = library.load(lib_path())
    live = lib_live()
    legacy = lib_legacy()
    active = set(live) | {e["id"] for e in lib["sessions"] if e.get("uuid") in legacy}
    entries = library.display_order(lib, active, include_archived=include_archived,
                                    trashed=trash)
    shell = lib_shell_live()
    pids = {e["id"]: live[e["id"]]["pane_pid"] for e in entries if e["id"] in live}
    if shell is not None:
        pids[_SHELL_KEY] = shell["pane_pid"]
    working = sample_working(pids)                  # one CPU sample for all rows
    return {"max_active": library.MAX_ACTIVE,
            "sessions": lib_rows(entries, live, legacy, working),
            # the "cmd" button's shell: not a topic, so not in sessions[]
            "shell": {"active": shell is not None,
                      "attached": bool(shell and shell["attached"]),
                      "status": lib_status(shell is not None, working.get(_SHELL_KEY, False))},
            "_system": get_system_stats()}      # CPU/RAM for the top bar, same as GET /


def lib_clean_name(v, required=False):
    """Display name: whitespace runs (newlines too) -> one space, control chars
    dropped (a name ends up in tmux/Telegram/logs), at most LIB_NAME_MAX chars."""
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise LibError(400, "name must be a string")
    v = "".join(ch for ch in " ".join(v.split()) if unicodedata.category(ch) != "Cc")
    if len(v) > LIB_NAME_MAX:
        raise LibError(400, f"name longer than {LIB_NAME_MAX} characters")
    if required and not v:
        raise LibError(400, "name is empty")
    return v


def lib_id(body):
    sid = body.get("id")
    if not library.valid_id(sid):
        raise LibError(400, "invalid id")
    return sid


def lib_edit(sid, fn):
    """Locked read-modify-write of one entry; unknown id -> 404, nothing saved."""
    try:
        with library.update(lib_path()) as lib:
            e = fn(lib)
    except KeyError:
        raise LibError(404, "unknown id")
    return lib_rows([e])[0]


def lib_post(route, body):
    if route == "new":
        name = lib_clean_name(body.get("name"))
        with library.update(lib_path()) as lib:
            e = library.create(lib, name, cwd=lib_workdir(), now=int(time.time()))
        return lib_rows([e], live={})[0]          # not started: nothing to ask tmux
    if route == "reorder":
        return lib_reorder(body.get("ids"))
    if route == "shell-close":
        # the "cmd" command line: exact name, configured socket; not running = ok
        killed = lib_tmux("kill-session", "-t", "=" + library.SHELL_TMUX).returncode == 0
        return {"ok": True, "killed": killed}

    if route == "empty-trash":
        return lib_empty_trash(body.get("ids"))

    sid = lib_id(body)
    if route in ("rename", "archive", "close", "restart", "restart-check",
                 "trash", "trash-check", "untrash"):
        sid = lib_canonical(sid)                  # act on the terminal's live number
    if route == "rename":
        name = lib_clean_name(body.get("name"), required=True)
        return lib_edit(sid, lambda lib: library.rename(lib, sid, name))
    if route == "archive":
        archived = body.get("archived", True)
        if not isinstance(archived, bool):
            raise LibError(400, "archived must be true or false")
        return lib_archive(sid, archived)
    if route == "close":
        force = body.get("force", False)
        if not isinstance(force, bool):
            raise LibError(400, "force must be true or false")
        e = library.find(library.load(lib_path()), sid)
        if e is None:
            raise LibError(404, "unknown id")
        name = library.tmux_name(sid)
        info = lib_live().get(sid)
        if info is not None and not force:
            if info["attached"]:
                raise LibError(409, "the topic is open in a browser/terminal right now; "
                                    "send force=true to unload it anyway")
            last = lib_last_output(name)
            if last is None or time.time() - last < CLOSE_QUIET_SECONDS:
                raise LibError(409, f"the topic printed output in the last "
                                    f"{CLOSE_QUIET_SECONDS // 60} min (it may be working); "
                                    "send force=true to unload it anyway")
        killed = lib_tmux("kill-session", "-t", "=" + name).returncode == 0
        row = lib_rows([e])[0]
        row["killed"] = killed
        return row
    if route == "restart":
        force = body.get("force", False)
        if not isinstance(force, bool):
            raise LibError(400, "force must be true or false")
        return lib_restart(sid, force)
    if route == "restart-check":
        # its own route, not a flag on "restart": a server older than the page
        # answers 404 here instead of taking the question for a restart
        return lib_restart(sid, False, check=True)
    if route in ("trash", "trash-check"):
        force = body.get("force", False)
        if not isinstance(force, bool):
            raise LibError(400, "force must be true or false")
        return lib_trash(sid, force, check=route == "trash-check")
    if route == "untrash":
        return lib_edit(sid, lambda lib: library.untrash(lib, sid))
    raise LibError(404, "not found")


LIB_REORDER_MAX = 10000
DELETE_QUIET_SECONDS = 120     # 2 min of quiet: an archived terminal's (idle_reaper's window)
ARCHIVE_BUSY_SECONDS = 15      # output this recent = working: Archive leaves it loaded


def lib_reorder(ids):
    """Manual order from the page: the full visible order of ids -> pos 0..n-1."""
    if not isinstance(ids, list) or not ids or len(ids) > LIB_REORDER_MAX:
        raise LibError(400, "ids must be a non-empty list")
    if not all(library.valid_id(i) for i in ids):
        raise LibError(400, "invalid id")
    if len(set(ids)) != len(ids):
        raise LibError(400, "duplicate id")
    try:
        with library.update(lib_path()) as lib:
            library.reorder(lib, ids)
    except KeyError as e:
        raise LibError(400, f"unknown id {e.args[0]}")
    return {"ok": True}


def lib_busy_reason(sid, info, working):
    """Why a loaded terminal must not be unloaded right now, or None when it
    may go. The owner's top rule is "never interrupt work": printing in the
    last ARCHIVE_BUSY_SECONDS (unknown output time counts as printing), an
    unexpired hold marker (a timer is pending), a live background task, or the
    cards' CPU status 'working'. An open tab is NOT a reason."""
    now = time.time()
    last = lib_last_output(library.tmux_name(sid))
    if last is None or now - last < ARCHIVE_BUSY_SECONDS:
        return "printing"
    if library.held(sid, now, lib_file=lib_path()):
        return "hold"
    pid = info.get("pane_pid")
    if pid and idle_reaper.tree_has_task_output(pid):
        return "background-task"
    if working:
        return "working"
    return None


def lib_archive(sid, archived):
    """Archive/restore. Archive unloads the terminal right away (owner: "When I
    press Archive, the terminal should unload") — even with a tab open, since
    the owner pressed Archive on purpose — unless it is working
    (lib_busy_reason): then it stays loaded, unload_pending, and idle_reaper
    unloads it once quiet (ARCHIVED_IDLE_SECONDS). The reply says which:
    unloaded true|false + reason (idle | printing | hold | background-task |
    working | not-loaded). A trashed one is refused (409 'trashed', checked under
    the registry lock): a stale row must not change where Restore puts it back."""
    try:
        with library.update(lib_path()) as lib:
            e = dict(library.archive(lib, sid, archived))
    except KeyError:
        raise LibError(404, "unknown id")
    except library.Trashed:
        raise LibError(409, "it is in the trash; restore it first", reason="trashed")
    if not archived:
        return lib_rows([e])[0]
    live = lib_live()
    info = live.get(sid)
    working = {}
    if info is None:
        unloaded, reason = False, "not-loaded"
    else:
        working = sample_working({sid: info["pane_pid"]})
        reason = lib_busy_reason(sid, info, working.get(sid, False))
        unloaded = reason is None
        if unloaded:
            lib_tmux("kill-session", "-t", "=" + library.tmux_name(sid))
            reason = "idle"
            live, working = None, {}
    row = lib_rows([e], live=live, working=working)[0]
    row["unloaded"] = unloaded
    row["reason"] = reason
    row["unload_pending"] = info is not None and not unloaded
    return row


# ── Restart (⟳ on a loaded row): Claude starts again in the same conversation ──
# Why (owner 2026-10-23): launch settings (e.g. CLAUDE_CODE_SCROLL_SPEED in bin/pane)
# take effect only on a fresh start, and a hung Claude needs reviving. The launch
# itself is not changed: the terminal is unloaded and loaded again by `library_cli
# ensure`, exactly what opening it does (bare `claude --resume <uuid>`).
#
# One claude per conversation: the check, the kill and the wait for the old claude
# run under library_cli's ensure lock, so nothing renames or starts a terminal
# meanwhile. An open tab races us — its attach returns when the session dies and
# ttyd runs open-session.sh (ensure) again; that ensure waits for the lock, and
# after it the two ensures take turns and the second finds the session running.
# Either way: one new claude.
# The check under the lock: lib_canonical's sync came before the busy sample, and
# a /clear, /resume or consent relaunch landing in between (with or without another
# syncer renaming the session) would make us restart the conversation it left —
# refused instead (409 "changed"), as is a kill-session that found no session.
# The old claude is waited for by pid + start time, not by "still a claude on u":
# Claude's shutdown removes its pid file first and then flushes for a while, and
# after /clear that file is all that ties it to u. A hung Claude may ignore the
# hangup tmux sends: then SIGTERM, then SIGKILL — only to those exact processes.
RESTART_EXIT_SECONDS = 5       # the old claude's time to exit after the hangup …
RESTART_TERM_SECONDS = 3       # … and after SIGTERM, and after SIGKILL
RESTART_ENSURE_TIMEOUT = 60
LIBRARY_CLI = os.path.join(_HERE, "library_cli.py")
# lib_busy_reason's reasons in plain words (the page says them in its own words)
BUSY_WORDS = {"printing": "it is printing right now", "hold": "a timer is pending",
              "background-task": "a background task is running", "working": "it is working"}
RESTART_CHANGED = "it switched conversations just now; press ⟳ again"


def _claude_pids(u):
    """pids of every claude running conversation u (library_cli's own test)."""
    return {p for p, _ in library_cli.claude_processes(u)}


def _proc_start(pid):
    """Start time of a running pid (/proc/<pid>/stat field 22); None when it is gone
    or a zombie (it has exited, only not reaped yet)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            st = f.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return None if len(st) < 20 or st[0] in ("Z", "X") else st[19]


def _still_running(procs):
    """The ones of {pid: start time} still running (a reused pid is another process)."""
    return {p: t for p, t in procs.items() if _proc_start(p) == t}


def _wait_gone(procs, seconds):
    """Wait until none of {pid: start time} runs any more; -> the ones that still do."""
    end = time.time() + seconds
    while True:
        left = _still_running(procs)
        if not left or time.time() >= end:
            return left
        time.sleep(0.1)


def _elsewhere_error(other):
    pids = ", ".join(str(p) for p, _ in other)
    return LibError(409, f"this conversation also runs in another Claude (pid {pids}); "
                         "close that one first", reason="elsewhere")


def _unload_locked(name, u, changed, stuck):
    """Unload terminal tmux `name` running conversation u; the caller holds the
    ensure lock. Refused (409) when the terminal is gone or runs another
    conversation now (`changed`: the words for that) or when u also runs in
    another claude. Kills the exact session, then waits for its claude by pid +
    start time; one that ignores the hangup gets SIGTERM, then SIGKILL — only those
    exact processes. One that still runs: 500, "<…> did not exit; it is unloaded,
    <stuck>"."""
    if (lib_tmux("has-session", "-t", "=" + name).returncode != 0
            or convo_sync.pane_conversations(lib_tmux).get(name, u) != u):
        raise LibError(409, changed, reason="changed")
    other = library_cli.claude_elsewhere(u, name)
    if other:
        raise _elsewhere_error(other)
    old = {p: t for p in _claude_pids(u)              # all in this terminal (checked above)
           if (t := _proc_start(p)) is not None}
    if lib_tmux("kill-session", "-t", "=" + name).returncode != 0:
        raise LibError(409, changed, reason="changed")
    left = _wait_gone(old, RESTART_EXIT_SECONDS)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not left:
            break
        for p in _still_running(left):            # the very processes, checked again
            try:
                os.kill(p, sig)
            except OSError:                       # gone meanwhile / not ours
                pass
        left = _wait_gone(left, RESTART_TERM_SECONDS)
    if left:
        raise LibError(500, f"the old Claude (pid {', '.join(map(str, sorted(left)))}) did not "
                            f"exit; it is unloaded, {stuck}")


def lib_restart(sid, force, check=False):
    """Unload terminal `sid` and load it again: claude starts anew on the same
    conversation and the terminal comes back loaded even with no tab open.
    Refused (409 + reason) when there is nothing to restart (archived,
    not-loaded), when the conversation also runs in another claude (elsewhere:
    ensure would then refuse to load it back), when it switched conversations
    while we checked (changed), and — unless force — when it is busy
    (lib_busy_reason). An open tab is not a reason: the owner pressed ⟳.
    check=True (POST restart-check) touches nothing and answers {"busy": reason
    | None}, so the page asks ONE question — plain, or red naming the reason
    (owner 2026-10-23)."""
    e = library.find(library.load(lib_path()), sid)
    if e is None:
        raise LibError(404, "unknown id")
    if library.is_trashed(e):
        raise LibError(409, "it is in the trash; restore it first", reason="trashed")
    if e.get("archived"):
        raise LibError(409, "it is archived; restore it first", reason="archived")
    info = lib_live().get(sid)
    if info is None:
        raise LibError(409, "it is not loaded: opening it starts Claude", reason="not-loaded")
    name = library.tmux_name(sid)
    if check:
        try:
            _, u = library_cli.checked_entry(e)
        except ValueError:
            raise LibError(500, "the registry entry is corrupt (uuid)")
        if library_cli.claude_elsewhere(u, name):
            raise LibError(409, "this conversation also runs in another Claude; close that one first",
                           reason="elsewhere")
    if check or not force:
        reason = _busy_now(sid, info)
        if check:
            return {"busy": reason}
        if reason is not None:
            raise LibError(409, f"{BUSY_WORDS[reason]}; send force=true to restart it anyway",
                           reason=reason)
    try:
        _, u = library_cli.checked_entry(e)
    except ValueError:
        raise LibError(500, "the registry entry is corrupt (uuid)")
    with convo_sync._ensure_lock(lib_path(), True):          # library_cli's ensure lock
        _unload_locked(name, u, RESTART_CHANGED, "nothing was started")
    env = dict(os.environ, AGENTDECK_LIBRARY=lib_path())
    try:
        r = subprocess.run([sys.executable, LIBRARY_CLI, "ensure", sid], capture_output=True,
                           text=True, env=env, timeout=RESTART_ENSURE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise LibError(500, "unloaded, but loading it again took too long; open it to retry")
    if r.returncode != 0:
        msg = " ".join((r.stderr or "").split())[:200] or f"library_cli exit {r.returncode}"
        raise LibError(500, f"unloaded, but couldn't load it again: {msg}")
    e = library.find(library.load(lib_path()), sid) or e
    row = lib_rows([e])[0]
    row["restarted"] = True
    return row


def _busy_now(sid, info):
    """lib_busy_reason of a loaded terminal, with one CPU sample of its own."""
    working = sample_working({sid: info["pane_pid"]})
    return lib_busy_reason(sid, info, working.get(sid, False))


# ── Trash (🗑 on every topic row; owner 2026-10-23) ──────────────────────────
# Archive = needed, just not now. Trash = not needed, kept just in case: a trashed
# terminal is in no list (library.display_order), never loaded (library_cli ensure
# refuses it), and nothing erases it by itself. Move to Trash unloads a loaded one
# the way ⟳ does (_unload_locked, under the ensure lock); a busy one only with
# force=true, after the page's red question. Empty trash erases for good, only on
# the owner's word: the entries the page showed that are trashed AND run nowhere.
TRASH_CHANGED = "it switched conversations just now; press 🗑 again"


def lib_trash(sid, force, check=False):
    """Move terminal `sid` to the trash: unload it if loaded, then set `trashed`.
    Refused (409 + reason) while its conversation runs in another claude or in an
    old numbered terminal (elsewhere), when it switched conversations while we
    checked (changed), and — unless force — when it is busy (lib_busy_reason; an
    open tab is not a reason: the owner pressed 🗑). check=True (POST trash-check)
    touches nothing and answers {"busy": reason | None}. Again on a trashed one:
    nothing changes. -> the row, + unloaded: whether it was loaded and now is not."""
    e = library.find(library.load(lib_path()), sid)
    if e is None:
        raise LibError(404, "unknown id")
    if library.is_trashed(e):
        return {"busy": None} if check else dict(lib_rows([e])[0], unloaded=False)
    try:
        _, u = library_cli.checked_entry(e)
    except ValueError:
        raise LibError(500, "the registry entry is corrupt (uuid)")
    name = library.tmux_name(sid)
    legacy = lib_legacy().get(u)
    if legacy:
        raise LibError(409, f"its conversation is running in the old terminal {legacy}; "
                            "stop it there first", reason="elsewhere")
    info = lib_live().get(sid)
    if check:
        other = library_cli.claude_elsewhere(u, name)
        if other:
            raise _elsewhere_error(other)
    if info is not None and (check or not force):
        reason = _busy_now(sid, info)
        if check:
            return {"busy": reason}
        if reason is not None:
            raise LibError(409, f"{BUSY_WORDS[reason]}; send force=true to move it to the "
                                "trash anyway", reason=reason)
    if check:
        return {"busy": None}
    with convo_sync._ensure_lock(lib_path(), True):          # nothing loads it meanwhile
        unloaded = lib_tmux("has-session", "-t", "=" + name).returncode == 0
        if unloaded:
            if info is None:                      # loaded since we looked: not what was asked
                raise LibError(409, TRASH_CHANGED, reason="changed")
            _unload_locked(name, u, TRASH_CHANGED, "it was not moved to the trash")
        else:
            other = library_cli.claude_elsewhere(u, name)
            if other:
                raise _elsewhere_error(other)
        try:
            with library.update(lib_path()) as lib:
                e = dict(library.trash(lib, sid, now=int(time.time())))
        except KeyError:
            raise LibError(404, "unknown id")
    row = lib_rows([e])[0]
    row["unloaded"] = unloaded
    return row


EMPTY_TRASH_REASONS = ("unknown", "not-trashed", "running", "corrupt", "erase-failed",
                       "left-on-disk")


def lib_empty_trash(ids):
    """Erase for good the trashed entries among `ids` (the ones the page showed):
    each one's transcript and its own folder (library.erase_conversation), then its
    registry entry. Skipped, with a reason (EMPTY_TRASH_REASONS): an id not in the
    registry or not in the trash, and one whose conversation runs anywhere — loaded,
    in another claude, in an old numbered terminal. Erased where claude ran it: the
    entry's folder and the one the pane falls back to when that folder is gone
    (library_cli.effective_cwd). Never reported erased while a copy of the
    transcript is still on disk in some other project dir: that one is kept in the
    trash, nothing removed ('left-on-disk'). Under the ensure lock, then the
    registry lock (the order library_cli.ensure and convo_sync take them).
    -> {"erased": [ids], "skipped": [{id, reason}]}"""
    if not isinstance(ids, list) or len(ids) > LIB_REORDER_MAX:
        raise LibError(400, "ids must be a list")
    if not all(library.valid_id(i) for i in ids):
        raise LibError(400, "invalid id")
    erased, skipped = [], []
    if not ids:
        return {"erased": erased, "skipped": skipped}
    legacy = lib_legacy()
    with convo_sync._ensure_lock(lib_path(), True):
        live = lib_live()
        with library.update(lib_path()) as lib:
            for sid in dict.fromkeys(ids):        # each once, in the page's order
                e = library.find(lib, sid)
                if e is None or not library.is_trashed(e):
                    skipped.append({"id": sid, "reason": "unknown" if e is None else "not-trashed"})
                    continue
                try:
                    _, u = library_cli.checked_entry(e)
                except ValueError:
                    skipped.append({"id": sid, "reason": "corrupt"})
                    continue
                if sid in live or u in legacy or library_cli.claude_processes(u):
                    skipped.append({"id": sid, "reason": "running"})
                    continue
                cwds = [e.get("cwd"), library_cli.effective_cwd(e)]
                try:
                    root = os.path.abspath(library.claude_projects_root())
                    own = {os.path.dirname(library.transcript_file(root, c, u))
                           for c in cwds if isinstance(c, str) and c}
                    if any(os.path.dirname(p) not in own for p in library.transcripts_anywhere(u)):
                        convo_sync.log(f"empty trash: {sid} kept: its transcript is in another project dir")
                        skipped.append({"id": sid, "reason": "left-on-disk"})
                        continue
                    library.erase_conversation(e, cwds=cwds)
                    left = library.transcripts_anywhere(u)
                except (OSError, ValueError) as ex:
                    convo_sync.log(f"empty trash: {sid} not erased: {str(ex)[:200]}")
                    skipped.append({"id": sid, "reason": "erase-failed"})
                    continue
                if left:                                  # never "erased" with a copy on disk
                    convo_sync.log(f"empty trash: {sid} kept: {len(left)} transcript(s) left")
                    skipped.append({"id": sid, "reason": "left-on-disk"})
                    continue
                library.delete(lib, sid)
                erased.append(sid)
    return {"erased": erased, "skipped": skipped}


# ── Server page: GET /api/server (see server_status.py) ─────────────────────
# The collector samples /proc every 5 s in a daemon thread (started in __main__);
# a request only reads the latest snapshot. Same-origin only, never cached.
SERVER_ROUTE = "/api/server"
SERVER_COLLECTOR = server_status.Collector()


# ── Server page, Services block: /api/services (2.0) ────────────────────────
# The services keeper (services_keeper.py, its own unit / the Docker entrypoint) owns the
# services; this only relays its /status and actions and reads the log tail from the
# same folder it uses. Same-origin only, like the library.
SVC_ROUTE = "/api/services"
SVC_LOG_LINES = 200


def svc_listing():
    services = services_keeper.Client().status()
    return {"available": services is not None, "services": services or {},
            "dir": str(services_keeper.default_dir()), "now": time.time()}


def svc_log(name):
    if not services_keeper.NAME_RE.match(name):
        raise LibError(400, "bad service name")
    path = services_keeper.default_dir() / f"{name}.log"
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            text = f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return {"name": name, "lines": []}
    return {"name": name, "lines": text.splitlines()[-SVC_LOG_LINES:]}


def svc_post(route):
    parts = route.split("/")
    if len(parts) != 2 or parts[0] not in services_keeper.ACTIONS:
        raise LibError(404, "not found")
    if not services_keeper.NAME_RE.match(parts[1]):
        raise LibError(400, "bad service name")
    if not services_keeper.Client().act(parts[0], parts[1]):
        raise LibError(404, "no such service (or the keeper isn't running)")
    return {"ok": True}


# ── Server page, Guests block: /api/guests (2.0) ────────────────────────────
# The optional guests module (<repo>/guests, GUESTS_DIR overrides) does the work; the
# panel drives its command guests/guestctl.py. Every call runs in a background thread
# (this server answers one request at a time; `remove` alone takes ~90 s): GET returns
# the last list / checks / job results, refreshing the list when it is older than
# GUESTS_LIST_TTL. Its .env holds the bot token: the page only learns whether one is set.
GUESTS_ROUTE = "/api/guests"
GUESTS_LIST_TTL = 10
GUEST_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
GUESTS_ACTIONS = ("stop", "start", "remove", "limit")
# what the wizard sets; OWNER_TG_ID is not asked: the guests' admin is the owner of
# "Your bot" (the dashboard's Telegram page) — "Let in?" goes there (decision 27.10.2026)
_GUESTS_ENV_KEYS = {"bot_token": "GUEST_BOT_TOKEN", "domain": "GUESTS_DOMAIN"}
_GUESTS_NEEDED = ("GUEST_BOT_TOKEN", "OWNER_TG_ID", "GUESTS_DOMAIN")
_GUESTS_VALID = {
    "bot_token": re.compile(r"^[0-9]{5,15}:[A-Za-z0-9_-]{30,60}$"),
    "domain": re.compile(r"^(?=.{4,200}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"),
}


GUESTS_DEFAULT_DIR = Path(__file__).resolve().parent / "guests"
DOCKERENV = Path("/.dockerenv")


def guests_dir():
    return Path(os.environ.get("GUESTS_DIR") or GUESTS_DEFAULT_DIR)


def guests_installed():
    """The module is here, and this is a server: inside Docker (a guest's own sandbox
    included) sandboxes can't be run, so no Guests block unless GUESTS_DIR says so."""
    if DOCKERENV.exists() and not os.environ.get("GUESTS_DIR"):
        return False
    return (guests_dir() / "guestctl.py").is_file()


def guests_env():
    """KEY=value lines of guests/.env (comments and order kept by guests_write_env)."""
    out = {}
    try:
        text = (guests_dir() / ".env").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def guests_write_env(values):
    path = guests_dir() / ".env"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = ["# AgentKennel guests module (written by the dashboard's Guests setup)"]
    left = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in left:
            lines[i] = f"{key}={left.pop(key)}"
    lines += [f"{k}={v}" for k, v in left.items()]
    tmp = path.with_name(".env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def guests_admin():
    """The owner of "Your bot": {"bot_set", "owner_id"} — no token, no owner = not set."""
    cfg = tg_load()
    owner = str(cfg.get("owner") or "").strip() if isinstance(cfg, dict) else ""
    token = str(cfg.get("token") or "").strip() if isinstance(cfg, dict) else ""
    ok = bool(token) and bool(re.fullmatch(r"[1-9][0-9]{0,14}", owner))
    return {"bot_set": ok, "owner_id": owner if ok else ""}


class GuestsPanel:
    def __init__(self):
        self.lock = threading.Lock()
        self.guests = None            # last `list --json` result
        self.list_at = 0.0
        self.list_error = None
        self.listing = False
        self.checks = None            # last `doctor --json` result
        self.checking = False
        self.jobs = {}                # name -> {action, started, done, ok, output}

    @staticmethod
    def run(*args, timeout=180):
        r = subprocess.run([sys.executable, str(guests_dir() / "guestctl.py"), *args],
                           capture_output=True, text=True, timeout=timeout,
                           cwd=str(guests_dir()))
        return r.returncode, r.stdout, r.stderr

    def _spawn(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _list(self):
        try:
            code, out, err = self.run("list", "--json", timeout=60)
            if code != 0:
                raise RuntimeError((err or out).strip()[-300:] or f"exit {code}")
            guests = json.loads(out).get("guests")
            if not isinstance(guests, list):
                raise RuntimeError("guestctl list: no guests list")
            with self.lock:
                self.guests, self.list_error = guests, None
        except Exception as e:
            with self.lock:
                self.list_error = str(e)[:300]
        finally:
            with self.lock:
                self.list_at, self.listing = time.time(), False

    def _doctor(self):
        try:
            code, out, err = self.run("doctor", "--json", timeout=60)
            checks = json.loads(out).get("checks")
            if not isinstance(checks, list):
                raise ValueError
        except Exception:
            checks = [{"id": "module", "title": "Guests module", "ok": False,
                       "detail": "guestctl doctor did not answer", "fix": ""}]
        with self.lock:
            self.checks, self.checking = checks, False

    def check(self):
        with self.lock:
            if self.checking:
                return
            self.checking = True
        self._spawn(self._doctor)

    def state(self):
        installed = guests_installed()
        env = guests_env() if installed else {}
        configured = installed and all(env.get(k) for k in _GUESTS_NEEDED)
        with self.lock:
            if configured and not self.listing and time.time() - self.list_at > GUESTS_LIST_TTL:
                self.listing = True
                self._spawn(self._list)
            return {"installed": installed, "configured": configured, "dir": str(guests_dir()),
                    "env": {"bot_token_set": bool(env.get("GUEST_BOT_TOKEN")),
                            "owner_id": env.get("OWNER_TG_ID", ""),
                            "domain": env.get("GUESTS_DOMAIN", "")},
                    "admin": guests_admin(),
                    "guests": self.guests if configured else None,
                    "list_error": self.list_error if configured else None,
                    "checks": self.checks, "checking": self.checking,
                    "jobs": {n: dict(j) for n, j in self.jobs.items()}}

    def setup(self, body):
        if not guests_installed():
            raise LibError(409, "the guests module is not in this AgentKennel")
        admin = guests_admin()
        if not admin["bot_set"]:
            raise LibError(409, "Set up your own Telegram bot first (the dashboard's Telegram "
                                "page): guests' requests to join go there")
        values = {"OWNER_TG_ID": admin["owner_id"]}
        for field, key in _GUESTS_ENV_KEYS.items():
            v = body.get(field)
            if v is None or v == "":
                continue                          # empty = keep what is there
            v = str(v).strip().lower() if field == "domain" else str(v).strip()
            if not _GUESTS_VALID[field].fullmatch(v):
                raise LibError(400, {"bot_token": "That doesn't look like a bot token (digits:letters, from @BotFather)",
                                     "domain": "A domain like example.com — no http://, no path"}[field])
            values[key] = v
        guests_write_env(values)
        self.check()
        return {"ok": True}

    def act(self, action, body):
        if action not in GUESTS_ACTIONS:
            raise LibError(404, "not found")
        name = body.get("name")
        if not isinstance(name, str) or not GUEST_NAME_RE.match(name):
            raise LibError(400, "bad guest name")
        args = [action, name]
        if action == "limit":
            limit = body.get("limit")
            if not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= 10**10:
                raise LibError(400, "the limit is a positive number of tokens per day")
            args.append(str(limit))
        if not self.state()["configured"]:
            raise LibError(409, "the guests module is not set up")
        with self.lock:
            job = self.jobs.get(name)
            if job and not job["done"]:
                raise LibError(409, f"{name}: {job['action']} is still running")
            self.jobs[name] = job = {"action": action, "started": time.time(), "done": False,
                                     "ok": None, "output": ""}

        def work():
            try:
                code, out, err = self.run(*args)
                ok, output = code == 0, (out + err).strip()[-500:]
            except Exception as e:
                ok, output = False, str(e)[:500]
            with self.lock:
                job.update(done=True, ok=ok, output=output)
            self._list()                          # the list shows the result at once
        self._spawn(work)
        return {"ok": True}


GUESTS = GuestsPanel()


class Handler(BaseHTTPRequestHandler):
    def _server_get(self):
        try:
            snap = SERVER_COLLECTOR.snapshot() or SERVER_COLLECTOR.sample()
            code, data = 200, snap
        except Exception as e:
            code, data = 500, {"error": str(e)[:200]}
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        return urlparse(getattr(self, "path", "/")).path.rstrip("/") or "/"

    def _lib_json(self, code, data):
        # same-origin only: no Access-Control-Allow-Origin on library responses
        self._json_response(code, data, cors=False)

    def _library_get(self):
        if self._route() != LIB_ROUTE:
            return self._lib_json(404, {"error": "not found"})
        qs = parse_qs(urlparse(self.path).query)
        yes = lambda k: qs.get(k, [""])[0].lower() in ("1", "true", "yes")   # noqa: E731
        try:
            self._lib_json(200, lib_listing(include_archived=yes("archived"), trash=yes("trash")))
        except Exception as e:
            self._lib_json(500, {"error": str(e)[:200]})

    def _write_body(self):
        """The JSON object of a same-origin write (the library's CSRF rule), or LibError."""
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise LibError(415, "Content-Type must be application/json")
        origin = self.headers.get("Origin")
        if origin is not None and not lib_origin_ok(origin, self.headers):
            raise LibError(403, "cross-origin request refused")
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > LIB_BODY_MAX:
            raise LibError(413, "body too large")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise LibError(400, "body is not JSON")
        if not isinstance(body, dict):
            raise LibError(400, "body must be a JSON object")
        return body

    def _is_services(self):
        r = self._route()
        return r == SVC_ROUTE or r.startswith(SVC_ROUTE + "/")

    def _services(self, method):
        route = self._route()
        try:
            if method == "GET" and route == SVC_ROUTE:
                data = svc_listing()
            elif method == "GET" and route.startswith(SVC_ROUTE + "/log/"):
                data = svc_log(route[len(SVC_ROUTE) + 5:])
            elif method == "POST" and route.startswith(SVC_ROUTE + "/"):
                self._write_body()
                data = svc_post(route[len(SVC_ROUTE) + 1:])
            else:
                raise LibError(404, "not found")
            self._lib_json(200, data)
        except LibError as e:
            self._lib_json(e.code, {"error": str(e)})
        except Exception as e:
            self._lib_json(500, {"error": str(e)[:200]})

    def _is_guests(self):
        r = self._route()
        return r == GUESTS_ROUTE or r.startswith(GUESTS_ROUTE + "/")

    def _guests(self, method):
        route = self._route()
        try:
            if method == "GET" and route == GUESTS_ROUTE:
                data = GUESTS.state()
            elif method == "POST" and route.startswith(GUESTS_ROUTE + "/"):
                body = self._write_body()
                what = route[len(GUESTS_ROUTE) + 1:]
                if what == "check":
                    GUESTS.check()
                    data = {"ok": True}
                elif what == "setup":
                    data = GUESTS.setup(body)
                else:
                    data = GUESTS.act(what, body)
            else:
                raise LibError(404, "not found")
            self._lib_json(200, data)
        except LibError as e:
            self._lib_json(e.code, {"error": str(e)})
        except Exception as e:
            self._lib_json(500, {"error": str(e)[:200]})

    def _library_post(self):
        route = self._route()
        if not route.startswith(LIB_ROUTE + "/"):
            return self._lib_json(404, {"error": "not found"})
        try:
            body = self._write_body()
            self._lib_json(200, lib_post(route[len(LIB_ROUTE) + 1:], body))
        except LibError as e:
            self._lib_json(e.code, {"error": str(e), **e.extra})
        except Exception as e:
            self._lib_json(500, {"error": str(e)[:200]})

    def _is_library(self):
        r = self._route()
        return r == LIB_ROUTE or r.startswith(LIB_ROUTE + "/")

    def do_GET(self):
        if self._is_library():
            return self._library_get()
        if self._route() == SERVER_ROUTE:
            return self._server_get()
        if self._is_services():
            return self._services("GET")
        if self._is_guests():
            return self._guests("GET")
        if (urlparse(getattr(self, "path", "/")).path.rstrip("/") or "/") == "/telegram":
            body = tg_render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        agents_meta = load_agents()

        # Phase 1: gather data + first CPU sample
        session_data = {}
        for t in SESSIONS:
            sid = t["id"]
            session_name = t["session"]

            r = subprocess.run(
                ["tmux", "has-session", "-t", "=" + session_name],   # exact, not a prefix
                capture_output=True,
            )
            if r.returncode != 0:
                session_data[sid] = {"active": False}
                continue

            pane_pid = get_pane_pid(session_name)
            cwd = get_cwd(session_name)
            pane_project, pane_task = parse_pane(session_name)

            # CWD-based project as fallback
            cwd_project = detect_project_from_cwd(cwd)
            auto_project = pane_project or cwd_project
            auto_task = pane_task

            if not pane_pid:
                session_data[sid] = {
                    "active": True,
                    "auto_project": auto_project,
                    "auto_task": auto_task,
                }
                continue

            session_data[sid] = {
                "active": True,
                "pane_pid": pane_pid,
                "ticks1": tree_cpu_ticks(pane_pid),
                "auto_project": auto_project,
                "auto_task": auto_task,
            }

        # Phase 2: CPU sampling
        time.sleep(SAMPLE_INTERVAL)

        # Phase 3: build response
        result = {}
        for t in SESSIONS:
            sid = t["id"]
            data = session_data[sid]
            meta = agents_meta.get(sid, {})
            active = data.get("active", False)
            working = False

            if "pane_pid" in data:
                working = is_working(data["ticks1"], tree_cpu_ticks(data["pane_pid"]))

            auto_proj = data.get("auto_project", "")
            auto_task = data.get("auto_task", "")

            saved_proj = meta.get("project", "")
            locked = meta.get("locked", False)

            # Auto-detect: set project and lock when found
            if not locked and auto_proj:
                if sid not in agents_meta:
                    agents_meta[sid] = {}
                agents_meta[sid]["project"] = auto_proj
                agents_meta[sid]["locked"] = True
                save_agents(agents_meta)
                saved_proj = auto_proj
                locked = True

            result[sid] = {
                "active": active,
                "working": working,
                "path": t["path"],
                "project": saved_proj,
                "auto_project": auto_proj,
                "task": meta.get("task") or auto_task,
                "locked": locked,
                # Per-agent overrides — UI uses these to render the model /
                # effort selectors. Empty string = "use default".
                "model": meta.get("model", ""),
                "effort": meta.get("effort", ""),
            }

        result["_system"] = get_system_stats()
        # Respect the user's saved _order:
        #   - missing key OR empty list → fresh install / new browser → start with
        #     the first four agents (add more with the "+ Claude" button)
        #   - non-empty list → echo it exactly, only stripping ids that no
        #     longer exist as a SESSION (e.g. leftover "k1" from a removed
        #     setup). DO NOT re-add ids the user explicitly removed with ×,
        #     or "closed" agents come back after every poll/refresh.
        all_ids = [t["id"] for t in SESSIONS]
        stored = agents_meta.get("_order")
        if isinstance(stored, list) and stored:
            result["_order"] = [i for i in stored if i in all_ids]
        else:
            result["_order"] = all_ids[:4]
        self._json_response(200, result)

    def _telegram_post(self):
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        token = form.get("token", [""])[0].strip()
        owner = form.get("owner", [""])[0].strip()
        action = form.get("action", ["start"])[0]
        out = tg_render(tg_action(action, token, owner)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        if self._is_library():
            return self._library_post()
        if self._is_services():
            return self._services("POST")
        if self._is_guests():
            return self._guests("POST")
        if (urlparse(getattr(self, "path", "/")).path.rstrip("/") or "/") == "/telegram":
            return self._telegram_post()
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        # Order sync
        if "_order" in body:
            agents = load_agents()
            agents["_order"] = body["_order"]
            save_agents(agents)
            self._json_response(200, {"ok": True})
            return

        agent_id = str(body.get("id", ""))
        valid_ids = {t["id"]: t["session"] for t in SESSIONS}
        if agent_id not in valid_ids:
            self._json_response(400, {"error": "invalid id"})
            return

        # Hard reset: wipe tmux session + conversation JSONL + override entry.
        if body.get("reset"):
            info = reset_agent(agent_id, valid_ids[agent_id])
            self._json_response(200, {"ok": True, **info})
            return

        # Fire-and-forget slash commands ("compact" so far). Pure runtime —
        # only sends if the tmux session is alive; nothing is persisted.
        VALID_ACTIONS = {"compact": "/compact"}
        if "action" in body:
            action = body["action"]
            if action not in VALID_ACTIONS:
                self._json_response(400, {"error": f"unknown action {action!r}"})
                return
            session_name = valid_ids[agent_id]
            r = subprocess.run(   # exact targets: never another slot (see reset_agent)
                ["tmux", "has-session", "-t", "=" + session_name],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                subprocess.run(
                    ["tmux", "send-keys", "-t", f"={session_name}:",
                     VALID_ACTIONS[action], "Enter"],
                    capture_output=True, text=True,
                )
            self._json_response(200, {"ok": True, "sent": r.returncode == 0})
            return

        # Effort must be one of these — validated BEFORE we touch the file
        # so a bad value never overwrites anything on disk.
        VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max", "auto"}
        if "effort" in body and body["effort"] not in ({""} | VALID_EFFORTS):
            self._json_response(400, {"error": f"invalid effort {body['effort']!r}"})
            return

        agents = load_agents()
        if agent_id not in agents:
            agents[agent_id] = {}
        if "project" in body:
            agents[agent_id]["project"] = body["project"]
            agents[agent_id]["locked"] = True
        if "task" in body:
            agents[agent_id]["task"] = body["task"]
        if "unlock" in body and body["unlock"]:
            agents[agent_id]["locked"] = False
        # Per-agent model override. Empty string clears the override so the
        # next launch falls back to the Claude subscription default.
        slash_cmds: list[str] = []
        if "model" in body:
            val = body["model"]
            if isinstance(val, str) and val:
                agents[agent_id]["model"] = val
                slash_cmds.append(f"/model {val}")
            else:
                agents[agent_id].pop("model", None)
        # Per-agent effort override. Empty string clears → next launch uses "auto".
        if "effort" in body:
            val = body["effort"]
            if isinstance(val, str) and val:
                agents[agent_id]["effort"] = val
                slash_cmds.append(f"/effort {val}")
            else:
                agents[agent_id].pop("effort", None)
        save_agents(agents)

        # Live-apply: if the agent's tmux session is alive, inject the slash
        # command(s) so the change takes effect immediately. Without this,
        # POSTing only persists to disk and the user wouldn't see anything
        # change in the terminal until the next launch.
        if slash_cmds:
            session_name = valid_ids[agent_id]
            r = subprocess.run(   # exact targets: never another slot (see reset_agent)
                ["tmux", "has-session", "-t", "=" + session_name],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                for cmd in slash_cmds:
                    subprocess.run(
                        ["tmux", "send-keys", "-t", f"={session_name}:", cmd, "Enter"],
                        capture_output=True, text=True,
                    )

        self._json_response(200, {"ok": True})

    def do_OPTIONS(self):
        self.send_response(204)
        if (self._is_library() or self._is_services() or self._is_guests()
                or self._route() == SERVER_ROUTE):   # no CORS preflight
            self.end_headers()
            return
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json_response(self, code, data, cors=True):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, *args):
        pass


class BufferHandler(BaseHTTPRequestHandler):
    """GET /  — return tmux paste buffer for a session."""
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        session = qs.get("session", [""])[0]
        if not session or not re.match(r'^[\w-]+$', session):
            self._resp(400, {"error": "bad session"})
            return
        r = subprocess.run(
            ["tmux", "show-buffer", "-t", session],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            # try without -t (global buffer)
            r = subprocess.run(["tmux", "show-buffer"], capture_output=True, text=True)
        self._resp(200, {"text": r.stdout if r.returncode == 0 else ""})

    def _resp(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, *args):
        pass


# the actually-served dashboard file (the old ../agents/index.html was archived,
# so page-version was stuck at "0" and live-reload never fired)
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WATCH_FILE = os.path.join(WEB_DIR, "index.html")   # kept for callers of the old name
# pages that poll /api/page-version, by the file name the browser shows in Referer;
# anything else (/, unknown names) watches index.html
WATCHED_PAGES = ("index.html", "index-lib.html")


def watch_file_for(referer):
    """The page file whose mtime is this page's version: the library page is
    served from web/index-lib.html, so edits to it must reload it too."""
    page = os.path.basename(urlparse(referer or "").path)
    return os.path.join(WEB_DIR, page if page in WATCHED_PAGES else "index.html")


class LiveHandler(BaseHTTPRequestHandler):
    """Returns mtime of the page that asks (Referer) for live-reload."""
    def do_GET(self):
        try:
            mtime = os.path.getmtime(watch_file_for(self.headers.get("Referer")))
        except OSError:
            mtime = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(str(mtime).encode())

    def log_message(self, *args):
        pass


# ── Cookie-session auth (replaces the nginx basic-auth browser dialog) ───────
# The basic-auth dialog re-prompts on every 401, so a burst of polling/reload
# requests showed it dozens of times ("can't even type the password"). A cookie
# session has NO native dialog: one login on a page → signed cookie → every
# request (fetch, iframe, ttyd ws) carries it automatically. Passwords are the
# SAME (verified against the existing /etc/nginx/.htpasswd_agents).

HTPASSWD_FILE = os.environ.get("AGENTDECK_HTPASSWD", "/etc/nginx/.htpasswd_agents")
# Cookie-signing key. AGENTDECK_AUTH_SECRET lets the entrypoint keep it in the persistent
# volume (so sessions survive a container recreate and the key is never baked into the image);
# the default sits next to this module, leaving the existing host service untouched.
AUTH_SECRET_FILE = os.environ.get(
    "AGENTDECK_AUTH_SECRET",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agents_auth_secret"),
)
AUTH_COOKIE = "agents_session"
AUTH_TTL = 30 * 24 * 3600          # 30 days


def _auth_secret():
    try:
        with open(AUTH_SECRET_FILE, "rb") as f:
            s = f.read().strip()
            if s:
                return s
    except OSError:
        pass
    s = os.urandom(32).hex().encode()
    old = os.umask(0o077)
    try:
        with open(AUTH_SECRET_FILE, "wb") as f:
            f.write(s)
    finally:
        os.umask(old)
    return s


AUTH_SECRET = _auth_secret()


def _sign_token(user, exp):
    sig = hmac.new(AUTH_SECRET, f"{user}.{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{user}.{exp}.{sig}"


def _verify_token(tok):
    if not tok or tok.count(".") < 2:
        return None
    user, exp, sig = tok.rsplit(".", 2)
    if not exp.isdigit() or int(exp) < int(time.time()):
        return None
    expect = hmac.new(AUTH_SECRET, f"{user}.{exp}".encode(), hashlib.sha256).hexdigest()
    return user if hmac.compare_digest(expect, sig) else None


# Standalone/Docker mode: keep a single dashboard password (pbkdf2 hash) in a file the
# user SETS on first run and can change in the UI. If AGENTDECK_PASSFILE is unset, behaviour
# is unchanged (verify against /etc/nginx/.htpasswd_agents) — the live nginx setup is untouched.
PASSFILE = os.environ.get("AGENTDECK_PASSFILE", "")


def _pw_is_set():
    try:
        return bool(PASSFILE) and os.path.getsize(PASSFILE) > 0
    except OSError:
        return False


def _pw_store(pw):
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    old = os.umask(0o077)
    try:
        with open(PASSFILE, "w") as f:
            f.write(f"{salt.hex()}${h.hex()}")
    finally:
        os.umask(old)
    _setup_token_drop()


# First run: the password may be set only through /login?setup=<token>. Without it, the
# first stranger to open a fresh install (sslip.io names show up in public certificate
# logs within minutes) would set the password and own a shell with your sudo. The token
# sits next to the passfile (0600); install.sh and the container's log print the link
# (`status_server.py --setup-link`); setting the password deletes it.
def _setup_token_file():
    return PASSFILE + ".setup" if PASSFILE else ""


def _setup_open():
    return bool(PASSFILE) and not _pw_is_set() and not _pw_seedable()


def _setup_token():
    """The current token, made on first use; '' when there is nothing to set up."""
    if not _setup_open():
        return ""
    path = _setup_token_file()
    try:
        with open(path) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    old = os.umask(0o077)
    try:
        with open(path, "w") as f:
            f.write(tok + "\n")
    finally:
        os.umask(old)
    return tok


def _setup_token_ok(given):
    tok = _setup_token()
    return bool(tok and given) and hmac.compare_digest(tok, given)


def _setup_token_drop():
    try:
        os.unlink(_setup_token_file())
    except OSError:
        pass


def setup_link_path():
    tok = _setup_token()
    return f"/login?setup={tok}" if tok else ""


def _pw_verify(pw):
    try:
        with open(PASSFILE) as f:
            salt_hex, want = f.read().strip().split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(h.hex(), want)
    except (OSError, ValueError):
        return False


def _htpasswd_verify(user, pw):
    if not pw or len(pw) > 256 or not re.match(r'^[A-Za-z0-9_.-]{1,32}$', user or ''):
        return False
    try:
        r = subprocess.run(["htpasswd", "-vb", HTPASSWD_FILE, user, pw],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _pw_seedable():
    """Passfile mode, passfile still empty, but the old nginx htpasswd exists:
    migrate on the next successful login instead of offering an open 'set a
    password' form (the first stranger to arrive would own the dashboard)."""
    return bool(PASSFILE) and not _pw_is_set() and os.path.exists(HTPASSWD_FILE)


def _login_verify(user, pw):
    """Login check; in the seedable state a correct htpasswd login also moves the
    password into the dashboard's own passfile (nobody has to retype it anywhere)."""
    if _pw_seedable():
        if _htpasswd_verify(user, pw):
            _pw_store(pw)
            return True
        return False
    return _check_password(user, pw)


def _verify_current(user, pw):
    """'Current password' on /change-password: the passfile once seeded, else htpasswd."""
    return _htpasswd_verify(user, pw) if _pw_seedable() else _pw_verify(pw)


def _check_password(user, pw):
    if not pw or len(pw) > 256:
        return False
    if PASSFILE:                       # standalone mode: one password, username ignored
        return _pw_verify(pw)
    return _htpasswd_verify(user, pw)


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AgentKennel — login</title>
<style>
*{box-sizing:border-box} html,body{height:100%}
body{margin:0;display:flex;align-items:center;justify-content:center;background:#0d1117;
  color:#e6edf3;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.card{width:min(92vw,340px);background:#161b22;border:1px solid #30363d;border-radius:14px;
  padding:28px 26px;box-shadow:0 10px 40px rgba(0,0,0,.4)}
h1{margin:0 0 4px;font-size:19px} .sub{margin:0 0 20px;color:#8b949e;font-size:13px}
label{display:block;font-size:12px;color:#8b949e;margin:12px 0 5px}
input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #30363d;background:#0d1117;
  color:#e6edf3;font-size:15px;outline:none}
input:focus{border-color:#388bfd;box-shadow:0 0 0 3px rgba(56,139,253,.25)}
button{width:100%;margin-top:18px;padding:11px;border:0;border-radius:8px;background:#238636;
  color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:hover{background:#2ea043}
.err{margin:14px 0 0;color:#ff7b72;font-size:13px;text-align:center}
</style>__BREAKOUT__</head><body>
<form class="card" method="POST" action="/login">
  <h1>🐾 AgentKennel</h1><p class="sub">Sign in to open the panel</p>
  __USER__<label for="p">Password</label>
  <input id="p" name="pass" type="password" autocomplete="current-password"__PFOCUS__>
  <button type="submit">Sign in</button>
  __ERR__
</form></body></html>"""


_LOGIN_USER_FIELD = ('<label for="u">Username</label>\n'
                     '  <input id="u" name="user" autocomplete="username" autofocus>\n  ')


def _login_needs_user():
    """The username matters only where htpasswd checks it: no passfile (htpasswd mode),
    or the passfile-still-empty migration state. A set passfile ignores it."""
    return not PASSFILE or _pw_seedable()


# A framed sign-in page takes the whole tab: after an expired session the terminal iframe
# reloads into /login, and logging in THERE nested a second dashboard inside the pane.
_FRAME_BREAKOUT = "<script>if(window.top!==window.self)window.top.location.replace('/login')</script>"


def login_html(error=False):
    """The sign-in page; Username field only where it is checked."""
    need_user = _login_needs_user()
    err = ("Invalid username or password" if need_user else "Invalid password")
    return (LOGIN_HTML
            .replace("__USER__", _LOGIN_USER_FIELD if need_user else "")
            .replace("__PFOCUS__", "" if need_user else " autofocus")
            .replace("__BREAKOUT__", _FRAME_BREAKOUT)
            .replace("__ERR__", f'<p class="err">{err}</p>' if error else ""))


_AUTH_HEAD = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
  '<meta name="viewport" content="width=device-width,initial-scale=1"><title>AgentKennel</title><style>'
  '*{box-sizing:border-box}html,body{height:100%}'
  'body{margin:0;display:flex;align-items:center;justify-content:center;background:#0d1117;'
  'color:#e6edf3;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}'
  '.card{width:min(92vw,340px);background:#161b22;border:1px solid #30363d;border-radius:14px;'
  'padding:28px 26px;box-shadow:0 10px 40px rgba(0,0,0,.4)}'
  'h1{margin:0 0 4px;font-size:19px}.sub{margin:0 0 20px;color:#8b949e;font-size:13px}'
  'label{display:block;font-size:12px;color:#8b949e;margin:12px 0 5px}'
  'input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #30363d;background:#0d1117;'
  'color:#e6edf3;font-size:15px;outline:none}'
  'input:focus{border-color:#388bfd;box-shadow:0 0 0 3px rgba(56,139,253,.25)}'
  'button{width:100%;margin-top:18px;padding:11px;border:0;border-radius:8px;background:#238636;'
  'color:#fff;font-size:15px;font-weight:600;cursor:pointer}button:hover{background:#2ea043}'
  '.err{margin:14px 0 0;color:#ff7b72;font-size:13px;text-align:center}'
  '.ok{margin:14px 0 0;color:#3fb950;font-size:13px;text-align:center}'
  'a{color:#58a6ff}</style>' + _FRAME_BREAKOUT + '</head><body>')


def _setup_form(error="", token=""):
    return (_AUTH_HEAD + '<form class="card" method="POST" action="/login">'
      '<h1>🐾 AgentKennel</h1><p class="sub">Set a password to protect the dashboard</p>'
      f'<input type="hidden" name="setup" value="{token}">'
      '<label for="p">New password</label>'
      '<input id="p" name="pass" type="password" autocomplete="new-password" autofocus>'
      '<label for="p2">Repeat password</label>'
      '<input id="p2" name="pass2" type="password" autocomplete="new-password">'
      '<button type="submit">Set password</button>'
      + (f'<p class="err">{error}</p>' if error else '') + '</form></body></html>')


def _setup_locked():
    return (_AUTH_HEAD + '<div class="card">'
      '<h1>🐾 AgentKennel</h1><p class="sub">No password yet</p>'
      '<p>Open the <b>setup link</b> the installer printed — only that link can set the '
      'password. To show it again, on the server run <code>~/agentdeck/install.sh</code> '
      'again, or <code>python3 status_server.py --setup-link</code> in the AgentKennel folder '
      '(Docker: <code>docker compose logs agentdeck | grep setup</code>).</p>'
      '</div></body></html>')


def _change_form(error="", ok=""):
    return (_AUTH_HEAD + '<form class="card" method="POST" action="/change-password">'
      '<h1>Change password</h1><p class="sub">Enter your current and a new password</p>'
      '<label for="c">Current password</label>'
      '<input id="c" name="cur" type="password" autocomplete="current-password" autofocus>'
      '<label for="p">New password</label>'
      '<input id="p" name="pass" type="password" autocomplete="new-password">'
      '<label for="p2">Repeat new password</label>'
      '<input id="p2" name="pass2" type="password" autocomplete="new-password">'
      '<button type="submit">Change password</button>'
      + (f'<p class="err">{error}</p>' if error else '')
      + (f'<p class="ok">{ok}</p>' if ok else '')
      + '<p style="text-align:center;margin-top:14px"><a href="/">&larr; back to dashboard</a></p></form></body></html>')


def _cookie_flags(secure=True):
    # HttpOnly: JS can't read it. Secure (HTTPS-only) is dropped on plain http so the
    # cookie actually comes back (e.g. a Docker http port).
    return "Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (AUTH_TTL, "; Secure" if secure else "")


class AuthHandler(BaseHTTPRequestHandler):
    """Cookie login. /check (auth probe), /login (form/verify; FIRST RUN in
    AGENTDECK_PASSFILE mode = set the password), /change-password, /logout."""

    def _secure(self):
        return self.headers.get("X-Forwarded-Proto", "") == "https"

    def _html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_login(self, error=False):
        if _setup_open():
            given = parse_qs(urlparse(self.path).query).get("setup", [""])[0]
            if not _setup_token_ok(given):
                return self._html(_setup_locked(), 403)
            return self._html(_setup_form(token=given))
        self._html(login_html(error), 401 if error else 200)

    def _session_cookie(self, user):
        tok = _sign_token(user or "admin", int(time.time()) + AUTH_TTL)
        return f"{AUTH_COOKIE}={tok}; {_cookie_flags(self._secure())}"

    def _login_cookie(self, user):
        self.send_response(302)
        self.send_header("Set-Cookie", self._session_cookie(user))
        self.send_header("Location", "/")
        self.end_headers()

    def _renew(self):
        # Sliding session: the open dashboard calls GET /login?renew=1 every few hours and a
        # still-valid cookie is reissued for another AUTH_TTL, so daily use never reaches the
        # expiry. No valid cookie → 401 and nothing set (an expired login is not revived).
        user = self._user()
        self.send_response(204 if user else 401)
        self.send_header("Cache-Control", "no-store")
        if user:
            self.send_header("Set-Cookie", self._session_cookie(user))
        self.end_headers()

    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _user(self):
        return _verify_token(self._cookies().get(AUTH_COOKIE, ""))

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/check":
            self.send_response(200 if self._user() else 401)
            self.end_headers()
            return
        if path == "/logout":
            self.send_response(302)
            self.send_header("Set-Cookie", f"{AUTH_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if path == "/change-password":
            if not (PASSFILE and self._user()):
                return self._redirect("/login")
            return self._html(_change_form())
        if path == "/login" and parse_qs(urlparse(self.path).query).get("renew") == ["1"]:
            return self._renew()
        self._send_login()      # /login (or anything else) → the form

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8", "replace") if 0 < length <= 4096 else ""
        form = parse_qs(body)

        if path == "/change-password":
            if not (PASSFILE and self._user()):
                return self._redirect("/login")
            cur = form.get("cur", [""])[0]
            new, new2 = form.get("pass", [""])[0], form.get("pass2", [""])[0]
            if not _verify_current(self._user(), cur):
                return self._html(_change_form(error="Current password is wrong"))
            if len(new) < 6 or new != new2:
                return self._html(_change_form(error="New passwords must match (6+ chars)"))
            _pw_store(new)
            return self._html(_change_form(ok="Password changed."))

        # /login — FIRST RUN (passfile mode, no password yet) sets it, with the setup
        # link's token only; otherwise verify
        if _setup_open():
            given = form.get("setup", [""])[0]
            if not _setup_token_ok(given):
                return self._html(_setup_locked(), 403)
            new, new2 = form.get("pass", [""])[0], form.get("pass2", [""])[0]
            if len(new) < 6 or new != new2:
                return self._html(_setup_form("Passwords must match (6+ chars)", token=given))
            _pw_store(new)
            return self._login_cookie("admin")

        user = (form.get("user", [""])[0]).strip()
        pw = form.get("pass", [""])[0]
        if _login_verify(user, pw):
            self._login_cookie(user)
        else:
            self._send_login(error=True)

    def log_message(self, *args):
        pass


# ── Clipboard image paste → save to tgimg, hand the terminal a file path ─────
# A web terminal (ttyd/tmux) can't accept a pasted image — it's text-only. So the
# dashboard intercepts an image paste, POSTs the bytes here, and types the saved
# file PATH into the terminal so the agent (Claude Code) can Read the image.

# Where pasted images are saved, and (optionally) the public base URL they're served at.
# Default: inside the repo (.sessions/paste — the persisted volume in Docker), no public
# URL → the response's "url" is the local path. The dashboard only uses "path" (typed into
# the terminal so the agent can Read it). A server that serves the folder publicly sets
# AGENTDECK_PASTE_DIR + AGENTDECK_PASTE_URL (TG_FILES_DIR_IMG is the legacy name for the dir).
PASTE_DIR = (os.environ.get("AGENTDECK_PASTE_DIR") or os.environ.get("TG_FILES_DIR_IMG")
             or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sessions", "paste"))
PASTE_URL = os.environ.get("AGENTDECK_PASTE_URL", "").rstrip("/")
_PASTE_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/svg+xml": ".svg",
}


def _paste_ext(ctype):
    return _PASTE_EXT.get((ctype or "").split(";")[0].strip().lower())


def save_paste_image(data: bytes, ctype: str, token: str, dest_dir: str | None = None) -> dict:
    """Save pasted image bytes to dest_dir as paste_<token><ext>; return {path,url}
    (url = PASTE_URL/<name>, or the local path when no public URL is configured)."""
    dest_dir = dest_dir or PASTE_DIR
    ext = _paste_ext(ctype)
    if not ext:
        raise ValueError(f"unsupported content-type: {ctype!r}")
    if not data:
        raise ValueError("empty body")
    name = f"paste_{token}{ext}"
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, name), "wb") as f:
        f.write(data)
    path = os.path.join(dest_dir, name)
    return {"path": path, "url": f"{PASTE_URL}/{name}" if PASTE_URL else path}


# ── 📎 File: any file from the browser → a PRIVATE folder, the terminal gets its path ──
# A path, not a link: the agent runs on this machine and reads the file directly, and a
# public link would expose whatever was uploaded (client documents, keys). Files keep
# their (made safe) name under <UPLOAD_DIR>/<YYYY-MM-DD>/, folder 0700, files 0600.
UPLOAD_DIR = (os.environ.get("AGENTDECK_UPLOAD_DIR")
              or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sessions", "uploads"))
UPLOAD_MAX = int(float(os.environ.get("AGENTDECK_UPLOAD_MAX_MB", "100")) * 1024 * 1024)


def _upload_name(raw):
    """The file's own name, safe to type into a terminal: its last path part only, letters
    (any script), digits and . _ - + kept, everything else (spaces, ; $ ` ( ) …) → _,
    no leading dot, at most 120 characters with the extension kept."""
    name = os.path.basename(str(raw or "").replace("\\", "/"))
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r"[^\w.+-]", "_", name)
    name = re.sub(r"_{2,}", "_", name).strip("._") or "file"
    if len(name) > 120:
        stem, ext = os.path.splitext(name)
        ext = ext[:16]
        name = stem[:120 - len(ext)] + ext
    return name


def save_upload(src, length: int, raw_name: str, dest_dir: str | None = None) -> dict:
    """Stream `length` bytes from src into <dest_dir>/<today>/<safe name> (name_2, name_3 …
    when taken); return {path, name, size}. A short body leaves no file behind."""
    if length <= 0:
        raise ValueError("empty body")
    root = dest_dir or UPLOAD_DIR
    day = os.path.join(root, time.strftime("%Y-%m-%d"))
    os.makedirs(day, mode=0o700, exist_ok=True)
    for d in (root, day):
        os.chmod(d, 0o700)
    name = _upload_name(raw_name)
    stem, ext = os.path.splitext(name)
    n = 1
    while True:
        path = os.path.join(day, name if n == 1 else f"{stem}_{n}{ext}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
    got = 0
    try:
        with os.fdopen(fd, "wb") as f:
            while got < length:
                chunk = src.read(min(1 << 20, length - got))
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
        if got < length:
            raise ValueError(f"upload cut short: {got} of {length} bytes")
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return {"path": path, "name": os.path.basename(path), "size": got}


class PasteHandler(BaseHTTPRequestHandler):
    """POST raw image bytes (Content-Type: image/*) → save → {path, url} JSON.
    POST /api/upload?name=<file name> with any bytes → save_upload → {path, name, size}."""

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _upload(self):
        # the proxy gates this too; checked here as well so a route slip can't open it
        cookies = dict(p.strip().split("=", 1) for p in (self.headers.get("Cookie", "") or "").split(";")
                       if "=" in p)
        if not _verify_token(cookies.get(AUTH_COOKIE, "")):
            self._json(401, {"error": "not signed in"}); return
        try:
            length = int(self.headers.get("Content-Length", "") or "-1")
        except ValueError:
            length = -1
        if length < 0:
            self._json(411, {"error": "Content-Length required"}); return
        if length > UPLOAD_MAX:
            self.close_connection = True
            self._json(413, {"error": "file too large", "max_mb": UPLOAD_MAX // (1024 * 1024)}); return
        name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
        try:
            res = save_upload(self.rfile, length, name)
        except ValueError as e:
            self._json(400, {"error": str(e)[:200]}); return
        except OSError as e:
            self._json(500, {"error": str(e)[:200]}); return
        print("UPLOAD-SAVED", time.strftime("%H:%M:%S"), res["path"], res["size"], flush=True)
        self._json(200, res)

    def do_POST(self):
        if urlparse(self.path).path.rstrip("/") == "/api/upload":
            return self._upload()
        try:
            ctype = (self.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            length = int(self.headers.get("Content-Length", "0") or "0")
            # diagnostic client log (so we can SEE what happens in the user's browser)
            if ctype == "application/json":
                body = self.rfile.read(length).decode("utf-8", "replace") if 0 < length <= 65536 else ""
                print("PASTE-CLIENTLOG", time.strftime("%H:%M:%S"), body[:600], flush=True)
                self._json(200, {"ok": True}); return
            if length <= 0 or length > 30 * 1024 * 1024:
                self._json(413, {"error": "empty or too large"}); return
            if not _paste_ext(ctype):
                self._json(415, {"error": "not an image"}); return
            data = self.rfile.read(length)
            res = save_paste_image(data, ctype, os.urandom(6).hex())
            print("PASTE-SAVED", time.strftime("%H:%M:%S"), res["path"], flush=True)
            self._json(200, res)
        except Exception as e:
            self._json(500, {"error": str(e)[:200]})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if "--setup-link" in sys.argv[1:]:   # the first-run link's path ('' + exit 1: none)
        link = setup_link_path()
        print(link)
        sys.exit(0 if link else 1)
    from threading import Thread
    SERVER_COLLECTOR.start()  # /api/server: sample /proc every 5 s
    tg_autostart()  # bring the Telegram bridge back up if it was configured + enabled
    Thread(target=lambda: HTTPServer(("127.0.0.1", 3014), LiveHandler).serve_forever(), daemon=True).start()
    Thread(target=lambda: HTTPServer(("127.0.0.1", 3045), BufferHandler).serve_forever(), daemon=True).start()
    Thread(target=lambda: HTTPServer(("127.0.0.1", 3046), AuthHandler).serve_forever(), daemon=True).start()
    # threaded: a 100 MB upload must not hold up an image paste
    Thread(target=lambda: ThreadingHTTPServer(("127.0.0.1", 3047), PasteHandler).serve_forever(), daemon=True).start()
    HTTPServer(("127.0.0.1", 3011), Handler).serve_forever()
