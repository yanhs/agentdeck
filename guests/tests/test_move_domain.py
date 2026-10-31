"""Guests move to another domain (31.10.2026: GUESTS_DOMAIN changed) without a
sandbox being restarted: a guest may be in the middle of something.

A sandbox keeps the address it was made with in its own settings (AGENTKENNEL_ORIGIN, the
one Origin its dashboard takes writes from) until it is recreated. So the new address
hands the dashboard the Origin it knows, the old address sends everyone to the new one,
and the agent's rules name the new domain. The next recreate takes the new address and
writes the dashboard's nginx again, without the stand-in.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

GUESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GUESTS))
sys.path.insert(0, str(GUESTS / "sitegate"))
import guestctl as gc  # noqa: E402
import sitegate  # noqa: E402


def https_block(text):
    return text[text.index("listen 443"):]


def proxy_location(text):
    https = https_block(text)
    return https[https.index("location / {"):]


# ── the dashboard's nginx follows the sandbox's own origin ────────────────────
def test_sandbox_made_for_this_address_gets_no_stand_in():
    text = gc.dashboard_vhost("anna", 18101, container_origin="https://sb-anna.example.test")
    assert "Origin" not in text


def test_sandbox_made_for_the_old_address_is_handed_the_origin_it_knows():
    loc = proxy_location(gc.dashboard_vhost("anna", 18101, container_origin="https://sb-anna.old.test"))
    assert 'set $sbx_origin $http_origin;' in loc
    assert 'if ($http_origin = "https://sb-anna.example.test") { set $sbx_origin "https://sb-anna.old.test"; }' in loc
    assert "proxy_set_header Origin $sbx_origin;" in loc
    # only the dashboard's own address is swapped: any other Origin goes through as it is
    assert loc.index("set $sbx_origin $http_origin;") < loc.index("if ($http_origin")


def test_terminal_connections_keep_their_own_origin():
    """The terminal (ttyd, a websocket) checks Origin against Host: it must get the real one."""
    loc = proxy_location(gc.dashboard_vhost("anna", 18101, container_origin="https://sb-anna.old.test"))
    swap = loc.index('if ($http_origin = "https://sb-anna.example.test")')
    keep = loc.index('if ($http_upgrade) { set $sbx_origin $http_origin; }')
    assert swap < keep < loc.index("proxy_set_header Origin $sbx_origin;")


def test_unknown_origin_gets_no_stand_in():
    """No container to ask (or no setting in it): the dashboard judges by the Host."""
    assert "Origin" not in gc.dashboard_vhost("anna", 18101, container_origin="")


def test_by_default_the_origin_is_read_from_the_container(monkeypatch):
    monkeypatch.setattr(gc, "_container_env", lambda name: {"AGENTKENNEL_ORIGIN": "https://sb-anna.old.test"})
    assert "https://sb-anna.old.test" in gc.dashboard_vhost("anna", 18101)
    monkeypatch.setattr(gc, "_container_env", lambda name: {"AGENTDECK_ORIGIN": "https://sb-anna.old.test"})
    assert "https://sb-anna.old.test" in gc.dashboard_vhost("anna", 18101)

    def gone(name):
        raise gc.subprocess.CalledProcessError(1, "docker")
    monkeypatch.setattr(gc, "_container_env", gone)
    assert "Origin" not in gc.dashboard_vhost("anna", 18101)


# ── the old address ───────────────────────────────────────────────────────────
def test_old_address_sends_everyone_to_the_new_one():
    text = sitegate.render_moved("sb-anna.old.test", "sb-anna.example.test")
    assert text.count("return 301 https://sb-anna.example.test$request_uri;") == 2   # http and https
    assert "ssl_certificate /etc/letsencrypt/live/sb-anna.old.test/fullchain.pem;" in text
    assert "server_name sb-anna.old.test;" in text
    assert "proxy_pass" not in text
    # the certificate still renews over http
    assert "location ^~ /.well-known/acme-challenge/ { root /var/www/html; }" in text


# ── the agent's rules ─────────────────────────────────────────────────────────
RULES_OLD = """# мои заметки
<!-- sbx-public-files -->
`https://sbx.old.test/f/anna-KEY/<имя файла>` последней строкой сообщения.
<!-- sbx-sites -->
## Сайт в интернет: <сайт>-<гость>.old.test
ответ даст адрес https://test-anna.old.test
мой собственный сайт на old.test не трогать
"""


def test_rules_name_the_new_domain():
    out = gc.rules_on_new_domain(RULES_OLD, "old.test", "example.test")
    assert "https://sbx.example.test/f/anna-KEY/" in out
    assert "## Сайт в интернет: <сайт>-<гость>.example.test" in out
    # what the guest wrote themselves stays theirs
    assert "мой собственный сайт на old.test не трогать" in out
    assert "# мои заметки" in out


def test_rules_already_on_the_new_domain_are_left_alone():
    once = gc.rules_on_new_domain(RULES_OLD, "old.test", "example.test")
    assert gc.rules_on_new_domain(once, "old.test", "example.test") == once


# ── recreate writes the dashboard's nginx again ───────────────────────────────
def test_recreate_rewrites_the_dashboard_vhost(monkeypatch):
    calls = []

    class Ops:
        def write_vhost(self, host, text):
            calls.append(("vhost", host, text))

        def reload(self):
            calls.append(("reload",))

    monkeypatch.setattr(gc, "_site_ops", lambda: Ops())
    monkeypatch.setattr(gc, "web_exists", lambda name: True)
    monkeypatch.setattr(gc, "_container_env", lambda name: {"AGENTKENNEL_ORIGIN": "https://sb-anna.example.test"})
    gc.refresh_web("anna", 18101)
    assert calls[0][:2] == ("vhost", "sb-anna.example.test")
    assert "Origin" not in calls[0][2] and "_owner-entry" in calls[0][2]
    assert calls[-1] == ("reload",)


def test_refresh_leaves_a_guest_without_an_address_alone(monkeypatch):
    monkeypatch.setattr(gc, "web_exists", lambda name: False)
    monkeypatch.setattr(gc, "_site_ops", lambda: pytest.fail("no address, nothing to write"))
    gc.refresh_web("anna", 18101)


def test_recreate_calls_refresh(monkeypatch):
    src = Path(gc.__file__).read_text()
    body = src[src.index("def cmd_recreate"):src.index("def container_state")]
    assert "refresh_web(a.name, info[\"port\"])" in body


# ── move-domain ───────────────────────────────────────────────────────────────
class FakeOps:
    def __init__(self, existing):
        self.existing = set(existing)
        self.log = []

    def vhost_exists(self, host):
        return host in self.existing

    def dns_create(self, host):
        self.log.append(("dns", host))

    def write_vhost(self, host, text):
        self.log.append(("vhost", host, text))

    def reload(self):
        self.log.append(("reload",))

    def cert_issue(self, host):
        self.log.append(("cert", host))


def test_move_domain_refuses_the_same_domain():
    with pytest.raises(SystemExit):
        gc.move_domain("example.test", ops=FakeOps([]), guests={}, fix_rules=lambda n, o, d: None)


def test_move_domain_moves_files_and_every_dashboard(monkeypatch):
    monkeypatch.setattr(gc, "_container_env", lambda name: {"AGENTKENNEL_ORIGIN": f"https://sb-{name}.old.test"})
    ops = FakeOps(["sbx.old.test", "sb-anna.old.test", "sb-bob.old.test"])
    fixed = []
    gc.move_domain("old.test", ops=ops,
                   guests={"anna": {"port": 18101}, "bob": {"port": 18102}, "cleo": {"port": 18103}},
                   fix_rules=lambda name, old, new: fixed.append((name, old, new)))
    final = {}
    for entry in ops.log:
        if entry[0] == "vhost":
            final[entry[1]] = entry[2]
    # new addresses: DNS and a certificate, then the full config
    for host in ("sbx.example.test", "sb-anna.example.test", "sb-bob.example.test"):
        assert ("dns", host) in ops.log and ("cert", host) in ops.log
    assert "alias " in final["sbx.example.test"]
    assert "proxy_pass http://127.0.0.1:18101;" in final["sb-anna.example.test"]
    assert '"https://sb-anna.old.test"' in final["sb-anna.example.test"]          # the stand-in
    # old addresses: only after the new one is served, and only to it
    assert "return 301 https://sb-anna.example.test$request_uri;" in final["sb-anna.old.test"]
    assert "return 301 https://sbx.example.test$request_uri;" in final["sbx.old.test"]
    assert ops.log.index(("cert", "sb-anna.example.test")) < \
        next(i for i, e in enumerate(ops.log) if e[:2] == ("vhost", "sb-anna.old.test"))
    # a guest who never had an address doesn't get one now
    assert not any("cleo" in str(e) for e in ops.log)
    assert sorted(fixed) == [("anna", "old.test", "example.test"), ("bob", "old.test", "example.test"),
                             ("cleo", "old.test", "example.test")]


def test_move_domain_is_on_the_command_line():
    src = Path(gc.__file__).read_text()
    assert 'sub.add_parser("move-domain")' in src
