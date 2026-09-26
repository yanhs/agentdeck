#!/usr/bin/env python3
"""One number per terminal: a terminal takes the number of the conversation live in it.

A terminal and its conversation are one thing with one number (the owner's rule,
2026-09-26): tmux cs-<id>, the registry entry, links, the task board and Telegram
all use the first 8 hex of the Claude conversation running in it. Claude changes
the conversation under a running terminal three ways:
  * its bypass-permissions consent: on "yes" Claude relaunches itself WITHOUT the
    --session-id AgentDeck gave it, and the conversation gets a new id;
  * /clear starts a new conversation in the same process;
  * /resume <other> switches the process to another conversation.

Claude Code writes ~/.claude/sessions/<pid>.json for every live process; its
`sessionId` is the conversation live in that process NOW (2.1.283 rewrites it on
/clear and /resume; CLAUDE_CODE_SESSION_ID in the Bash tool follows too — both seen
on a live probe). sync() reads those files, finds which cs-<id> pane each Claude
runs in, and when pane cs-A runs conversation B it makes the terminal B:
library.switch() moves the registry entry, the tmux session is renamed cs-A -> cs-B
(an attached browser tab stays attached: tmux clients follow the session, not its
name), and the hold marker moves. The process is never touched.

Which pane a Claude belongs to comes from the process tree, not from the file's
`tmux` field: that names no tmux server, and it keeps the session's name from when
Claude started (after our own rename it is stale — seen on the probe). A file
counts when its pid is alive and still the Claude that wrote it (`procStart` =
field 22 of /proc/<pid>/stat), it is an interactive CLI Claude, and walking up its
parents reaches the first pane of a cs-<id> session — the one AgentDeck started; a
Claude in a pane opened later in the session (split-window, new-window) is not the
terminal's — whose terminal it runs on (same controlling tty: a
Claude that some command in the pane started on a pty of its own, e.g. under
`script`, is not the terminal's conversation). Two files on one pane (Claude
relaunched as a child of itself): the later-started one is the live one.

Rules for the old number A:
  * A never held a conversation (the consent case): the entry itself becomes B and
    keeps A as an alias, so old links to A land on B;
  * A has a transcript with messages (/clear after work): A stays in the list as
    its own unloaded terminal, "… (earlier)";
  * B already open in another terminal cs-B (/resume of a conversation that is open
    elsewhere): nothing is merged, renamed or killed — logged once.

Who calls sync(): status_server (every listing poll, lock="try"; before close /
archive / delete / rename), library_cli (ensure, active — lock=False inside the
ensure lock), idle_reaper (before a sweep). Lock order is the ensure lock, then the
registry lock (library.update) — the same as library_cli.ensure — and the ensure
lock is held from the check through the rename, so ensure(B) cannot start a second
cs-B in between. Nothing to switch: no lock, no write.
"""
import contextlib
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import library  # noqa: E402

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_PIDFILE = re.compile(r"(\d+)\.json")
MAX_WALK = 12                      # parent steps from a Claude up to its pane
PIDFILE_MAX = 64 * 1024
CLAUDE_NAMES = ("claude", "claude.exe")   # argv[0]: the native build, the npm one

_logged = set()


def log(msg, key=None):
    """One line on stderr (the caller's log); with `key`, once per process."""
    if key is not None:
        if key in _logged:
            return
        _logged.add(key)
    print(f"convo_sync: {msg}", file=sys.stderr, flush=True)


def sessions_dir():
    return (os.getenv("AGENTDECK_CLAUDE_SESSIONS")
            or os.path.join(os.path.expanduser("~"), ".claude", "sessions"))


# ── /proc ───────────────────────────────────────────────────────────────────
def _stat(pid):
    """Fields of /proc/<pid>/stat after the command name: [0] state, [1] ppid,
    [4] tty_nr, [19] starttime. None when the process is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None


def _argv0(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return os.path.basename(f.read().split(b"\0", 1)[0].decode("utf-8", "replace"))
    except OSError:
        return ""


# ── live Claude processes ───────────────────────────────────────────────────
def live_files(directory=None):
    """[{pid, uuid, start}] for every live interactive Claude CLI process, from
    its pid file. Skips: a dead pid; a pid now used by another process (its start
    time differs from the file's procStart; without procStart, argv[0] must be
    claude); `claude -p` and other non-terminal kinds; anything unreadable."""
    directory = directory or sessions_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    out = []
    for name in names:
        m = _PIDFILE.fullmatch(name)
        if not m:
            continue
        try:
            with open(os.path.join(directory, name), "rb") as f:
                d = json.loads(f.read(PIDFILE_MAX))
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(d, dict):
            continue
        pid, u = d.get("pid"), d.get("sessionId")
        if (not isinstance(pid, int) or isinstance(pid, bool) or pid != int(m.group(1))
                or not (isinstance(u, str) and _UUID.fullmatch(u))
                or d.get("kind") != "interactive" or d.get("entrypoint") != "cli"):
            continue
        st = _stat(pid)
        if st is None or len(st) < 20:
            continue
        ps = d.get("procStart")
        if isinstance(ps, str) and ps:
            if ps != st[19]:
                continue
        elif _argv0(pid) not in CLAUDE_NAMES:
            continue
        out.append({"pid": pid, "uuid": u, "start": int(st[19])})
    return out


def _default_run(*args):
    sock = os.getenv("AGENTDECK_TMUX_SOCKET")
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    return subprocess.run(["tmux", *(["-L", sock] if sock else []), *args],
                          capture_output=True, text=True, env=env, timeout=15)


_PANE_ID = re.compile(r"%(\d+)")


def terminal_panes(run=None):
    """{pane pid: "cs-<id>"} — the pane AgentDeck started in each library session:
    its first pane (the lowest pane id; tmux numbers panes in creation order,
    server-wide). A pane opened later in the session (split-window, new-window —
    by hand, or by the pane's Claude through its Bash tool, where $TMUX points at
    this session) is not the terminal, and a Claude started there is not its
    conversation. One tmux call."""
    run = run or _default_run
    r = run("list-panes", "-a", "-F", "#{session_name}\t#{pane_id}\t#{pane_pid}")
    if getattr(r, "returncode", 1) != 0:
        return {}
    first = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, pane_id, pid = parts
        m = _PANE_ID.fullmatch(pane_id)
        if not (m and library.id_from_tmux(name) and pid.isdigit()):
            continue
        n = int(m.group(1))
        if name not in first or n < first[name][0]:
            first[name] = (n, int(pid))
    return {pid: name for name, (_, pid) in first.items()}


def pane_conversations(run=None, files=None):
    """{"cs-<id>": uuid of the conversation live in that pane}, for the library
    terminals (their first pane, see terminal_panes) whose Claude has a live pid
    file. One tmux call."""
    panes = terminal_panes(run)
    if not panes:
        return {}
    files = live_files() if files is None else files
    found = {}
    for f in files:
        cur = f["pid"]
        for _ in range(MAX_WALK + 1):
            if cur in panes or cur <= 1:
                break
            st = _stat(cur)
            cur = int(st[1]) if st and st[1].isdigit() else 0
        if cur not in panes:
            continue
        mine, pane = _stat(f["pid"]), _stat(cur)
        if not (mine and pane and mine[4] == pane[4]):      # its own pty: not the terminal's
            continue
        found.setdefault(panes[cur], []).append(f)
    out = {}
    for name, fs in found.items():
        best = max(fs, key=lambda f: (f["start"], f["pid"]))
        if len({f["uuid"] for f in fs}) > 1:
            log(f"{len(fs)} Claude processes in {name} (pids "
                f"{', '.join(str(f['pid']) for f in fs)}); the newest, pid {best['pid']}, "
                f"is the terminal's conversation {library.id_from_uuid(best['uuid'])}",
                key=("several", name, tuple(sorted((f["pid"], f["uuid"]) for f in fs))))
        out[name] = best["uuid"]
    return out


# ── plan / apply ────────────────────────────────────────────────────────────
def plan(lib, panes):
    """[(A, uuid)] for every pane cs-A running a conversation whose number is not A,
    when there is something to do: A is in the registry, or the registry already
    holds that conversation (then only the tmux name is behind). A cs-<id> session
    that is not a library terminal is left alone."""
    out = []
    for name, u in sorted(panes.items()):
        a = library.id_from_tmux(name)
        if a is None or library.id_from_uuid(u) == a:
            continue
        if library.find(lib, a) is None and not any(e.get("uuid") == u for e in lib["sessions"]):
            log(f"{name} runs conversation {u}, but neither is in the library; left as is",
                key=("stray", name, u))
            continue
        out.append((a, u))
    return out


def _transcript(projects_root, e):
    """A's transcript: where its folder puts it, else wherever it is (a transcript
    is named by its uuid, so a folder that differs still finds it)."""
    u = e["uuid"]
    exact = library.transcript_file(projects_root, e.get("cwd") or "/", u)
    if os.path.exists(exact):
        return exact
    hits = glob.glob(os.path.join(glob.escape(projects_root), "*", glob.escape(u + ".jsonl")))
    return hits[0] if hits else exact


def _apply(run, lib_file, a, u, now, projects_root):
    b = library.id_from_uuid(u)
    old, new = library.tmux_name(a), library.tmux_name(b)
    if run("has-session", "-t", "=" + new).returncode == 0:
        log(f"conversation {b} is open in {old} and {new}; left as is (a conversation "
            "must not run in two terminals — close one of them)", key=("both", a, b))
        return None
    lib = library.load(lib_file)
    e = library.find(lib, a)
    if e is not None:
        msgs = library.has_messages(_transcript(projects_root, e))
        try:
            with library.update(lib_file) as lib:
                library.switch(lib, a, u, msgs, now)
        except (KeyError, ValueError) as ex:
            log(f"{old} now runs conversation {u}; not switched: {ex}", key=("refused", a, u))
            return None
    elif not any(x.get("uuid") == u for x in lib["sessions"]):
        return None                                   # gone meanwhile
    r = run("rename-session", "-t", "=" + old, new)
    if r.returncode != 0:
        log(f"couldn't rename {old} to {new} ({(r.stderr or '').strip()[:120]}); "
            "the next sync tries again")
        return None
    library.move_hold(a, b, lib_file)
    log(f"terminal {a} is now {b}: its conversation changed (consent relaunch, /clear or "
        f"/resume); tmux {old} renamed {new}")
    return (a, b)


@contextlib.contextmanager
def _ensure_lock(lib_file, lock):
    """library_cli's ensure lock. lock=True waits, "try" gives up at once
    (yields False), False = the caller already holds it."""
    if lock is False:
        yield True
        return
    os.makedirs(os.path.dirname(os.path.abspath(lib_file)), exist_ok=True)
    with open(lib_file + ".ensure.lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | (fcntl.LOCK_NB if lock == "try" else 0))
        except BlockingIOError:
            yield False
            return
        yield True


def sync(run=None, lib_file=None, lock=True, now=None, projects_root=None):
    """Make every library terminal carry the number of the conversation live in
    it. Returns the switches made, [(A, B)]. Reads first without any lock; only a
    needed switch takes the ensure lock and writes."""
    run = run or _default_run
    lib_file = lib_file or os.getenv("AGENTDECK_LIBRARY") or library.LIB_FILE
    projects_root = projects_root or library.claude_projects_root()
    panes = pane_conversations(run)
    if not panes or not plan(library.load(lib_file), panes):
        return []
    done = []
    with _ensure_lock(lib_file, lock) as got:
        if not got:
            return []
        for a, u in plan(library.load(lib_file), pane_conversations(run)):
            r = _apply(run, lib_file, a, u, int(time.time() if now is None else now),
                       projects_root)
            if r:
                done.append(r)
    return done


if __name__ == "__main__":
    for a, b in sync():
        print(f"{a} -> {b}")
