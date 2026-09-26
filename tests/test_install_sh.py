"""install.sh — the bare-metal installer (agents get the whole server).

SAFETY FIRST. These tests never run the repo's install.sh and never reach the real machine:

  * a COPY of install.sh is placed in a tmp sandbox (with stub status_server.py /
    open-session.sh so it looks like a clone) — `SOURCE` is only ever read;
  * PATH = a shim dir + a dir of explicitly symlinked harmless tools. Every external
    command install.sh may call (sudo, systemctl, tmux, apt-get, curl, npm, git, python3,
    ss, caddy, ttyd, …) is a shim that records its argv and changes nothing. `sudo` NEVER
    executes its command;
  * commands that write to paths (rm, install, mkdir, chmod, touch, mv, cp, ln, tee) are
    guarded shims: any path argument outside the sandbox is refused and recorded as a
    violation, which fails the test;
  * the environment is built from scratch (HOME, TMPDIR, TMUX_TMPDIR in the sandbox);
  * any "command not found" (a command missing from the shim table) fails the test.

The real install runs only in systemd containers (tests/install/run.sh) and on CI VMs.
"""
from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "install.sh"          # read-only: copied into each sandbox, never executed

BASH = shutil.which("bash")

OS_RELEASES = {
    "ubuntu-22.04": 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="22.04"\n',
    "ubuntu-24.04": 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n',
    "debian-12": 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\n',
    "debian-11": 'ID=debian\nVERSION_ID="11"\n',
    "ubuntu-20.04": 'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="20.04"\n',
    "fedora-40": 'NAME="Fedora Linux"\nID=fedora\nVERSION_ID=40\n',
    "alpine": 'NAME="Alpine Linux"\nID=alpine\nVERSION_ID=3.20.0\n',
    "mint": 'ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="21.3"\n',
}

# read-only / harmless tools install.sh may use for real
HARMLESS = ("cat", "grep", "sed", "awk", "tr", "cut", "head", "tail", "sort", "wc",
            "dirname", "basename", "mktemp", "seq", "readlink", "date", "true", "false",
            "sleep")

# tools that write to paths: run for real, but only inside the sandbox
GUARDED = ("rm", "install", "mkdir", "chmod", "touch", "mv", "cp", "ln", "tee")

SHIM = """#!{bash}
echo "{name} $*" >> "$SHIM_LOG"
{body}
"""

# every other external command: record + canned answer, nothing real happens
BODIES = {
    # sudo never runs anything; `sudo -n true` answers from FAKE_SUDO_NOPASS
    "sudo": 'case "$*" in "-n true") [ "${FAKE_SUDO_NOPASS:-1}" = 1 ]; exit $?;; esac\nexit 0',
    "id": 'case "$1" in -u) echo "${FAKE_UID:-1000}";; -un) echo "${FAKE_USER:-tester}";;'
          ' *) echo "uid=${FAKE_UID:-1000}";; esac',
    "uname": 'case "$1" in -m) echo "${FAKE_ARCH:-x86_64}";; *) echo Linux;; esac',
    "curl": "exit 7",
    # systemctl is-active: FAKE_ACTIVE names the active units (default: none)
    "systemctl": 'case "$*" in *is-active*) for u in ${FAKE_ACTIVE:-}; do'
                 ' case "$*" in *"$u"*) exit 0;; esac; done; exit 3;; esac; exit 0',
    "tmux": 'echo "tmux-env TMUX_TMPDIR=${TMUX_TMPDIR:-} $*" >> "$SHIM_LOG"; exit 0',
    "ss": "exit 0",
    "getent": 'echo "tester:x:1000:1000::$HOME:/bin/bash"',
    "hostname": "echo 192.0.2.10",
    "dpkg-query": "exit 1",
    **{n: "exit 0" for n in ("apt-get", "npm", "node", "git", "loginctl", "python3", "gpg",
                              "tar", "ufw", "caddy", "ttyd", "journalctl", "env", "chown")},
}

GUARD = """#!{bash}
# {name}: allowed only on paths inside the sandbox
echo "{name} $*" >> "$SHIM_LOG"
for a in "$@"; do
  case "$a" in -*) continue ;; esac
  case "$a" in */*|.*|~*) ;; *) continue ;; esac
  p="$({realpath} -m -- "$a")"
  case "$p" in "$SANDBOX"|"$SANDBOX"/*) ;; *)
    echo "{name} $*" >> "$SANDBOX/VIOLATIONS"; echo "sandbox: {name} $a refused" >&2; exit 99 ;;
  esac
done
exec {real} "$@"
"""


class Box:
    """One sandbox: a copy of install.sh in a fake clone, a fake HOME, the shims."""

    def __init__(self, tmp: Path):
        self.root = tmp.resolve()
        self.repo = self.root / "clone"
        self.home = self.root / "home"
        self.bin = self.root / "shims"
        self.sys = self.root / "sysbin"
        self.log = self.root / "calls.log"
        for d in (self.repo, self.home, self.bin, self.sys, self.root / "tmp",
                  self.root / "tmux", self.root / "systemd"):
            d.mkdir(parents=True, exist_ok=True)
        self.script = self.repo / "install.sh"
        shutil.copy2(SOURCE, self.script)
        assert self.script.resolve() != SOURCE.resolve()
        assert str(self.script.resolve()).startswith(str(self.root))
        (self.repo / "status_server.py").write_text("# stub\n")
        (self.repo / "open-session.sh").write_text("# stub\n")
        self.log.write_text("")
        for name, body in BODIES.items():
            self._exe(self.bin / name, SHIM.format(bash=BASH, name=name, body=body))
        realpath = shutil.which("realpath")
        for name in GUARDED:
            real = shutil.which(name)
            assert real, name
            self._exe(self.bin / name, GUARD.format(bash=BASH, name=name, real=real,
                                                    realpath=realpath))
        for name in HARMLESS + ("bash",):
            real = shutil.which(name)
            assert real, name
            os.symlink(real, self.sys / name)

    @staticmethod
    def _exe(p: Path, text: str):
        p.write_text(text)
        p.chmod(0o755)

    def os_release(self, key):
        p = self.root / f"os-release-{key}"
        p.write_text(OS_RELEASES[key])
        return str(p)

    def env(self, **extra):
        e = {"PATH": f"{self.bin}:{self.sys}", "HOME": str(self.home),
             "TMPDIR": str(self.root / "tmp"), "TMUX_TMPDIR": str(self.root / "tmux"),
             "SHIM_LOG": str(self.log), "SANDBOX": str(self.root), "LANG": "C.UTF-8",
             "AGENTDECK_SYSTEMD_DIR": str(self.root / "systemd"),
             "AGENTDECK_OS_RELEASE": self.os_release("ubuntu-24.04"),
             "AGENTDECK_PUBLIC_IP": "203.0.113.7", "AGENTDECK_PORT": str(free_port())}
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def _run(self, argv, extra):
        r = subprocess.run(argv, capture_output=True, text=True, env=self.env(**extra),
                           cwd=self.root, timeout=30)
        self.check_clean(r)
        return r

    def run(self, args: str = "", **extra):
        """bash <copy of install.sh> args"""
        return self._run([BASH, str(self.script), *shlex.split(args)], extra)

    def lib(self, code: str, **extra):
        """`code` with the copy's functions loaded (AGENTDECK_SOURCE_ONLY=1, no main)."""
        return self._run([BASH, "-c", f'AGENTDECK_SOURCE_ONLY=1 . "{self.script}"\n{code}'],
                         extra)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def check_clean(self, r):
        v = self.root / "VIOLATIONS"
        assert not v.exists(), f"wrote outside the sandbox: {v.read_text()}"
        assert "command not found" not in r.stderr, \
            f"install.sh called a command that has no shim:\n{r.stderr}"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    yield b
    assert b.script.resolve() != SOURCE.resolve()


# commands that would change a machine: none may run during a refusal
CHANGERS = ("sudo", "apt-get", "systemctl", "npm", "git", "loginctl", "tmux", "rm",
            "install", "python3", "curl")


def changes(calls, allow=("sudo -n true",)):
    return [c for c in calls if c.split(" ", 1)[0] in CHANGERS
            and not any(c.startswith(a) for a in allow)]


# ── the sandbox itself ────────────────────────────────────────────────────────────
def test_sandbox_path_has_no_real_dangerous_tools(box):
    r = box.lib("for c in rm tmux systemctl sudo apt-get install; do command -v $c; done")
    assert r.returncode == 0
    for line in r.stdout.split():
        assert line.startswith(str(box.bin)), line


def test_sandbox_catches_an_unshimmed_command(box):
    r = subprocess.run([BASH, "-c", "frobnicate-xyz"], capture_output=True, text=True,
                       env=box.env())
    with pytest.raises(AssertionError, match="no shim"):
        box.check_clean(r)


def test_sandbox_refuses_writes_outside(box):
    r = subprocess.run([BASH, "-c", "rm -f /etc/hostname-agentdeck-test-none"],
                       capture_output=True, text=True, env=box.env())
    assert r.returncode == 99
    with pytest.raises(AssertionError, match="outside the sandbox"):
        box.check_clean(r)


# ── the file itself ───────────────────────────────────────────────────────────────
def test_script_exists_and_is_valid_bash():
    assert SOURCE.exists()
    assert os.access(SOURCE, os.X_OK), "install.sh must be executable (./install.sh)"
    r = subprocess.run([BASH, "-n", str(SOURCE)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_help_mentions_every_flag(box):
    r = box.run("--help")
    assert r.returncode == 0
    for flag in ("--yes", "--https", "--uninstall", "--purge", "--check", "--telegram"):
        assert flag in r.stdout, flag
    assert not changes(box.calls())


def test_unknown_flag_is_refused(box):
    r = box.run("--frobnicate")
    assert r.returncode != 0
    assert "--frobnicate" in r.stderr
    assert not changes(box.calls())


# ── OS / arch detection ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("key,expect", [("ubuntu-22.04", "ubuntu 22.04"),
                                        ("ubuntu-24.04", "ubuntu 24.04"),
                                        ("debian-12", "debian 12")])
def test_supported_os_detected(box, key, expect):
    r = box.lib("detect_os", AGENTDECK_OS_RELEASE=box.os_release(key))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expect


@pytest.mark.parametrize("key", ["debian-11", "ubuntu-20.04", "fedora-40", "alpine", "mint"])
def test_unsupported_os_rejected(box, key):
    assert box.lib("detect_os", AGENTDECK_OS_RELEASE=box.os_release(key)).returncode != 0


def test_missing_os_release_rejected(box):
    assert box.lib("detect_os", AGENTDECK_OS_RELEASE=box.root / "nope").returncode != 0


@pytest.mark.parametrize("arch,asset", [("x86_64", "ttyd.x86_64"), ("amd64", "ttyd.x86_64"),
                                        ("aarch64", "ttyd.aarch64"), ("arm64", "ttyd.aarch64")])
def test_ttyd_asset_by_arch(box, arch, asset):
    r = box.lib(f"ttyd_asset {arch}")
    assert r.returncode == 0 and r.stdout.strip() == asset


@pytest.mark.parametrize("arch,name", [("x86_64", "amd64"), ("aarch64", "arm64")])
def test_caddy_arch(box, arch, name):
    r = box.lib(f"caddy_arch {arch}")
    assert r.returncode == 0 and r.stdout.strip() == name


@pytest.mark.parametrize("arch", ["riscv64", "armv7l", "i686", "s390x"])
def test_unknown_arch_has_no_asset(box, arch):
    assert box.lib(f"ttyd_asset {arch}").returncode != 0
    assert box.lib(f"caddy_arch {arch}").returncode != 0


# ── preflight refusals change nothing ───────────────────────────────────────────
@pytest.mark.parametrize("key", ["fedora-40", "debian-11", "alpine"])
def test_unsupported_os_refuses_cleanly(box, key):
    r = box.run("--yes", AGENTDECK_OS_RELEASE=box.os_release(key))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "not supported" in out
    assert "docker compose up -d" in out            # points at the sandbox option
    assert not changes(box.calls()), changes(box.calls())
    assert not any((box.root / "systemd").iterdir())


def test_unsupported_arch_refuses_cleanly(box):
    r = box.run("--yes", FAKE_ARCH="riscv64")
    assert r.returncode != 0
    assert "not supported" in r.stdout + r.stderr
    assert not changes(box.calls())


def test_root_is_refused_without_override(box):
    r = box.run("--yes", FAKE_UID="0")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "AGENTDECK_ALLOW_ROOT=1" in out and "root" in out
    assert not changes(box.calls())


def test_root_allowed_with_override(box):
    r = box.run("--yes --check", FAKE_UID="0", AGENTDECK_ALLOW_ROOT="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "preflight ok" in r.stdout


def test_check_mode_passes_and_changes_nothing(box):
    r = box.run("--yes --check", AGENTDECK_OS_RELEASE=box.os_release("debian-12"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "preflight ok" in r.stdout
    assert changes(box.calls()) == []


def test_yes_with_password_sudo_fails_fast(box):
    r = box.run("--yes", FAKE_SUDO_NOPASS="0")
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "sudo" in out and "password" in out
    assert not changes(box.calls())


def test_no_sudo_at_all_is_refused(box):
    (box.bin / "sudo").unlink()
    r = box.run("--yes")
    assert r.returncode != 0
    assert "sudo" in r.stdout + r.stderr
    assert not changes(box.calls())


# ── --https name + ports ─────────────────────────────────────────────────────────
def test_https_with_domain_uses_it(box):
    r = box.lib("https_site example.org")
    assert r.returncode == 0 and r.stdout.strip() == "example.org"


def test_https_without_domain_uses_sslip(box):
    r = box.lib("https_site ''")
    assert r.returncode == 0 and r.stdout.strip() == "203-0-113-7.sslip.io"


@pytest.mark.parametrize("bad", ["http://x.org", "x.org/path", "a b", "-x", "x.org:443"])
def test_https_bad_domain_refused(box, bad):
    assert box.lib(f"https_site '{bad}'").returncode != 0


def test_https_without_ip_refused(box):
    # the curl shim fails every lookup; no AGENTDECK_PUBLIC_IP
    assert box.lib("https_site ''", AGENTDECK_PUBLIC_IP="").returncode != 0


def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_port_busy_probe(box):
    s, port = listener()
    try:
        assert box.lib(f"port_busy {port}").returncode == 0
    finally:
        s.close()
    assert box.lib(f"port_busy {port}").returncode != 0


def test_https_refuses_busy_port(box):
    s, busy = listener()
    try:
        r = box.run("--yes --check --https example.org", AGENTDECK_HTTP_PORT=busy,
                    AGENTDECK_HTTPS_PORT=free_port())
    finally:
        s.close()
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert str(busy) in out and "busy" in out
    assert not changes(box.calls())


def test_plain_mode_refuses_busy_dashboard_port(box):
    s, busy = listener()
    try:
        r = box.run("--yes --check", AGENTDECK_PORT=busy)
    finally:
        s.close()
    assert r.returncode != 0
    assert "busy" in r.stdout + r.stderr


def test_rerun_our_own_caddy_on_the_port_is_not_busy(box):
    """A re-run finds the dashboard port taken by OUR running Caddy — that is fine."""
    s, port = listener()
    (box.root / "systemd" / "agentdeck-caddy.service").write_text(
        f"[Service]\nEnvironment=AGENTDECK_SITE=:{port}\n")
    try:
        ok = box.run("--yes --check", AGENTDECK_PORT=port, FAKE_ACTIVE="agentdeck-caddy.service")
        stopped = box.run("--yes --check", AGENTDECK_PORT=port)
    finally:
        s.close()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert stopped.returncode != 0 and "busy" in stopped.stdout + stopped.stderr


# ── systemd units ────────────────────────────────────────────────────────────────
UNITS = ("agentdeck-status.service", "agentdeck-tasks.service",
         "agentdeck-sessions.service", "agentdeck-caddy.service",
         "agentdeck-reaper.service", "agentdeck-reaper.timer")

UNIT_ENV = {"AGENTDECK_DIR": "/home/tester/agentdeck", "AGENTDECK_USER": "tester",
            "AGENTDECK_HOME": "/home/tester", "AGENTDECK_SITE": ":8765"}


def render(box, name, **extra):
    r = box.lib(f"render_unit {name}", **{**UNIT_ENV, **extra})
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_unit_list(box):
    assert box.lib("echo $UNITS").stdout.split() == list(UNITS)


@pytest.mark.parametrize("name", [u for u in UNITS if u.endswith(".service")])
def test_services_run_as_the_user_in_the_repo(box, name):
    u = render(box, name)
    assert "User=tester" in u
    assert "WorkingDirectory=/home/tester/agentdeck" in u
    assert "Environment=HOME=/home/tester" in u
    assert "TRACKER_STATE=/home/tester/agentdeck/.sessions/tasks-state.json" in u
    assert "EnvironmentFile=-/home/tester/agentdeck/.env" in u
    assert "LANG=C.UTF-8" in u


@pytest.mark.parametrize("name", [u for u in UNITS if u.endswith(".service")])
def test_agents_get_a_tmux_server_of_their_own(box, name):
    # every tmux call of the stack (and of the agents) goes to this folder's server,
    # so nothing AgentDeck does can reach the user's own tmux sessions
    assert "Environment=TMUX_TMPDIR=/home/tester/agentdeck/.sessions/tmux" in render(box, name)


@pytest.mark.parametrize("name", ["agentdeck-status.service", "agentdeck-tasks.service",
                                  "agentdeck-sessions.service", "agentdeck-caddy.service"])
def test_long_running_services_restart_and_start_at_boot(box, name):
    u = render(box, name)
    assert "Restart=always" in u
    assert "WantedBy=multi-user.target" in u


@pytest.mark.parametrize("name", ["agentdeck-status.service", "agentdeck-sessions.service"])
def test_restarting_a_service_never_kills_the_agents(box, name):
    assert "KillMode=process" in render(box, name)


def test_sessions_unit_runs_the_one_ttyd(box):
    u = render(box, "agentdeck-sessions.service")
    line = next(l for l in u.splitlines() if l.startswith("ExecStart="))
    for part in ("/usr/local/bin/ttyd", "-W", "-a", "-O", "-i lo", "-p 3031",
                 "--base-path /sess", "/home/tester/agentdeck/open-session.sh"):
        assert part in line, part


def test_caddy_unit_site_and_low_ports(box):
    u = render(box, "agentdeck-caddy.service", AGENTDECK_SITE="1-2-3-4.sslip.io")
    assert "Environment=AGENTDECK_SITE=1-2-3-4.sslip.io" in u
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in u
    assert "/usr/local/bin/caddy run" in u
    assert "XDG_DATA_HOME=/home/tester/.local/share" in u


def test_reaper_is_a_timer_every_minute(box):
    t = render(box, "agentdeck-reaper.timer")
    assert "OnUnitActiveSec=1min" in t
    assert "WantedBy=timers.target" in t
    s = render(box, "agentdeck-reaper.service")
    assert "Type=oneshot" in s and "idle_reaper.py" in s


def test_paths_with_spaces_are_quoted(box):
    u = render(box, "agentdeck-sessions.service", AGENTDECK_DIR="/home/t/my deck")
    assert '"/home/t/my deck/open-session.sh"' in u


def test_unit_writing_is_idempotent(box):
    sysd = box.root / "systemd"
    code = 'write_unit agentdeck-status.service; echo "changed=${CHANGED_UNITS[*]}"'
    env = {**UNIT_ENV, "SUDO": ""}
    r1 = box.lib(code, **env)
    assert r1.returncode == 0, r1.stderr
    assert "changed=agentdeck-status.service" in r1.stdout
    f = sysd / "agentdeck-status.service"
    first, mtime = f.read_text(), f.stat().st_mtime_ns
    r2 = box.lib(code, **env)
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip().endswith("changed=")
    assert f.read_text() == first and f.stat().st_mtime_ns == mtime
    r3 = box.lib(code, **{**env, "AGENTDECK_USER": "other"})
    assert "changed=agentdeck-status.service" in r3.stdout
    assert "User=other" in f.read_text()


# ── the install record ───────────────────────────────────────────────────────────
def conf_path(box):
    return box.home / ".config" / "agentdeck" / "install.env"


def test_prepare_data_records_the_install(box):
    d = box.root / "deck"
    d.mkdir()
    (d / ".agentdeck-managed").write_text("x")
    r = box.lib(f'DIR="{d}"; SITE=":8765"; prepare_data')
    assert r.returncode == 0, r.stderr
    conf = conf_path(box).read_text()
    assert f"AGENTDECK_DIR={d}\n" in conf
    assert "AGENTDECK_MANAGED=1\n" in conf
    assert f"AGENTDECK_TMUX_TMPDIR={d}/.sessions/tmux\n" in conf
    assert "AGENTDECK_SITE=:8765\n" in conf
    assert (d / ".sessions" / "tmux").is_dir()
    assert (d / ".sessions" / "tasks-state.json").exists()


def test_a_clone_is_recorded_as_not_managed(box):
    r = box.lib(f'DIR="{box.repo}"; SITE=":8765"; prepare_data')
    assert r.returncode == 0, r.stderr
    assert "AGENTDECK_MANAGED=0\n" in conf_path(box).read_text()


# ── uninstall / purge: only what the installer recorded ──────────────────────────
def record(box, d, managed=True, marker=True, tmuxdir=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / ".sessions").mkdir(exist_ok=True)
    (d / ".sessions" / "library.json").write_text("{}")
    if marker:
        (d / ".agentdeck-managed").write_text("x")
    tmuxdir = tmuxdir if tmuxdir is not None else d / ".sessions" / "tmux"
    Path(tmuxdir).mkdir(parents=True, exist_ok=True)
    conf_path(box).parent.mkdir(parents=True, exist_ok=True)
    conf_path(box).write_text(f"AGENTDECK_DIR={d}\nAGENTDECK_MANAGED={1 if managed else 0}\n"
                              f"AGENTDECK_TMUX_TMPDIR={tmuxdir}\nAGENTDECK_SITE=:8765\n")


def tmux_calls(box):
    return [c for c in box.calls() if c.startswith("tmux-env ")]


def test_uninstall_without_a_record_touches_no_tmux_and_deletes_nothing(box):
    r = box.run("--uninstall --yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "AgentDeck services removed" in r.stdout
    assert tmux_calls(box) == []
    assert not [c for c in box.calls() if c.startswith("rm ")]


def test_uninstall_stops_only_the_recorded_tmux_server(box):
    d = box.home / "agentdeck"
    record(box, d)
    r = box.run("--uninstall --yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert tmux_calls(box) == [f"tmux-env TMUX_TMPDIR={d}/.sessions/tmux kill-server"]
    assert (d / ".sessions" / "library.json").exists()      # data kept
    assert conf_path(box).exists()                           # record kept for a later --purge


@pytest.mark.parametrize("bad", ["/tmp", "HOME", "/home/x/.sessions/other"])
def test_uninstall_ignores_a_tmux_dir_it_did_not_make(box, bad):
    d = box.home / "agentdeck"
    tdir = str(box.home) if bad == "HOME" else bad
    record(box, d, tmuxdir=box.root / "unused")
    text = conf_path(box).read_text().replace(f"={box.root}/unused", f"={tdir}")
    conf_path(box).write_text(text)
    r = box.run("--uninstall --yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert tmux_calls(box) == []


def test_purge_deletes_the_folder_the_installer_cloned(box):
    d = box.home / "agentdeck"
    record(box, d)
    r = box.run("--uninstall --purge --yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not d.exists()
    assert not conf_path(box).exists()
    assert box.home.exists()


@pytest.mark.parametrize("case", ["no-record", "not-managed", "no-marker", "home", "clone"])
def test_purge_refuses_anything_else_and_changes_nothing(box, case):
    d = box.home / "agentdeck"
    if case == "not-managed":
        record(box, d, managed=False)
    elif case == "no-marker":
        record(box, d, marker=False)
    elif case == "home":
        record(box, box.home)
    elif case == "clone":
        # the clone install.sh itself lives in (like running ./install.sh from a checkout)
        (box.repo / ".sessions").mkdir()
        (box.repo / ".sessions" / "library.json").write_text("{}")
        conf_path(box).parent.mkdir(parents=True)
        conf_path(box).write_text(f"AGENTDECK_DIR={box.repo}\nAGENTDECK_MANAGED=0\n")
    r = box.run("--uninstall --purge --yes")
    assert r.returncode != 0
    assert "--purge" in r.stderr and "nothing was changed" in r.stderr
    assert changes(box.calls()) == []
    assert (box.repo / "install.sh").exists()
    if case == "clone":
        assert (box.repo / ".sessions" / "library.json").exists()
    if case in ("not-managed", "no-marker"):
        assert (d / ".sessions" / "library.json").exists()


def test_purge_needs_uninstall(box):
    r = box.run("--purge --yes")
    assert r.returncode != 0 and "--uninstall" in r.stderr
    assert changes(box.calls()) == []
