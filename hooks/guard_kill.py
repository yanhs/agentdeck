#!/usr/bin/env python3
"""Claude Code hook: send signals only to your own processes.

A guard against every terminal dying at once.

**The failure this file exists for.** In 2026 an agent ran

    pkill -f "noconftest 2>&1 | grep"

and every live Claude terminal on the machine died at the same moment. The pattern of
`pkill -f` is an extended regular expression: "|" means OR, so it matched every process
whose command line contains " grep". The tmux server that hosts all the terminals inherits
the argv of the client that started it, and AgentKennel starts it with `tmux -f ...
new-session ... bash -lic 'for v in $(env | cut -d= -f1 | grep -i CLAUDE); do unset "$v";
done; ... exec claude --resume ...'`. That line contains "grep", "claude", "bash" and "env",
so any careless pkill/killall/pgrep|xargs kill with one of those words kills the server and
every session in it.

**The rule.** A Bash command (and a Monitor command - Monitor also runs
tool_input.command in a shell) may send signals only to processes in its own tree: the
descendants of the Claude process that runs this guard (the guard is itself a descendant:
walk up by PPID to the nearest `claude`). Anything else is blocked with a list of up to five
processes that would be hit and a hint: "kill the PID you saved with $!".

**How it checks.** Matches are not guessed, they are computed by the same tool with the
same pattern semantics, read-only: for `pkill ...` the guard runs `pgrep` with the same
arguments (no signal), for `killall` it runs `pgrep -x` / `pgrep` with the regex, for
`kill $(pgrep ...)`, `pgrep ... | xargs kill`, `pidof x | xargs kill`, `ps|grep|awk` it runs
the read-only part itself. The guard's own pgrep never shows up in the matches (pgrep
excludes itself). The guard does not rewrite regexes.

The guard runs the read-only part BEFORE the command is approved, so only like this: the
binary is a system one (/usr/bin, /bin, /usr/sbin, /sbin; not `./cat` or `/tmp/x/pgrep`),
PATH is the guard's own, of the assignments in front of the command only LC_*/LANG/TZ/
COLUMNS/POSIXLY_CORRECT are repeated (not `PATH=... pgrep` or `LD_PRELOAD=...`), options
that run a program or write a file (sort -o/--compress-program, awk -f/-i/--source/@include,
ss -K/-D) are refused, pipes and devices are not read. If it cannot run it, it cannot know
whom the signal would hit: block.

Parsed: `;` `&&` `||` `|` newline, `( )`, `{ }`, `$( )`, backticks, `<( )`, a heredoc fed
to a shell, `sudo`, `env VAR=..`, `timeout N`, `nohup`, `nice`, `setsid`, `bash -c '...'`,
`eval`, `for p in $(...)`, `... | while read p`, assignments in the same command.
tmux kill-server/kill-session/kill-window/kill-pane is blocked only on the LIVE socket (the
default socket, the socket from $TMUX); on your own one (-L, -S, your own TMUX_TMPDIR) it is
allowed. systemctl stop/restart/kill/... is blocked for agentdeck-* and for any unit whose
cgroup holds a tmux server or a Claude process.

`kill -0 PID` is let through: signal 0 delivers nothing, it is a liveness check.
`pkill -0` / `killall -0` by pattern are not: they rehearse the same -9, look at the matches
with pgrep instead.

`fuser -k` is checked the same way (`fuser` without -k). `killall5` and `skill` are always
blocked.

**Too dynamic** (eval of unknown text, a pattern or PID in a variable of unknown origin, a
PID from `docker top` / `systemctl show` / `tmux list-panes`) - block: the guard cannot know
where the signal would go.

**Checked on real commands**: 1666 logged Bash commands containing these words; the 33 it
blocked all signalled someone else's processes, and the false blocks found on the way are
covered by tests.

**Override** - the prefix `KILL_OK=1`: visible in the transcript, for the rare legitimate
case.

**Fast path** (guard_kill_fast.py, shared with the core). The guard runs before EVERY Bash
call. The words kill/pkill/killall/xargs/pidof/tmux/systemctl/fuser are looked for in the
raw text and in the text with quotes and backslashes stripped (`k'i'll`, `k\\ill` are the
same kill) - then parsing is strict. If no such word is there but a command name may be
assembled at run time (`$...`, `` `...` ``, `$'\\x6b...'`, a `k?ll` glob, text for
bash/sh/eval, `service`), the core parses leniently: it blocks only what the parse took all
the way to a signal and lets unparsed text through. Otherwise it exits at once, without
loading the core.

**What the guard does not see.** Scripts in files (`bash x.sh`, `source x.sh`), signals from
code (`python -c 'os.kill(...)'`, a name built by an interpreter), a name produced by a
program the guard does not run (`$(base64 -d <<<...)`, `$(which kill)`), shell functions
with positional arguments, `find ... -exec kill`, `docker kill/stop/restart`, `pm2 kill`,
`reboot`/`shutdown` without systemctl, `tmux send-keys C-c`. A failed strict parse is a
block, not a pass. `service NAME stop` is checked as `systemctl stop NAME`.

**Connection.** Installed by hooks/install_guards.py (PreToolUse, matcher Bash and Monitor).

**Layout.** This file is only the entry point and the fast path; parsing lives in
guard_kill_core.py. A big file run as a script is compiled on every run (~40 ms extra), a
module only once (it is loaded from __pycache__). The core loads heavy modules lazily.
Tests: python3 -m pytest tests/test_guard_kill.py -q
"""
import json
import os
import re
import sys

from guard_kill_fast import look

OVERRIDE = re.compile(r"\bKILL_OK=1\b")
TOOLS = ("Bash", "Monitor")                   # both run tool_input.command in a shell


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0
    if not isinstance(data, dict) or data.get("tool_name") not in TOOLS:
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not isinstance(cmd, str):
        return 0
    mode = look(cmd)
    if mode is None:
        return 0                              # fast path: run nothing
    if OVERRIDE.search(cmd):
        return 0
    cwd = data.get("cwd") or os.getcwd()
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import guard_kill_core
        text = guard_kill_core.run(cmd, cwd, strict=(mode == "strict"))
        if text is None:
            return 0
    except Exception as e:
        if mode != "strict":                  # no signal word in the text: no reason to block
            return 0
        text = ("BLOCKED by guard_kill: internal guard error on a command with "
                f"kill/tmux/systemctl/xargs: {type(e).__name__}: {e}\n"
                "Not letting a signal-sending command through blind.\n"
                "Kill the PID you saved with $! instead.\n"
                "Really needed? Prefix the command with KILL_OK=1 (visible in the transcript).")
    print(text, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
