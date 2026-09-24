#!/usr/bin/env python3
"""Telegram ↔ tmux terminal bridge.

Relays between a Telegram chat (owner only) and the agent tmux sessions — the
session library's topics (tmux cs-<id>, see library.py) and, during the move to
the library, the old numbered claude-terminal[-N] slots:

  - a plain text message is typed into the *current* topic (tmux send-keys),
    then the bot streams progress and posts the agent's reply; before typing,
    `library_cli.py ensure <id>` loads the topic if it was unloaded;
  - /use <text>   — pick the current topic by a part of its name or its id
                    (library.resolve; several matches → buttons); no text →
                    button menu. `/use N` (a legacy slot number) still works as
                    the transition fallback: the topic migrated from slot N
                    (even an archived one — then it refuses), else the old
                    claude-terminal-N. A migrated slot's old launch script is
                    never run again (it would be a 2nd Claude on one uuid).
  - owner text is typed only when the pane runs Claude
    (`library_cli.py pane-is-claude`, or pane_current_command for a legacy
    terminal), with exact tmux targets (=name / =name:) and `send-keys -l --`.
  - /new <name>   — create a topic, select it and load it
  - /list         — topics, loaded ones first, + which is current
  - /read         — re-read the current terminal screen now
  - /esc          — interrupt the agent (Escape ×2)
  - /enter        — send a bare Enter

The current selection is stored per chat as the topic id (8 hex), or as the
slot number for a legacy terminal.

Design (see plan reflective-sauteeing-anchor):
  - the agent's REPLY TEXT (live + final) is read from the session transcript
    .jsonl — the agent's actual words, clean, no TUI scraping;
  - the visible pane is used ONLY for the "is it working?" boolean and for
    AskUserQuestion menu detection/navigation — never for reply content.

Only the owner (TG_BRIDGE_OWNER) may use it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid as _uuid

import status_server as ss  # same dir: SESSIONS, strip_ansi, is_junk
import library              # topic registry (.sessions/library.json)
import library_cli          # tmux socket choice, transcript path of a topic

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import TelegramError, RetryAfter
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

log = logging.getLogger("tgbridge")

TOKEN = os.environ["TG_BRIDGE_TOKEN"]
OWNER_ID = int(os.environ.get("TG_BRIDGE_OWNER", "0"))  # set to your Telegram numeric user id
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tg_bridge_state.json")
GATE_DIR = os.path.dirname(os.path.abspath(__file__))
TG_LIMIT = 3900       # keep under Telegram's 4096 with headroom (send_message)
TG_EDIT_LIMIT = 4096  # edit_text hard limit
MAX_REPLY = 20000     # cap a very long reply (keep the tail), then chunk (~200 lines)
# a turn is finished only when the agent's message ends with one of these — NOT
# "tool_use" (mid-turn). Using this instead of the flickery 'esc to interrupt'
# spinner stops the bot from finalizing a truncated '🔧 Bash:' line as the reply.
TERMINAL_STOPS = {"end_turn", "stop_sequence", "stop", "max_tokens"}

# agent id -> tmux session name (only the resumable claude agents)
SESSIONS = {t["id"]: t["session"] for t in ss.SESSIONS
            if t["session"].startswith("claude-terminal")}

# uploaded files are saved here and served at the public URL (served by nginx on
# reimake.com — NEVER yanhs.stream, which is deprecated). 20 MB is Telegram's
# bot getFile cap, so anything bigger can't be downloaded by the bot at all.
TGFILES_DIR = os.environ.get("TG_FILES_DIR", "/home/ubuntu/pr/tgfiles")
TGFILES_URL = os.environ.get("TG_FILES_URL", "https://reimake.com/tgfiles")
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
# voice notes are transcribed with faster-whisper, which the bridge's own python
# (system /usr/bin/python3) does NOT have — so we shell out to a venv python that
# does, via whisper_transcribe.py, keeping the model out of the bridge process.
WHISPER_PY = os.environ.get(
    "TG_WHISPER_PY",
    "/home/ubuntu/.cache/pypoetry/virtualenvs/claude-code-telegram-1NskcX1W-py3.11/bin/python")
WHISPER_SCRIPT = os.path.join(GATE_DIR, "whisper_transcribe.py")


# ── tmux helpers ──────────────────────────────────────────────────────────

def _tmux(*args) -> subprocess.CompletedProcess:
    # AGENTDECK_TMUX_SOCKET → `tmux -L <name>`, the same server library_cli uses
    # (tests run on their own server and never reach the live one)
    return subprocess.run(library_cli.tmux_argv(*args), capture_output=True, text=True)


def session_for(agent_id) -> str | None:
    """tmux session of a selection: a topic id → cs-<id>; a slot number → the
    legacy claude-terminal-N; anything else → None."""
    key = str(agent_id)
    if library.valid_id(key):
        return library.tmux_name(key)
    return SESSIONS.get(key)


# Exact targets: a bare "-t cs-aaaa1111" also matches cs-aaaa1111x (tmux falls
# back to a prefix match), which once typed a message into the wrong terminal.
# "=name" pins the session; pane commands need "=name:" (session:window).
def _exact(session: str) -> str:
    return "=" + session


def _pane(session: str) -> str:
    return "=" + session + ":"


def has_session(session: str) -> bool:
    return _tmux("has-session", "-t", _exact(session)).returncode == 0


def _enable_in_dashboard(aid: str) -> None:
    """Add the agent to agents.json `_order` (same as the dashboard's '+ Claude')
    so its launch isn't blocked by the order gate and it shows up in the panel."""
    f = os.path.join(GATE_DIR, "agents.json")
    try:
        data = json.load(open(f))
    except (OSError, ValueError):
        data = {}
    order = data.get("_order")
    if not isinstance(order, list):
        order = []
    if str(aid) not in [str(x) for x in order]:
        order.append(str(aid))
        data["_order"] = order
        try:
            with open(f, "w") as fh:
                json.dump(data, fh)
        except OSError:
            pass


def start_session(aid: str) -> tuple[bool, str]:
    """Launch a stopped agent's tmux session (detached), the same way the dashboard
    '+ Claude' does — enable it in the order list, resolve its claude command via
    DRY_RUN, create the session and send the command. Returns (ok, message)."""
    session = SESSIONS.get(aid)
    if not session:
        return False, f"no terminal #{aid}"
    e = migrated_topic(aid)
    if e is not None:
        # its conversation now lives in the library: the old launch script would
        # start a SECOND Claude on the same uuid
        return False, f"слот #{aid} переехал в тему {topic_label(e)} — старый терминал не запускаю"
    if has_session(session):
        return True, "already running"
    script = "launch-claude.sh" if str(aid) == "1" else f"launch-claude-{aid}.sh"
    spath = os.path.join(GATE_DIR, script)
    if not os.path.exists(spath):
        return False, f"no launch script for #{aid}"
    _enable_in_dashboard(aid)            # selecting via the bot = enable + start
    # resolve the exact claude command the launch script would run
    r = subprocess.run(["bash", spath], capture_output=True, text=True,
                       env={**os.environ, "DRY_RUN": "1"})
    cmd = (r.stdout or "").strip().splitlines()
    cmd = cmd[-1] if cmd else ""
    if not cmd:
        return False, f"could not resolve the launch command for #{aid}"
    _tmux("new-session", "-d", "-s", session, "-c",
          os.environ.get("TG_AGENT_CWD", "/home/ubuntu/pr"))
    _tmux("set", "-t", _exact(session), "mouse", "on")
    time.sleep(0.3)
    _tmux("send-keys", "-t", _pane(session), "-l", "--", cmd)
    time.sleep(0.3)
    _tmux("send-keys", "-t", _pane(session), "Enter")
    return True, f"started #{aid}"


def send_text(session: str, text: str) -> None:
    # Ctrl+U clears the input line first: after an Esc-cancel the TUI can restore
    # the previous command into the box, and without this the new text sticks to
    # it ("…old commandnew message"). C-u on an empty box is a harmless no-op.
    _tmux("send-keys", "-t", _pane(session), "C-u")
    time.sleep(0.1)
    # -l = literal (don't interpret as key names). The TUI debounces pasted
    # input, so a brief pause before Enter is REQUIRED or the Enter is swallowed
    # and the text just sits in the input box, never submitted.
    # "--" ends the options: a message starting with "-" is text, not a flag.
    _tmux("send-keys", "-t", _pane(session), "-l", "--", text)
    time.sleep(0.4)
    _tmux("send-keys", "-t", _pane(session), "Enter")


def send_key(session: str, key: str) -> None:
    _tmux("send-keys", "-t", _pane(session), key)


def capture(session: str, lines: int = 200) -> str:
    r = _tmux("capture-pane", "-pt", _pane(session), "-S", f"-{lines}")
    return r.stdout if r.returncode == 0 else ""


def visible(session: str) -> str:
    """Only the currently visible screen (no scrollback) — an ACTIVE menu and the
    'working' spinner always live here, so detection never matches stale history."""
    r = _tmux("capture-pane", "-pt", _pane(session))
    return r.stdout if r.returncode == 0 else ""


# the foreground command of a pane running Claude Code: "claude" (the process
# name), or its version when Claude sets the process title to it ("2.1.87")
_CLAUDE_CMD = re.compile(r"claude|\d+\.\d+\.\d+")


def is_claude_command(cmd) -> bool:
    return isinstance(cmd, str) and bool(_CLAUDE_CMD.fullmatch(cmd.strip()))


def legacy_pane_command(session: str) -> str | None:
    """#{pane_current_command} of a session's first pane, by exact name.
    list-panes -a + filtering here, never display-message (it segfaults tmux
    3.2a on a target that has just vanished)."""
    r = _tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_current_command}")
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        name, _, cmd = line.partition("\t")
        if name == session:
            return cmd
    return None


# ── session library: topics instead of numbered slots ─────────────────────────
#
# A topic is one Claude conversation, known by its id (8 hex = the start of the
# Claude uuid); it runs in tmux as cs-<id> when loaded. Loading/unloading is
# library_cli.py's job (it keeps at most library.MAX_ACTIVE loaded and never
# unloads a working or watched one) — the bridge only asks it.

LIBRARY_CLI = os.path.join(GATE_DIR, "library_cli.py")
NEED_PICK = "Сначала выберите тему: /use <часть имени или код> · /list · /new <имя>"
TOPIC_NAME_MAX = 100       # a topic name typed in /new
TOPIC_BUTTONS_MAX = 24     # topic buttons in one /use picker
READY_TIMEOUT = 45         # s to wait for a just-loaded Claude to draw its screen
_fresh: dict[str, float] = {}   # cs-<id> → monotonic time the bridge loaded it

_CTRL_NL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")   # control chars except \n
_TUI_MARKERS = ("bypass permissions", "shift+tab", "esc to interrupt", "? for shortcuts")


def _lib_file() -> str:
    return os.environ.get("AGENTDECK_LIBRARY") or library.LIB_FILE


def _load_lib() -> dict:
    return library.load(_lib_file())


def _clip(text: str, n: int = 600) -> str:
    return _CTRL_NL.sub("", str(text or "")).strip()[:n]


def is_topic(key) -> bool:
    return library.valid_id(str(key or ""))


def topic_entry(sid: str, include_archived: bool = False) -> dict | None:
    if not is_topic(sid):
        return None
    e = library.find(_load_lib(), sid)
    if e is None or (e.get("archived") and not include_archived):
        return None
    return e


def topic_label(e: dict) -> str:
    """cs-<id> «name» — the form used in logs and library_cli's messages."""
    name = _clip(e.get("name", ""), 60).replace("\n", " ")
    return f"{library.tmux_name(e['id'])} «{name}»"


def target_label(key) -> str:
    key = str(key or "")
    if is_topic(key):
        e = topic_entry(key, include_archived=True)
        return topic_label(e) if e else library.tmux_name(key)
    return f"#{key}"


def migrated_topic(slot: str) -> dict | None:
    """The topic migrated from legacy slot N (library.migrate_from_slots sets
    `legacy_slot`), archived ones included: once a slot has moved into the
    library, its old launch script must never run again (a second Claude on the
    same conversation), so `/use N` goes to the topic or refuses."""
    try:
        rows = _load_lib()["sessions"]
    except (OSError, ValueError, KeyError):
        return None
    for e in rows:
        if str(e.get("legacy_slot", "")) == str(slot) and is_topic(e.get("id")):
            return e
    return None


def topic_transcript(sid: str) -> str | None:
    e = topic_entry(sid, include_archived=True)
    if e is None:
        return None
    try:
        _, u = library_cli.checked_entry(e)
    except ValueError:
        return None
    return library_cli.transcript_path(os.path.expanduser("~"), library_cli.effective_cwd(e), u)


def run_library_cli(*args, timeout: int = 60) -> subprocess.CompletedProcess:
    # always called off the event loop (asyncio.to_thread) and always bounded
    return subprocess.run([sys.executable, LIBRARY_CLI, *args],
                          capture_output=True, text=True, timeout=timeout)


ENSURE_ELSEWHERE = 4


def ensure_topic(sid: str) -> tuple[int, str]:
    """`library_cli.py ensure <id>` → (exit code, its message). 0 = running now
    (the message may say which idle topic was unloaded to make room); 2 = no such
    topic / archived; 3 = every loaded topic is busy, nothing could be unloaded;
    4 = this conversation already runs elsewhere (refused: never two Claudes on
    one conversation)."""
    try:
        r = run_library_cli("ensure", sid, timeout=60)
    except (OSError, subprocess.SubprocessError) as ex:
        return 1, f"library_cli не отработал: {ex}"
    msg = _clip(r.stderr)
    if r.returncode == ENSURE_ELSEWHERE:
        msg = ("эта переписка уже открыта в другом месте (другой терминал) — второй "
               "Claude на ней не запускаю, сообщение не отправлено. Закройте её там "
               "или пишите туда." + (f"\n({msg})" if msg else ""))
    return r.returncode, msg


def pane_is_claude(sid, session: str) -> bool:
    """Is Claude the program in the pane right now? Owner text is typed only
    then — into a shell it would run as commands. A topic asks
    `library_cli.py pane-is-claude <id>` (exit 0 = yes); a legacy terminal is
    checked by its pane_current_command. Anything unclear = no."""
    if is_topic(sid):
        try:
            return run_library_cli("pane-is-claude", str(sid), timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        return is_claude_command(legacy_pane_command(session))
    except (OSError, subprocess.SubprocessError):
        return False


def loaded_topics() -> dict | None:
    """id → {attached, working, last_output} of the loaded topics, from
    `library_cli.py active`; None when that could not be found out."""
    try:
        r = run_library_cli("active", timeout=15)
        if r.returncode != 0:
            return None
        rows = json.loads(r.stdout or "[]")
        return {x["id"]: x for x in rows if isinstance(x, dict) and is_topic(x.get("id"))}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def tui_ready(pane: str) -> bool:
    """True once Claude's TUI is on screen (status bar, spinner or a question) —
    not while the shell is still echoing the launch command."""
    low = ss.strip_ansi(pane or "").lower()
    return any(m in low for m in _TUI_MARKERS + _MENU_MARKERS)


async def _ready_sleep(s: float) -> None:
    await asyncio.sleep(s)


async def _wait_ready(session: str, timeout: float | None = None) -> bool:
    """A topic the bridge just loaded needs a few seconds before keystrokes
    reach Claude (typed earlier, they land in the shell). No-op otherwise.
    False = Claude never came up: the caller must NOT send."""
    timeout = READY_TIMEOUT if timeout is None else timeout
    t0 = _fresh.get(session)
    if t0 is None:
        return True
    while True:
        if tui_ready(await asyncio.to_thread(visible, session)):
            _fresh.pop(session, None)
            return True
        if time.monotonic() - t0 >= timeout:
            _fresh.pop(session, None)
            log.info("READY-TIMEOUT %s — not sending", session)
            return False
        await _ready_sleep(0.5)


async def _load_topic(sid: str) -> tuple[str | None, str]:
    """Make sure the topic runs: (cs-<id>, notice) or (None, why not)."""
    name = library.tmux_name(sid)
    was = await asyncio.to_thread(has_session, name)
    code, msg = await asyncio.to_thread(ensure_topic, sid)
    if code != 0:
        log.info("ENSURE %s -> exit %s: %s", name, code, msg[:200])
        return None, msg or f"library_cli ensure: код {code}"
    if not was:
        _fresh[name] = time.monotonic()
    return name, msg


def resolve_current(chat_id: int) -> str | None:
    """The chat's selection; a legacy slot that has since been migrated into a
    topic is switched to that topic (and saved)."""
    cur = get_current(chat_id)
    if cur and not is_topic(cur):
        e = migrated_topic(cur)     # archived too: ensure then refuses, never the old slot
        if e is not None:
            set_current(chat_id, e["id"])
            return e["id"]
    return cur


# ── conversation logging (so the exact bytes the user sees are recorded) ─────

CONVO_LOG = os.environ.get("TG_CONVO_LOG", os.path.join(GATE_DIR, "tg_convo.log"))


def _convo(direction: str, text: str, extra: str = "") -> None:
    """Record every message the bot sends/receives — the byte-exact content the
    user sees in Telegram — to journald AND tg_convo.log, for review/debugging."""
    body = (text or "").replace("\n", "\\n")
    log.info("CONVO %s %s | %s", direction, extra, body[:400])
    try:
        with open(CONVO_LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {direction} {extra} | {(text or '')[:3800]!r}\n")
    except OSError:
        pass


# ── safe Telegram calls (never let a transient network error kill a loop) ────

def _retry_after_secs(e) -> float:
    """Seconds to wait from a RetryAfter, tolerant of PTB's upcoming switch of
    `retry_after` from int to datetime.timedelta."""
    ra = getattr(e, "retry_after", 0) or 0
    return ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)


async def _safe_edit(message, text: str, reply_markup=None, retries: int = 0) -> bool:
    """Edit a message, swallowing transient errors. On flood control (RetryAfter)
    with retries>0, WAIT the requested time and retry — so the FINAL reply is
    delivered even when a long stream tripped Telegram's edit rate limit."""
    for attempt in range(retries + 1):
        try:
            await message.edit_text(text, reply_markup=reply_markup)
            _convo("EDIT-OK", text, f"kb={'1' if reply_markup else '0'}")
            return True
        except RetryAfter as e:
            _convo("EDIT-FLOOD", f"retry in {_retry_after_secs(e)}s (attempt {attempt + 1}/{retries + 1})", "")
            if attempt < retries:
                await asyncio.sleep(_retry_after_secs(e) + 1)
                continue
            return False
        except TelegramError as e:
            # "message is not modified" is benign (content unchanged); log the rest
            _convo("EDIT-FAIL", f"{type(e).__name__}: {e}", "")
            return False
    return False


async def _safe_send(bot, chat_id: int, text: str, retries: int = 0):
    """Send a message, swallowing transient errors; wait+retry on flood control
    (RetryAfter) when retries>0 so a chunked final reply isn't lost to the limit."""
    for attempt in range(retries + 1):
        try:
            m = await bot.send_message(chat_id, text)
            _convo("SEND-OK", text, f"chat={chat_id}")
            return m
        except RetryAfter as e:
            _convo("SEND-FLOOD", f"retry in {_retry_after_secs(e)}s (attempt {attempt + 1}/{retries + 1})", f"chat={chat_id}")
            if attempt < retries:
                await asyncio.sleep(_retry_after_secs(e) + 1)
                continue
            return None
        except TelegramError as e:
            _convo("SEND-FAIL", f"{type(e).__name__}: {e}", f"chat={chat_id}")
            return None
    return None


# ── stream persistence: survive a bot restart (don't freeze a half-streamed
#    message — resume it on startup and post the final the user missed) ────────

STREAMS_FILE = os.path.join(GATE_DIR, ".tg_bridge_streams.json")


def _streams_load() -> dict:
    try:
        with open(STREAMS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _streams_save(d: dict) -> None:
    try:
        with open(STREAMS_FILE, "w") as f:
            json.dump(d, f)
    except OSError:
        pass


def _stream_register(chat_id: int, message_id: int, session: str, aid: str,
                     user_text: str, baseline: set[str]) -> None:
    d = _streams_load()
    d[str(message_id)] = {"chat_id": chat_id, "session": session, "aid": aid,
                          "user_text": user_text, "baseline": list(baseline)}
    _streams_save(d)


def _stream_done(message_id: int) -> None:
    d = _streams_load()
    if d.pop(str(message_id), None) is not None:
        _streams_save(d)


class _ResumableMsg:
    """Stand-in for a Telegram Message after a restart — edits by (chat_id,
    message_id) so a resumed stream keeps writing to the same message."""
    def __init__(self, bot, chat_id: int, message_id: int):
        self._bot = bot
        self.chat_id = chat_id
        self.message_id = message_id

    def get_bot(self):
        return self._bot

    async def edit_text(self, text: str, reply_markup=None):
        await self._bot.edit_message_text(text, chat_id=self.chat_id,
                                          message_id=self.message_id, reply_markup=reply_markup)


# ── pure helpers ──────────────────────────────────────────────────────────

def is_working(pane: str) -> bool:
    """True while the Claude TUI is generating (spinner shows 'esc to interrupt')."""
    return "esc to interrupt" in pane.lower()


def clean_pane(pane: str) -> str:
    """Strip ANSI + TUI chrome, collapse blank runs → readable text (for /read)."""
    lines = []
    for raw in pane.splitlines():
        line = ss.strip_ansi(raw).rstrip()
        if not line.strip():
            if lines and lines[-1].strip():
                lines.append("")
            continue
        if ss.is_junk(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def chunk(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split long text into Telegram-sized pieces on line boundaries."""
    if len(text) <= limit:
        return [text] if text else []
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single very long line
            if cur:
                out.append(cur); cur = ""
            out.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def cap_reply(text: str, max_chars: int = MAX_REPLY) -> str:
    """Cap a very long reply, keeping the tail (the most recent output)."""
    return ("…\n" + text[-max_chars:]) if len(text) > max_chars else text


# ── session transcript = source of truth for the reply ──────────────────────

_SID_RE = re.compile(r'AGENT_SESSION_ID="([^"]+)"')


def transcript_path(aid: str) -> str | None:
    """The session transcript .jsonl: a topic's from the registry (uuid + cwd),
    a legacy agent's from its launch script."""
    if is_topic(aid):
        return topic_transcript(str(aid))
    script = "launch-claude.sh" if str(aid) == "1" else f"launch-claude-{aid}.sh"
    try:
        text = open(os.path.join(GATE_DIR, script)).read()
    except OSError:
        return None
    m = _SID_RE.search(text)
    if not m:
        return None
    return os.path.expanduser(f"~/.claude/projects/-home-ubuntu-pr/{m.group(1)}.jsonl")


def _summarize_tool(block: dict) -> str:
    """A one-line summary of a tool_use block, e.g. '🔧 Bash: ls -la'."""
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    s = ""
    if isinstance(inp, dict):
        if name == "Bash":
            s = (inp.get("command") or "").strip().splitlines()[0] if inp.get("command") else ""
        elif name in ("Edit", "Write", "Read", "NotebookEdit"):
            s = inp.get("file_path") or inp.get("notebook_path") or ""
        elif name in ("Glob", "Grep"):
            s = inp.get("pattern") or ""
        elif name == "Task":
            s = inp.get("description") or ""
        elif name == "AskUserQuestion":
            qs = inp.get("questions") or []
            s = "; ".join(q.get("question", "") for q in qs if isinstance(q, dict))
    s = (s or "").strip().replace("\n", " ")[:80]
    return f"🔧 {name}" + (f": {s}" if s else "")


def _record_parts(msg: dict) -> tuple[str, str]:
    """(text, tools) for one assistant message: text = joined text blocks
    verbatim; tools = joined tool_use one-liners (AskUserQuestion excluded — it's
    surfaced as buttons). Thinking is skipped."""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip(), ""
    if not isinstance(content, list):
        return "", ""
    texts, tools = [], []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            txt = (b.get("text") or "").strip()
            if txt:
                texts.append(txt)
        elif t == "tool_use" and b.get("name") != "AskUserQuestion":
            tools.append(_summarize_tool(b))
    return "\n".join(texts).strip(), "\n".join(tools).strip()


def _render_record(msg: dict) -> str:
    """Text + tool one-liners combined (live view / tests)."""
    text, tools = _record_parts(msg)
    return "\n".join(p for p in (text, tools) if p).strip()


def _records(path: str | None, tail_bytes: int = 1_000_000) -> list[dict]:
    """Parsed assistant messages from the transcript tail (skips model=='<synthetic>'
    limit notices and isSidechain subagent chatter): [{uuid, text, tools, stop}]."""
    if not path:
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()
            data = f.read()
    except OSError:
        return []
    out = []
    for raw in data.splitlines():
        if b'"assistant"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if obj.get("type") != "assistant" or obj.get("isSidechain"):
            continue
        msg = obj.get("message", {})
        if not isinstance(msg, dict) or msg.get("model") == "<synthetic>":
            continue
        text, tools = _record_parts(msg)
        out.append({"uuid": obj.get("uuid", ""), "text": text, "tools": tools,
                    "stop": msg.get("stop_reason")})
    return out


def assistant_records(path: str, tail_bytes: int = 1_000_000) -> list[tuple[str, str]]:
    """[(uuid, rendered)] — kept for baseline capture and tests."""
    return [(r["uuid"], "\n".join(p for p in (r["text"], r["tools"]) if p).strip())
            for r in _records(path, tail_bytes)]


def baseline_uuids(path: str | None) -> set[str]:
    return {r["uuid"] for r in _records(path)}


def turn_state(path: str | None, baseline: set[str]) -> tuple[str, str, bool]:
    """For the agent's NEW output since `baseline`: (live, final, done).
      live  = text + tool one-liners (progress view);
      final = text only — or tool one-liners if there's no text — the clean reply;
      done  = the turn finished (a new record has a TERMINAL stop_reason, not
              'tool_use'). `done` is what stops the bot from posting a mid-turn
              '🔧 Bash:' line as the answer."""
    new = [r for r in _records(path) if r["uuid"] and r["uuid"] not in baseline]
    live = "\n".join(p for r in new for p in (r["text"], r["tools"]) if p).strip()
    text = "\n".join(r["text"] for r in new if r["text"]).strip()
    tools = "\n".join(r["tools"] for r in new if r["tools"]).strip()
    done = any(r["stop"] in TERMINAL_STOPS for r in new)
    return live, (text or tools), done


def render_new(path: str | None, baseline: set[str]) -> str:
    """The agent's new text/tool output since `baseline` (the live body)."""
    return turn_state(path, baseline)[0]


def last_askuserquestion(path: str | None, baseline: set[str]) -> dict | None:
    """The most recent pending AskUserQuestion tool_use input (question+options)
    among the new records — used to enrich the on-screen menu with clean labels."""
    if not path:
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > 1_000_000:
                f.seek(size - 1_000_000); f.readline()
            data = f.read()
    except OSError:
        return None
    found = None
    for raw in data.splitlines():
        if b"AskUserQuestion" not in raw or b'"assistant"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if obj.get("type") != "assistant" or obj.get("uuid", "") in baseline:
            continue
        for b in (obj.get("message", {}).get("content") or []):
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                qs = (b.get("input") or {}).get("questions") or []
                if qs and isinstance(qs[0], dict):
                    q = qs[0]
                    opts = [o.get("label", "") for o in (q.get("options") or []) if isinstance(o, dict)]
                    found = {"question": q.get("question", ""), "labels": opts}
    return found


# ── per-chat state (current terminal) ─────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def get_current(chat_id: int) -> str | None:
    return load_state().get(str(chat_id))


def set_current(chat_id: int, agent_id: str) -> None:
    state = load_state()
    state[str(chat_id)] = str(agent_id)
    save_state(state)


# ── AskUserQuestion menu detection (visible pane only) ──────────────────────

_MENU_MARKERS = ("to navigate", "to select", "esc to cancel")
_OPT_RE = re.compile(r"^❯?\s*(\d+)\.\s+(.+)$")


def parse_menu(pane: str) -> dict | None:
    """If the VISIBLE pane shows an ACTIVE AskUserQuestion menu, return
    {question, options:[(num, title)], current}; else None.

    Strict (no false positives on numbered prose): the footer must be present
    and the options a contiguous 1..N block right above it (skipping the ─────
    divider and ☐/☑ category lines). `current` = the highlighted (❯) option."""
    lines = [ss.strip_ansi(l).rstrip() for l in pane.splitlines()]
    foot = None
    for i in range(len(lines) - 1, -1, -1):
        if any(m in lines[i].lower() for m in _MENU_MARKERS):
            foot = i
            break
    if foot is None:
        return None
    opts = []          # (num, title, is_current), bottom-to-top
    descs = {}         # num -> description (the indented line(s) under the option)
    question = ""
    buf = []           # buffered description lines (bottom-to-top) for the option above
    i = foot - 1
    while i >= 0:
        raw = lines[i]
        s = raw.strip()
        if not s:
            i -= 1
            continue
        m = _OPT_RE.match(s)
        if m:
            num = int(m.group(1))
            opts.append((num, m.group(2).strip(), s.startswith("❯")))
            if buf:                               # the indented lines below it = its description
                descs[num] = " ".join(reversed(buf)).strip()
                buf = []
            if num == 1:
                j = i - 1
                while j >= 0:
                    qs = lines[j].strip()
                    if qs and qs[0] not in "☐☑":
                        question = qs
                        break
                    j -= 1
                break
            i -= 1
            continue
        if s[0] in "☐☑─━═│╌╍┄┅┈┉╴╶":
            buf = []
            i -= 1
            continue
        if raw.startswith(("   ", "\t")):
            buf.append(s)                         # option description line → buffer
            i -= 1
            continue
        break
    opts.reverse()
    nums = [n for n, _, _ in opts]
    if len(nums) < 2 or nums != list(range(1, len(nums) + 1)):
        return None
    current = next((n for n, _, c in opts if c), nums[0])
    return {"question": question, "options": [(n, t) for n, t, _ in opts],
            "current": current, "descs": descs}


def enrich_menu(menu: dict, path: str | None, baseline: set[str]) -> dict:
    """Replace the pane-parsed question/option titles with the cleaner text from
    the transcript's AskUserQuestion tool_use, when the option count matches."""
    aq = last_askuserquestion(path, baseline)
    if not aq:
        return menu
    labels = aq.get("labels") or []
    real = _real_options(menu)
    if len(labels) == len(menu["options"]) and labels:
        menu = dict(menu)
        menu["question"] = aq.get("question") or menu["question"]
        menu["options"] = [(menu["options"][i][0], labels[i]) for i in range(len(labels))]
    elif len(labels) == len(real) and labels:
        # the pane also carried Claude Code's built-ins → relabel ONLY the real
        # options with the clean transcript text; keep the built-ins untouched so
        # free-text submission (_answer_freeform) can still find 'Type something'.
        relabel = {real[i][0]: labels[i] for i in range(len(labels))}
        menu = dict(menu)
        menu["question"] = aq.get("question") or menu["question"]
        menu["options"] = [(n, relabel.get(n, t)) for n, t in menu["options"]]
    elif aq.get("question"):
        menu = dict(menu)
        menu["question"] = aq["question"]
    return menu


# Claude Code appends its OWN actions to every AskUserQuestion picker — 'Type
# something' (free-text) and 'Chat about this instead'. They are NOT agent
# options: they don't move under arrow-nav like the real ones, so a tap on them
# wraps the cursor and mis-selects the first real option (the 'I tapped Type
# something but it counted Red' bug). Never surface them as buttons; the user
# answers free-text by simply typing a message (handled by _answer_freeform).
_BUILTIN_OPTION_PREFIXES = ("type something", "chat about this")


def _is_builtin_option(title: str) -> bool:
    return (title or "").strip().lower().startswith(_BUILTIN_OPTION_PREFIXES)


def _real_options(menu: dict) -> list:
    """The agent's actual options, with Claude Code's built-in trailing actions
    removed. Falls back to the full list if that would leave nothing."""
    real = [(n, t) for n, t in menu["options"] if not _is_builtin_option(t)]
    return real or list(menu["options"])


def _has_freetext(menu: dict) -> bool:
    """True if the picker offers Claude Code's 'Type something' free-text row —
    then the user can answer by simply typing instead of tapping a button."""
    return any((t or "").strip().lower().startswith("type something")
               for _, t in menu["options"])


def _chat_option(menu: dict) -> int | None:
    """The option number of Claude Code's 'Chat about this instead' row, if the
    picker has one — selecting it abandons the question and returns to free chat.
    Surfaced as a '💬 Talk' button (triggered via Escape, never arrow-nav)."""
    for n, t in menu["options"]:
        if (t or "").strip().lower().startswith("chat about this"):
            return n
    return None


def _menu_keyboard(aid: str, menu: dict) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{n}. {t[:48]}", callback_data=f"msel:{aid}:{n}")]
            for n, t in _real_options(menu)]
    if _chat_option(menu) is not None:                 # Claude Code's 'Chat about this instead'
        rows.append([InlineKeyboardButton("💬 Talk", callback_data=f"mchat:{aid}")])
    return InlineKeyboardMarkup(rows)


def _menu_text(menu: dict, chosen: int | None = None) -> str:
    descs = menu.get("descs") or {}
    lines = ["📋 " + (menu["question"] or "Menu:")]
    for num, title in _real_options(menu):
        lines.append(f"{'✅' if num == chosen else '▫️'} {num}. {title}")
        d = descs.get(num)
        if d:
            lines.append(f"      ↳ {d}")          # the option's description from the screen
    if chosen is None and _has_freetext(menu):
        lines.append("✍️ …or just type your answer")
    return "\n".join(lines)[:TG_EDIT_LIMIT]


# ── per-terminal lock ───────────────────────────────────────────────────────

_locks: dict[str, asyncio.Lock] = {}


def lock_for(session: str) -> asyncio.Lock:
    """One lock per terminal so two quick messages don't interleave keystrokes."""
    return _locks.setdefault(session, asyncio.Lock())


# ── the core: stream the reply from the transcript ──────────────────────────

_SPIN_RE = re.compile(r"\(\d+s\s*·")  # "(12s · ↓ …)" spinner counter


def _filter_activity(text: str, ut: str = "") -> list[str]:
    """Drop TUI chrome from cleaned pane text → readable activity lines: working
    spinner, '⎿ Tip:' (+ its wrapped continuation), ctrl/background hints, the ❯
    input-box line, the assistant '●' bullet, and fragments of the user's own
    (possibly wrapped) input `ut`."""
    out, skip_tip = [], False
    for l in text.splitlines():
        s = l.strip()
        low = s.lower()
        if (not s or s[0] in "✻✶✳✽✺✷✸✹✼❋✢❂⊹∗*·" or _SPIN_RE.search(s)
                or (s.endswith("…") and len(s.split()) <= 3)):
            continue  # working/thinking spinner ("* Pontificating…", "· Reticulating…")
        if ut and len(s) > 2 and s in ut:
            continue  # echo of our own input (incl. wrapped continuation lines)
        if "tip:" in low:
            skip_tip = True
            continue
        if skip_tip:
            if l[:1] in (" ", "\t"):
                continue
            skip_tip = False
        if "ctrl+" in low or "to run in background" in low:
            continue
        if ("session-id" in low or "dangerously-skip-permissions" in low
                or "claude code v" in low or "claude max" in low
                or any(g in s for g in "▐▛▜▌▝▘█")):
            continue  # launch command / Claude boot banner (fresh-session noise)
        if s[:1] in "❯›▶":
            continue
        if s.startswith("●"):
            s = s[1:].strip()
        if s:
            out.append(s)
    return out


def _screen_mirror(pane: str, user_text: str = "", n: int = 200) -> str:
    """Faithful mirror of the terminal screen for the CURRENT turn. Anchored at the
    user's question (to cut off prior turns from the scrollback) but the question
    echo itself is dropped — shows the working spinner ('✶ Thinking… (12s)'), what
    the agent does, the 'Crunched for Ns' timing, and the answer. Drops chrome:
    box borders, '⎿ Tip:', ctrl/background hints, boot banner, the ❯ input box,
    the bottom status bar, the session survey, and the echoed question."""
    ut = (user_text or "").replace("\n", " ").strip()
    lines = [ss.strip_ansi(l).rstrip() for l in pane.splitlines()]
    # Anchor at the user's question (INCLUSIVE) so we show only THIS turn — never
    # prior turns from the scrollback. Use a short marker (~25 chars) so it fits on
    # one wrapped screen line. If the question isn't found, fall back to just the
    # recent screen (last ~45 lines) — NOT the whole 200-line scrollback dump.
    m = ut[:25]
    start = None
    if m:
        for i in range(len(lines) - 1, -1, -1):
            if m in lines[i]:
                start = i
                break
    if start is not None:
        seg, cap = lines[start:], n
    else:
        seg, cap = lines[-n:], n               # question scrolled off → keep the SAME
        #                                        size tail (don't shrink the live window)
    out, skip_tip = [], False
    for l in seg:
        s = l.strip()
        low = s.lower()
        if not s:
            if out and out[-1] != "":
                out.append("")                         # keep blank lines (paragraph breaks)
            continue
        if set(s) <= set("─━═-_ │╭╮╰╯┌┐└┘▔▁"):
            continue                                   # borders / dividers
        if "tip:" in low:
            skip_tip = True; continue
        if skip_tip:
            if l[:1] in (" ", "\t") or l[:2] == "  ":
                continue
            skip_tip = False
        if ("ctrl+" in low or "to run in background" in low or "bypass permissions" in low
                or "shift+tab" in low or "focus-events" in low or "tmux.conf" in low
                or "session-id" in low or "skip-perm" in low or "--dangerously" in low
                or "/bin/claude" in low or "@vps" in low or "claude code v" in low
                or "claude max" in low or any(g in s for g in "▐▛▜▌▝▘█")
                or "how is claude doing" in low or "0: dismiss" in low or low == "(optional)"):
            continue                                   # hints / status bar / banner / session survey
        if s[:1] in "❯›▶":
            s = s[1:].strip()                          # input box / question echo → strip marker
        if s.startswith("●"):
            s = s[1:].strip()
        if not s:
            continue                                   # empty input box
        if ut and len(s) > 2 and s in ut:
            continue                                   # the QUESTION echo (+ its wraps) — not wanted
        out.append(s)
    while out and out[-1] == "":
        out.pop()                                      # drop trailing blank lines
    return "\n".join(out[-cap:]).strip()


def _screen_tail(pane: str, user_text: str = "", n: int = 12) -> str:
    """Recent on-screen activity (filtered) — ALWAYS reflects what's happening now
    while the agent works, with no dependence on locating the input line. The
    robust real-time source on any terminal."""
    ut = (user_text or "").replace("\n", " ")
    return "\n".join(_filter_activity(clean_pane(pane), ut)[-n:]).strip()


def _pane_after_input(pane: str, marker: str, n: int = 14) -> str:
    """Cleaned activity AFTER the user's input line (preferred when locatable —
    avoids pre-input/stale content); "" if not found, caller falls back to
    _screen_tail so the live view is never blank."""
    m = (marker or "").strip()[:40]
    if not m:
        return ""
    ut = (marker or "").replace("\n", " ")
    lines = pane.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if m in ss.strip_ansi(lines[i]):
            body = _filter_activity(clean_pane("\n".join(lines[i + 1:])), ut)
            return "\n".join(body[-n:]).strip()
    return ""


def _live_edit_interval(elapsed: float) -> float:
    """Seconds between live spinner edits, growing with how long the reply has
    been streaming. A short reply updates briskly; a multi-minute one slows down
    so it never racks up enough edits to a single message to trip Telegram's
    flood control (which froze the counter and dropped finals before this)."""
    if elapsed < 60:
        return 4.0
    if elapsed < 150:
        return 8.0
    return 15.0


async def _post_final(message, text: str, aid: str) -> None:
    final = cap_reply(text) if text else "(agent finished with no text reply)"
    log.info("OUT #%s reply chars=%d preview=%r", aid, len(final), final[:160])
    parts = chunk(final) or ["(empty)"]
    # retries>0: the final reply MUST land even if the stream tripped flood control
    await _safe_edit(message, parts[0], retries=3)
    for p in parts[1:]:
        await _safe_send(message.get_bot(), message.chat_id, p, retries=3)


async def stream_live(message, session: str, path: str | None, baseline: set[str],
                      aid: str = "", user_text: str = "", timeout: int = 7200) -> None:
    """Stream the agent's reply. Live view = the live screen activity while a tool
    runs (anchored after the user's input), then the growing transcript output.
    The turn is FINISHED only when a new transcript record has a terminal
    stop_reason (end_turn/…), not 'tool_use' — so a mid-turn '🔧 Bash:' line is
    never posted as the answer. Final reply = the agent's TEXT. An AskUserQuestion
    menu → inline buttons (a tap resumes via callback)."""
    loop = asyncio.get_event_loop()
    start = loop.time()
    deadline = start + timeout
    start_deadline = start + 25
    reacted = False
    last_shown = None
    last_mir = None
    settle = 0
    last_edit_t = 0.0      # when we last refreshed the live message (edit throttle)
    flood_until = 0.0      # don't touch the message until this time (flood back-off)
    mid = getattr(message, "message_id", None)
    cid = getattr(message, "chat_id", None)
    if mid and cid:
        _stream_register(cid, mid, session, aid, user_text, baseline)  # survive a bot restart
    try:
        await asyncio.sleep(0.6)
        while loop.time() < deadline:
            vis = visible(session)
            working = is_working(vis)
            menu = parse_menu(vis)                 # visible screen only → no stale menu
            _, _, done = turn_state(path, baseline)  # transcript stop_reason → turn ended?
            mir = _screen_mirror(capture(session, lines=200), user_text)  # screen + scrollback
            if working or mir or menu:
                reacted = True

            if menu:                               # agent asked → interactive buttons
                menu = enrich_menu(menu, path, baseline)
                log.info("OUT #%s MENU q=%r opts=%s", aid, menu["question"], [n for n, _ in menu["options"]])
                await _safe_edit(message, _menu_text(menu), reply_markup=_menu_keyboard(aid, menu))
                return

            # completion: the turn ended (terminal stop_reason) and the spinner is gone
            # → freeze the final screen (question + work + 'Crunched for Ns' + answer).
            if done and not working and mir:
                await _post_final(message, mir, aid)
                return
            # safety net: spinner gone + screen unchanged for ~15s without a terminal
            # stop (transcript lag / no path) → finalize on the stable screen.
            if reacted and not working and mir:
                settle = settle + 1 if mir == last_mir else 0
                last_mir = mir
                if settle >= 6:
                    log.info("OUT #%s settle-finalize", aid)
                    await _post_final(message, mir, aid)
                    return
            else:
                settle = 0
                last_mir = None

            if not reacted and loop.time() > start_deadline:
                log.info("OUT #%s NO-REPLY (agent didn't react)", aid)
                await _safe_edit(message, "🤔 Agent didn't respond (busy, or the input didn't land).\n"
                                          "/read — show the screen, /esc — interrupt.")
                return

            # live: mirror the terminal screen, but THROTTLE edits (interval grows
            # with elapsed time) and back off the whole flood window on RetryAfter,
            # so a long reply never trips Telegram's per-message edit limit (which
            # used to freeze the counter and then drop the final reply).
            now = loop.time()
            shown = (mir or "⏳ …")[-(TG_EDIT_LIMIT - 4):]
            if (shown != last_shown and now >= flood_until
                    and now - last_edit_t >= _live_edit_interval(now - start)):
                try:
                    await message.edit_text(shown)
                    _convo("EDIT-OK", shown, "live")
                    last_shown = shown
                    last_edit_t = now
                except RetryAfter as e:
                    flood_until = now + _retry_after_secs(e) + 1     # stop hammering until it clears
                    _convo("EDIT-FLOOD", f"live back off {_retry_after_secs(e)}s", "")
                except TelegramError as e:
                    _convo("EDIT-FAIL", f"{type(e).__name__}: {e}", "live")
                try:                               # typing… only when we actually refresh
                    await message.get_bot().send_chat_action(message.chat_id, ChatAction.TYPING)
                except TelegramError:
                    pass
            await asyncio.sleep(2.5)

        log.info("OUT #%s TIMEOUT", aid)
        await _post_final(message, _screen_mirror(capture(session, lines=200), user_text) or "(no reply within the time limit)", aid)
    finally:
        if mid and cid:
            _stream_done(mid)


# ── Telegram handlers ─────────────────────────────────────────────────────

def _running_legacy() -> list[str]:
    """Legacy slot numbers whose claude-terminal-N still runs (until migration)."""
    return [aid for aid in sorted(SESSIONS, key=int) if has_session(SESSIONS[aid])]


def _agents_overview(chat_id: int) -> str:
    """/list: topics (loaded first, each group most recent first), then the
    legacy terminals that still run."""
    cur = resolve_current(chat_id)
    live = loaded_topics()
    act = live or {}
    rows = library.display_order(_load_lib(), set(act))
    lines = [f"Текущая: {target_label(cur)}" if cur else NEED_PICK, ""]
    if live is None:
        lines.append("⚠️ не удалось узнать, какие темы загружены (library_cli active) — "
                     "все показаны как выгруженные")
    if rows:
        lines.append("Темы (🟢 загружена · ⚪️ выгружена · ⚙️ работает):")
        for e in rows:
            s = act.get(e["id"])
            name = _clip(e.get("name", ""), 60).replace("\n", " ")
            lines.append(f"{'🟢' if s else '⚪️'} «{name}» · {e['id']}"
                         f"{' ⚙️' if s and s.get('working') else ''}"
                         f"{' ← текущая' if e['id'] == cur else ''}")
    else:
        lines.append("Тем пока нет — /new <имя>")
    legacy = _running_legacy()
    if legacy:
        lines += ["", "Старые терминалы (до переезда, /use N):"]
        lines += [f"🟢 #{aid} ({SESSIONS[aid]}){' ← текущий' if aid == cur else ''}"
                  for aid in legacy]
    return "\n".join(lines)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # library_cli active + tmux queries: off the event loop
    text = await asyncio.to_thread(_agents_overview, update.effective_chat.id)
    for part in chunk(text):
        await update.message.reply_text(part)


def _agent_labels() -> dict:
    """aid -> project label, from the status server (saved or auto-detected)."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:3011/", timeout=3) as r:
            data = json.load(r)
        return {k: (v.get("project") or "") for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        return {}


def _button_text(aid: str, alive: bool, label: str) -> str:
    dot = "🟢" if alive else "⚪️"
    label = (label or "").strip()[:22]
    return f"{dot} #{aid}" + (f" · {label}" if label else "")


def _topic_button(e: dict, state: dict | None) -> InlineKeyboardButton:
    dot = "🟢" if state else "⚪️"
    name = _clip(e.get("name", ""), 28).replace("\n", " ")
    return InlineKeyboardButton(f"{dot} {name} · {e['id']}", callback_data=f"use:{e['id']}")


def _topics_keyboard(entries: list[dict], with_legacy: bool = False) -> InlineKeyboardMarkup:
    """One button per topic (loaded first, then most recent), then — for the
    plain /use picker — the legacy terminals that still run, two per row."""
    act = loaded_topics() or {}
    order = library.display_order({"sessions": entries}, set(act))[:TOPIC_BUTTONS_MAX]
    rows = [[_topic_button(e, act.get(e["id"]))] for e in order]
    if with_legacy:
        legacy = _running_legacy()
        labels = _agent_labels() if legacy else {}
        row = []
        for aid in legacy:
            row.append(InlineKeyboardButton(_button_text(aid, True, labels.get(aid, "")),
                                            callback_data=f"use:{aid}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
    return InlineKeyboardMarkup(rows)


def _use_keyboard() -> InlineKeyboardMarkup:
    return _topics_keyboard(_load_lib()["sessions"], with_legacy=True)


async def _apply_use(chat_id: int, aid: str) -> str:
    set_current(chat_id, aid)
    head = f"✅ Current terminal: #{aid} ({SESSIONS[aid]})"
    if has_session(SESSIONS[aid]):
        return head
    # not running → start it on selection
    ok, msg = await asyncio.to_thread(start_session, aid)
    return head + (f"\n▶️ wasn't running — starting it…" if ok
                   else f"\n⚠️ couldn't start: {msg}")


async def _apply_topic(chat_id: int, e: dict) -> str:
    """Select a topic (stored by id) and load it now, so Claude is up by the
    time the first message arrives. A refusal keeps the selection: the next
    message asks library_cli again."""
    sid = e["id"]
    set_current(chat_id, sid)
    head = f"✅ Текущая тема: {topic_label(e)}"
    name, msg = await _load_topic(sid)
    if name is None:
        return head + f"\n⚠️ не загрузилась: {msg}\nСледующее сообщение попробует снова."
    out = head + ("\n▶️ была выгружена — загружаю…" if name in _fresh else "")
    return out + (f"\nℹ️ {msg}" if msg else "")


async def _use_number(chat_id: int, aid: str) -> str:
    """`/use N` during the transition: the topic migrated from slot N if there
    is one, else the old claude-terminal-N."""
    e = migrated_topic(aid)
    if e is not None:
        if e.get("archived"):
            # never fall back to the old launch script: it would start a second
            # Claude on this very conversation
            return (f"Слот #{aid} переехал в тему {topic_label(e)}, а она в архиве — "
                    "верните её из архива на странице библиотеки. Старый терминал "
                    "не запускаю (это был бы второй Claude на той же переписке).")
        return await _apply_topic(chat_id, e)
    return await _apply_use(chat_id, aid)


async def cmd_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    query = " ".join(ctx.args or []).strip()
    if not query:
        cur = resolve_current(chat_id)
        head = f"Сейчас: {target_label(cur)}. Выберите тему:" if cur else "Выберите тему:"
        kb = await asyncio.to_thread(_use_keyboard)
        await update.message.reply_text(head, reply_markup=kb)
        return
    # a bare legacy slot number is the old form: numbers are too common in topic
    # names ("Налоги 2026") for name search to be the first reading of "/use 6"
    num = query.lstrip("#")
    if num in SESSIONS:
        await update.message.reply_text(await _use_number(chat_id, num))
        return
    hits = library.resolve(_load_lib(), query)
    if not hits:
        await update.message.reply_text(
            f"Темы «{_clip(query, 60)}» нет (или она в архиве). "
            "Все темы: /list · новая: /new <имя>")
        return
    if len(hits) == 1:
        await update.message.reply_text(await _apply_topic(chat_id, hits[0]))
        return
    kb = await asyncio.to_thread(_topics_keyboard, hits)
    await update.message.reply_text(f"Нашлось тем: {len(hits)}. Какую?", reply_markup=kb)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/new <name> — create a topic (name optional: a dated default), select it
    and load it. It starts where the dashboard's «＋ Новая тема» starts one."""
    name = _CTRL_NL.sub("", " ".join(ctx.args or [])).replace("\n", " ").strip()[:TOPIC_NAME_MAX]
    cwd = os.environ.get("AGENTDECK_WORKDIR") or os.path.dirname(GATE_DIR)
    try:
        with library.update(_lib_file()) as lib:
            e = dict(library.create(lib, name, cwd=cwd, now=int(time.time())))
    except (OSError, ValueError) as ex:
        await update.message.reply_text(f"⚠️ не удалось создать тему: {_clip(ex, 200)}")
        return
    log.info("NEW %s", topic_label(e))
    await update.message.reply_text("🆕 " + await _apply_topic(update.effective_chat.id, e))


async def on_use_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if update.effective_user.id != OWNER_ID:
        await q.answer("no access"); return
    await q.answer()
    key = q.data.split(":", 1)[1]
    if is_topic(key):
        e = topic_entry(key)
        if e is None:
            await q.edit_message_text("Такой темы нет (или она в архиве) — /list"); return
        await q.edit_message_text(await _apply_topic(q.message.chat_id, e))
        return
    if key not in SESSIONS:
        await q.edit_message_text("No such terminal"); return
    await q.edit_message_text(await _use_number(q.message.chat_id, key))


async def on_menu_select_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tap an AskUserQuestion option → navigate the TUI to it, Enter, stream reply."""
    q = update.callback_query
    if update.effective_user.id != OWNER_ID:
        await q.answer("no access"); return
    _, aid, num = q.data.split(":")
    target = int(num)
    session = session_for(aid)
    if not session or not has_session(session):
        await q.answer("terminal unavailable"); return
    menu = parse_menu(visible(session))      # re-parse NOW (state may have changed)
    if not menu:
        await q.answer("menu already closed"); return
    title = dict(menu["options"]).get(target, str(target))
    await q.answer(f"✅ {title}")
    path = transcript_path(aid)
    async with lock_for(session):
        baseline = baseline_uuids(path)      # post-answer reply = anything new
        delta = target - menu["current"]
        log.info("SELECT #%s opt=%s current=%s delta=%s", aid, target, menu["current"], delta)
        key = "Down" if delta > 0 else "Up"
        for _ in range(abs(delta)):
            send_key(session, key)
            await asyncio.sleep(0.15)
        send_key(session, "Enter")
        try:
            await q.edit_message_text(_menu_text(menu, chosen=target), reply_markup=None)
        except TelegramError:
            pass
        for _ in range(8):                   # wait for the picker to clear
            await asyncio.sleep(0.4)
            if not parse_menu(visible(session)):
                break
        msg = await ctx.bot.send_message(q.message.chat_id, f"➡️ {target_label(aid)}: …")
        await stream_live(msg, session, path, baseline, aid=aid,
                          user_text="answered Claude's questions")


async def on_menu_chat_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tap '💬 Talk' → abandon the AskUserQuestion and return to free chat
    (Claude Code's 'Chat about this instead'). Cancels the picker with Escape —
    NOT arrow-nav — so it can never accidentally select a real option."""
    q = update.callback_query
    if update.effective_user.id != OWNER_ID:
        await q.answer("no access"); return
    aid = q.data.split(":", 1)[1]
    session = session_for(aid)
    if not session or not has_session(session):
        await q.answer("terminal unavailable"); return
    if not parse_menu(visible(session)):
        await q.answer("menu already closed"); return
    await q.answer("💬")
    path = transcript_path(aid)
    async with lock_for(session):
        baseline = baseline_uuids(path)      # the agent's reply to the decline = anything new
        log.info("CHAT-DECLINE #%s (Escape)", aid)
        send_key(session, "Escape")          # cancel the picker → agent gets a decline
        try:
            await q.edit_message_text("💬 Question dismissed — type and let's talk.", reply_markup=None)
        except TelegramError:
            pass
        for _ in range(8):                   # wait for the picker to clear
            await asyncio.sleep(0.4)
            if not parse_menu(visible(session)):
                break
        msg = await ctx.bot.send_message(q.message.chat_id, f"➡️ {target_label(aid)}: …")
        await stream_live(msg, session, path, baseline, aid=aid,
                          user_text="declined the question to chat")


async def cmd_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = _require_session(update)
    if not s:
        await update.message.reply_text(NEED_PICK); return
    if not has_session(s):
        await update.message.reply_text(
            f"⚪️ {s} не загружен — пришлите сообщение (или /use), и тема загрузится"); return
    text = clean_pane(capture(s)) or "(empty)"
    for part in chunk(text):
        await update.message.reply_text(part)


async def cmd_esc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = _require_session(update)
    if not s:
        await update.message.reply_text(NEED_PICK); return
    log.info("ESC ×2 -> %s", s)
    for _ in range(2):
        send_key(s, "Escape")
        await asyncio.sleep(0.25)
    send_key(s, "C-u")          # the 2nd Escape restores the last command to the
    #                             input box → clear it so the next send doesn't stick
    await update.message.reply_text("⎋ Escape ×2 (interrupt)")


async def cmd_enter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = _require_session(update)
    if not s:
        await update.message.reply_text(NEED_PICK); return
    send_key(s, "Enter")
    await update.message.reply_text("⏎ Enter sent")


async def cmd_compact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = _require_session(update)
    if not s:
        await update.message.reply_text(NEED_PICK); return
    if not has_session(s):
        await update.message.reply_text("⚠️ terminal isn't running"); return
    cur = resolve_current(update.effective_chat.id)
    if not await asyncio.to_thread(pane_is_claude, cur, s):
        await update.message.reply_text(_not_claude_text(target_label(cur))); return
    log.info("COMPACT -> %s", s)
    await asyncio.to_thread(send_text, s, "/compact")
    await update.message.reply_text("🗜 /compact sent")


def _not_claude_text(label: str) -> str:
    return (f"⚠️ {label}: в терминале сейчас не Claude (например, голая оболочка) — "
            "текст НЕ отправлен, иначе он выполнился бы как команды. Посмотрите /read.")


def _require_session(update: Update) -> str | None:
    cur = resolve_current(update.effective_chat.id)
    if not cur:
        return None
    return session_for(cur)


async def _answer_freeform(session: str, menu: dict, text: str) -> bool:
    """The user typed while an AskUserQuestion is open → deliver their text as a
    free-text response. Verified in Claude Code 2.1.183: the 'Type something' row
    doesn't open an answer field — it just DECLINES the question and drops to the
    normal prompt (same as Escape). So we decline with Escape — robust, no fragile
    arrow-nav that could land on the wrong row — then type the text as the message.
    False when the picker has no 'Type something' row (caller shows the buttons)."""
    if not _has_freetext(menu):
        return False
    send_key(session, "Escape")             # decline the picker → normal input prompt
    await asyncio.sleep(0.4)
    await asyncio.to_thread(send_text, session, text)  # type the message + submit
    return True


def _incoming_text(msg) -> str:
    """The text to send to the terminal: the typed text PLUS the message it
    replies to / the forwarded message — so replying-to or forwarding a message
    delivers that content to the terminal too (instead of being ignored)."""
    body = (msg.text or getattr(msg, "caption", None) or "").strip()
    rt = getattr(msg, "reply_to_message", None)
    if rt is not None:
        quoted = (getattr(rt, "text", None) or getattr(rt, "caption", None) or "").strip()
        if quoted:
            body = (quoted + ("\n\n" + body if body else "")).strip()
    return body


# ── uploaded files: save to disk, hand back a public reimake.com link ───────

def _stored_name(original: str, ext: str, token: str) -> str:
    """A unique, URL/shell-safe on-disk name: '<token>_<safe-stem><ext>' (or
    '<token><ext>' when the original name has no ASCII-safe characters, e.g. a
    fully-Cyrillic name). The token guarantees no collision; the readable stem
    keeps the link self-describing."""
    stem, oext = os.path.splitext(os.path.basename(original or ""))
    ext = (ext or oext or "").lower()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("._")[:50]
    return f"{token}_{safe}{ext}" if safe else f"{token}{ext}"


def _public_url(filename: str) -> str:
    return f"{TGFILES_URL.rstrip('/')}/{filename}"


def _media_info(msg):
    """(media_obj, kind, original_name, ext) for a document/photo/video/audio
    message, or None. Voice notes are handled separately (transcribed, not stored)."""
    doc = getattr(msg, "document", None)
    if doc is not None:
        # ".bin" fallback so a document with no name-ext AND an unknown mime still
        # gets a sane extension (mirrors the photo/video/audio defaults below).
        ext = os.path.splitext(doc.file_name or "")[1] or (mimetypes.guess_extension(doc.mime_type or "") or ".bin")
        return doc, "document", (doc.file_name or ""), ext
    photo = getattr(msg, "photo", None)
    if photo:                                    # a list of sizes, ascending → take the largest
        return photo[-1], "photo", "", ".jpg"
    vid = getattr(msg, "video", None)
    if vid is not None:
        name = getattr(vid, "file_name", "") or ""
        ext = os.path.splitext(name)[1] or (mimetypes.guess_extension(vid.mime_type or "") or ".mp4")
        return vid, "video", name, ext
    aud = getattr(msg, "audio", None)
    if aud is not None:
        name = getattr(aud, "file_name", "") or ""
        ext = os.path.splitext(name)[1] or (mimetypes.guess_extension(aud.mime_type or "") or ".mp3")
        return aud, "audio", name, ext
    return None


def transcribe_voice(path: str) -> str:
    """Transcribe an audio file to text with faster-whisper, via a subprocess to a
    venv python that has it — so the bridge process never loads the model itself."""
    r = subprocess.run([WHISPER_PY, WHISPER_SCRIPT, path],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "whisper failed").strip()[-300:] or "whisper failed")
    return r.stdout.strip()


# ── deliver a message into the current terminal and stream the reply ────────

async def _deliver_to_terminal(reply_to, chat_id: int, text: str) -> None:
    """Type `text` into the current terminal and stream the agent's reply, posting
    under `reply_to`. Shared by text, voice transcripts and captioned files.

    The per-session lock is held ONLY around the keystroke send (so two quick
    messages can't interleave keys); it is released BEFORE streaming, so a message
    sent while the agent is still working reaches the terminal right away."""
    if not text:
        return
    cur = resolve_current(chat_id)
    if not cur:
        await reply_to.reply_text(NEED_PICK); return
    label = target_label(cur)
    if is_topic(cur):
        # a topic: library_cli loads it if it was unloaded (or refuses when every
        # loaded topic is busy) — keystrokes go to cs-<id> only after that
        session, note = await _load_topic(cur)
        if session is None:
            await reply_to.reply_text(f"⚠️ {label}: {note}"); return
        if note:
            await reply_to.reply_text(f"ℹ️ {note}")
    else:
        session = SESSIONS.get(cur)
        if not session:
            await reply_to.reply_text(NEED_PICK); return
        if not has_session(session):
            await reply_to.reply_text(f"⚠️ Terminal #{cur} isn't running (no tmux session)."); return
    _convo("IN", text, f"chat={chat_id} {label}")
    starting = time.monotonic() - _fresh.get(session, float("-inf")) < READY_TIMEOUT
    placeholder = await reply_to.reply_text(f"➡️ {label}: …" + ("\n▶️ загружаю тему…" if starting else ""))
    if not await _wait_ready(session):    # no-op unless the bridge just loaded it
        await _safe_edit(placeholder,
                         f"⚠️ {label}: Claude не поднялся за {READY_TIMEOUT} с — сообщение "
                         "не отправлено (иначе оно ушло бы в оболочку). Посмотрите /read "
                         "и пришлите ещё раз.")
        return
    path = transcript_path(cur)
    async with lock_for(session):
        # the last look before typing: only into Claude, never into a shell
        if not await asyncio.to_thread(pane_is_claude, cur, session):
            log.info("NOT-CLAUDE %s — not sending", session)
            await _safe_edit(placeholder, _not_claude_text(label))
            return
        menu = parse_menu(visible(session))
        if menu:
            # a question (AskUserQuestion) is open → submit the text as the answer
            baseline = baseline_uuids(path)
            if not await _answer_freeform(session, menu, text):
                m = enrich_menu(menu, path, baseline)   # no free-text option → show buttons
                await _safe_edit(placeholder, _menu_text(m), reply_markup=_menu_keyboard(cur, m))
                return
            log.info("MENU on #%s -> free-text answer %r", cur, text[:40])
            ut = "answered Claude's questions"
        else:
            baseline = baseline_uuids(path)  # capture BEFORE send (under the lock)
            await asyncio.to_thread(send_text, session, text)
            ut = text
    await stream_live(placeholder, session, path, baseline, aid=cur, user_text=ut)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _deliver_to_terminal(update.message, update.effective_chat.id,
                               _incoming_text(update.message))


async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner sent a file (document/photo/video/audio) → save it and reply with a
    public reimake.com link. A caption means the file is meant for the agent, so
    we also hand the local path + URL to the current terminal."""
    msg = update.message
    info = _media_info(msg)
    if not info:
        return
    media, kind, original, ext = info
    size = getattr(media, "file_size", None)
    if size and size > TG_DOWNLOAD_LIMIT:
        await msg.reply_text(f"⚠️ File is {size // (1024 * 1024)} MB — Telegram won't hand bots files larger than 20 MB.")
        return
    token = _uuid.uuid4().hex[:8]
    filename = _stored_name(original, ext, token)
    dest = os.path.join(TGFILES_DIR, filename)
    try:
        os.makedirs(TGFILES_DIR, exist_ok=True)
        tg_file = await media.get_file()
        await tg_file.download_to_drive(dest)
    except Exception as e:                       # network / size / disk
        _convo("FILE-FAIL", str(e), kind)
        await msg.reply_text(f"⚠️ Could not save the file: {str(e)[:200]}")
        return
    url = _public_url(filename)
    _convo("FILE", f"{kind} {original} -> {dest}", f"url={url}")
    await msg.reply_text(f"📎 {url}")
    caption = (getattr(msg, "caption", None) or "").strip()
    if caption:                                  # the agent is meant to act on this file
        ref = f"{caption}\n\n📎 file: {dest}\n{url}"
        await _deliver_to_terminal(msg, update.effective_chat.id, ref)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner sent a voice note → transcribe it with whisper, show the transcript,
    and send it into the current terminal as if it had been typed."""
    msg = update.message
    voice = getattr(msg, "voice", None)
    if voice is None:
        return
    size = getattr(voice, "file_size", None)
    if size and size > TG_DOWNLOAD_LIMIT:
        await msg.reply_text("⚠️ Voice note is larger than 20 MB — Telegram won't hand it to bots.")
        return
    note = await msg.reply_text("🎙 Transcribing…")
    tmp = os.path.join("/tmp", f"tgvoice_{getattr(voice, 'file_unique_id', 'x')}_{_uuid.uuid4().hex}.oga")
    try:
        tg_file = await voice.get_file()
        await tg_file.download_to_drive(tmp)
        text = await asyncio.to_thread(transcribe_voice, tmp)
    except Exception as e:
        await _safe_edit(note, f"⚠️ Could not transcribe: {str(e)[:200]}")
        return
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if not text:
        await _safe_edit(note, "🎙 Empty transcription (silence?).")
        return
    await _safe_edit(note, f"🎙 \"{text}\"")
    _convo("VOICE", text, f"chat={msg.chat_id}")
    await _deliver_to_terminal(msg, update.effective_chat.id, text)


BOT_COMMANDS = [
    BotCommand("use", "выбрать тему: /use <часть имени или код>"),
    BotCommand("list", "темы: загруженные сверху, текущая отмечена"),
    BotCommand("new", "новая тема: /new <имя>"),
    BotCommand("read", "re-read the current terminal screen"),
    BotCommand("esc", "interrupt the agent (Escape)"),
    BotCommand("enter", "send Enter"),
    BotCommand("compact", "compact the agent's conversation (/compact)"),
]


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(BOT_COMMANDS)
    # resume any streams that were mid-flight when we last stopped (a restart
    # killed them) — re-attach to the same message and post the missed final
    for mid, e in list(_streams_load().items()):
        try:
            msg = _ResumableMsg(app.bot, e["chat_id"], int(mid))
            asyncio.create_task(stream_live(msg, e["session"], transcript_path(e["aid"]),
                                            set(e.get("baseline") or []), aid=e["aid"],
                                            user_text=e.get("user_text", "")))
            log.info("RESUME stream msg=%s #%s", mid, e["aid"])
        except Exception as ex:
            log.warning("resume failed for %s: %s", mid, ex)
    _streams_save({})  # the running tasks re-register themselves


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    app = (Application.builder().token(TOKEN).post_init(_post_init)
           .concurrent_updates(True).build())
    owner = filters.User(user_id=OWNER_ID)
    app.add_handler(CommandHandler("list", cmd_list, filters=owner))
    app.add_handler(CommandHandler("use", cmd_use, filters=owner))
    app.add_handler(CommandHandler("new", cmd_new, filters=owner))
    app.add_handler(CommandHandler("read", cmd_read, filters=owner))
    app.add_handler(CommandHandler("esc", cmd_esc, filters=owner))
    app.add_handler(CommandHandler("enter", cmd_enter, filters=owner))
    app.add_handler(CommandHandler("compact", cmd_compact, filters=owner))
    app.add_handler(CallbackQueryHandler(on_use_cb, pattern=r"^use:"))
    app.add_handler(CallbackQueryHandler(on_menu_select_cb, pattern=r"^msel:"))
    app.add_handler(CallbackQueryHandler(on_menu_chat_cb, pattern=r"^mchat:"))
    # voice → transcribe; other media → save + link. Registered BEFORE on_text so a
    # captioned file routes to on_file (only the first matching handler in a group runs).
    app.add_handler(MessageHandler(owner & filters.VOICE, on_voice))
    app.add_handler(MessageHandler(
        owner & (filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO), on_file))
    # TEXT | CAPTION so a forwarded/replied message (incl. captioned media) also lands
    app.add_handler(MessageHandler(owner & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
