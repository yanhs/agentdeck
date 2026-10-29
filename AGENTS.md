# Agent operating rules (example)

An example set of standing rules for Claude Code agents that work on a server around the
clock. Copy what fits into your own `~/.claude/CLAUDE.md` or a project's `CLAUDE.md` —
Claude Code reads those files when a session starts; it does not read this one.

AgentKennel already writes a short version of the safety, test-first and links rules into
the `~/.claude/CLAUDE.md` it creates for its agents (see the README's Safety section).
This file is the longer, editable form.

## Working style

- **Test first.** Write a test that fails for the right reason, then the code that makes it
  pass, then run the whole suite. Show the result before saying "done". A bug fix starts
  with a test that reproduces the bug. If a change breaks an existing test, fix the change,
  not the test.
- **Run it for real.** Every task includes actually running the code, not just reading it.
  "Should work" is not a result.
- **Try it small first.** Before a big or destructive run, try a short run on a slice of the
  real data and check the result where it lands (the database, the file), then do the full run.
- **Report what was measured.** Mark anything untested as untested; never describe a
  predicted outcome as if it was observed.
- **Read before writing.** Read the existing code before changing it, and don't ask what
  the code can answer. If a requirement is ambiguous, pick a reasonable option, write the
  choice down and move on.

## Finishing what you start

- **Keep going until done or truly blocked.** A finished sub-step or a commit is not a
  reason to stop. A real blocker is a missing decision, credential or access — then say
  "BLOCKED on X, need Y".
- **No empty promises.** Don't end a turn with "continuing…". Either do the next step now,
  or leave something that brings you back: a background command, a timer
  (`ScheduleWakeup` / `CronCreate`), or the task closed on the board.
- **"I'll check back in 10 minutes" means a timer is set** in the same turn.

## Big or multi-step tasks

- **Put the task on the board before you start**, and move it at each milestone, not at
  the end. With AgentKennel's board (`tasks-dashboard/tracker.py`):

  ```bash
  python3 tracker.py add-task fix-login --title "Fix the login redirect" --agent "claude · myproject"
  python3 tracker.py add-item fix-login "Reproduce with a failing test"
  python3 tracker.py set-task fix-login active
  python3 tracker.py set fix-login 0 done
  ```

- **Long jobs report themselves.** Wrap them with
  `tasks-dashboard/track-run.sh <task> <item> -- <command>`: the item flips to `done` on
  exit 0 and `blocked` on failure, so the board can't go stale.
- **Keep a progress note** (`.progress-<task>.md` next to the code) for work that spans
  sessions: what is done, what is next, key decisions. Keep it after the task is finished.

## Safety

- **Secrets stay out of git.** No keys, tokens, passwords, IP addresses or personal data
  in a commit, a public file or a link. Add files by name (never `git add -A`), read the
  diff before a push, use made-up values in tests.
- **Signals only to your own processes.** Kill the PID you saved; never `pkill -f` or
  `killall` by pattern — a pattern can match far more than you think.
- **Ask first** before deleting what you did not create, force-pushing or rewriting pushed
  history, dropping data, or sending anything to people. A yes covers that one action only.
- **Mail and messages to people** go out only after the owner has seen the text, the
  sender and the recipient, and said send.
- **Scheduled work reports back.** A job that runs later without you (cron, a timer) ends
  by checking its result and telling the owner — success and failure alike.

## Conventions

- **A link goes last.** A link the reader should open is the last line of the reply, alone,
  with nothing after it: chat apps and terminals glue a trailing period or bracket to it.
- **Long commands run in the background** (test suites, builds, deploys), so the
  conversation stays responsive.
