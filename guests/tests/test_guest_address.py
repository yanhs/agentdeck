"""A guest's address can be a name of its own (31.10.2026: a guest named after
her Telegram id, sb-g4242424242.<domain>; the owner asked for a normal one).

The guest keeps its name everywhere inside (container sbx-<name>, its disk, its pass);
only the addresses people see use the registry's "address": sb-<address>.<domain> for
the dashboard and <site>-<address>.<domain> for its sites.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

GUESTS = Path(__file__).resolve().parents[1]
for p in (GUESTS, GUESTS / "sitegate", GUESTS / "tgrelay", GUESTS / "turnstile"):
    sys.path.insert(0, str(p))
import guestctl as gc  # noqa: E402
import guestenv  # noqa: E402
import sitegate  # noqa: E402
import tgrelay  # noqa: E402

REG = {"g4242424242": {"port": 18102, "address": "mila"}, "anna": {"port": 18101}}


def test_the_address_comes_from_the_registry():
    assert guestenv.dashboard_host("g4242424242", REG) == "sb-mila.example.test"
    assert guestenv.dashboard_host("anna", REG) == "sb-anna.example.test"
    assert guestenv.dashboard_host("nobody", REG) == "sb-nobody.example.test"


def test_guestctl_uses_it(monkeypatch):
    monkeypatch.setattr(gc, "_registry", lambda: REG)
    assert gc.dashboard_host("g4242424242") == "sb-mila.example.test"


def test_the_guest_bot_gives_that_address(tmp_path):
    data, sites = tmp_path / "data", tmp_path / "sites"
    data.mkdir(), sites.mkdir()
    (data / "sandboxes.json").write_text(json.dumps(REG))
    (sites / "sb-mila.example.test.conf").write_text("server {}")
    info = tgrelay.dashboard_info("g4242424242", data, sites)
    assert info["url"] == "https://sb-mila.example.test"


def test_sites_use_it_too():
    assert sitegate.host_for("blog", "g4242424242", REG) == "blog-mila.example.test"
    assert sitegate.host_for("blog", "anna", REG) == "blog-anna.example.test"


@pytest.mark.parametrize("bad", ["", "Mila", "-x", "a" * 42, "sb.x", "anna", "g4242424242x/"])
def test_bad_or_taken_addresses_are_refused(bad):
    with pytest.raises(SystemExit):
        gc.check_address("g4242424242", bad, REG)


def test_a_guest_may_take_back_its_own_name():
    assert gc.check_address("g4242424242", "g4242424242", REG) == "g4242424242"
    assert gc.check_address("g4242424242", "mila", REG) == "mila"


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


def test_set_address_serves_the_new_one_and_moves_the_old(monkeypatch):
    reg = {"g4242424242": {"port": 18102}}
    monkeypatch.setattr(gc, "_registry", lambda: reg)
    monkeypatch.setattr(gc, "_save_registry", lambda r: reg.update(r))
    monkeypatch.setattr(gc, "_container_env",
                        lambda n: {"AGENTKENNEL_ORIGIN": "https://sb-g4242424242.example.test"})
    ops = FakeOps(["sb-g4242424242.example.test"])
    gc.set_address("g4242424242", "mila", ops)
    assert reg["g4242424242"]["address"] == "mila"
    final = {e[1]: e[2] for e in ops.log if e[0] == "vhost"}
    assert ("cert", "sb-mila.example.test") in ops.log
    assert "proxy_pass http://127.0.0.1:18102;" in final["sb-mila.example.test"]
    # the sandbox still knows its old address until it is recreated
    assert '"https://sb-g4242424242.example.test"' in final["sb-mila.example.test"]
    assert "return 301 https://sb-mila.example.test$request_uri;" in final["sb-g4242424242.example.test"]


def test_set_address_failing_keeps_the_old_one(monkeypatch):
    reg = {"g4242424242": {"port": 18102}}
    monkeypatch.setattr(gc, "_registry", lambda: json.loads(json.dumps(reg)))
    monkeypatch.setattr(gc, "_save_registry", lambda r: (reg.clear(), reg.update(r)))
    monkeypatch.setattr(gc, "_container_env", lambda n: {})
    ops = FakeOps(["sb-g4242424242.example.test"])

    def no_cert(host):
        raise RuntimeError("certbot: too many requests")
    ops.cert_issue = no_cert
    with pytest.raises(RuntimeError):
        gc.set_address("g4242424242", "mila", ops)
    assert "address" not in reg["g4242424242"]
    assert not [e for e in ops.log if e[0] == "vhost" and e[1] == "sb-g4242424242.example.test"]


def test_address_is_on_the_command_line():
    assert 'sub.add_parser("address")' in Path(gc.__file__).read_text()
