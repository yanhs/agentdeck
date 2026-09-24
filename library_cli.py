#!/usr/bin/env python3
"""Load topic-sessions into tmux on demand — the one entry point for the ttyd
page (open-session.sh) and the Telegram bridge.

    library_cli.py ensure <id>     print cs-<id> once it runs (already did, or
                                   started now, detached). Exit 0; 2 = unknown or
                                   archived id; 3 = all MAX_ACTIVE loaded ones are
                                   busy, nothing could be unloaded; 4 = this
                                   conversation's uuid already runs in another
                                   claude (outside cs-<id>) — a second one on the
                                   same transcript would corrupt it.
    library_cli.py active          JSON [{id, attached, working, last_output}]
                                   for every loaded cs-<id> session.
    library_cli.py pane-cmd <id>   print the command the pane would run (dry run).
    library_cli.py pane-is-claude <id>
                                   exit 0 only if cs-<id>'s pane is running claude
                                   right now (the bridge asks before typing text).
    library_cli.py hold <id|-> <seconds>
                                   keep the session counted as working until now +
                                   seconds (`-` = $AGENTDECK_SESSION); a longer
                                   existing hold is kept.

The id arrives from a URL (/sess/?arg=<id>), so it is checked before anything
else: 8 hex chars, present in the registry, not archived, and its uuid must be a
real uuid that starts with the id. Only those checked pieces reach the pane
command; the topic name (free text) never does.

At the limit (library.MAX_ACTIVE) the least recently used session that is idle
(no screen output for AGENTDECK_WORKING_SECONDS, default 1800, no live
background task — idle_reaper's test — and no unexpired hold marker) and has
no tab open is unloaded; "least recently" = the later of last_used and last
screen output
(`tmux kill-session`; the transcript stays on disk, `--resume` brings it back).

tmux: AGENTDECK_TMUX_SOCKET=<name> -> `tmux -L <name>` (tests use their own
server). Queries go through list-sessions / list-windows -F only: tmux 3.2a
crashes the whole server on `display-message -t =<name>` (no trailing colon)
with a window/time format. Targets are exact: `=name` for sessions, `=name:`
for panes.
"""
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import idle_reaper  # noqa: E402  (background-task detection, pure /proc walk)
import library  # noqa: E402

WORKDIR = os.getenv("AGENTDECK_WORKDIR") or os.path.dirname(HERE)
WORKING_SECONDS = int(os.getenv("AGENTDECK_WORKING_SECONDS", "1800"))
MAX_HOLD_SECONDS = 7 * 86400

EXIT_OK, EXIT_FAIL, EXIT_UNKNOWN, EXIT_BUSY, EXIT_ELSEWHERE = 0, 1, 2, 3, 4

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

USAGE = ("usage: library_cli.py ensure <id> | active | pane-cmd <id> | pane-is-claude <id>"
         " | hold <id|-> <seconds>")


# ── tmux ────────────────────────────────────────────────────────────────────
def tmux_argv(*args):
    sock = os.getenv("AGENTDECK_TMUX_SOCKET")
    return (["tmux", "-L", sock] if sock else ["tmux"]) + list(args)


def _clean_env():
    """Our env minus CLAUDE* (a Claude-spawned caller must not leak into the
    pane if this starts the tmux server) and TMUX (the socket is chosen above)."""
    return {k: v for k, v in os.environ.items() if "CLAUDE" not in k.upper() and k != "TMUX"}


def _tmux(*args):
    return subprocess.run(tmux_argv(*args), capture_output=True, text=True,
                          env=_clean_env(), timeout=15)


def _has(name):
    return _tmux("has-session", "-t", "=" + name).returncode == 0


def live_sessions(now=None, working_seconds=None):
    """Loaded library sessions: [{id, attached, working, last_output, pane_pid}]."""
    now = time.time() if now is None else now
    working_seconds = WORKING_SECONDS if working_seconds is None else working_seconds
    r = _tmux("list-sessions", "-F", "#{session_name}|#{session_attached}|#{pane_pid}")
    if r.returncode != 0:                          # no server = nothing loaded
        return []
    rows = {}
    for line in r.stdout.splitlines():
        try:
            name, att, pid = line.rsplit("|", 2)
        except ValueError:
            continue
        sid = library.id_from_tmux(name)
        if sid:
            rows[sid] = {"id": sid, "attached": att.isdigit() and int(att) > 0,
                         "pane_pid": int(pid) if pid.isdigit() else None, "last_output": None}
    # window_activity moves on pane output; session_activity does not (detached)
    w = _tmux("list-windows", "-a", "-F", "#{session_name}|#{window_activity}")
    for line in w.stdout.splitlines():
        name, _, act = line.rpartition("|")
        sid = library.id_from_tmux(name)
        if sid in rows and act.isdigit():
            rows[sid]["last_output"] = max(rows[sid]["last_output"] or 0, int(act))
    for s in rows.values():
        last = s["last_output"]
        recent = last is None or now - last < working_seconds   # unknown: assume busy
        s["working"] = bool(recent or library.held(s["id"], now)
                            or (s["pane_pid"] is not None
                                and idle_reaper.tree_has_task_output(s["pane_pid"])))
    return list(rows.values())


def pane_is_claude(sid):
    """True only if cs-<sid>'s pane is running claude now (not a bare shell)."""
    name = library.tmux_name(sid)
    r = _tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_current_command}")
    if r.returncode != 0:
        return False
    cmds = [c for n, _, c in (l.partition("\t") for l in r.stdout.splitlines()) if n == name]
    return bool(cmds) and all(c == "claude" for c in cmds)


# ── one claude per conversation ─────────────────────────────────────────────
def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _ppid(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _runs_uuid(argv, u):
    """argv is a claude started on conversation u: argv[0] is claude itself (a
    shell or the tmux server carrying `claude --resume u` as text is not), and
    the uuid follows --resume / --session-id (or is joined with `=`)."""
    if not argv or os.path.basename(argv[0]) != "claude":
        return False
    for i, a in enumerate(argv[1:], 1):
        if a in ("--resume", "-r", "--session-id") and i + 1 < len(argv) and argv[i + 1] == u:
            return True
        if a in (f"--resume={u}", f"--session-id={u}"):
            return True
    return False


def claude_processes(u):
    """[(pid, tmux session name or None)] of every running claude on uuid u."""
    me = os.getpid()
    pids = [int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) != me]
    hits = [p for p in pids if _runs_uuid(_cmdline(p), u)]
    if not hits:
        return []
    panes = {}
    r = _tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}")
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        n, _, pid = line.partition("\t")
        if pid.isdigit():
            panes[int(pid)] = n
    out = []
    for p in hits:
        cur, seen, where = p, set(), None
        while cur > 1 and cur not in seen:
            seen.add(cur)
            if cur in panes:
                where = panes[cur]
                break
            cur = _ppid(cur)
        out.append((p, where))
    return out


def claude_elsewhere(u, own_session):
    return [(p, w) for p, w in claude_processes(u) if w != own_session]


def active():
    return [{k: s[k] for k in ("id", "attached", "working", "last_output")}
            for s in live_sessions()]


# ── pane command ────────────────────────────────────────────────────────────
def slug(cwd):
    """Claude's project-dir name for a cwd: every non-alphanumeric char -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def effective_cwd(e):
    c = e.get("cwd")
    if isinstance(c, str) and os.path.isabs(c) and os.path.isdir(c):
        return c
    return WORKDIR


def transcript_path(home, cwd, u):
    return os.path.join(home, ".claude", "projects", slug(cwd), f"{u}.jsonl")


def checked_entry(e):
    """Raise ValueError unless id and uuid are well-formed and agree."""
    sid, u = e.get("id"), e.get("uuid")
    if not (library.valid_id(sid) and isinstance(u, str) and _UUID.fullmatch(u)
            and library.id_from_uuid(u) == sid):
        raise ValueError(f"corrupt registry entry {sid!r}")
    return sid, u


def pane_command(e, home=None, claude_bin=None):
    """The line typed into the new pane. Built only from checked pieces:
    the 8-hex id, a strict uuid, and quoted paths — never the topic name."""
    sid, u = checked_entry(e)
    home = home or os.path.expanduser("~")
    claude = (claude_bin or os.getenv("CLAUDE_BIN") or shutil.which("claude")
              or os.path.join(home, ".local", "bin", "claude"))
    have = os.path.isfile(transcript_path(home, effective_cwd(e), u))
    flag = "--resume" if have else "--session-id"
    oauth = shlex.quote(os.path.join(home, ".claude", "oauth.env"))
    return ('for v in $(env | cut -d= -f1 | grep -i CLAUDE); do unset "$v"; done; '
            f"[ -r {oauth} ] && . {oauth}; "
            f"export AGENTDECK_SESSION={sid}; "
            # exec: when claude exits the pane closes; a leftover shell prompt
            # would run whatever text the bridge types next as commands
            f"exec {shlex.quote(claude)} {flag} {u} --dangerously-skip-permissions")


# ── ensure ──────────────────────────────────────────────────────────────────
def _say(msg):
    print(msg, file=sys.stderr, flush=True)


def _clean(text):
    return _CTRL.sub("", str(text))[:120]


def _lookup(sid):
    """Registry entry for a usable id, else None. Read-only: never creates the file."""
    if not library.valid_id(sid):
        return None
    e = library.find(library.load(library.LIB_FILE), sid)
    if e is None or e.get("archived"):
        return None
    return e


@contextlib.contextmanager
def _ensure_lock():
    """One ensure at a time: two tabs opening topics at 11/12 must not both start."""
    with open(library.LIB_FILE + ".ensure.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def _unknown(sid):
    shown = f" {sid}" if library.valid_id(sid) else ""
    _say(f"unknown session{shown}: такой темы нет в библиотеке (или она в архиве). "
         "Откройте тему из списка на дашборде.")
    return EXIT_UNKNOWN


def _make_room(limit):
    """Unload LRU idle sessions until one more fits. False if none can go."""
    live = live_sessions()
    lib = library.load(library.LIB_FILE)
    while library.needs_eviction(len(live), limit):
        for s in live:
            e = library.find(lib, s["id"]) or {}
            s["last_used"] = e.get("last_used") or 0   # pick_victim: max(used, output)
        victim = library.pick_victim(live)
        if victim is None:
            return False
        _tmux("kill-session", "-t", "=" + library.tmux_name(victim))
        name = (library.find(lib, victim) or {}).get("name", "?")
        _say(f"выгружена тема {library.tmux_name(victim)} «{_clean(name)}» — давно не "
             "использовалась; переписка сохранена, откроется снова по клику.")
        live = [s for s in live if s["id"] != victim]
    return True


def _start(e, cmd):
    name = library.tmux_name(e["id"])
    r = _tmux("new-session", "-d", "-s", name, "-c", effective_cwd(e))
    if r.returncode != 0:
        if _has(name):                             # someone else just started it
            return
        raise RuntimeError(r.stderr.strip() or "tmux new-session failed")
    target = f"={name}:"                           # pane target needs the colon
    _tmux("send-keys", "-t", target, "-l", cmd)
    _tmux("send-keys", "-t", target, "Enter")


def ensure(sid, now=None):
    e = _lookup(sid)
    if e is None:
        return _unknown(sid)
    try:
        checked_entry(e)
    except ValueError:
        _say(f"unknown session {sid}: запись в библиотеке повреждена (uuid).")
        return EXIT_UNKNOWN
    name = library.tmux_name(sid)
    limit = library.MAX_ACTIVE
    with _ensure_lock():
        e = _lookup(sid)                           # may have been archived meanwhile
        if e is None:
            return _unknown(sid)
        if not _has(name):
            other = claude_elsewhere(e["uuid"], name)
            if other:
                where = ", ".join(f"pid {p} в tmux-сессии {w}" if w else f"pid {p} (вне tmux)"
                                  for p, w in other)
                _say(f"тема {name} уже открыта в другом Claude: {where}. Второй Claude на "
                     "ту же переписку испортит её — закройте тот или откройте тему там. "
                     f"(uuid already running elsewhere)")
                return EXIT_ELSEWHERE
            if not _make_room(limit):
                _say(f"Все {limit} загруженных тем сейчас заняты работой или открыты во "
                     "вкладках — новую загрузить некуда. Закройте вкладку или выгрузите "
                     f"тему и попробуйте снова. (all {limit} loaded sessions are busy)")
                return EXIT_BUSY
            try:
                _start(e, pane_command(e))
            except (RuntimeError, ValueError) as ex:
                _say(f"не удалось запустить {name}: {_clean(ex)}")
                return EXIT_FAIL
        try:
            with library.update(library.LIB_FILE) as lib:
                library.touch(lib, sid, int(time.time() if now is None else now))
        except KeyError:
            pass
    print(name, flush=True)
    return EXIT_OK


# ── hold ────────────────────────────────────────────────────────────────────
def hold(sid, seconds, now=None):
    if sid == "-":
        sid = os.getenv("AGENTDECK_SESSION", "")
    if not library.valid_id(sid):
        _say("hold: нужен 8-значный код темы (или '-' с AGENTDECK_SESSION).")
        return EXIT_UNKNOWN
    try:
        secs = int(seconds)
    except ValueError:
        secs = -1
    if secs <= 0:
        _say(f"hold: число секунд > 0, а не {_clean(seconds)!r}")
        return EXIT_FAIL
    now = time.time() if now is None else now
    until = library.set_hold(sid, int(now) + min(secs, MAX_HOLD_SECONDS), library.LIB_FILE)
    print(until, flush=True)
    return EXIT_OK


# ── main ────────────────────────────────────────────────────────────────────
def main(argv):
    try:
        return _main(argv)
    except library.CorruptRegistry as ex:
        _say(f"реестр тем повреждён, ничего не делаю: {ex}")
        return EXIT_FAIL


def _main(argv):
    if len(argv) == 3 and argv[0] == "hold":
        return hold(argv[1], argv[2])
    if len(argv) == 2 and argv[0] == "pane-is-claude":
        if not library.valid_id(argv[1]):
            return _unknown(argv[1])
        return EXIT_OK if pane_is_claude(argv[1]) else EXIT_FAIL
    if len(argv) == 2 and argv[0] == "ensure":
        return ensure(argv[1])
    if len(argv) == 1 and argv[0] == "active":
        print(json.dumps(active()))
        return EXIT_OK
    if len(argv) == 2 and argv[0] == "pane-cmd":
        e = _lookup(argv[1])
        if e is None:
            return _unknown(argv[1])
        try:
            print(pane_command(e))
        except ValueError:
            return _unknown(argv[1])
        return EXIT_OK
    _say(USAGE)
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
