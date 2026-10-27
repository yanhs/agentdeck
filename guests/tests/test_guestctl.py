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
    assert env["PUBLIC_URL"] == "https://sbx.example.test/f/anna-k3y"


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
    assert env["AGENTDECK_ORIGIN"] == "https://sb-anna.example.test"


def test_dashboard_vhost_points_at_the_localhost_port_only():
    text = gc.dashboard_vhost("anna", 18101)
    assert "server_name sb-anna.example.test;" in text
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


def test_new_guests_get_the_image_of_this_agentdeck_version(monkeypatch):
    """agentdeck-guest:<version> is built from this repo by install.sh (its keeper is the
    core services_keeper.py); GUEST_IMAGE in guests/.env picks another one."""
    monkeypatch.setattr(gc, "repo_version", lambda: "v2.0.0")
    assert gc.default_image() == "agentdeck-guest:v2.0.0"
    monkeypatch.setenv("GUEST_IMAGE", "my-guest:1")
    assert gc.default_image() == "my-guest:1"


def test_repo_version_is_a_valid_image_tag():
    import re
    assert re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", gc.repo_version())


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
    assert ops.calls[0][1] == "sb-anna.example.test" and "404" in ops.calls[0][2]


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


# --------------------------------------------------------------------------- #
# configuration instead of one server's names
# --------------------------------------------------------------------------- #
def test_the_core_services_keeper_is_reachable_from_the_host():
    """The host's Telegram relay asks the sandbox keeper at <sandbox ip>:9876 (/services);
    the core keeper listens on localhost unless told otherwise."""
    env = dict(e.split("=", 1) for e in pairs(args(), "-e"))
    assert env["SERVICES_BIND"] == "0.0.0.0"


def test_without_a_domain_a_sandbox_is_not_made(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setenv("GUESTS_ENV_FILE", str(tmp_path / "none.env"))
    with pytest.raises(SystemExit) as exc:
        args()
    assert "GUESTS_DOMAIN" in str(exc.value)


def test_the_seed_gets_the_domain_for_the_agents_site_rule():
    argv = gc.seed_argv("anna", "sbx-PASS", 42)
    assert argv[:6] == ["docker", "exec", "sbx-anna", "python3", "-c", gc.SEED]
    assert argv[6:] == ["sbx-PASS", "42", "example.test"]
    assert "sys.argv[3]" in gc.SEED


def test_the_wall_check_names_only_the_module_s_own_walls():
    """turnstiles, the host's ssh and nginx, the internet — nothing of another setup."""
    import re
    labels = re.findall(r'\("([^"]+)", "[^"]+", \d+\)', gc.CHECK)
    assert labels == ["турникет Claude", "турникет Телеграм", "ssh сервера", "nginx сервера",
                      "интернет (example.com:443)"]


def test_files_vhost_is_rendered_for_the_domain_and_public_folder():
    text = gc.files_vhost()
    assert "server_name sbx.example.test;" in text
    assert f"alias {gc.PUBLIC_ROOT}/;" in text
    assert "disable_symlinks on;" in text and "@" not in text.replace("$", "")


# --------------------------------------------------------------------------- #
# list --json, limit
# --------------------------------------------------------------------------- #
import json  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(gc, "DATA", d)
    monkeypatch.setattr(gc, "REGISTRY", d / "sandboxes.json")
    return d


def test_list_json_shape(data, monkeypatch, capsys):
    book = gc.ts.GuestBook(data / "guests.json")
    book.add("anna", 20_000_000, 111)
    book.add("boris", 5_000, 222)
    book.set_enabled("boris", False)
    gc.ts.Ledger(data / "usage.db").record("anna", "opus", {"input_tokens": 1000})
    (data / "sandboxes.json").write_text(json.dumps({
        "anna": {"port": 18101, "telegram_id": 111, "created": "2026-10-27 08:54:00"},
        "boris": {"port": 18102, "telegram_id": 222, "created": "2026-10-27 09:00:00"}}))
    states = {"anna": "running", "boris": ""}
    monkeypatch.setattr(gc, "container_state", lambda name: states[name] or "missing")
    monkeypatch.setattr(gc, "web_exists", lambda name: name == "anna")
    assert gc.main(["list", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"guests": [
        {"name": "anna", "telegram_id": 111, "port": 18101, "state": "running",
         "enabled": True, "spent_today": 1000, "limit": 20_000_000,
         "url": "https://sb-anna.example.test", "created": "2026-10-27 08:54:00"},
        {"name": "boris", "telegram_id": 222, "port": 18102, "state": "missing",
         "enabled": False, "spent_today": 0, "limit": 5_000, "url": None,
         "created": "2026-10-27 09:00:00"}]}


def test_list_json_with_no_guests_yet(data, capsys):
    assert gc.main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"guests": []}


def test_container_state_is_missing_without_a_container(monkeypatch):
    class R:
        stdout, returncode = "", 1
    monkeypatch.setattr(gc, "_docker", lambda *a, **k: R())
    assert gc.container_state("anna") == "missing"
    R.stdout, R.returncode = "exited\n", 0
    assert gc.container_state("anna") == "exited"


def test_limit_changes_the_daily_limit(data, capsys):
    book = gc.ts.GuestBook(data / "guests.json")
    book.add("anna", 20_000_000, 111)
    assert gc.main(["limit", "anna", "35000000"]) == 0
    assert gc.ts.GuestBook(data / "guests.json").get("anna").daily_limit == 35_000_000
    assert "35000000" in capsys.readouterr().out


def test_limit_of_an_unknown_guest_is_refused(data):
    with pytest.raises(SystemExit) as exc:
        gc.main(["limit", "nobody", "10"])
    assert "nobody" in str(exc.value)


def test_limit_must_not_be_negative(data):
    gc.ts.GuestBook(data / "guests.json").add("anna", 1, 1)
    with pytest.raises(SystemExit):
        gc.main(["limit", "anna", "-5"])


# --------------------------------------------------------------------------- #
# doctor: the setup wizard's checks
# --------------------------------------------------------------------------- #
class Run:
    def __init__(self, rc=0, out="", err="", missing=False):
        self.rc, self.out, self.err, self.missing, self.calls = rc, out, err, missing, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.missing:
            raise FileNotFoundError(argv[0])
        return type("R", (), {"returncode": self.rc, "stdout": self.out, "stderr": self.err})()


def keys(check):
    return set(check)


def test_docker_check():
    ok = gc.check_docker(Run(out="27.3.1\n"))
    assert ok["ok"] and ok["id"] == "docker" and "27.3.1" in ok["detail"] and ok["fix"] == ""
    denied = gc.check_docker(Run(rc=1, err="permission denied while trying to connect"))
    assert not denied["ok"] and "usermod -aG docker" in denied["fix"]
    absent = gc.check_docker(Run(missing=True))
    assert not absent["ok"] and "install" in absent["fix"].lower()


def test_bot_check():
    good = gc.check_bot({"GUEST_BOT_TOKEN": "1:T"}, get_me=lambda token: "my_guest_bot")
    assert good["ok"] and good["detail"] == "@my_guest_bot"
    unset = gc.check_bot({}, get_me=lambda token: pytest.fail("no call without a token"))
    assert not unset["ok"] and "BotFather" in unset["fix"] and "GUEST_BOT_TOKEN" in unset["fix"]

    def refuse(token):
        raise RuntimeError("401 Unauthorized")
    bad = gc.check_bot({"GUEST_BOT_TOKEN": "1:T"}, get_me=refuse)
    assert not bad["ok"] and "401" in bad["detail"] and "GUEST_BOT_TOKEN" in bad["fix"]


def test_get_me_sends_a_user_agent_and_a_short_timeout():
    seen = {}

    def opener(req, timeout):
        seen.update(ua=req.get_header("User-agent"), timeout=timeout, url=req.full_url)

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true, "result": {"username": "my_guest_bot"}}'
        return Resp()
    assert gc.telegram_get_me("1:T", opener=opener) == "my_guest_bot"
    assert seen["url"] == "https://api.telegram.org/bot1:T/getMe"
    assert seen["timeout"] <= 10 and "urllib" not in (seen["ua"] or "urllib").lower()


@pytest.mark.parametrize("value,ok", [("123456789", True), ("", False), ("@me", False),
                                      (None, False)])
def test_owner_check(value, ok):
    c = gc.check_owner({"OWNER_TG_ID": value} if value is not None else {})
    assert c["ok"] is ok and c["id"] == "owner"
    if not ok:
        assert "OWNER_TG_ID" in c["fix"]


def test_domain_check_resolves_to_this_server():
    resolve = {"sb-test.example.org": ["203.0.113.5"]}.get
    c = gc.check_domain("example.org", "203.0.113.5", resolve=lambda h: resolve(h) or [])
    assert c["ok"] and "203.0.113.5" in c["detail"]


def test_domain_check_falls_back_to_the_domain_itself():
    names = {"example.org": ["203.0.113.5"]}
    c = gc.check_domain("example.org", "203.0.113.5", resolve=lambda h: names.get(h, []))
    assert c["ok"]


def test_domain_elsewhere_fails_unless_cloudflare_manages_it():
    names = {"example.org": ["104.16.0.1"]}
    c = gc.check_domain("example.org", "203.0.113.5", resolve=lambda h: names.get(h, []))
    assert not c["ok"] and "104.16.0.1" in c["detail"] and "203.0.113.5" in c["fix"]
    cf = gc.check_domain("example.org", "203.0.113.5", resolve=lambda h: names.get(h, []),
                         cf_zone=lambda d: "example.org")
    assert cf["ok"] and "Cloudflare" in cf["detail"]


def test_domain_check_without_settings():
    c = gc.check_domain("", "203.0.113.5", resolve=lambda h: pytest.fail("no lookup"))
    assert not c["ok"] and "GUESTS_DOMAIN" in c["fix"]
    c = gc.check_domain("example.org", "", resolve=lambda h: ["203.0.113.5"])
    assert not c["ok"] and "SERVER_IP" in c["fix"]


def test_services_check():
    up = gc.check_services(lambda unit: True)
    assert up["ok"]
    half = gc.check_services(lambda unit: "tgrelay" not in unit)
    assert not half["ok"] and "agentdeck-guest-tgrelay" in half["detail"]
    assert "install.sh" in half["fix"]


def test_unit_active_asks_the_system_then_the_user_manager():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return type("R", (), {"returncode": 0 if "--user" in argv else 3})()
    assert gc.unit_active("agentdeck-guest-turnstile.service", run=run)
    assert calls[0][:2] == ["systemctl", "is-active"] and "--user" in calls[1]


def test_doctor_without_any_settings_fails_every_check_with_a_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("GUESTS_ENV_FILE", str(tmp_path / "none.env"))
    checks = gc.doctor(run=Run(missing=True), get_me=lambda t: pytest.fail("no token"),
                       resolve=lambda h: [], detect_ip=lambda: "", cf_zone=lambda d: None,
                       is_active=lambda u: False)
    assert [c["id"] for c in checks] == ["docker", "bot", "owner", "domain", "services"]
    for c in checks:
        assert keys(c) == {"id", "title", "ok", "detail", "fix"}
        assert c["ok"] is False and c["fix"] and c["title"]


def test_doctor_all_good(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("GUESTS_DOMAIN=example.org\nGUEST_BOT_TOKEN=1:T\nOWNER_TG_ID=42\n"
                   "SERVER_IP=203.0.113.5\n")
    monkeypatch.setenv("GUESTS_ENV_FILE", str(env))
    checks = gc.doctor(run=Run(out="27.0\n"), get_me=lambda t: "b", resolve=lambda h: ["203.0.113.5"],
                       detect_ip=lambda: pytest.fail("SERVER_IP is set"), cf_zone=lambda d: None,
                       is_active=lambda u: True)
    assert all(c["ok"] for c in checks), checks


def test_doctor_detects_the_server_ip_when_not_set(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("GUESTS_DOMAIN=example.org\n")
    monkeypatch.setenv("GUESTS_ENV_FILE", str(env))
    checks = gc.doctor(run=Run(), get_me=lambda t: "b", resolve=lambda h: ["198.51.100.9"],
                       detect_ip=lambda: "198.51.100.9", cf_zone=lambda d: None,
                       is_active=lambda u: True)
    assert {c["id"]: c for c in checks}["domain"]["ok"]


def test_doctor_json_cli(monkeypatch, capsys):
    fake = [{"id": "docker", "title": "Docker", "ok": False, "detail": "d", "fix": "f"}]
    monkeypatch.setattr(gc, "doctor", lambda **kw: fake)
    assert gc.main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"checks": fake}
    assert gc.main(["doctor"]) == 1          # human form: failure is the exit code
