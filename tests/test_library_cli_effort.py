"""A terminal keeps its own session-only effort (ultracode, max) across a restart.

Claude Code never saves `/effort ultracode` or `/effort max` ("this session
only"), so a plain `claude --resume <uuid>` comes back at the saved default
(e.g. medium). library_cli.pane_command reads from the transcript the effort the
terminal ended at (newest evidence wins: a command's output, an ultra_effort_enter /
ultra_effort_exit attachment, the effort a reply ran at) and, on the --resume path
only, relaunches with:

    ultracode -> --settings '{"ultracode":true}'
    max       -> --effort max
    anything else (low/medium/high/xhigh/auto, or no choice) -> no flag

Only a real command record counts: a user-type, non-sidechain line whose
message.content is a STRING starting with <local-command-stdout>. The same words
inside a tool result, an assistant reply or a subagent's line are not a choice.
Transcripts reach 100+ MB, so the scan runs backwards in chunks and stops at the
most recent effort record. Any read problem -> no flag; the launch never fails.
"""
import json
import os
import shlex
import stat
import time

import pytest

from tests.test_library_cli import U1, U2, Deck, _mod, wait_for

CWD = "/home/ubuntu/pr"
ULTRA = "Set effort level to ultracode (this session only): xhigh + dynamic workflow orchestration"
MAX = "Set effort level to max (this session only): Maximum capability with deepest reasoning"
MEDIUM = "Set effort level to medium (saved as your default for new sessions): Balanced"
AUTO = "Effort level set to auto"
ULTRA_FLAG = "--settings '{\"ultracode\":true}'"


# ── transcript lines, shaped like Claude Code 2.1.283 writes them ───────────
def dumps(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)   # as JSON.stringify


def cmd_out(text, **over):
    """The record a slash command's output becomes."""
    rec = {"parentUuid": "p", "isSidechain": False, "promptId": "q", "type": "user",
           "message": {"role": "user",
                       "content": f"<local-command-stdout>{text}</local-command-stdout>"},
           "uuid": "u", "timestamp": "2026-09-26T10:00:00.000Z", "userType": "external",
           "entrypoint": "cli", "cwd": CWD, "sessionId": U1, "version": "2.1.283"}
    rec.update(over)
    return dumps(rec)


def tool_result(text):
    return dumps({"type": "user", "isSidechain": False, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": f"<local-command-stdout>{text}"
                                                                f"</local-command-stdout>"}]}})


def assistant(text):
    return dumps({"type": "assistant", "isSidechain": False, "message": {
        "role": "assistant", "content": [{"type": "text", "text": f"<local-command-stdout>{text}"}]}})


def assistant_string(text):
    return dumps({"type": "assistant", "isSidechain": False,
                       "message": {"role": "assistant", "content": f"<local-command-stdout>{text}"}})


def prompt(text):
    return dumps({"type": "user", "isSidechain": False, "message": {"role": "user", "content": text}})


FILLER = dumps({"type": "assistant", "isSidechain": False, "message": {
    "role": "assistant", "content": [{"type": "text", "text": "x" * 2000}]}})


def attachment(kind, sidechain=False, **fields):
    """What Claude Code writes on the next user turn after ultracode goes on or off,
    whatever turned it (/effort, /config, Alt+P, Remote Control)."""
    return dumps({"parentUuid": "p", "isSidechain": sidechain,
                  "attachment": {"type": kind, **fields}, "type": "attachment", "uuid": "a",
                  "timestamp": "2026-09-26T10:00:00.000Z",
                  "rendered": [{"content": "<system-reminder>\nUltracode …\n</system-reminder>"}],
                  "userType": "external", "entrypoint": "cli", "cwd": CWD, "sessionId": U1,
                  "version": "2.1.283"})


ENTER = attachment("ultra_effort_enter", reminderType="full")
SPARSE = attachment("ultra_effort_enter", reminderType="sparse")
EXIT = attachment("ultra_effort_exit")


def reply(effort, sidechain=False, **over):
    """A model reply: Claude Code stamps it with the effort the request ran at."""
    rec = {"parentUuid": "p", "isSidechain": sidechain, "message": {
        "model": "claude-opus-5-5", "role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        "requestId": "req_1", "type": "assistant", "uuid": "r",
        "timestamp": "2026-09-26T10:00:00.000Z", "effort": effort, "perTurnEffort": effort,
        "sessionId": U1, "version": "2.1.283"}
    rec.update(over)
    return dumps(rec)


# /config -> Model, or the Alt+P picker's change log: no effort in the text
SET_MODEL_CONFIG = "Set model to \x1b[1mOpus 5.5 (1M context)\x1b[22m"


def write_transcript(tmp_path, lines, home_name="h", tail="\n"):
    m = _mod()
    home = tmp_path / home_name
    t = home / ".claude" / "projects" / m.slug(CWD) / f"{U1}.jsonl"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text("\n".join(lines) + tail)
    return str(home), t


def launch(tmp_path, lines, **kw):
    """pane_command for a topic whose transcript holds `lines`."""
    m = _mod()
    home, _ = write_transcript(tmp_path, lines, **kw)
    e = {"id": "aaaaaaaa", "uuid": U1, "name": "x", "cwd": CWD}
    return m.pane_command(e, home=home, claude_bin="/opt/claude")


def has_no_effort_flag(cmd):
    return "--settings" not in cmd and "--effort" not in cmd


# ── restore ─────────────────────────────────────────────────────────────────
def test_ultracode_is_restored_on_resume(tmp_path):
    cmd = launch(tmp_path, [prompt("hi"), cmd_out(ULTRA), FILLER])
    assert cmd.endswith(f"exec /opt/claude --resume {U1} --dangerously-skip-permissions "
                        f"{ULTRA_FLAG}"), cmd


def test_max_is_restored_on_resume(tmp_path):
    cmd = launch(tmp_path, [cmd_out(MAX), FILLER])
    assert cmd.endswith(f"--resume {U1} --dangerously-skip-permissions --effort max"), cmd
    assert "--settings" not in cmd


def test_the_exact_ultracode_command_line(tmp_path):
    home, _ = write_transcript(tmp_path, [cmd_out(ULTRA)])
    m = _mod()
    cmd = m.pane_command({"id": "aaaaaaaa", "uuid": U1, "cwd": CWD}, home=home,
                         claude_bin="/opt/claude")
    oauth = f"{home}/.claude/oauth.env"
    assert cmd == ('for v in $(env | cut -d= -f1 | grep -i CLAUDE); do unset "$v"; done; '
                   f"[ -r {oauth} ] && . {oauth}; export AGENTDECK_SESSION=aaaaaaaa; "
                   f"exec /opt/claude --resume {U1} --dangerously-skip-permissions "
                   "--settings '{\"ultracode\":true}'")


def test_the_flag_survives_shell_parsing_as_two_arguments(tmp_path):
    cmd = launch(tmp_path, [cmd_out(ULTRA)])
    argv = shlex.split(cmd.rsplit("exec ", 1)[1])
    assert argv[-2:] == ["--settings", '{"ultracode":true}']
    assert json.loads(argv[-1]) == {"ultracode": True}


def test_the_latest_choice_wins(tmp_path):
    assert launch(tmp_path, [cmd_out(MAX), cmd_out(ULTRA)]).endswith(ULTRA_FLAG)
    assert launch(tmp_path, [cmd_out(ULTRA), cmd_out(MAX)], home_name="h2").endswith("--effort max")


@pytest.mark.parametrize("later", [
    MEDIUM,
    "Set effort level to high (saved as your default for new sessions): Comprehensive",
    "Set effort level to xhigh (this session only): Deeper reasoning",
    "Set effort level to low (saved as your default for new sessions): Quick",
    AUTO,
    "Effort level set to auto (this session only)",
    "Effort set to auto for this session, but CLAUDE_CODE_EFFORT_LEVEL=high still controls this session",
    "Cleared effort from settings, but CLAUDE_CODE_EFFORT_LEVEL=high still controls this session",
    # /effort max on a model capped below it: Claude sets the cap instead
    "Effort 'max' exceeds the cap for opus set by your settings or organization; set to 'xhigh' "
    "instead (this session only): Deeper reasoning",
    # the /model picker choosing a saved level
    "Set model to \x1b[1mOpus 5\x1b[22m and saved as your default for new sessions with "
    "\x1b[1mhigh\x1b[22m effort",
])
def test_a_later_other_level_means_no_flag(tmp_path, later):
    cmd = launch(tmp_path, [cmd_out(ULTRA), FILLER, cmd_out(later), FILLER])
    assert f"--resume {U1}" in cmd and has_no_effort_flag(cmd), cmd


def test_no_effort_choice_at_all_means_no_flag(tmp_path):
    cmd = launch(tmp_path, [prompt("hi"), FILLER,
                            cmd_out("Set model to `Opus 5.5` and saved as your default for new sessions")])
    assert f"--resume {U1}" in cmd and has_no_effort_flag(cmd)


def test_new_session_gets_no_flag(tmp_path):
    m = _mod()
    e = {"id": "aaaaaaaa", "uuid": U1, "name": "x", "cwd": CWD}
    cmd = m.pane_command(e, home=str(tmp_path / "empty-home"), claude_bin="/opt/claude")
    assert f"--session-id {U1}" in cmd and has_no_effort_flag(cmd)


# ── the /model picker ───────────────────────────────────────────────────────
@pytest.mark.parametrize("text,flag", [
    ("Set model to \x1b[1mOpus 5 (1M context)\x1b[22m for this session only with "
     "\x1b[1multracode\x1b[22m effort (ultracode applies to this session only)", ULTRA_FLAG),
    ("Set model to `Fable 5` and saved as your default for new sessions with `ultracode` effort "
     "(ultracode applies to this session only)", ULTRA_FLAG),
    ("Set model to \x1b[1mOpus 5\x1b[22m for this session only with \x1b[1mmax\x1b[22m effort",
     "--effort max"),
])
def test_the_model_picker_choice_is_restored(tmp_path, text, flag):
    assert launch(tmp_path, [cmd_out(MEDIUM), cmd_out(text)]).endswith(flag)


def test_an_env_override_still_records_the_sessions_choice(tmp_path):
    t = ("CLAUDE_CODE_EFFORT_LEVEL=high overrides effort this session — clear it and "
         "ultracode takes over")
    assert launch(tmp_path, [cmd_out(t)]).endswith(ULTRA_FLAG)
    t = ("Not applied: CLAUDE_CODE_EFFORT_LEVEL=high overrides effort this session, and max is "
         "session-only (nothing saved)")
    assert launch(tmp_path, [cmd_out(t)], home_name="h2").endswith("--effort max")


@pytest.mark.parametrize("noise", [
    "Current effort level: medium (Balanced)",                         # a status query
    "Effort level: auto (currently medium)",
    "Ultracode needs dynamic workflows enabled (see /config). Valid options are: low, medium",
    "Failed to set effort level: boom",
    "Invalid argument: turbo. Valid options are: low, medium, high",
])
def test_outputs_that_change_nothing_do_not_hide_the_choice(tmp_path, noise):
    assert launch(tmp_path, [cmd_out(ULTRA), cmd_out(noise)]).endswith(ULTRA_FLAG)


# ── changes made without /effort: /config, Alt+P, Remote Control ────────────
# They leave no effort text in the transcript, but every ultracode switch shows
# up as an ultra_effort_enter / ultra_effort_exit attachment on the next user
# turn, and every model reply carries the effort it ran at. The newest evidence wins.
@pytest.mark.parametrize("config_line", [[cmd_out(SET_MODEL_CONFIG)], []], ids=["config", "alt-p"])
def test_ultracode_turned_off_without_effort_is_not_restored(tmp_path, config_line):
    lines = [cmd_out(ULTRA), ENTER, reply("xhigh")] + config_line + [EXIT, reply("medium")]
    cmd = launch(tmp_path, lines)
    assert f"--resume {U1}" in cmd and has_no_effort_flag(cmd), cmd
    home, t = write_transcript(tmp_path, lines, home_name="h2")
    assert _mod().last_effort(str(t)) == "medium"


def test_a_bare_exit_attachment_means_no_flag(tmp_path):
    assert has_no_effort_flag(launch(tmp_path, [cmd_out(ULTRA), prompt("hi"), EXIT]))


@pytest.mark.parametrize("enter", [ENTER, SPARSE], ids=["full", "sparse"])
def test_ultracode_turned_on_without_a_command_is_restored(tmp_path, enter):
    assert launch(tmp_path, [prompt("hi"), enter, reply("xhigh"), FILLER]).endswith(ULTRA_FLAG)
    # the owner's b36f2fd1 case: a saved level earlier, ultracode switched on in /config
    assert launch(tmp_path, [cmd_out(MEDIUM), reply("medium"), enter, reply("xhigh")],
                  home_name="h2").endswith(ULTRA_FLAG)


def test_the_newest_of_command_and_attachment_wins(tmp_path):
    # /effort medium after ultracode had been on: the command is newer
    assert has_no_effort_flag(launch(tmp_path, [ENTER, reply("xhigh"), cmd_out(MEDIUM)]))
    # /effort ultracode and a restart before the next turn wrote its attachment
    assert launch(tmp_path, [ENTER, EXIT, reply("medium"), cmd_out(ULTRA)],
                  home_name="h2").endswith(ULTRA_FLAG)
    # ultracode on again after an exit
    assert launch(tmp_path, [ENTER, EXIT, reply("medium"), SPARSE, reply("xhigh")],
                  home_name="h3").endswith(ULTRA_FLAG)


def test_an_older_enter_does_not_outlive_a_later_exit(tmp_path):
    lines = [ENTER, reply("xhigh"), EXIT] + [FILLER] * 5
    assert has_no_effort_flag(launch(tmp_path, lines))


@pytest.mark.parametrize("fake", [
    attachment("ultra_effort_enter", sidechain=True, reminderType="full"),   # a subagent's
    tool_result('{"type":"attachment","attachment":{"type":"ultra_effort_enter"}}'),   # a grep
    assistant('"attachment":{"type":"ultra_effort_enter","reminderType":"full"}'),
    dumps({"type": "user", "isSidechain": False, "attachment": {"type": "ultra_effort_enter"}}),
    dumps({"type": "attachment", "isSidechain": False, "attachment": "ultra_effort_enter"}),
])
def test_an_enter_that_is_not_a_real_attachment_is_ignored(tmp_path, fake):
    assert has_no_effort_flag(launch(tmp_path, [cmd_out(MEDIUM), fake, FILLER]))


def test_an_exit_that_is_not_a_real_attachment_is_ignored(tmp_path):
    fakes = [attachment("ultra_effort_exit", sidechain=True),
             tool_result('{"type":"attachment","attachment":{"type":"ultra_effort_exit"}}')]
    assert launch(tmp_path, [cmd_out(ULTRA), ENTER] + fakes).endswith(ULTRA_FLAG)


# max leaves no attachment; the replies after it show whether it still held
def test_max_changed_in_config_is_not_restored(tmp_path):
    lines = [cmd_out(MAX), reply("max"), cmd_out(SET_MODEL_CONFIG), reply("medium")]
    assert has_no_effort_flag(launch(tmp_path, lines))
    assert has_no_effort_flag(launch(tmp_path, [cmd_out(MAX), reply("max"), reply("high")],
                                     home_name="h2"))                  # Alt+P: no line at all


def test_max_that_still_held_is_restored(tmp_path):
    assert launch(tmp_path, [cmd_out(MAX), reply("max"), FILLER]).endswith("--effort max")
    # restarted before any reply: the command is the newest word
    assert launch(tmp_path, [reply("medium"), cmd_out(MAX)], home_name="h2").endswith("--effort max")


def test_max_set_without_a_command_is_restored(tmp_path):
    assert launch(tmp_path, [cmd_out(MEDIUM), reply("medium"), reply("max")]).endswith("--effort max")


def test_max_after_ultracode_survives_the_exit_it_causes(tmp_path):
    lines = [cmd_out(ULTRA), ENTER, reply("xhigh"), cmd_out(MAX), prompt("go"), EXIT, reply("max")]
    cmd = launch(tmp_path, lines)
    assert cmd.endswith("--effort max") and "--settings" not in cmd, cmd
    lines = [cmd_out(ULTRA), ENTER, reply("xhigh"), cmd_out(MAX), EXIT]      # no reply yet
    assert launch(tmp_path, lines, home_name="h2").endswith("--effort max")


@pytest.mark.parametrize("other", [
    reply("medium", sidechain=True),                                   # a subagent's reply
    reply(None),                                                       # no effort stamped
    dumps({"type": "assistant", "isSidechain": False, "message": {"model": "<synthetic>",
          "role": "assistant", "content": [{"type": "text", "text": "API Error"}]},
          "isApiErrorMessage": True, "perTurnEffort": None}),          # a synthetic error line
    tool_result('{"type":"assistant","effort":"medium"}'),
    assistant('{"type":"assistant","effort":"medium"}'),
])
def test_only_a_real_reply_can_overrule_max(tmp_path, other):
    assert launch(tmp_path, [cmd_out(MAX), reply("max"), other]).endswith("--effort max")


def test_a_reply_written_with_spaced_json_still_counts(tmp_path):
    spaced = json.dumps(json.loads(reply("medium")))                   # '"effort": "medium"'
    assert has_no_effort_flag(launch(tmp_path, [cmd_out(MAX), reply("max"), spaced]))


def test_ultracode_replies_run_at_xhigh_and_do_not_hide_it(tmp_path):
    assert launch(tmp_path, [cmd_out(ULTRA), prompt("hi"), reply("xhigh")] * 3).endswith(ULTRA_FLAG)


def test_the_scan_still_stops_early_with_attachments(tmp_path, monkeypatch):
    earlier = [FILLER] * 4000                                          # ~8 MB before
    home, t = write_transcript(tmp_path, earlier + [SPARSE, reply("xhigh"), FILLER])
    m = _mod()
    read = []
    real = m._read_at
    monkeypatch.setattr(m, "_read_at", lambda f, pos, n: read.append(n) or real(f, pos, n))
    assert m.last_effort(str(t)) == "ultracode"
    assert sum(read) <= 2 * m.EFFORT_CHUNK, sum(read)
    read.clear()
    home, t = write_transcript(tmp_path, earlier + [cmd_out(ULTRA), EXIT, reply("medium")],
                               home_name="h2")
    assert m.last_effort(str(t)) == "medium"
    assert sum(read) <= 2 * m.EFFORT_CHUNK, sum(read)


# ── only a real command record counts ───────────────────────────────────────
@pytest.mark.parametrize("fake", [
    tool_result(ULTRA),                                                # a grep's output
    assistant(ULTRA),                                                  # Claude quoting it
    assistant_string(ULTRA),
    cmd_out(ULTRA, isSidechain=True),                                  # a subagent's line
    cmd_out(ULTRA, type="assistant"),
    prompt(f"why does it say {ULTRA}?"),                               # typed by a person
    prompt(f" <local-command-stdout>{ULTRA}</local-command-stdout>"),  # not at the start
    dumps(f"<local-command-stdout>{ULTRA}</local-command-stdout>"),   # not an object
    dumps([{"type": "user", "message": {"content": f"<local-command-stdout>{ULTRA}"}}]),
    dumps({"type": "user", "message": f"<local-command-stdout>{ULTRA}"}),
])
def test_the_phrase_outside_a_command_record_is_not_a_choice(tmp_path, fake):
    cmd = launch(tmp_path, [cmd_out(MEDIUM), fake, FILLER])
    assert has_no_effort_flag(cmd), cmd


def test_a_fake_after_the_real_choice_does_not_override_it(tmp_path):
    cmd = launch(tmp_path, [cmd_out(ULTRA), tool_result(MEDIUM), assistant(MEDIUM),
                            cmd_out(MEDIUM, isSidechain=True)])
    assert cmd.endswith(ULTRA_FLAG)


def test_malformed_lines_are_skipped(tmp_path):
    lines = [cmd_out(ULTRA), "{not json", '{"type":"user","message":{"content":"<local-command-'
             'stdout>Set effort level to medium', "\x00\xff garbage", FILLER[:500]]
    cmd = launch(tmp_path, lines, tail="")                             # torn last line, no \n
    assert cmd.endswith(ULTRA_FLAG), cmd


def test_invalid_utf8_is_skipped(tmp_path):
    home, t = write_transcript(tmp_path, [cmd_out(ULTRA)])
    with open(t, "ab") as f:
        f.write(b'{"type":"user","message":{"content":"<local-command-stdout>Set effort level'
                b' to medium \xff\xfe"}}\n')
    m = _mod()
    assert m.last_effort(str(t)) == "ultracode"


# ── big transcripts ─────────────────────────────────────────────────────────
def test_found_behind_megabytes_of_later_output(tmp_path):
    later = [FILLER] * 3000                                            # ~6 MB after the choice
    home, t = write_transcript(tmp_path, [prompt("start"), cmd_out(ULTRA)] + later)
    assert os.path.getsize(t) > 5_000_000
    m = _mod()
    e = {"id": "aaaaaaaa", "uuid": U1, "cwd": CWD}
    assert m.pane_command(e, home=home, claude_bin="/opt/claude").endswith(ULTRA_FLAG)


def test_the_scan_stops_at_the_latest_choice(tmp_path, monkeypatch):
    earlier = [FILLER] * 4000                                          # ~8 MB before the choice
    home, t = write_transcript(tmp_path, earlier + [cmd_out(ULTRA), FILLER, FILLER])
    size = os.path.getsize(t)
    m = _mod()
    read = []
    real = m._read_at
    monkeypatch.setattr(m, "_read_at", lambda f, pos, n: read.append(n) or real(f, pos, n))
    assert m.last_effort(str(t)) == "ultracode"
    assert sum(read) <= 2 * m.EFFORT_CHUNK < size / 10, (sum(read), size)


def test_a_line_longer_than_the_limit_is_skipped_not_carried(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "EFFORT_MAX_LINE", 10_000)
    huge = assistant("y" * 50_000 + ULTRA)                             # one giant line, no newline inside
    home, t = write_transcript(tmp_path, [cmd_out(MAX), huge, FILLER])
    assert m.last_effort(str(t), chunk=4096) == "max"
    home, t = write_transcript(tmp_path, [cmd_out(MAX), huge], home_name="h2", tail="")
    assert m.last_effort(str(t), chunk=4096) == "max"                 # giant line at EOF
    home, t = write_transcript(tmp_path, [huge, cmd_out(MAX)], home_name="h3")
    assert m.last_effort(str(t), chunk=4096) == "max"                 # giant line at the start


@pytest.mark.parametrize("chunk", [1, 2, 7, 64, 577, 4096, 1 << 20])
def test_any_chunk_size_reads_lines_across_chunk_edges(tmp_path, chunk):
    m = _mod()
    lines = [prompt("a"), cmd_out(MAX), "", cmd_out(ULTRA), prompt("b"), tool_result(MEDIUM)]
    home, t = write_transcript(tmp_path, lines)
    assert m.last_effort(str(t), chunk=chunk) == "ultracode"
    home, t = write_transcript(tmp_path, [cmd_out(ULTRA)], home_name="h2", tail="")
    assert m.last_effort(str(t), chunk=chunk) == "ultracode"           # the only line, no \n


# ── a problem reading means no flag, never a failed launch ──────────────────
def test_empty_transcript_means_no_flag(tmp_path):
    m = _mod()
    home, t = write_transcript(tmp_path, [], tail="")
    assert m.last_effort(str(t)) is None
    assert has_no_effort_flag(m.pane_command({"id": "aaaaaaaa", "uuid": U1, "cwd": CWD},
                                             home=home, claude_bin="/opt/claude"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000 file anyway")
def test_unreadable_transcript_means_no_flag(tmp_path):
    m = _mod()
    home, t = write_transcript(tmp_path, [cmd_out(ULTRA)])
    os.chmod(t, 0)
    try:
        assert m.last_effort(str(t)) is None
        cmd = m.pane_command({"id": "aaaaaaaa", "uuid": U1, "cwd": CWD}, home=home,
                             claude_bin="/opt/claude")
        assert f"--resume {U1}" in cmd and has_no_effort_flag(cmd)
    finally:
        os.chmod(t, stat.S_IRUSR | stat.S_IWUSR)


def test_missing_file_or_directory_or_fifo_means_no_flag_and_no_hang(tmp_path):
    m = _mod()
    assert m.last_effort(str(tmp_path / "nope.jsonl")) is None
    assert m.last_effort(str(tmp_path)) is None
    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)
    t0 = time.time()
    assert m.last_effort(str(fifo)) is None                            # no writer: must not block
    assert time.time() - t0 < 2


def test_an_unexpected_error_means_no_flag(tmp_path, monkeypatch):
    m = _mod()
    home, _ = write_transcript(tmp_path, [cmd_out(ULTRA)])

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(m, "_read_at", boom)
    cmd = m.pane_command({"id": "aaaaaaaa", "uuid": U1, "cwd": CWD}, home=home,
                         claude_bin="/opt/claude")
    assert f"--resume {U1}" in cmd and has_no_effort_flag(cmd)


# ── end to end: the pane really starts claude with the flag ─────────────────
@pytest.fixture
def deck(tmp_path):
    d = Deck(tmp_path)
    yield d
    d.close()


def _deck_transcript(deck, e, lines):
    slug = "".join(c if c.isalnum() and c.isascii() else "-" for c in e["cwd"])
    p = deck.home / ".claude" / "projects" / slug / f"{e['uuid']}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")


def test_ensure_starts_claude_with_ultracode_restored(deck):
    e = deck.add("ultra", uuid=U1)
    _deck_transcript(deck, e, [cmd_out(ULTRA), FILLER])
    r = deck.cli("ensure", e["id"])
    assert r.returncode == 0, r.stderr
    calls = wait_for(deck.calls)
    args = calls[0].split("|")[1]                                      # the fake claude's "$*"
    assert args == f'--resume {U1} --dangerously-skip-permissions --settings {{"ultracode":true}}'


def test_ensure_resumes_without_a_flag_after_a_saved_level(deck):
    e = deck.add("medium", uuid=U2)
    _deck_transcript(deck, e, [cmd_out(ULTRA), cmd_out(MEDIUM)])
    assert deck.cli("ensure", e["id"]).returncode == 0
    calls = wait_for(deck.calls)
    assert calls[0].split("|")[1] == f"--resume {U2} --dangerously-skip-permissions"


def test_pane_cmd_subcommand_shows_the_restored_flag(deck):
    e = deck.add("max", uuid=U1)
    _deck_transcript(deck, e, [cmd_out(MAX)])
    r = deck.cli("pane-cmd", e["id"])
    assert r.returncode == 0 and r.stdout.strip().endswith("--effort max"), r.stdout
    assert not deck.server_up()
