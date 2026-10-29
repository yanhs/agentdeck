"""guests/install.sh — sets the guests module up on a server where AgentKennel is installed.

Same safety as the root installer's tests (tests/test_install_sh.py): a COPY of the script
runs in a throwaway fake clone; PATH holds only shims that record their argv and change
nothing (sudo never executes its command; docker, systemctl, python3 answer canned) plus
a few harmless read-only tools; commands that write to paths are guarded and refuse any
path outside the sandbox; a "command not found" fails the test.
"""
from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

GUESTS = Path(__file__).resolve().parents[1]
SOURCE = GUESTS / "install.sh"          # read-only: copied into each sandbox
BASH = shutil.which("bash")
UNITS = ("agentkennel-guest-turnstile.service", "agentkennel-guest-tgrelay.service",
         "agentkennel-guest-sitegate.service")
NET_UNITS = ("sbx-firewall.service", "sbx-firewall.timer")

HARMLESS = ("cat", "grep", "sed", "awk", "tr", "cut", "head", "tail", "sort", "wc",
            "dirname", "basename", "mktemp", "readlink", "true", "false", "cmp", "printf")
GUARDED = ("rm", "install", "mkdir", "chmod", "cp", "mv", "ln", "tee")

SHIM = """#!{bash}
echo "{name} $*" >> "$SHIM_LOG"
{body}
"""
BODIES = {
    "sudo": 'case "$*" in "-n true") [ "${FAKE_SUDO_NOPASS:-1}" = 1 ]; exit $?;; esac\nexit 0',
    "id": 'case "$1" in -u) echo 1000;; -un) echo tester;; *) echo uid=1000;; esac',
    "getent": 'echo "tester:x:1000:1000::$HOME:/bin/bash"',
    # docker info: FAKE_DOCKER = ok (default) | denied | down
    "docker": 'case "$1" in info) case "${FAKE_DOCKER:-ok}" in ok) echo 27.0; exit 0;;'
              ' denied) echo "permission denied while trying to connect" >&2; exit 1;;'
              ' *) echo "Cannot connect to the Docker daemon" >&2; exit 1;; esac;; esac; exit 0',
    # systemctl is-active: FAKE_ACTIVE (system) / FAKE_USER_ACTIVE (--user) name active units
    "systemctl": 'case "$*" in *is-active*) case "$*" in *--user*) l="${FAKE_USER_ACTIVE:-}";;'
                 ' *) l="${FAKE_ACTIVE:-}";; esac; for u in $l; do case "$*" in *"$u"*) exit 0;;'
                 ' esac; done; exit 3;; esac; exit 0',
    "python3": 'case "$*" in *image-tag*) echo agentkennel-guest:v9.9.9;; esac; exit 0',
    **{n: "exit 0" for n in ("nginx", "certbot", "iptables")},
}
GUARD = """#!{bash}
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
    def __init__(self, tmp: Path):
        self.root = tmp.resolve()
        self.repo = self.root / "clone"
        self.guests = self.repo / "guests"
        self.home = self.root / "home"
        self.bin = self.root / "shims"
        self.sys = self.root / "sysbin"
        self.systemd = self.root / "systemd"
        self.log = self.root / "calls.log"
        for d in (self.guests, self.home, self.bin, self.sys, self.root / "tmp", self.systemd):
            d.mkdir(parents=True, exist_ok=True)
        self.script = self.guests / "install.sh"
        shutil.copy2(SOURCE, self.script)
        for sub in ("systemd", "net", "image"):
            shutil.copytree(GUESTS / sub, self.guests / sub,
                            ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(GUESTS / ".env.example", self.guests / ".env.example")
        (self.guests / "guestctl.py").write_text("# stub\n")
        (self.repo / "Dockerfile").write_text("FROM scratch\n")
        (self.repo / "services_keeper.py").write_text("# stub\n")
        self.log.write_text("")
        for name, body in BODIES.items():
            self._exe(self.bin / name, SHIM.format(bash=BASH, name=name, body=body))
        realpath = shutil.which("realpath")
        for name in GUARDED:
            self._exe(self.bin / name, GUARD.format(bash=BASH, name=name, real=shutil.which(name),
                                                    realpath=realpath))
        for name in HARMLESS + ("bash",):
            real = shutil.which(name)
            if real:
                os.symlink(real, self.sys / name)

    @staticmethod
    def _exe(p: Path, text: str):
        p.write_text(text)
        p.chmod(0o755)

    def env(self, **extra):
        e = {"PATH": f"{self.bin}:{self.sys}", "HOME": str(self.home),
             "TMPDIR": str(self.root / "tmp"), "SHIM_LOG": str(self.log),
             "SANDBOX": str(self.root), "LANG": "C.UTF-8",
             "GUESTS_SYSTEMD_DIR": str(self.systemd)}
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def _run(self, argv, extra):
        r = subprocess.run(argv, capture_output=True, text=True, env=self.env(**extra),
                           cwd=self.root, timeout=30)
        assert not (self.root / "VIOLATIONS").exists(), \
            f"wrote outside the sandbox: {(self.root / 'VIOLATIONS').read_text()}"
        assert "command not found" not in r.stderr, f"a command has no shim:\n{r.stderr}"
        return r

    def run(self, args: str = "", **extra):
        return self._run([BASH, str(self.script), *shlex.split(args)], extra)

    def lib(self, code: str, **extra):
        return self._run([BASH, "-c", f'GUESTS_SOURCE_ONLY=1 . "{self.script}"\n{code}'], extra)

    def calls(self):
        return self.log.read_text().splitlines()

    def fill_env(self, text="GUESTS_DOMAIN=example.org\nGUEST_BOT_TOKEN=1:T\nOWNER_TG_ID=42\n"):
        p = self.guests / ".env"
        p.write_text(text)
        p.chmod(0o600)


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    yield b
    assert b.script.resolve() != SOURCE.resolve()


CHANGERS = ("sudo", "systemctl", "docker", "install", "rm", "cp", "mv", "ln", "tee", "mkdir")


def changes(calls):
    """Calls that could change the machine (read-only probes allowed)."""
    ok = ("sudo -n true", "docker info", "systemctl is-active", "systemctl --user is-active")
    return [c for c in calls if c.split(" ", 1)[0] in CHANGERS
            and not any(c.startswith(a) for a in ok)]


# ── the file itself ───────────────────────────────────────────────────────────────
def test_script_exists_is_executable_and_valid_bash():
    assert SOURCE.exists()
    assert os.access(SOURCE, os.X_OK)
    r = subprocess.run([BASH, "-n", str(SOURCE)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_help_names_the_flags(box):
    r = box.run("--help")
    assert r.returncode == 0
    assert "--check" in r.stdout and "--yes" in r.stdout
    assert not changes(box.calls())


def test_unknown_flag_is_refused(box):
    r = box.run("--frobnicate")
    assert r.returncode != 0 and "--frobnicate" in r.stderr
    assert not changes(box.calls())


# ── units from the templates ──────────────────────────────────────────────────────
@pytest.mark.parametrize("unit", UNITS)
def test_service_units_run_this_checkout_as_this_user(box, unit):
    r = box.lib(f'GUESTS_USER=tester GUESTS_HOME=/home/tester render_unit {unit}')
    assert r.returncode == 0, r.stderr
    text = r.stdout
    assert "@" not in text.replace("@BotFather", "")
    assert "User=tester\n" in text and "Environment=HOME=/home/tester\n" in text
    prog = {"turnstile": "turnstile/turnstile.py", "tgrelay": "tgrelay/tgrelay.py",
            "sitegate": "sitegate/sitegate.py"}[unit.split("-")[2].split(".")[0]]
    assert f'/usr/bin/python3 -I "{box.guests}/{prog}"' in text
    assert "WantedBy=multi-user.target" in text


def test_firewall_unit_runs_the_checkouts_script(box):
    r = box.lib("render_unit sbx-firewall.service; render_unit sbx-firewall.timer")
    assert r.returncode == 0, r.stderr
    assert f'ExecStart=/bin/bash "{box.guests}/net/sbx-firewall.sh"' in r.stdout
    assert "OnUnitActiveSec=2min" in r.stdout and "@" not in r.stdout


def test_percent_in_a_path_is_escaped_for_systemd(box):
    r = box.lib('GUESTS_DIR="/srv/a%b/guests" GUESTS_USER=u GUESTS_HOME=/h '
                'render_unit agentkennel-guest-turnstile.service')
    assert '"/srv/a%%b/guests/turnstile/turnstile.py"' in r.stdout


def test_unknown_unit_is_an_error(box):
    assert box.lib("render_unit nope.service").returncode != 0


# ── --check ───────────────────────────────────────────────────────────────────────
def test_check_changes_nothing_and_passes_on_a_ready_server(box):
    box.fill_env()
    r = box.run("--check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not changes(box.calls()), changes(box.calls())
    assert not list(box.systemd.iterdir())


def test_check_reports_docker_not_usable_with_the_fix(box):
    r = box.run("--check", FAKE_DOCKER="denied")
    assert r.returncode == 1
    assert "usermod -aG docker" in r.stdout + r.stderr
    assert not changes(box.calls())


def test_check_reports_docker_missing(box):
    (box.bin / "docker").unlink()
    r = box.run("--check")
    assert r.returncode == 1 and "Docker" in r.stdout + r.stderr
    assert not changes(box.calls())


def test_check_needs_agentkennel_itself(box):
    (box.repo / "services_keeper.py").unlink()
    r = box.run("--check")
    assert r.returncode == 1 and "AgentKennel" in r.stdout + r.stderr


def test_check_mentions_missing_settings(box):
    r = box.run("--check")
    assert "guests/.env" in r.stdout
    assert not (box.guests / ".env").exists()


def test_refuses_next_to_the_same_services_running_as_user_units(box):
    """A setup from before 2.0 runs them with systemctl --user: a second copy as system
    units would fight it for the same ports."""
    r = box.run("--yes", FAKE_USER_ACTIVE="agentkennel-guest-turnstile.service")
    assert r.returncode != 0 and "--user" in r.stdout + r.stderr
    assert not changes(box.calls())


# ── the install ───────────────────────────────────────────────────────────────────
def test_with_real_sudo_every_change_goes_through_it(box):
    box.fill_env()
    r = box.run("--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls()
    assert "sudo systemctl enable -q --now sbx-firewall.timer" in calls
    assert any(c.startswith("sudo install -m 0644 ") and c.endswith("/sbx-firewall.timer")
               for c in calls)
    assert not any(c.startswith("systemctl ") and "is-active" not in c for c in calls)


def test_yes_needs_sudo_without_a_password(box):
    box.fill_env()
    r = box.run("--yes", FAKE_SUDO_NOPASS=0)
    assert r.returncode != 0 and "sudo" in r.stderr
    assert not changes(box.calls())


# SUDO="" (as in the root installer's tests): the unit files land in the sandbox's
# systemd folder for real; systemctl is still a recording shim
def test_full_install(box):
    box.fill_env()
    r = box.run("--yes", SUDO="")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls()
    written = sorted(p.name for p in box.systemd.iterdir())
    assert written == sorted(UNITS + NET_UNITS)
    text = (box.systemd / "agentkennel-guest-tgrelay.service").read_text()
    assert f'"{box.guests}/tgrelay/tgrelay.py"' in text and "User=tester" in text
    # network + firewall before the services that bind to its gateway
    i_fw = calls.index("systemctl enable -q --now sbx-firewall.timer")
    assert "systemctl start sbx-firewall.service" in calls
    # the image: AgentKennel of this checkout, then the guest image on top
    i_base = calls.index(f"docker build -t agentkennel:v9.9.9 {box.repo}")
    i_guest = calls.index(f"docker build -t agentkennel-guest:v9.9.9 --build-arg "
                          f"BASE=agentkennel:v9.9.9 {box.guests}/image")
    i_svc = calls.index("systemctl enable -q " + " ".join(UNITS))
    assert "systemctl restart " + " ".join(UNITS) in calls
    assert i_fw < i_svc and i_base < i_guest < i_svc
    assert "doctor" in r.stdout


def test_first_install_writes_the_settings_file_and_waits_for_it(box):
    r = box.run("--yes", SUDO="")
    assert r.returncode == 0, r.stdout + r.stderr
    env = box.guests / ".env"
    assert env.exists() and stat.S_IMODE(env.stat().st_mode) == 0o600
    assert env.read_text() == (box.guests / ".env.example").read_text()
    calls = box.calls()
    # units are written, but nothing that needs the bot token is started yet
    assert (box.systemd / "agentkennel-guest-tgrelay.service").exists()
    assert not any(c.startswith("systemctl enable -q agentkennel-guest") for c in calls)
    assert not any(c.startswith("systemctl restart agentkennel-guest") for c in calls)
    assert "GUEST_BOT_TOKEN" in r.stdout and "install.sh" in r.stdout


def test_an_existing_settings_file_is_never_overwritten(box):
    box.fill_env("GUESTS_DOMAIN=mine.org\nGUEST_BOT_TOKEN=1:T\n")
    assert box.run("--yes", SUDO="").returncode == 0
    assert (box.guests / ".env").read_text() == "GUESTS_DOMAIN=mine.org\nGUEST_BOT_TOKEN=1:T\n"


def test_rerun_leaves_unchanged_units_alone(box):
    box.fill_env()
    assert box.run("--yes", SUDO="").returncode == 0
    box.log.write_text("")
    r = box.run("--yes", SUDO="")
    assert r.returncode == 0
    assert not any(c.startswith("install ") for c in box.calls())
    assert "unchanged" in r.stdout


def test_docker_down_stops_before_any_change(box):
    box.fill_env()
    r = box.run("--yes", FAKE_DOCKER="down")
    assert r.returncode != 0
    assert not changes(box.calls())


# ── v2.1.0: agentdeck-guest-* became agentkennel-guest-* ──────────────────────────────
OLD_UNITS = tuple(u.replace("agentkennel-guest-", "agentdeck-guest-") for u in UNITS)


def test_the_old_names_running_as_user_units_also_refuse(box):
    """The owner's server runs the services as user units under their old names."""
    r = box.run("--yes", FAKE_USER_ACTIVE="agentdeck-guest-tgrelay.service")
    assert r.returncode != 0 and "--user" in r.stdout + r.stderr
    assert not changes(box.calls())


def test_system_units_under_the_old_names_are_moved(box):
    box.fill_env()
    for u in OLD_UNITS:
        (box.systemd / u).write_text("[Unit]\n")
    r = box.run("--yes", SUDO="")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls()
    for u in OLD_UNITS:
        assert not (box.systemd / u).exists(), u
        assert f"systemctl disable --now -q {u}" in calls, u
    for u in UNITS:
        assert (box.systemd / u).exists(), u
    i_old = max(calls.index(f"systemctl disable --now -q {u}") for u in OLD_UNITS)
    assert i_old < calls.index("systemctl restart " + " ".join(UNITS))
    assert "moved" in r.stdout


def test_nothing_to_move_on_a_fresh_server(box):
    box.fill_env()
    r = box.run("--yes", SUDO="")
    assert r.returncode == 0
    assert not [c for c in box.calls() if "agentdeck-" in c and "is-active" not in c]
