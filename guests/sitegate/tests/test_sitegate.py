"""Site gate: a guest's agent asks to publish a site from its sandbox; the host
creates <site>-<guest>.reimake.com (DNS, nginx, certificate) pointing at that
sandbox only, and tells the owner. Server operations are faked here."""

import json
import sys
import threading
import http.client
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "turnstile"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import turnstile as ts  # noqa: E402

import sitegate as sg  # noqa: E402


class FakeOps:
    def __init__(self):
        self.log = []
        self.fail_cert = False

    def dns_create(self, host):
        self.log.append(("dns+", host))

    def dns_delete(self, host):
        self.log.append(("dns-", host))

    def write_vhost(self, host, text):
        self.log.append(("vhost", host, text))

    def remove_vhost(self, host):
        self.log.append(("vhost-", host))

    def reload(self):
        self.log.append(("reload",))

    def cert_issue(self, host):
        if self.fail_cert:
            raise RuntimeError("certbot failed")
        self.log.append(("cert+", host))

    def cert_delete(self, host):
        self.log.append(("cert-", host))

    def notify(self, text):
        self.log.append(("notify", text))


@pytest.fixture
def book(tmp_path):
    return ts.GuestBook(tmp_path / "guests.json")


@pytest.fixture
def passes(book):
    return {"anna": book.add("anna", 10, telegram_id=1),
            "boris": book.add("boris", 10, telegram_id=2)}


@pytest.fixture
def ops():
    return FakeOps()


@pytest.fixture
def gate(tmp_path, book, ops, passes):
    ips = {"anna": "172.31.250.11", "boris": "172.31.250.12"}
    return sg.Gate(book, ops, tmp_path / "sites.json", ip_of=ips.get, cleanup_delay=0)


# --------------------------------------------------------------------------- #
class TestNames:
    @pytest.mark.parametrize("name", ["test", "shop2", "my-site", "a"])
    def test_good_site_names(self, name):
        assert sg.validate_site(name) == name

    @pytest.mark.parametrize("name", ["", "-x", "x-", "Test", "a.b", "a b",
                                      "x" * 25, "../etc", "a;reload"])
    def test_bad_site_names(self, name):
        with pytest.raises(ValueError):
            sg.validate_site(name)

    @pytest.mark.parametrize("port", [0, 80, 1023, 65536, "3000; rm"])
    def test_bad_ports(self, port):
        with pytest.raises(ValueError):
            sg.validate_port(port)

    def test_host_is_site_dash_guest_on_reimake(self):
        assert sg.host_for("test", "anna") == "test-anna.reimake.com"


class TestVhost:
    def test_https_vhost_points_only_at_the_guest_sandbox(self):
        text = sg.render_vhost("test-anna.reimake.com", "172.31.250.11", 3000, tls=True)
        assert "server_name test-anna.reimake.com;" in text
        assert "proxy_pass http://172.31.250.11:3000;" in text
        assert "/etc/letsencrypt/live/test-anna.reimake.com/fullchain.pem" in text
        assert "Upgrade" in text  # websockets for dev servers
        assert "alias" not in text and " root /home" not in text

    def test_http_only_vhost_for_the_first_certificate(self):
        text = sg.render_vhost("test-anna.reimake.com", "172.31.250.11", 3000, tls=False)
        assert "acme-challenge" in text and "443" not in text


class TestPublish:
    def test_publish_creates_dns_cert_vhost_and_tells_the_owner(self, gate, ops):
        res = gate.publish("anna", "test", 3000)
        assert res["url"] == "https://test-anna.reimake.com"
        kinds = [entry[0] for entry in ops.log]
        assert kinds.index("dns+") < kinds.index("cert+") < kinds.index("notify")
        final = [e for e in ops.log if e[0] == "vhost"][-1][2]
        assert "listen 443" in final and "172.31.250.11:3000" in final
        note = [e for e in ops.log if e[0] == "notify"][0][1]
        assert "anna" in note and "test-anna.reimake.com" in note

    def test_republish_same_site_only_changes_the_port(self, gate, ops):
        gate.publish("anna", "test", 3000)
        ops.log.clear()
        gate.publish("anna", "test", 5173)
        assert not any(e[0] in ("dns+", "cert+") for e in ops.log)
        assert "172.31.250.11:5173" in [e for e in ops.log if e[0] == "vhost"][-1][2]

    def test_site_limit_per_guest(self, gate):
        for i in range(sg.MAX_SITES):
            gate.publish("anna", f"s{i}", 3000)
        with pytest.raises(sg.GateError):
            gate.publish("anna", "onemore", 3000)

    def test_failed_certificate_rolls_back(self, gate, ops):
        ops.fail_cert = True
        with pytest.raises(sg.GateError):
            gate.publish("anna", "test", 3000)
        kinds = [e[0] for e in ops.log]
        assert "dns-" in kinds and "vhost-" in kinds
        assert gate.sites("anna") == []

    def test_unpublish_only_own_site(self, gate, ops):
        gate.publish("anna", "test", 3000)
        with pytest.raises(sg.GateError):
            gate.unpublish("boris", "test")
        gate.unpublish("anna", "test")
        kinds = [e[0] for e in ops.log]
        assert "dns-" in kinds and "cert-" in kinds and "vhost-" in kinds
        assert gate.sites("anna") == []

    def test_unpublish_puts_a_404_stub_before_dns_goes(self, gate, ops):
        """Cloudflare keeps sending the name for a few seconds after the DNS delete;
        without a vhost nginx would serve its FIRST site (another dashboard's login)."""
        gate.publish("anna", "test", 3000)
        ops.log.clear()
        gate.unpublish("anna", "test")
        kinds = [e[0] for e in ops.log]
        stub = [e for e in ops.log if e[0] == "vhost"][0][2]
        assert "return 404" in stub and "proxy_pass" not in stub
        assert kinds.index("vhost") < kinds.index("dns-") < kinds.index("vhost-")

    def test_disabled_guest_cannot_publish(self, gate, book):
        book.set_enabled("anna", False)
        with pytest.raises(sg.GateError):
            gate.publish("anna", "test", 3000)

    def test_guest_without_sandbox_ip_cannot_publish(self, tmp_path, book, ops, passes):
        g = sg.Gate(book, ops, tmp_path / "s.json", ip_of=lambda name: None)
        with pytest.raises(sg.GateError):
            g.publish("anna", "test", 3000)


# --------------------------------------------------------------------------- #
@pytest.fixture
def server(gate):
    srv = sg.make_server("127.0.0.1", 0, gate)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def req(server, method, path, secret=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    headers = {"content-type": "application/json"}
    if secret:
        headers["authorization"] = f"Bearer {secret}"
    c.request(method, path, body=json.dumps(body) if body is not None else None,
              headers=headers)
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


class TestHttp:
    def test_no_pass_is_401(self, server):
        assert req(server, "POST", "/publish", body={"site": "t", "port": 3000})[0] == 401

    def test_publish_list_unpublish(self, server, passes):
        p = passes["anna"]
        status, body = req(server, "POST", "/publish", p, {"site": "test", "port": 3000})
        assert status == 200 and body["url"] == "https://test-anna.reimake.com"
        status, body = req(server, "GET", "/sites", p)
        assert [s["url"] for s in body["sites"]] == ["https://test-anna.reimake.com"]
        status, _ = req(server, "DELETE", "/publish/test", p)
        assert status == 200

    def test_bad_input_is_400_with_a_reason(self, server, passes):
        status, body = req(server, "POST", "/publish", passes["anna"],
                           {"site": "Bad Name", "port": 3000})
        assert status == 400 and body["error"]
