"""The tmux server's command line carries no word a careless kill pattern hits.

2026-09-26: `pkill -f "noconftest 2>&1 | grep"`, run elsewhere on the server, is the
extended regex `noconftest 2>&1 ` OR ` grep`. It matched the tmux server that hosts every
terminal: tmux keeps, as the server's own command line, the command line of the client
that started it, and that was `tmux -f …/tmux.conf new-session -d -s cs-<id> -c <dir>
bash -lic 'for v in $(env | cut -d= -f1 | grep -i CLAUDE); … exec claude --resume …'`.
Every terminal died at once.

Now, when no server runs, AgentDeck starts one first with a line that says nothing —
`tmux [-L sock] -f /dev/null new-session -d -s hold-<hex> tmux wait-for hold-<hex>`: no
checkout path either, so neither `pkill -f agentdeck` (the default ~/agentdeck, or
another install) nor `pkill -f <checkout>` matches — sources tmux.conf into it, makes the
real session and lets the placeholder go. tmux is called from `/`: the server keeps the
working directory of the call that starts it, and a topic folder there would make
`fuser -k <folder>` kill every terminal. The real new-session runs on a server that is
already up, so it may carry `-c <folder>` (tmux's new windows open there) and
bin/pane <id|shell> — a launcher whose name carries no kill word either. Should the
placeholder fail, the new-session goes without -c and the launcher enters the folder
from the prepared start; either way it does what the long command did: the login +
interactive bash environment, CLAUDE* unset, ~/.claude/oauth.env, AGENTDECK_SESSION,
exec claude with the arguments `library_cli.py ensure` prepared.

The prepared start is found without the environment on either side: it lives in
<the launcher's checkout>/.sessions/launch/<id>. The pane's environment is the tmux
server's (whoever started it), so AGENTDECK_LIBRARY there says nothing about ensure's.

Isolation as in test_library_cli: a private tmux socket per test, temp HOME and registry,
a copy of the launcher in a temp checkout, a fake claude, conftest's TMUX_TMPDIR. No
process these tests did not start is signalled.
"""
import os
import re
import shutil
import stat
import subprocess
import time

import pytest

from tests.test_library_cli import BAD_IDS, REPO_LAUNCHER, U1, U2, Deck, _mod, wait_for

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.path.join(REPO, "tmux.conf")
OPEN_SESSION = os.path.join(REPO, "open-session.sh")

KILL_WORDS = ("grep", "claude", "bash", "env", "python", "node", "sleep", "pytest",
              "agentdeck")
TOPIC_DIR = "claude-code-telegram bash env grep agentdeck"    # a folder name full of them
ULTRA_LINE = '{"type":"attachment","attachment":{"type":"ultra_effort_enter"}}\n'


def kill_words(argv, *checkouts):
    """The KILL_WORDS AgentDeck put on argv, case-insensitive, as `pkill -f <word>` would
    find them. Left out: where AgentDeck was cloned (this checkout, the run's launcher
    copy — conftest — and a test's own copies: the user picks that path, ~/agentdeck by
    default) and a test's own `-L <socket>`."""
    a = list(argv)
    if len(a) > 2 and a[1] == "-L":
        del a[1:3]
    text = " ".join(a)
    run_co = os.path.dirname(os.path.dirname(os.environ.get("AGENTDECK_LAUNCHER", "")))
    for c in sorted({REPO, run_co, *map(str, checkouts)} - {""}, key=len, reverse=True):
        text = text.replace(c, "<repo>")
    return [w for w in KILL_WORDS if w in text.lower()]


HOLD = re.compile(r"hold-[0-9a-f]{8}")


def neutral_server(argv, sock=None):
    """True for the line hold_server starts a server with: tmux options, the placeholder,
    `tmux wait-for` — no path but /dev/null, no kill word."""
    head = ["tmux", *(["-L", sock] if sock else []), "-f", "/dev/null",
            "new-session", "-d", "-s"]
    n = len(head)
    return (argv[:n] == head and len(argv) == n + 4 and bool(HOLD.fullmatch(argv[n]))
            and argv[n + 1:] == ["tmux", "wait-for", argv[n]]
            and kill_words(argv) == [] and [a for a in argv if "/" in a] == ["/dev/null"])


def sessions(tmux):
    return sorted(tmux("list-sessions", "-F", "#{session_name}").stdout.split())


def cmdline(pid):
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]


def environ(pid):
    with open(f"/proc/{pid}/environ", "rb") as f:
        pairs = (v.decode("utf-8", "replace").partition("=") for v in f.read().split(b"\0") if v)
    return {k: v for k, _, v in pairs}


def server_pid(tmux):
    """#{pid} from list-sessions — never display-message, which crashes tmux 3.2a."""
    r = tmux("list-sessions", "-F", "#{pid}")
    assert r.returncode == 0, r.stderr
    return int(r.stdout.split()[0])


def server_argv(tmux):
    """The tmux server's own command line."""
    return cmdline(server_pid(tmux))


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
    r = deck.cli("ensure", e["id"], _cwd=str(work))       # called from inside the folder
    assert r.returncode == 0, r.stderr
    argv = server_argv(deck.tmux)
    assert neutral_server(argv, deck.socket), argv
    assert not re.search(r"noconftest 2>&1 | grep", " ".join(argv))   # the incident's pattern
    assert sessions(deck.tmux) == ["cs-aaaaaaaa"]      # the placeholder is gone already
    # tmux.conf was in before the first real pane: its scrollback is AgentDeck's
    assert pane(deck, "cs-aaaaaaaa", "#{history_limit}") == "50000"
    # claude still runs in the topic's own folder, with its flags
    sid, args, cwd, leaked = wait_for(deck.calls)[0].split("|")
    assert (sid, cwd, leaked) == ("aaaaaaaa", str(work), "")
    assert args == f"--session-id {U1} --dangerously-skip-permissions"


def test_the_server_the_cmd_line_starts_has_no_kill_words_either(deck):
    work = deck.tmp / TOPIC_DIR
    work.mkdir()
    r = deck.cli("shell-ensure", AGENTDECK_WORKDIR=str(work), AGENTDECK_SESSION="deadbeef",
                 _cwd=str(work))
    assert r.returncode == 0, r.stderr
    argv = server_argv(deck.tmux)
    assert neutral_server(argv, deck.socket), argv
    assert sessions(deck.tmux) == ["cmd-shell"]        # the placeholder is gone already
    # still a login bash in AGENTDECK_WORKDIR, with no CLAUDE* and no topic id
    assert wait_for(lambda: pane(deck, "cmd-shell", "#{pane_current_command}") == "bash")
    assert wait_for(lambda: pane(deck, "cmd-shell", "#{pane_current_path}") == str(work))
    pid = int(pane(deck, "cmd-shell", "#{pane_pid}"))
    assert cmdline(pid)[0].endswith("bash") and "-l" in cmdline(pid)
    env = environ(pid)
    assert not [k for k in env if "CLAUDE" in k.upper()]
    assert "AGENTDECK_SESSION" not in env
    assert not (deck.launch_dir / "shell").exists()          # read once


def test_open_session_starts_a_neutral_server_and_the_topic_runs(deck):
    e = deck.add("A", uuid=U1)
    tab = subprocess.Popen(["script", "-qfc", f"bash {OPEN_SESSION} {e['id']}", "/dev/null"],
                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=deck.env)
    deck.clients.append(tab)
    assert wait_for(lambda: deck.has("cs-aaaaaaaa"))
    argv = server_argv(deck.tmux)
    assert neutral_server(argv, deck.socket), argv
    assert wait_for(lambda: deck.active().get("aaaaaaaa", {}).get("attached"))
    sid, args, _, _ = wait_for(deck.calls)[0].split("|")
    assert sid == "aaaaaaaa" and f"--session-id {U1}" in args


def test_a_legacy_slot_the_bridge_starts_leaves_a_neutral_server(tmp_path, monkeypatch):
    """tg_bridge.start_session (old numbered slots) makes `claude-terminal-N`: it is
    created under a neutral name (no "claude", no "agentdeck") and renamed, from `/`, so
    a server it starts carries none of those words nor its folder as its working
    directory — and the slot still gets its typed launch command, in its folder."""
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
        assert neutral_server(argv, sock), argv
        assert os.readlink(f"/proc/{server_pid(tm)}/cwd") == "/"
        assert wait_for(lambda: sessions(tm) == ["claude-terminal-5"])
        started = home / "legacy-started"
        assert wait_for(started.exists)
        assert wait_for(lambda: started.read_text().strip() == str(work))
        assert tb.start_session("5") == (True, "already running")
    finally:
        tm("kill-server")


# ── the server's working directory: never a topic's folder ─────────────────
@pytest.mark.parametrize("what", ["ensure", "shell-ensure"])
def test_the_server_does_not_keep_a_topic_folder_as_its_working_directory(deck, what):
    """`fuser -k <folder>` or `lsof +D <folder> | xargs kill` ("free this folder") must not
    hit the server: tmux is called from /, whoever calls ensure from where."""
    work = deck.tmp / "some-project"
    work.mkdir()
    e = deck.add("A", uuid=U1, cwd=str(work))
    args = ("ensure", e["id"]) if what == "ensure" else ("shell-ensure",)
    r = deck.cli(*args, AGENTDECK_WORKDIR=str(work), _cwd=str(work))
    assert r.returncode == 0, r.stderr
    assert os.readlink(f"/proc/{server_pid(deck.tmux)}/cwd") == "/"
    name = "cs-aaaaaaaa" if what == "ensure" else "cmd-shell"
    assert wait_for(lambda: pane(deck, name, "#{pane_current_path}") == str(work))


def test_tmux_new_windows_in_a_terminal_open_in_its_folder(deck):
    """The real new-session runs on a server that is already up (a placeholder brought it
    up if none ran), so it names the folder with -c — its line is not the server's —
    and tmux's new windows in the terminal open there, as before."""
    work = deck.tmp / TOPIC_DIR
    work.mkdir()
    e = deck.add("A", uuid=U1, cwd=str(work))
    assert deck.cli("ensure", e["id"]).returncode == 0            # starts the server
    assert deck.cli("shell-ensure", AGENTDECK_WORKDIR=str(deck.work)).returncode == 0
    r = deck.tmux("list-sessions", "-F", "#{session_name}\t#{session_path}")
    paths = dict(l.split("\t", 1) for l in r.stdout.splitlines())
    assert paths == {"cs-aaaaaaaa": str(work), "cmd-shell": str(deck.work)}, paths
    assert neutral_server(server_argv(deck.tmux), deck.socket)
    sid, _, cwd, _ = wait_for(deck.calls)[0].split("|")
    assert (sid, cwd) == ("aaaaaaaa", str(work))


# ── the prepared start: found without the environment ───────────────────────
@pytest.mark.parametrize("case", ["server-without-library", "server-with-another-library",
                                  "relative-library"])
def test_the_pane_finds_its_start_whatever_environment_started_the_server(deck, case):
    """The pane's environment is the tmux server's — whoever started it first (the user's
    own tmux, the bridge, an older AgentDeck). AGENTDECK_LIBRARY there, or a relative one
    read from another folder, must not send the launcher to another place than ensure's."""
    e = deck.add("A", uuid=U1)
    extra, where = {}, None
    if case == "relative-library":
        extra, where = {"AGENTDECK_LIBRARY": os.path.relpath(deck.lib, deck.tmp)}, str(deck.tmp)
    else:
        env0 = {k: v for k, v in deck.env.items()
                if k not in ("AGENTDECK_LIBRARY", "AGENTDECK_LAUNCHER")}
        if case == "server-with-another-library":
            env0["AGENTDECK_LIBRARY"] = str(deck.tmp / "elsewhere" / "library.json")
        first = subprocess.run(["tmux", "-L", deck.socket, "new-session", "-d", "-s", "users-own"],
                               env=env0, cwd=str(deck.home), capture_output=True, text=True,
                               timeout=10)
        assert first.returncode == 0, first.stderr
    r = deck.cli("ensure", e["id"], _cwd=where, **extra)
    assert (r.returncode, r.stdout.strip()) == (0, "cs-aaaaaaaa"), r.stderr
    calls = wait_for(deck.calls)
    assert calls and calls[0].startswith(f"aaaaaaaa|--session-id {U1} "), calls
    assert wait_for(lambda: not (deck.launch_dir / "aaaaaaaa").exists())
    assert deck.has("cs-aaaaaaaa")


# ── every new-session argv, whatever runs first ─────────────────────────────
def _checkout(tmp_path):
    """A copy of the launcher in a checkout of its own: (launcher path, its launch dir)."""
    co = tmp_path / "co"
    (co / "bin").mkdir(parents=True, exist_ok=True)
    launcher = co / "bin" / "pane"
    if not launcher.exists():
        shutil.copy2(REPO_LAUNCHER, launcher)
    return str(launcher), co / ".sessions" / "launch"


def test_every_new_session_library_cli_runs_is_neutral(tmp_path, monkeypatch):
    """No server: a placeholder brings one up with a line that says nothing, then the
    session is made on it (-c <folder>: that line is not the server's) and the
    placeholder is let go. A server up: just the session. The placeholder failed: the
    session without -c (the launcher enters the folder from the prepared start)."""
    m = _mod()
    monkeypatch.setattr(m.library, "LIB_FILE", str(tmp_path / "reg" / "library.json"))
    launcher, launch_dir = _checkout(tmp_path)
    monkeypatch.setattr(m, "LAUNCHER", launcher)
    folder = tmp_path / TOPIC_DIR
    folder.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(folder))
    state = {"up": False, "hold_works": True}
    calls, bare = [], []

    def rec(*a, cwd=None):
        calls.append((a, cwd))
        if a[0] == "list-sessions":
            rc = 0 if state["up"] else 1
        elif a[0] == "has-session":
            rc = 0 if a[2].startswith("=hold-") and state["hold_works"] else 1
        else:
            rc = 0
        return subprocess.CompletedProcess(a, rc, "", "")

    def rec_bare(*a):
        bare.append(a)
        return subprocess.CompletedProcess(a, 0, "", "")
    monkeypatch.setattr(m, "_tmux", rec)
    monkeypatch.setattr(m, "_tmux_bare", rec_bare)
    e = {"id": "aaaaaaaa", "uuid": U1, "cwd": str(folder)}
    argv = ["/opt/claude", "--session-id", U1, "--dangerously-skip-permissions"]

    def new_sessions():
        out = [(a, cwd) for a, cwd in calls if a[0] == "new-session"]
        calls.clear()
        return out

    # 1. no server: the placeholder starts it; the conf goes in before the real pane
    m._start(e, argv)
    assert len(bare) == 1 and neutral_server(["tmux", *bare[0]]), bare
    hold = bare[0][5]
    seq = [a for a, _ in calls]
    assert seq.index(("source-file", m.TMUX_CONF)) < [a[0] for a in seq].index("new-session")
    assert seq[-1] == ("kill-session", "-t", "=" + hold)          # let go after
    assert new_sessions() == [(("new-session", "-d", "-s", "cs-aaaaaaaa", "-c", str(folder),
                                launcher, "aaaaaaaa"), "/")]
    bare.clear()
    assert m.shell_ensure() == 0
    assert len(bare) == 1
    assert new_sessions() == [(("new-session", "-d", "-s", "cmd-shell", "-c", str(folder),
                                launcher, "shell"), "/")]
    # 2. a server runs: no placeholder
    state["up"], bare[:] = True, []
    m._start(e, argv)
    assert m.shell_ensure() == 0
    assert bare == []
    assert [a for a, _ in new_sessions()] == [
        ("new-session", "-d", "-s", "cs-aaaaaaaa", "-c", str(folder), launcher, "aaaaaaaa"),
        ("new-session", "-d", "-s", "cmd-shell", "-c", str(folder), launcher, "shell")]
    # 3. no server and the placeholder failed: nothing but the launcher and the id
    state.update(up=False, hold_works=False)
    m._start(e, argv)
    assert m.shell_ensure() == 0
    new = new_sessions()
    assert [a for a, _ in new] == [
        ("new-session", "-d", "-s", "cs-aaaaaaaa", launcher, "aaaaaaaa"),
        ("new-session", "-d", "-s", "cmd-shell", launcher, "shell")]
    for a, cwd in new:
        assert cwd == "/" and kill_words(m.tmux_argv(*a), tmp_path / "co") == [], a
    assert not [a for a, _ in calls if a[0] == "send-keys"]            # nothing typed
    # the folder travels in the prepared start too
    assert (launch_dir / "aaaaaaaa").read_bytes().split(b"\0")[0] == os.fsencode(folder)
    assert (launch_dir / "shell").read_bytes() == os.fsencode(folder) + b"\0"


def test_start_refuses_loudly_when_the_launcher_is_not_executable(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m.library, "LIB_FILE", str(tmp_path / "reg" / "library.json"))
    (tmp_path / "bin").mkdir()
    plain = tmp_path / "bin" / "pane"
    plain.write_text("#!/bin/sh\n")
    monkeypatch.setattr(m, "LAUNCHER", str(plain))
    calls = []
    monkeypatch.setattr(m, "_tmux", lambda *a, cwd=None: calls.append(a))
    with pytest.raises(RuntimeError, match="not executable"):
        m._start({"id": "aaaaaaaa", "uuid": U1, "cwd": str(tmp_path)},
                 ["/opt/claude", "--session-id", U1, "--dangerously-skip-permissions"])
    assert calls == []


def test_the_launcher_ships_executable_under_a_neutral_name(monkeypatch):
    assert os.access(REPO_LAUNCHER, os.X_OK)
    # "agentdeck" too: a `pkill -f agentdeck` meant for another install on the machine
    assert kill_words(["<repo>/bin/" + os.path.basename(REPO_LAUNCHER)]) == []
    monkeypatch.delenv("AGENTDECK_LAUNCHER", raising=False)
    assert _mod().LAUNCHER == REPO_LAUNCHER                 # the checkout's own by default
    monkeypatch.setenv("AGENTDECK_LAUNCHER", "rel/bin/pane")
    assert _mod().LAUNCHER == os.path.abspath("rel/bin/pane")   # tmux runs it from /


# ── the pane: claude with the right flags and environment ───────────────────
def test_the_pane_runs_claude_with_its_flags_env_and_rc(deck):
    (deck.home / ".profile").write_text(
        "export AGENTDECK_RC_LOGIN=1\n"
        "case $- in *i*) export AGENTDECK_RC_INTERACTIVE=1 ;; esac\n"
        "export CLAUDE_FROM_RC=leak\n"
        'echo "$PWD" > "$HOME/rc-pwd"\n')
    (deck.home / ".claude").mkdir()
    (deck.home / ".claude" / "oauth.env").write_text(
        "export CLAUDE_CODE_OAUTH_TOKEN=tok-from-oauth-env\n")
    e = deck.add("ultra", uuid=U1)
    deck.transcript(e).write_text(ULTRA_LINE)                  # ended at ultracode
    assert deck.cli("ensure", e["id"]).returncode == 0
    sid, args, cwd, leaked = wait_for(deck.calls)[0].split("|")
    assert sid == "aaaaaaaa" and cwd == str(deck.work) and leaked == ""
    assert args == f'--resume {U1} --dangerously-skip-permissions --settings {{"ultracode":true}}'
    assert (deck.home / "rc-pwd").read_text().strip() == str(deck.work)   # rc runs in the folder
    # tmux was handed the launcher; the pane's process is claude itself now (exec)
    start = pane(deck, "cs-aaaaaaaa", "#{pane_start_command}")
    assert deck.launcher in start and start.rstrip("\"'").endswith("aaaaaaaa"), start
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
    assert not (deck.launch_dir / "aaaaaaaa").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters any folder")
def test_a_topic_whose_folder_cannot_be_entered_starts_in_home_as_tmux_did(deck):
    """tmux's rule for a -c it cannot enter: home. No traceback, nothing left behind."""
    locked = deck.tmp / "locked"
    locked.mkdir()
    e = deck.add("A", uuid=U1, cwd=str(locked))
    locked.chmod(0o600)
    try:
        r = deck.cli("ensure", e["id"])
        assert r.returncode == 0, r.stderr
        sid, _, cwd, _ = wait_for(deck.calls)[0].split("|")
        assert (sid, cwd) == ("aaaaaaaa", str(deck.home))
        assert not (deck.launch_dir / "aaaaaaaa").exists()
    finally:
        locked.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters any folder")
def test_the_cmd_line_whose_folder_cannot_be_entered_opens_in_home(deck):
    locked = deck.tmp / "locked"
    locked.mkdir()
    locked.chmod(0o600)
    try:
        r = deck.cli("shell-ensure", AGENTDECK_WORKDIR=str(locked))
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert wait_for(lambda: pane(deck, "cmd-shell", "#{pane_current_command}") == "bash")
        assert wait_for(lambda: pane(deck, "cmd-shell", "#{pane_current_path}") == str(deck.home))
    finally:
        locked.chmod(0o700)


# ── the launcher on its own ─────────────────────────────────────────────────
def _launcher_env(tmp_path, **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".profile").write_text(f'echo rc >> "{home}/rc.log"\n')
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "LANG": "C.UTF-8"}
    env.update(extra)
    return home, env


def _run(argv, env, **kw):
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20, **kw)


def _prepare(launch_dir, sid, folder, argv):
    launch_dir.mkdir(parents=True, exist_ok=True)
    launch_dir.chmod(0o700)
    f = launch_dir / sid
    f.write_bytes(b"".join(os.fsencode(str(a)) + b"\0" for a in [folder, *argv]))
    f.chmod(0o600)
    return f


def _fake(tmp_path):
    """A stand-in named claude: the launcher runs nothing else."""
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    f = d / "claude"
    f.write_text('#!/bin/sh\necho "ran $* as $AGENTDECK_SESSION in $PWD"\n')
    f.chmod(0o755)
    return str(f)


@pytest.mark.parametrize("args", [[], ["aaaaaaaa", "aaaaaaaa"], ["shell", "x"], ["SHELL"],
                                  ["shell "], ["cmd-shell"]] + [[b] for b in BAD_IDS])
def test_launcher_refuses_anything_but_one_id_or_shell_before_running_anything(tmp_path, args):
    launcher, _ = _checkout(tmp_path)
    home, env = _launcher_env(tmp_path)
    r = _run([launcher, *args], env)
    assert r.returncode == 2, (args, r.stdout, r.stderr)
    assert "pane: " in r.stderr
    assert not (home / "rc.log").exists()                      # no login shell ran


def test_launcher_without_a_prepared_start_runs_nothing(tmp_path):
    launcher, _ = _checkout(tmp_path)
    home, env = _launcher_env(tmp_path)
    r = _run([launcher, "aaaaaaaa"], env)
    assert r.returncode != 0
    assert "nothing prepared" in r.stderr and "ran " not in r.stdout
    assert not (home / "rc.log").exists()                      # refused before the rc


def test_launcher_refuses_a_start_prepared_for_another_topic(tmp_path):
    launcher, launch_dir = _checkout(tmp_path)
    home, env = _launcher_env(tmp_path)
    f = _prepare(launch_dir, "aaaaaaaa", tmp_path,
                 [_fake(tmp_path), "--resume", U2, "--dangerously-skip-permissions"])
    r = _run([launcher, "aaaaaaaa"], env)
    assert r.returncode == 2 and "not for this topic" in r.stderr, r.stderr
    assert "ran " not in r.stdout and not f.exists()


def test_launcher_runs_the_prepared_start_once_in_its_folder(tmp_path):
    launcher, launch_dir = _checkout(tmp_path)
    home, env = _launcher_env(tmp_path, CLAUDECODE="1")
    folder = tmp_path / "topic folder"
    folder.mkdir()
    f = _prepare(launch_dir, "aaaaaaaa", folder,
                 [_fake(tmp_path), "--session-id", U1, "--dangerously-skip-permissions",
                  "--settings", '{"ultracode":true}'])
    r = _run([launcher, "aaaaaaaa"], env, cwd="/")
    assert r.returncode == 0, r.stderr
    assert (f"ran --session-id {U1} --dangerously-skip-permissions --settings "
            f'{{"ultracode":true}} as aaaaaaaa in {folder}') in r.stdout
    assert (home / "rc.log").read_text() == "rc\n"            # one login shell, not a loop
    assert not f.exists()
    assert _run([launcher, "aaaaaaaa"], env).returncode != 0   # used up


def test_launcher_finds_its_start_in_its_own_checkout_whatever_the_environment_says(tmp_path):
    """Not $AGENTDECK_LIBRARY's folder: the pane's environment is the tmux server's."""
    launcher, launch_dir = _checkout(tmp_path)
    decoy = _prepare(tmp_path / "elsewhere" / "launch", "aaaaaaaa", tmp_path,
                     [_fake(tmp_path), "--session-id", U1, "--decoy"])
    f = _prepare(launch_dir, "aaaaaaaa", tmp_path,
                 [_fake(tmp_path), "--session-id", U1, "--dangerously-skip-permissions"])
    home, env = _launcher_env(tmp_path, AGENTDECK_LIBRARY=str(tmp_path / "elsewhere" /
                                                               "library.json"))
    r = _run([launcher, "aaaaaaaa"], env)
    assert r.returncode == 0, r.stderr
    assert f"ran --session-id {U1} --dangerously-skip-permissions as aaaaaaaa" in r.stdout
    assert not f.exists() and decoy.exists()


def test_launcher_runs_nothing_but_claude(tmp_path):
    """Whatever lands in the launch folder, the launcher execs only a program named claude
    (claude.exe: the npm build) — not a script someone left there."""
    launcher, launch_dir = _checkout(tmp_path)
    home, env = _launcher_env(tmp_path)
    marker = tmp_path / "PWNED"
    evil = tmp_path / "evil.sh"
    evil.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    evil.chmod(0o755)
    f = _prepare(launch_dir, "aaaaaaaa", tmp_path, [evil, "--resume", U1, "x"])
    r = _run([launcher, "aaaaaaaa"], env)
    assert r.returncode == 2 and "not for this topic" in r.stderr, r.stderr
    assert not marker.exists() and not f.exists()
    exe = tmp_path / "npm" / "claude.exe"
    exe.parent.mkdir()
    shutil.copy2(_fake(tmp_path), exe)
    _prepare(launch_dir, "aaaaaaaa", tmp_path, [exe, "--resume", U1, "x"])
    r = _run([launcher, "aaaaaaaa"], env)
    assert r.returncode == 0 and f"ran --resume {U1} x" in r.stdout, r.stderr


@pytest.mark.parametrize("how", ["symlink", "group-writable", "folder-writable-by-others"])
def test_launcher_refuses_a_start_anyone_else_could_have_written(tmp_path, how):
    launcher, launch_dir = _checkout(tmp_path)
    home, env = _launcher_env(tmp_path)
    real = _prepare(tmp_path / "real", "aaaaaaaa", tmp_path,
                    [_fake(tmp_path), "--session-id", U1, "--dangerously-skip-permissions"])
    f = _prepare(launch_dir, "aaaaaaaa", tmp_path,
                 [_fake(tmp_path), "--session-id", U1, "--dangerously-skip-permissions"])
    if how == "symlink":
        f.unlink()
        f.symlink_to(real)
    elif how == "group-writable":
        f.chmod(0o620)
    else:
        launch_dir.chmod(0o777)
    r = _run([launcher, "aaaaaaaa"], env)
    assert r.returncode == 2 and "ran " not in r.stdout, (r.stdout, r.stderr)
    assert "not safe" in r.stderr, r.stderr
    assert real.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters any folder")
def test_launcher_starts_in_home_when_the_folder_cannot_be_entered(tmp_path):
    launcher, launch_dir = _checkout(tmp_path)
    home, env = _launcher_env(tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o600)
    try:
        _prepare(launch_dir, "aaaaaaaa", locked,
                 [_fake(tmp_path), "--session-id", U1, "--dangerously-skip-permissions"])
        r = _run([launcher, "aaaaaaaa"], env, cwd="/")
        assert r.returncode == 0, r.stderr
        assert f"as aaaaaaaa in {home}" in r.stdout and str(locked) in r.stderr
    finally:
        locked.chmod(0o700)


def test_launcher_shell_opens_a_clean_login_bash_in_the_prepared_folder(tmp_path):
    launcher, launch_dir = _checkout(tmp_path)
    home, env = _launcher_env(tmp_path, CLAUDECODE="1", AGENTDECK_SESSION="deadbeef")
    folder = tmp_path / "work dir"
    folder.mkdir()
    f = _prepare(launch_dir, "shell", folder, [])
    r = _run([launcher, "shell"], env, cwd="/", stdin=None,
             input='echo "at $PWD"; env | cut -d= -f1\n')
    assert r.returncode == 0, r.stderr
    assert f"at {folder}" in r.stdout
    names = r.stdout.split()
    assert "CLAUDECODE" not in names and "AGENTDECK_SESSION" not in names
    assert (home / "rc.log").read_text() == "rc\n"            # a login shell
    assert not f.exists()
    # nothing prepared (started by hand): where it was started
    r = _run([launcher, "shell"], env, cwd=str(folder), stdin=None, input='echo "at $PWD"\n')
    assert f"at {folder}" in r.stdout


def test_library_cli_prepares_the_start_where_the_launcher_reads_it(tmp_path, monkeypatch):
    m = _mod()
    # the registry is elsewhere: it has no say (the launcher can't know about it)
    monkeypatch.setattr(m.library, "LIB_FILE", str(tmp_path / "reg" / "library.json"))
    launcher, launch_dir = _checkout(tmp_path)
    monkeypatch.setattr(m, "LAUNCHER", launcher)
    argv = ["/opt/my claude", "--resume", U1, "--dangerously-skip-permissions",
            "--settings", '{"ultracode":true}']
    m.prepare_launch("aaaaaaaa", "/work/topic dir", argv)
    f = launch_dir / "aaaaaaaa"
    assert f.read_bytes() == b"".join(a.encode() + b"\0" for a in ["/work/topic dir", *argv])
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(launch_dir.stat().st_mode) == 0o700
    assert m.launch_file("aaaaaaaa") == str(f)
    assert not (tmp_path / "reg" / "launch").exists()
    m.prepare_launch("shell", "/work", [])
    assert (launch_dir / "shell").read_bytes() == b"/work\0"
    for bad in ("../x", "AAAAAAAA", "cmd-shell", ""):
        with pytest.raises(ValueError):
            m.prepare_launch(bad, "/work", [])
    with pytest.raises(ValueError):
        m.prepare_launch("aaaaaaaa", "/work\0x", [])
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
    assert not (deck.launch_dir / "aaaaaaaa").exists()


def test_a_hung_tmux_leaves_no_prepared_start_and_no_traceback(tmp_path, monkeypatch, capsys):
    m = _mod()
    lib = tmp_path / "reg" / "library.json"
    monkeypatch.setattr(m.library, "LIB_FILE", str(lib))
    monkeypatch.setenv("AGENTDECK_TMUX_SOCKET", f"agentdeck-test-hang-{os.getpid()}")
    launcher, launch_dir = _checkout(tmp_path)
    monkeypatch.setattr(m, "LAUNCHER", launcher)

    def hangs_on_new_session(*a, cwd=None):
        if a[0] == "new-session":
            raise subprocess.TimeoutExpired(["tmux", *a], 15)
        return subprocess.CompletedProcess(a, 1, "", "")
    monkeypatch.setattr(m, "_tmux", hangs_on_new_session)
    monkeypatch.setattr(m, "_tmux_bare", lambda *a: subprocess.CompletedProcess(a, 1, "", ""))
    with pytest.raises(RuntimeError, match="tmux did not answer"):
        m._start({"id": "aaaaaaaa", "uuid": U1, "cwd": str(tmp_path)},
                 ["/opt/claude", "--session-id", U1, "--dangerously-skip-permissions"])
    assert not (launch_dir / "aaaaaaaa").exists()
    # the commands themselves: a message and exit 1, not a traceback
    with m.library.update(str(lib)) as L:
        m.library.create(L, "A", cwd=str(tmp_path), now=int(time.time()), uuid=U1)
    assert m.main(["ensure", "aaaaaaaa"]) == 1
    assert "tmux did not answer" in capsys.readouterr().err
    assert m.main(["shell-ensure"]) == 1
    assert "tmux did not answer" in capsys.readouterr().err

    def hangs(*a, cwd=None):
        raise subprocess.TimeoutExpired(["tmux", *a], 15)
    monkeypatch.setattr(m, "_tmux", hangs)
    assert m.main(["ensure", "aaaaaaaa"]) == 1
    assert m.main(["shell-ensure"]) == 1
    assert "tmux did not answer" in capsys.readouterr().err
    assert not (launch_dir / "aaaaaaaa").exists() and not (launch_dir / "shell").exists()
