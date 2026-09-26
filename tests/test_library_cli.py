"""library_cli.py — the one place that starts / unloads topic-sessions in tmux.

`ensure <id>` is what the ttyd page (open-session.sh) and the Telegram bridge
call before attaching: it refuses anything that is not a known, non-archived
8-hex id (the id arrives from a URL), keeps at most MAX_ACTIVE sessions loaded
by unloading the least recently used idle one, and starts Claude with
`--resume` when the transcript exists, `--session-id` otherwise.

Every test runs in an isolated world: temp registry (AGENTDECK_LIBRARY), temp
HOME with a fake `claude`, and a private tmux server (AGENTDECK_TMUX_SOCKET =
agentdeck-test-cli-*) that is killed at the end. The live tmux server on the
default socket (claude-terminal*, cs-*) is never touched.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid as _uuid

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "library_cli.py")

_spec = importlib.util.spec_from_file_location("library", os.path.join(REPO, "library.py"))
library = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(library)

U1 = "aaaaaaaa-1111-4111-8111-111111111111"
U2 = "bbbbbbbb-2222-4222-8222-222222222222"
U3 = "c0ffee00-1111-4222-8333-444455556666"

BAD_IDS = [";rm -rf /", "AAAAAAAA", "aaaaaaa", "aaaaaaaa5", "../x", "", "aaaaaaaa\n",
           "$(id)", "aaaaaaaa;id", "-t", "aaaaaaag"]

FAKE_CLAUDE = r"""#!/bin/bash
# stands in for the claude CLI: records how it was started, then idles
echo "$AGENTDECK_SESSION|$*|$PWD|${CLAUDECODE:-}${CLAUDE_CODE_ENTRYPOINT:-}" >> "$HOME/claude-calls.log"
if [ -f "$HOME/bg-$AGENTDECK_SESSION" ]; then          # simulate a live background task
  mkdir -p "$HOME/claude-tmp/tasks"
  sleep 600 > "$HOME/claude-tmp/tasks/b0bcat.output" &
fi
[ -f "$HOME/exit-$AGENTDECK_SESSION" ] && exit 0        # simulate Claude quitting
# keep looking like claude to tmux (#{pane_current_command} = argv[0]) and to a
# /proc scan (argv[0] "claude" + its --resume/--session-id <uuid> arguments)
exec -a claude /bin/bash -c 'sleep 600; :' claude "$@"
"""

# a claude process started outside the library, e.g. `claude --resume <uuid>`
# typed by hand in another terminal: argv[0] "claude", the uuid in its arguments
def outside_claude_argv(flag, u):
    return ["bash", "-c", f"exec -a claude /bin/bash -c 'sleep 600; :' claude {flag} {u}"]


def wait_for(pred, timeout=6.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


class Deck:
    """Temp registry + temp HOME + fake claude + a private tmux server."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.lib = str(tmp_path / "reg" / "library.json")
        self.socket = f"agentdeck-test-cli-{os.getpid()}-{_uuid.uuid4().hex[:6]}"
        self.claude = tmp_path / "fake-claude"
        self.claude.write_text(FAKE_CLAUDE)
        self.claude.chmod(0o755)
        # second guard: even if CLAUDE_BIN got lost, `claude` on PATH is the fake
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "claude").symlink_to(self.claude)
        self.clients = []
        # not TMUX, and not the conversation of a Claude that runs this suite: `hold -`
        # reads CLAUDE_CODE_SESSION_ID (a pane's live conversation)
        env = {k: v for k, v in os.environ.items()
               if k not in ("TMUX", "CLAUDE_CODE_SESSION_ID")}
        env.update(PATH=f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                   HOME=str(self.home), AGENTDECK_LIBRARY=self.lib,
                   AGENTDECK_TMUX_SOCKET=self.socket, CLAUDE_BIN=str(self.claude),
                   AGENTDECK_WORKING_SECONDS="0", AGENTDECK_MAX_ACTIVE="12",
                   OPEN_SESSION_PAUSE="0", TERM="xterm-256color", LANG="C.UTF-8",
                   # a Claude-spawned environment: must NOT leak into the pane
                   CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="sdk-py")
        self.env = env

    # registry
    def add(self, name="тема", uuid=None, archived=False, cwd=None, last_used=None):
        with library.update(self.lib) as L:
            e = library.create(L, name, cwd=cwd or str(self.work), now=int(time.time()),
                               uuid=uuid or str(_uuid.uuid4()))
            e["archived"] = archived
            if last_used is not None:
                e["last_used"] = last_used
        return dict(e)

    def entry(self, sid):
        return library.find(library.load(self.lib), sid)

    def set_last_used(self, sid, t):
        with library.update(self.lib) as L:
            library.find(L, sid)["last_used"] = t

    def transcript(self, e):
        slug = "".join(c if c.isalnum() and c.isascii() else "-" for c in e["cwd"])
        p = self.home / ".claude" / "projects" / slug / f"{e['uuid']}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n")
        return p

    # processes
    def cli(self, *args, **extra):
        env = dict(self.env, **extra)
        return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True,
                              env=env, timeout=30)

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.socket, *args], capture_output=True,
                              text=True, env=self.env, timeout=10)

    def has(self, name):
        return self.tmux("has-session", "-t", "=" + name).returncode == 0

    def server_up(self):
        return self.tmux("list-sessions").returncode == 0

    def calls(self):
        p = self.home / "claude-calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def attach(self, sid):
        """A real tmux client in a pty (what a browser tab is), kept open."""
        p = subprocess.Popen(
            ["script", "-qfc", f"tmux -L {self.socket} attach-session -t =cs-{sid}", "/dev/null"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=self.env)
        self.clients.append(p)
        return p

    def active(self):
        r = self.cli("active")
        assert r.returncode == 0, r.stderr
        return {s["id"]: s for s in json.loads(r.stdout)}

    def close(self):
        for p in self.clients:
            subprocess.run(["pkill", "-P", str(p.pid)], capture_output=True)
            p.kill()
        self.tmux("kill-server")
        # anything the deck started that outlived its tmux server (seen: a fake
        # `claude --session-id aaaaaaaa…` reparented to init, which made later runs'
        # ensure answer "already running elsewhere"); all of them carry HOME=<deck home>
        for pid in _procs_with_home(self.home):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        try:
            os.unlink(f"/tmp/tmux-{os.getuid()}/{self.socket}")
        except OSError:
            pass


@pytest.fixture
def deck(tmp_path):
    d = Deck(tmp_path)
    yield d
    d.close()


def _mod():
    spec = importlib.util.spec_from_file_location("library_cli", CLI)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── pane command (pure) ─────────────────────────────────────────────────────
def test_slug_matches_claude_project_dirs():
    m = _mod()
    assert m.slug("/home/ubuntu/pr") == "-home-ubuntu-pr"
    assert m.slug("/home/ubuntu/pr/Appeals/dev/web.next") == "-home-ubuntu-pr-Appeals-dev-web-next"
    assert m.slug("/tmp/claude-1000/-home-ubuntu-pr/x") == "-tmp-claude-1000--home-ubuntu-pr-x"


# Claude Code 2.1.283's own names (qx): the regex runs over UTF-16 code units, so
# a character above U+FFFF becomes "--"; past 200 chars the name is cut and
# "-" + base-36 of |Java string hash| of the cwd is appended. Values from node.
CLAUDE_DIR_NAMES = [
    ("/home/ubuntu/pr/Светлота 💡 (Rus)", "-home-ubuntu-pr--------------Rus-"),
    ("/" + "b" * 199, "-" + "b" * 199),                                 # 200: not cut
    ("/" + "b" * 200, "-" + "b" * 199 + "-86ms41"),
    ("/home/ubuntu/" + "deep/" * 45 + "end",
     ("-home-ubuntu-" + "deep-" * 45 + "end")[:200] + "-cqcn6p"),
]


@pytest.mark.parametrize("cwd,name", CLAUDE_DIR_NAMES)
def test_slug_is_claudes_name_for_emoji_and_long_paths(cwd, name):
    assert _mod().slug(cwd) == name
    assert library.cwd_slug(cwd) == name                               # one rule, both modules


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="needs node")
def test_slug_agrees_with_claudes_own_code_on_random_paths():
    import random
    rnd = random.Random(7)
    alphabet = "abcXYZ019/._- ~é€Ωж日💡😀\U0001f9ea"
    cwds = ["/" + "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 260)))
            for _ in range(300)]
    js = ("function KJ(t){let e=0;for(let n=0;n<t.length;n++)e=(e<<5)-e+t.charCodeAt(n)|0;return e}"
          "function qx(e){let n=e.replace(/[^a-zA-Z0-9]/g,'-');if(n.length<=200)return n;"
          "return `${n.slice(0,200)}-${Math.abs(KJ(e)).toString(36)}`}"
          "const a=JSON.parse(require('fs').readFileSync(0,'utf8'));"
          "process.stdout.write(JSON.stringify(a.map(qx)))")
    r = subprocess.run(["node", "-e", js], input=json.dumps(cwds), capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    m = _mod()
    assert [m.slug(c) for c in cwds] == json.loads(r.stdout)


def test_pane_command_resumes_a_topic_in_an_emoji_folder(tmp_path):
    m = _mod()
    cwd = tmp_path / "Светлота 💡 (Rus)"
    cwd.mkdir()
    name = "".join(c if c.isascii() and c.isalnum() else "-" * (len(c.encode("utf-16-le")) // 2)
                   for c in str(cwd))
    t = tmp_path / "h" / ".claude" / "projects" / name / f"{U1}.jsonl"
    t.parent.mkdir(parents=True)
    t.write_text("{}\n")
    e = {"id": "aaaaaaaa", "uuid": U1, "name": "x", "cwd": str(cwd)}
    cmd = m.pane_command(e, home=str(tmp_path / "h"), claude_bin="/opt/claude")
    assert f"--resume {U1}" in cmd, cmd


def test_transcript_path_of_a_long_cwd_finds_claudes_folder(tmp_path):
    m = _mod()
    home = tmp_path / "h"
    cwd = "/home/ubuntu/" + "deep/" * 45 + "end"
    exact = m.transcript_path(str(home), cwd, U1)
    assert os.path.basename(os.path.dirname(exact)) == CLAUDE_DIR_NAMES[3][1]
    # Claude also accepts another hash after the same 200 chars (it globs for it)
    other = home / ".claude" / "projects" / (CLAUDE_DIR_NAMES[3][1][:200] + "-zz9") / f"{U1}.jsonl"
    other.parent.mkdir(parents=True)
    other.write_text("{}\n")
    assert m.transcript_path(str(home), cwd, U1) == str(other)
    assert m.transcript_path(str(home), cwd, U2).endswith(
        CLAUDE_DIR_NAMES[3][1] + f"/{U2}.jsonl")                        # none: the exact path
    os.makedirs(os.path.dirname(exact))
    with open(exact, "w") as f:
        f.write("{}\n")
    assert m.transcript_path(str(home), cwd, U1) == exact              # the exact one first


def test_transcript_path_of_a_short_cwd_never_globs(tmp_path):
    m = _mod()
    home = tmp_path / "h"
    stray = home / ".claude" / "projects" / "-home-ubuntu-pr-x" / f"{U1}.jsonl"
    stray.parent.mkdir(parents=True)
    stray.write_text("{}\n")
    assert m.transcript_path(str(home), "/home/ubuntu/pr", U1) == str(
        home / ".claude" / "projects" / "-home-ubuntu-pr" / f"{U1}.jsonl")


def test_pane_command_session_id_when_no_transcript(tmp_path):
    m = _mod()
    e = {"id": "aaaaaaaa", "uuid": U1, "name": "x", "cwd": str(tmp_path)}
    cmd = m.pane_command(e, home=str(tmp_path / "h"), claude_bin="/opt/claude")
    assert f"--session-id {U1}" in cmd and "--resume" not in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "export AGENTDECK_SESSION=aaaaaaaa" in cmd


def test_pane_command_resume_when_transcript_exists(tmp_path):
    m = _mod()
    home = tmp_path / "h"
    e = {"id": "aaaaaaaa", "uuid": U1, "name": "x", "cwd": "/home/ubuntu/pr"}
    t = home / ".claude" / "projects" / "-home-ubuntu-pr" / f"{U1}.jsonl"
    t.parent.mkdir(parents=True)
    t.write_text("{}\n")
    cmd = m.pane_command(e, home=str(home), claude_bin="/opt/claude")
    assert f"--resume {U1}" in cmd and "--session-id" not in cmd


def test_pane_command_scrubs_claude_vars_and_sources_oauth(tmp_path):
    m = _mod()
    e = {"id": "aaaaaaaa", "uuid": U1, "name": "x", "cwd": str(tmp_path)}
    cmd = m.pane_command(e, home="/home/u", claude_bin="/opt/claude")
    assert "grep -i CLAUDE" in cmd and 'unset "$v"' in cmd
    assert "[ -r /home/u/.claude/oauth.env ] && . /home/u/.claude/oauth.env" in cmd
    # order: scrub, token, id, then claude
    assert cmd.index("unset") < cmd.index("oauth.env") < cmd.index("AGENTDECK_SESSION") \
        < cmd.index("/opt/claude")


def test_pane_command_never_contains_the_topic_name(tmp_path):
    m = _mod()
    evil = "Налоги'; touch /tmp/pwned; echo \"$(id)\" `id` $HOME"
    e = {"id": "aaaaaaaa", "uuid": U1, "name": evil, "cwd": str(tmp_path)}
    cmd = m.pane_command(e, home="/home/u", claude_bin="/opt/claude")
    assert "Налоги" not in cmd and "pwned" not in cmd and "$(id)" not in cmd


def test_pane_command_rejects_a_bad_uuid_or_mismatched_id(tmp_path):
    m = _mod()
    for bad in ({"id": "aaaaaaaa", "uuid": "aaaaaaaa; rm -rf ~", "cwd": "/"},
                {"id": "deadbeef", "uuid": U1, "cwd": "/"},             # id != uuid[:8]
                {"id": "aaaaaaaa", "uuid": U1.upper(), "cwd": "/"}):
        with pytest.raises(ValueError):
            m.pane_command(bad, home="/home/u", claude_bin="/opt/claude")


def test_pane_command_quotes_the_claude_path(tmp_path):
    m = _mod()
    e = {"id": "aaaaaaaa", "uuid": U1, "name": "x", "cwd": str(tmp_path)}
    cmd = m.pane_command(e, home="/home/u", claude_bin="/opt/my claude;x")
    assert "'/opt/my claude;x' --session-id" in cmd


# ── ensure: refusals ────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", BAD_IDS)
def test_ensure_rejects_malformed_ids_before_any_tmux(deck, bad):
    deck.add("живая", uuid=U1)
    r = deck.cli("ensure", bad)
    assert r.returncode == 2, (bad, r.stdout, r.stderr)
    assert r.stdout == ""
    assert "unknown session" in r.stderr
    assert not deck.server_up(), "a tmux server was started for a bad id"


def test_ensure_unknown_id_exits_2_and_does_not_create_the_registry(deck):
    r = deck.cli("ensure", "deadbeef")
    assert r.returncode == 2 and "unknown session" in r.stderr
    assert not os.path.exists(deck.lib) and not os.path.exists(os.path.dirname(deck.lib))
    assert not deck.server_up()


def test_ensure_archived_id_exits_2(deck):
    e = deck.add("в архиве", uuid=U1, archived=True)
    r = deck.cli("ensure", e["id"])
    assert r.returncode == 2 and "unknown session" in r.stderr
    assert not deck.server_up()


def test_ensure_refuses_registry_entry_with_corrupt_uuid(deck):
    deck.add("ok", uuid=U1)
    with library.update(deck.lib) as L:
        library.find(L, "aaaaaaaa")["uuid"] = "aaaaaaaa-x; touch /tmp/pwned"
    r = deck.cli("ensure", "aaaaaaaa")
    assert r.returncode == 2 and "unknown session" in r.stderr and "pwned" not in r.stderr
    assert not deck.server_up()


# ── ensure: start / reuse / resume ─────────────────────────────────────────
def test_ensure_starts_detached_session_and_prints_tmux_name(deck):
    e = deck.add("ImmAppeal деплой", uuid=U1, last_used=100)
    r = deck.cli("ensure", e["id"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "cs-aaaaaaaa"
    assert deck.has("cs-aaaaaaaa")
    calls = wait_for(deck.calls)
    assert len(calls) == 1
    sid, args, cwd, leaked = calls[0].split("|")
    assert sid == "aaaaaaaa"                                   # AGENTDECK_SESSION exported
    assert f"--session-id {U1}" in args and "--dangerously-skip-permissions" in args
    assert cwd == str(deck.work)                               # runs in the entry's cwd
    assert leaked == ""                                        # CLAUDE* vars scrubbed
    assert deck.entry("aaaaaaaa")["last_used"] > 100           # touched


def test_ensure_twice_reuses_the_running_session(deck):
    e = deck.add("одна", uuid=U1)
    assert deck.cli("ensure", e["id"]).returncode == 0
    wait_for(deck.calls)
    deck.set_last_used(e["id"], 5)
    r = deck.cli("ensure", e["id"])
    assert r.returncode == 0 and r.stdout.strip() == "cs-aaaaaaaa"
    time.sleep(0.5)
    assert len(deck.calls()) == 1                              # no second claude
    assert deck.entry(e["id"])["last_used"] > 5


def test_ensure_resumes_when_transcript_exists(deck):
    e = deck.add("старая", uuid=U2)
    deck.transcript(e)
    assert deck.cli("ensure", e["id"]).returncode == 0
    calls = wait_for(deck.calls)
    assert f"--resume {U2}" in calls[0] and "--session-id" not in calls[0]


def test_ensure_touches_nothing_on_the_default_socket(deck):
    e = deck.add("x", uuid=U3)
    assert deck.cli("ensure", e["id"]).returncode == 0
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    r = subprocess.run(["tmux", "has-session", "-t", "=cs-c0ffee00"], capture_output=True, env=env)
    assert r.returncode != 0                                   # read-only check


# ── ensure: the 12 limit ───────────────────────────────────────────────────
def test_ensure_unloads_least_recently_used_idle_session_at_the_limit(deck):
    a, b = deck.add("A", uuid=U1), deck.add("B", uuid=U2)
    c = deck.add("C", uuid=U3)
    assert deck.cli("ensure", a["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.cli("ensure", b["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    deck.set_last_used(a["id"], 100)                           # A is the oldest
    deck.set_last_used(b["id"], 200)
    r = deck.cli("ensure", c["id"], AGENTDECK_MAX_ACTIVE="2")
    assert r.returncode == 0, r.stderr
    assert not deck.has("cs-aaaaaaaa") and deck.has("cs-bbbbbbbb") and deck.has("cs-c0ffee00")
    assert "cs-aaaaaaaa" in r.stderr and "«A»" in r.stderr      # says whom it unloaded


def test_ensure_refuses_with_exit_3_when_all_loaded_are_working(deck):
    a, b, c = deck.add("A", uuid=U1), deck.add("B", uuid=U2), deck.add("C", uuid=U3)
    assert deck.cli("ensure", a["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.cli("ensure", b["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    # both just printed something -> both "working" within a 1-hour window
    r = deck.cli("ensure", c["id"], AGENTDECK_MAX_ACTIVE="2", AGENTDECK_WORKING_SECONDS="3600")
    assert r.returncode == 3
    assert r.stdout == ""
    assert "2" in r.stderr and "busy" in r.stderr
    assert deck.has("cs-aaaaaaaa") and deck.has("cs-bbbbbbbb") and not deck.has("cs-c0ffee00")


def test_session_with_a_live_background_task_is_working_and_kept(deck):
    a, b, c = deck.add("A", uuid=U1), deck.add("B", uuid=U2), deck.add("C", uuid=U3)
    (deck.home / "bg-aaaaaaaa").write_text("")                 # A runs a background task
    assert deck.cli("ensure", a["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.cli("ensure", b["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    wait_for(lambda: len(deck.calls()) == 2)
    act = wait_for(lambda: (lambda x: x if x["aaaaaaaa"]["working"] else None)(deck.active()))
    assert act["aaaaaaaa"]["working"] is True and act["bbbbbbbb"]["working"] is False
    deck.set_last_used(a["id"], 100)                           # A oldest, but busy
    deck.set_last_used(b["id"], 200)
    assert deck.cli("ensure", c["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.has("cs-aaaaaaaa") and not deck.has("cs-bbbbbbbb")


def test_attached_session_is_not_unloaded(deck):
    a, b, c = deck.add("A", uuid=U1), deck.add("B", uuid=U2), deck.add("C", uuid=U3)
    assert deck.cli("ensure", a["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.cli("ensure", b["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    deck.attach(a["id"])                                       # a tab is open on A
    assert wait_for(lambda: deck.active()["aaaaaaaa"]["attached"])
    deck.set_last_used(a["id"], 100)
    deck.set_last_used(b["id"], 200)
    assert deck.cli("ensure", c["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.has("cs-aaaaaaaa") and not deck.has("cs-bbbbbbbb")


def test_non_library_sessions_neither_count_nor_get_killed(deck):
    deck.tmux("new-session", "-d", "-s", "claude-terminal-3")  # a legacy slot on the same server
    a, b = deck.add("A", uuid=U1), deck.add("B", uuid=U2)
    assert deck.cli("ensure", a["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.cli("ensure", b["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.has("cs-aaaaaaaa") and deck.has("cs-bbbbbbbb")   # legacy did not count
    c = deck.add("C", uuid=U3)
    deck.set_last_used(a["id"], 100)
    assert deck.cli("ensure", c["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.has("claude-terminal-3") and not deck.has("cs-aaaaaaaa")


# ── active ──────────────────────────────────────────────────────────────────
def test_active_lists_loaded_library_sessions_as_json(deck):
    deck.tmux("new-session", "-d", "-s", "claude-terminal-3")
    deck.tmux("new-session", "-d", "-s", "cs-NOTHEX1")          # not a valid id: ignored
    a = deck.add("A", uuid=U1)
    assert deck.cli("ensure", a["id"]).returncode == 0
    act = deck.active()
    assert set(act) == {"aaaaaaaa"}
    s = act["aaaaaaaa"]
    assert set(s) >= {"id", "attached", "working", "last_output"}
    assert s["attached"] is False and s["working"] is False
    assert abs(s["last_output"] - time.time()) < 60


def test_active_with_no_tmux_server_is_an_empty_list(deck):
    r = deck.cli("active")
    assert r.returncode == 0 and json.loads(r.stdout) == []
    assert not deck.server_up()


def test_pane_cmd_subcommand_prints_the_command_without_starting(deck):
    e = deck.add("x", uuid=U1)
    r = deck.cli("pane-cmd", e["id"])
    assert r.returncode == 0 and f"--session-id {U1}" in r.stdout
    assert not deck.server_up()
    r = deck.cli("pane-cmd", "deadbeef")
    assert r.returncode == 2


# ── the pane is claude, never a bare shell ─────────────────────────────────
def test_pane_command_execs_claude(tmp_path):
    m = _mod()
    e = {"id": "aaaaaaaa", "uuid": U1, "name": "x", "cwd": str(tmp_path)}
    cmd = m.pane_command(e, home="/home/u", claude_bin="/opt/claude")
    assert "exec /opt/claude --session-id" in cmd


def test_when_claude_exits_the_session_ends_instead_of_leaving_a_shell(deck):
    # a bash prompt left behind would run Telegram text as shell commands
    e = deck.add("A", uuid=U1)
    (deck.home / "exit-aaaaaaaa").write_text("")
    r = deck.cli("ensure", e["id"])
    assert r.returncode == 0, r.stderr
    assert wait_for(lambda: not deck.has("cs-aaaaaaaa")), "pane fell back to a shell"


def test_pane_is_claude(deck):
    e = deck.add("A", uuid=U1)
    assert deck.cli("ensure", e["id"]).returncode == 0
    assert wait_for(lambda: deck.cli("pane-is-claude", "aaaaaaaa").returncode == 0)
    deck.add("B", uuid=U2)
    deck.tmux("new-session", "-d", "-s", "cs-bbbbbbbb")       # a bare shell in the pane
    time.sleep(0.3)
    assert deck.cli("pane-is-claude", "bbbbbbbb").returncode == 1
    assert deck.cli("pane-is-claude", "c0ffee00").returncode == 1   # not loaded
    assert deck.cli("pane-is-claude", "; id").returncode == 2


def test_pane_is_claude_matches_the_exact_session_only(deck):
    deck.add("A", uuid=U1)
    deck.tmux("new-session", "-d", "-s", "cs-aaaaaaaax",
              "bash -c \"exec -a claude /bin/bash -c 'sleep 600; :'\"")
    time.sleep(0.3)
    assert deck.cli("pane-is-claude", "aaaaaaaa").returncode == 1


# ── hold: a pending timer keeps the session loaded ─────────────────────────
def test_hold_writes_an_expiry_marker(deck):
    t = time.time()
    r = deck.cli("hold", "aaaaaaaa", "600")
    assert r.returncode == 0, r.stderr
    until = library.hold_until("aaaaaaaa", lib_file=deck.lib)
    assert t + 590 <= until <= time.time() + 610
    assert not deck.server_up()


def test_hold_dash_takes_the_id_from_agentdeck_session(deck):
    r = deck.cli("hold", "-", "60", AGENTDECK_SESSION="bbbbbbbb")
    assert r.returncode == 0, r.stderr
    assert library.hold_until("bbbbbbbb", lib_file=deck.lib) > time.time()


def test_hold_refuses_bad_input(deck):
    env = {k: v for k, v in deck.env.items() if k != "AGENTDECK_SESSION"}
    r = subprocess.run([sys.executable, CLI, "hold", "-", "60"], capture_output=True,
                       text=True, env=env, timeout=30)
    assert r.returncode == 2
    assert deck.cli("hold", "../x", "60").returncode == 2
    assert deck.cli("hold", "aaaaaaaa", "soon").returncode == 1
    assert deck.cli("hold", "aaaaaaaa", "-5").returncode == 1
    assert not os.path.exists(os.path.join(os.path.dirname(deck.lib), "hold-aaaaaaaa"))


def test_held_session_is_working_and_not_unloaded(deck):
    a, b, c = deck.add("A", uuid=U1), deck.add("B", uuid=U2), deck.add("C", uuid=U3)
    assert deck.cli("ensure", a["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.cli("ensure", b["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.cli("hold", a["id"], "3600").returncode == 0
    act = deck.active()
    assert act["aaaaaaaa"]["working"] is True and act["bbbbbbbb"]["working"] is False
    deck.set_last_used(a["id"], 100)                           # A oldest, but held
    deck.set_last_used(b["id"], 200)
    assert deck.cli("ensure", c["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.has("cs-aaaaaaaa") and not deck.has("cs-bbbbbbbb")


def test_expired_hold_does_not_protect(deck):
    a, b, c = deck.add("A", uuid=U1), deck.add("B", uuid=U2), deck.add("C", uuid=U3)
    assert deck.cli("ensure", a["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert deck.cli("ensure", b["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    library.set_hold(a["id"], int(time.time()) - 1, lib_file=deck.lib)
    deck.set_last_used(a["id"], 100)
    deck.set_last_used(b["id"], 200)
    assert deck.cli("ensure", c["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    assert not deck.has("cs-aaaaaaaa") and deck.has("cs-bbbbbbbb")


def test_working_window_defaults_to_30_minutes(monkeypatch):
    monkeypatch.delenv("AGENTDECK_WORKING_SECONDS", raising=False)
    assert _mod().WORKING_SECONDS == 1800


# ── eviction order: max(last_used, last_output) ────────────────────────────
def test_make_room_orders_by_the_later_of_use_and_output(deck, monkeypatch):
    for k in ("AGENTDECK_LIBRARY", "AGENTDECK_TMUX_SOCKET"):
        monkeypatch.setenv(k, deck.env[k])
    m = _mod()
    monkeypatch.setattr(m.library, "LIB_FILE", deck.lib)
    deck.add("A", uuid=U1, last_used=10)                       # used long ago, printed now
    deck.add("B", uuid=U2, last_used=100)                      # used later, silent since
    monkeypatch.setattr(m, "live_sessions", lambda: [
        dict(id="aaaaaaaa", attached=False, working=False, last_output=500, pane_pid=None),
        dict(id="bbbbbbbb", attached=False, working=False, last_output=50, pane_pid=None)])
    killed = []
    monkeypatch.setattr(m, "_tmux", lambda *a: killed.append(a) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    assert m._make_room(2) is True
    assert killed == [("kill-session", "-t", "=cs-bbbbbbbb")]


# ── one conversation, one claude ───────────────────────────────────────────
@pytest.mark.parametrize("flag", ["--resume", "--session-id"])
def test_ensure_refuses_exit_4_when_the_uuid_already_runs_elsewhere(deck, flag):
    e = deck.add("A", uuid=U1)
    p = subprocess.Popen(outside_claude_argv(flag, U1), env=deck.env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deck.clients.append(p)
    time.sleep(0.3)
    r = deck.cli("ensure", e["id"])
    assert r.returncode == 4, (r.stdout, r.stderr)
    assert r.stdout == ""
    assert str(p.pid) in r.stderr
    assert not deck.has("cs-aaaaaaaa")
    assert deck.calls() == []


def test_exit_4_names_the_tmux_session_it_runs_in(deck):
    e = deck.add("A", uuid=U1)
    cmd = " ".join(__import__("shlex").quote(a) for a in outside_claude_argv("--resume", U1))
    deck.tmux("new-session", "-d", "-s", "other-work", cmd)
    time.sleep(0.4)
    r = deck.cli("ensure", e["id"])
    assert r.returncode == 4 and "other-work" in r.stderr, r.stderr


def test_uuid_inside_a_non_claude_command_line_is_not_a_second_claude(deck):
    # the tmux server's own cmdline carries `send-keys ... claude --resume <uuid>`
    e = deck.add("A", uuid=U1)
    p = subprocess.Popen(["bash", "-c", f"sleep 600; : claude --resume {U1}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deck.clients.append(p)
    time.sleep(0.2)
    r = deck.cli("ensure", e["id"])
    assert r.returncode == 0, r.stderr


def test_claude_inside_its_own_session_is_not_elsewhere(deck, monkeypatch):
    e = deck.add("A", uuid=U1)
    assert deck.cli("ensure", e["id"]).returncode == 0
    wait_for(deck.calls)
    monkeypatch.setenv("AGENTDECK_TMUX_SOCKET", deck.socket)
    m = _mod()
    found = wait_for(lambda: m.claude_processes(U1))
    assert [s for _, s in found] == ["cs-aaaaaaaa"]
    assert m.claude_elsewhere(U1, "cs-aaaaaaaa") == []


def _procs_with_home(home):
    """pids whose environment has HOME=<home> (the deck's fake claudes and their children)."""
    want = f"HOME={home}".encode()
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/environ", "rb") as f:
                if want in f.read().split(b"\0"):
                    out.append(int(d))
        except OSError:
            pass
    return out


def test_deck_close_leaves_no_fake_claude_behind(tmp_path):
    """A leftover fake `claude --session-id <uuid>` from one test made the next test's
    ensure answer 'already running elsewhere' (exit 4). close() must kill every
    process the deck started, background tasks included."""
    d = Deck(tmp_path)
    try:
        (d.home / "bg-aaaaaaaa").write_text("")
        e = d.add("A", uuid=U1)
        assert d.cli("ensure", e["id"]).returncode == 0
        f = d.add("B", uuid=U3)
        assert d.cli("ensure", f["id"]).returncode == 0
        wait_for(lambda: len(_procs_with_home(d.home)) >= 3)
    finally:
        d.close()
    wait_for(lambda: not _procs_with_home(d.home), timeout=3)
    assert _procs_with_home(d.home) == []


# ── the pane starts WITH claude's command (no typed line) ────────────────────
def test_start_passes_the_command_to_new_session_and_types_nothing(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m.library, "LIB_FILE", str(tmp_path / "reg" / "library.json"))
    calls = []

    class R:
        returncode = 0; stdout = ""; stderr = ""
    monkeypatch.setattr(m, "_tmux", lambda *a, cwd=None: calls.append(a) or R())
    e = {"id": "aaaaaaaa", "uuid": U1, "cwd": "/tmp"}
    m._start(e, ["claude", "--session-id", U1])
    assert not [c for c in calls if c and c[0] == "send-keys"], calls   # the long line isn't typed
    new = [c for c in calls if c and c[0] == "new-session"]
    assert len(new) == 1
    # tmux runs the launcher with the id; the launcher (a login + interactive bash's
    # environment, test_neutral_server_argv) execs claude with the prepared arguments
    assert new[0][-2:] == (m.LAUNCHER, "aaaaaaaa"), new[0]
    assert (tmp_path / "reg" / "launch" / "aaaaaaaa").read_bytes() == (
        b"claude\0--session-id\0" + U1.encode() + b"\0")


def test_ensure_pane_start_command_is_claudes(deck):
    e = deck.add("A", uuid=U1)
    assert deck.cli("ensure", e["id"]).returncode == 0
    wait_for(deck.calls)
    r = deck.tmux("list-panes", "-a", "-F",
                  "#{session_name}\t#{pane_start_command}\t#{pane_pid}")
    line = [l.split("\t") for l in r.stdout.splitlines() if l.startswith("cs-aaaaaaaa\t")]
    assert line, r.stdout
    _, start, pid = line[0]
    assert "agentdeck-pane" in start and "aaaaaaaa" in start, start   # what tmux was handed
    with open(f"/proc/{pid}/cmdline", "rb") as f:                      # what runs: claude itself
        argv = f.read().split(b"\0")
    assert argv[0] == b"claude" and U1.encode() in argv, argv


# ── one number per terminal (convo_sync): ensure / active follow the conversation ──
# Claude writes ~/.claude/sessions/<pid>.json (here: the deck's HOME) with the
# conversation live in the process. After the consent relaunch, /clear or /resume
# it names another conversation than the terminal's number; ensure and active
# sync first, so the terminal carries the live conversation's number.
def pane_pid(deck, name):
    r = deck.tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}")
    for line in r.stdout.splitlines():
        n, _, pid = line.partition("\t")
        if n == name and pid.isdigit():
            return int(pid)
    return None


def live_file(deck, pid, u):
    """Claude's pid file: process `pid` runs conversation `u` now."""
    with open(f"/proc/{pid}/stat") as f:
        start = f.read().rsplit(")", 1)[1].split()[19]
    d = deck.home / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": u, "procStart": start, "kind": "interactive",
         "entrypoint": "cli", "tmux": "cs-stale:@0.%0", "cwd": str(deck.work)}))


def with_messages(deck, e):
    p = deck.transcript(e)
    p.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    return p


def running(deck, sid):
    """cs-<sid> is up and its pane runs the (fake) claude; returns the pane pid."""
    pid = wait_for(lambda: pane_pid(deck, "cs-" + sid))
    assert pid and wait_for(lambda: open(f"/proc/{pid}/cmdline", "rb").read().startswith(b"claude"))
    return pid


def test_ensure_syncs_first_and_starts_no_second_claude(deck):
    e = deck.add("Deploy", uuid=U1)
    assert deck.cli("ensure", e["id"]).returncode == 0
    live_file(deck, running(deck, "aaaaaaaa"), U2)        # consent relaunch: now U2
    r = deck.cli("ensure", "aaaaaaaa")                    # an old link to the terminal
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "cs-bbbbbbbb"
    assert deck.has("cs-bbbbbbbb") and not deck.has("cs-aaaaaaaa")
    assert len(deck.calls()) == 1                          # no second claude
    L = library.load(deck.lib)
    assert [x["id"] for x in L["sessions"]] == ["bbbbbbbb"]
    assert L["sessions"][0]["aliases"] == ["aaaaaaaa"] and L["sessions"][0]["name"] == "Deploy"


def test_ensure_by_alias_prints_the_renamed_session(deck):
    deck.add("Deploy", uuid=U1)
    with library.update(deck.lib) as L:
        library.switch(L, "aaaaaaaa", U2, False, now=1)
    r = deck.cli("ensure", "aaaaaaaa")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "cs-bbbbbbbb"
    wait_for(deck.calls)
    assert f"--session-id {U2}" in deck.calls()[0]
    assert deck.cli("ensure", "aaaaaaaa").stdout.strip() == "cs-bbbbbbbb"   # running: same
    assert len(deck.calls()) == 1


def test_active_lists_the_terminal_under_its_live_number(deck):
    e = deck.add("Deploy", uuid=U1)
    assert deck.cli("ensure", e["id"]).returncode == 0
    live_file(deck, running(deck, "aaaaaaaa"), U2)
    assert set(deck.active()) == {"bbbbbbbb"}


def test_ensure_exit_4_when_live_map_shows_the_uuid_outside_tmux(deck):
    # a claude /resume'd onto U1 outside AgentDeck: nothing on its command line says
    # U1, only its pid file does
    e = deck.add("A", uuid=U1)
    p = subprocess.Popen(["bash", "-c", "exec -a claude sleep 600"], env=deck.env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deck.clients.append(p)
    assert wait_for(lambda: open(f"/proc/{p.pid}/cmdline", "rb").read().startswith(b"claude"))
    live_file(deck, p.pid, U1)
    r = deck.cli("ensure", e["id"])
    assert r.returncode == 4, (r.stdout, r.stderr)
    assert str(p.pid) in r.stderr and not deck.has("cs-aaaaaaaa")


def test_earlier_conversation_opens_while_its_old_process_runs_the_new_one(deck):
    # /clear after work in cs-aaaaaaaa: its claude (still `--resume U1` on the command
    # line) now runs U2. The terminal becomes cs-bbbbbbbb; the earlier conversation
    # U1 stays in the list and opens on its own — the old process no longer runs it
    e = deck.add("Deploy", uuid=U1)
    with_messages(deck, e)
    assert deck.cli("ensure", e["id"]).returncode == 0
    old = running(deck, "aaaaaaaa")
    assert f"--resume\x00{U1}".encode() in open(f"/proc/{old}/cmdline", "rb").read()
    live_file(deck, old, U2)
    r = deck.cli("ensure", "aaaaaaaa")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "cs-aaaaaaaa"
    assert deck.has("cs-bbbbbbbb") and pane_pid(deck, "cs-bbbbbbbb") == old
    wait_for(lambda: len(deck.calls()) == 2)
    assert f"--resume {U1}" in deck.calls()[1]
    earlier = library.find(library.load(deck.lib), "aaaaaaaa")
    assert earlier["name"] == "Deploy (earlier)"
    assert library.find(library.load(deck.lib), "bbbbbbbb")["name"] == "Deploy"


def test_pane_cmd_after_switch_resumes_the_live_uuid(deck):
    deck.add("Deploy", uuid=U1)
    with library.update(deck.lib) as L:
        b = dict(library.switch(L, "aaaaaaaa", U2, False, now=1))
    deck.transcript(b)
    for asked in ("bbbbbbbb", "aaaaaaaa"):
        r = deck.cli("pane-cmd", asked)
        assert r.returncode == 0, r.stderr
        assert f"--resume {U2}" in r.stdout and U1 not in r.stdout
        assert "AGENTDECK_SESSION=bbbbbbbb" in r.stdout


def test_hold_dash_prefers_the_live_conversation(deck):
    # AGENTDECK_SESSION is the number the pane had at launch; after a switch the
    # terminal is the conversation Claude runs now (CLAUDE_CODE_SESSION_ID)
    r = deck.cli("hold", "-", "60", AGENTDECK_SESSION="aaaaaaaa", CLAUDE_CODE_SESSION_ID=U2)
    assert r.returncode == 0, r.stderr
    assert library.hold_until("bbbbbbbb", lib_file=deck.lib) > time.time()
    assert library.hold_until("aaaaaaaa", lib_file=deck.lib) is None
    r = deck.cli("hold", "-", "60", AGENTDECK_SESSION="aaaaaaaa", CLAUDE_CODE_SESSION_ID="x/../y")
    assert r.returncode == 0, r.stderr
    assert library.hold_until("aaaaaaaa", lib_file=deck.lib) > time.time()


def test_pane_is_claude_accepts_the_npm_binary_name(deck):
    # the npm package's binary is claude.exe; after the consent relaunch the pane
    # may show that name
    deck.add("A", uuid=U1)
    deck.tmux("new-session", "-d", "-s", "cs-aaaaaaaa", "bash -c 'exec -a claude.exe sleep 600'")
    assert wait_for(lambda: deck.cli("pane-is-claude", "aaaaaaaa").returncode == 0)
