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
import shutil
import time
import uuid as _uuid

HERE = os.path.dirname(os.path.abspath(__file__))
LIB_FILE = os.getenv("AGENTDECK_LIBRARY", os.path.join(HERE, ".sessions", "library.json"))
MAX_ACTIVE = int(os.getenv("AGENTDECK_MAX_ACTIVE", "12"))
TMUX_PREFIX = "cs-"
# The dashboard's one plain command line (the "cmd" button): a bash shell, not a
# topic — never counted toward MAX_ACTIVE, never an eviction victim.
SHELL_TMUX = "cmd-shell"

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


class CorruptRegistry(RuntimeError):
    """The registry file exists but is not a valid library. Never papered over
    with an empty one: the next update() would save that and lose every topic."""


def load(path=LIB_FILE):
    """The registry. Empty ONLY when the file does not exist yet."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty()
    except ValueError as ex:
        raise CorruptRegistry(f"session registry {path} is not valid JSON ({ex}); "
                              f"fix it or restore {path}.bak") from None
    if not isinstance(data, dict) or not isinstance(data.setdefault("sessions", []), list):
        raise CorruptRegistry(f"session registry {path} has no session list; "
                              f"fix it or restore {path}.bak")
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
        lib = load(path)                           # raises on a corrupt file
        yield lib
        if os.path.exists(path):                   # previous version, one step back
            shutil.copyfile(path, path + ".bak.tmp")
            os.replace(path + ".bak.tmp", path + ".bak")
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
    return time.strftime("Terminal %d.%m %H:%M", time.gmtime(now))


def create(lib, name, cwd, now, uuid=None):
    u = uuid or str(_uuid.uuid4())
    while uuid is None and find(lib, id_from_uuid(u)):      # fresh uuid collided: redraw
        u = str(_uuid.uuid4())
    sid = id_from_uuid(u)
    if find(lib, sid):
        raise ValueError(f"session {sid} already exists")
    e = {"id": sid, "uuid": u, "name": (name or "").strip() or default_name(now),
         "cwd": cwd, "created": now, "last_used": now, "archived": False}
    placed = [x["pos"] for x in lib["sessions"] if _has_pos(x)]
    if placed:                                  # manual order in use: new one on top
        e["pos"] = min(placed) - 1
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


def delete(lib, sid):
    """Remove the entry from the registry (KeyError if unknown). Callers only
    delete archived, unloaded topics; the transcript goes to trash_transcript()."""
    e = _get(lib, sid)
    lib["sessions"].remove(e)
    return e


# ── transcripts → trash (moved, never unlinked) ─────────────────────────────
def claude_projects_root():
    """~/.claude/projects, or $AGENTDECK_CLAUDE_PROJECTS (tests)."""
    return (os.getenv("AGENTDECK_CLAUDE_PROJECTS")
            or os.path.join(os.path.expanduser("~"), ".claude", "projects"))


def cwd_slug(cwd):
    """Claude's project-dir name for a cwd: every non-alphanumeric char -> '-'
    (same rule as library_cli.slug; '/' -> '-' is the common case)."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _free_name(path):
    if not os.path.lexists(path):
        return path
    root, ext = os.path.splitext(path) if not os.path.isdir(path) else (path, "")
    n = 1
    while os.path.lexists(f"{root}.{int(time.time())}-{n}{ext}"):
        n += 1
    return f"{root}.{int(time.time())}-{n}{ext}"


def trash_transcript(e, lib_file=None, projects_root=None):
    """Move <projects>/<slug(cwd)>/<uuid>.jsonl (and a sibling <uuid>/ folder)
    into <registry dir>/trash/. Nothing is ever unlinked; an earlier trashed copy
    with the same name is kept (the new one gets a suffix). Returns the trashed
    transcript path, or None when there was no transcript."""
    u = e.get("uuid")
    if not (isinstance(u, str) and _UUID.fullmatch(u)):
        raise ValueError(f"bad uuid {u!r}")
    cwd = e.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    src_dir = os.path.join(projects_root or claude_projects_root(), cwd_slug(cwd))
    trash = os.path.join(os.path.dirname(os.path.abspath(lib_file or LIB_FILE)), "trash")
    out = None
    for name in (f"{u}.jsonl", u):
        src = os.path.join(src_dir, name)
        if not os.path.lexists(src):
            continue
        os.makedirs(trash, exist_ok=True)
        dst = _free_name(os.path.join(trash, name))
        shutil.move(src, dst)
        if name.endswith(".jsonl"):
            out = dst
    return out


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


def resolve(lib, text, archived=False):
    """For `/use <text>`: exact id, else id prefix, else name substring — among
    the topics outside the archive, or (archived=True) only among the archived."""
    q = (text or "").strip().casefold()
    if not q:
        return []
    live = [e for e in lib["sessions"] if bool(e.get("archived")) == bool(archived)]
    for rule in (lambda e: e["id"] == q, lambda e: e["id"].startswith(q),
                 lambda e: q in e["name"].casefold()):
        hits = [e for e in live if rule(e)]
        if hits:
            return hits
    return []


def _has_pos(e):
    p = e.get("pos")
    return isinstance(p, (int, float)) and not isinstance(p, bool)


def order_key(e, active_ids):
    """Active (loaded) first, then the rest; inside each group the manual order
    (`pos`, set by dragging on the page) first, then the others most recent first."""
    if _has_pos(e):
        return (e["id"] not in active_ids, 0, e["pos"])
    return (e["id"] not in active_ids, 1, -(e.get("last_used") or 0))


def display_order(lib, active_ids, include_archived=False):
    rows = [e for e in lib["sessions"] if include_archived or not e.get("archived")]
    return sorted(rows, key=lambda e: order_key(e, active_ids))


def reorder(lib, ids):
    """Manual order: ids[i] gets pos i. KeyError (nothing changed) if any id is unknown."""
    entries = [_get(lib, sid) for sid in ids]
    for i, e in enumerate(entries):
        e["pos"] = i


# ── LRU eviction ────────────────────────────────────────────────────────────
def needs_eviction(active_count, limit=MAX_ACTIVE):
    return active_count >= limit


def last_touched(s):
    """The later of last use (opened / typed to) and last screen output."""
    return max(s.get("last_used") or 0, s.get("last_output") or 0)


def pick_victim(live):
    """live: [{id, last_used, last_output, attached, working}] -> id to unload,
    or None. Candidates: no tab open and not working (a held session counts as
    working — the caller folds held() into `working`)."""
    idle = [s for s in live if not s["attached"] and not s["working"]]
    if not idle:
        return None
    return min(idle, key=last_touched)["id"]


# ── hold markers ────────────────────────────────────────────────────────────
# `.sessions/hold-<id>` holds a unix time: until then the session counts as
# working (LRU eviction and idle_reaper both keep it). Written when Claude sets
# a timer (hooks/hold_on_timer.py -> `library_cli.py hold`), so unloading does
# not kill a pending ScheduleWakeup / CronCreate / Monitor.
def hold_path(sid, lib_file=None):
    if not valid_id(sid):
        raise ValueError(f"bad session id {sid!r}")
    reg = lib_file or LIB_FILE
    return os.path.join(os.path.dirname(os.path.abspath(reg)), f"hold-{sid}")


def hold_until(sid, lib_file=None):
    """Expiry of the session's hold (unix time), or None."""
    try:
        with open(hold_path(sid, lib_file)) as f:
            return int(float(f.read().strip()))
    except (ValueError, OSError):
        return None


def held(sid, now, lib_file=None):
    u = hold_until(sid, lib_file)
    return u is not None and now < u


def set_hold(sid, until, lib_file=None):
    """Hold the session until `until`; an existing later hold is kept."""
    path = hold_path(sid, lib_file)
    until = int(until)
    cur = hold_until(sid, lib_file)
    if cur is not None and cur >= until:
        return cur
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write(f"{until}\n")
    os.replace(tmp, path)
    return until


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
