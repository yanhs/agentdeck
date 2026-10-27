"""guestenv: the module's settings come from guests/.env (the process environment wins),
and the owner is told things through a configurable channel."""
import json

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guestenv as ge  # noqa: E402


def write(tmp_path, text):
    p = tmp_path / ".env"
    p.write_text(text)
    return p


def test_reads_key_value_lines_comments_and_quotes(tmp_path):
    p = write(tmp_path, "# comment\nGUESTS_DOMAIN=example.org\n\nOWNER_TG_ID='42'\n"
                        'GUEST_BOT_TOKEN="1:abc"\nexport SERVER_IP=203.0.113.5\nnot a line\n')
    s = ge.load(p, environ={})
    assert s["GUESTS_DOMAIN"] == "example.org"
    assert s["OWNER_TG_ID"] == "42"
    assert s["GUEST_BOT_TOKEN"] == "1:abc"
    assert s["SERVER_IP"] == "203.0.113.5"


def test_missing_file_is_empty_settings_not_a_crash(tmp_path):
    assert ge.load(tmp_path / "nope.env", environ={}) == {}


def test_process_environment_wins_for_the_module_keys(tmp_path):
    p = write(tmp_path, "GUESTS_DOMAIN=from-file.org\n")
    s = ge.load(p, environ={"GUESTS_DOMAIN": "from-env.org", "PATH": "/bin"})
    assert s["GUESTS_DOMAIN"] == "from-env.org"
    assert "PATH" not in s


def test_settings_file_can_be_pointed_elsewhere(tmp_path, monkeypatch):
    p = write(tmp_path, "GUESTS_DOMAIN=elsewhere.org\n")
    monkeypatch.setenv("GUESTS_ENV_FILE", str(p))
    assert ge.env_file() == p
    assert ge.get("GUESTS_DOMAIN") == "elsewhere.org"


def test_default_settings_file_is_next_to_the_module(monkeypatch):
    monkeypatch.delenv("GUESTS_ENV_FILE")
    assert ge.env_file() == Path(ge.__file__).resolve().parent / ".env"


@pytest.mark.parametrize("raw,clean", [("Example.ORG", "example.org"), (" .example.org. ", "example.org"),
                                       ("https://example.org/", "example.org"), ("", "")])
def test_domain_is_normalised(tmp_path, monkeypatch, raw, clean):
    monkeypatch.setenv("GUESTS_ENV_FILE", str(write(tmp_path, f"GUESTS_DOMAIN={raw}\n")))
    assert ge.domain() == clean


def test_the_test_run_never_sees_a_real_env():
    assert ge.domain() == "example.test"
    assert ge.get("GUEST_BOT_TOKEN") is None


# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return type("R", (), {"returncode": self.rc, "stdout": "", "stderr": ""})()


def test_notify_runs_the_owners_command_with_the_text():
    run, posts = Runner(), []
    ge.notify_owner("привет", settings={"GUESTS_NOTIFY_CMD": 'tg "$1"'}, run=run,
                    post=lambda *a, **k: posts.append(a))
    assert run.calls == [["bash", "-c", 'tg "$1"', "_", "привет"]]
    assert posts == []


def test_a_failing_notify_command_raises():
    with pytest.raises(RuntimeError):
        ge.notify_owner("x", settings={"GUESTS_NOTIFY_CMD": "false"}, run=Runner(rc=1))


def test_notify_goes_to_the_admins_own_bot(tmp_path):
    """Decision 27.10: warnings go to the admin's own bot (AgentDeck's Telegram page)."""
    conf = tmp_path / "telegram.json"
    conf.write_text(json.dumps({"enabled": True, "token": "9:ADMIN", "owner": 42}))
    posts = []
    ge.notify_owner("hi", settings={"GUEST_BOT_TOKEN": "1:T", "OWNER_TG_ID": "42"},
                    run=Runner(), post=lambda url, data, timeout: posts.append((url, data)),
                    admin_conf=conf)
    assert posts == [("https://api.telegram.org/bot9:ADMIN/sendMessage", {"chat_id": "42", "text": "hi"})]


def test_notify_not_through_a_bot_of_someone_else(tmp_path):
    conf = tmp_path / "telegram.json"
    conf.write_text(json.dumps({"enabled": True, "token": "9:ADMIN", "owner": 7}))
    posts = []
    ge.notify_owner("hi", settings={"GUEST_BOT_TOKEN": "1:T", "OWNER_TG_ID": "42"},
                    run=Runner(), post=lambda url, data, timeout: posts.append((url, data)),
                    admin_conf=conf)
    assert posts[0][0] == "https://api.telegram.org/bot1:T/sendMessage"


def test_notify_without_the_admins_bot_falls_back_to_the_guest_bot(tmp_path):
    posts = []
    ge.notify_owner("hi", settings={"GUEST_BOT_TOKEN": "1:T", "OWNER_TG_ID": "42"},
                    run=Runner(), post=lambda url, data, timeout: posts.append((url, data)),
                    admin_conf=tmp_path / "none.json")
    (url, data), = posts
    assert url == "https://api.telegram.org/bot1:T/sendMessage"
    assert data == {"chat_id": "42", "text": "hi"}


def test_notify_with_nowhere_to_send_raises():
    with pytest.raises(RuntimeError):
        ge.notify_owner("x", settings={}, run=Runner(), post=lambda *a, **k: None,
                        admin_conf=Path("/nonexistent/telegram.json"))


def test_env_example_names_every_setting_and_holds_no_secret():
    text = (Path(ge.__file__).resolve().parent / ".env.example").read_text()
    for key in ge.KEYS:
        assert f"{key}=" in text, key
    values = ge.parse(text)
    assert not values.get("GUEST_BOT_TOKEN") and not values.get("OWNER_TG_ID")
    assert not values.get("GUESTS_DOMAIN")      # unfilled: doctor says "not set"
