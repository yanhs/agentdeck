"""The tmux server's command line carries no word a careless kill pattern hits.

2026-09-26: `pkill -f "noconftest 2>&1 | grep"`, run elsewhere on the server, is the
extended regex `noconftest 2>&1 ` OR ` grep`. It matched the tmux server that hosts every
terminal: tmux keeps, as the server's own command line, the command line of the client
that started it, and that was `tmux -f …/tmux.conf new-session -d -s cs-<id> -c <dir>
bash -lic 'for v in $(env | cut -d= -f1 | grep -i CLAUDE); … exec claude --resume …'`.
Every terminal died at once.

Now every `tmux new-session` AgentDeck runs (any one of them may be the call that starts
the server) is only tmux options, the session name and bin/agentdeck-pane with the topic
id (or `shell`). The pane's folder is the tmux client's working directory, not an
argument: a topic folder such as ~/claude-code-telegram would otherwise put "claude" back
on the server's line. In the pane, the launcher does what the long command did: the login
+ interactive bash environment, CLAUDE* unset, ~/.claude/oauth.env, AGENTDECK_SESSION,
exec claude with the arguments `library_cli.py ensure` prepared for this start.

Isolation as in test_library_cli: a private tmux socket per test, temp HOME and registry,
a fake claude, conftest's TMUX_TMPDIR. No process these tests did not start is signalled.
"""
import os
import re
import shutil
import subprocess
import time

import pytest

from tests.test_library_cli import BAD_IDS, U1, U2, Deck, _mod, wait_for

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "bin", "agentdeck-pane")
CONF = os.path.join(REPO, "tmux.conf")
OPEN_SESSION = os.path.join(REPO, "open-session.sh")

KILL_WORDS = ("grep", "claude", "bash", "env", "python", "node", "sleep", "pytest")
TOPIC_DIR = "claude-code-telegram bash env grep"     # a folder name full of those words
ULTRA_LINE = '{"type":"attachment","attachment":{"type":"ultra_effort_enter"}}\n'


def kill_words(argv):
    """The KILL_WORDS in argv, case-insensitive, as `pkill -f <word>` would find them.
    The checkout's own path is where the user cloned AgentDeck (~/agentdeck by
    default), not a word AgentDeck puts there, so it is left out."""
    text = " ".join(argv).replace(REPO, "<repo>").lower()
    return [w for w in KILL_WORDS if w in text]


def cmdline(pid):
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]


def environ(pid):
    with open(f"/proc/{pid}/environ", "rb") as f:
        pairs = (v.decode("utf-8", "replace").partition("=") for v in f.read().split(b"\0") if v)
    return {k: v for k, _, v in pairs}


def server_argv(tmux):
    """The tmux server's own command line (#{pid} from list-sessions; never
    display-message, which crashes tmux 3.2a)."""
    r = tmux("list-sessions", "-F", "#{pid}")
    assert r.returncode == 0, r.stderr
    return cmdline(int(r.stdout.split()[0]))


def pane(deck, name, fmt):
    r = deck.tmux("list-panes", "-a", "-F", "#{session_name}\t" + fmt)
    rows = [l.split("\t", 1)[1] for l in r.stdout.splitlines() if l.split("\t", 1)[0] == name]
    return rows[0] if rows else None


@pytest.fixture
def deck(tmp_path):
    d = Deck(tmp_path)
    yield d
    d.close()


# ── the server a topic starts ───────────────────────────────────────────────
def test_the_server_ensure_starts_has_no_kill_words_on_its_command_line(deck):
    work = deck.tmp / TOPIC_DIR
    work.mkdir()
    e = deck.add("A", uuid=U1, cwd=str(work))
    r = deck.cli("ensure", e["id"])
    assert r.returncode == 0, r.stderr
    argv = server_argv(deck.tmux)
    assert argv == ["tmux", "-L", deck.socket, "-f", CONF, "new-session", "-d",
                    "-s", "cs-aaaaaaaa", LAUNCHER, "aaaaaaaa"], argv
    assert kill_words(argv) == []
    assert not re.search(r"noconftest 2>&1 | grep", " ".join(argv))   # the incident's pattern
    # claude still runs in the topic's own folder, with its flags
    sid, args, cwd, leaked = wait_for(deck.calls)[0].split("|")
    assert (sid, cwd, leaked) == ("aaaaaaaa", str(work), "")
    assert args == f"--session-id {U1} --dangerously-skip-permissions"


def test_the_server_the_cmd_line_starts_has_no_kill_words_either(deck):
    work = deck.tmp / TOPIC_DIR
    work.mkdir()
    r = deck.cli("shell-ensure", AGENTDECK_WORKDIR=str(work), AGENTDECK_SESSION="deadbeef")
    assert r.returncode == 0, r.stderr
    argv = server_argv(deck.tmux)
    assert argv == ["tmux", "-L", deck.socket, "-f", CONF, "new-session", "-d",
                    "-s", "cmd-shell", LAUNCHER, "shell"], argv
    assert kill_words(argv) == []
    # still a login bash in AGENTDECK_WORKDIR, with no CLAUDE* and no topic id
    assert wait_for(lambda: pane(deck, "cmd-shell", "#{pane_current_command}") == "bash")
    assert pane(deck, "cmd-shell", "#{pane_current_path}") == str(work)
    pid = int(pane(deck, "cmd-shell", "#{pane_pid}"))
    assert cmdline(pid)[0].endswith("bash") and "-l" in cmdline(pid)
    env = environ(pid)
    assert not [k for k in env if "CLAUDE" in k.upper()]
    assert "AGENTDECK_SESSION" not in env


def test_open_session_starts_a_neutral_server_and_the_topic_runs(deck):
    e = deck.add("A", uuid=U1)
    tab = subprocess.Popen(["script", "-qfc", f"bash {OPEN_SESSION} {e['id']}", "/dev/null"],
                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=deck.env)
    deck.clients.append(tab)
    assert wait_for(lambda: deck.has("cs-aaaaaaaa"))
    argv = server_argv(deck.tmux)
    assert argv[-2:] == [LAUNCHER, "aaaaaaaa"] and kill_words(argv) == [], argv
    assert wait_for(lambda: deck.active().get("aaaaaaaa", {}).get("attached"))
    sid, args, _, _ = wait_for(deck.calls)[0].split("|")
    assert sid == "aaaaaaaa" and f"--session-id {U1}" in args


def test_a_legacy_slot_the_bridge_starts_leaves_a_neutral_server(tmp_path, monkeypatch):
    """tg_bridge.start_session (old numbered slots) makes `claude-terminal-N`: it is
    created under a neutral name and renamed, so a server it starts carries no
    `claude` either — and the slot still gets its typed launch command."""
    from tests.test_tg_bridge import tb
    home, gate, work = tmp_path / "home", tmp_path / "gate", tmp_path / TOPIC_DIR
    for d in (home, gate, work):
        d.mkdir()
    (gate / "launch-claude-5.sh").write_text(
        '#!/bin/bash\n[ "${DRY_RUN:-}" = 1 ] && echo "pwd > $HOME/legacy-started"\n')
    sock = f"agentdeck-test-legacy-{os.getpid()}"
    monkeypatch.setenv("AGENTDECK_TMUX_SOCKET", sock)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tb, "GATE_DIR", str(gate))
    monkeypatch.setattr(tb, "SESSIONS", {"5": "claude-terminal-5"})
    monkeypatch.setattr(tb, "migrated_topic", lambda aid: None)
    monkeypatch.setattr(tb, "AGENT_CWD", str(work))

    def tm(*a):
        return subprocess.run(["tmux", "-L", sock, *a], capture_output=True, text=True, timeout=10)
    try:
        ok, msg = tb.start_session("5")
        assert ok, msg
        argv = server_argv(tm)
        assert kill_words(argv) == [], argv
        names = tm("list-sessions", "-F", "#{session_name}").stdout.split()
        assert names == ["claude-terminal-5"]
        started = home / "legacy-started"
        assert wait_for(started.exists)
        assert wait_for(lambda: started.read_text().strip() == str(work))
        assert tb.start_session("5") == (True, "already running")
    finally:
        tm("kill-server")


# ── every new-session argv, whatever runs first ─────────────────────────────
def test_every_new_session_library_cli_runs_is_neutral(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m.library, "LIB_FILE", str(tmp_path / "reg" / "library.json"))
    folder = tmp_path / TOPIC_DIR
    folder.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(folder))
    calls = []

    def rec(*a, cwd=None):
        calls.append((a, cwd))
        return subprocess.CompletedProcess(a, 1 if a[0] == "has-session" else 0, "", "")
    monkeypatch.setattr(m, "_tmux", rec)
    m._start({"id": "aaaaaaaa", "uuid": U1, "cwd": str(folder)},
             ["/opt/claude", "--session-id", U1, "--dangerously-skip-permissions"])
    assert m.shell_ensure() == 0
    new = [(a, cwd) for a, cwd in calls if a[0] == "new-session"]
    assert [a for a, _ in new] == [
        ("new-session", "-d", "-s", "cs-aaaaaaaa", LAUNCHER, "aaaaaaaa"),
        ("new-session", "-d", "-s", "cmd-shell", LAUNCHER, "shell")]
    assert [cwd for _, cwd in new] == [str(folder), str(folder)]       # the folder: cwd
    for a, _ in new:
        assert kill_words(m.tmux_argv(*a)) == [], a
    assert not [a for a, _ in calls if a[0] == "send-keys"]            # nothing typed


def test_start_refuses_loudly_when_the_launcher_is_not_executable(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m.library, "LIB_FILE", str(tmp_path / "reg" / "library.json"))
    plain = tmp_path / "agentdeck-pane"
    plain.write_text("#!/bin/sh\n")
    monkeypatch.setattr(m, "LAUNCHER", str(plain))
    calls = []
    monkeypatch.setattr(m, "_tmux", lambda *a, cwd=None: calls.append(a))
    with pytest.raises(RuntimeError, match="not executable"):
        m._start({"id": "aaaaaaaa", "uuid": U1, "cwd": str(tmp_path)},
                 ["/opt/claude", "--session-id", U1, "--dangerously-skip-permissions"])
    assert calls == []


def test_the_launcher_ships_executable():
    assert os.access(LAUNCHER, os.X_OK)


# ── the pane: claude with the right flags and environment ───────────────────
def test_the_pane_runs_claude_with_its_flags_env_and_rc(deck):
    (deck.home / ".profile").write_text(
        "export AGENTDECK_RC_LOGIN=1\n"
        "case $- in *i*) export AGENTDECK_RC_INTERACTIVE=1 ;; esac\n"
        "export CLAUDE_FROM_RC=leak\n")
    (deck.home / ".claude").mkdir()
    (deck.home / ".claude" / "oauth.env").write_text(
        "export CLAUDE_CODE_OAUTH_TOKEN=tok-from-oauth-env\n")
    e = deck.add("ultra", uuid=U1)
    deck.transcript(e).write_text(ULTRA_LINE)                  # ended at ultracode
    assert deck.cli("ensure", e["id"]).returncode == 0
    sid, args, cwd, leaked = wait_for(deck.calls)[0].split("|")
    assert sid == "aaaaaaaa" and cwd == str(deck.work) and leaked == ""
    assert args == f'--resume {U1} --dangerously-skip-permissions --settings {{"ultracode":true}}'
    # tmux was handed the launcher; the pane's process is claude itself now (exec)
    start = pane(deck, "cs-aaaaaaaa", "#{pane_start_command}")
    assert LAUNCHER in start and start.rstrip("\"'").endswith("aaaaaaaa"), start
    pid = int(pane(deck, "cs-aaaaaaaa", "#{pane_pid}"))
    argv = cmdline(pid)
    assert argv[0] == "claude" and U1 in argv, argv
    env = environ(pid)
    assert env.get("AGENTDECK_SESSION") == "aaaaaaaa"
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-from-oauth-env"   # sourced after the scrub
    assert env.get("AGENTDECK_RC_LOGIN") == "1" and env.get("AGENTDECK_RC_INTERACTIVE") == "1"
    for gone in ("CLAUDE_FROM_RC", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_BIN",
                 "AGENTDECK_PANE_RC"):
        assert gone not in env, gone
    assert wait_for(lambda: deck.cli("pane-is-claude", "aaaaaaaa").returncode == 0)
    # the prepared start was read once and removed
    assert not os.path.exists(os.path.join(os.path.dirname(deck.lib), "launch", "aaaaaaaa"))


# ── the launcher on its own ─────────────────────────────────────────────────
def _launcher_env(tmp_path, **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".profile").write_text(f'echo rc >> "{home}/rc.log"\n')
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "LANG": "C.UTF-8", "AGENTDECK_LIBRARY": str(tmp_path / "reg" / "library.json")}
    env.update(extra)
    return home, env


def _run(argv, env):
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20,
                          stdin=subprocess.DEVNULL)


def _prepare(dirpath, sid, argv):
    d = dirpath / "launch"
    d.mkdir(parents=True, exist_ok=True)
    (d / sid).write_bytes(b"".join(a.encode() + b"\0" for a in argv))
    return d / sid


def _fake(tmp_path):
    f = tmp_path / "fake-claude-echo"
    f.write_text('#!/bin/sh\necho "ran $* as $AGENTDECK_SESSION"\n')
    f.chmod(0o755)
    return str(f)


@pytest.mark.parametrize("args", [[], ["aaaaaaaa", "aaaaaaaa"], ["shell", "x"], ["SHELL"],
                                  ["shell "], ["cmd-shell"]] + [[b] for b in BAD_IDS])
def test_launcher_refuses_anything_but_one_id_or_shell_before_running_anything(tmp_path, args):
    home, env = _launcher_env(tmp_path)
    r = _run([LAUNCHER, *args], env)
    assert r.returncode == 2, (args, r.stdout, r.stderr)
    assert "agentdeck-pane" in r.stderr
    assert not (home / "rc.log").exists()                      # no login shell ran


def test_launcher_without_a_prepared_start_runs_nothing(tmp_path):
    home, env = _launcher_env(tmp_path)
    r = _run([LAUNCHER, "aaaaaaaa"], env)
    assert r.returncode != 0
    assert "nothing prepared" in r.stderr and "ran " not in r.stdout


def test_launcher_refuses_a_start_prepared_for_another_topic(tmp_path):
    home, env = _launcher_env(tmp_path)
    f = _prepare(tmp_path / "reg", "aaaaaaaa",
                 [_fake(tmp_path), "--resume", U2, "--dangerously-skip-permissions"])
    r = _run([LAUNCHER, "aaaaaaaa"], env)
    assert r.returncode == 2 and "not for this topic" in r.stderr, r.stderr
    assert "ran " not in r.stdout and not f.exists()


def test_launcher_runs_the_prepared_start_once(tmp_path):
    home, env = _launcher_env(tmp_path, CLAUDECODE="1")
    f = _prepare(tmp_path / "reg", "aaaaaaaa",
                 [_fake(tmp_path), "--session-id", U1, "--dangerously-skip-permissions",
                  "--settings", '{"ultracode":true}'])
    r = _run([LAUNCHER, "aaaaaaaa"], env)
    assert r.returncode == 0, r.stderr
    assert (f"ran --session-id {U1} --dangerously-skip-permissions --settings "
            '{"ultracode":true} as aaaaaaaa') in r.stdout
    assert (home / "rc.log").read_text() == "rc\n"            # one login shell, not a loop
    assert not f.exists()
    assert _run([LAUNCHER, "aaaaaaaa"], env).returncode != 0   # used up


def test_launcher_finds_the_start_next_to_the_default_registry(tmp_path):
    # no AGENTDECK_LIBRARY: <repo>/.sessions/library.json, like library.LIB_FILE
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    shutil.copy2(LAUNCHER, repo / "bin" / "agentdeck-pane")
    f = _prepare(repo / ".sessions", "aaaaaaaa",
                 [_fake(tmp_path), "--session-id", U1, "--dangerously-skip-permissions"])
    home, env = _launcher_env(tmp_path)
    del env["AGENTDECK_LIBRARY"]
    r = _run([str(repo / "bin" / "agentdeck-pane"), "aaaaaaaa"], env)
    assert r.returncode == 0, r.stderr
    assert f"ran --session-id {U1} --dangerously-skip-permissions as aaaaaaaa" in r.stdout
    assert not f.exists()


def test_library_cli_prepares_the_start_where_the_launcher_reads_it(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m.library, "LIB_FILE", str(tmp_path / "reg" / "library.json"))
    argv = ["/opt/my claude", "--resume", U1, "--dangerously-skip-permissions",
            "--settings", '{"ultracode":true}']
    m.prepare_launch("aaaaaaaa", argv)
    f = tmp_path / "reg" / "launch" / "aaaaaaaa"
    assert f.read_bytes() == b"".join(a.encode() + b"\0" for a in argv)
    assert (f.stat().st_mode & 0o777) == 0o600
    assert m.launch_file("aaaaaaaa") == str(f)
    # the same argv the dry run prints, quoted
    e = {"id": "aaaaaaaa", "uuid": U1, "cwd": str(tmp_path)}
    assert m.pane_argv(e, home=str(tmp_path / "h"), claude_bin="/opt/claude") == [
        "/opt/claude", "--session-id", U1, "--dangerously-skip-permissions"]
    assert m.pane_command(e, home=str(tmp_path / "h"), claude_bin="/opt/claude").endswith(
        f"exec /opt/claude --session-id {U1} --dangerously-skip-permissions")


def test_ensure_leaves_no_prepared_start_when_tmux_fails(deck):
    """A start tmux refused must not stay behind for a later, unrelated launch."""
    e = deck.add("A", uuid=U1)
    r = deck.cli("ensure", e["id"], AGENTDECK_TMUX_SOCKET="bad/socket/name" * 20)
    assert r.returncode == 1, (r.stdout, r.stderr)
    time.sleep(0.2)
    assert not os.path.exists(os.path.join(os.path.dirname(deck.lib), "launch", "aaaaaaaa"))
