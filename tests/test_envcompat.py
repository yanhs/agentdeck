"""envcompat.py — AgentKennel's settings by their new names, with the earlier names
(AGENTDECK_*, before v2.1.0) still accepted.

The owner's live server runs this code with systemd drop-ins that set AGENTDECK_ORIGIN,
AGENTDECK_PASSFILE, AGENTDECK_PASTE_DIR and AGENTDECK_PASTE_URL; guest sandboxes are
created with AGENTDECK_PASSWORD. Those must keep working without a change on their side,
and a new name set next to an old one wins.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import envcompat  # noqa: E402


def test_new_name_wins_over_the_old_one():
    env = {"AGENTKENNEL_ORIGIN": "https://new.example", "AGENTDECK_ORIGIN": "https://old.example"}
    assert envcompat.get("ORIGIN", env=env) == "https://new.example"


def test_the_old_name_alone_still_works():
    assert envcompat.get("PASSFILE", env={"AGENTDECK_PASSFILE": "/x/.dashpass"}) == "/x/.dashpass"


def test_neither_name_gives_the_default():
    assert envcompat.get("MAX_ACTIVE", "12", env={}) == "12"
    assert envcompat.get("MAX_ACTIVE", env={}) is None


def test_an_empty_new_name_falls_back_to_the_old_one():
    """docker-compose passes `AGENTKENNEL_ORIGIN: ""` when the person set nothing new."""
    env = {"AGENTKENNEL_ORIGIN": "", "AGENTDECK_ORIGIN": "https://old.example"}
    assert envcompat.get("ORIGIN", env=env) == "https://old.example"
    assert envcompat.get("ORIGIN", "d", env={"AGENTKENNEL_ORIGIN": "", "AGENTDECK_ORIGIN": ""}) == "d"


def test_full_names_are_accepted_too():
    env = {"AGENTDECK_WORKDIR": "/w"}
    assert envcompat.get("AGENTKENNEL_WORKDIR", env=env) == "/w"
    assert envcompat.get("AGENTDECK_WORKDIR", env=env) == "/w"


def test_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.delenv("AGENTKENNEL_PASTE_URL", raising=False)
    monkeypatch.setenv("AGENTDECK_PASTE_URL", "https://files.example/p")
    assert envcompat.get("PASTE_URL") == "https://files.example/p"
    monkeypatch.setenv("AGENTKENNEL_PASTE_URL", "https://files.example/new")
    assert envcompat.get("PASTE_URL") == "https://files.example/new"


def test_both_sets_the_new_and_the_old_name():
    """What AgentKennel hands to child processes: hooks and the owner's own tools may
    still read the old name."""
    assert envcompat.both("SESSION", "abcdef12") == {"AGENTKENNEL_SESSION": "abcdef12",
                                                     "AGENTDECK_SESSION": "abcdef12"}


def test_names():
    assert envcompat.names("SITE") == ("AGENTKENNEL_SITE", "AGENTDECK_SITE")


def _py(code, **env):
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("AGENTKENNEL_", "AGENTDECK_"))}
    e.update(env)
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=e, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.parametrize("env, want", [
    ({"AGENTDECK_MAX_ACTIVE": "5"}, "5"),
    ({"AGENTKENNEL_MAX_ACTIVE": "7", "AGENTDECK_MAX_ACTIVE": "5"}, "7"),
    ({}, "12"),
])
def test_the_library_limit_reads_either_name(env, want):
    assert _py("import library; print(library.MAX_ACTIVE)", **env) == want


@pytest.mark.parametrize("old, new", [("AGENTDECK_PASSFILE", "AGENTKENNEL_PASSFILE")])
def test_the_live_servers_drop_in_still_sets_the_password_file(tmp_path, old, new):
    pf = str(tmp_path / ".dashpass")
    assert _py("import status_server as s; print(s.PASSFILE)", **{old: pf}) == pf
    assert _py("import status_server as s; print(s.PASSFILE)",
               **{old: pf, new: pf + ".new"}) == pf + ".new"


def test_the_secrets_guard_reads_either_name():
    code = ("import sys; sys.path.insert(0, 'hooks'); import guard_secrets as g; "
            "print(','.join(sorted(g.env_ips())))")
    assert _py(code, AGENTDECK_PUBLIC_IPS="203.0.113.7") == "203.0.113.7"
    assert _py(code, AGENTDECK_PUBLIC_IPS="203.0.113.7",
               AGENTKENNEL_PUBLIC_IPS="198.51.100.4") == "198.51.100.4"


def test_the_secrets_guard_reads_both_ip_files(tmp_path):
    """~/.claude/agentdeck-public-ips (written before v2.1.0) still counts; the new file
    is ~/.claude/agentkennel-public-ips."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agentdeck-public-ips").write_text("203.0.113.7\n")
    code = ("import sys; sys.path.insert(0, 'hooks'); import guard_secrets as g; "
            "print(sorted(g.ips_file_paths())); "
            "print(','.join(sorted(set().union(*map(g.read_ip_file, g.ips_file_paths())))))")
    out = _py(code, HOME=str(tmp_path)).splitlines()
    assert "agentkennel-public-ips" in out[0] and "agentdeck-public-ips" in out[0]
    assert out[1] == "203.0.113.7"
    (tmp_path / ".claude" / "agentkennel-public-ips").write_text("198.51.100.4\n")
    assert _py(code, HOME=str(tmp_path)).splitlines()[1] == "198.51.100.4,203.0.113.7"


# ── every read goes through the helper ──────────────────────────────────────────────
PY_SOURCES = [p for p in REPO.rglob("*.py")
              if ".git" not in p.parts and "tests" not in p.parts
              and p.name != "envcompat.py" and "__pycache__" not in p.parts]
DIRECT_READ = re.compile(r"""(environ\.get\(|getenv\(|environ\[)\s*["'](AGENTDECK|AGENTKENNEL)_[A-Z0-9_]+["']\s*(?!\]\s*=[^=])""")


def test_no_python_reads_an_agentkennel_variable_directly():
    """A direct os.environ read of either name would miss the other one."""
    hits = [f"{p.relative_to(REPO)}:{i}: {line.strip()}"
            for p in PY_SOURCES
            for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1)
            if DIRECT_READ.search(line)]
    assert not hits, "\n".join(hits)


SHELL_SOURCES = [REPO / n for n in ("install.sh", "start.sh", "https.sh", "open-session.sh",
                                    "docker/entrypoint.sh", "bin/pane", "guests/install.sh")]
OLD_READ = re.compile(r"\$\{?AGENTDECK_([A-Z0-9_]+)")


@pytest.mark.parametrize("path", SHELL_SOURCES, ids=lambda p: str(p.relative_to(REPO)))
def test_shell_reads_the_old_name_only_as_the_fallback_of_the_new(path):
    """`${AGENTKENNEL_X:-${AGENTDECK_X…}}` is the one allowed form of an old-name read."""
    bad = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        line = line.replace("\\$\\$", "$")        # systemd's escaped $ inside a unit text
        for m in OLD_READ.finditer(line):
            name = m.group(1)
            if f"${{AGENTKENNEL_{name}:-${{AGENTDECK_{name}" not in line:
                bad.append(f"{i}: {line.strip()}")
    assert not bad, "\n".join(bad)
