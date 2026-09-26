"""AgentDeck's own tmux settings: `tmux.conf` at the repo root.

Selecting text in a dashboard terminal needs tmux's mouse mode, and the
dashboard's copy (web/index.html enableClipboardCopy -> /api/tmux-buffer) needs
tmux to copy the selection into its buffer when the mouse button is released.
A fresh install had none of that (it lived only in the author's ~/.tmux.conf).

A tmux server AgentDeck starts gets the file via `source-file` right after it
comes up, before the first real pane (library_cli.hold_server: the server itself is
started with -f /dev/null, so no path lands on its command line); every other tmux
call carries `-f <repo>/tmux.conf` (library_cli.tmux_argv — tmux reads -f only when
the server starts) for a server that comes up another way; a server that was already
running gets it once via `source-file` on ensure / shell-ensure (the file sets the
marker @agentdeck-conf, so it is never sourced twice). The file ends by sourcing the
user's own ~/.tmux.conf, which therefore still applies on top.

Isolation: a private tmux socket per test (Deck from test_library_cli, killed at
the end), fake HOME; conftest points TMUX_TMPDIR at a throwaway directory. The
live server is never touched.
"""
import os

import pytest

from tests.test_library_cli import U1, Deck, _mod, wait_for

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.path.join(REPO, "tmux.conf")


@pytest.fixture
def deck(tmp_path):
    d = Deck(tmp_path)
    d.env["AGENTDECK_WORKDIR"] = str(d.work)
    yield d
    d.close()


def opt(deck, *args):
    r = deck.tmux("show-options", "-v", *args)
    return r.stdout.strip()


def conf_lines():
    with open(CONF) as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]


# ── the file ────────────────────────────────────────────────────────────────
def test_repo_ships_tmux_conf_with_the_mouse_and_copy_settings():
    lines = conf_lines()
    for want in ("set -g mouse on",
                 "set -s set-clipboard on",
                 "bind -T copy-mode MouseDragEnd1Pane send-keys -X copy-selection-and-cancel",
                 "bind -T copy-mode-vi MouseDragEnd1Pane send-keys -X copy-selection-and-cancel",
                 "set -g history-limit 50000",
                 'set -g default-terminal "xterm-256color"',
                 "unbind -T root MouseDown3Pane",
                 "unbind -T root MouseDown3Status",
                 "unbind -T root MouseDown3StatusLeft",
                 "unbind -T root M-MouseDown3Pane"):
        assert want in lines, want
    # the user's own config comes last, so it still applies on top
    assert lines[-1] == 'if-shell "test -f ~/.tmux.conf" "source-file ~/.tmux.conf"'


def test_dockerignore_keeps_tmux_conf_in_the_image():
    # the image is `COPY . /app`; library_cli runs from /app, so /app/tmux.conf is used
    with open(os.path.join(REPO, ".dockerignore")) as f:
        ignored = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    assert "tmux.conf" not in ignored and "*.conf" not in ignored
    with open(os.path.join(REPO, "Dockerfile")) as f:
        assert "COPY . /app" in f.read()


# ── every tmux call carries -f (the first one may start the server) ─────────
def test_tmux_argv_passes_the_conf(monkeypatch):
    m = _mod()
    monkeypatch.delenv("AGENTDECK_TMUX_SOCKET", raising=False)
    assert m.tmux_argv("new-session", "-d")[:3] == ["tmux", "-f", CONF]
    monkeypatch.setenv("AGENTDECK_TMUX_SOCKET", "agentdeck-test-x")
    a = m.tmux_argv("new-session", "-d")
    assert a[:5] == ["tmux", "-L", "agentdeck-test-x", "-f", CONF]
    assert a[5:] == ["new-session", "-d"]


def test_tg_bridge_tmux_calls_carry_the_conf():
    from tests.test_tg_bridge import tb      # imports it with the test token env
    assert tb._tmux("-V").args[-3:] == ["-f", CONF, "-V"]
    src = open(os.path.join(REPO, "tg_bridge.py")).read()
    # a server the bridge finds running without our settings gets them first
    start = src.index("def start_session")
    assert src.index("ensure_tmux_conf(", start) < src.index('"new-session"', start)


# ── a server started by ensure ──────────────────────────────────────────────
def _assert_agentdeck_settings(deck, sess):
    assert opt(deck, "-g", "mouse") == "on"
    assert opt(deck, "-t", f"={sess}:", "mouse") in ("", "on")   # not turned off per session
    assert opt(deck, "-g", "history-limit") == "50000"
    assert opt(deck, "-s", "set-clipboard") == "on"
    assert opt(deck, "-g", "default-terminal") == "xterm-256color"
    for table in ("copy-mode", "copy-mode-vi"):
        r = deck.tmux("list-keys", "-T", table, "MouseDragEnd1Pane")
        assert "copy-selection-and-cancel" in r.stdout, (table, r.stdout, r.stderr)
    # right click is left to the browser (no tmux menu)
    assert deck.tmux("list-keys", "-T", "root", "MouseDown3Pane").returncode != 0


def _drag_copies(deck, sess):
    """Select the pane's first line in copy mode and 'release the mouse':
    the selection must land in tmux's buffer (what /api/tmux-buffer reads)."""
    t = f"={sess}:"
    deck.tmux("delete-buffer")
    assert deck.tmux("copy-mode", "-t", t).returncode == 0
    for x in ("history-top", "start-of-line", "begin-selection", "end-of-line"):
        deck.tmux("send-keys", "-t", t, "-X", x)
    assert deck.tmux("send-keys", "-t", t, "MouseDragEnd1Pane").returncode == 0
    return deck.tmux("show-buffer").stdout


def test_ensure_starts_the_server_with_agentdeck_settings(deck):
    assert not deck.server_up()
    e = deck.add("A", uuid=U1)
    r = deck.cli("ensure", e["id"])
    assert r.returncode == 0, r.stderr
    sess = r.stdout.strip()
    _assert_agentdeck_settings(deck, sess)


def test_mouse_release_copies_the_selection_to_the_buffer(deck):
    e = deck.add("A", uuid=U1)
    sess = deck.cli("ensure", e["id"]).stdout.strip()
    # a pane with known text: a second window would need another session; use cmd-shell
    assert deck.cli("shell-ensure").returncode == 0
    deck.tmux("send-keys", "-t", "=cmd-shell:", "-l", "clear; echo copy-me-please")
    deck.tmux("send-keys", "-t", "=cmd-shell:", "Enter")
    wait_for(lambda: "copy-me-please" in deck.tmux("capture-pane", "-p", "-t",
                                                   "=cmd-shell:").stdout.split("\n", 1)[-1])
    out = _drag_copies(deck, "cmd-shell")
    assert out.strip(), out
    assert sess.startswith("cs-")


def test_shell_ensure_starts_the_server_with_agentdeck_settings(deck):
    assert not deck.server_up()
    assert deck.cli("shell-ensure").returncode == 0
    _assert_agentdeck_settings(deck, "cmd-shell")


# ── a server that was already running without it ───────────────────────────
def test_ensure_sources_the_conf_once_into_a_running_server(deck):
    # the user's config logs every time it is read
    (deck.home / ".tmux.conf").write_text(
        'run-shell "echo read >> $HOME/user-conf.log"\n')
    # someone else started the server (no -f): default config only
    deck.tmux("new-session", "-d", "-s", "other-work")
    assert opt(deck, "-g", "history-limit") != "50000"
    log = deck.home / "user-conf.log"
    wait_for(log.exists)
    assert log.read_text().count("read") == 1              # tmux's own default read
    e = deck.add("A", uuid=U1)
    sess = deck.cli("ensure", e["id"]).stdout.strip()
    _assert_agentdeck_settings(deck, sess)
    assert deck.cli("shell-ensure").returncode == 0
    assert deck.cli("ensure", e["id"]).returncode == 0
    wait_for(lambda: log.read_text().count("read") >= 2)
    assert log.read_text().count("read") == 2              # + exactly one source-file


# ── the user's own ~/.tmux.conf still applies on top ────────────────────────
def test_user_tmux_conf_is_sourced_after_ours(deck):
    (deck.home / ".tmux.conf").write_text("set -g @user-mark yes\nset -g history-limit 777\n")
    e = deck.add("A", uuid=U1)
    assert deck.cli("ensure", e["id"]).returncode == 0
    assert opt(deck, "-g", "@user-mark") == "yes"
    assert opt(deck, "-g", "history-limit") == "777"       # the user's value wins
    assert opt(deck, "-g", "mouse") == "on"                # ours still there
