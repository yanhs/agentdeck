"""The owner opens a guest's dashboard without the guest's password (decision 29.10.2026).

`guestctl.py session <name>` signs a dashboard login for the owner with the sandbox's own
cookie key (/app/.sessions/.agents_auth_secret inside the container, the same in every
AgentKennel version that has guests), so nothing in the sandbox changes: the guest's
password stays as it is and no container is recreated. The panel hands the signed cookie
over on the guest's own address, at one path that the guest's nginx sends to the panel:
/_owner-entry -> 127.0.0.1:3011/api/guests/enter (tests/test_guests_api.py).
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import sys
from pathlib import Path

import pytest

GUESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GUESTS))
sys.path.insert(0, str(GUESTS / "sitegate"))
import guestctl as gc  # noqa: E402
import sitegate  # noqa: E402

SECRET = b"9f" * 32


def https_block(text):
    return text[text.index("listen 443"):]


# ── the guest's nginx: one extra path, and only on the dashboard ──────────────
def test_dashboard_vhost_sends_the_entry_path_to_the_panel():
    text = gc.dashboard_vhost("anna", 18101)
    https = https_block(text)
    assert "location = /_owner-entry {" in https
    entry = https[https.index("location = /_owner-entry {"):]
    entry = entry[:entry.index("}") + 1]
    assert "proxy_pass http://127.0.0.1:3011/api/guests/enter;" in entry
    assert "proxy_set_header Host $host;" in entry
    # exact match, before the catch-all: nothing else of the panel is reachable from here
    assert https.index("location = /_owner-entry") < https.index("location / {")
    assert text.count("3011") == 1
    assert "proxy_pass http://127.0.0.1:18101;" in https


def test_the_panel_answers_where_the_guest_nginx_sends(tmp_path, monkeypatch):
    """One path in two programs: guestctl writes it into nginx, status_server serves it."""
    monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(tmp_path / "k"))
    sys.path.insert(0, str(GUESTS.parent))
    sys.modules.pop("status_server", None)
    ss = importlib.import_module("status_server")
    try:
        assert ss.GUEST_ENTRY_PATH == gc.OWNER_ENTRY
        assert gc.OWNER_PANEL_ENTRY == "http://127.0.0.1:3011" + ss.GUEST_ENTRY_ROUTE
        assert ss.GUEST_ENTRY_COOKIE_RE.fullmatch(gc.owner_session("anna", secret=SECRET)["cookie"])
    finally:
        sys.modules.pop("status_server", None)


def test_plain_http_only_redirects():
    http = gc.dashboard_vhost("anna", 18101).split("listen 443")[0]
    assert "_owner-entry" not in http and "3011" not in http


def test_guests_published_sites_have_no_entry_path():
    text = sitegate.render_vhost("blog-anna.example.test", "172.31.250.11", 8080, tls=True)
    assert "_owner-entry" not in text and "3011" not in text


# ── the signed login ──────────────────────────────────────────────────────────
def test_owner_session_is_signed_with_the_sandbox_key():
    s = gc.owner_session("anna", now=1_000_000, secret=SECRET)
    assert s["host"] == "sb-anna.example.test"
    assert s["max_age"] == gc.OWNER_TTL == 12 * 3600
    name, value = s["cookie"].split("=", 1)
    assert name == "agents_session"
    user, exp, sig = value.split(".")
    assert user == "owner" and int(exp) == 1_000_000 + 12 * 3600
    assert sig == hmac.new(SECRET, f"owner.{exp}".encode(), hashlib.sha256).hexdigest()


def test_the_guest_dashboard_accepts_it(tmp_path, monkeypatch):
    """Checked by AgentKennel's own verifier, not by a copy of it."""
    key = tmp_path / "auth_secret"
    key.write_bytes(SECRET + b"\n")                   # the file ends with a newline
    monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(key))
    sys.path.insert(0, str(GUESTS.parent))
    sys.modules.pop("status_server", None)
    ss = importlib.import_module("status_server")
    try:
        cookie = gc.owner_session("anna", secret=SECRET)["cookie"]
        assert ss._verify_token(cookie.split("=", 1)[1]) == "owner"
        assert ss._verify_token(gc.owner_session("anna", secret=b"other")["cookie"].split("=", 1)[1]) is None
    finally:
        sys.modules.pop("status_server", None)


def test_the_key_is_read_from_inside_the_sandbox(monkeypatch):
    calls = []

    class R:
        returncode, stdout, stderr = 0, SECRET + b"\n", b""

    def run(argv, **kw):
        calls.append(argv)
        return R()
    monkeypatch.setattr(gc.subprocess, "run", run)
    assert gc.sandbox_auth_secret("anna") == SECRET
    argv = calls[0]
    assert argv[:3] == ["docker", "exec", "sbx-anna"]
    assert "/app/.sessions/.agents_auth_secret" in " ".join(argv)


@pytest.mark.parametrize("env,want", [
    ({}, "default"),
    ({"AGENTDECK_AUTH_SECRET": "old"}, "old"),                        # sandboxes up to 2.0.x
    ({"AGENTKENNEL_AUTH_SECRET": "new", "AGENTDECK_AUTH_SECRET": "old"}, "new"),   # 2.1+
])
def test_the_key_path_follows_the_sandbox_s_own_setting(monkeypatch, tmp_path, env, want):
    """The command runs in the sandbox's shell: run the same line here, with files."""
    files = {k: tmp_path / k for k in ("default", "old", "new")}
    for k, f in files.items():
        f.write_text(k)
    seen, real_run = [], gc.subprocess.run

    class R:
        returncode, stderr = 0, b""

    def run(argv, **kw):
        line = argv[-1].replace(gc.SANDBOX_AUTH_SECRET, str(files["default"]))
        r = R()
        r.stdout = real_run(
            ["sh", "-c", line], capture_output=True,
            env={k: str(files[v]) for k, v in env.items()} | {"PATH": "/usr/bin:/bin"}).stdout
        seen.append(argv[:3])
        return r
    monkeypatch.setattr(gc.subprocess, "run", run)
    assert gc.sandbox_auth_secret("anna") == want.encode()
    assert seen == [["docker", "exec", "sbx-anna"]]


@pytest.mark.parametrize("rc,out,err", [(1, b"", b"Error: No such container: sbx-anna"),
                                        (0, b"\n", b""),
                                        (0, b"x" * 5000, b"")],
                         ids=["no-container", "empty", "too-long"])
def test_no_usable_key_no_session(monkeypatch, rc, out, err):
    class R:
        returncode, stdout, stderr = rc, out, err
    monkeypatch.setattr(gc.subprocess, "run", lambda argv, **kw: R())
    with pytest.raises(SystemExit) as e:
        gc.sandbox_auth_secret("anna")
    assert "anna" in str(e.value)


# ── the command the panel runs ────────────────────────────────────────────────
@pytest.fixture
def data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(gc, "DATA", d)
    monkeypatch.setattr(gc, "REGISTRY", d / "sandboxes.json")
    (d / "sandboxes.json").write_text(json.dumps({"anna": {"port": 18101, "telegram_id": 111}}))
    monkeypatch.setattr(gc, "sandbox_auth_secret", lambda name: SECRET)
    return d


class FakeOps:
    def __init__(self, vhosts):
        self.VHOSTS = vhosts
        self.done = []

    def write_vhost(self, host, text):
        (self.VHOSTS / f"{host}.conf").write_text(text)
        self.done.append(("write", host))

    def reload(self):
        self.done.append(("reload",))


@pytest.fixture
def ops(tmp_path, monkeypatch):
    v = tmp_path / "sites"
    v.mkdir()
    o = FakeOps(v)
    monkeypatch.setattr(gc, "_site_ops", lambda: o)
    return o


def test_session_prints_json(data, ops, capsys):
    assert gc.main(["session", "anna"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"host", "cookie", "max_age"}
    assert out["host"] == "sb-anna.example.test" and out["cookie"].startswith("agents_session=owner.")


def test_session_of_an_unknown_guest_is_refused(data, ops):
    with pytest.raises(SystemExit):
        gc.main(["session", "boris"])


def test_an_older_dashboard_vhost_gets_the_entry_path(data, ops, capsys):
    """Guests made before the Open button: their page is brought up to date once."""
    conf = ops.VHOSTS / "sb-anna.example.test.conf"
    conf.write_text(sitegate.render_vhost("sb-anna.example.test", "127.0.0.1", 18101, tls=True))
    assert gc.main(["session", "anna"]) == 0
    assert "location = /_owner-entry" in conf.read_text()
    assert ops.done == [("write", "sb-anna.example.test"), ("reload",)]
    assert gc.main(["session", "anna"]) == 0          # up to date: nothing more to do
    assert ops.done == [("write", "sb-anna.example.test"), ("reload",)]


def test_a_page_still_waiting_for_its_certificate_is_left_alone(data, ops):
    conf = ops.VHOSTS / "sb-anna.example.test.conf"
    pending = sitegate.render_vhost("sb-anna.example.test", "127.0.0.1", 18101, tls=False)
    conf.write_text(pending)
    gc.main(["session", "anna"])
    assert conf.read_text() == pending and ops.done == []


def test_a_page_being_taken_down_is_left_alone(data, ops):
    """`remove` puts a 404 stub on the name first (it has an https block too): Open must
    never bring a removed guest's page back."""
    conf = ops.VHOSTS / "sb-anna.example.test.conf"
    gone = sitegate.render_gone("sb-anna.example.test")
    conf.write_text(gone)
    gc.main(["session", "anna"])
    assert conf.read_text() == gone and ops.done == []


def test_no_page_no_change(data, ops):
    gc.main(["session", "anna"])
    assert ops.done == [] and not list(ops.VHOSTS.iterdir())
