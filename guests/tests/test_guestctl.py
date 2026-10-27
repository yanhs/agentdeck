"""guestctl builds the guest container safely: own network, localhost-only port,
resource limits, turnstile addresses, and no host folders inside."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guestctl as gc  # noqa: E402


def args():
    return gc.container_args("anna", port=18101, secret="sbx-PASS", password="pw-1",
                             image="agentdeck-guest:latest", public_key="anna-k3y")


def pairs(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_runs_on_the_sandbox_network_only():
    argv = args()
    assert pairs(argv, "--network") == ["sbx"]
    assert "--privileged" not in argv
    assert "no-new-privileges:true" in pairs(argv, "--security-opt")


def test_port_is_published_on_localhost_only():
    assert pairs(args(), "-p") == ["127.0.0.1:18101:8765"]


def test_has_memory_cpu_and_process_limits():
    argv = args()
    assert pairs(argv, "--memory") == pairs(argv, "--memory-swap")  # no swap growth
    assert pairs(argv, "--cpus") and pairs(argv, "--pids-limit")


def test_talks_to_claude_and_telegram_only_through_the_turnstiles():
    env = dict(e.split("=", 1) for e in pairs(args(), "-e"))
    assert env["ANTHROPIC_BASE_URL"] == "http://172.31.250.1:8790"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sbx-PASS"
    assert env["TG_BRIDGE_API_BASE"] == "http://172.31.250.1:8792"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env


def test_only_named_volumes_and_the_public_folder():
    mounts = pairs(args(), "-v")
    host = [m for m in mounts if m.startswith("/")]
    assert host == [f"{gc.PUBLIC_ROOT}/anna-k3y:/work/public"]
    for mount in mounts:
        if not mount.startswith("/"):
            assert mount.split(":", 1)[0].startswith("sbx-anna-")


def test_public_link_address_is_given_to_the_agent():
    env = dict(e.split("=", 1) for e in pairs(args(), "-e"))
    assert env["PUBLIC_DIR"] == "/work/public"
    assert env["PUBLIC_URL"] == "https://sbx.reimake.com/f/anna-k3y"


def test_public_key_is_long_and_random():
    a, b = gc.new_public_key("anna"), gc.new_public_key("anna")
    assert a != b and a.startswith("anna-") and len(a) >= len("anna-") + 16


def test_owner_ids_are_validated():
    import pytest

    with pytest.raises(ValueError):
        gc.container_args("Bad Name", port=1, secret="s", password="p", image="i",
                          public_key="k")


def test_sandbox_gets_a_fixed_address_for_its_sites():
    argv = gc.container_args("anna", port=18101, secret="s", password="p", image="i",
                             public_key="k", ip="172.31.250.11")
    assert pairs(argv, "--ip") == ["172.31.250.11"]


def test_next_ip_takes_the_lowest_free_address():
    assert gc.next_ip(set()) == "172.31.250.11"
    assert gc.next_ip({"172.31.250.11", "172.31.250.13"}) == "172.31.250.12"


def test_dashboard_knows_its_public_address():
    env = dict(e.split("=", 1) for e in pairs(args(), "-e"))
    assert env["AGENTDECK_ORIGIN"] == "https://sb-anna.reimake.com"


def test_dashboard_vhost_points_at_the_localhost_port_only():
    text = gc.dashboard_vhost("anna", 18101)
    assert "server_name sb-anna.reimake.com;" in text
    assert "proxy_pass http://127.0.0.1:18101;" in text


def test_recreate_refuses_while_terminals_are_open(monkeypatch):
    import pytest

    monkeypatch.setattr(gc, "_registry", lambda: {"anna": {"port": 1, "telegram_id": 2}})
    monkeypatch.setattr(gc, "live_terminals", lambda name: ["cs-1234: 1 windows"])
    removed = []
    monkeypatch.setattr(gc, "_docker", lambda *a, **k: removed.append(a))
    args = type("A", (), {"name": "anna", "image": None, "force": False})()
    with pytest.raises(SystemExit) as exc:
        gc.cmd_recreate(args)
    assert "открыт" in str(exc.value)
    assert not any(a[:1] == ("rm",) for a in removed)


def test_an_init_process_reaps_orphans():
    """Services daemonize and leave orphans; without an init they'd pile up as zombies."""
    assert "--init" in args()


def test_the_agent_is_told_how_to_run_services():
    assert "<!-- sbx-services -->" in gc.SEED
    assert "/work/.services/" in gc.SEED
    assert "/services" in gc.SEED                 # where the human sees them
    assert "скажи" in gc.SEED                     # the agent must tell the human


def test_services_limit_is_one_number_everywhere():
    env = dict(e.split("=", 1) for e in pairs(args(), "-e"))
    assert gc.SERVICES_MAX == 10
    assert env["SERVICES_MAX"] == str(gc.SERVICES_MAX)      # the keeper reads it
    assert "SERVICES_MAX" in gc.SEED and "5 служб" not in gc.SEED   # the agent's rule too


def test_the_tmux_placeholder_is_not_a_terminal(monkeypatch):
    """hold-<hex> only keeps the tmux server up (library_cli.hold_server): it must not
    block a recreate forever."""
    class R:
        stdout = "hold-6141cd60: 1 windows (created x)\ncs-1cf36ae5: 1 windows (created y)\n"
    monkeypatch.setattr(gc, "_docker", lambda *a, **k: R())
    assert gc.live_terminals("anna") == ["cs-1cf36ae5: 1 windows (created y)"]
    R.stdout = "hold-6141cd60: 1 windows\n"
    assert gc.live_terminals("anna") == []


def test_process_limit_leaves_room():
    """4096 slots (threads count too: one Claude is ~15). With the init reaping orphans
    a sandbox uses ~40; the limit only stops a runaway (10 guests x 4096 is well under
    the host's threads-max of ~187k)."""
    assert pairs(args(), "--pids-limit") == ["4096"]


def test_new_guests_get_the_services_image():
    assert "svc" in gc.IMAGE                  # the keeper is in it (image/Dockerfile)


class FakeOps:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a))


def test_taking_the_address_down_never_falls_through():
    """Same order as sitegate: a 404 stub first (Cloudflare keeps routing the name for a
    while; without a vhost nginx would answer with another guest's login page), then DNS,
    then the stub and the certificate."""
    ops = FakeOps()
    gc.take_down_web("anna", ops, delay=0)
    names = [c[0] for c in ops.calls]
    assert names == ["write_vhost", "reload", "dns_delete", "remove_vhost", "reload",
                     "cert_delete"]
    assert ops.calls[0][1] == "sb-anna.reimake.com" and "404" in ops.calls[0][2]


def test_remove_takes_the_address_down_too(monkeypatch):
    """Otherwise sb-<name> keeps pointing at a freed port the next guest may get."""
    downs = []
    monkeypatch.setattr(gc, "_docker", lambda *a, **k: None)
    monkeypatch.setattr(gc, "_registry", lambda: {"anna": {"port": 18101}})
    monkeypatch.setattr(gc, "_save_registry", lambda reg: None)
    monkeypatch.setattr(gc, "web_exists", lambda name: True)
    monkeypatch.setattr(gc, "take_down_web", lambda name, ops=None, delay=90: downs.append(name))
    monkeypatch.setattr(gc.ts.GuestBook, "remove", lambda self, name: None)
    args = type("A", (), {"cmd": "remove", "name": "anna"})()
    gc.cmd_simple(args)
    assert downs == ["anna"]
