# Agent rules (shared, versioned)

Canonical, human-readable copy of the standing rules for Claude agents working on this
server. This file is the shared source of truth you can read and edit in git.

> How recall actually works: each session loads my per-project **auto-memory**
> (`~/.claude/projects/<project>/memory/`) into context — that is what makes me *follow*
> these rules. This repo file does **not** auto-load for arbitrary projects; it is the
> shared canonical copy. Keep the two in sync: when a rule changes, update both.

## Working style
- **TDD always.** Write a failing test first, make it pass, refactor. Show test results
  before saying "done".
- **Always test for real.** Every task includes actually running the code (and tests),
  not just code review. No "should work".
- **Validate on real data.** Before a big or destructive run, test on a slice of the
  ACTUAL data, and on a short run (a couple of minutes / small slice); check the result
  (e.g. in the DB) before the full run.
- **No projection from incomplete data.** Report only measured facts. Mark untested
  outcomes as untested; never predict them as if observed.
- **Supervisor mode:** medium strictness, max 3 self-review iterations, then show the
  result. If a gate fails, fix it rather than report it.

## Finishing what I start
> **#1 RULE — DO NOT STOP.** On an authorized, unfinished task, keep going to DONE or a REAL
> blocker. A finished sub-phase, a commit, a "clean boundary", "this is consequential so I'll
> confirm", or "say go to continue" are NOT reasons to stop — they are the exact betrayal the
> user has called out repeatedly. The only legitimate stop is a genuine blocker (missing
> decision/credential/access). Everything else: continue.

- **No empty continuation.** Never end a turn with a bare "continuing / will do it now".
  Do it in the same turn or stop explicitly. Between turns I do not exist.
- **Autonomous to completion.** When told "to the end / don't stop", run until the task is
  DONE or genuinely BLOCKED. Do not stop at "milestones". If a turn ends mid-task, in that
  SAME turn call `ScheduleWakeup` with `delaySeconds: 60` and a prompt that resumes the
  task, so it self-continues without the user. Only stop without a wakeup when DONE+verified
  or truly BLOCKED (then say "DONE" or "BLOCKED on X — need Y").
- **Self-timer for promises.** "I'll check back in N min" → schedule the wakeup in the same
  turn; an unscheduled promise is empty.

## Big / multi-step tasks
- **Use a tracker, up front.** For anything beyond ~3-4 steps or spanning turns, set up a
  visible tracker before diving in and update it at each milestone (not at the end):
  - In-session task list (Task tools).
  - Durable `.progress-<task>.md` for cross-session state (keep it after completion).
  - The shared live board: **https://agents.reimake.com/tasks/** — one state.json shared by
    all agents, SSE-pushed to every open tab. Move it with `tasks-dashboard/tracker.py`
    (`set <task> <item_index> <todo|active|done|blocked>`).
  - **Auto-sync (preferred for long/background jobs):** wrap the command with
    `tasks-dashboard/track-run.sh <task> <item> [--note N] -- <cmd>` and it flips its own
    board item on completion — `done` on exit 0, `blocked` on failure (exit code preserved).
    No manual post-hoc `set`, so the board can't go stale.

## Conventions
- **Never produce `yanhs.stream` URLs.** Use `reimake.com` (e.g. `reimake.com/tgimg/` for
  uploaded images).
- **Offer 1M context** proactively for whole-codebase / multi-file work; default 200k for
  incremental work.

---
Derived from per-project auto-memory (`feedback_*` notes). If you change a rule here, also
update the matching memory note so future sessions recall it.
