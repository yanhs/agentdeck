#!/usr/bin/env python3
"""Load topic-sessions into tmux on demand — the one entry point for the ttyd
page (open-session.sh) and the Telegram bridge.

    library_cli.py ensure <id>     print cs-<id> once it runs (already did, or
                                   started now, detached); for an old number of a
                                   terminal, the terminal's cs-<number now>. Exit 0; 2 = unknown or
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
                                   seconds (`-` = this pane's terminal: the live
                                   conversation, $CLAUDE_CODE_SESSION_ID, inside a
                                   library pane, $AGENTDECK_SESSION); a longer
                                   existing hold is kept.
    library_cli.py shell-ensure    print cmd-shell once the dashboard's one plain
                                   command line runs (`bash -l` in WORKDIR, mouse
                                   on; started detached if missing). Not a topic:
                                   not counted toward MAX_ACTIVE, never unloaded
                                   to make room. Exit 0; 1 = tmux failed.

The id arrives from a URL (/sess/?arg=<id>), so it is checked before anything
else: 8 hex chars, present in the registry, not archived, and its uuid must be a
real uuid that starts with the id. Only those checked pieces reach the pane
command; the topic name (free text) never does.

One number per terminal: a terminal's number is the conversation live in it. When
Claude switches the conversation under a running terminal (its bypass-permissions
consent relaunch, /clear, /resume) convo_sync renames cs-A to cs-B and moves the
registry entry; ensure and active sync first. An old number that never was a
conversation of its own is an alias of the terminal that took it over: `ensure A`
prints cs-B.

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
for panes. Every call carries `-f <repo>/tmux.conf` (AgentDeck's mouse/copy
settings, then the user's ~/.tmux.conf); a server started without it gets it
once from ensure / shell-ensure. A new pane runs `bin/agentdeck-pane <id|shell>`
from the pane's folder — nothing else on the command line, which the tmux server
keeps as its own (see LAUNCHER: a `pkill -f grep` must not match it).
"""
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import convo_sync  # noqa: E402  (a terminal follows its conversation's number)
import idle_reaper  # noqa: E402  (background-task detection, pure /proc walk)
import library  # noqa: E402

WORKDIR = os.getenv("AGENTDECK_WORKDIR") or os.path.dirname(HERE)
WORKING_SECONDS = int(os.getenv("AGENTDECK_WORKING_SECONDS", "1800"))
MAX_HOLD_SECONDS = 7 * 86400

EXIT_OK, EXIT_FAIL, EXIT_UNKNOWN, EXIT_BUSY, EXIT_ELSEWHERE = 0, 1, 2, 3, 4

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

USAGE = ("usage: library_cli.py ensure <id> | active | pane-cmd <id> | pane-is-claude <id>"
         " | hold <id|-> <seconds> | shell-ensure")


# ── tmux ────────────────────────────────────────────────────────────────────
# AgentDeck's tmux settings (mouse selection, copy on release into the buffer the
# dashboard reads, scrollback). tmux reads -f only when that command starts the
# server, so every call carries it: whichever call comes first starts the server
# with it. A server someone else started gets it once from ensure_tmux_conf().
TMUX_CONF = os.path.join(HERE, "tmux.conf")
TMUX_CONF_MARK = "@agentdeck-conf"                 # set by tmux.conf itself


def tmux_argv(*args):
    sock = os.getenv("AGENTDECK_TMUX_SOCKET")
    base = ["tmux", "-L", sock] if sock else ["tmux"]
    if os.path.isfile(TMUX_CONF):
        base += ["-f", TMUX_CONF]
    return base + list(args)


# What a new pane runs. tmux keeps, as the SERVER's own command line, the command line of
# the client that started it — whichever new-session came first. A careless `pkill -f
# grep` (or claude, bash, env, …) anywhere on the machine matches that line and kills
# the server with every terminal in it (2026-09-26: `pkill -f "… | grep"` did). So every
# new-session here is only `tmux [-L sock] -f <conf> new-session -d -s <name> LAUNCHER
# <id|shell>`: the pane's folder is the tmux client's working directory (a detached
# new-session starts there) instead of `-c <folder>` — a folder like ~/claude-code-bot
# would put the word back — and bin/agentdeck-pane does the rest inside the pane.
LAUNCHER = os.path.join(HERE, "bin", "agentdeck-pane")


def _clean_env():
    """Our env minus CLAUDE* (a Claude-spawned caller must not leak into the
    pane if this starts the tmux server) and TMUX (the socket is chosen above)."""
    return {k: v for k, v in os.environ.items() if "CLAUDE" not in k.upper() and k != "TMUX"}


def _tmux(*args, cwd=None):
    """cwd: the folder a new-session starts its pane in (see LAUNCHER); a folder that
    is gone falls back to home, as tmux does for a bad -c."""
    if cwd is not None and not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    return subprocess.run(tmux_argv(*args), capture_output=True, text=True,
                          env=_clean_env(), timeout=15, cwd=cwd)


def _has(name):
    return _tmux("has-session", "-t", "=" + name).returncode == 0


def ensure_tmux_conf(run=None):
    """Source tmux.conf into a running server that has not read it (started
    without our -f: by hand, by an older AgentDeck). Once per server: the file
    sets TMUX_CONF_MARK. No server running: nothing — the next call starts one
    with -f. Show-options, not display-message (see the module docstring).
    run: the caller's tmux runner (the bridge passes its own)."""
    run = run or _tmux
    if not os.path.isfile(TMUX_CONF):
        return
    r = run("show-options", "-gqv", TMUX_CONF_MARK)
    if getattr(r, "returncode", 1) != 0 or (getattr(r, "stdout", "") or "").strip():
        return
    run("source-file", TMUX_CONF)


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
    """True only if cs-<sid>'s pane is running claude now (not a bare shell). The
    npm package's binary is claude.exe (a consent relaunch re-executes that)."""
    name = library.tmux_name(sid)
    r = _tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_current_command}")
    if r.returncode != 0:
        return False
    cmds = [c for n, _, c in (l.partition("\t") for l in r.stdout.splitlines()) if n == name]
    return bool(cmds) and all(c in convo_sync.CLAUDE_NAMES for c in cmds)


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
    if not argv or os.path.basename(argv[0]) not in convo_sync.CLAUDE_NAMES:
        return False
    for i, a in enumerate(argv[1:], 1):
        if a in ("--resume", "-r", "--session-id") and i + 1 < len(argv) and argv[i + 1] == u:
            return True
        if a in (f"--resume={u}", f"--session-id={u}"):
            return True
    return False


def claude_processes(u):
    """[(pid, tmux session name or None)] of every running claude on uuid u.

    A claude with a live pid file (~/.claude/sessions, convo_sync.live_files) runs
    the conversation that file names — after /clear or /resume its command line
    (`--resume <old>`) is stale; only a claude without one is judged by its
    command line (--resume / --session-id)."""
    me = os.getpid()
    live = {f["pid"]: f["uuid"] for f in convo_sync.live_files()}
    pids = [int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) != me]
    hits = [p for p in pids
            if (live[p] == u if p in live else _runs_uuid(_cmdline(p), u))]
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


def sync(lock=True):
    """convo_sync.sync on our tmux and registry; lock=False inside _ensure_lock.
    A failure other than a corrupt registry is reported and ignored."""
    try:
        return convo_sync.sync(run=_tmux, lib_file=library.LIB_FILE, lock=lock)
    except library.CorruptRegistry:
        raise
    except Exception as ex:  # noqa: BLE001 — never stop an open over it
        _say(f"couldn't check which conversation each terminal runs: {_clean(ex)}")
        return []


def active():
    sync()
    return [{k: s[k] for k in ("id", "attached", "working", "last_output")}
            for s in live_sessions()]


# ── pane command ────────────────────────────────────────────────────────────
def slug(cwd):
    """Claude's project-dir name for a cwd (library.cwd_slug: UTF-16, 200-char cap)."""
    return library.cwd_slug(cwd)


def effective_cwd(e):
    c = e.get("cwd")
    if isinstance(c, str) and os.path.isabs(c) and os.path.isdir(c):
        return c
    return WORKDIR


def transcript_path(home, cwd, u):
    return library.transcript_file(os.path.join(home, ".claude", "projects"), cwd, u)


# ── the terminal's own session-only effort ──────────────────────────────────
# Claude Code saves /effort low|medium|high|xhigh as the default for new sessions,
# so those come back on their own. ultracode and max are "this session only": a
# plain --resume drops them. Relaunch with the one the terminal ended at (never
# any other level — launch stays a bare --resume otherwise). The transcript shows
# it three ways, the newest evidence wins:
#   - a slash command's output record (/effort, the /model picker);
#   - an ultra_effort_enter / ultra_effort_exit attachment, written on the next
#     user turn after ultracode goes on or off, whatever turned it (/config,
#     the Alt+P picker and Remote Control print no effort text);
#   - the effort every model reply is stamped with (max has no attachment).
EFFORT_FLAGS = {"ultracode": ("--settings", '{"ultracode":true}'), "max": ("--effort", "max")}
EFFORT_CHUNK = 256 * 1024          # bytes read per step, from the end of the file
EFFORT_MAX_LINE = 1024 * 1024      # a command record is ~600 bytes; longer lines are output
_STDOUT = "<local-command-stdout>"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_LEVEL = r"`?([A-Za-z]+)`?"
_EFFORT_OUTPUTS = [                # Claude Code 2.1.283's messages, first match wins
    (re.compile(r"Set effort level to " + _LEVEL + r"\b"), None),
    (re.compile(r"(?:Effort level set to auto|Effort set to auto for this session"
                r"|Cleared effort from settings)\b"), "auto"),
    (re.compile(r"Effort '[^']*' exceeds the cap\b.*?; set to '([A-Za-z]+)' instead"), None),
    (re.compile(r"Not applied: CLAUDE_CODE_EFFORT_LEVEL=\S* overrides effort this session, "
                r"and " + _LEVEL + r" is session-only"), None),
    (re.compile(r"CLAUDE_CODE_EFFORT_LEVEL=\S* overrides (?:effort )?this session\W+"
                r"clear it and " + _LEVEL + r" takes over"), None),
    (re.compile(r"Set model to .*? with " + _LEVEL + r" effort\b"), None),   # the /model picker
]


def effort_from_output(text):
    """The effort a slash command's output says the session now runs at
    ('ultracode', 'max', 'medium', 'auto', …), or None if it changed none."""
    t = _ANSI.sub("", text)
    t = t[len(_STDOUT):] if t.startswith(_STDOUT) else t
    for pat, fixed in _EFFORT_OUTPUTS:
        mt = pat.match(t.lstrip())
        if mt:
            return fixed or mt.group(1).lower()
    return None


_ULTRA_ATTACHMENTS = {"ultra_effort_enter": True, "ultra_effort_exit": False}


def _record_effort(line, replies=True):
    """What one transcript line (bytes) says about the effort, or None:
      ("command", level)  a slash command's output set `level`;
      ("ultracode", bool) an ultra_effort_enter / _exit attachment;
      ("reply", level)    a model reply ran at `level` (only when `replies`).
    Only real records of the main chain count (not a sidechain; a command record
    is a user line whose content is a string starting with the output tag). The
    same words inside a tool result or a reply's text are not a record."""
    command = _STDOUT.encode() in line and (b"ffort" in line or b"EFFORT" in line)
    attach = b"ultra_effort_" in line
    reply = replies and b'"effort"' in line and b'"assistant"' in line
    if not (command or attach or reply):
        return None
    try:
        d = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(d, dict) or d.get("isSidechain"):
        return None
    kind = d.get("type")
    if kind == "attachment":
        a = d.get("attachment")
        on = _ULTRA_ATTACHMENTS.get(a.get("type")) if isinstance(a, dict) else None
        return None if on is None else ("ultracode", on)
    if kind == "assistant":
        e = d.get("effort")
        return ("reply", e.lower()) if replies and isinstance(e, str) and e else None
    if kind != "user":
        return None
    msg = d.get("message")
    c = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(c, str) or not c.startswith(_STDOUT):
        return None
    level = effort_from_output(c)
    return ("command", level) if level else None


def _read_at(f, pos, n):
    f.seek(pos)
    return f.read(n)


def _lines_from_end(f, size, chunk):
    """Complete lines of f, last first, reading `chunk` bytes at a time from the
    end. A line longer than EFFORT_MAX_LINE is skipped instead of carried."""
    pos, carry, skipping = size, b"", False
    while pos > 0:
        n = min(chunk, pos)
        pos -= n
        buf = _read_at(f, pos, n)
        if len(buf) != n:                          # the file shrank under us
            return
        parts = (buf + carry).split(b"\n")
        carry = parts.pop(0)                       # may continue in the chunk before
        if skipping:
            if not parts:                          # still inside the long line
                carry = b""
                continue
            parts.pop()                            # the long line's head
            skipping = False
        yield from reversed(parts)
        if len(carry) > EFFORT_MAX_LINE:
            carry, skipping = b"", True
    if carry and not skipping:
        yield carry


def _effort_at_end(events):
    """The effort a session ended at, from its effort events newest first:
    'ultracode' if the newest ultracode evidence (a command or an attachment)
    says on; otherwise the newest level (a command or a reply), or None.
    Stops reading as soon as the answer is known."""
    ultra = level = None
    for kind, v in events:
        if kind == "ultracode":
            ultra = v if ultra is None else ultra
        elif kind == "reply":
            level = v if level is None else level
        else:                                      # a command answers both
            ultra = (v == "ultracode") if ultra is None else ultra
            level = ("xhigh" if v == "ultracode" else v) if level is None else level
        if ultra or (ultra is False and level is not None):
            break
    return "ultracode" if ultra else level


def last_effort(path, chunk=None):
    """The effort the terminal ended at, as its transcript `path` shows it
    ('ultracode', 'max', 'medium', …), or None (no sign, or the file cannot be
    read). Reads backwards, stops once the newest evidence settles it; never
    blocks on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        regular = stat.S_ISREG(st.st_mode)         # not a directory, a FIFO, a device
    except OSError:
        regular = False
    if not regular:
        os.close(fd)
        return None
    with os.fdopen(fd, "rb") as f:
        return _effort_at_end(_effort_events(f, st.st_size, chunk or EFFORT_CHUNK))


def _effort_events(f, size, chunk):
    """_record_effort over f's lines, last first. Once a level is known (a reply
    or a command), older replies no longer matter and are not parsed."""
    replies = True
    for line in _lines_from_end(f, size, chunk):
        ev = _record_effort(line, replies)
        if ev:
            replies = replies and ev[0] == "ultracode"
            yield ev


def effort_flags(path):
    """claude arguments that bring back the terminal's own session-only effort
    (ultracode or max); [] for anything else. Any failure -> [] — reading the
    transcript must never stop a terminal from starting."""
    try:
        return list(EFFORT_FLAGS.get(last_effort(path), ()))
    except Exception:  # noqa: BLE001 — best effort by design, see above
        return []


def checked_entry(e):
    """Raise ValueError unless id and uuid are well-formed and agree."""
    sid, u = e.get("id"), e.get("uuid")
    if not (library.valid_id(sid) and isinstance(u, str) and _UUID.fullmatch(u)
            and library.id_from_uuid(u) == sid):
        raise ValueError(f"corrupt registry entry {sid!r}")
    return sid, u


def pane_argv(e, home=None, claude_bin=None):
    """claude's command line for the topic: [claude, --resume|--session-id, uuid,
    --dangerously-skip-permissions, the session's own effort…]. Built only from
    checked pieces: a strict uuid that starts with the 8-hex id, the claude path and
    fixed flags — never the topic name. A resumed conversation keeps its own
    session-only effort (effort_flags). The claude path is found here, in the
    caller's environment (CLAUDE_BIN, PATH), as the pane's may not have it."""
    _, u = checked_entry(e)
    home = home or os.path.expanduser("~")
    claude = (claude_bin or os.getenv("CLAUDE_BIN") or shutil.which("claude")
              or os.path.join(home, ".local", "bin", "claude"))
    transcript = transcript_path(home, effective_cwd(e), u)
    have = os.path.isfile(transcript)
    flag = "--resume" if have else "--session-id"
    return [claude, flag, u, "--dangerously-skip-permissions",
            *(effort_flags(transcript) if have else ())]


def pane_command(e, home=None, claude_bin=None):
    """What the pane does, as one shell line (the dry run: `pane-cmd`, DRY_RUN=1):
    what bin/agentdeck-pane runs after the login shell's rc, with pane_argv."""
    sid, _ = checked_entry(e)
    home = home or os.path.expanduser("~")
    oauth = shlex.quote(os.path.join(home, ".claude", "oauth.env"))
    return ('for v in $(env | cut -d= -f1 | grep -i CLAUDE); do unset "$v"; done; '
            f"[ -r {oauth} ] && . {oauth}; "
            # the number the terminal has at launch; after a conversation switch
            # the live one is CLAUDE_CODE_SESSION_ID (see hold(), hold_on_timer)
            f"export AGENTDECK_SESSION={sid}; "
            # exec: when claude exits the pane closes; a leftover shell prompt
            # would run whatever text the bridge types next as commands
            "exec " + " ".join(shlex.quote(a) for a in pane_argv(e, home, claude_bin)))


def launch_file(sid):
    """Where ensure leaves claude's arguments for `agentdeck-pane <sid>`: next to the
    registry (the launcher applies the same rule: $AGENTDECK_LIBRARY, else
    <repo>/.sessions/library.json)."""
    return os.path.join(os.path.dirname(library.LIB_FILE), "launch", sid)


def prepare_launch(sid, argv):
    """Leave argv (NUL-separated, 0600) for the launcher, which reads it once and
    removes it. Data, not a command: nothing in it is run as shell code."""
    if not library.valid_id(sid) or any("\0" in a for a in argv):
        raise ValueError("bad launch arguments")
    path = launch_file(sid)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(b"".join(os.fsencode(a) + b"\0" for a in argv))
    os.replace(tmp, path)
    return path


# ── ensure ──────────────────────────────────────────────────────────────────
def _say(msg):
    print(msg, file=sys.stderr, flush=True)


def _clean(text):
    return _CTRL.sub("", str(text))[:120]


def _lookup(sid):
    """Registry entry for a usable id (or an old number of it: an alias), else None.
    Read-only: never creates the file."""
    if not library.valid_id(sid):
        return None
    e = library.find_or_alias(library.load(library.LIB_FILE), sid)
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
    _say(f"unknown session{shown}: no such topic in the library (or it is archived). "
         "Open a topic from the list on the dashboard.")
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
        _say(f"unloaded topic {library.tmux_name(victim)} «{_clean(name)}» — unused for a "
             "while; the conversation is saved and opens again on click.")
        live = [s for s in live if s["id"] != victim]
    return True


def _start(e, argv):
    """Start cs-<id> running claude (argv: pane_argv) — as the pane's own command, not
    typed into a shell with send-keys (that showed the long `for v in … exec claude …`
    line, echoed twice, before Claude drew). tmux is handed only LAUNCHER and the id
    (see LAUNCHER); the launcher loads the login + interactive bash environment the
    shell tmux used to start had, and execs claude with argv, prepared here — so
    claude is the pane's process and the pane closes when claude exits."""
    sid = e["id"]
    name = library.tmux_name(sid)
    if not os.access(LAUNCHER, os.X_OK):
        raise RuntimeError(f"{LAUNCHER} is not executable (chmod +x it)")
    prepared = prepare_launch(sid, argv)
    r = _tmux("new-session", "-d", "-s", name, LAUNCHER, sid, cwd=effective_cwd(e))
    if r.returncode != 0:
        if _has(name):                             # someone else just started it
            return
        with contextlib.suppress(OSError):         # never left for a later launch
            os.unlink(prepared)
        raise RuntimeError(r.stderr.strip() or "tmux new-session failed")


def ensure(sid, now=None):
    e = _lookup(sid)
    if e is None:
        return _unknown(sid)
    try:
        checked_entry(e)
    except ValueError:
        _say(f"unknown session {sid}: library entry is corrupt (uuid).")
        return EXIT_UNKNOWN
    limit = library.MAX_ACTIVE
    with _ensure_lock():
        sync(lock=False)                           # terminals carry their live numbers
        e = _lookup(sid)                           # may have been archived meanwhile
        if e is None:
            return _unknown(sid)
        try:
            sid, _ = checked_entry(e)              # an old number: the terminal's own
        except ValueError:
            _say(f"unknown session {sid}: library entry is corrupt (uuid).")
            return EXIT_UNKNOWN
        name = library.tmux_name(sid)
        ensure_tmux_conf()
        if not _has(name):
            other = claude_elsewhere(e["uuid"], name)
            if other:
                where = ", ".join(f"pid {p} in tmux session {w}" if w else f"pid {p} (outside tmux)"
                                  for p, w in other)
                _say(f"topic {name} is already open in another Claude: {where}. A second Claude "
                     "on the same conversation would corrupt it — close that one or open "
                     "the topic there. (uuid already running elsewhere)")
                return EXIT_ELSEWHERE
            if not _make_room(limit):
                _say(f"All {limit} loaded topics are busy working or open in tabs — no "
                     "room to load another. Close a tab or unload a topic and try again.")
                return EXIT_BUSY
            try:
                _start(e, pane_argv(e))
            except (RuntimeError, ValueError, OSError) as ex:
                _say(f"couldn't start {name}: {_clean(ex)}")
                return EXIT_FAIL
        try:
            with library.update(library.LIB_FILE) as lib:
                library.touch(lib, sid, int(time.time() if now is None else now))
        except KeyError:
            pass
    print(name, flush=True)
    return EXIT_OK


# ── the plain command line ──────────────────────────────────────────────────
# The pane inherits the tmux SERVER's environment when the server was started by
# someone else (a Claude-spawned caller would leak CLAUDE* into it): `agentdeck-pane
# shell` scrubs it, then execs so the pane's process is the login bash itself.
def shell_ensure():
    """Start cmd-shell unless it runs; either way print its name. tmux refuses a
    second session with the same name, so two presses at once still make one."""
    name = library.SHELL_TMUX
    ensure_tmux_conf()
    if not _has(name):
        cwd = WORKDIR if os.path.isdir(WORKDIR) else os.path.expanduser("~")
        r = _tmux("new-session", "-d", "-s", name, LAUNCHER, "shell", cwd=cwd)
        if r.returncode != 0 and not _has(name):
            _say(f"couldn't start the command line: {_clean(r.stderr.strip())}")
            return EXIT_FAIL
        # option commands need the `=name:` form (a bare `=name` is "no such session")
        _tmux("set-option", "-t", f"={name}:", "mouse", "on")
    print(name, flush=True)
    return EXIT_OK


# ── hold ────────────────────────────────────────────────────────────────────
def pane_session():
    """`hold -`: the terminal this runs in. Only inside a library pane
    (AGENTDECK_SESSION, exported at launch); its number is the conversation live
    now — CLAUDE_CODE_SESSION_ID, which Claude sets for every shell it starts —
    unless that is missing or malformed."""
    pane = os.getenv("AGENTDECK_SESSION", "")
    if not library.valid_id(pane):
        return ""
    u = os.getenv("CLAUDE_CODE_SESSION_ID", "")
    return library.id_from_uuid(u) if _UUID.fullmatch(u) else pane


def hold(sid, seconds, now=None):
    if sid == "-":
        sid = pane_session()
    if not library.valid_id(sid):
        _say("hold: needs an 8-character topic id (or '-' inside a topic's pane).")
        return EXIT_UNKNOWN
    try:
        secs = int(seconds)
    except ValueError:
        secs = -1
    if secs <= 0:
        _say(f"hold: seconds must be a number > 0, not {_clean(seconds)!r}")
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
        _say(f"topic registry is corrupt, doing nothing: {ex}")
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
    if len(argv) == 1 and argv[0] == "shell-ensure":
        return shell_ensure()
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
