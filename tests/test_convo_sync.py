"""convo_sync.py — a terminal takes the number of the conversation live in it.

The owner's rule (2026-09-26): a terminal and its conversation are ONE thing with
ONE number. Claude changes the conversation under a running terminal three ways:
the bypass-permissions consent relaunches Claude without --session-id, /clear
starts a new conversation, /resume switches to another. Claude writes
~/.claude/sessions/<pid>.json for every live process, sessionId = the
conversation live in it now (seen on a live probe: /clear in the same process
rewrote it and CLAUDE_CODE_SESSION_ID; the file's `tmux` field stayed on the
session's OLD name after rename-session). convo_sync reads those files, finds
each cs-<id> pane's Claude by the process tree, and when the pane runs another
conversation it moves the registry entry and renames the tmux session.

Isolated world per test: a private tmux server (agentdeck-test-convo-*), a temp
sessions dir (AGENTDECK_CLAUDE_SESSIONS), temp projects dir
(AGENTDECK_CLAUDE_PROJECTS) and a temp registry (AGENTDECK_LIBRARY). The "Claude"
processes are sleeps; the test writes their pid files with their real start time.
Only processes the test started are killed (by saved pid).
"""
import fcntl
import importlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid as _uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import library  # noqa: E402

UA = "a1a1a1a1-1111-4111-8111-111111111111"
UB = "b2b2b2b2-2222-4222-8222-222222222222"
UC = "c3c3c3c3-3333-4333-8333-333333333333"
A, B, C = "a1a1a1a1", "b2b2b2b2", "c3c3c3c3"


def wait_for(pred, timeout=6.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


def stat_fields(pid):
    with open(f"/proc/{pid}/stat") as f:
        return f.read().rsplit(")", 1)[1].split()


def start_time(pid):
    return stat_fields(pid)[19]


def children(pid):
    out = []
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                if int(stat_fields(p)[1]) == pid:
                    out.append(int(p))
            except (OSError, IndexError, ValueError):
                pass
    return out


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.sock = f"agentdeck-test-convo-{os.getpid()}-{_uuid.uuid4().hex[:6]}"
        self.sessions = tmp_path / "sessions"
        self.sessions.mkdir()
        self.projects = tmp_path / "projects"
        self.projects.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.lib = str(tmp_path / "reg" / "library.json")
        self.procs = []
        for k, v in (("AGENTDECK_CLAUDE_SESSIONS", self.sessions),
                     ("AGENTDECK_CLAUDE_PROJECTS", self.projects),
                     ("AGENTDECK_LIBRARY", self.lib), ("AGENTDECK_TMUX_SOCKET", self.sock)):
            monkeypatch.setenv(k, str(v))
        monkeypatch.delenv("TMUX", raising=False)
        sys.modules.pop("convo_sync", None)
        self.cs = importlib.import_module("convo_sync")

    # tmux (private server only)
    def run(self, *args):
        assert self.sock.startswith("agentdeck-test-convo-")
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        return subprocess.run(["tmux", "-L", self.sock, "-f", "/dev/null", *args],
                              capture_output=True, text=True, env=env, timeout=15)

    def start(self, name, *argv):
        """A session whose pane runs argv directly (tmux execs a multi-word command
        without a shell), so the pane pid is argv's process."""
        argv = argv or ("sleep", "600")
        r = self.run("new-session", "-d", "-s", name, "-x", "80", "-y", "24", *argv)
        assert r.returncode == 0, r.stderr
        return wait_for(lambda: self.pane_pid(name))

    def pane_pid(self, name):
        r = self.run("list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}")
        for line in r.stdout.splitlines():
            n, _, pid = line.partition("\t")
            if n == name and pid.isdigit():
                return int(pid)
        return None

    def names(self):
        r = self.run("list-sessions", "-F", "#{session_name}")
        return sorted(r.stdout.split()) if r.returncode == 0 else []

    def attached(self, name):
        r = self.run("list-sessions", "-F", "#{session_name}\t#{session_attached}")
        return any(l == f"{name}\t1" for l in r.stdout.splitlines())

    def outside(self, argv=("sleep", "600")):
        p = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p.pid

    # registry / transcripts / pid files
    def add(self, u, name="Deploy", **extra):
        with library.update(self.lib) as L:
            e = library.create(L, name, cwd=str(self.work), now=100, uuid=u)
            e.update(extra)

    def reg(self):
        return library.load(self.lib)

    def entry(self, sid):
        return library.find(self.reg(), sid)

    def transcript(self, u, prompt=True):
        p = Path(library.transcript_file(str(self.projects), str(self.work), u))
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = ({"type": "user", "message": {"role": "user", "content": "deploy it"}}
               if prompt else {"type": "file-history-snapshot"})
        p.write_text(json.dumps(rec) + "\n")
        return p

    def pidfile(self, pid, u, tmux="cs-00000000:@0.%0", start=True, **extra):
        d = {"pid": pid, "sessionId": u, "cwd": str(self.work), "startedAt": 1,
             "kind": "interactive", "entrypoint": "cli", "tmux": tmux, "version": "2.1.283"}
        if start is True:
            d["procStart"] = start_time(pid)
        elif start is not None:
            d["procStart"] = start
        d.update(extra)
        (self.sessions / f"{pid}.json").write_text(json.dumps(d))

    def sync(self, **kw):
        kw.setdefault("run", self.run)
        kw.setdefault("lib_file", self.lib)
        return self.cs.sync(**kw)

    def close(self):
        self.run("kill-server")
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)
        sock_file = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / self.sock
        if sock_file.is_socket():
            sock_file.unlink()


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    try:
        yield world
    finally:
        world.close()


# ── which Claude processes are live ─────────────────────────────────────────
def test_live_files_skip_dead_reused_pid_sdk_and_garbage(w):
    live = w.outside()
    w.pidfile(live, UA)
    reused = w.outside()
    w.pidfile(reused, UB, start="1")                    # the pid is someone else now
    sdk = w.outside()
    w.pidfile(sdk, UC, entrypoint="sdk-cli")            # `claude -p`: not a terminal
    bg = w.outside()
    w.pidfile(bg, UC, kind="bg")
    dead = subprocess.Popen(["true"])
    dead.wait()
    w.pidfile(dead.pid, UC, start="12345")
    (w.sessions / "999999.json").write_text("{not json")
    (w.sessions / "999998.json").write_text("[1, 2]")
    (w.sessions / "notes.json").write_text(json.dumps({"pid": live, "sessionId": UC}))
    other = w.outside()                                  # file name and pid disagree
    (w.sessions / f"{other}.json").write_text(json.dumps(
        {"pid": live, "sessionId": UC, "kind": "interactive", "entrypoint": "cli",
         "procStart": start_time(live)}))
    bad_uuid = w.outside()
    w.pidfile(bad_uuid, "not-a-uuid")
    # no procStart: only a process that is claude by its argv[0] counts
    plain = w.outside()
    w.pidfile(plain, UC, start=None)
    named = w.outside(["bash", "-c", "exec -a claude.exe sleep 600"])
    wait_for(lambda: open(f"/proc/{named}/cmdline", "rb").read().startswith(b"claude.exe"))
    w.pidfile(named, UB, start=None)

    got = sorted((f["pid"], f["uuid"]) for f in w.cs.live_files())
    assert got == sorted([(live, UA), (named, UB)])
    assert all(isinstance(f["start"], int) for f in w.cs.live_files())


def test_live_files_of_a_missing_dir_is_empty(w, tmp_path):
    assert w.cs.live_files(str(tmp_path / "nope")) == []


# ── which pane a Claude belongs to ──────────────────────────────────────────
def test_pane_found_by_process_tree_not_by_tmux_field(w):
    pane = w.start("cs-" + A, "bash", "-c", "sleep 600 & wait")
    kid = wait_for(lambda: children(pane))[0]
    w.pidfile(kid, UB, tmux="cs-somethingelse:@9.%9")    # stale / wrong tmux field
    w.start("cs-" + C)
    stray = w.outside()                                  # not in any pane
    w.pidfile(stray, UA, tmux=f"cs-{C}:@0.%0")           # its file claims cs-C
    assert w.cs.pane_conversations(w.run) == {"cs-" + A: UB}


def test_two_files_on_one_pane_newest_start_wins(w, capfd):
    # the consent relaunch as a child: the parent's file names the old conversation,
    # the child (started later) the live one
    pane = w.start("cs-" + A, "bash", "-c", "sleep 0.3; sleep 600 & wait")
    kid = wait_for(lambda: children(pane) and [
        c for c in children(pane) if b"600" in open(f"/proc/{c}/cmdline", "rb").read()])[0]
    w.pidfile(pane, UA)
    w.pidfile(kid, UB)
    assert int(start_time(kid)) > int(start_time(pane))
    assert w.cs.pane_conversations(w.run) == {"cs-" + A: UB}
    assert "cs-" + A in capfd.readouterr().err                # logged: two Claudes, one pane


def test_a_claude_on_its_own_terminal_inside_a_pane_does_not_count(w):
    # e.g. `script -c claude` run by the pane's Claude: a descendant, but on its own
    # pty — not the conversation the terminal shows
    pane = w.start("cs-" + A, "bash", "-c", "sleep 0.3; script -qfc 'sleep 600' /dev/null & wait")
    w.pidfile(pane, UA)

    def nested():
        todo = list(children(pane))
        while todo:
            p = todo.pop()
            try:
                if open(f"/proc/{p}/cmdline", "rb").read() == b"sleep\x00600\x00":
                    return p
            except OSError:
                continue
            todo.extend(children(p))
    far = wait_for(nested)
    assert far and stat_fields(far)[4] != stat_fields(pane)[4]      # another tty
    w.pidfile(far, UB)
    assert w.cs.pane_conversations(w.run) == {"cs-" + A: UA}


def _pane_pids(w, name):
    r = w.run("list-panes", "-s", "-t", "=" + name, "-F", "#{pane_id}\t#{pane_pid}")
    return [int(l.split("\t")[1]) for l in r.stdout.splitlines()]


@pytest.mark.parametrize("how", ["split-window", "new-window"])
def test_a_claude_in_another_pane_of_the_session_is_not_the_terminals(w, how):
    # review 2026-09-26: someone (the owner, or the pane's Claude through its Bash
    # tool — $TMUX points at cs-A) opens a second pane in the terminal and starts
    # another interactive Claude there. It is started later, on its own pane's tty,
    # and its parent chain ends at a pane of cs-A — but the terminal's conversation
    # is the Claude in the pane AgentDeck started, not this one.
    w.add(UA)
    w.transcript(UA)
    first = w.start("cs-" + A)
    w.pidfile(first, UA)
    time.sleep(0.05)                                     # a later start (clock ticks)
    target = f"=cs-{A}:" if how == "new-window" else f"=cs-{A}:0"
    r = w.run(how, "-d", "-t", target, "sleep", "600")
    assert r.returncode == 0, r.stderr
    other = wait_for(lambda: [p for p in _pane_pids(w, "cs-" + A) if p != first])[0]
    w.pidfile(other, UC)
    assert int(start_time(other)) > int(start_time(first))
    assert w.cs.pane_conversations(w.run) == {"cs-" + A: UA}
    assert w.sync() == []
    assert w.names() == ["cs-" + A]
    assert [e["id"] for e in w.reg()["sessions"]] == [A]


# ── sync ────────────────────────────────────────────────────────────────────
def test_consent_case_renames_and_keeps_the_client_attached(w):
    w.add(UA, pos=1)
    pane = w.start("cs-" + A)
    w.pidfile(pane, UB, tmux=f"cs-{A}:@0.%0")
    w.start("viewer", "env", "-u", "TMUX", "tmux", "-L", w.sock, "attach", "-t", f"=cs-{A}")
    assert wait_for(lambda: w.attached("cs-" + A))
    library.set_hold(A, 4_000_000_000, lib_file=w.lib)

    assert w.sync() == [(A, B)]
    assert w.names() == sorted(["cs-" + B, "viewer"])
    assert w.attached("cs-" + B)                        # the tab stays on the terminal
    assert w.pane_pid("cs-" + B) == pane                # same process, untouched
    L = w.reg()
    assert [e["id"] for e in L["sessions"]] == [B]
    e = L["sessions"][0]
    assert e["uuid"] == UB and e["aliases"] == [A] and e["prev_id"] == A
    assert e["name"] == "Deploy" and e["pos"] == 1
    assert library.hold_until(B, lib_file=w.lib) == 4_000_000_000
    assert library.hold_until(A, lib_file=w.lib) is None
    assert w.sync() == []                               # settled


def test_after_clear_old_conversation_stays_as_an_unloaded_row(w):
    w.add(UA, pos=0)
    w.transcript(UA)
    pane = w.start("cs-" + A)
    w.pidfile(pane, UB)
    assert w.sync() == [(A, B)]
    assert w.names() == ["cs-" + B]
    b, a = w.entry(B), w.entry(A)
    assert b["uuid"] == UB and b["name"] == "Deploy" and b["pos"] == 0 and b["prev_id"] == A
    assert a["uuid"] == UA and a["name"] == "Deploy (earlier)" and "pos" not in a
    assert "aliases" not in b


def test_clear_after_a_huge_first_prompt_keeps_the_old_conversation(w):
    # review 2026-09-26: a first prompt longer than the transcript scan (4 MB)
    w.add(UA)
    p = Path(library.transcript_file(str(w.projects), str(w.work), UA))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"type": "user", "message": {
        "role": "user", "content": "x" * (library.TRANSCRIPT_SCAN_MAX + 1024)}}) + "\n")
    w.pidfile(w.start("cs-" + A), UB)
    assert w.sync() == [(A, B)]
    a = w.entry(A)
    assert a is not None and a["uuid"] == UA and a["name"] == "Deploy (earlier)"
    assert w.entry(B)["prev_id"] == A


def test_transcript_found_when_the_entry_folder_differs(w, tmp_path):
    # the pane may have started in another folder than the entry says (a folder that
    # is gone falls back to the work dir): the transcript is looked up by its uuid
    w.add(UA)
    p = Path(library.transcript_file(str(w.projects), str(tmp_path / "elsewhere"), UA))
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    w.pidfile(w.start("cs-" + A), UB)
    assert w.sync() == [(A, B)]
    assert w.entry(A)["name"] == "Deploy (earlier)"


def test_resume_of_a_listed_conversation_brings_it_back(w):
    w.add(UA, name="Scratch")
    w.add(UB, name="Taxes", archived=True)
    w.pidfile(w.start("cs-" + A), UB)
    assert w.sync() == [(A, B)]
    assert w.names() == ["cs-" + B]
    assert [e["id"] for e in w.reg()["sessions"]] == [B]
    b = w.entry(B)
    assert b["name"] == "Taxes" and b["archived"] is False and b["aliases"] == [A]


def test_no_switch_writes_nothing(w):
    w.add(UA)
    w.pidfile(w.start("cs-" + A), UA)
    before = os.stat(w.lib).st_mtime_ns
    assert w.sync() == []
    assert os.stat(w.lib).st_mtime_ns == before
    assert not os.path.exists(w.lib + ".bak")
    assert not os.path.exists(w.lib + ".ensure.lock")   # no lock taken either


def test_conversation_open_in_two_terminals_is_refused_and_logged(w, capfd):
    # /resume, inside terminal A, of the conversation terminal B has open
    w.add(UA)
    w.add(UB)
    w.pidfile(w.start("cs-" + A), UB)
    w.pidfile(w.start("cs-" + B), UB)
    before = json.dumps(w.reg(), sort_keys=True)
    assert w.sync() == []
    assert w.names() == sorted(["cs-" + A, "cs-" + B])        # nothing killed or renamed
    assert json.dumps(w.reg(), sort_keys=True) == before
    err = capfd.readouterr().err
    assert B in err and "cs-" + A in err and "cs-" + B in err
    assert w.sync() == []
    assert capfd.readouterr().err == ""                       # logged once per pair


def test_registry_already_switched_retries_only_the_tmux_rename(w):
    w.add(UA)
    with library.update(w.lib) as L:
        library.switch(L, A, UB, False, now=500)
    before = os.stat(w.lib).st_mtime_ns
    w.pidfile(w.start("cs-" + A), UB)
    assert w.sync() == [(A, B)]
    assert w.names() == ["cs-" + B]
    assert os.stat(w.lib).st_mtime_ns == before


def test_failed_rename_is_retried_by_the_next_sync(w, capfd):
    w.add(UA)
    w.pidfile(w.start("cs-" + A), UB)

    def no_rename(*args):
        if args and args[0] == "rename-session":
            return subprocess.CompletedProcess(args, 1, "", "refused")
        return w.run(*args)
    w.sync(run=no_rename)
    assert w.names() == ["cs-" + A] and w.entry(B) is not None
    assert "rename" in capfd.readouterr().err
    assert w.sync() == [(A, B)]
    assert w.names() == ["cs-" + B]


def test_number_taken_by_another_conversation_is_left_alone(w, capfd):
    w.add(UA)
    w.add("b2b2b2b2-9999-4999-8999-999999999999", name="Other")
    w.pidfile(w.start("cs-" + A), UB)
    before = json.dumps(w.reg(), sort_keys=True)
    assert w.sync() == []
    assert w.names() == ["cs-" + A]
    assert json.dumps(w.reg(), sort_keys=True) == before
    assert B in capfd.readouterr().err


def test_non_library_sessions_untouched(w):
    w.add(UA)
    for name in ("claude-terminal-3", "cmd-shell", "viewer", "cs-deadbeef", "cs-AAAAAAAA"):
        w.pidfile(w.start(name), UC)
    assert w.sync() == []
    assert w.names() == sorted(["claude-terminal-3", "cmd-shell", "viewer", "cs-deadbeef",
                                "cs-AAAAAAAA"])
    assert not os.path.exists(w.lib + ".bak")


def test_busy_lock_with_try_skips_this_round(w):
    w.add(UA)
    w.pidfile(w.start("cs-" + A), UB)
    with open(w.lib + ".ensure.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        assert w.sync(lock="try") == []
        assert w.names() == ["cs-" + A]
    assert w.sync(lock="try") == [(A, B)]


def test_no_tmux_server_is_nothing_to_do(w):
    w.add(UA)
    assert w.sync() == []


def test_nothing_pre_accepts_bypass_consent():
    # the owner has not decided to skip Claude's bypass-permissions consent; the
    # relaunch it causes is handled by convo_sync instead (rule 6)
    code = re.compile(r"\.(py|sh|js|json|html|ya?ml|conf)$|^(Dockerfile|Caddyfile)$")
    hits = []
    for p in REPO.rglob("*"):
        rel = p.relative_to(REPO)
        if (not p.is_file() or rel.parts[0] in (".git", "tests", "node_modules")
                or "__pycache__" in rel.parts):
            continue
        if not (code.search(p.name) or rel.parts[0] == "bin"):
            continue
        if "skipDangerousModePermissionPrompt" in p.read_text(errors="replace"):
            hits.append(str(rel))
    assert hits == []
