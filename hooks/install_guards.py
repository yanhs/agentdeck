#!/usr/bin/env python3
"""Turn the task-board guard hooks on (or off) in a Claude Code settings file.

The set: guard_task_board.py + guard_dont_stop.py (the board), and hold_on_timer.py
(PostToolUse on ScheduleWakeup/CronCreate/Monitor: a terminal waiting on its own timer
stays loaded — idle_reaper skips it while the hold marker is fresh; the terminal is
identified by the conversation in the hook's payload, which is the terminal's number,
and $AGENTDECK_SESSION, exported in every cs-<id> pane, marks a library terminal).

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
                         (the board, where agents run, the safety rules); a CLAUDE.md
                         AgentDeck wrote earlier gets the newer blocks once, by their mark
  --tracker PATH         tracker.py path shown in that CLAUDE.md
  --services-dir PATH    with --claude-md: the services rule (programs that outlive a
                         terminal go there); appended once to an existing CLAUDE.md too

  --workdir DIR          the agents' work folder (default: the parent of --services-dir)

The safety guards (2.0.1): guard_kill.py (signals only to your own process tree),
guard_email.py (mail to people only with a visible SEND_APPROVED=1), guard_secrets.py
(no keys, tokens or this machine's IP in a git commit or push).

A guard the work folder's own .claude/settings.json or settings.local.json already runs
(a hook command with the same file name) is left out: the agents run there and would get
both, and one command blocked twice. --check counts it as installed.

The Docker entrypoint runs this on every start (opt-out: AGENTDECK_GUARDS=0).
"""
import argparse
import json
import os
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GUARDS = ("guard_task_board.py", "guard_dont_stop.py", "hold_on_timer.py",
          "guard_kill.py", "guard_email.py", "guard_secrets.py")

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

# What the agents must know about where they run. 27.10.2026 a sandbox agent, asked
# whether changing the password and opening the web would stop the first terminal,
# answered "I can't tell how your platform works". Every fact below is AgentDeck's.
ENV_MARK = "<!-- agentdeck-environment -->"
ENV_MD = """\
{mark}
## Where you run: an AgentDeck terminal
The dashboard and the Telegram bot are two windows into the same terminals: closing
them, logging in elsewhere or changing the password stops nothing. A terminal is one
conversation; restart or reopening after an idle unload resumes it (claude --resume).
A new terminal is a new conversation and sees no other: keep shared facts in files or memory.
"""
AGENTDECK_HEADER = "# Working rules (AgentDeck)"

# What never happens without the person. 28.10.2026 an agent committed a server's IP and
# Telegram ids into a public repo; 26.09 a `pkill -f` pattern closed every terminal. The
# guard hooks (guard_secrets, guard_kill, guard_email) hold the same lines in code.
SAFETY_MARK = "<!-- agentdeck-safety -->"
SAFETY_MD = """\
{mark}
## Safety: what never happens without the person
- Secrets and personal data (keys, tokens, passwords, IP addresses, phone numbers, user ids)
  never go into a commit, a public file or a link. Add files by name (never `git add -A` or
  `git add .`), read the diff before a push, use made-up values in tests.
- Signals only to processes you started: kill the PID you saved. Never `pkill -f` or
  `killall` by name: a pattern can match the terminals' tmux server and close them all.
- Ask first: deleting what you did not create, force-push or rewriting pushed history,
  dropping data, anything sent to other people (mail, messages, posts). A yes covers that
  one action only.
- Don't print environment variables or config files that hold secrets: name the key only.
- A job that runs later without you (timer, cron, scheduled send) reports its result to
  the person, success and failure alike.
- Try risky changes on a copy, never on the live system. "Done" means you checked the
  result, not that the command finished.
"""
# Test before code. 28.10.2026 the owner asked why his agents write tests first: his own
# CLAUDE.md says "TDD always", AgentDeck's agents were never told. Now they are.
TDD_MARK = "<!-- agentdeck-tdd -->"
TDD_MD = """\
{mark}
## Test first
Every code change starts with a test that fails for the right reason, then the code
that makes it pass, then the whole test suite, and the result is shown before you say
"done". A bug fix starts with a test that reproduces the bug. If your change breaks an
existing test, fix the change, not the test. Skip only for docs, config values and
one-off commands.
"""
# Blocks that go into AgentDeck's own CLAUDE.md (new, or written by AgentDeck earlier), once.
# Keeping only a block's mark line opts out of it: the block is not added back.
OWN_BLOCKS = ((ENV_MARK, ENV_MD), (SAFETY_MARK, SAFETY_MD), (TDD_MARK, TDD_MD))

SERVICES_MARK = "<!-- agentdeck-services -->"
SERVICES_MD = """\
{mark}
## Programs that must keep running: make them services
A terminal is unloaded when idle and what it started dies with it (&, nohup, tmux).
A tunnel, a site's server, a bot: put its command in {dir}/<name>.sh
(name: a-z 0-9 - _), in the foreground (`exec <program>`, no &, no daemon mode).
The keeper starts it in seconds, restarts it if it crashes, logs to <name>.log.
State: `curl -s localhost:9876/status`; `curl -X POST localhost:9876/stop/<name>`
(also start, restart). Delete the file to remove it. At most 10 services.
When you add, stop or remove a service, tell the user in one line what and why.
"""


def template(hooks_dir: str, tracker_state: str, skip=()) -> dict:
    """The example's hooks with real paths; guards named in `skip` left out."""
    with open(os.path.join(HERE, "settings.example.json")) as f:
        hooks = json.load(f)["hooks"]
    prefix = f"TRACKER_STATE={shlex.quote(tracker_state)} " if tracker_state else ""
    out = {}
    for event, groups in hooks.items():
        for g in groups:
            hs = []
            for h in g["hooks"]:
                if os.path.basename(h["command"].split()[-1]) in skip:
                    continue
                cmd = h["command"].replace("/path/to/agentdeck/hooks", hooks_dir)
                hs.append(dict(h, command=prefix + cmd))
            if hs:
                out.setdefault(event, []).append(dict(g, hooks=hs))
    return out


def present_in_project(workdir: str, hooks_dir: str) -> set:
    """Our guard names that the work folder's own Claude settings already run."""
    found = set()
    for name in ("settings.json", "settings.local.json"):
        data, ok = load(os.path.join(workdir, ".claude", name)) if workdir else ({}, True)
        hooks = (data or {}).get("hooks") if ok else None
        if not isinstance(hooks, dict):
            continue
        for event in hooks:
            for cmd in _commands(hooks, event):
                for g in GUARDS:
                    if g in cmd and os.path.join(hooks_dir, g) not in cmd:
                        found.add(g)
    return found


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


def _commands(hooks: dict, event: str) -> list:
    groups = hooks.get(event)
    return [h.get("command", "") or ""
            for g in (groups if isinstance(groups, list) else []) if isinstance(g, dict)
            for h in (g.get("hooks") or []) if isinstance(h, dict)]


def installed(data: dict, hooks_dir: str, skip=()) -> bool:
    """True only when every hook of the set is present under its event — an older
    install missing one (e.g. hold_on_timer) reads as not installed."""
    have = data.get("hooks") or {}
    want = template(hooks_dir, "", skip)
    for event, groups in want.items():
        cmds = _commands(have, event)
        for g in groups:
            for h in g["hooks"]:
                script = next(os.path.join(hooks_dir, n) for n in GUARDS if n in h["command"])
                if not any(script in c for c in cmds):
                    return False
    return True


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
    ap.add_argument("--services-dir", default="")
    ap.add_argument("--workdir", default="")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--remove", action="store_true")
    mode.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    hooks_dir = os.path.abspath(a.hooks_dir)
    workdir = a.workdir or (os.path.dirname(a.services_dir.rstrip("/")) if a.services_dir else "")
    skip = present_in_project(workdir, hooks_dir)

    data, ok = load(a.settings)
    if a.check:
        return 0 if ok and installed(data, hooks_dir, skip) else 1
    if not ok:
        print(f"[agentdeck] {a.settings} is not valid JSON; guard hooks left unchanged",
              file=sys.stderr)
        return 0

    had_file = os.path.exists(a.settings)
    hooks = strip_ours(data.get("hooks") or {}, hooks_dir)
    if not a.remove:
        for event, groups in template(hooks_dir, a.tracker_state, skip).items():
            hooks.setdefault(event, []).extend(groups)
        for g in sorted(skip):
            print(f"[agentdeck] {g}: {workdir}/.claude already runs one; not added again")
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
    if a.claude_md and not a.remove and os.path.exists(a.claude_md):
        # once, and only into the file AgentDeck wrote — a person's own CLAUDE.md is theirs
        for mark, block in OWN_BLOCKS:
            text = open(a.claude_md).read()
            if text.startswith(AGENTDECK_HEADER) and mark not in text:
                with open(a.claude_md, "a") as f:
                    f.write(("\n" if not text.endswith("\n\n") else "")
                            + block.format(mark=mark))
    if a.claude_md and a.services_dir and not a.remove:
        # once, also into the user's own CLAUDE.md (keep the mark line to opt out)
        text = open(a.claude_md).read() if os.path.exists(a.claude_md) else ""
        if SERVICES_MARK not in text:
            block = SERVICES_MD.format(mark=SERVICES_MARK, dir=a.services_dir.rstrip("/"))
            with open(a.claude_md, "a") as f:
                f.write(("\n" if text and not text.endswith("\n\n") else "") + block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
