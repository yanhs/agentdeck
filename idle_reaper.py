#!/usr/bin/env python3
"""Unload idle Claude terminals from RAM to save memory.

Each terminal is a tmux session running `claude`. A session that nobody is
watching and that hasn't burned CPU for a while is just holding ~0.2-1 GB for
nothing — the whole conversation lives on disk in its transcript, so killing
the session loses nothing: the dashboard shows it greyed (active=false), and
clicking it reloads it via ttyd -> launch script -> `claude --resume`.

Run once per minute from cron. Each run:
  * samples every existing session's CPU (working?) and attach state (a browser
    tab open on it?),
  * bumps a per-session "last active" timestamp whenever it is working OR
    attached,
  * unloads (tmux kill-session) any session that has been neither working nor
    attached for IDLE_SECONDS.

It never touches agents.json (the id stays in the dashboard's order, so the
card stays visible and reloadable) and never deletes a transcript. A session
with an open tab is exempt by the owner's choice — it is kept loaded.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
IDLE_SECONDS = int(os.getenv("REAPER_IDLE_SECONDS", "1200"))   # 20 minutes
SAMPLE_INTERVAL = float(os.getenv("REAPER_SAMPLE", "0.30"))
CPU_TICK_THRESHOLD = int(os.getenv("REAPER_CPU_THRESHOLD", "2"))
STATE_FILE = os.path.join(HERE, ".idle_reaper_state.json")

SESSIONS = ["claude-terminal"] + [f"claude-terminal-{i}" for i in range(2, 9)]


# ── tmux / proc helpers (mirror status_server.py) ──────────────────────────
def _tmux(args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def session_exists(session):
    return _tmux(["has-session", "-t", session]).returncode == 0


def session_attached(session):
    r = _tmux(["display-message", "-t", session, "-p", "#{session_attached}"])
    return r.returncode == 0 and r.stdout.strip() not in ("", "0")


def get_pane_pid(session):
    r = _tmux(["list-panes", "-t", session, "-F", "#{pane_pid}"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return int(r.stdout.strip().split("\n")[0])


def get_child_pids(pid):
    r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return [int(p) for p in r.stdout.strip().split("\n") if p.strip()]


def read_cpu_ticks(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split(")")[-1].strip().split()
            return int(fields[11]) + int(fields[12])  # utime + stime
    except (FileNotFoundError, PermissionError, IndexError, ValueError):
        return 0


def tree_ticks(pane_pid):
    """CPU ticks for the pane's shell + its children + grandchildren."""
    if pane_pid is None:
        return 0
    total = read_cpu_ticks(pane_pid)
    for cpid in get_child_pids(pane_pid):
        total += read_cpu_ticks(cpid)
        for gpid in get_child_pids(cpid):
            total += read_cpu_ticks(gpid)
    return total


def sample_working(session):
    """True if the session's process tree burned CPU over SAMPLE_INTERVAL."""
    pane = get_pane_pid(session)
    if pane is None:
        return False
    t1 = tree_ticks(pane)
    time.sleep(SAMPLE_INTERVAL)
    return (tree_ticks(pane) - t1) > CPU_TICK_THRESHOLD


def unload(session):
    return _tmux(["kill-session", "-t", session]).returncode == 0


# ── pure decision logic (unit-tested) ──────────────────────────────────────
def decide(now, last_active, attached, working, idle_seconds):
    """Return (new_last_active, should_unload).

    - working or attached  -> alive: reset the clock, never unload.
    - never seen before     -> seed the clock now, don't unload yet.
    - idle long enough       -> unload.
    """
    if working or attached:
        return now, False
    if last_active is None:
        return now, False
    if now - last_active >= idle_seconds:
        return last_active, True
    return last_active, False


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
    for s in SESSIONS:
        if not session_exists(s):
            state.pop(s, None)          # forget dead sessions
            continue
        attached = session_attached(s)
        working = False if attached else sample_working(s)   # attached => keep, skip sampling
        new_last, do_unload = decide(now, state.get(s), attached, working, IDLE_SECONDS)
        state[s] = new_last
        if do_unload:
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
