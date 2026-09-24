#!/usr/bin/env python3
"""Session library: named topic-sessions instead of numbered slots.

A session is one Claude conversation. Its identity is `id` — the first 8 hex
chars of the Claude session UUID — so the tmux name `cs-<id>`, a log line and
the transcript file `~/.claude/projects/<cwd-slug>/<uuid>.jsonl` all grep to
each other. There is no slot number anywhere.

Unlimited sessions live in the registry (.sessions/library.json); at most
MAX_ACTIVE run in RAM as tmux sessions. Opening one more unloads the least
recently used idle one (never a working one, never one with a tab open).
Unloading loses nothing: the conversation is on disk and `claude --resume`
continues it.

Only status_server writes the registry at runtime (through update(), which
locks and replaces the file atomically); scripts read it.
"""
import contextlib
import fcntl
import json
import os
import re
import time
import uuid as _uuid

HERE = os.path.dirname(os.path.abspath(__file__))
LIB_FILE = os.getenv("AGENTDECK_LIBRARY", os.path.join(HERE, ".sessions", "library.json"))
MAX_ACTIVE = int(os.getenv("AGENTDECK_MAX_ACTIVE", "12"))
TMUX_PREFIX = "cs-"

_ID = re.compile(r"[0-9a-f]{8}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# ── identity ────────────────────────────────────────────────────────────────
def id_from_uuid(u):
    return u.replace("-", "")[:8]


def valid_id(s):
    return isinstance(s, str) and _ID.fullmatch(s) is not None


def tmux_name(sid):
    return TMUX_PREFIX + sid


def id_from_tmux(name):
    if name.startswith(TMUX_PREFIX) and valid_id(name[len(TMUX_PREFIX):]):
        return name[len(TMUX_PREFIX):]
    return None


# ── registry file ───────────────────────────────────────────────────────────
def empty():
    return {"sessions": []}


def load(path=LIB_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return empty()
    data.setdefault("sessions", [])
    return data


def save(path, lib):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


@contextlib.contextmanager
def update(path=LIB_FILE):
    """Locked read-modify-write: `with update() as lib: ...` saves on exit."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        lib = load(path)
        yield lib
        save(path, lib)


# ── edit ────────────────────────────────────────────────────────────────────
def find(lib, sid):
    for e in lib["sessions"]:
        if e["id"] == sid:
            return e
    return None


def _get(lib, sid):
    e = find(lib, sid)
    if e is None:
        raise KeyError(sid)
    return e


def default_name(now):
    return time.strftime("Тема %d.%m %H:%M", time.gmtime(now))


def create(lib, name, cwd, now, uuid=None):
    u = uuid or str(_uuid.uuid4())
    while uuid is None and find(lib, id_from_uuid(u)):      # fresh uuid collided: redraw
        u = str(_uuid.uuid4())
    sid = id_from_uuid(u)
    if find(lib, sid):
        raise ValueError(f"session {sid} already exists")
    e = {"id": sid, "uuid": u, "name": (name or "").strip() or default_name(now),
         "cwd": cwd, "created": now, "last_used": now, "archived": False}
    lib["sessions"].append(e)
    return e


def rename(lib, sid, name):
    e = _get(lib, sid)
    name = (name or "").strip()
    if name:
        e["name"] = name
    return e


def touch(lib, sid, now):
    e = _get(lib, sid)
    e["last_used"] = now
    return e


def archive(lib, sid, archived=True):
    e = _get(lib, sid)
    e["archived"] = bool(archived)
    return e


# ── search / resolve / order ────────────────────────────────────────────────
def search(lib, text, include_archived=False):
    q = (text or "").strip().casefold()
    out = []
    for e in lib["sessions"]:
        if e.get("archived") and not include_archived:
            continue
        if not q or q in e["name"].casefold() or e["id"].startswith(q):
            out.append(e)
    return out


def resolve(lib, text):
    """For `/use <text>`: exact id, else id prefix, else name substring."""
    q = (text or "").strip().casefold()
    if not q:
        return []
    live = [e for e in lib["sessions"] if not e.get("archived")]
    for rule in (lambda e: e["id"] == q, lambda e: e["id"].startswith(q),
                 lambda e: q in e["name"].casefold()):
        hits = [e for e in live if rule(e)]
        if hits:
            return hits
    return []


def display_order(lib, active_ids, include_archived=False):
    """Active (loaded) first, then the rest; each group most recent first."""
    rows = [e for e in lib["sessions"] if include_archived or not e.get("archived")]
    return sorted(rows, key=lambda e: (e["id"] not in active_ids, -e.get("last_used", 0)))


# ── LRU eviction ────────────────────────────────────────────────────────────
def needs_eviction(active_count, limit=MAX_ACTIVE):
    return active_count >= limit


def pick_victim(live):
    """live: [{id, last_used, attached, working}] -> id to unload, or None."""
    idle = [s for s in live if not s["attached"] and not s["working"]]
    if not idle:
        return None
    return min(idle, key=lambda s: s.get("last_used", 0))["id"]


# ── migration from numbered slots ───────────────────────────────────────────
def migrate_from_slots(sessions_dir, agents, cwd, now, last_used=None):
    """Registry entries from the old .sessions/agent-N.id files.

    Name = the slot's `project` label from agents.json, else "терминал N".
    Returns (lib, report) — report lists every slot and what became of it.
    """
    last_used = last_used or {}
    lib, report = empty(), []
    files = [f for f in os.listdir(sessions_dir) if re.fullmatch(r"agent-\d+\.id", f)]
    for f in sorted(files, key=lambda x: int(re.search(r"\d+", x).group())):
        slot = int(re.search(r"\d+", f).group())
        u = open(os.path.join(sessions_dir, f)).read().strip()
        if not _UUID.fullmatch(u):
            report.append(dict(slot=slot, skipped=True, reason="not a uuid"))
            continue
        label = ((agents or {}).get(str(slot)) or {}).get("project") or f"терминал {slot}"
        e = create(lib, label, cwd=cwd, now=now, uuid=u)
        e["legacy_slot"] = slot
        e["last_used"] = last_used.get(e["id"], now)
        report.append(dict(slot=slot, skipped=False, id=e["id"], name=e["name"]))
    return lib, report


def merge(base, extra):
    """Add entries from `extra` whose id isn't in `base`; existing ones win."""
    out = {"sessions": list(base["sessions"])}
    have = {e["id"] for e in out["sessions"]}
    for e in extra["sessions"]:
        if e["id"] not in have:
            out["sessions"].append(e)
            have.add(e["id"])
    return out
