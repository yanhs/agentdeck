"""hooks/guard_kill.py - the signal guard must stop signals to FOREIGN processes and let
signals to your own processes through.

The incident behind it: an agent ran

    pkill -f "noconftest 2>&1 | grep"

and every live Claude terminal on the machine died at once. The pattern of pkill -f is an
extended regular expression, "|" in it means OR, and the pattern matched every process whose
command line contains " grep". The tmux server holding all terminals inherits the argv of
the client that started it, and AgentKennel starts it with `... | grep -i CLAUDE ...` inside.
The server died, and every session with it.

The rule: a signal may only go to processes in your own tree - the descendants of the
Claude process that runs the guard. Anything else is blocked with a list of what would be
hit and a hint: "kill the PID you saved with $!".

These tests NEVER run a dangerous command: the guard only gets its text, and the guard only
reads (pgrep, /proc). The victims are the tests' own `sleep` processes with a unique mark;
tmux runs only on the tests' own sockets in a temp dir, always with an explicit -S.

Without a `claude` ancestor (CI, a plain shell) the guard treats its parent - pytest - as the
root of the tree; test_tree_is_rooted_at_the_nearest_claude_ancestor fakes one.

Run (in the background - it takes a while): python3 -m pytest tests/test_guard_kill.py -q
"""
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from types import SimpleNamespace

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "guard_kill.py")
UID = os.getuid()
USER = pwd.getpwuid(UID).pw_name
INCIDENT = 'pkill -f "noconftest 2>&1 | grep"'
UNIT_SUFFIX = re.compile(r"\.(service|scope|slice)$")


def need(tool):
    return pytest.mark.skipif(shutil.which(tool) is None, reason=f"{tool} is not installed")


# --- Running the guard --------------------------------------------------------------------

def run(command, env=None, cwd=None, tool="Bash", timeout=60):
    """Run the guard the way the harness runs it: JSON on stdin.

    Returns (code, stderr). Code 2 means blocked, 0 means let through."""
    tool_input = {"command": command}
    if tool == "Monitor":
        tool_input.update(description="gk test", timeout_ms=1000)
    payload = {
        "session_id": "test-guard-kill",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "cwd": cwd or os.getcwd(),
    }
    assert os.path.exists(HOOK), HOOK
    p = subprocess.run(
        [sys.executable, HOOK], input=json.dumps(payload),
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    assert p.returncode in (0, 2), f"the guard crashed: rc={p.returncode}\n{p.stderr}"
    # A crashed interpreter can exit with 2 too - a block counts only with the guard's message.
    if p.returncode == 2:
        assert "KILL_OK=1" in p.stderr, f"code 2 without the guard's message:\n{p.stderr}"
    return p.returncode, p.stderr


def blocked(command, **kw):
    return run(command, **kw)[0] == 2


def message(command, **kw):
    rc, err = run(command, **kw)
    assert rc == 2, f"expected a block: {command!r}"
    return err


# --- Our own victims ----------------------------------------------------------------------

def _marker(tag):
    return f"zzz_gk_{tag}_{uuid.uuid4().hex[:10]}"


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def _find_by_argv0(marker, timeout=5.0):
    """Find a process by the start of its command line - by reading /proc, no signals."""
    deadline = time.time() + timeout
    want = marker.encode()
    while time.time() < deadline:
        for d in os.listdir("/proc"):
            if d.isdigit() and _read(f"/proc/{d}/cmdline").startswith(want):
                return int(d)
        time.sleep(0.05)
    raise AssertionError(f"victim {marker} did not show up")


def _ppid(pid):
    stat = _read(f"/proc/{pid}/stat").decode()
    return int(stat.rsplit(")", 1)[1].split()[1])


def _is_descendant_of_me(pid):
    me = os.getpid()
    while pid > 1:
        if pid == me:
            return True
        pid = _ppid(pid)
    return False


def _gone(pid, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        stat = _read(f"/proc/{pid}/stat").decode()
        if not stat or stat.rsplit(")", 1)[1].split()[0] == "Z":
            return True
        time.sleep(0.05)
    return False


def _spawn_outside(argv):
    """Start a process OUTSIDE our tree: setsid -f forks twice, the parent exits, and the
    process is handed to init. The victim is ours, and we remove it ourselves."""
    subprocess.run(["setsid", "-f"] + argv, check=True)


def _require_foreign(pid):
    """An orphan goes to the nearest subreaper; if that is inside our tree, the process is
    not foreign and the test means nothing here."""
    if _is_descendant_of_me(pid):
        os.kill(pid, signal.SIGKILL)       # our own process, its saved PID
        _gone(pid)
        pytest.skip("orphans are reparented into the test's own tree (a subreaper)")


@pytest.fixture
def own():
    """Our own descendant: a child of pytest with its own process group."""
    m = _marker("own")
    p = subprocess.Popen(["bash", "-c", f"exec -a {m} sleep 1000"], start_new_session=True)
    pid = _find_by_argv0(m)
    assert pid == p.pid
    yield SimpleNamespace(pid=pid, marker=m)
    p.kill()
    p.wait()


@pytest.fixture
def outside():
    """Foreign: a descendant of neither pytest nor Claude (double fork)."""
    m = _marker("out")
    _spawn_outside(["bash", "-c", f"exec -a {m} sleep 1000"])
    pid = _find_by_argv0(m)
    _require_foreign(pid)
    yield SimpleNamespace(pid=pid, marker=m)
    os.kill(pid, signal.SIGKILL)   # our own process
    _gone(pid)


@pytest.fixture
def comm_bin():
    """A copy of sleep with a unique name - for killall/pidof, which look at the name."""
    d = tempfile.mkdtemp(prefix="gk_bin_")
    name = "gk" + uuid.uuid4().hex[:11]          # 13 chars: not cut down to 15
    path = os.path.join(d, name)
    shutil.copy(shutil.which("sleep") or "/bin/sleep", path)
    yield SimpleNamespace(path=path, name=name)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def own_named(comm_bin):
    p = subprocess.Popen([comm_bin.path, "1000"])
    yield SimpleNamespace(pid=p.pid, name=comm_bin.name)
    p.kill()
    p.wait()


@pytest.fixture
def outside_named(comm_bin):
    _spawn_outside([comm_bin.path, "1000"])
    pid = None
    deadline = time.time() + 5
    while pid is None and time.time() < deadline:
        for d in os.listdir("/proc"):
            if d.isdigit() and _read(f"/proc/{d}/comm").strip() == comm_bin.name.encode():
                pid = int(d)
        time.sleep(0.05)
    assert pid, f"victim {comm_bin.name} did not show up"
    _require_foreign(pid)
    yield SimpleNamespace(pid=pid, name=comm_bin.name, path=comm_bin.path)
    os.kill(pid, signal.SIGKILL)
    _gone(pid)


@pytest.fixture
def grep_argv_victim():
    """A foreign process whose argv looks like AgentKennel's tmux server: it has " grep"."""
    m = _marker("inc")
    argv0 = f"{m} bash -lic for v in $(env | cut -d= -f1 | grep -i CLAUDE); do unset x; done"
    _spawn_outside(["bash", "-c", f"exec -a '{argv0}' sleep 1000"])
    pid = _find_by_argv0(m)
    _require_foreign(pid)
    assert b" grep" in _read(f"/proc/{pid}/cmdline")
    yield SimpleNamespace(pid=pid, marker=m)
    os.kill(pid, signal.SIGKILL)
    _gone(pid)


# --- tmux on our own sockets --------------------------------------------------------------

def _clean_env(tmpdir=None):
    """An environment without TMUX: otherwise tmux would go to the live server's socket."""
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")}
    if tmpdir:
        env["TMUX_TMPDIR"] = tmpdir
    return env


def _tmux(sock, *args, env):
    """Only an explicit -S to a socket in a temp dir - the live server is out of reach."""
    assert not sock.startswith(f"/tmp/tmux-{UID}/"), "tests never touch the live tmux dir"
    return subprocess.run(["tmux", "-S", sock, "-f", "/dev/null"] + list(args),
                          env=env, capture_output=True, text=True, timeout=20)


@pytest.fixture
def fake_tmux():
    """A fake "live" tmux: the default socket in our own TMUX_TMPDIR.

    The guard gets the same TMUX_TMPDIR - to it, this is the default socket."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    base = tempfile.mkdtemp(prefix="gkt")
    sockdir = os.path.join(base, f"tmux-{UID}")
    os.mkdir(sockdir, 0o700)
    env = _clean_env(base)
    live = os.path.join(sockdir, "default")
    r = _tmux(live, "new-session", "-d", "-s", "live", "sleep 1000", env=env)
    assert r.returncode == 0, r.stderr
    r = _tmux(live, "list-sessions", "-F", "#{pid}", env=env)
    server = int(r.stdout.split()[0])
    priv = os.path.join(sockdir, "gk_priv")
    r = _tmux(priv, "new-session", "-d", "-s", "p", "sleep 1000", env=env)
    assert r.returncode == 0, r.stderr
    yield SimpleNamespace(base=base, env=env, live=live, priv=priv, server=server)
    _tmux(priv, "kill-server", env=env)
    _tmux(live, "kill-server", env=env)
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def scoped_tmux():
    """A tmux server in its own systemd unit - to check the protection by cgroup."""
    if shutil.which("tmux") is None or shutil.which("systemd-run") is None:
        pytest.skip("tmux or systemd-run is not installed")
    unit = f"gk-test-{uuid.uuid4().hex[:10]}"
    base = tempfile.mkdtemp(prefix="gks")
    sock = os.path.join(base, "s")
    env = _clean_env(base)
    env["XDG_RUNTIME_DIR"] = f"/run/user/{UID}"
    r = subprocess.run(
        ["systemd-run", "--user", "--scope", "--quiet", "--unit", unit, "--",
         "tmux", "-S", sock, "-f", "/dev/null", "new-session", "-d", "sleep 1000"],
        env=env, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        pytest.skip(f"no user systemd: {r.stderr.strip()}")
    yield SimpleNamespace(unit=unit)
    _tmux(sock, "kill-server", env=env)
    shutil.rmtree(base, ignore_errors=True)


# ==========================================================================================
# 1. The incident
# ==========================================================================================

def test_incident_command_blocked(grep_argv_victim):
    # Exactly that command. A foreign process with " grep" in its argv exists, so: block.
    err = message(INCIDENT)
    assert "KILL_OK=1" in err and "$!" in err


def test_incident_message_names_victim_when_pattern_is_narrow(grep_argv_victim):
    err = message(f'pkill -f "noconftest 2>&1 |{grep_argv_victim.marker}"')
    assert str(grep_argv_victim.pid) in err
    assert grep_argv_victim.marker in err


# ==========================================================================================
# 2. Our own descendant - allowed
# ==========================================================================================

def test_own_kill_pid(own):
    assert not blocked(f"kill {own.pid}")


def test_own_kill_9(own):
    assert not blocked(f"kill -9 {own.pid}")


def test_own_kill_signal_name(own):
    assert not blocked(f"kill -s TERM {own.pid}")


def test_own_pkill_f(own):
    assert not blocked(f"pkill -f {own.marker}")


def test_own_pkill_anchored(own):
    assert not blocked(f"pkill -9 -f '^{own.marker}'")


def test_own_kill_pgrep_substitution(own):
    assert not blocked(f"kill $(pgrep -f {own.marker})")


def test_own_pgrep_xargs(own):
    assert not blocked(f"pgrep -f {own.marker} | xargs kill")


def test_own_process_group(own):
    # start_new_session: the victim has its own group, pgid = pid
    assert not blocked(f"kill -- -{own.pid}")


def test_own_killall(own_named):
    assert not blocked(f"killall {own_named.name}")


@need("pidof")
def test_own_pidof_xargs(own_named):
    assert not blocked(f"pidof {own_named.name} | xargs kill -9")


def test_saved_bang_pid():
    assert not blocked("sleep 1000 & kill $!")


def test_saved_bang_pid_in_variable():
    assert not blocked("sleep 5 & pid=$!\nsleep 1; kill -TERM $pid; wait $pid")


def test_jobspec():
    assert not blocked("sleep 100 & kill %1")


def test_own_process_group_zero():
    # Every Bash call has its own process group: kill 0 hits only that.
    assert not blocked("kill 0")


def test_kill_list_signals():
    assert not blocked("kill -l")


def test_pkill_children_of_this_shell():
    assert not blocked("sleep 100 & pkill -P $$ sleep")


def test_pgrep_does_not_match_itself():
    # The pattern matches the command line of the very pgrep the guard checks with -
    # the guard must exclude it, or it blocks for nothing.
    assert not blocked(f"pkill -f {_marker('nobody')}")


def test_kill_zero_signal_liveness_check_is_fine(outside):
    # kill -0 delivers nothing: it is a liveness check.
    assert not blocked(f"kill -0 {outside.pid}")


def test_no_such_pid():
    assert not blocked("kill 3999999")


def test_tree_is_rooted_at_the_nearest_claude_ancestor(own):
    # The hook runs under a shell named "claude": pytest -> "claude" (bash) -> hook. The
    # guard's tree is now that shell's descendants only, so pytest's own child is foreign.
    # `; exit $?` keeps bash from exec-ing the hook in its own place.
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": f"kill {own.pid}"},
                          "cwd": os.getcwd()})
    script = f"{shlex.quote(sys.executable)} {shlex.quote(HOOK)}; exit $?"
    p = subprocess.run(["bash", "-c", f"exec -a claude bash -c {shlex.quote(script)}"],
                       input=payload, capture_output=True, text=True, timeout=60)
    assert p.returncode == 2, p.stderr
    assert str(own.pid) in p.stderr and "KILL_OK=1" in p.stderr
    # Without the fake ancestor the same process is ours.
    assert not blocked(f"kill {own.pid}")


# ==========================================================================================
# 3. A foreign process - not allowed
# ==========================================================================================

def test_outside_kill_pid(outside):
    err = message(f"kill {outside.pid}")
    assert str(outside.pid) in err and outside.marker in err


def test_outside_kill_9(outside):
    assert blocked(f"kill -9 {outside.pid}")


def test_outside_kill_dashdash(outside):
    assert blocked(f"kill -KILL -- {outside.pid}")


def test_outside_pkill_f(outside):
    assert blocked(f"pkill -f {outside.marker}")


def test_outside_pkill_signal_first(outside):
    assert blocked(f"pkill -9 -f {outside.marker}")


def test_outside_pkill_signal_long(outside):
    assert blocked(f"pkill --signal=KILL -f {outside.marker}")
    assert blocked(f"pkill --signal KILL -f {outside.marker}")


def test_outside_pkill_echo_cluster(outside):
    assert blocked(f"pkill -ef {outside.marker}")


def test_outside_pkill_zero_is_still_a_rehearsal(outside):
    # By pattern - even with signal 0: it rehearses the same -9.
    assert blocked(f"pkill -0 -f {outside.marker}")


def test_outside_pkill_regex_alternation(outside):
    assert blocked(f"pkill -f 'zzz_nothing_here|{outside.marker}'")


def test_outside_pkill_dot_star(outside):
    assert blocked(f"pkill -f '{outside.marker[:-3]}.*'")


def test_outside_pkill_anchor(outside):
    assert blocked(f"pkill -f '^{outside.marker}'")


def test_outside_pkill_user(outside):
    # Every process of the user - the foreign victim is one of them.
    assert blocked(f"pkill -u {UID}")


def test_outside_pkill_exact_name(outside_named):
    assert blocked(f"pkill -x {outside_named.name}")


def test_outside_killall(outside_named):
    err = message(f"killall {outside_named.name}")
    assert str(outside_named.pid) in err


def test_outside_killall_signal(outside_named):
    assert blocked(f"killall -s KILL {outside_named.name}")
    assert blocked(f"killall -9 {outside_named.name}")


def test_outside_killall_regex(outside_named):
    assert blocked(f"killall -r '^{outside_named.name[:8]}'")


def test_outside_killall_path(outside_named):
    assert blocked(f"killall {outside_named.path}")


def test_outside_kill_pgrep_substitution(outside):
    assert blocked(f"kill $(pgrep -f {outside.marker})")


def test_outside_kill_backtick_pgrep(outside):
    assert blocked(f"kill -9 `pgrep -f {outside.marker}`")


def test_outside_pgrep_xargs(outside):
    assert blocked(f"pgrep -f {outside.marker} | xargs kill")
    assert blocked(f"pgrep -f {outside.marker} | xargs -r kill -9")


@need("pidof")
def test_outside_pidof_xargs(outside_named):
    assert blocked(f"pidof {outside_named.name} | xargs kill")


def test_outside_ps_grep_awk_xargs(outside):
    assert blocked(f"ps aux | grep {outside.marker} | grep -v grep | "
                   f"awk '{{print $2}}' | xargs kill")


def test_outside_for_loop(outside):
    assert blocked(f"for p in $(pgrep -f {outside.marker}); do kill $p; done")


def test_outside_while_read(outside):
    assert blocked(f"pgrep -f {outside.marker} | while read p; do kill -9 \"$p\"; done")


def test_outside_process_group(outside):
    # setsid: the victim has its own group, pgid = pid
    assert blocked(f"kill -9 -{outside.pid}")
    assert blocked(f"kill -- -{outside.pid}")


@need("fuser")
def test_outside_fuser_k(outside_named):
    # fuser -k hits everyone holding the file - a pattern too, only by file.
    assert blocked(f"fuser -k {outside_named.path}")


@need("fuser")
def test_own_fuser_k(own_named, comm_bin):
    assert not blocked(f"fuser -k {comm_bin.path}")


@need("lsof")
def test_outside_lsof_substitution(outside_named):
    assert blocked(f"kill $(lsof -t {outside_named.path})")


def test_pidfile(outside, own, tmp_path):
    out_f, own_f = tmp_path / "out.pid", tmp_path / "own.pid"
    out_f.write_text(f"{outside.pid}\n")
    own_f.write_text(f"{own.pid}\n")
    assert blocked(f"kill $(cat {out_f})")
    assert blocked(f"kill -9 $(< {out_f})")
    assert not blocked(f"kill $(cat {own_f})")


def test_kill_minus_one():
    assert blocked("kill -9 -1")
    assert blocked("kill -- -1")


def test_pkill_variable_resolved(outside, own):
    assert blocked(f"P={outside.marker}; pkill -f \"$P\"")
    assert not blocked(f"P={own.marker}; pkill -f \"$P\"")


def test_parent_of_shell_is_claude():
    # $PPID of the Bash wrapper is the Claude process itself.
    assert blocked("kill $PPID")


# ==========================================================================================
# 4. Parsing compound commands
# ==========================================================================================

@pytest.mark.parametrize("tpl", [
    "true; kill {pid}",
    "true && kill {pid}",
    "false || kill {pid}",
    "echo x | kill {pid}",
    "echo a\nkill {pid}",
    "(kill {pid})",
    "{{ kill {pid}; }}",
    "echo $(kill {pid})",
    "echo `kill {pid}`",
    "sudo kill {pid}",
    "sudo -n -u {user} kill -9 {pid}",
    "env FOO=1 kill {pid}",
    "FOO=1 kill {pid}",
    "timeout 5 kill {pid}",
    "timeout -s KILL 5 kill {pid}",
    "nohup kill {pid} &",
    "nice -n 5 kill {pid}",
    "bash -c 'kill {pid}'",
    "sh -c \"kill -TERM {pid}\"",
    "bash -lc 'cd /tmp && kill {pid}'",
    "bash <<'EOF'\nkill {pid}\nEOF",
    "/bin/kill {pid}",
    "command kill {pid}",
    "exec kill {pid}",
    "if true; then kill {pid}; fi",
    "while false; do :; done; kill {pid}",
    "x=$(kill {pid})",
    "diff <(kill {pid}) /dev/null",
    "eval 'kill {pid}'",
])
def test_compound_forms_reach_kill(outside, tpl):
    assert blocked(tpl.format(pid=outside.pid, user=USER)), tpl


@pytest.mark.parametrize("tpl", [
    "true; pkill -f {m}",
    "sudo pkill -f {m}",
    "env A=1 B=2 pkill -9 -f '{m}'",
    "timeout 10 pkill -f {m}",
    "bash -c \"pkill -f '{m}'\"",
    "sh -c 'sleep 0 && pkill -f {m}'",
])
def test_compound_forms_reach_pkill(outside, tpl):
    assert blocked(tpl.format(m=outside.marker)), tpl


# ==========================================================================================
# 5. Too dynamic - block, with the override
# ==========================================================================================

def test_eval_dynamic():
    err = message('eval "pkill -f $X"')
    assert "KILL_OK=1" in err


def test_unknown_variable_pattern():
    assert blocked('pkill -f "$SOME_PATTERN_FROM_NOWHERE"')


def test_unknown_variable_pid():
    assert blocked("kill -9 $SOMEPID")


def test_unparseable():
    assert blocked("kill \"123")


def test_xargs_into_shell(outside, own):
    # xargs puts the items into the script text: the guard does the same.
    assert blocked(f"pgrep -f {outside.marker} | xargs -I{{}} sh -c 'kill {{}}'")
    assert not blocked(f"pgrep -f {own.marker} | xargs -I{{}} sh -c 'kill {{}}'")


def test_xargs_into_shell_unknown_input():
    assert blocked("some_tool --list | xargs -I{} sh -c 'kill {}'")


# ==========================================================================================
# 6. tmux
# ==========================================================================================

def test_tmux_kill_server_on_live_default_blocked(fake_tmux):
    err = message("tmux kill-server", env=fake_tmux.env)
    assert str(fake_tmux.server) in err


@pytest.mark.parametrize("cmd", [
    "tmux kill-session -t live",
    "tmux kill-ses -t live",
    "tmux -L default kill-server",
    "tmux -f /dev/null kill-server",
    "tmux killp -t live",
    "tmux kill-window -t live:0",
    "tmux ls \\; kill-server",
    f"sudo -u {USER} -E tmux kill-server",
])
def test_tmux_live_variants_blocked(fake_tmux, cmd):
    assert blocked(cmd, env=fake_tmux.env), cmd


def test_tmux_explicit_live_socket_blocked(fake_tmux):
    assert blocked(f"tmux -S {fake_tmux.live} kill-server", env=fake_tmux.env)


def test_tmux_real_default_socket_always_protected(fake_tmux):
    # The real live socket is always protected, even when TMUX_TMPDIR points elsewhere.
    assert blocked(f"tmux -S /tmp/tmux-{UID}/default kill-server", env=fake_tmux.env)


def test_tmux_private_label_allowed(fake_tmux):
    assert not blocked("tmux -L gk_priv kill-server", env=fake_tmux.env)


def test_tmux_private_socket_allowed(fake_tmux):
    assert not blocked(f"tmux -S {fake_tmux.priv} kill-session -t p", env=fake_tmux.env)


def test_tmux_private_tmpdir_allowed(fake_tmux):
    other = tempfile.mkdtemp(prefix="gko")
    try:
        assert not blocked(f"TMUX_TMPDIR={other} tmux kill-server", env=fake_tmux.env)
        assert not blocked(f"export TMUX_TMPDIR={other}; tmux kill-server", env=fake_tmux.env)
    finally:
        shutil.rmtree(other)


def test_tmux_env_TMUX_wins_over_tmpdir(fake_tmux):
    # Inside a pane tmux takes the socket from $TMUX, not from TMUX_TMPDIR.
    env = dict(fake_tmux.env, TMUX=f"{fake_tmux.live},{fake_tmux.server},0")
    other = tempfile.mkdtemp(prefix="gko")
    try:
        assert blocked(f"TMUX_TMPDIR={other} tmux kill-server", env=env)
        assert not blocked(f"env -u TMUX TMUX_TMPDIR={other} tmux kill-server", env=env)
    finally:
        shutil.rmtree(other)


def test_tmux_plain_assignment_does_not_export():
    # Without export the variable does not reach tmux's environment - the socket stays the
    # live one (/tmp/tmux-UID/default). The guard only computes the path, never touches it.
    env = _clean_env()
    assert blocked("TMUX_TMPDIR=/tmp/elsewhere; tmux kill-server", env=env)
    assert not blocked("export TMUX_TMPDIR=/tmp/elsewhere; tmux kill-server", env=env)


@pytest.mark.parametrize("cmd", [
    "tmux ls",
    "tmux list-sessions -F '#{session_name}'",
    "tmux -L gk_priv new-session -d 'sleep 1'",
    "tmux capture-pane -p -t live",
])
def test_tmux_read_only_allowed(fake_tmux, cmd):
    assert not blocked(cmd, env=fake_tmux.env), cmd


def test_tmux_shell_command_is_inspected(fake_tmux, outside):
    # A command that tmux itself will run is a command too.
    assert blocked(f"tmux -L gk_priv new-session -d 'pkill -f {outside.marker}'",
                   env=fake_tmux.env)


# ==========================================================================================
# 7. systemctl
# ==========================================================================================

@pytest.mark.parametrize("cmd", [
    "systemctl --user stop agentdeck-sessions.service",
    "systemctl restart agentdeck-web",
    "sudo systemctl kill -s KILL agentdeck-web",
    "systemctl stop 'agentdeck-*'",
    "systemctl --user stop 'agent*'",
    "sudo systemctl isolate rescue.target",
    "sudo systemctl disable --now agentdeck-web",
])
def test_systemctl_protected_blocked(cmd):
    assert blocked(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "systemctl status agentdeck-web",
    "systemctl --user daemon-reload",
    "sudo systemctl restart nginx",
    "systemctl is-active agentdeck-web",
    "sudo systemctl disable agentdeck-web",
    "journalctl -u agentdeck-web -n 20",
])
def test_systemctl_harmless_allowed(cmd):
    assert not blocked(cmd), cmd


def test_systemctl_unit_holding_a_tmux_server(fake_tmux):
    # Every unit on the cgroup path of a tmux server is protected: stopping it stops the
    # server. The command is only shown to the guard, never run.
    units = set()
    for line in _read(f"/proc/{fake_tmux.server}/cgroup").decode().splitlines():
        units.update(c for c in line.split(":", 2)[-1].split("/") if UNIT_SUFFIX.search(c))
    if not units:
        pytest.skip("the tmux server is not inside a systemd unit (a container?)")
    for unit in sorted(units):
        assert blocked(f"sudo systemctl stop {unit}"), unit


def test_systemctl_unit_holding_tmux_by_cgroup(scoped_tmux):
    assert blocked(f"systemctl --user stop {scoped_tmux.unit}.scope")
    assert blocked(f"systemctl --user kill {scoped_tmux.unit}.scope")
    assert not blocked(f"systemctl --user stop gk-other-{scoped_tmux.unit[8:]}.scope")


# ==========================================================================================
# 8. Stay out of the way of harmless commands
# ==========================================================================================

@pytest.mark.parametrize("cmd", [
    "ls -la",
    'grep -rn "pkill -f" hooks/',
    'echo "kill -9 123"',
    'git commit -m "tmux kill-server fix"',
    "man kill",
    "find . -name '*.py' | xargs grep -l kill",
    "pidof sleep",
    "pgrep -af tmux",
    "ps aux | grep kill",
    "cat > /tmp/gk_never_written.sh <<'EOF'\npkill -f grep\nEOF",
    "python3 -c 'print(1)'  # kill it later",
    "echo $(pgrep -f zzz_gk_none_at_all)",
    "xargs --version",
    "systemctl list-units | grep tmux",
])
def test_harmless_allowed(cmd):
    assert not blocked(cmd), cmd


# ==========================================================================================
# 9. The override and odds and ends
# ==========================================================================================

def test_bypass_prefix(outside):
    assert not blocked(f"KILL_OK=1 kill {outside.pid}")


def test_non_bash_tool_ignored():
    p = subprocess.run([sys.executable, HOOK],
                       input=json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "/x"}}),
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0


def test_garbage_input_ignored():
    p = subprocess.run([sys.executable, HOOK], input="not json",
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0


def test_message_shape(outside):
    err = message(f"kill {outside.pid}")
    assert err.startswith("BLOCKED by guard_kill")
    assert str(outside.pid) in err and outside.marker in err      # what would be hit
    for part in ("Rule:", "Do this instead:", "$!", "Why:", "KILL_OK=1"):
        assert part in err, part
    assert err.isascii()


# ==========================================================================================
# 10. False blocks found by a run over real commands from logs
# ==========================================================================================

@pytest.mark.parametrize("cmd", [
    # an assignment after do/then is an assignment, not a command name
    "for u in $(systemctl list-units --no-legend | awk '{print $1}'); do "
    "n=$(systemctl show \"$u\" -p NRestarts --value); echo $u $n; done",
    "systemctl list-units --plain | awk '{print $1}' | while read u; do "
    "t=$(systemctl show \"$u\" -p Type --value 2>/dev/null); echo $t; done",
    "if true; then p=$(systemctl --user show x -p MainPID --value); echo $p; fi",
    # a command in a variable, built in the same command - on its own socket
    "for i in 1 2; do S=agentdeck-test-q$i; T=\"env -u TMUX tmux -L $S\"; "
    "$T new-session -d -s x 'cat'; $T kill-server; done",
    # $PWD is the working directory of the call
    "export TMUX_TMPDIR=$PWD/tmx && mkdir -p tmx && unset TMUX; tmux kill-server",
])
def test_real_world_false_positives(cmd):
    assert not blocked(cmd, env=_clean_env()), cmd


def test_command_in_variable_still_checked(outside):
    assert blocked(f"K=\"kill -9\"; $K {outside.pid}")
    assert blocked("for i in 1; do T='tmux'; $T kill-server; done", env=_clean_env())


def test_cd_is_followed(tmp_path, outside):
    (tmp_path / "x.pid").write_text(f"{outside.pid}\n")
    assert blocked(f"cd {tmp_path} && kill $(cat x.pid)")


@pytest.mark.parametrize("cmd", [
    # an unknown command name without signal marks is no reason to block
    "ENV=$(systemctl --user show x -p Environment --value); env $ENV timeout 600 some-tool migrate",
    # $RANDOM in the name of our own socket: it can never be the live one ("default")
    "S=agentdeck-test-r$RANDOM; T=\"env -u TMUX tmux -L $S\"; "
    "$T new-session -d -s x; $T kill-server",
    # kill -0 sends nothing, even when the PID is computed at run time
    "PID=$(systemctl --user show x -p MainPID --value); kill -0 $PID && echo alive",
])
def test_real_world_false_positives_2(cmd):
    assert not blocked(cmd, env=_clean_env()), cmd


@pytest.mark.parametrize("cmd", [
    "T=$(which tmux); $T kill-server",
    "PID=$(systemctl --user show x -p MainPID --value); kill -TERM $PID",
    "kill $RANDOM",
])
def test_dynamic_signal_still_blocked(cmd):
    assert blocked(cmd, env=_clean_env()), cmd


# ==========================================================================================
# 11. Holes found by a smoke run over odd commands
# ==========================================================================================

def test_heredoc_through_cat_into_shell(outside, own):
    assert blocked(f"cat <<'EOF' | bash\nkill {outside.pid}\nEOF")
    assert not blocked(f"cat <<'EOF' | bash\nkill {own.pid}\nEOF")


def test_tmux_if_shell_runs_tmux_command():
    assert blocked("tmux if-shell 'true' 'kill-server'", env=_clean_env())


def test_nsenter_is_unwrapped(outside):
    assert blocked(f"sudo nsenter -t 1 -a kill {outside.pid}")


# ==========================================================================================
# 12. Second review: the fast path, foreign programs, Monitor
# ==========================================================================================

# --- 12.1. The command name is not spelled literally -------------------------------------
# The fast path used to look for words in the raw text, before the shell strips quotes. The
# commands below kill a foreign process, and the guard let them through without parsing.

@pytest.mark.parametrize("tpl", [
    "k'i'll {pid}",
    "k\\ill {pid}",
    '"ki"ll {pid}',
    "ki''ll -9 {pid}",
    "ki\\\nll {pid}",                      # a line continuation inside the word
    "s'u'do k'i'll {pid}",
    "$'\\x6bill' {pid}",                   # $'...' turns \x6b into "k"
    "$'\\153ill' {pid}",
    "a=ki; b=ll; $a$b {pid}",              # the name built from variables of the same command
    "/bin/k?ll {pid}",                     # a glob in the command name
    "/usr/bin/k[i]ll {pid}",
    "$(printf '\\x6bill') {pid}",          # the name is the output of a read-only command
    "printf 'kil%s {pid}' l | bash",       # the text for the shell is built at run time
    "echo -e '\\x6bill {pid}' | sh",
])
def test_name_spelled_indirectly_still_checked(outside, tpl):
    assert blocked(tpl.format(pid=outside.pid)), tpl


@pytest.mark.parametrize("tpl", ["pk'i'll -f {m}", '"pki"ll -9 -f {m}', "p\\k\\ill -f {m}"])
def test_pkill_spelled_indirectly_still_checked(outside, tpl):
    assert blocked(tpl.format(m=outside.marker)), tpl


def test_tmux_spelled_indirectly_still_checked(fake_tmux):
    assert blocked("tm'u'x kil''l-server", env=fake_tmux.env)


@pytest.mark.parametrize("tpl", ["k'i'll {pid}", "a=ki; b=ll; $a$b {pid}", "$'\\x6bill' {pid}"])
def test_name_spelled_indirectly_own_process_allowed(own, tpl):
    assert not blocked(tpl.format(pid=own.pid)), tpl


# The new path must add no false blocks: neither for the legitimate commands from the review
# nor for everyday commands with $, quotes, globs and backslashes.
@pytest.mark.parametrize("cmd", [
    "sleep 100 & pid=$!; kill $pid",
    "sleep 100 & pid=$!; kill -0 $pid",
    "kill $(jobs -p)",
    "timeout -s KILL 10 true",
    "git log --grep=kill --oneline | head",
    "grep -rn pkill hooks/ | head",
    "cat ~/.claude/skills/x/SKILL.md",
    "tmux -L mysock_test kill-server",
    "find . -name '*.pyc' | xargs rm -f",
    "git ls-files | xargs grep -n pkill",
    "echo 'kill -9 1' > /tmp/gk_never_written.txt",
    "docker ps --format '{{.Names}}' | grep web",
    'for f in *.py; do echo "$f"; done',
    "echo \"$HOME\" | sed 's/a/b/'",
    "X=$(date +%s); echo $((X+1))",
    "ls [a-z]* 2>/dev/null | head",
    "python3 -c \"print('\\\\x6b')\"",
    "printf '%s\\n' a b | sort",
    "bash -c 'echo $((1+2))'",
    "cat <<EOF | bash\necho $HOME\nEOF",
    "$PYTHON -c 'import os'",
    '"$(dirname "$0")/run.sh" --flag',
    "source ~/.bashrc && echo ok",
    "sh ./build.sh && echo done",
    "echo $(( 1 + ",                       # does not parse - but has no signal either
    'echo "$(date"',
    "service nginx status",
    "service --status-all",
    "sudo service nginx restart",
])
def test_new_path_no_false_blocks(cmd):
    assert not blocked(cmd, env=_clean_env()), cmd


# The fast path stays fast: an ordinary command does not load the core, and a command with $
# loads the core without the process-starting modules (subprocess, tempfile...) - those are
# needed only when the guard runs something. Checked by the list of loaded modules, not by
# a stopwatch.
def _imported(command):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": os.getcwd()}
    p = subprocess.run([sys.executable, "-X", "importtime", HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=60)
    mods = {line.rsplit("|", 1)[-1].strip() for line in p.stderr.splitlines()
            if line.startswith("import time:")}
    return p.returncode, mods


HEAVY = {"subprocess", "tempfile", "select", "signal", "glob", "fnmatch", "shutil"}


def test_fast_path_loads_nothing():
    rc, mods = _imported("ls -la /srv/app && git status --short 'a b'")
    assert rc == 0
    assert "guard_kill_core" not in mods and not (HEAVY & mods), sorted(HEAVY & mods)


def test_lenient_path_loads_core_without_process_machinery():
    rc, mods = _imported('for f in *.py; do echo "$f" | wc -l; done')
    assert rc == 0
    assert "guard_kill_core" in mods
    assert not (HEAVY & mods), sorted(HEAVY & mods)


# --- 12.2. The guard never runs a program the command names --------------------------------
# To learn the PIDs the guard runs the read-only part of the command. It used to pick the
# binary by file name (./cat, /tmp/x/pgrep), take PATH and LD_PRELOAD from the command itself
# and ignore options that run programs or write files. All of that BEFORE the command was
# approved.

@pytest.fixture
def spies(tmp_path):
    mark = tmp_path / "RAN"
    for name in ("pgrep", "cat", "sort", "awk"):
        p = tmp_path / name
        p.write_text(f"#!/bin/sh\necho \"$0\" >> {mark}\n")
        p.chmod(0o755)
    (tmp_path / "inc.awk").write_text(f'BEGIN {{ system("echo awk >> {mark}") }}\n')
    (tmp_path / "big.txt").write_text("\n".join(str(i) for i in range(200000)))
    return SimpleNamespace(d=str(tmp_path), mark=mark)


@pytest.mark.parametrize("tpl", [
    "kill $({d}/pgrep -f x)",
    "kill $(./cat pids)",
    "kill $(PATH={d} pgrep -f x)",
    "kill $(PATH={d}:/usr/bin cat /dev/null)",
    "kill $(LD_PRELOAD={d}/x.so pgrep -f x)",
    "kill -9 $(ps aux | PATH={d}:/usr/bin awk '{{print $2}}')",
    "kill $(sort --compress-program={d}/cat -S 16k {d}/big.txt | head -1)",
    "kill $(sort --compress-prog={d}/cat -S 16k {d}/big.txt | head -1)",
    "kill $(awk -i {d}/inc.awk 'BEGIN{{}}')",
    "kill $(awk '@include \"{d}/inc.awk\"')",
    "kill $(awk --source='BEGIN{{system(\"echo src >> {d}/RAN\")}}')",
    "kill $(awk -f {d}/inc.awk)",
])
def test_guard_never_runs_what_the_command_names(spies, tpl):
    cmd = tpl.format(d=spies.d)
    rc, _ = run(cmd, cwd=spies.d)
    assert not spies.mark.exists(), f"the guard ran a foreign program: {spies.mark.read_text()}"
    assert rc == 2, cmd                     # where the signal would go is unknown


@pytest.mark.parametrize("tpl", [
    "kill $(sort -no {d}/out.txt {d}/big.txt)",
    "kill $(sort --outp={d}/out.txt {d}/big.txt)",
    "kill $(sort -o {d}/out.txt {d}/big.txt)",
])
def test_guard_never_writes_files(spies, tpl):
    run(tpl.format(d=spies.d), cwd=spies.d)
    assert not os.path.exists(os.path.join(spies.d, "out.txt"))


def test_producers_by_system_path_still_work(outside, own, tmp_path):
    assert blocked(f"kill $(/usr/bin/pgrep -f {outside.marker})")
    assert not blocked(f"kill $(/usr/bin/pgrep -f {own.marker})")
    assert not blocked(f"kill $(/bin/pgrep -f {own.marker} | LC_ALL=C sort -n)")
    assert blocked(f"kill $(pgrep -f {outside.marker} | LC_ALL=C sort -n | head -1)")
    assert blocked(f"kill $(ps -eo pid,args | awk -F' ' '/{outside.marker}/ {{print $1}}')")
    # another guard's marker in front of a read-only command is no reason not to run it
    assert not blocked(f"kill $(FOREGROUND_OK=1 pgrep -f {own.marker})")
    assert blocked(f"kill $(FOREGROUND_OK=1 pgrep -f {outside.marker})")


def test_fifo_does_not_hang_the_guard(tmp_path):
    fifo = tmp_path / "pids"
    os.mkfifo(fifo)
    # The guard must not hang reading a pipe nobody writes to.
    assert run(f"kill $(< {fifo})", timeout=20)[0] == 2
    assert run(f"kill $(cat < {fifo})", timeout=20)[0] == 2
    assert run(f"xargs kill < {fifo}", timeout=20)[0] == 2


# --- 12.3. Monitor runs a shell command too --------------------------------------------------

def test_monitor_command_is_checked(outside, own):
    assert run(f"kill {outside.pid}", tool="Monitor")[0] == 2
    assert run(f"pkill -f {outside.marker}", tool="Monitor")[0] == 2
    assert run(f"kill {own.pid}", tool="Monitor")[0] == 0
    assert run("tail -f /var/log/app.log | grep --line-buffered ERROR", tool="Monitor")[0] == 0


def test_monitor_websocket_ignored():
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(
        {"tool_name": "Monitor", "tool_input": {"ws": {"url": "wss://example.com"},
                                                "description": "d", "timeout_ms": 1000}}),
        capture_output=True, text=True, timeout=30)
    assert p.returncode == 0


# --- 12.4. service stops a unit just like systemctl ------------------------------------------

@pytest.mark.parametrize("cmd", [
    "sudo service agentdeck-web stop",
    "service agentdeck-web restart",
    "sudo service agentdeck-web --full-restart",
])
def test_service_on_protected_unit_blocked(cmd):
    assert blocked(cmd), cmd


# --- 12.5. The guard files are plain ASCII -------------------------------------------------

@pytest.mark.parametrize("name", ["guard_kill.py", "guard_kill_core.py", "guard_kill_fast.py"])
def test_guard_files_are_ascii(name):
    with open(os.path.join(REPO, "hooks", name), "rb") as f:
        assert f.read().isascii(), name
