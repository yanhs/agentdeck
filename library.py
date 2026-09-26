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
import glob
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


def find_or_alias(lib, sid):
    """The entry with this id, else the one that took this number over (its
    `aliases`: numbers of conversations that never were one — see switch())."""
    e = find(lib, sid)
    if e is not None:
        return e
    for e in lib["sessions"]:
        if sid in (e.get("aliases") or ()):
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
    while uuid is None and find_or_alias(lib, id_from_uuid(u)):   # collided: redraw
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


# ── one number per terminal ─────────────────────────────────────────────────
# A terminal and its conversation are one thing with one number. When the
# conversation live in terminal A becomes B — Claude's bypass-permissions consent
# relaunches Claude without --session-id, /clear starts a new conversation,
# /resume switches to another — the terminal becomes B (convo_sync.py notices and
# calls switch(); the tmux session cs-A is renamed cs-B).
ALIASES_MAX = 20
EARLIER = " (earlier)"


def _with_aliases(e, ids):
    have = [a for a in (e.get("aliases") or []) if valid_id(a)]
    for a in ids:
        if valid_id(a) and a != e["id"] and a not in have:
            have.append(a)
    if have:
        e["aliases"] = have[-ALIASES_MAX:]


def switch(lib, old, new_uuid, old_has_messages, now):
    """Terminal `old` now runs conversation `new_uuid` (number B). Returns B's entry.

    - B unknown, A never held a conversation (the consent relaunch): A's entry
      itself becomes B — name, folder, place in the list, archive state kept;
      A's number stays reachable as an alias of B (old links).
    - B unknown, A has messages (/clear after work): a new entry B carries the
      terminal on (A's name, folder, created, last_used, archived, place); A stays
      as its own row, "… (earlier)", out of the manual order.
    - B already in the library (/resume of a listed conversation): B is brought
      back (out of the archive) with its own name; A goes (its number becomes an
      alias of B) when it never held a conversation, else stays as it is.
    B's number taken by another conversation (an 8-hex collision) -> ValueError,
    nothing changed. Every entry keeps id == uuid[:8]."""
    if not (isinstance(new_uuid, str) and _UUID.fullmatch(new_uuid)):
        raise ValueError(f"bad uuid {new_uuid!r}")
    a = _get(lib, old)
    new = id_from_uuid(new_uuid)
    b = find(lib, new)
    if b is not None and b.get("uuid") != new_uuid:
        raise ValueError(f"number {new} is already another conversation ({b.get('uuid')})")
    if b is a:
        return a                                    # nothing switched
    moved = {"prev_id": old, "switched_at": now}
    if b is None and not old_has_messages:
        a.update(id=new, uuid=new_uuid, **moved)
        _with_aliases(a, [old])
        return a
    if b is None:
        b = {"id": new, "uuid": new_uuid, **{k: a[k] for k in (
            "name", "cwd", "created", "last_used", "archived") if k in a}, **moved}
        if _has_pos(a):
            b["pos"] = a.pop("pos")
        a["name"] = a["name"][:200 - len(EARLIER)] + EARLIER
        lib["sessions"].insert(lib["sessions"].index(a), b)
        return b
    b.update(archived=False, last_used=now, **moved)
    if not old_has_messages:
        lib["sessions"].remove(a)
        _with_aliases(b, [old] + list(a.get("aliases") or []))
    return b


TRANSCRIPT_SCAN_MAX = 4 * 1024 * 1024
# What Claude Code 2.1.283 writes as "user" records that are not a conversation:
# a slash command (/clear itself is the first record of the conversation it
# starts) and its output. A new conversation right after /clear holds only these.
_NOT_A_MESSAGE = ("<command-name>", "<command-message>", "<local-command-stdout>",
                  "<local-command-stderr>", "<local-command-caveat>")


def _is_message(d):
    kind = d.get("type")
    if kind not in ("user", "assistant") or d.get("isMeta"):
        return False
    m = d.get("message")
    if kind == "assistant":
        return not (isinstance(m, dict) and m.get("model") == "<synthetic>")
    c = m.get("content") if isinstance(m, dict) else None
    return not (isinstance(c, str) and c.lstrip().startswith(_NOT_A_MESSAGE))


def has_messages(path, limit=TRANSCRIPT_SCAN_MAX):
    """True when the transcript at `path` holds a conversation: a prompt or a
    model reply — not only summaries, snapshots, slash commands and their output.
    Reads at most `limit` bytes; a missing or unreadable file is False."""
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
    except OSError:
        return False
    for line in data.splitlines():
        if b'"user"' not in line and b'"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(d, dict) and _is_message(d):
            return True
    return False


# ── transcripts → trash (moved, never unlinked) ─────────────────────────────
def claude_projects_root():
    """~/.claude/projects, or $AGENTDECK_CLAUDE_PROJECTS (tests)."""
    return (os.getenv("AGENTDECK_CLAUDE_PROJECTS")
            or os.path.join(os.path.expanduser("~"), ".claude", "projects"))


SLUG_MAX = 200                     # Claude Code cuts longer project-dir names


def _utf16_units(s):
    b = s.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


def cwd_slug(cwd):
    """Claude Code's project-dir name for a cwd (its qx() in 2.1.283): every
    non-alphanumeric UTF-16 code unit -> '-' (so an emoji becomes '--'); a name
    over 200 chars is cut and gets '-' + base 36 of |Java string hash of cwd|."""
    units = _utf16_units(cwd)
    name = "".join(chr(u) if chr(u).isascii() and chr(u).isalnum() else "-" for u in units)
    if len(name) <= SLUG_MAX:
        return name
    h = 0
    for u in units:                                # (h << 5) - h + u, as a signed int32
        h = (h * 31 + u) & 0xFFFFFFFF
    h = abs(h - (1 << 32) if h >= 1 << 31 else h)
    digits = ""
    while True:
        h, r = divmod(h, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + digits
        if not h:
            break
    return f"{name[:SLUG_MAX]}-{digits}"


def transcript_file(projects_root, cwd, u):
    """<projects_root>/<cwd_slug(cwd)>/<u>.jsonl. For a cut (over-200) name,
    Claude also accepts any '<first 200 chars>-*' folder, so the file is looked
    for there too; the exact path is returned when there is none anywhere."""
    exact = os.path.join(projects_root, cwd_slug(cwd), f"{u}.jsonl")
    name = os.path.basename(os.path.dirname(exact))
    if len(name) <= SLUG_MAX or os.path.lexists(exact):
        return exact
    found = sorted(glob.glob(os.path.join(glob.escape(projects_root),
                                          glob.escape(name[:SLUG_MAX]) + "-*",
                                          glob.escape(f"{u}.jsonl"))))
    return found[0] if found else exact


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
    src_dir = os.path.dirname(transcript_file(projects_root or claude_projects_root(), cwd, u))
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
    for rule in (lambda e: e["id"] == q or q in (e.get("aliases") or ()),
                 lambda e: e["id"].startswith(q),
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


def move_hold(old, new, lib_file=None):
    """The terminal's number changed (switch()): its hold goes with it."""
    until = hold_until(old, lib_file)
    if until is not None:
        set_hold(new, until, lib_file)
    with contextlib.suppress(OSError):
        os.unlink(hold_path(old, lib_file))


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
