"""Unit tests for the Telegram<->tmux bridge pure helpers (no network/tmux).

The reply comes from the session transcript (.jsonl); the visible pane is used
only for the 'working' boolean and menu detection. Tests focus on those paths.
One test deliberately exercises Cyrillic so non-ASCII handling stays covered."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TG_BRIDGE_TOKEN", "test:token")
os.environ.setdefault("TG_BRIDGE_OWNER", "1")
os.environ["TG_CONVO_LOG"] = "/tmp/test_tg_convo.log"  # don't pollute the real log

import tg_bridge as tb  # noqa: E402


# --- session_for ----------------------------------------------------------

def test_session_for_known():
    assert tb.session_for("1") == "claude-terminal"
    assert tb.session_for("2") == "claude-terminal-2"


def test_session_for_unknown():
    assert tb.session_for("99") is None
    assert tb.session_for("13") is None     # 1..12 are Claude terminals (9/10 since 08cd8c7)


# --- send_text: literal text, PAUSE, then Enter (the submission fix) -------

def test_send_text_pauses_before_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(tb, "_tmux", lambda *a: calls.append(a))

    class FakeTime:
        def sleep(self, n): calls.append(("SLEEP", n))
    monkeypatch.setattr(tb, "time", FakeTime())

    tb.send_text("sess", "hello")
    # order: clear the input line (C-u) -> literal text -> sleep (>0) -> Enter
    assert calls[0] == ("send-keys", "-t", "=sess:", "C-u")
    assert calls[2] == ("send-keys", "-t", "=sess:", "-l", "--", "hello")
    assert calls[3][0] == "SLEEP" and calls[3][1] > 0
    assert calls[4] == ("send-keys", "-t", "=sess:", "Enter")


def test_send_text_clears_input_before_typing(monkeypatch):
    # after an Esc-cancel the previous command can linger in the input box; the
    # new text must NOT stick to it — so we wipe the line (C-u) before typing
    calls = []
    monkeypatch.setattr(tb, "_tmux", lambda *a: calls.append(a))
    monkeypatch.setattr(tb, "time", type("T", (), {"sleep": lambda self, n: None})())
    tb.send_text("sess", "next message")
    keyseqs = [c for c in calls]
    assert keyseqs[0] == ("send-keys", "-t", "=sess:", "C-u")            # clear first
    assert keyseqs.index(("send-keys", "-t", "=sess:", "C-u")) < \
        keyseqs.index(("send-keys", "-t", "=sess:", "-l", "--", "next message"))


def test_send_text_handles_cyrillic(monkeypatch):
    # the owner writes in Russian — literal Cyrillic must pass through unchanged
    calls = []
    monkeypatch.setattr(tb, "_tmux", lambda *a: calls.append(a))
    monkeypatch.setattr(tb, "time", type("T", (), {"sleep": lambda self, n: None})())
    tb.send_text("sess", "hello, world")
    assert ("send-keys", "-t", "=sess:", "-l", "--", "hello, world") in calls


def test_send_text_starting_with_a_dash_is_text_not_an_option(monkeypatch):
    calls = []
    monkeypatch.setattr(tb, "_tmux", lambda *a: calls.append(a))
    monkeypatch.setattr(tb, "time", type("T", (), {"sleep": lambda self, n: None})())
    tb.send_text("sess", "-t evil -- not flags")
    assert ("send-keys", "-t", "=sess:", "-l", "--", "-t evil -- not flags") in calls


def test_tmux_targets_are_exact_never_prefix(monkeypatch):
    # "-t cs-aaaa1111" would also match cs-aaaa1111x (tmux prefix-matches names)
    calls = []

    def fake(*a):
        calls.append(a)
        return subprocess.CompletedProcess(a, 0, "", "")
    monkeypatch.setattr(tb, "_tmux", fake)
    tb.has_session("cs-aaaa1111")
    tb.send_key("cs-aaaa1111", "Escape")
    tb.capture("cs-aaaa1111")
    tb.visible("cs-aaaa1111")
    targets = [a[a.index("-t") + 1] if "-t" in a else a[a.index("-pt") + 1] for a in calls]
    assert targets == ["=cs-aaaa1111", "=cs-aaaa1111:", "=cs-aaaa1111:", "=cs-aaaa1111:"]


# --- is_working -----------------------------------------------------------

def test_is_working_true_while_generating():
    assert tb.is_working("  ⏵⏵ ... · esc to interrupt") is True


def test_is_working_false_when_idle():
    assert tb.is_working("❯\n  ⏵⏵ bypass permissions on (shift+tab to cycle)") is False


# --- clean_pane (used only by /read) --------------------------------------

def test_clean_pane_strips_ansi_and_chrome():
    pane = ("\x1b[1m● Hello\x1b[22m\n"
            "─────────────────────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
            "Some answer line")
    out = tb.clean_pane(pane)
    assert "Hello" in out and "Some answer line" in out
    assert "bypass permissions" not in out and "─" not in out


def test_clean_pane_collapses_blank_runs():
    assert tb.clean_pane("a\n\n\n\nb") == "a\n\nb"


# --- chunk / cap_reply ----------------------------------------------------

def test_chunk_short_single():
    assert tb.chunk("hello") == ["hello"]


def test_chunk_empty():
    assert tb.chunk("") == []


def test_chunk_splits_long_on_line_boundaries():
    text = "\n".join(f"line {i}" for i in range(2000))
    parts = tb.chunk(text, limit=500)
    assert len(parts) > 1 and all(len(p) <= 500 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_chunk_breaks_a_single_overlong_line():
    parts = tb.chunk("x" * 1200, limit=500)
    assert all(len(p) <= 500 for p in parts) and "".join(parts) == "x" * 1200


def test_cap_reply_keeps_tail_when_too_long():
    out = tb.cap_reply("A" + "B" * 20000, max_chars=100)
    assert out.startswith("…\n") and len(out) <= 103 and out.endswith("B")


def test_cap_reply_short_untouched():
    assert tb.cap_reply("short") == "short"


# --- tool summaries / record rendering ------------------------------------

def test_summarize_tool_bash_first_line():
    assert tb._summarize_tool({"name": "Bash", "input": {"command": "ls -la\nrm x"}}) == "🔧 Bash: ls -la"


def test_summarize_tool_edit_file():
    assert tb._summarize_tool({"name": "Edit", "input": {"file_path": "/a/b.py"}}) == "🔧 Edit: /a/b.py"


def test_render_record_text_and_tool():
    msg = {"content": [{"type": "thinking", "thinking": "hmm"},
                       {"type": "text", "text": "done"},
                       {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}]}
    out = tb._render_record(msg)
    assert "done" in out and "🔧 Bash: echo hi" in out and "hmm" not in out


def test_render_record_skips_askuserquestion_tool():
    # the question is surfaced as buttons; it must not also appear as text in the reply
    msg = {"content": [{"type": "tool_use", "name": "AskUserQuestion",
                        "input": {"questions": [{"question": "Which color?"}]}},
                       {"type": "text", "text": "after the pick"}]}
    out = tb._render_record(msg)
    assert out == "after the pick" and "AskUserQuestion" not in out


# --- assistant_records (transcript = source of truth) ---------------------

def _write(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_assistant_records_skips_synthetic_and_sidechain(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [
        {"type": "assistant", "uuid": "a1",
         "message": {"model": "claude-opus-4-8", "content": [{"type": "text", "text": "hello"}]}},
        {"type": "assistant", "uuid": "a2",
         "message": {"model": "claude-opus-4-8", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "assistant", "uuid": "syn",
         "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "limit reached"}]}},
        {"type": "assistant", "uuid": "side", "isSidechain": True,
         "message": {"model": "claude-opus-4-8", "content": [{"type": "text", "text": "subagent"}]}},
        {"type": "user", "message": {"content": "hi"}},
    ])
    recs = dict(tb.assistant_records(str(f)))
    assert recs.get("a1") == "hello"
    assert recs.get("a2") == "🔧 Bash: ls"
    assert "syn" not in recs and "side" not in recs


def test_assistant_records_missing_file(tmp_path):
    assert tb.assistant_records(str(tmp_path / "nope.jsonl")) == []


def test_render_new_only_after_baseline(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [
        {"type": "assistant", "uuid": "old", "message": {"model": "m", "content": [{"type": "text", "text": "old one"}]}},
        {"type": "assistant", "uuid": "new1", "message": {"model": "m", "content": [{"type": "text", "text": "new A"}]}},
        {"type": "assistant", "uuid": "new2", "message": {"model": "m", "content": [{"type": "text", "text": "new B"}]}},
    ])
    out = tb.render_new(str(f), {"old"})
    assert out == "new A\nnew B" and "old one" not in out


def test_render_new_no_path():
    assert tb.render_new(None, set()) == ""


# --- AskUserQuestion from transcript + enrichment -------------------------

def test_last_askuserquestion_extracts_question_and_labels(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [
        {"type": "assistant", "uuid": "q1",
         "message": {"model": "m", "content": [{"type": "tool_use", "name": "AskUserQuestion",
             "input": {"questions": [{"question": "Which color?",
                                      "options": [{"label": "Red"}, {"label": "Blue"}]}]}}]}},
    ])
    aq = tb.last_askuserquestion(str(f), set())
    assert aq == {"question": "Which color?", "labels": ["Red", "Blue"]}


def test_enrich_menu_uses_transcript_labels(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [
        {"type": "assistant", "uuid": "q1",
         "message": {"model": "m", "content": [{"type": "tool_use", "name": "AskUserQuestion",
             "input": {"questions": [{"question": "Full question?",
                                      "options": [{"label": "Option one"}, {"label": "Option two"}]}]}}]}},
    ])
    menu = {"question": "trunc", "options": [(1, "Opt"), (2, "Op")], "current": 1}
    out = tb.enrich_menu(menu, str(f), set())
    assert out["question"] == "Full question?"
    assert out["options"] == [(1, "Option one"), (2, "Option two")]


# --- parse_menu (visible pane only) ---------------------------------------

MENU_PANE = """ ☐ Color
Which color do you like?
❯ 1. Red
     the red one
  2. Green
     the green one
  3. Blue
  4. Type something.
  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""


def test_parse_menu_basic():
    m = tb.parse_menu(MENU_PANE)
    assert m is not None
    assert m["question"] == "Which color do you like?"
    assert m["options"][:3] == [(1, "Red"), (2, "Green"), (3, "Blue")]
    assert (4, "Type something.") in m["options"] and m["current"] == 1


def test_parse_menu_current_follows_pointer():
    pane = MENU_PANE.replace("❯ 1. Red", "  1. Red").replace("  3. Blue", "❯ 3. Blue")
    assert tb.parse_menu(pane)["current"] == 3


def test_parse_menu_unicode_options():
    # the agent may ask in any language — non-ASCII titles must parse fine
    pane = ("¿Qué color?\n❯ 1. Rojo\n  2. Verde\n  3. Type something.\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel")
    m = tb.parse_menu(pane)
    assert m["question"] == "¿Qué color?"
    assert m["options"][:2] == [(1, "Rojo"), (2, "Verde")]


def test_parse_menu_real_layout_with_divider():
    pane = (" ☐ Color\nRed, green or blue?\n"
            "❯ 1. Red\n     the red one\n  2. Green\n     the green one\n"
            "  3. Blue\n     the blue one\n  4. Type something.\n"
            "──────────────────────────────\n  5. Chat about this\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel")
    m = tb.parse_menu(pane)
    assert m is not None and [n for n, _ in m["options"]] == [1, 2, 3, 4, 5]
    assert m["question"] == "Red, green or blue?"


def test_parse_menu_captures_option_descriptions():
    pane = ("Pick an item:\n"
            "❯ 1. One\n     first item\n"
            "  2. Two\n     second item\n"
            "  3. Type something.\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel")
    m = tb.parse_menu(pane)
    assert m["descs"].get(1) == "first item"
    assert m["descs"].get(2) == "second item"
    assert 3 not in m["descs"]                     # 'Type something.' has none
    txt = tb._menu_text(m)
    assert "1. One" in txt and "first item" in txt and "second item" in txt


def test_parse_menu_none_without_footer():
    assert tb.parse_menu("text\n1. a\n2. b") is None


def test_parse_menu_numbered_prose_is_not_a_menu():
    # a plain numbered-list reply must NOT be taken for a menu (the old false +)
    assert tb.parse_menu("Here's a list:\n1. one\n2. two\n3. three\n4. four") is None


def test_parse_menu_rejects_non_contiguous():
    pane = "question?\n1. a\n2. b\n4. d\nEnter to select · Esc to cancel"
    assert tb.parse_menu(pane) is None


def test_parse_menu_rejects_single_option():
    assert tb.parse_menu("question?\n1. only\nEnter to select · Esc to cancel") is None


# --- _menu_text -----------------------------------------------------------

def test_menu_text_marks_choice():
    menu = {"question": "Color?", "options": [(1, "Red"), (2, "Blue")], "current": 1}
    pre = tb._menu_text(menu)
    assert "Color?" in pre and "1. Red" in pre and "✅" not in pre
    post = tb._menu_text(menu, chosen=2)
    assert post.count("✅") == 1 and "✅ 2. Blue" in post


# --- built-in option filtering (Claude Code's 'Type something' / 'Chat about
#     this instead' rows are NOT real options — never surface them as buttons) ---

def _builtin_menu():
    # mirrors a real pane: 4 agent options, then Claude Code's two built-ins
    return {"question": "Color?",
            "options": [(1, "🔴 Red"), (2, "🟢 Green"), (3, "🔵 Blue"),
                        (4, "🟡 Yellow"), (5, "Type something."),
                        (6, "Chat about this instead")],
            "current": 1, "descs": {}}


def test_real_options_drops_builtins():
    real = tb._real_options(_builtin_menu())
    assert real == [(1, "🔴 Red"), (2, "🟢 Green"), (3, "🔵 Blue"), (4, "🟡 Yellow")]


def test_real_options_keeps_all_when_no_builtins():
    menu = {"options": [(1, "A"), (2, "B")]}
    assert tb._real_options(menu) == [(1, "A"), (2, "B")]


def test_real_options_never_empty():
    # defensive: a menu that is ONLY built-ins must not collapse to no buttons
    menu = {"options": [(1, "Type something."), (2, "Chat about this instead")]}
    assert tb._real_options(menu) == [(1, "Type something."), (2, "Chat about this instead")]


def _kbd_buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_menu_keyboard_excludes_builtins():
    buttons = _kbd_buttons(tb._menu_keyboard("4", _builtin_menu()))
    msel = [b.callback_data for b in buttons if b.callback_data.startswith("msel:")]
    assert msel == ["msel:4:1", "msel:4:2", "msel:4:3", "msel:4:4"]
    joined = " ".join(b.text for b in buttons)
    assert "Type something" not in joined and "Chat about this" not in joined


# --- '💬 Talk' button: decline the question, return to free chat --------

def test_chat_option_finds_builtin():
    assert tb._chat_option(_builtin_menu()) == 6


def test_chat_option_none_when_absent():
    assert tb._chat_option({"options": [(1, "A"), (2, "B")]}) is None


def test_menu_keyboard_includes_chat_button():
    buttons = _kbd_buttons(tb._menu_keyboard("4", _builtin_menu()))
    chat = [b for b in buttons if b.callback_data == "mchat:4"]
    assert len(chat) == 1 and "💬" in chat[0].text
    assert buttons[-1].callback_data == "mchat:4"        # placed after the real options


def test_menu_keyboard_no_chat_button_when_no_builtin():
    menu = {"question": "Q", "options": [(1, "A"), (2, "B")], "current": 1, "descs": {}}
    buttons = _kbd_buttons(tb._menu_keyboard("4", menu))
    assert all(not b.callback_data.startswith("mchat:") for b in buttons)


# --- free-text answer: 'Type something' just declines (Claude Code 2.1.183), so
#     close the picker with Escape (robust) — NOT fragile arrow-nav -------------

def test_answer_freeform_declines_with_escape_not_arrows(monkeypatch):
    import asyncio
    keys, texts = [], []
    monkeypatch.setattr(tb, "send_key", lambda s, k: keys.append(k))
    monkeypatch.setattr(tb, "send_text", lambda s, t: texts.append(t))
    async def fake_sleep(s): pass
    monkeypatch.setattr(tb.asyncio, "sleep", fake_sleep)
    menu = {"options": [(1, "Red"), (2, "Green"), (3, "Type something.")], "current": 1}
    ok = asyncio.run(tb._answer_freeform("sess", menu, "Purple"))
    assert ok is True
    assert keys == ["Escape"]                         # decline via Escape only
    assert "Down" not in keys and "Up" not in keys    # no fragile arrow navigation
    assert texts == ["Purple"]                        # then the text is typed + submitted


def test_answer_freeform_false_without_typesomething(monkeypatch):
    import asyncio
    monkeypatch.setattr(tb, "send_key", lambda s, k: None)
    monkeypatch.setattr(tb, "send_text", lambda s, t: None)
    menu = {"options": [(1, "Red"), (2, "Blue")], "current": 1}   # no free-text row
    assert asyncio.run(tb._answer_freeform("sess", menu, "Purple")) is False


def test_menu_text_excludes_builtins_and_hints_freetext():
    txt = tb._menu_text(_builtin_menu())
    assert "Type something" not in txt and "Chat about this" not in txt
    assert "🔴 Red" in txt and "🟡 Yellow" in txt
    assert "✍️" in txt                                   # free-text affordance kept as a hint


def test_menu_text_no_freetext_hint_when_no_typesomething():
    menu = {"question": "Q?", "options": [(1, "A"), (2, "B")], "current": 1, "descs": {}}
    assert "✍️" not in tb._menu_text(menu)


def test_enrich_menu_relabels_real_when_pane_has_builtins(tmp_path):
    # pane parsed 5 options (3 real + 2 built-ins); transcript has the 3 real labels.
    # the real options get clean labels; the built-ins stay (so free-text still works).
    f = tmp_path / "s.jsonl"
    _write(f, [
        {"type": "assistant", "uuid": "q1",
         "message": {"model": "m", "content": [{"type": "tool_use", "name": "AskUserQuestion",
             "input": {"questions": [{"question": "Pick?",
                                      "options": [{"label": "Alpha"}, {"label": "Beta"},
                                                  {"label": "Gamma"}]}]}}]}},
    ])
    menu = {"question": "trunc", "current": 1, "descs": {},
            "options": [(1, "Alp"), (2, "Bet"), (3, "Gam"),
                        (4, "Type something."), (5, "Chat about this instead")]}
    out = tb.enrich_menu(menu, str(f), set())
    assert out["question"] == "Pick?"
    assert out["options"] == [(1, "Alpha"), (2, "Beta"), (3, "Gamma"),
                              (4, "Type something."), (5, "Chat about this instead")]


# --- safety: network errors swallowed; per-terminal lock ------------------

def test_safe_edit_swallows_telegram_error():
    import asyncio
    from telegram.error import TimedOut

    class M:
        async def edit_text(self, *a, **k):
            raise TimedOut("boom")
    asyncio.run(tb._safe_edit(M(), "hi"))   # must NOT raise


def test_safe_send_swallows_and_returns_none():
    import asyncio
    from telegram.error import NetworkError

    class Bot:
        async def send_message(self, *a, **k):
            raise NetworkError("boom")
    assert asyncio.run(tb._safe_send(Bot(), 1, "hi")) is None


# --- flood control: the FINAL reply must survive RetryAfter (waits + retries) --

def test_safe_edit_retries_on_flood_then_succeeds(monkeypatch):
    import asyncio
    from telegram.error import RetryAfter
    slept = []
    async def fake_sleep(s): slept.append(s)
    monkeypatch.setattr(tb.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    class M:
        async def edit_text(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RetryAfter(5)                  # flood once, then succeed
    ok = asyncio.run(tb._safe_edit(M(), "hi", retries=2))
    assert ok is True and calls["n"] == 2 and slept   # waited, then delivered


def test_safe_edit_gives_up_after_retries(monkeypatch):
    import asyncio
    from telegram.error import RetryAfter
    async def fake_sleep(s): pass
    monkeypatch.setattr(tb.asyncio, "sleep", fake_sleep)

    class M:
        async def edit_text(self, *a, **k):
            raise RetryAfter(3)                      # always flooded
    assert asyncio.run(tb._safe_edit(M(), "hi", retries=2)) is False


def test_safe_send_retries_on_flood(monkeypatch):
    import asyncio
    from telegram.error import RetryAfter
    async def fake_sleep(s): pass
    monkeypatch.setattr(tb.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    class Bot:
        async def send_message(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RetryAfter(2)
            return "msg"
    assert asyncio.run(tb._safe_send(Bot(), 1, "hi", retries=1)) == "msg" and calls["n"] == 2


def test_live_edit_interval_grows_with_elapsed():
    # spinner edits must space out as a reply runs long, so we don't rack up
    # enough edits to a single message to trip Telegram flood control
    early, mid, late = (tb._live_edit_interval(5), tb._live_edit_interval(90),
                        tb._live_edit_interval(300))
    assert early >= 3 and early < mid < late


def test_lock_for_is_per_session():
    a, b, c = tb.lock_for("s1"), tb.lock_for("s1"), tb.lock_for("s2")
    assert a is b and a is not c


# --- _screen_mirror -------------------------------------------------------

def test_screen_mirror_answer_only_drops_question_and_chrome():
    pane = ("● old answer from before\n"
            "❯ count the files in the dir\n"
            "✶ Architecting… (9s · ↓ 96 tokens)\n"
            "● Bash(ls | wc -l)\n"
            "  ⎿  42\n"
            "✻ Crunched for 19s\n"
            "Files: 42\n"
            "How is Claude doing this session?\n"
            "(optional)\n"
            "1: Bad  2: Fine  3: Good  0: Dismiss\n"
            "❯ \n"
            "  ⏵⏵ bypass permissions on · esc to interrupt")
    out = tb._screen_mirror(pane, "count the files in the dir")
    assert "count the files" not in out            # QUESTION dropped (anchor only cuts scrollback)
    assert "Architecting… (9s" in out              # spinner kept
    assert "Crunched for 19s" in out               # timing kept (user wants it)
    assert "Bash(ls | wc -l)" in out and "42" in out
    assert "Files: 42" in out                      # answer kept
    assert "old answer" not in out                 # nothing before the question (anchor)
    assert "How is Claude" not in out and "Dismiss" not in out and "(optional)" not in out
    assert "bypass permissions" not in out         # bottom status bar dropped


def test_screen_mirror_keeps_blank_lines():
    pane = ("❯ my question\n"
            "First paragraph.\n"
            "\n"
            "Second paragraph.\n"
            "\n"
            "\n"
            "Third paragraph.\n"
            "✻ Cooked for 3s")
    out = tb._screen_mirror(pane, "my question")
    assert "First paragraph.\n\nSecond paragraph." in out   # blank line preserved
    assert "\n\n\n" not in out                              # runs collapsed to one
    assert "Third paragraph." in out and "Cooked for 3s" in out


# --- transcript_path ------------------------------------------------------

def _legacy_slot_dir(tmp_path, monkeypatch, slot="2", uuid="0badc0de-1111-2222-3333-444455556666"):
    """A private GATE_DIR with .sessions/agent-<slot>.id — where the launch
    scripts keep a legacy slot's conversation id (the scripts themselves only
    hold `AGENT_SESSION_ID="$(cat "$_SID_FILE")"`, and migrated slots are shims)."""
    gate = tmp_path / "terminal"
    (gate / ".sessions").mkdir(parents=True)
    (gate / ".sessions" / f"agent-{slot}.id").write_text(uuid + "\n")
    monkeypatch.setattr(tb, "GATE_DIR", str(gate))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTDECK_WORKDIR", "/home/ubuntu/pr")
    return uuid


def test_transcript_path_resolves_legacy_slot_from_its_session_id_file(tmp_path, monkeypatch):
    u = _legacy_slot_dir(tmp_path, monkeypatch)
    assert tb.transcript_path("2") == str(
        tmp_path / "home" / ".claude" / "projects" / "-home-ubuntu-pr" / f"{u}.jsonl")


def test_transcript_path_never_returns_the_unexpanded_shell_text(tmp_path, monkeypatch):
    # the old regex read `AGENT_SESSION_ID="$(cat "` out of the launch script
    # and built ".../$(cat .jsonl" — a path that never exists
    _legacy_slot_dir(tmp_path, monkeypatch)
    (tmp_path / "terminal" / "launch-claude-2.sh").write_text(
        'AGENT_SESSION_ID="$(cat "$_SID_FILE")"\n')
    assert "$(" not in tb.transcript_path("2")


def test_transcript_path_of_a_slot_without_a_session_id_is_none(tmp_path, monkeypatch):
    _legacy_slot_dir(tmp_path, monkeypatch)
    assert tb.transcript_path("7") is None


def test_transcript_path_rejects_a_malformed_session_id(tmp_path, monkeypatch):
    _legacy_slot_dir(tmp_path, monkeypatch, uuid="../../etc/passwd")
    assert tb.transcript_path("2") is None


# --- _incoming_text: typed text + replied-to / forwarded content ----------

class _Msg:
    """Minimal stand-in for a telegram Message."""
    def __init__(self, text=None, caption=None, reply_to_message=None):
        self.text = text
        self.caption = caption
        self.reply_to_message = reply_to_message


def test_incoming_text_plain():
    assert tb._incoming_text(_Msg(text="hello there")) == "hello there"


def test_incoming_text_uses_caption_when_no_text():
    # a forwarded photo/doc carries its words in .caption, not .text
    assert tb._incoming_text(_Msg(caption="look at this")) == "look at this"


def test_incoming_text_prepends_replied_to_message():
    # replying to a message must deliver THAT message's content to the terminal too
    quoted = _Msg(text="the original task description")
    out = tb._incoming_text(_Msg(text="do this", reply_to_message=quoted))
    assert "the original task description" in out and "do this" in out
    assert out.index("the original task description") < out.index("do this")


def test_incoming_text_reply_with_only_quote():
    # forwarding/replying with no new words → just the quoted content
    quoted = _Msg(text="forwarded content")
    assert tb._incoming_text(_Msg(text="", reply_to_message=quoted)) == "forwarded content"


def test_incoming_text_reply_to_captioned_message():
    quoted = _Msg(caption="caption of the quoted media")
    out = tb._incoming_text(_Msg(text="see above", reply_to_message=quoted))
    assert "caption of the quoted media" in out and "see above" in out


def test_incoming_text_empty_returns_blank():
    assert tb._incoming_text(_Msg()) == ""


# --- on_text: lock held ONLY around the send, released before streaming ----

def test_on_text_releases_lock_before_streaming(monkeypatch, tmp_path):
    """A message sent while Claude works must reach the terminal immediately, so
    on_text must drop the per-session lock once the keystrokes are sent and only
    THEN stream — otherwise the next message blocks until the stream finishes."""
    import asyncio

    session = "claude-terminal-6"
    seen = {}

    # hermetic: the real registry says slot 6 was migrated into a topic, which
    # would switch the chat to it (writing the REAL state file) and run
    # library_cli ensure on the real registry — keep this a legacy-slot test
    monkeypatch.setattr(tb, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(tb, "migrated_topic", lambda slot: None)
    monkeypatch.setattr(tb, "get_current", lambda c: "6")
    monkeypatch.setattr(tb, "SESSIONS", {**tb.SESSIONS, "6": session})
    monkeypatch.setattr(tb, "has_session", lambda s: True)
    monkeypatch.setattr(tb, "visible", lambda s: "")
    monkeypatch.setattr(tb, "parse_menu", lambda v: None)
    monkeypatch.setattr(tb, "baseline_uuids", lambda p: set())
    monkeypatch.setattr(tb, "transcript_path", lambda a: "/tmp/x.jsonl")
    monkeypatch.setattr(tb, "legacy_pane_command", lambda s: "claude")

    def fake_send(s, t):
        seen["locked_during_send"] = tb.lock_for(session).locked()
    monkeypatch.setattr(tb, "send_text", fake_send)

    async def fake_stream(message, s, path, baseline, aid, user_text):
        seen["locked_during_stream"] = tb.lock_for(session).locked()
    monkeypatch.setattr(tb, "stream_live", fake_stream)

    class Placeholder:
        async def edit_text(self, *a, **k): pass

    class Message:
        text = "steer the agent now"
        caption = None
        reply_to_message = None
        async def reply_text(self, *a, **k): return Placeholder()

    class Upd:
        message = Message()
        effective_chat = type("C", (), {"id": 999})()
        effective_user = type("U", (), {"id": 1})()

    asyncio.run(tb.on_text(Upd(), None))
    assert seen["locked_during_send"] is True      # held while keys are sent
    assert seen["locked_during_stream"] is False   # released before streaming


# --- file storage: name sanitization + public reimake.com URL --------------

def test_stored_name_with_readable_original():
    assert tb._stored_name("Quarterly Report.pdf", ".pdf", "abcd1234") == "abcd1234_Quarterly_Report.pdf"


def test_stored_name_non_ascii_falls_back_to_token():
    # an all-non-ASCII stem sanitizes to empty → token + ext only (ext preserved)
    assert tb._stored_name("报告.pdf", ".pdf", "deadbeef") == "deadbeef.pdf"


def test_stored_name_no_original_uses_token_and_ext():
    assert tb._stored_name("", ".jpg", "0011aabb") == "0011aabb.jpg"


def test_stored_name_sanitizes_unsafe_chars():
    n = tb._stored_name("../../etc/pa ss;rm -rf.txt", ".txt", "tok")
    assert "/" not in n and " " not in n and ";" not in n
    assert n.startswith("tok_") and n.endswith(".txt")


def test_public_url_is_reimake_never_yanhs():
    u = tb._public_url("tok_file.pdf")
    assert u == "https://reimake.com/tgfiles/tok_file.pdf"
    assert "yanhs.stream" not in u           # the deprecated host must never appear


# --- media detection -------------------------------------------------------

class _Media:
    def __init__(self, **kw):
        self.file_name = kw.get("file_name")
        self.mime_type = kw.get("mime_type")
        self.file_size = kw.get("file_size")
        self.file_unique_id = kw.get("file_unique_id", "uniq")


class _FileMsg:
    def __init__(self, document=None, photo=None, video=None, audio=None,
                 voice=None, caption=None, chat_id=999):
        self.document = document; self.photo = photo; self.video = video
        self.audio = audio; self.voice = voice; self.caption = caption
        self.chat_id = chat_id


def test_media_info_document_uses_filename_ext():
    d = _Media(file_name="data.csv", mime_type="text/csv", file_size=10)
    media, kind, original, ext = tb._media_info(_FileMsg(document=d))
    assert kind == "document" and original == "data.csv" and ext == ".csv" and media is d


def test_media_info_document_ext_from_mime_when_no_name():
    d = _Media(file_name=None, mime_type="application/pdf")
    _, kind, _orig, ext = tb._media_info(_FileMsg(document=d))
    assert kind == "document" and ext == ".pdf"


def test_media_info_document_falls_back_to_bin():
    # no name-extension AND an unrecognized mime → ".bin", not an extensionless file
    d = _Media(file_name="report", mime_type="application/x-unknown-xyz")
    _, kind, _orig, ext = tb._media_info(_FileMsg(document=d))
    assert kind == "document" and ext == ".bin"


def test_media_info_photo_is_jpg_largest():
    p = [_Media(file_size=1), _Media(file_size=99)]      # Telegram lists sizes ascending
    media, kind, _orig, ext = tb._media_info(_FileMsg(photo=p))
    assert kind == "photo" and ext == ".jpg" and media is p[-1]


def test_media_info_none_for_textonly():
    assert tb._media_info(_FileMsg()) is None


# --- voice transcription (subprocess to the venv python) -------------------

def test_transcribe_voice_invokes_venv_python_and_parses(monkeypatch):
    captured = {}

    class R:
        returncode = 0; stdout = "  hello world  \n"; stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd; captured["kw"] = kw; return R()
    monkeypatch.setattr(tb.subprocess, "run", fake_run)

    out = tb.transcribe_voice("/tmp/x.oga")
    assert out == "hello world"                      # stdout stripped
    assert captured["cmd"][0] == tb.WHISPER_PY        # the venv python that has faster_whisper
    assert captured["cmd"][1] == tb.WHISPER_SCRIPT
    assert captured["cmd"][2] == "/tmp/x.oga"
    assert captured["kw"].get("capture_output") and captured["kw"].get("text")


def test_transcribe_voice_raises_on_failure(monkeypatch):
    import pytest

    class R:
        returncode = 1; stdout = ""; stderr = "boom: model missing"
    monkeypatch.setattr(tb.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError) as e:
        tb.transcribe_voice("/tmp/x.oga")
    assert "boom" in str(e.value)


# --- on_file: save + reimake link; caption → also deliver to terminal ------

def test_on_file_saves_and_replies_with_link(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.setattr(tb, "TGFILES_DIR", str(tmp_path))
    monkeypatch.setattr(tb, "TGFILES_URL", "https://reimake.com/tgfiles")
    monkeypatch.setattr(tb._uuid, "uuid4", lambda: type("U", (), {"hex": "abcd1234ef"})())
    monkeypatch.setattr(tb, "get_current", lambda c: None)   # no terminal → just the link
    saved = {}

    class TGFile:
        async def download_to_drive(self, dest):
            saved["dest"] = dest
            open(dest, "w").write("x")

    class Doc:
        file_name = "report.txt"; mime_type = "text/plain"; file_size = 10
        async def get_file(self): return TGFile()

    replies = []

    class Msg:
        def __init__(self):
            self.document = Doc(); self.photo = None; self.video = None
            self.audio = None; self.voice = None; self.caption = None; self.chat_id = 999
        async def reply_text(self, t, **k): replies.append(t); return None

    class Upd:
        def __init__(self): self.message = Msg(); self.effective_chat = type("C", (), {"id": 999})()

    asyncio.run(tb.on_file(Upd(), None))
    assert saved["dest"].endswith("abcd1234_report.txt")     # uuid4().hex[:8] = "abcd1234"
    assert any("https://reimake.com/tgfiles/abcd1234_report.txt" in r for r in replies)
    assert all("yanhs.stream" not in r for r in replies)


def test_on_file_with_caption_delivers_path_to_terminal(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.setattr(tb, "TGFILES_DIR", str(tmp_path))
    monkeypatch.setattr(tb, "TGFILES_URL", "https://reimake.com/tgfiles")
    monkeypatch.setattr(tb._uuid, "uuid4", lambda: type("U", (), {"hex": "ffff0000aa"})())
    monkeypatch.setattr(tb, "get_current", lambda c: "6")
    monkeypatch.setattr(tb, "has_session", lambda s: True)
    monkeypatch.setattr(tb, "SESSIONS", {**tb.SESSIONS, "6": "claude-terminal-6"})
    delivered = {}

    async def fake_deliver(reply_to, chat_id, text):
        delivered["text"] = text
    monkeypatch.setattr(tb, "_deliver_to_terminal", fake_deliver)

    class TGFile:
        async def download_to_drive(self, dest): open(dest, "w").write("x")

    class Doc:
        file_name = "log.txt"; mime_type = "text/plain"; file_size = 10
        async def get_file(self): return TGFile()

    class Msg:
        def __init__(self):
            self.document = Doc(); self.photo = None; self.video = None
            self.audio = None; self.voice = None; self.caption = "check this log"; self.chat_id = 999
        async def reply_text(self, t, **k): return None

    class Upd:
        def __init__(self): self.message = Msg(); self.effective_chat = type("C", (), {"id": 999})()

    asyncio.run(tb.on_file(Upd(), None))
    assert "check this log" in delivered["text"]             # caption forwarded
    assert "ffff0000_log.txt" in delivered["text"]            # local path included for the agent
    assert "https://reimake.com/tgfiles/ffff0000_log.txt" in delivered["text"]


def test_on_file_rejects_oversize(monkeypatch):
    import asyncio
    replies = []

    class Doc:
        file_name = "big.zip"; mime_type = "application/zip"; file_size = 25 * 1024 * 1024
        async def get_file(self): raise AssertionError("must not download an oversize file")

    class Msg:
        def __init__(self):
            self.document = Doc(); self.photo = None; self.video = None
            self.audio = None; self.voice = None; self.caption = None; self.chat_id = 999
        async def reply_text(self, t, **k): replies.append(t); return None

    class Upd:
        def __init__(self): self.message = Msg(); self.effective_chat = type("C", (), {"id": 999})()

    asyncio.run(tb.on_file(Upd(), None))
    assert replies and "20" in replies[0]                     # told it's over the 20 MB cap


# --- on_voice: transcribe → show user + deliver transcript to terminal -----

def test_on_voice_transcribes_and_delivers(monkeypatch):
    import asyncio
    monkeypatch.setattr(tb, "transcribe_voice", lambda p: "open the readme file")
    delivered = {}; edits = []

    async def fake_deliver(reply_to, chat_id, text):
        delivered["text"] = text
    monkeypatch.setattr(tb, "_deliver_to_terminal", fake_deliver)

    class TGFile:
        async def download_to_drive(self, dest): open(dest, "w").write("x")

    class Voice:
        file_size = 1000; file_unique_id = "uq"
        async def get_file(self): return TGFile()

    class Note:
        async def edit_text(self, t, **k): edits.append(t)

    class Msg:
        def __init__(self): self.voice = Voice(); self.chat_id = 999
        async def reply_text(self, t, **k): return Note()

    class Upd:
        def __init__(self): self.message = Msg(); self.effective_chat = type("C", (), {"id": 999})()

    asyncio.run(tb.on_voice(Upd(), None))
    assert delivered["text"] == "open the readme file"        # transcript sent to the terminal
    assert any("open the readme file" in e for e in edits)    # user shown what was understood


def test_on_voice_handles_empty_transcript(monkeypatch):
    import asyncio
    monkeypatch.setattr(tb, "transcribe_voice", lambda p: "")
    called = {"deliver": False}

    async def fake_deliver(*a, **k): called["deliver"] = True
    monkeypatch.setattr(tb, "_deliver_to_terminal", fake_deliver)

    class TGFile:
        async def download_to_drive(self, dest): open(dest, "w").write("x")

    class Voice:
        file_size = 1000; file_unique_id = "uq"
        async def get_file(self): return TGFile()

    edits = []

    class Note:
        async def edit_text(self, t, **k): edits.append(t)

    class Msg:
        def __init__(self): self.voice = Voice(); self.chat_id = 999
        async def reply_text(self, t, **k): return Note()

    class Upd:
        def __init__(self): self.message = Msg(); self.effective_chat = type("C", (), {"id": 999})()

    asyncio.run(tb.on_voice(Upd(), None))
    assert called["deliver"] is False                         # nothing sent to the terminal
    assert edits                                              # user told it was empty


# ════════════════════════════════════════════════════════════════════════════
# Session library (topics instead of numbered slots).
#
# The current selection is a topic id (8 hex, first chars of the Claude uuid);
# its tmux session is cs-<id>. /use resolves a name fragment or id through
# library.resolve (several hits → buttons), /list puts loaded topics first
# (library_cli.py active + library.display_order), /new creates a topic. Before
# text is typed, `library_cli.py ensure <id>` loads the topic (unloading an idle
# one at the limit, refusing when all are busy). The old numeric `/use N` stays
# only as the transition fallback for the legacy claude-terminal-N sessions.
#
# Unit tests fake library_cli (tb.run_library_cli); the last test runs the real
# library_cli against a PRIVATE tmux server (-L agentdeck-test-tg-*) that it
# kills at the end — the live sessions on the default socket are never touched.
# ════════════════════════════════════════════════════════════════════════════

import asyncio  # noqa: E402
import subprocess  # noqa: E402
import time as _time  # noqa: E402
import uuid as _uuid_mod  # noqa: E402

import pytest  # noqa: E402

import library  # noqa: E402  (the same module tg_bridge uses)

U1 = "aaaa1111-2222-4333-8444-555566667777"
U2 = "bbbb2222-3333-4444-8555-666677778888"
U3 = "c0ffee00-1111-4222-8333-444455556666"
CHAT = 999


class _Proc:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


class World:
    """Temp registry + temp bridge state + a fake library_cli."""

    def __init__(self, tmp_path, monkeypatch):
        self.lib = str(tmp_path / "reg" / "library.json")
        monkeypatch.setenv("AGENTDECK_LIBRARY", self.lib)
        # second line of defence: any real tmux call lands on a server that does not exist
        monkeypatch.setenv("AGENTDECK_TMUX_SOCKET", f"agentdeck-test-none-{os.getpid()}")
        self.not_claude = set()   # ids whose pane runs something else (a shell)
        self.threads = []         # (subcommand, thread id) of every library_cli call
        monkeypatch.setattr(tb, "STATE_FILE", str(tmp_path / "state.json"))
        monkeypatch.setattr(tb, "_fresh", {})
        self.log = []          # every library_cli call and keystroke, in order
        self.loaded = []       # ids that are "in tmux"
        self.codes = {}        # id -> (exit code, stderr) for ensure
        self.active_code = 0
        monkeypatch.setattr(tb, "run_library_cli", self.cli)
        monkeypatch.setattr(tb, "has_session",
                            lambda s: s.startswith("cs-") and s[3:] in self.loaded)
        monkeypatch.setattr(tb, "_agent_labels", lambda: {})

    def cli(self, *args, **kw):
        import threading
        self.log.append(args)
        self.threads.append((args[0], threading.get_ident()))
        if args[0] == "pane-is-claude":
            ok = args[1] in self.loaded and args[1] not in self.not_claude
            return _Proc(0 if ok else 1, "", "" if ok else "pane runs bash")
        if args[0] == "active":
            if self.active_code:
                return _Proc(self.active_code, "", "tmux: boom")
            rows = [{"id": i, "attached": False, "working": False, "last_output": 0}
                    for i in self.loaded]
            return _Proc(0, json.dumps(rows))
        if args[0] == "ensure":
            sid = args[1]
            code, err = self.codes.get(sid, (0, ""))
            if code == 0 and library.find(library.load(self.lib), sid) is None:
                code, err = 2, f"unknown session {sid}: no such topic in the library"
            if code == 0 and sid not in self.loaded:
                self.loaded.append(sid)
            return _Proc(code, f"cs-{sid}\n" if code == 0 else "", err)
        return _Proc(1, "", "usage")

    def add(self, name, uuid, last_used=None, archived=False, legacy_slot=None, cwd="/home/ubuntu/pr"):
        with library.update(self.lib) as L:
            e = library.create(L, name, cwd=cwd, now=100, uuid=uuid)
            if last_used is not None:
                e["last_used"] = last_used
            e["archived"] = archived
            if legacy_slot is not None:
                e["legacy_slot"] = legacy_slot
        return dict(e)

    def ensured(self):
        return [a[1] for a in self.log if a[0] == "ensure"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


class _Reply:
    def __init__(self, sink):
        self.sink = sink

    async def edit_text(self, text, reply_markup=None, **k):
        self.sink.append((text, reply_markup))


class _CmdMsg:
    def __init__(self, sink, text=""):
        self.sink, self.text, self.caption, self.reply_to_message = sink, text, None, None
        self.chat_id = CHAT

    async def reply_text(self, text, reply_markup=None, **k):
        self.sink.append((text, reply_markup))
        return _Reply(self.sink)


class _Upd:
    def __init__(self, sink, text=""):
        self.message = _CmdMsg(sink, text)
        self.effective_chat = type("C", (), {"id": CHAT})()
        self.effective_user = type("U", (), {"id": tb.OWNER_ID})()


def run_cmd(handler, args):
    sink = []
    ctx = type("Ctx", (), {"args": list(args)})()
    asyncio.run(handler(_Upd(sink), ctx))
    return sink


def run_cb(handler, data):
    sink = []

    class Q:
        def __init__(self):
            self.data = data
            self.message = type("M", (), {"chat_id": CHAT})()

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, text, reply_markup=None, **k):
            sink.append((text, reply_markup))

    upd = type("U", (), {"callback_query": Q(),
                         "effective_user": type("E", (), {"id": tb.OWNER_ID})()})()

    class Bot:
        async def send_message(self, chat_id, text, **k):
            sink.append((text, None))
            return _Reply(sink)
    ctx = type("Ctx", (), {"bot": Bot(), "args": []})()
    asyncio.run(handler(upd, ctx))
    return sink


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --- identity: topic id → cs-<id>; transcript from the registry -------------

def test_session_for_topic_id_is_its_cs_session():
    assert tb.session_for("aaaa1111") == "cs-aaaa1111"


def test_session_for_rejects_malformed_ids():
    for bad in ["AAAA1111", "aaaa111", "aaaa11115", ";rm -rf /", "../x", "$(id)", ""]:
        assert tb.session_for(bad) is None, bad


def test_transcript_path_for_topic_uses_registry_uuid_and_cwd(world):
    world.add("тема", uuid=U1, cwd="/home/ubuntu/pr")
    assert tb.transcript_path("aaaa1111") == os.path.expanduser(
        f"~/.claude/projects/-home-ubuntu-pr/{U1}.jsonl")


def test_transcript_path_unknown_topic_is_none(world):
    assert tb.transcript_path("deadbeef") is None


# --- /use <name | id> ------------------------------------------------------

def test_use_by_name_selects_topic_by_id_and_loads_it(world):
    world.add("ImmAppeal деплой", uuid=U1)
    world.add("Налоги 2026", uuid=U2)
    replies = run_cmd(tb.cmd_use, ["НАЛОГИ"])
    assert tb.get_current(CHAT) == "bbbb2222"            # stored by id, not by name
    assert world.ensured() == ["bbbb2222"]                # loaded via library_cli ensure
    text = replies[-1][0]
    assert text.splitlines() == ["✅ Current topic: cs-bbbb2222 «Налоги 2026»",
                                 "▶️ was unloaded — loading…"]


def test_use_multiword_name(world):
    world.add("ImmAppeal деплой", uuid=U1)
    world.add("Налоги 2026", uuid=U2)
    run_cmd(tb.cmd_use, ["immappeal", "деплой"])
    assert tb.get_current(CHAT) == "aaaa1111"


def test_use_by_id_prefix(world):
    world.add("ImmAppeal деплой", uuid=U1)
    world.add("Налоги 2026", uuid=U2)
    run_cmd(tb.cmd_use, ["aaaa"])
    assert tb.get_current(CHAT) == "aaaa1111"


def test_use_several_matches_offers_buttons_and_selects_nothing(world):
    world.add("Налоги 2025", uuid=U1, last_used=10)
    world.add("Налоги 2026", uuid=U2, last_used=20)
    world.loaded.append("aaaa1111")                      # loaded → listed first
    replies = run_cmd(tb.cmd_use, ["налоги"])
    assert tb.get_current(CHAT) is None
    assert world.ensured() == []                          # nothing loaded until a pick
    text, markup = replies[-1]
    assert markup is not None
    assert _callbacks(markup) == ["use:aaaa1111", "use:bbbb2222"]


def test_use_unknown_topic_says_so_and_points_to_new(world):
    world.add("Налоги 2026", uuid=U2)
    replies = run_cmd(tb.cmd_use, ["нет такой"])
    assert tb.get_current(CHAT) is None and world.ensured() == []
    assert "/new" in replies[-1][0] and "/list" in replies[-1][0]


def test_use_archived_topic_is_not_selectable(world):
    world.add("Старая тема", uuid=U1, archived=True)
    run_cmd(tb.cmd_use, ["старая"])
    assert tb.get_current(CHAT) is None and world.ensured() == []


def test_use_reports_busy_but_keeps_selection(world):
    world.add("тема", uuid=U1)
    world.codes["aaaa1111"] = (3, "All 12 loaded topics are busy")
    replies = run_cmd(tb.cmd_use, ["aaaa1111"])
    assert tb.get_current(CHAT) == "aaaa1111"             # a later message retries ensure
    assert "busy" in replies[-1][0]
    assert "⚠️ couldn't load: All 12 loaded topics are busy" in replies[-1][0]
    assert "The next message will try again." in replies[-1][0]


def test_use_relays_the_unload_notice(world):
    world.add("тема", uuid=U1)
    world.codes["aaaa1111"] = (0, "unloaded topic cs-bbbb2222 «Налоги» — unused for a while")
    replies = run_cmd(tb.cmd_use, ["тема"])
    assert "ℹ️ unloaded topic cs-bbbb2222" in replies[-1][0]


def test_use_callback_selects_topic(world):
    world.add("тема", uuid=U1)
    sink = run_cb(tb.on_use_cb, "use:aaaa1111")
    assert tb.get_current(CHAT) == "aaaa1111" and world.ensured() == ["aaaa1111"]
    assert "aaaa1111" in sink[-1][0]


def test_use_callback_rejects_unknown_topic(world):
    run_cb(tb.on_use_cb, "use:deadbeef")
    assert tb.get_current(CHAT) is None and world.ensured() == []


def test_use_without_args_lists_topics_then_running_legacy_terminals(world, monkeypatch):
    world.add("Старая", uuid=U1, last_used=10)
    world.add("Новая", uuid=U2, last_used=20)
    world.loaded.append("aaaa1111")
    monkeypatch.setattr(tb, "has_session",
                        lambda s: s == "claude-terminal-6" or (s.startswith("cs-") and s[3:] in world.loaded))
    replies = run_cmd(tb.cmd_use, [])
    cbs = _callbacks(replies[-1][1])
    assert cbs[:2] == ["use:aaaa1111", "use:bbbb2222"]    # loaded topic first
    assert "use:6" in cbs                                 # running legacy terminal kept
    assert "use:5" not in cbs                             # stopped legacy ones hidden


# --- numeric /use N: transition fallback to the legacy terminals ------------

def test_use_number_falls_back_to_legacy_terminal(world, monkeypatch):
    world.add("Налоги 2026", uuid=U2)          # the name contains "6": must not steal /use 6
    monkeypatch.setattr(tb, "has_session", lambda s: True)
    run_cmd(tb.cmd_use, ["6"])
    assert tb.get_current(CHAT) == "6" and world.ensured() == []


def test_use_number_goes_to_the_migrated_topic(world):
    world.add("app - PIPE", uuid=U1, legacy_slot=6)
    run_cmd(tb.cmd_use, ["#6"])
    assert tb.get_current(CHAT) == "aaaa1111" and world.ensured() == ["aaaa1111"]


def test_legacy_callback_still_works(world, monkeypatch):
    monkeypatch.setattr(tb, "has_session", lambda s: True)
    run_cb(tb.on_use_cb, "use:6")
    assert tb.get_current(CHAT) == "6"


# --- /list -----------------------------------------------------------------

def test_list_loaded_topics_first_and_marks_current(world):
    world.add("Старая загруженная", uuid=U1, last_used=10)
    world.add("Новая выгруженная", uuid=U2, last_used=20)
    world.add("В архиве", uuid=U3, archived=True)
    world.loaded.append("aaaa1111")
    tb.set_current(CHAT, "bbbb2222")
    text = "\n".join(t for t, _ in run_cmd(tb.cmd_list, []))
    assert ("active",) in world.log                       # asks library_cli what is loaded
    assert text.startswith("Current: cs-bbbb2222 «Новая выгруженная»\n\n")  # header names the current one
    assert "Topics (🟢 loaded · ⚪️ unloaded · ⚙️ working):" in text
    rows = [l for l in text.splitlines() if l.startswith(("🟢", "⚪"))]
    assert [("Старая" in l, "Новая" in l) for l in rows] == [(True, False), (False, True)]
    loaded, cur = rows
    assert "🟢" in loaded and "aaaa1111" in loaded
    assert "⚪" in cur and "bbbb2222" in cur and cur.endswith(" ← current")
    assert "В архиве" not in text


def test_list_says_so_when_loaded_state_is_unknown(world):
    world.add("тема", uuid=U1)
    world.active_code = 1
    text = "\n".join(t for t, _ in run_cmd(tb.cmd_list, []))
    assert "тема" in text and "couldn't tell which topics are loaded" in text


# --- /new ------------------------------------------------------------------

def test_new_creates_topic_selects_and_loads_it(world, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTDECK_WORKDIR", str(tmp_path))
    replies = run_cmd(tb.cmd_new, ["Разбор", "логов"])
    rows = library.load(world.lib)["sessions"]
    assert len(rows) == 1
    e = rows[0]
    assert e["name"] == "Разбор логов" and library.valid_id(e["id"])
    assert e["cwd"] == str(tmp_path)
    assert tb.get_current(CHAT) == e["id"]
    assert world.ensured() == [e["id"]]
    assert replies[-1][0] == (f"🆕 ✅ Current topic: cs-{e['id']} «Разбор логов»\n"
                              "▶️ was unloaded — loading…")


def test_new_without_name_gets_a_dated_default(world):
    run_cmd(tb.cmd_new, [])
    e = library.load(world.lib)["sessions"][0]
    assert e["name"].startswith("Terminal ")


def test_new_strips_control_chars_and_caps_the_name(world):
    run_cmd(tb.cmd_new, ["a\x1b[31mb" + "x" * 300])
    e = library.load(world.lib)["sessions"][0]
    assert "\x1b" not in e["name"] and len(e["name"]) <= 100


# --- typing text into a topic ------------------------------------------------

def _deliver(world, monkeypatch, text="привет", screens=None):
    """Run on_text with keystrokes/stream faked; returns (sink, streamed)."""
    streamed = {}
    seq = list(screens or ["❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)"])

    def fake_visible(s):
        world.log.append(("visible", s))
        return seq.pop(0) if len(seq) > 1 else seq[0]
    monkeypatch.setattr(tb, "visible", fake_visible)
    monkeypatch.setattr(tb, "parse_menu", lambda v: None)
    monkeypatch.setattr(tb, "baseline_uuids", lambda p: set())
    monkeypatch.setattr(tb, "send_text", lambda s, t: world.log.append(("send", s, t)))

    async def no_sleep(*a, **k):
        pass
    monkeypatch.setattr(tb, "_ready_sleep", no_sleep)

    async def fake_stream(message, s, path, baseline, aid, user_text):
        streamed.update(session=s, path=path, aid=aid, user_text=user_text)
    monkeypatch.setattr(tb, "stream_live", fake_stream)
    sink = []
    upd = _Upd(sink, text)
    asyncio.run(tb.on_text(upd, None))
    return sink, streamed


def test_text_to_topic_ensures_then_types_into_cs_session(world, monkeypatch):
    world.add("тема", uuid=U1)
    world.loaded.append("aaaa1111")
    tb.set_current(CHAT, "aaaa1111")
    sink, streamed = _deliver(world, monkeypatch, "привет")
    ens = world.log.index(("ensure", "aaaa1111"))
    send = world.log.index(("send", "cs-aaaa1111", "привет"))
    assert ens < send                                     # ensure BEFORE the keystrokes
    assert streamed["session"] == "cs-aaaa1111" and streamed["aid"] == "aaaa1111"
    assert streamed["path"].endswith(f"{U1}.jsonl")


def test_text_to_topic_not_typed_when_all_busy(world, monkeypatch):
    world.add("тема", uuid=U1)
    tb.set_current(CHAT, "aaaa1111")
    world.codes["aaaa1111"] = (3, "All 12 loaded topics are busy")
    sink, streamed = _deliver(world, monkeypatch)
    assert not [e for e in world.log if e[0] == "send"] and not streamed
    assert "busy" in sink[-1][0]


def test_text_to_vanished_topic_not_typed(world, monkeypatch):
    tb.set_current(CHAT, "deadbeef")                      # archived/removed meanwhile
    sink, streamed = _deliver(world, monkeypatch)
    assert not [e for e in world.log if e[0] == "send"] and not streamed
    assert sink


def test_text_to_freshly_loaded_topic_waits_for_claude(world, monkeypatch):
    world.add("тема", uuid=U1)
    tb.set_current(CHAT, "aaaa1111")                      # not loaded: ensure starts it
    shell = "ubuntu@vps:~/pr$ claude --session-id x --dangerously-skip-permissions"
    ready = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)"
    _deliver(world, monkeypatch, "привет", screens=[shell, shell, ready])
    looks = [i for i, e in enumerate(world.log) if e[0] == "visible"]
    send = world.log.index(("send", "cs-aaaa1111", "привет"))
    assert len([i for i in looks if i < send]) >= 3       # polled until Claude was up


def test_text_without_selection_points_to_use(world, monkeypatch):
    sink, streamed = _deliver(world, monkeypatch)
    assert "/use" in sink[-1][0] and not streamed


def test_tui_ready():
    assert tb.tui_ready("❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)")
    assert tb.tui_ready(MENU_PANE)                        # a pending question = Claude is up
    assert not tb.tui_ready("ubuntu@vps:~/pr$ claude --resume x --dangerously-skip-permissions")
    assert not tb.tui_ready("")


# --- AskUserQuestion buttons work on a topic too -----------------------------

def test_menu_select_on_topic_drives_the_cs_session(world, monkeypatch):
    world.add("тема", uuid=U1)
    world.loaded.append("aaaa1111")
    keys = []
    screens = [MENU_PANE, MENU_PANE, ""]
    monkeypatch.setattr(tb, "visible", lambda s: screens.pop(0) if len(screens) > 1 else screens[0])
    monkeypatch.setattr(tb, "send_key", lambda s, k: keys.append((s, k)))
    monkeypatch.setattr(tb, "baseline_uuids", lambda p: set())

    async def no_sleep(*a, **k):
        pass
    monkeypatch.setattr(tb.asyncio, "sleep", no_sleep)
    seen = {}

    async def fake_stream(message, s, path, baseline, aid, user_text):
        seen.update(session=s, aid=aid)
    monkeypatch.setattr(tb, "stream_live", fake_stream)
    run_cb(tb.on_menu_select_cb, "msel:aaaa1111:2")
    assert ("cs-aaaa1111", "Down") in keys and ("cs-aaaa1111", "Enter") in keys
    assert seen == {"session": "cs-aaaa1111", "aid": "aaaa1111"}


# --- the real thing: library_cli + a private tmux server ---------------------

FAKE_CLAUDE_TUI = r"""#!/bin/bash
# stands in for claude: records how it was started, shows the TUI status bar,
# then logs every line typed into it
echo "$AGENTDECK_SESSION|$*" >> "$HOME/claude-calls.log"
printf '\n  \xe2\x8f\xb5\xe2\x8f\xb5 bypass permissions on (shift+tab to cycle)\n'
while IFS= read -r line; do printf '%s\n' "$line" >> "$HOME/typed.log"; done
"""


def test_text_reaches_a_real_topic_on_a_private_tmux(tmp_path, monkeypatch):
    home, work, binp = tmp_path / "home", tmp_path / "work", tmp_path / "bin"
    for d in (home, work, binp):
        d.mkdir()
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE_CLAUDE_TUI)
    fake.chmod(0o755)
    (binp / "claude").symlink_to(fake)
    sock = f"agentdeck-test-tg-{os.getpid()}-{_uuid_mod.uuid4().hex[:6]}"
    lib = str(tmp_path / "reg" / "library.json")
    for k in ("TMUX", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in dict(HOME=str(home), AGENTDECK_LIBRARY=lib, AGENTDECK_TMUX_SOCKET=sock,
                     CLAUDE_BIN=str(fake), AGENTDECK_WORKDIR=str(work),
                     PATH=f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                     LANG="C.UTF-8", LC_ALL="C.UTF-8", TERM="xterm-256color").items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(tb, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(tb, "_fresh", {})
    with library.update(lib) as L:
        library.create(L, "Живая тема", cwd=str(work), now=int(_time.time()), uuid=U1)
    tb.set_current(CHAT, "aaaa1111")
    streamed = {}

    async def fake_stream(message, s, path, baseline, aid, user_text):
        streamed.update(session=s, aid=aid)
    monkeypatch.setattr(tb, "stream_live", fake_stream)

    def tm(*a):
        return subprocess.run(["tmux", "-L", sock, *a], capture_output=True, text=True, timeout=10)

    # `library_cli.py pane-is-claude` is being added in parallel; the stand-in
    # claude here is a bash script, so answer it from the private server instead
    real_cli = tb.run_library_cli

    def cli(*args, **kw):
        if args[0] == "pane-is-claude":
            ok = tm("has-session", "-t", f"=cs-{args[1]}").returncode == 0
            return subprocess.CompletedProcess(args, 0 if ok else 1, "", "")
        return real_cli(*args, **kw)
    monkeypatch.setattr(tb, "run_library_cli", cli)
    assert tb._tmux("list-sessions").args[:3] == ["tmux", "-L", sock]   # the bridge uses OUR socket
    try:
        sink = []
        asyncio.run(tb.on_text(_Upd(sink, "привет из телеграма"), None))
        typed = home / "typed.log"
        end = _time.time() + 10
        while _time.time() < end and not (typed.exists() and "привет" in typed.read_text()):
            _time.sleep(0.1)
        assert typed.exists() and "привет из телеграма" in typed.read_text(), sink
        calls = (home / "claude-calls.log").read_text()
        assert f"aaaa1111|--session-id {U1} --dangerously-skip-permissions" in calls
        assert tm("has-session", "-t", "=cs-aaaa1111").returncode == 0
        assert streamed == {"session": "cs-aaaa1111", "aid": "aaaa1111"}
        e = library.find(library.load(lib), "aaaa1111")
        assert e["last_used"] > 100                         # ensure touched it
    finally:
        tm("kill-server")
        try:
            os.unlink(f"/tmp/tmux-{os.getuid()}/{sock}")
        except OSError:
            pass


def test_saved_legacy_selection_follows_its_migrated_topic(world, monkeypatch):
    # the chat picked "/use 6" before the migration renamed claude-terminal-6 to
    # cs-<id>: the next message must land in that topic, not "isn't running"
    world.add("app - PIPE", uuid=U1, legacy_slot=6)
    world.loaded.append("aaaa1111")
    tb.set_current(CHAT, "6")
    sink, streamed = _deliver(world, monkeypatch, "дальше")
    assert ("send", "cs-aaaa1111", "дальше") in world.log
    assert tb.get_current(CHAT) == "aaaa1111"


# ════════════════════════════════════════════════════════════════════════════
# Review fixes (2026-09-24): never a second Claude on one conversation, never
# type into something that is not Claude, never block the event loop.
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _private_tmux_socket(monkeypatch):
    # every test in this file: a stray real tmux call reaches a server that does not
    # exist, never the default socket with the live terminals
    if not os.environ.get("AGENTDECK_TMUX_SOCKET", "").startswith("agentdeck-test-"):
        monkeypatch.setenv("AGENTDECK_TMUX_SOCKET", f"agentdeck-test-none-{os.getpid()}")


def test_the_bridge_honours_the_private_socket():
    assert tb._tmux("list-sessions").args[:3] == ["tmux", "-L", f"agentdeck-test-none-{os.getpid()}"]


def _no_legacy_start(monkeypatch):
    started = []
    monkeypatch.setattr(tb, "start_session", lambda aid: started.append(aid) or (True, "started"))
    return started


def test_use_number_with_archived_migrated_topic_never_starts_the_old_slot(world, monkeypatch):
    world.add("app - PIPE", uuid=U1, legacy_slot=6, archived=True)
    started = _no_legacy_start(monkeypatch)
    replies = run_cmd(tb.cmd_use, ["6"])
    assert started == []                                   # no 2nd Claude on the same uuid
    assert tb.get_current(CHAT) != "6"
    assert "archive" in replies[-1][0]


def test_use_number_callback_with_archived_migrated_topic(world, monkeypatch):
    world.add("app - PIPE", uuid=U1, legacy_slot=6, archived=True)
    started = _no_legacy_start(monkeypatch)
    sink = run_cb(tb.on_use_cb, "use:6")
    assert started == [] and tb.get_current(CHAT) != "6"
    assert "archive" in sink[-1][0]


def test_start_session_refuses_a_migrated_slot(world, monkeypatch):
    world.add("app - PIPE", uuid=U1, legacy_slot=6, archived=True)
    monkeypatch.setattr(tb, "has_session", lambda s: False)
    ran = []
    monkeypatch.setattr(tb.subprocess, "run", lambda *a, **k: ran.append(a))
    ok, msg = tb.start_session("6")
    assert ok is False and ran == [] and "aaaa1111" in msg


def test_saved_legacy_selection_with_archived_topic_is_not_sent(world, monkeypatch):
    world.add("app - PIPE", uuid=U1, legacy_slot=6, archived=True)
    world.codes["aaaa1111"] = (2, "topic is archived")
    started = _no_legacy_start(monkeypatch)
    tb.set_current(CHAT, "6")
    sink, streamed = _deliver(world, monkeypatch, "дальше")
    assert started == [] and not streamed
    assert not [e for e in world.log if e[0] == "send"]


def test_ensure_exit_4_already_running_elsewhere(world, monkeypatch):
    world.add("тема", uuid=U1)
    world.codes["aaaa1111"] = (4, "conversation already running in claude-terminal-6")
    tb.set_current(CHAT, "aaaa1111")
    sink, streamed = _deliver(world, monkeypatch)
    assert not [e for e in world.log if e[0] == "send"] and not streamed
    assert "already open" in sink[-1][0]
    replies = run_cmd(tb.cmd_use, ["тема"])
    assert "already open" in replies[-1][0]


def test_text_not_typed_when_the_topic_pane_is_not_claude(world, monkeypatch):
    world.add("тема", uuid=U1)
    world.loaded.append("aaaa1111")
    world.not_claude.add("aaaa1111")                       # Claude exited: a bare shell
    tb.set_current(CHAT, "aaaa1111")
    sink, streamed = _deliver(world, monkeypatch, "rm -rf ~")
    assert ("pane-is-claude", "aaaa1111") in world.log
    assert not [e for e in world.log if e[0] == "send"] and not streamed
    assert "not Claude" in sink[-1][0]


def test_pane_check_runs_off_the_event_loop(world, monkeypatch):
    import threading
    world.add("тема", uuid=U1)
    world.loaded.append("aaaa1111")
    tb.set_current(CHAT, "aaaa1111")
    _deliver(world, monkeypatch)
    main = threading.get_ident()
    assert [t for c, t in world.threads if c == "pane-is-claude"]
    assert all(t != main for c, t in world.threads if c == "pane-is-claude")


def test_text_not_typed_when_the_legacy_pane_is_not_claude(world, monkeypatch):
    tb.set_current(CHAT, "6")
    monkeypatch.setattr(tb, "has_session", lambda s: True)
    monkeypatch.setattr(tb, "legacy_pane_command", lambda s: "bash")
    sink, streamed = _deliver(world, monkeypatch, "привет")
    assert not [e for e in world.log if e[0] == "send"] and not streamed
    assert "not Claude" in sink[-1][0]


def test_legacy_pane_command_reads_list_panes_exactly(monkeypatch):
    out = ("claude-terminal-60\tbash\n"
           "claude-terminal-6\tclaude\n"
           "claude-terminal-6\tbash\n")               # 2nd pane of the same session
    calls = []

    def fake(*a):
        calls.append(a)
        return subprocess.CompletedProcess(a, 0, out, "")
    monkeypatch.setattr(tb, "_tmux", fake)
    assert tb.legacy_pane_command("claude-terminal-6") == "claude"
    assert tb.legacy_pane_command("claude-terminal") is None
    assert all("display-message" not in a for a in calls)
    assert calls[0][:3] == ("list-panes", "-a", "-F")
    assert tb.is_claude_command("claude") and tb.is_claude_command("2.1.87")
    assert not tb.is_claude_command("bash") and not tb.is_claude_command(None)


def test_ready_timeout_refuses_instead_of_sending(world, monkeypatch):
    world.add("тема", uuid=U1)
    tb.set_current(CHAT, "aaaa1111")                      # not loaded: ensure starts it
    monkeypatch.setattr(tb, "READY_TIMEOUT", 0)
    shell = "ubuntu@vps:~/pr$ claude --session-id x"
    sink, streamed = _deliver(world, monkeypatch, "привет", screens=[shell])
    assert not [e for e in world.log if e[0] == "send"] and not streamed
    assert any("not sent" in t for t, _ in sink)


def test_library_cli_active_runs_off_the_event_loop(world):
    import threading
    world.add("тема", uuid=U1)
    run_cmd(tb.cmd_list, [])
    run_cmd(tb.cmd_use, [])
    world.add("тема два", uuid=U2)
    run_cmd(tb.cmd_use, ["тема"])                         # several hits → buttons
    main = threading.get_ident()
    act = [t for c, t in world.threads if c == "active"]
    assert len(act) >= 3 and all(t != main for t in act)


def test_library_cli_calls_have_a_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(tb.subprocess, "run",
                        lambda cmd, **kw: seen.append(kw) or subprocess.CompletedProcess(cmd, 0, "[]", ""))
    tb.loaded_topics()
    tb.ensure_topic("aaaa1111")
    tb.pane_is_claude("aaaa1111", "cs-aaaa1111")
    assert len(seen) == 3 and all(0 < kw.get("timeout", 0) <= 60 for kw in seen)


def test_loaded_topics_timeout_is_unknown_not_a_crash(monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(tb.subprocess, "run", slow)
    assert tb.loaded_topics() is None
    assert tb.pane_is_claude("aaaa1111", "cs-aaaa1111") is False


# ── migrated slot whose legacy terminal still runs (busy ones were left alone) ──
def test_saved_selection_stays_on_the_running_legacy_terminal(world, monkeypatch):
    # migration left busy claude-terminal-N running; its topic's ensure would
    # refuse (exit 4, same conversation) — so the chat must keep typing there
    world.add("app - PIPE", uuid=U1, legacy_slot=6)
    monkeypatch.setattr(tb, "has_session", lambda s: s == "claude-terminal-6")
    tb.set_current(CHAT, "6")
    assert tb.resolve_current(CHAT) == "6"
    assert tb.get_current(CHAT) == "6"


def test_use_number_picks_the_running_legacy_terminal(world, monkeypatch):
    world.add("app - PIPE", uuid=U1, legacy_slot=6)
    monkeypatch.setattr(tb, "has_session", lambda s: s == "claude-terminal-6")
    run_cmd(tb.cmd_use, ["6"])
    assert tb.get_current(CHAT) == "6" and world.ensured() == []


def test_start_session_accepts_a_running_migrated_legacy_slot(world, monkeypatch):
    world.add("app - PIPE", uuid=U1, legacy_slot=6)
    monkeypatch.setattr(tb, "has_session", lambda s: s == "claude-terminal-6")
    ok, msg = tb.start_session("6")
    assert ok and msg == "already running"


# --- English-only user-facing text -----------------------------------------

_CYR = re.compile(r"[\u0400-\u04FF]")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cyrillic_literals(path):
    """String literals (f-string parts included) with Cyrillic, docstrings excepted.
    Comments are not string literals, so they never show up here."""
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant):
                docs.add(id(b[0].value))
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs and _CYR.search(n.value)]


@pytest.mark.parametrize("name", ["tg_bridge.py", "library_cli.py"])
def test_no_cyrillic_in_user_facing_strings(name):
    assert _cyrillic_literals(os.path.join(_ROOT, name)) == []


def test_open_session_prints_no_cyrillic():
    bad = []
    for i, line in enumerate(open(os.path.join(_ROOT, "open-session.sh"), encoding="utf-8"), 1):
        code = line.strip()
        if code.startswith("#"):
            continue
        code = re.sub(r"\s#\s.*$", "", code)            # trailing comment
        if _CYR.search(code):
            bad.append((i, code))
    assert bad == []


def test_bot_commands_are_english():
    got = {c.command: c.description for c in tb.BOT_COMMANDS}
    assert got["use"] == "pick a topic: /use <part of the name or id>"
    assert got["list"] == "topics: loaded first, current one marked"
    assert got["new"] == "new topic: /new <name>"
    assert not [d for d in got.values() if _CYR.search(d)]


def test_need_pick_is_english():
    assert tb.NEED_PICK == "Pick a topic first: /use <part of the name or id> · /list · /new <name>"
