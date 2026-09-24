#!/usr/bin/env python3
"""Unload idle Claude terminals from RAM to save memory.

Each terminal is a tmux session running `claude`. A session that nobody is
watching and that hasn't printed anything for a while is just holding
~0.2-1 GB for nothing — the whole conversation lives on disk in its
transcript, so killing the session loses nothing: the dashboard shows it
greyed (active=false), and clicking it reloads it via ttyd -> launch script ->
`claude --resume`.

Run once per minute from cron. "Idle" is measured from the terminal's last
screen output (tmux `session_activity`): a working Claude animates its spinner
every second, an idle one prints nothing. (The first version sampled CPU for
0.3 s each minute; an idle Claude still burns a few ticks now and then, which
read as "working" ~3-4% of the time — over two hours the clock almost never
ran out, so terminals sat loaded for days.)

A session is unloaded when ALL of these hold:
  * no browser tab is open on it (attached sessions are exempt by the owner's
    choice), and it hasn't had one for IDLE_SECONDS either — closing a tab
    restarts the clock, a glance counts as use;
  * it hasn't printed anything for IDLE_SECONDS (default 2 hours — long enough
    that a terminal with an ongoing task, merely paused, is not snatched away);
  * it has no live background task (a shell writing into Claude's
    tasks/*.output — a pipeline run, a wake-up timer), unless it has been
    silent for BG_MAX_SECONDS (default 24 h): by then it is a stuck loop,
    not work.

It never touches agents.json (the id stays in the dashboard's order, so the
card stays visible and reloadable) and never deletes a transcript.
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
IDLE_SECONDS = int(os.getenv("REAPER_IDLE_SECONDS", "7200"))        # 2 hours
BG_MAX_SECONDS = int(os.getenv("REAPER_BG_MAX_SECONDS", "86400"))   # 24 hours
STATE_FILE = os.path.join(HERE, ".idle_reaper_state.json")          # last seen attached

# Legacy numbered terminals (until the session-library migration) ...
SESSIONS = ["claude-terminal"] + [f"claude-terminal-{i}" for i in range(2, 13)]
# ... plus every library session, tmux `cs-<8 hex>` (see library.py).
_LIBRARY_SESSION = re.compile(r"cs-[0-9a-f]{8}")

# Claude Code sends a background task's stdout to
# /tmp/claude-<uid>/<project>/<session>/tasks/<task-id>.output
_TASK_OUTPUT = re.compile(r"/tasks/[A-Za-z0-9_-]+\.output$")


# ── tmux / proc helpers ─────────────────────────────────────────────────────
def _tmux(args):
    # AGENTDECK_TMUX_SOCKET points tests at a private tmux server (tmux -L).
    sock = os.getenv("AGENTDECK_TMUX_SOCKET")
    base = ["tmux", "-L", sock] if sock else ["tmux"]
    return subprocess.run([*base, *args], capture_output=True, text=True)


# Everything below asks tmux through list-sessions / list-windows, never
# `display-message`: in tmux 3.2a `display-message -p` on a target that has just
# vanished segfaults the WHOLE server and takes every live terminal with it
# (happened 2026-09-24 10:38).
def _session_fields(fmt):
    r = _tmux(["list-sessions", "-F", "#{session_name}\t" + fmt])
    out = {}
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        name, _, val = line.partition("\t")
        out[name] = val
    return out


def _tmux_session_names():
    r = _tmux(["list-sessions", "-F", "#{session_name}"])
    return r.stdout.split() if r.returncode == 0 else []


def watched_sessions():
    """Legacy terminals + library sessions currently in tmux; nothing else."""
    lib = [s for s in _tmux_session_names() if _LIBRARY_SESSION.fullmatch(s)]
    return SESSIONS + [s for s in lib if s not in SESSIONS]


def session_exists(session):
    return session in _session_fields("#{session_attached}")


def session_attached(session):
    return _session_fields("#{session_attached}").get(session, "0") not in ("", "0")


def session_activity(session):
    """Unix time of the session's last screen output, or None.

    #{window_activity}, not #{session_activity}: the latter is CLIENT activity and
    stays frozen while a detached session prints (measured 2026-09-24).
    """
    r = _tmux(["list-windows", "-a", "-F", "#{session_name}\t#{window_activity}"])
    times = [int(v) for n, _, v in (l.partition("\t") for l in r.stdout.splitlines())
             if n == session and v.isdigit()] if r.returncode == 0 else []
    return max(times) if times else None


def get_pane_pid(session):
    r = _tmux(["list-panes", "-t", session, "-F", "#{pane_pid}"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return int(r.stdout.strip().split("\n")[0])


def get_child_pids(pid):
    r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return [int(p) for p in r.stdout.strip().split("\n") if p.strip()]


def is_task_output(path):
    return bool(_TASK_OUTPUT.search(path))


def _stdout_target(pid):
    try:
        return os.readlink(f"/proc/{pid}/fd/1")
    except OSError:
        return ""


def tree_has_task_output(root_pid):
    """True if any process under root_pid writes its stdout to a Claude task file."""
    stack, seen = [root_pid], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if is_task_output(_stdout_target(pid)):
            return True
        stack.extend(get_child_pids(pid))
    return False


def session_has_bg_jobs(session):
    pane = get_pane_pid(session)
    return pane is not None and tree_has_task_output(pane)


def unload(session):
    return _tmux(["kill-session", "-t", session]).returncode == 0


# ── pure decision logic (unit-tested) ──────────────────────────────────────
def last_active_of(output_at, seen_attached_at):
    """Latest of last screen output and last time a tab was seen open."""
    known = [t for t in (output_at, seen_attached_at) if t is not None]
    return max(known) if known else None


def decide(now, last_active, attached, bg_jobs, idle_seconds, bg_max_seconds):
    """Return True if the session should be unloaded.

    - a tab is open             -> keep.
    - last activity unknown     -> keep (never guess towards killing).
    - active within the window  -> keep.
    - background task running   -> keep, until silent for bg_max_seconds.
    """
    if attached or last_active is None:
        return False
    idle = now - last_active
    if idle < idle_seconds:
        return False
    if bg_jobs and idle < bg_max_seconds:
        return False
    return True


# ── state file ──────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ── main sweep ──────────────────────────────────────────────────────────────
def sweep(now=None, dry_run=False):
    now = now if now is not None else time.time()
    state = load_state()
    actions = []
    for s in watched_sessions():
        if not session_exists(s):
            state.pop(s, None)          # forget dead sessions
            continue
        attached = session_attached(s)
        if attached:
            state[s] = now              # a tab is open: remember when we last saw it
            continue
        last = last_active_of(session_activity(s), state.get(s))
        # the process-tree walk only for sessions already past the idle window
        bg = last is not None and now - last >= IDLE_SECONDS and session_has_bg_jobs(s)
        if decide(now, last, False, bg, IDLE_SECONDS, BG_MAX_SECONDS):
            actions.append(s)
            if not dry_run and unload(s):
                state.pop(s, None)
    save_state(state)
    return actions


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    killed = sweep(dry_run=dry)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if killed:
        print(f"{stamp} {'WOULD unload' if dry else 'unloaded'}: {', '.join(killed)}")
    else:
        print(f"{stamp} nothing idle past {IDLE_SECONDS}s")
