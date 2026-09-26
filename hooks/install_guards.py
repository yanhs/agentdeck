#!/usr/bin/env python3
"""Turn the task-board guard hooks on (or off) in a Claude Code settings file.

The hooks block comes from hooks/settings.example.json (one source), with
/path/to/agentdeck replaced by the real repo path. The merge is idempotent and keeps
everything else in the file: other keys, and every hook that is not one of ours. A file
that is not valid JSON is left untouched (with a warning) — never overwritten.

  install_guards.py                          install into ~/.claude/settings.json
  install_guards.py --remove                 take them out again
  install_guards.py --check                  exit 0 if installed, 1 if not
  --settings PATH        settings file (default ~/.claude/settings.json)
  --hooks-dir DIR        where the guard scripts live (default: this folder)
  --tracker-state PATH   bake TRACKER_STATE=PATH into the hook commands
  --claude-md PATH       also write a short default CLAUDE.md there, only if none exists
  --tracker PATH         tracker.py path shown in that CLAUDE.md

The Docker entrypoint runs this on every start (opt-out: AGENTDECK_GUARDS=0).
"""
import argparse
import json
import os
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GUARDS = ("guard_task_board.py", "guard_dont_stop.py")

CLAUDE_MD = """\
# Working rules (AgentDeck)

## Put multi-step work on the task board
Anything beyond a one-line change goes on the board BEFORE you start:

    python3 {tracker} add-task <id> --title "..." --agent "claude"
    python3 {tracker} add-item <id> "step"          # one per step
    python3 {tracker} set-task <id> active

Move the steps AS YOU GO, not at the end:

    python3 {tracker} set <id> <item_index> active|done|blocked --note "..."

When everything is done: `python3 {tracker} set-task <id> done`.
A genuinely trivial edit: run `NO_BOARD=1 true` instead (the edit guard then stands down).

## Do not stop in the middle of a task
While your task is active on the board, do not end a turn with "continuing..." and
nothing scheduled. Leave something that brings you back: a background command
(run_in_background), a timer (ScheduleWakeup / CronCreate), a Monitor or a sub-agent.
Otherwise close or park the task on the board.
"""


def template(hooks_dir: str, tracker_state: str) -> dict:
    with open(os.path.join(HERE, "settings.example.json")) as f:
        hooks = json.load(f)["hooks"]
    prefix = f"TRACKER_STATE={shlex.quote(tracker_state)} " if tracker_state else ""
    for groups in hooks.values():
        for g in groups:
            for h in g["hooks"]:
                cmd = h["command"].replace("/path/to/agentdeck/hooks", hooks_dir)
                h["command"] = prefix + cmd
    return hooks


def is_ours(cmd: str, hooks_dir: str) -> bool:
    return any(os.path.join(hooks_dir, g) in (cmd or "") for g in GUARDS)


def strip_ours(hooks: dict, hooks_dir: str) -> dict:
    out = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            out[event] = groups
            continue
        kept = []
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                kept.append(g)
                continue
            hs = [h for h in g["hooks"]
                  if not (isinstance(h, dict) and is_ours(h.get("command", ""), hooks_dir))]
            if hs:
                kept.append(dict(g, hooks=hs))
        if kept:
            out[event] = kept
    return out


def load(path: str):
    """(settings dict, ok). Missing file -> ({}, True); unreadable/invalid -> (None, False)."""
    if not os.path.exists(path):
        return {}, True
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None, False
    return (data, True) if isinstance(data, dict) else (None, False)


def installed(data: dict, hooks_dir: str) -> bool:
    for groups in (data.get("hooks") or {}).values():
        for g in groups if isinstance(groups, list) else []:
            for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                if isinstance(h, dict) and is_ours(h.get("command", ""), hooks_dir):
                    return True
    return False


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".agentdeck-tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--settings", default=os.path.expanduser("~/.claude/settings.json"))
    ap.add_argument("--hooks-dir", default=HERE)
    ap.add_argument("--tracker-state", default="")
    ap.add_argument("--claude-md", default="")
    ap.add_argument("--tracker", default=os.path.join(REPO, "tasks-dashboard", "tracker.py"))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--remove", action="store_true")
    mode.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    hooks_dir = os.path.abspath(a.hooks_dir)

    data, ok = load(a.settings)
    if a.check:
        return 0 if ok and installed(data, hooks_dir) else 1
    if not ok:
        print(f"[agentdeck] {a.settings} is not valid JSON; guard hooks left unchanged",
              file=sys.stderr)
        return 0

    had_file = os.path.exists(a.settings)
    hooks = strip_ours(data.get("hooks") or {}, hooks_dir)
    if not a.remove:
        for event, groups in template(hooks_dir, a.tracker_state).items():
            hooks.setdefault(event, []).extend(groups)
    new = dict(data)
    if hooks:
        new["hooks"] = hooks
    else:
        new.pop("hooks", None)
    if new != data or (not had_file and not a.remove):
        write_json(a.settings, new)

    if a.claude_md and not a.remove and not os.path.exists(a.claude_md):
        os.makedirs(os.path.dirname(a.claude_md) or ".", exist_ok=True)
        with open(a.claude_md, "w") as f:
            f.write(CLAUDE_MD.format(tracker=a.tracker))
    return 0


if __name__ == "__main__":
    sys.exit(main())
