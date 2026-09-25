"""migrate_library.py — moving the numbered slots (.sessions/agent-N.id) into
the session library without touching a running Claude.

Owner decision (2026-09-24): running legacy terminals are NOT renamed or
restarted; they stay `claude-terminal-N` until they are unloaded. Each
migrated slot's launch-claude-N.sh becomes a shim: legacy session running ->
attach to it as before; otherwise -> open-session.sh <id>. Because the legacy
claude keeps the same uuid, `library_cli ensure <id>` must refuse (exit 4)
while it runs — never a second claude on one transcript.

Isolation: every test works on a temp COPY of the repo (launch scripts, web/,
.sessions/, the library modules), a temp registry (AGENTDECK_LIBRARY), a temp
HOME with a fake `claude` on PATH, and a private tmux socket
(AGENTDECK_TMUX_SOCKET=agentdeck-test-cli-*). The real scripts, registry,
dashboard and the default tmux server are never touched.
"""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from tests.test_library_cli import Deck, library, wait_for

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# synthetic uuids: a real one may be running on this machine right now (then
# ensure rightly refuses with exit 4 and the test would measure the machine)

U1 = "1a1a1a1a-a508-4c54-8225-08b2ad8887da"     # slot 1 (session "claude-terminal")
U2 = "2b2b2b2b-a4d9-4494-96ae-8bbffa94cb1e"     # slot 2
U3 = "3c3c3c3c-632a-4c8b-a485-c2d769bc0fb6"     # slot 3
U10 = "4d4d4d4d-d3fa-469d-ba1d-50c63316ac47"    # slot 10: no transcript
U4 = "5e5e5e5e-c9f9-41cc-b06c-2ccb34cf4613"     # slot 4: transcript < 10 KB

AGENTS = {"1": {"project": "Terminal"}, "2": {"project": "app - PIPE"},
          "3": {"project": "Instagram"}, "_order": ["2", "1", "3"]}

MODULES = ["library.py", "library_cli.py", "idle_reaper.py", "open-session.sh"]
SCRIPTS = {1: "launch-claude.sh", 2: "launch-claude-2.sh", 3: "launch-claude-3.sh",
           4: "launch-claude-4.sh", 10: "launch-claude-10.sh"}


def legacy_launch_script(path, n):
    """A numbered-slot launch script as the pre-library releases shipped it."""
    session = "claude-terminal" if n == 1 else f"claude-terminal-{n}"
    path.write_text(f"""#!/bin/bash
SESSION="{session}"
AGENT_ID="{n}"
if ! python3 "$(dirname "${{BASH_SOURCE[0]:-$0}}")/_order_gate.py" "$AGENT_ID" >/dev/null 2>&1; then
  echo "not in order"; exit 0
fi
tmux has-session -t "=$SESSION" 2>/dev/null || tmux new-session -d -s "$SESSION" claude
exec tmux attach-session -t "=$SESSION"
""")
    path.chmod(0o755)


class Mig(Deck):
    """Deck + a temp copy of the repo with slots 1,2,3 (real transcripts),
    4 (tiny transcript) and 10 (no transcript)."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.repo = tmp_path / "repo"
        (self.repo / ".sessions").mkdir(parents=True)
        (self.repo / "web").mkdir()
        for f in MODULES:
            shutil.copy2(os.path.join(REPO, f), self.repo / f)
        # an old install's numbered launch scripts (no longer shipped in the repo)
        for n, f in SCRIPTS.items():
            legacy_launch_script(self.repo / f, n)
        (self.repo / "web" / "index.html").write_text("<html>LEGACY</html>")
        (self.repo / "web" / "index-lib.html").write_text("<html>LIBRARY</html>")
        (self.repo / "agents.json").write_text(json.dumps(AGENTS))
        self.uuids = {1: U1, 2: U2, 3: U3, 4: U4, 10: U10}
        for n, u in self.uuids.items():
            (self.repo / ".sessions" / f"agent-{n}.id").write_text(u + "\n")
        self.env["AGENTDECK_WORKDIR"] = str(self.work)
        self.mtimes = {}
        for n, size in ((1, 20_000), (2, 50_000), (3, 12_000), (4, 900)):
            p = self.transcript_for(self.uuids[n], size)
            t = 1_700_000_000 + n * 1000
            os.utime(p, (t, t))
            self.mtimes[n] = t

    def transcript_for(self, u, size):
        slug = "".join(c if c.isalnum() and c.isascii() else "-" for c in str(self.work))
        p = self.home / ".claude" / "projects" / slug / f"{u}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x" * size)
        return p

    def migrate(self, *args, **extra):
        env = dict(self.env, **extra)
        return subprocess.run([sys.executable, os.path.join(REPO, "migrate_library.py"),
                               "--repo", str(self.repo), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def reg(self):
        return library.load(self.lib)

    def script(self, n):
        return self.repo / SCRIPTS[n]

    def legacy(self, name, u=None):
        """A running legacy terminal on the private socket (fake claude on U)."""
        cmd = f"claude --resume {u}" if u else "sleep 600"
        r = self.tmux("new-session", "-d", "-s", name, "-c", str(self.work), cmd)
        assert r.returncode == 0, r.stderr

    def snapshot(self):
        out = {}
        for root, _, files in os.walk(self.repo):
            for f in files:
                p = os.path.join(root, f)
                if "__pycache__" in p:
                    continue
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, self.repo)] = fh.read()
        return out


@pytest.fixture
def mig(tmp_path):
    m = Mig(tmp_path)
    yield m
    m.close()


def sid(u):
    return library.id_from_uuid(u)


# ── dry run ────────────────────────────────────────────────────────────────
def test_dry_run_is_the_default_and_changes_nothing(mig):
    before = mig.snapshot()
    r = mig.migrate()
    assert r.returncode == 0, r.stderr
    assert not os.path.exists(mig.lib)
    assert mig.snapshot() == before
    assert not mig.server_up()


def test_dry_run_table_lists_every_slot_with_id_size_date_name_action(mig):
    r = mig.migrate("--dry-run")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for n in (1, 2, 3):
        line = next(l for l in out.splitlines() if l.split() and l.split()[0] == str(n))
        assert sid(mig.uuids[n]) in line
        assert AGENTS[str(n)]["project"] in line
        assert time.strftime("%Y-%m-%d", time.localtime(mig.mtimes[n])) in line
        assert "в реестр" in line
    assert "20 КБ" in out and "49 КБ" in out


def test_dry_run_skips_missing_and_tiny_transcripts(mig):
    out = mig.migrate().stdout
    l10 = next(l for l in out.splitlines() if l.split() and l.split()[0] == "10")
    l4 = next(l for l in out.splitlines() if l.split() and l.split()[0] == "4")
    assert "пропуск" in l10 and "нет переписки" in l10
    assert "пропуск" in l4 and "< 10 КБ" in l4


def test_dry_run_reports_a_running_legacy_terminal_as_staying(mig):
    mig.legacy("claude-terminal-2")
    mig.legacy("claude-terminal")                  # slot 1's legacy name has no -1
    out = mig.migrate().stdout
    l2 = next(l for l in out.splitlines() if l.split() and l.split()[0] == "2")
    l1 = next(l for l in out.splitlines() if l.split() and l.split()[0] == "1")
    assert "работает — остаётся claude-terminal-2 до выгрузки" in l2
    assert "работает — остаётся claude-terminal до выгрузки" in l1
    l3 = next(l for l in out.splitlines() if l.split() and l.split()[0] == "3")
    assert "работает" not in l3


# ── apply: registry ────────────────────────────────────────────────────────
def test_apply_writes_migrated_entries_with_transcript_mtime_as_last_used(mig):
    r = mig.migrate("--apply")
    assert r.returncode == 0, r.stderr
    lib = mig.reg()
    got = {e["id"]: e for e in lib["sessions"]}
    assert set(got) == {sid(U1), sid(U2), sid(U3)}
    e2 = got[sid(U2)]
    assert e2["uuid"] == U2 and e2["name"] == "app - PIPE" and e2["cwd"] == str(mig.work)
    assert e2["last_used"] == mig.mtimes[2] and e2["legacy_slot"] == 2
    assert not e2["archived"]


def test_apply_merges_into_an_existing_registry_without_overwriting(mig):
    mig.add("моя тема", uuid="aaaaaaaa-1111-4111-8111-111111111111", cwd=str(mig.work))
    mig.add("переименована", uuid=U2, cwd=str(mig.work))
    assert mig.migrate("--apply").returncode == 0
    got = {e["id"]: e for e in mig.reg()["sessions"]}
    assert got["aaaaaaaa"]["name"] == "моя тема"
    assert got[sid(U2)]["name"] == "переименована"       # existing entry wins
    assert sid(U1) in got and sid(U3) in got


def test_apply_twice_changes_nothing(mig):
    assert mig.migrate("--apply").returncode == 0
    before = mig.snapshot()
    reg_before = open(mig.lib, "rb").read()
    bak = os.path.exists(mig.lib + ".bak") and open(mig.lib + ".bak", "rb").read()
    r = mig.migrate("--apply")
    assert r.returncode == 0, r.stderr
    assert open(mig.lib, "rb").read() == reg_before
    assert (os.path.exists(mig.lib + ".bak") and open(mig.lib + ".bak", "rb").read()) == bak
    assert mig.snapshot() == before


def test_apply_refuses_a_corrupt_registry_and_changes_nothing(mig):
    os.makedirs(os.path.dirname(mig.lib), exist_ok=True)
    open(mig.lib, "w").write("{broken")
    before = mig.snapshot()
    r = mig.migrate("--apply")
    assert r.returncode == 1
    assert open(mig.lib).read() == "{broken"
    assert mig.snapshot() == before


# ── apply: running legacy terminals are left alone ─────────────────────────
def test_apply_does_not_rename_or_restart_a_running_legacy_terminal(mig):
    mig.legacy("claude-terminal-2", U2)
    wait_for(mig.calls)
    pids = mig.tmux("list-panes", "-a", "-F", "#{session_name} #{pane_pid}").stdout
    r = mig.migrate("--apply")
    assert r.returncode == 0, r.stderr
    assert "работает — остаётся claude-terminal-2 до выгрузки" in r.stdout
    assert mig.tmux("list-panes", "-a", "-F", "#{session_name} #{pane_pid}").stdout == pids
    assert not mig.has(f"cs-{sid(U2)}")
    assert len(mig.calls()) == 1                          # no second claude started


# ── apply: launch-script shims ─────────────────────────────────────────────
def test_apply_replaces_migrated_launch_scripts_keeping_the_originals(mig):
    orig = {n: mig.script(n).read_bytes() for n in SCRIPTS}
    assert mig.migrate("--apply").returncode == 0
    for n in (1, 2, 3):
        pre = mig.repo / (SCRIPTS[n] + ".pre-library")
        assert pre.read_bytes() == orig[n]
        shim = mig.script(n).read_text()
        assert sid(mig.uuids[n]) in shim and "open-session.sh" in shim
        assert "_order_gate" not in shim                  # the library decides now
        assert os.access(mig.script(n), os.X_OK)
    for n in (4, 10):                                     # skipped slots stay as they were
        assert mig.script(n).read_bytes() == orig[n]
        assert not (mig.repo / (SCRIPTS[n] + ".pre-library")).exists()


def test_shim_replaces_the_file_instead_of_rewriting_it_in_place(mig):
    # a running bash reads its script lazily: the old inode must stay intact
    ino = os.stat(mig.script(2)).st_ino
    assert mig.migrate("--apply").returncode == 0
    assert os.stat(mig.script(2)).st_ino != ino


def test_shim_bash_syntax_is_valid(mig):
    assert mig.migrate("--apply").returncode == 0
    for n in (1, 2, 3):
        r = subprocess.run(["bash", "-n", str(mig.script(n))], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_shim_attaches_to_the_running_legacy_session(mig):
    mig.legacy("claude-terminal-3", U3)
    wait_for(mig.calls)
    assert mig.migrate("--apply").returncode == 0
    p = subprocess.Popen(["script", "-qfc", f"bash {mig.script(3)}", "/dev/null"],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=mig.env)
    mig.clients.append(p)
    clients = wait_for(lambda: mig.tmux("list-clients", "-F", "#{client_session}").stdout.split())
    assert clients == ["claude-terminal-3"]
    assert not mig.has(f"cs-{sid(U3)}")
    assert len(mig.calls()) == 1


def test_shim_attach_matches_the_legacy_name_exactly(mig):
    # slot 1 is "claude-terminal": a running "claude-terminal-2" must not count
    mig.legacy("claude-terminal-2")
    assert mig.migrate("--apply").returncode == 0
    r = subprocess.run(["bash", str(mig.script(1))], capture_output=True, text=True,
                       env=dict(mig.env, DRY_RUN="1"), timeout=30)
    assert r.returncode == 0, r.stderr
    assert "attach =" not in r.stdout
    assert f"--resume {U1}" in r.stdout                   # open-session.sh dry run


def test_shim_opens_the_library_topic_when_no_legacy_session(mig):
    assert mig.migrate("--apply").returncode == 0
    subprocess.run(["bash", str(mig.script(2))], capture_output=True, text=True,
                   env=mig.env, timeout=30)          # no tty: attach fails, ensure ran
    assert wait_for(lambda: mig.has(f"cs-{sid(U2)}"))
    call = wait_for(mig.calls)
    assert f"--resume {U2}" in call[0]
    assert not mig.has("claude-terminal-2")


def test_shim_dry_run_reports_attach_when_legacy_runs(mig):
    mig.legacy("claude-terminal-2")
    assert mig.migrate("--apply").returncode == 0
    r = subprocess.run(["bash", str(mig.script(2))], capture_output=True, text=True,
                       env=dict(mig.env, DRY_RUN="1"), timeout=30)
    assert r.returncode == 0 and "attach =claude-terminal-2" in r.stdout


def test_apply_rewrites_a_stale_shim_but_never_the_pre_library_original(mig):
    assert mig.migrate("--apply").returncode == 0
    pre = (mig.repo / "launch-claude-2.sh.pre-library").read_bytes()
    mig.script(2).write_text("#!/bin/bash\necho stale\n")
    assert mig.migrate("--apply").returncode == 0
    assert "open-session.sh" in mig.script(2).read_text()
    assert (mig.repo / "launch-claude-2.sh.pre-library").read_bytes() == pre


# ── the legacy claude blocks a second one on the same uuid ─────────────────
def test_ensure_refuses_while_the_legacy_claude_runs_the_same_uuid(mig):
    assert mig.migrate("--apply").returncode == 0
    mig.legacy("claude-terminal-2", U2)            # `claude --resume U2` via PATH
    wait_for(mig.calls)
    time.sleep(0.3)
    r = mig.cli("ensure", sid(U2))
    assert r.returncode == 4, (r.stdout, r.stderr)
    assert "claude-terminal-2" in r.stderr
    assert not mig.has(f"cs-{sid(U2)}")
    assert len(mig.calls()) == 1


def test_ensure_refuses_a_bare_claude_process_on_path(mig):
    assert mig.migrate("--apply").returncode == 0
    p = subprocess.Popen(["claude", "--resume", U3], env=mig.env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    mig.clients.append(p)
    wait_for(mig.calls)
    time.sleep(0.3)
    r = mig.cli("ensure", sid(U3))
    assert r.returncode == 4, (r.stdout, r.stderr)
    assert str(p.pid) in r.stderr


# ── dashboard swap ─────────────────────────────────────────────────────────
def test_swap_dashboard_keeps_the_legacy_page(mig):
    r = mig.migrate("--apply", "--swap-dashboard")
    assert r.returncode == 0, r.stderr
    web = mig.repo / "web"
    assert (web / "index-legacy.html").read_text() == "<html>LEGACY</html>"
    assert (web / "index.html").read_text() == "<html>LIBRARY</html>"
    assert (web / "index-lib.html").read_text() == "<html>LIBRARY</html>"


def test_swap_dashboard_twice_does_not_clobber_the_legacy_copy(mig):
    assert mig.migrate("--apply", "--swap-dashboard").returncode == 0
    assert mig.migrate("--apply", "--swap-dashboard").returncode == 0
    assert (mig.repo / "web" / "index-legacy.html").read_text() == "<html>LEGACY</html>"


def test_rollback_dashboard_restores_the_legacy_page(mig):
    assert mig.migrate("--apply", "--swap-dashboard").returncode == 0
    r = mig.migrate("--apply", "--rollback-dashboard")
    assert r.returncode == 0, r.stderr
    web = mig.repo / "web"
    assert (web / "index.html").read_text() == "<html>LEGACY</html>"
    assert (web / "index-legacy.html").exists() and (web / "index-lib.html").exists()


def test_swap_saves_an_edited_index_before_overwriting(mig):
    assert mig.migrate("--apply", "--swap-dashboard").returncode == 0
    (mig.repo / "web" / "index.html").write_text("<html>HAND EDIT</html>")
    assert mig.migrate("--apply", "--swap-dashboard").returncode == 0
    saved = [f for f in os.listdir(mig.repo / "web") if f.startswith("index.html.pre-swap")]
    assert len(saved) == 1
    assert (mig.repo / "web" / saved[0]).read_text() == "<html>HAND EDIT</html>"


def test_dashboard_flags_without_apply_change_nothing(mig):
    before = mig.snapshot()
    assert mig.migrate("--swap-dashboard").returncode == 0
    assert mig.snapshot() == before


def test_apply_without_swap_leaves_the_dashboard(mig):
    assert mig.migrate("--apply").returncode == 0
    assert (mig.repo / "web" / "index.html").read_text() == "<html>LEGACY</html>"
    assert not (mig.repo / "web" / "index-legacy.html").exists()


def test_nothing_is_ever_deleted(mig):
    before = set(mig.snapshot())
    mig.legacy("claude-terminal-2", U2)
    assert mig.migrate("--apply", "--swap-dashboard").returncode == 0
    assert mig.migrate("--apply", "--rollback-dashboard").returncode == 0
    assert before <= set(mig.snapshot())


def test_shim_does_not_carry_the_topic_name():
    """The shims are committed to a public repo: the topic's name (private) must not
    end up in them — the 8-hex id is enough to find the topic."""
    import migrate_library as ml
    txt = ml.shim_text(3, "claude-terminal-3", "f91c3981", "Private client name")
    assert "Private client name" not in txt
    assert "f91c3981" in txt
