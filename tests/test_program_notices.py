"""Claude Code's own notices never reach the Telegram chat.

27.10.2026 a guest got, in her chat, the Stop hook's message to the agent
("Ran 1 stop hook / Stop hook error: Do not stop yet: …") and the program's
"✔ Update installed · Restart to apply" (Telegram translated it as "restart
your device"). Those are the terminal's lines for the person at the keyboard,
not the agent's reply. The screens below keep the exact shape of the real
ones (indentation, glyphs, padding); the agent's own words are made up."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TG_BRIDGE_TOKEN", "test:token")
os.environ.setdefault("TG_BRIDGE_OWNER", "1")
os.environ["TG_CONVO_LOG"] = "/tmp/test_tg_convo.log"

import status_server as ss  # noqa: E402
import tg_bridge as tb  # noqa: E402

# The stop hook firing after a reply, as the TUI draws it (real layout).
HOOK_SCREEN = """\
● Paused halfway: the browser window came to the front and my clicks started
  landing there. I am not touching the mouse any more.

  Done so far:
  - Left panel: 6 of 12 points are in place.

● Ran 1 stop hook
  ⎿  Stop hook error: Do not stop yet: this session has an open task on the board and nothing is
  scheduled to bring you back to it. Ending the turn with "continuing..." and no
  way to resume is a silent stop.

  Do ONE of these, then carry on:
    1. Run the next step as a background command (run_in_background: true);
       you are resumed automatically when it finishes.
    2. Set a timer: CronCreate (or ScheduleWakeup in /loop mode).
    3. Is the work really finished? Close the task on the board:
       cd /app/tasks-dashboard
       python3 tracker.py set-task <id> done

  Open tasks: demo-task

  Ran 3 shell commands
  ⎿  Read demo/seams.py (88 lines)

● I put the work on pause.

  - Done: half of the points.
  - Left: the other half.

✻ Cogitated for 35m 7s · done 2:33 PM
"""

# A fresh `claude` after the program updated itself (real layout: the notice
# sits far right, after ~290 spaces of padding).
UPDATE_SCREEN = (
    "▐▛███▛█   Claude Code v2.1.311\n"
    "▝▜██████▀  Opus 5.5 · API Usage Billing\n"
    " ▝▝   ▝▝   /work\n"
    "\n"
    "  Updated to latest. Got 62 features, 439 bugfixes, and 155 other changes.\n"
    "  code.claude.com/docs/en/changelog for details\n"
    "\n"
    + " " * 291 + "✔ Update installed · Restart to apply"
)

# The live view mid-reply, and the final one (real lines from the bridge log).
LIVE_SCREEN = (
    "❯ hi there\n"
    "✻ Skedaddling… (0s · thinking)\n"
    "✔ Update installed · Restart to apply\n"
)
FINAL_SCREEN = (
    "❯ hi there\n"
    "● Hi! How can I help?\n"
    "\n"
    "✻ Brewed for 1s · done 2:40 PM\n"
    "\n"
    "✔ Update installed · Restart to apply\n"
)

HOOK_WORDS = ("stop hook", "Do not stop yet", "Do ONE of these", "run_in_background",
              "CronCreate", "tracker.py set-task", "Open tasks", "silent stop")
UPDATE_WORDS = ("Update installed", "Restart to apply", "Updated to latest",
                "changelog for details")


def _none_of(words, text):
    left = [w for w in words if w.lower() in text.lower()]
    assert not left, f"leaked: {left}\n---\n{text}"


# --- the shared filter ------------------------------------------------------

def test_drop_notices_removes_the_whole_hook_block():
    out = "\n".join(ss.drop_program_notices(HOOK_SCREEN.splitlines()))
    _none_of(HOOK_WORDS, out)
    assert "Paused halfway" in out and "Left panel: 6 of 12" in out   # before the hook
    assert "Ran 3 shell commands" in out                             # the work after it
    assert "I put the work on pause." in out and "Left: the other half." in out
    assert "Cogitated for 35m 7s" in out


def test_drop_notices_removes_update_lines():
    out = "\n".join(ss.drop_program_notices(UPDATE_SCREEN.splitlines()))
    _none_of(UPDATE_WORDS, out)


def test_drop_notices_other_hook_shapes():
    lines = [
        "● Done.",
        "  ⎿  Stop hook feedback:",
        "  Do not stop yet: this session has an open task on the board.",
        "● Ran 2 PreToolUse hooks",
        "  ⎿  PreToolUse:Bash hook error: blocked by guard",
        "● Next answer.",
        "  ⎿  UserPromptSubmit hook error: Failed with non-blocking status code: boom",
        "✗ Auto-update failed · Try claude doctor or npm i -g @anthropic-ai/claude-code",
        "● Last answer.",
    ]
    out = "\n".join(ss.drop_program_notices(lines))
    _none_of(("hook", "Do not stop yet", "Auto-update failed"), out)
    for keep in ("Done.", "Next answer.", "Last answer."):
        assert keep in out


def test_drop_notices_keeps_ordinary_text_about_hooks():
    """The agent may talk about hooks; only the program's own lines go."""
    lines = [
        "● I added a stop hook to settings.json — it runs the tests.",
        "  The update was installed on the server; restart nginx to apply it.",
        "  Ran 1 shell command",
    ]
    assert ss.drop_program_notices(lines) == lines


# --- every path that sends the screen to the chat ---------------------------

def test_clean_pane_has_no_notices():           # /read and the re-read after a pick
    _none_of(HOOK_WORDS + UPDATE_WORDS, tb.clean_pane(HOOK_SCREEN + "\n" + UPDATE_SCREEN))
    assert "I put the work on pause." in tb.clean_pane(HOOK_SCREEN)


def test_screen_mirror_has_no_notices():        # the live view and the final reply
    mir = tb._screen_mirror("❯ go on\n" + HOOK_SCREEN, "go on")
    _none_of(HOOK_WORDS, mir)
    assert "I put the work on pause." in mir and "Cogitated for 35m 7s" in mir

    live = tb._screen_mirror(LIVE_SCREEN, "hi there")
    _none_of(UPDATE_WORDS, live)
    assert "Skedaddling" in live

    final = tb._screen_mirror(FINAL_SCREEN, "hi there")
    _none_of(UPDATE_WORDS, final)
    assert final == "Hi! How can I help?\n\n✻ Brewed for 1s · done 2:40 PM"


def test_screen_tail_has_no_notices():
    _none_of(HOOK_WORDS, tb._screen_tail(HOOK_SCREEN, "", n=40))
