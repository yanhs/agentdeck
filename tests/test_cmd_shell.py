"""The dashboard's plain command line: ONE bash shell in tmux session `cmd-shell`.

The "cmd" button on the library page opens /sess/?arg=shell, so open-session.sh
accepts the literal `shell` besides an 8-hex id (and still refuses anything
else before tmux or python run). `library_cli.py shell-ensure` starts the shell
detached (`bash -l` in AGENTDECK_WORKDIR, mouse on) once; every later press
attaches to the same one. It is not a library topic: it does not count toward
MAX_ACTIVE and is never unloaded to make room. GET /api/library reports it in a
top-level `shell` field, and idle_reaper unloads it after the usual 2 h idle.

Isolation as in test_library_cli: temp registry/HOME, private tmux socket
agentdeck-test-*; the live server on the default socket is never touched.
"""
import os
import subprocess
import time

import pytest

from tests.test_idle_reaper import reaper
from tests.test_library_api import U1 as API_U1, _seed, _start, get
from tests.test_library_api import api  # noqa: F401  (fixture)
from tests.test_library_cli import BAD_IDS, U1, U2, Deck, wait_for

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "open-session.sh")
SHELL = "cmd-shell"


@pytest.fixture
def deck(tmp_path):
    d = Deck(tmp_path)
    d.env["AGENTDECK_WORKDIR"] = str(d.work)
    yield d
    d.close()


def run(deck, *args, **extra):
    return subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True,
                          env=dict(deck.env, **extra), timeout=30)


def pane_pids(deck, name):
    r = deck.tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}")
    return [p for n, _, p in (l.partition("\t") for l in r.stdout.splitlines()) if n == name]


def sessions(deck):
    r = deck.tmux("list-sessions", "-F", "#{session_name}")
    return r.stdout.split() if r.returncode == 0 else []


# ── open-session.sh: the argument ───────────────────────────────────────────
@pytest.mark.parametrize("args", [["shell", "shell"], ["shell", "aaaaaaaa"], ["SHELL"],
                                  ["shel"], ["shell;id"], ["shell "], [" shell"],
                                  ["shellx"], ["cmd-shell"], ["shell\n"]])
def test_open_session_refuses_shell_lookalikes(deck, args):
    r = run(deck, *args)
    assert r.returncode == 2, (args, r.stdout, r.stderr)
    assert "unknown session" in (r.stdout + r.stderr)
    assert not deck.server_up()


@pytest.mark.parametrize("bad", BAD_IDS)
def test_open_session_still_refuses_bad_ids(deck, bad):
    r = run(deck, bad)
    assert r.returncode == 2 and not deck.server_up()


def test_open_session_shell_dry_run_starts_nothing(deck):
    r = run(deck, "shell", DRY_RUN="1")
    assert r.returncode == 0, r.stderr
    assert "bash -l" in r.stdout and SHELL in r.stdout
    assert not deck.server_up()


def test_open_session_shell_creates_and_attaches(deck):
    tab = subprocess.Popen(["script", "-qfc", f"bash {SCRIPT} shell", "/dev/null"],
                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=deck.env)
    deck.clients.append(tab)
    assert wait_for(lambda: deck.has(SHELL))
    r = deck.tmux("list-sessions", "-F", "#{session_name}\t#{session_attached}")
    assert wait_for(lambda: f"{SHELL}\t1" in deck.tmux(
        "list-sessions", "-F", "#{session_name}\t#{session_attached}").stdout.splitlines()), r.stdout
    assert deck.calls() == []                                  # no claude was started
    tab.kill()
    time.sleep(0.3)
    assert deck.has(SHELL)                                     # closing the tab keeps it


def test_second_press_attaches_to_the_same_shell(deck):
    tabs = []
    for _ in range(2):
        t = subprocess.Popen(["script", "-qfc", f"bash {SCRIPT} shell", "/dev/null"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=deck.env)
        deck.clients.append(t)
        tabs.append(t)
        assert wait_for(lambda: deck.has(SHELL))
    first = pane_pids(deck, SHELL)
    assert wait_for(lambda: f"{SHELL}\t2" in deck.tmux(
        "list-sessions", "-F", "#{session_name}\t#{session_attached}").stdout.splitlines())
    assert sessions(deck) == [SHELL] and len(first) == 1


# ── library_cli shell-ensure ────────────────────────────────────────────────
def test_shell_ensure_starts_bash_once_and_reuses_it(deck):
    r = deck.cli("shell-ensure")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == SHELL
    assert wait_for(lambda: pane_pids(deck, SHELL))
    pid = pane_pids(deck, SHELL)
    r2 = deck.cli("shell-ensure")
    assert r2.returncode == 0 and r2.stdout.strip() == SHELL
    assert pane_pids(deck, SHELL) == pid                       # same shell, not a new one
    assert sessions(deck) == [SHELL]


def test_shell_runs_login_bash_in_the_workdir_with_mouse_on(deck):
    assert deck.cli("shell-ensure").returncode == 0
    assert wait_for(lambda: deck.tmux("list-panes", "-a", "-F",
                                      "#{session_name}\t#{pane_current_command}")
                    .stdout.splitlines().count(f"{SHELL}\tbash") == 1)
    r = deck.tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}")
    assert f"{SHELL}\t{deck.work}" in r.stdout.splitlines()
    pid = pane_pids(deck, SHELL)[0]
    argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    assert argv[0].endswith(b"bash") and b"-l" in argv
    env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
    assert not [v for v in env if v.upper().startswith(b"CLAUDE")]   # no Claude env leaks in
    opt = deck.tmux("show-options", "-t", f"={SHELL}:", "mouse")
    assert opt.stdout.strip() == "mouse on", (opt.stdout, opt.stderr)


def test_shell_restarts_after_it_was_closed(deck):
    assert deck.cli("shell-ensure").returncode == 0
    deck.tmux("kill-session", "-t", "=" + SHELL)
    assert not deck.has(SHELL)
    assert deck.cli("shell-ensure").returncode == 0
    assert deck.has(SHELL)


def test_shell_is_not_a_topic_and_never_an_eviction_victim(deck):
    a, b = deck.add("A", uuid=U1), deck.add("B", uuid=U2)
    assert deck.cli("shell-ensure").returncode == 0
    # limit 1: the shell does not take the only place ...
    assert deck.cli("ensure", a["id"], AGENTDECK_MAX_ACTIVE="1").returncode == 0
    assert deck.has(SHELL) and deck.has("cs-aaaaaaaa")
    # ... and when room is needed, the idle topic goes, not the shell
    assert deck.cli("ensure", b["id"], AGENTDECK_MAX_ACTIVE="1").returncode == 0
    assert deck.has(SHELL) and deck.has("cs-bbbbbbbb") and not deck.has("cs-aaaaaaaa")
    assert set(deck.active()) == {"bbbbbbbb"}                  # not listed as a topic


def test_shell_ensure_takes_no_arguments(deck):
    r = deck.cli("shell-ensure", "x")
    assert r.returncode == 1 and not deck.server_up()


# ── GET /api/library: top-level `shell` ─────────────────────────────────────
def test_api_shell_off_when_not_running(api):  # noqa: F811
    code, data = get(api)
    assert code == 200
    assert data["shell"] == {"active": False, "attached": False, "status": "off"}
    assert set(data) == {"max_active", "sessions", "_system", "shell"}


def test_api_shell_active_and_not_mixed_into_sessions(api):  # noqa: F811
    _seed(api, (API_U1, "тема", 10))
    _start(api, SHELL)
    code, data = get(api)
    assert code == 200
    assert data["shell"] == {"active": True, "attached": False, "status": "idle"}
    assert [s["id"] for s in data["sessions"]] == ["aaaa1111"]
    assert data["sessions"][0]["active"] is False


def test_api_shell_working_when_it_burns_cpu(api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(api.ss, "SAMPLE_INTERVAL", 0.5)
    _start(api, SHELL, "while :; do :; done")
    assert get(api)[1]["shell"]["status"] == "working"


def test_api_shell_name_is_exact(api):  # noqa: F811
    _start(api, "cmd-shellx")
    _start(api, "cmd-shel")
    assert get(api)[1]["shell"]["active"] is False


# ── idle_reaper watches it ──────────────────────────────────────────────────
def test_reaper_watches_the_shell_when_it_exists(monkeypatch):
    monkeypatch.setattr(reaper, "_tmux_session_names",
                        lambda: ["cmd-shell", "cmd-shellx", "cs-aaaaaaaa"])
    w = reaper.watched_sessions()
    assert "cmd-shell" in w and "cmd-shellx" not in w and "cs-aaaaaaaa" in w
    assert len(w) == len(set(w))
    monkeypatch.setattr(reaper, "_tmux_session_names", lambda: [])
    assert "cmd-shell" not in reaper.watched_sessions()
