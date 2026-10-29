"""hooks/install_guards.py — turns the guard hooks on (or off) in a Claude Code settings file.

The Docker entrypoint runs it on every start so the agents in the container have the task
board guards by default (opt-out: AGENTDECK_GUARDS=0). The settings file lives in a
persisted volume the user may have edited, so the merge must be idempotent, keep every other
key and every hook the user added, and never overwrite a file it cannot parse.

Also installs a short default ~/.claude/CLAUDE.md telling agents how to use the board —
only when none exists.

The default CLAUDE.md is read by every agent on every turn, so it stays short: under
MAX_MD_LINES. The limit was 40 until 2.0.1, when the owner asked for the safety rules
(secrets, signals, asking first) to go in too, and 60 until 2.0.2, when the "test first"
rule joined them (the full file with every block is 62 lines); short is still the goal.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "hooks", "install_guards.py")
HOOKS = os.path.join(REPO, "hooks")
EXAMPLE = os.path.join(HOOKS, "settings.example.json")
MAX_MD_LINES = 70


def run(tmp_path, *args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(
        [sys.executable, INSTALL, "--settings", str(tmp_path / "settings.json"),
         "--hooks-dir", HOOKS, *args],
        capture_output=True, text=True, env=e, timeout=30)


def settings(tmp_path):
    return json.loads((tmp_path / "settings.json").read_text())


def commands(s, event=None):
    out = []
    for ev, groups in (s.get("hooks") or {}).items():
        if event and ev != event:
            continue
        for g in groups:
            for h in g.get("hooks", []):
                out.append(h.get("command", ""))
    return out


OURS = ("guard_task_board.py", "guard_dont_stop.py", "hold_on_timer.py",
        "guard_kill.py", "guard_email.py", "guard_secrets.py")
SAFETY_GUARDS = ("guard_kill.py", "guard_email.py", "guard_secrets.py")


def guard_commands(s):
    return [c for c in commands(s) if any(n in c for n in OURS)]


# ── install ──────────────────────────────────────────────────────────────────
def test_installs_into_a_missing_file_same_shape_as_the_example(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    s = settings(tmp_path)
    ex = json.load(open(EXAMPLE))
    # derived from settings.example.json: same events, same matchers, same hook count
    for ev, groups in ex["hooks"].items():
        assert [g.get("matcher") for g in s["hooks"][ev]] == [g.get("matcher") for g in groups]
    assert len(guard_commands(s)) == len(commands(ex))
    assert not any("/path/to/agentdeck" in c for c in commands(s))
    for c in guard_commands(s):
        script = c.split()[-1]
        assert os.path.isfile(script), c          # points at the real hook scripts
    assert any("guard_dont_stop.py" in c for c in commands(s, "Stop"))


def test_board_state_is_baked_into_the_hook_command(tmp_path):
    r = run(tmp_path, "--tracker-state", "/app/.sessions/tasks-state.json")
    assert r.returncode == 0, r.stderr
    for c in guard_commands(settings(tmp_path)):
        assert c.startswith("TRACKER_STATE=/app/.sessions/tasks-state.json "), c


def test_idempotent(tmp_path):
    run(tmp_path)
    first = (tmp_path / "settings.json").read_text()
    for _ in range(3):
        assert run(tmp_path).returncode == 0
    assert (tmp_path / "settings.json").read_text() == first


def test_keeps_other_keys_and_the_users_own_hooks(tmp_path):
    user = {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "python3 /mine/audit.py"}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
        },
    }
    (tmp_path / "settings.json").write_text(json.dumps(user))
    assert run(tmp_path).returncode == 0
    run(tmp_path)
    s = settings(tmp_path)
    assert s["model"] == "opus" and s["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert commands(s).count("python3 /mine/audit.py") == 1
    assert commands(s, "SessionStart") == ["echo hi"]
    assert guard_commands(s)
    # remove takes ours out and leaves theirs
    assert run(tmp_path, "--remove").returncode == 0
    s = settings(tmp_path)
    assert guard_commands(s) == []
    assert commands(s, "PreToolUse") == ["python3 /mine/audit.py"]
    assert commands(s, "SessionStart") == ["echo hi"]
    assert "Stop" not in s["hooks"]                 # emptied groups/events are dropped
    assert s["model"] == "opus"


def test_moving_the_state_path_replaces_not_duplicates(tmp_path):
    run(tmp_path, "--tracker-state", "/a/state.json")
    run(tmp_path, "--tracker-state", "/b/state.json")
    cs = guard_commands(settings(tmp_path))
    assert len(cs) == len(commands(json.load(open(EXAMPLE))))
    assert all(c.startswith("TRACKER_STATE=/b/state.json ") for c in cs)


def test_remove_on_a_missing_file_creates_nothing(tmp_path):
    assert run(tmp_path, "--remove").returncode == 0
    assert not (tmp_path / "settings.json").exists()


def test_unparseable_settings_are_left_alone(tmp_path):
    (tmp_path / "settings.json").write_text("{ not json")
    r = run(tmp_path)
    assert r.returncode == 0                       # never breaks the container start
    assert (tmp_path / "settings.json").read_text() == "{ not json"
    assert "not valid JSON" in r.stderr or "not valid JSON" in r.stdout


def test_check_reports_state(tmp_path):
    assert run(tmp_path, "--check").returncode == 1
    run(tmp_path)
    assert run(tmp_path, "--check").returncode == 0
    run(tmp_path, "--remove")
    assert run(tmp_path, "--check").returncode == 1


# ── default CLAUDE.md ────────────────────────────────────────────────────────
def test_claude_md_written_only_when_missing(tmp_path):
    md = tmp_path / "CLAUDE.md"
    r = run(tmp_path, "--claude-md", str(md), "--tracker", "/app/tasks-dashboard/tracker.py")
    assert r.returncode == 0, r.stderr
    text = md.read_text()
    assert "python3 /app/tasks-dashboard/tracker.py add-task" in text
    assert "add-item" in text and "set-task" in text and " set " in text
    assert "run_in_background" in text or "background" in text
    assert "ScheduleWakeup" in text or "timer" in text
    assert text.isascii() and len(text.splitlines()) < MAX_MD_LINES
    md.write_text("my own rules\n")
    run(tmp_path, "--claude-md", str(md))
    assert md.read_text() == "my own rules\n"


def test_a_new_claude_md_tells_the_agents_about_services(tmp_path):
    """2.0: a program that must outlive a terminal goes into the services folder, and the
    agent says so to the user."""
    md = tmp_path / "CLAUDE.md"
    r = run(tmp_path, "--claude-md", str(md), "--services-dir", "/work/.services")
    assert r.returncode == 0, r.stderr
    text = md.read_text()
    assert "/work/.services/<name>.sh" in text and "localhost:9876" in text
    assert "tell the user" in text
    assert text.isascii() and len(text.splitlines()) < MAX_MD_LINES
    assert "tracker.py add-task" in text                  # the board part is still there


def test_an_existing_claude_md_gets_the_services_rule_once(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("my own rules\n")
    for _ in range(2):
        run(tmp_path, "--claude-md", str(md), "--services-dir", "/home/u/projects/.services")
    text = md.read_text()
    assert text.startswith("my own rules\n")
    assert text.count("<!-- agentdeck-services -->") == 1
    assert "/home/u/projects/.services/<name>.sh" in text


def test_a_new_claude_md_says_where_the_agent_runs(tmp_path):
    """27.10.2026: asked "if I change the password and open the web, does the first terminal
    stop?", a sandbox agent answered "I can't tell how your platform works" and that it
    would not remember the conversation. The agents must know AgentKennel's facts."""
    md = tmp_path / "CLAUDE.md"
    r = run(tmp_path, "--claude-md", str(md))
    assert r.returncode == 0, r.stderr
    text = md.read_text()
    assert "<!-- agentdeck-environment -->" in text
    assert "same terminals" in text                       # web and Telegram: one terminal
    assert "password" in text and "stops nothing" in text
    assert "claude --resume" in text                      # restart / reopen: same conversation
    assert "new terminal" in text.lower() and "memory" in text
    assert text.isascii()


def test_a_new_claude_md_says_agentkennel(tmp_path):
    """The brand is AgentKennel (29.10.2026); the <!-- agentdeck-... --> marks stay."""
    md = tmp_path / "CLAUDE.md"
    r = run(tmp_path, "--claude-md", str(md), "--services-dir", "/work/.services")
    assert r.returncode == 0, r.stderr
    text = md.read_text()
    assert text.startswith("# Working rules (AgentKennel)\n")
    assert "an AgentKennel terminal" in text and "AgentDeck" not in text
    assert "<!-- agentdeck-environment -->" in text and "<!-- agentdeck-safety -->" in text


def test_a_claude_md_with_the_new_header_gets_the_blocks_once(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Working rules (AgentKennel)\n\n## Put multi-step work on the task board\n")
    for _ in range(2):
        run(tmp_path, "--claude-md", str(md))
    text = md.read_text()
    assert text.count("<!-- agentdeck-environment -->") == 1
    assert text.count("<!-- agentdeck-safety -->") == 1


def test_a_claude_md_agentdeck_wrote_earlier_gets_the_environment_note_once(tmp_path):
    """Sandboxes and servers set up before the note have our file without it (headed
    AgentDeck, the product's name before 29.10.2026).
    A person's own CLAUDE.md is still never touched (test_claude_md_written_only_when_missing)."""
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Working rules (AgentDeck)\n\n## Put multi-step work on the task board\n")
    for _ in range(2):
        run(tmp_path, "--claude-md", str(md))
    text = md.read_text()
    assert text.startswith("# Working rules (AgentDeck)\n")
    assert text.count("<!-- agentdeck-environment -->") == 1


SAFETY_MARK = "<!-- agentdeck-safety -->"


def test_a_new_claude_md_carries_the_safety_rules(tmp_path):
    """28.10.2026: an agent committed a server's IP and Telegram ids into a public repo.
    Every agent is told what never happens without the person."""
    md = tmp_path / "CLAUDE.md"
    r = run(tmp_path, "--claude-md", str(md), "--services-dir", "/work/.services")
    assert r.returncode == 0, r.stderr
    text = md.read_text()
    assert text.count(SAFETY_MARK) == 1
    safety = " ".join(text.split(SAFETY_MARK, 1)[1].split())     # lines wrap anywhere
    for words in ("never go into a commit", "git add -A", "made-up values",   # secrets
                  "kill the PID you saved", "pkill -f", "killall",            # signals
                  "Ask first", "force-push", "one action only",               # ask first
                  "environment variables",                                    # don't print
                  "reports its result",                                       # later jobs
                  "on a copy", "you checked the result"):                     # risky, done
        assert words in safety, words
    assert text.isascii() and len(text.splitlines()) < MAX_MD_LINES
    assert "tracker.py add-task" in text and "<!-- agentdeck-environment -->" in text


def test_a_claude_md_agentdeck_wrote_earlier_gets_the_safety_rules_once(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Working rules (AgentDeck)\n\n## Put multi-step work on the task board\n")
    for _ in range(2):
        run(tmp_path, "--claude-md", str(md), "--services-dir", "/work/.services")
    text = md.read_text()
    assert text.startswith("# Working rules (AgentDeck)\n")
    assert text.count(SAFETY_MARK) == 1
    assert text.count("<!-- agentdeck-environment -->") == 1


def test_a_persons_own_claude_md_gets_no_safety_rules(tmp_path):
    """Their rules are theirs: only the services rule is appended (with its own mark)."""
    md = tmp_path / "CLAUDE.md"
    md.write_text("my own rules\n")
    run(tmp_path, "--claude-md", str(md), "--services-dir", "/work/.services")
    assert SAFETY_MARK not in md.read_text()


TDD_MARK = "<!-- agentdeck-tdd -->"


def test_a_new_claude_md_tells_the_agents_to_test_first(tmp_path):
    """28.10.2026: the owner's agents write a test before the code because his own CLAUDE.md
    says "TDD always"; AgentKennel's agents were never told. Asked to put it in the release."""
    md = tmp_path / "CLAUDE.md"
    r = run(tmp_path, "--claude-md", str(md), "--services-dir", "/work/.services")
    assert r.returncode == 0, r.stderr
    text = md.read_text()
    assert text.count(TDD_MARK) == 1
    tdd = " ".join(text.split(TDD_MARK, 1)[1].split())
    for words in ("## Test first", "a test that fails for the right reason",
                  "the whole test suite", 'before you say "done"',
                  "reproduces the bug", "fix the change, not the test",
                  "Skip only for docs, config values and one-off commands"):
        assert words in tdd, words
    assert text.isascii() and len(text.splitlines()) < MAX_MD_LINES
    for mark in (SAFETY_MARK, "<!-- agentdeck-environment -->", "<!-- agentdeck-services -->"):
        assert text.count(mark) == 1, mark


def test_a_claude_md_agentdeck_wrote_earlier_gets_test_first_once(tmp_path):
    """Sandboxes and servers set up before 2.0.2, including ones that already got the
    safety block from 2.0.1."""
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Working rules (AgentDeck)\n\n## Put multi-step work on the task board\n"
                  "\n" + SAFETY_MARK + "\n## Safety: what never happens without the person\n")
    for _ in range(2):
        run(tmp_path, "--claude-md", str(md), "--services-dir", "/work/.services")
    text = md.read_text()
    assert text.startswith("# Working rules (AgentDeck)\n")
    assert text.count(TDD_MARK) == 1 and text.count(SAFETY_MARK) == 1


def test_a_persons_own_claude_md_gets_no_test_first_rule(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("my own rules\n")
    run(tmp_path, "--claude-md", str(md), "--services-dir", "/work/.services")
    assert TDD_MARK not in md.read_text()


def test_keeping_only_the_mark_line_opts_out_of_test_first(tmp_path):
    """To drop the rule, delete the block but keep its mark line: it is not added back."""
    md = tmp_path / "CLAUDE.md"
    run(tmp_path, "--claude-md", str(md))
    text = md.read_text()
    head, tail = text.split(TDD_MARK, 1)
    rest = tail.split("<!--", 1)
    md.write_text(head + TDD_MARK + "\n" + ("<!--" + rest[1] if len(rest) > 1 else ""))
    run(tmp_path, "--claude-md", str(md))
    assert "## Test first" not in md.read_text() and TDD_MARK in md.read_text()


def test_remove_never_touches_claude_md(tmp_path):
    md = tmp_path / "CLAUDE.md"
    run(tmp_path, "--claude-md", str(md))
    before = md.read_text()
    run(tmp_path, "--remove", "--claude-md", str(md))
    assert md.read_text() == before


# ── end to end: the installed command really guards ─────────────────────────
def test_installed_command_blocks_then_allows(tmp_path):
    state = tmp_path / "state.json"
    run(tmp_path, "--tracker-state", str(state))
    s = settings(tmp_path)
    edit_cmd = next(c for g in s["hooks"]["PreToolUse"] if "Edit" in (g.get("matcher") or "")
                    for c in [h["command"] for h in g["hooks"]])
    bash_cmd = next(c for g in s["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"
                    for c in [h["command"] for h in g["hooks"]] if "guard_task_board" in c)
    env = dict(os.environ, AGENTDECK_BOARD_MARKS=str(tmp_path / "m"))

    def sh(cmd, payload):
        return subprocess.run(["sh", "-c", cmd], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=30)

    ed = {"session_id": "e2e", "tool_name": "Edit",
          "tool_input": {"file_path": "/srv/x.py"}}
    r = sh(edit_cmd, ed)
    assert r.returncode == 2 and "BLOCKED" in r.stderr
    sh(bash_cmd, {"session_id": "e2e", "tool_name": "Bash",
                  "tool_input": {"command": "python3 tracker.py add-task t1 --title x"}})
    assert sh(edit_cmd, ed).returncode == 0


# ── hold_on_timer: a terminal waiting on its own timer stays loaded ─────────
def test_example_wires_hold_on_timer_after_the_timer_tools():
    ex = json.load(open(EXAMPLE))
    groups = ex["hooks"].get("PostToolUse") or []
    g = next(g for g in groups
             if any("hold_on_timer.py" in h["command"] for h in g["hooks"]))
    for tool in ("ScheduleWakeup", "CronCreate", "Monitor"):
        assert tool in g["matcher"].split("|"), g
    assert all("hold_on_timer.py" not in c
               for c in commands(ex, "PreToolUse") + commands(ex, "Stop"))


def test_install_remove_check_cover_hold_on_timer(tmp_path):
    assert run(tmp_path).returncode == 0
    s = settings(tmp_path)
    hold = [c for c in commands(s, "PostToolUse") if "hold_on_timer.py" in c]
    assert len(hold) == 1 and os.path.isfile(hold[0].split()[-1]), hold
    run(tmp_path)                                               # idempotent
    assert [c for c in commands(settings(tmp_path)) if "hold_on_timer.py" in c] == hold
    assert run(tmp_path, "--remove").returncode == 0
    assert not (tmp_path / "settings.json").exists() or \
        "hold_on_timer.py" not in (tmp_path / "settings.json").read_text()


def test_check_fails_when_only_part_of_the_set_is_installed(tmp_path):
    """An older install (guards only, no hold hook) must read as not installed, so
    --check and start.sh's hint notice the missing hook."""
    run(tmp_path)
    s = settings(tmp_path)
    s["hooks"].pop("PostToolUse")
    (tmp_path / "settings.json").write_text(json.dumps(s))
    assert run(tmp_path, "--check").returncode == 1
    assert run(tmp_path).returncode == 0                        # re-install completes it
    assert run(tmp_path, "--check").returncode == 0


def test_installed_hold_command_writes_the_marker_for_the_panes_session(tmp_path):
    """The installed command, run the way claude runs it inside a cs-<id> pane
    (AGENTDECK_SESSION exported by library_cli.pane_command), holds that session."""
    run(tmp_path, "--tracker-state", str(tmp_path / "state.json"))
    cmd = next(c for c in commands(settings(tmp_path), "PostToolUse") if "hold_on_timer" in c)
    reg = tmp_path / "reg" / "library.json"
    env = dict(os.environ, AGENTDECK_SESSION="abcdef12", AGENTDECK_LIBRARY=str(reg))
    r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, env=env, timeout=30,
                       input=json.dumps({"hook_event_name": "PostToolUse",
                                         "tool_name": "ScheduleWakeup",
                                         "tool_input": {"delaySeconds": 300}}))
    assert r.returncode == 0, r.stderr
    assert (reg.parent / "hold-abcdef12").is_file()


# ── 2.0.1: the safety guards ─────────────────────────────────────────────────
def test_example_wires_the_safety_guards_before_every_bash_call():
    """Words in CLAUDE.md get forgotten; these hold the same rules in code."""
    ex = json.load(open(EXAMPLE))
    bash = [c for g in ex["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"
            for c in [h["command"] for h in g["hooks"]]]
    for name in SAFETY_GUARDS:
        assert any(c.endswith("/hooks/" + name) for c in bash), name
    # Monitor runs tool_input.command in a shell too
    mon = [c for g in ex["hooks"]["PreToolUse"]
           if "Monitor" in (g.get("matcher") or "").split("|")
           for c in [h["command"] for h in g["hooks"]]]
    assert any(c.endswith("/hooks/guard_kill.py") for c in mon)


def test_install_remove_check_cover_the_safety_guards(tmp_path):
    assert run(tmp_path).returncode == 0
    cs = commands(settings(tmp_path), "PreToolUse")
    for name in SAFETY_GUARDS:
        hit = [c for c in cs if c.endswith("/" + name)]
        assert hit and all(os.path.isfile(c.split()[-1]) for c in hit), name
    assert run(tmp_path, "--check").returncode == 0
    s = settings(tmp_path)
    for g in s["hooks"]["PreToolUse"]:
        g["hooks"] = [h for h in g["hooks"] if "guard_secrets.py" not in h["command"]]
    (tmp_path / "settings.json").write_text(json.dumps(s))
    assert run(tmp_path, "--check").returncode == 1        # one missing: not installed
    run(tmp_path, "--remove")
    assert not (tmp_path / "settings.json").exists() or not any(
        n in (tmp_path / "settings.json").read_text() for n in SAFETY_GUARDS)


def _project(tmp_path, names, local=False):
    """A work folder whose own .claude/settings*.json already runs guards of these names."""
    work = tmp_path / "work"
    (work / ".claude").mkdir(parents=True, exist_ok=True)
    hooks = [{"type": "command", "command": f"python3 {work}/.claude/hooks/{n}"} for n in names]
    f = work / ".claude" / ("settings.local.json" if local else "settings.json")
    f.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks}]}}))
    return work


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read()
    except OSError:                                       # gone, or not ours to read
        return b""


@pytest.mark.parametrize("local", [False, True])
def test_a_guard_the_work_folder_already_runs_is_not_added_twice(tmp_path, local):
    """A server whose own project settings (the agents' work folder) already run a
    guard_kill.py / guard_email.py of their own: two copies of one guard would block the
    same command twice. Ours is left out; everything else goes in, and --check is happy."""
    work = _project(tmp_path, ["guard_kill.py", "guard_email.py"], local=local)
    r = run(tmp_path, "--services-dir", str(work / ".services"))
    assert r.returncode == 0, r.stderr
    cs = commands(settings(tmp_path))
    assert not any("guard_kill.py" in c or "guard_email.py" in c for c in cs)
    assert any(c.endswith("/guard_secrets.py") for c in cs)
    assert any("guard_task_board.py" in c for c in cs)
    assert "guard_kill.py" in r.stdout + r.stderr                  # says what it left out
    assert run(tmp_path, "--check", "--services-dir", str(work / ".services")).returncode == 0
    # the explicit --workdir wins over the one derived from --services-dir
    other = tmp_path / "elsewhere"
    run(tmp_path, "--services-dir", str(work / ".services"), "--workdir", str(other))
    assert any("guard_kill.py" in c for c in commands(settings(tmp_path)))


def test_without_a_work_folder_every_guard_goes_in(tmp_path):
    _project(tmp_path, ["guard_kill.py"])
    run(tmp_path)                                         # no --services-dir / --workdir
    assert any("guard_kill.py" in c for c in commands(settings(tmp_path)))


def test_installed_kill_guard_blocks_a_pattern_kill_that_hits_others(tmp_path):
    """End to end through the installed command: `pkill -f` whose pattern matches a process
    outside the caller's tree is blocked, and nothing is signalled (the guard only reads)."""
    import signal
    import time
    import uuid
    mark = "adck-victim-" + uuid.uuid4().hex[:8]
    # outside our tree: setsid -f forks twice and the parent exits, so it goes to init
    subprocess.run(["setsid", "-f", "bash", "-c", f"exec -a {mark} sleep 300"], check=True)
    pid = None
    for _ in range(100):
        pid = next((int(d) for d in os.listdir("/proc")
                    if d.isdigit() and _cmdline(d).startswith(mark.encode())), None)
        if pid:
            break
        time.sleep(0.05)
    assert pid, "the victim did not start"
    try:
        run(tmp_path)
        cmd = next(c for c in commands(settings(tmp_path), "PreToolUse")
                   if c.endswith("/guard_kill.py"))
        payload = {"tool_name": "Bash", "cwd": str(tmp_path),
                   "tool_input": {"command": f"pkill -f '{mark}|nothing'"}}
        r = subprocess.run(["sh", "-c", cmd], input=json.dumps(payload), capture_output=True,
                           text=True, timeout=30)
        assert r.returncode == 2 and "BLOCKED" in r.stderr, r.stderr
        assert os.path.exists(f"/proc/{pid}")              # still alive
    finally:
        os.kill(pid, signal.SIGKILL)                      # the PID we saved


LINKS_MARK = "<!-- agentdeck-links -->"


def test_a_new_claude_md_puts_links_on_the_last_line(tmp_path):
    """28.10.2026, the owner: the link goes on the last line, as he has told his own agents
    since 26.09 - Telegram and terminals glue the next character to the address, and the
    link stops opening. Asked to make it every agent's rule, in the release and sandboxes."""
    md = tmp_path / "CLAUDE.md"
    r = run(tmp_path, "--claude-md", str(md), "--services-dir", "/work/.services")
    assert r.returncode == 0, r.stderr
    text = md.read_text()
    assert text.count(LINKS_MARK) == 1
    links = " ".join(text.split(LINKS_MARK, 1)[1].split())
    for words in ("LAST line of your message", "nothing after it",
                  "no period, bracket, quote or more text", "one per line"):
        assert words in links, words
    assert text.isascii() and len(text.splitlines()) < MAX_MD_LINES
    for mark in (TDD_MARK, SAFETY_MARK, "<!-- agentdeck-environment -->",
                 "<!-- agentdeck-services -->"):
        assert text.count(mark) == 1, mark


def test_a_claude_md_agentdeck_wrote_earlier_gets_the_links_rule_once(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Working rules (AgentDeck)\n\n" + TDD_MARK + "\n## Test first\n")
    for _ in range(2):
        run(tmp_path, "--claude-md", str(md), "--services-dir", "/work/.services")
    text = md.read_text()
    assert text.count(LINKS_MARK) == 1 and text.count(TDD_MARK) == 1


def test_a_persons_own_claude_md_gets_no_links_rule(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("my own rules\n")
    run(tmp_path, "--claude-md", str(md), "--services-dir", "/work/.services")
    assert LINKS_MARK not in md.read_text()
