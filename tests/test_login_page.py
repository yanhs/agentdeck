"""The login page: AgentDeck branding, and no Username field where it is ignored.

Passfile mode (Docker / start.sh: one dashboard password) ignores the username, so the
page must not ask for it. It stays where it matters: htpasswd mode (no passfile) and the
passfile-still-empty migration state, where the login is checked against htpasswd."""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def load(tmp_path, monkeypatch):
    def _load(passfile: bool, htpasswd: bool, seeded: bool = False):
        pf = tmp_path / ".dashpass"
        if passfile:
            monkeypatch.setenv("AGENTDECK_PASSFILE", str(pf))
        else:
            monkeypatch.delenv("AGENTDECK_PASSFILE", raising=False)
        monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(tmp_path / ".secret"))
        sys.modules.pop("status_server", None)
        m = importlib.import_module("status_server")
        ht = tmp_path / "htpasswd"
        if htpasswd:
            subprocess.run(["htpasswd", "-cb", str(ht), "admin", "pw-123456"], check=True,
                           capture_output=True)
        monkeypatch.setattr(m, "HTPASSWD_FILE", str(ht))
        if seeded:
            m._pw_store("pw-123456")
        return m
    yield _load
    sys.modules.pop("status_server", None)


def test_title_is_agentdeck(load):
    html = load(passfile=True, htpasswd=False, seeded=True).login_html()
    assert "🛰 AgentDeck" in html and "🖥 Agents" not in html
    assert "<title>AgentDeck" in html


def test_passfile_mode_hides_the_username(load):
    html = load(passfile=True, htpasswd=False, seeded=True).login_html()
    assert 'name="user"' not in html and "Username" not in html
    assert 'name="pass"' in html and "autofocus" in html.split('name="pass"')[1].split(">")[0]


def test_htpasswd_mode_keeps_the_username(load):
    html = load(passfile=False, htpasswd=True).login_html()
    assert 'name="user"' in html and "Username" in html


def test_migration_state_keeps_the_username(load):
    m = load(passfile=True, htpasswd=True)           # passfile empty, htpasswd present
    assert m._pw_seedable()
    assert 'name="user"' in m.login_html()


def test_error_text_matches_the_mode(load):
    assert "Invalid password" in load(passfile=True, htpasswd=False, seeded=True).login_html(error=True)
    assert "Invalid username or password" in load(passfile=False, htpasswd=True).login_html(error=True)
