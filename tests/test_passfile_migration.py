"""Dashboard password: move from the nginx htpasswd file to the dashboard's own
passfile WITHOUT anyone typing the password into a chat (owner 2026-09-25).

With AGENTDECK_PASSFILE set but still empty and the old htpasswd present:
  * the login page stays a normal login (never the open 'set a password' form —
    the first stranger to arrive would own the dashboard),
  * a login is checked against htpasswd and, on success, seeds the passfile,
  * /change-password checks the current password against htpasswd until seeded.
"""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

TERMINAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TERMINAL_DIR))


@pytest.fixture
def mod(tmp_path, monkeypatch):
    ht = tmp_path / "htpasswd"
    subprocess.run(["htpasswd", "-cb", str(ht), "admin", "old-secret-1"], check=True,
                   capture_output=True)
    monkeypatch.setenv("AGENTDECK_PASSFILE", str(tmp_path / ".dashpass"))
    monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(tmp_path / ".secret"))
    sys.modules.pop("status_server", None)
    m = importlib.import_module("status_server")
    monkeypatch.setattr(m, "HTPASSWD_FILE", str(ht))
    return m


def test_empty_passfile_with_htpasswd_is_not_an_open_setup(mod):
    assert mod._pw_is_set() is False
    assert mod._pw_seedable() is True          # login page must be the normal one


def test_login_checks_htpasswd_and_seeds_the_passfile(mod):
    assert mod._login_verify("admin", "wrong") is False
    assert mod._pw_is_set() is False
    assert mod._login_verify("admin", "old-secret-1") is True
    assert mod._pw_is_set() is True
    assert mod._pw_verify("old-secret-1") is True        # same password, now in passfile


def test_after_seeding_only_the_passfile_counts(mod):
    mod._login_verify("admin", "old-secret-1")
    mod._pw_store("new-secret-2")
    assert mod._login_verify("admin", "old-secret-1") is False
    assert mod._login_verify("admin", "new-secret-2") is True


def test_change_password_before_seeding_uses_htpasswd_for_current(mod):
    assert mod._verify_current("admin", "old-secret-1") is True
    assert mod._verify_current("admin", "nope") is False


def test_no_htpasswd_keeps_the_first_run_setup(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "HTPASSWD_FILE", str(tmp_path / "missing"))
    assert mod._pw_seedable() is False
