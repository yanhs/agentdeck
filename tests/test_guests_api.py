"""TDD: the dashboard's Guests block API on the status server (AgentKennel 2.0).

The guests module lives in <repo>/guests (GUESTS_DIR overrides); the panel only drives
its command, guests/guestctl.py:

    GET  /api/guests            -> {installed, configured, dir, env: {bot_token_set, owner_id,
                                    domain}, guests: [...] | null, list_error, checks: [...] | null,
                                    checking, jobs: {name: {action, done, ok, output}}}
    POST /api/guests/check      -> run `guestctl.py doctor --json` (in the background)
    POST /api/guests/setup      {bot_token?, owner_id?, domain?} -> guests/.env, then a check
    POST /api/guests/stop|start|remove {name}, /limit {name, limit} -> `guestctl.py <action>`
                                    (limit: the weekly one; the 5 hours get a seventh)
    POST /api/guests/open {name} -> {url}: a one-time link that opens the guest's dashboard
                                    signed in as the owner (`guestctl.py session <name>`)
    GET  /api/guests/enter?t=   -> reached only through the guest's nginx (/_owner-entry):
                                    spends the link, sets the dashboard cookie, 302 to /

Every guestctl call runs in a background thread: the status server answers one request at
a time, and `remove` alone can take 90 s. Here guestctl.py is a fake script in a temp
folder that records its arguments.
"""
from __future__ import annotations

import importlib
import json
import os
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

TERMINAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TERMINAL_DIR))

TOKEN = "123456789:AAH" + "x" * 32

FAKE = r'''
import json, sys, time
from pathlib import Path
here = Path(__file__).parent
with open(here / "calls.log", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
behave = json.loads((here / "behave.json").read_text())
if behave.get("sleep"):
    time.sleep(behave["sleep"])
cmd = sys.argv[1]
if cmd == "list":
    print(json.dumps({"guests": behave.get("guests", [])}))
elif cmd == "doctor":
    print(json.dumps({"checks": behave.get("checks", [])}))
elif cmd in behave.get("fail", []):
    print("docker: no such container", file=sys.stderr)
    sys.exit(1)
elif cmd == "session":
    print(json.dumps(behave.get("session", {"host": "sb-%s.example.com" % sys.argv[2],
                                            "cookie": "agents_session=owner.1900000000." + "ab" * 32,
                                            "max_age": 43200})))
else:
    print(cmd + " done")
'''

# `guestctl list --json` rows: a 5-hour and a weekly limit, like the subscription's (30.10.2026)
GUESTS = [{"name": "masha", "telegram_id": 111, "port": 18101, "state": "running",
           "enabled": True,
           "five_hour": {"used": 1_500_000, "limit": 20_000_000, "resets_at": 1793430000},
           "seven_day": {"used": 9_000_000, "limit": 140_000_000, "resets_at": 1793689200},
           "url": "https://sb-masha.example.com", "created": "2026-10-20 10:00:00"}]
CHECKS = [{"id": "docker", "title": "Docker", "ok": True, "detail": "26.1", "fix": ""},
          {"id": "bot", "title": "Guest bot", "ok": False, "detail": "no token",
           "fix": "Create a bot with @BotFather"}]


@pytest.fixture
def g(tmp_path, monkeypatch):
    gd = tmp_path / "guests"
    gd.mkdir()
    (gd / "guestctl.py").write_text(FAKE)
    (gd / "behave.json").write_text(json.dumps({"guests": GUESTS, "checks": CHECKS}))
    monkeypatch.setenv("GUESTS_DIR", str(gd))
    monkeypatch.delenv("AGENTKENNEL_ORIGIN", raising=False)
    sys.modules.pop("status_server", None)
    ss = importlib.import_module("status_server")
    # "Your bot" (the dashboard's Telegram page): its owner is the guests' admin
    tg = tmp_path / "telegram.json"
    tg.write_text(json.dumps({"enabled": True, "token": "999:" + "y" * 35, "owner": 42}))
    monkeypatch.setattr(ss, "TG_CONF", str(tg))
    srv = HTTPServer(("127.0.0.1", 0), ss.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class G:
        dir = gd
        tg_conf = tg
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        mod = ss

        @staticmethod
        def behave(**kw):
            (gd / "behave.json").write_text(json.dumps({"guests": GUESTS, "checks": CHECKS, **kw}))

        @staticmethod
        def calls():
            p = gd / "calls.log"
            return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    try:
        yield G
    finally:
        srv.shutdown()
        srv.server_close()


def get(base, path="/api/guests"):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def post(base, path, body=None, ctype="application/json", origin=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(),
                                 method="POST", headers={"Content-Type": ctype})
    if origin is not None:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def until(fn, seconds=8.0):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return None


def configure(g, **env):
    lines = {"GUEST_BOT_TOKEN": TOKEN, "OWNER_TG_ID": "42", "GUESTS_DOMAIN": "example.com", **env}
    (g.dir / ".env").write_text("".join(f"{k}={v}\n" for k, v in lines.items()))


def test_not_installed(tmp_path, monkeypatch, g):
    monkeypatch.setenv("GUESTS_DIR", str(tmp_path / "nowhere"))
    code, d = get(g.base)
    assert code == 200
    assert d["installed"] is False and d["configured"] is False and d["guests"] is None


def test_inside_a_container_there_is_no_guests_block(tmp_path, monkeypatch, g):
    """A Docker install (a guest's own sandbox included) can't run sandboxes: no block."""
    marker = tmp_path / ".dockerenv"
    marker.write_text("")
    monkeypatch.setattr(g.mod, "DOCKERENV", marker)
    monkeypatch.delenv("GUESTS_DIR")
    monkeypatch.setattr(g.mod, "GUESTS_DEFAULT_DIR", g.dir)
    assert get(g.base)[1]["installed"] is False
    monkeypatch.setenv("GUESTS_DIR", str(g.dir))       # said so explicitly: shown
    assert get(g.base)[1]["installed"] is True


def test_installed_but_not_configured_lists_nothing(g):
    code, d = get(g.base)
    assert code == 200
    assert d["installed"] is True and d["configured"] is False
    assert d["env"] == {"bot_token_set": False, "owner_id": "", "domain": ""}
    assert d["admin"] == {"bot_set": True, "owner_id": "42"}
    assert d["guests"] is None
    time.sleep(0.3)
    assert ["list", "--json"] not in g.calls()


def test_configured_lists_guests_in_the_background(g):
    configure(g)
    d = until(lambda: (lambda r: r[1] if r[1]["guests"] is not None else None)(get(g.base)))
    assert d["configured"] is True
    assert d["guests"] == GUESTS
    assert d["env"] == {"bot_token_set": True, "owner_id": "42", "domain": "example.com"}
    assert TOKEN not in json.dumps(d)                  # the token never goes to the page


def test_a_slow_guestctl_does_not_hold_the_server(g):
    configure(g)
    g.behave(sleep=3)
    t = time.time()
    get(g.base)
    get(g.base)
    assert time.time() - t < 1.5


def test_check_runs_the_doctor(g):
    code, _ = post(g.base, "/api/guests/check")
    assert code == 200
    d = until(lambda: (lambda r: r[1] if r[1]["checks"] else None)(get(g.base)))
    assert d["checks"] == CHECKS
    assert ["doctor", "--json"] in g.calls()


def test_setup_writes_env_and_checks_again(g):
    (g.dir / ".env").write_text("# keep me\nSERVER_IP=1.2.3.4\nOWNER_TG_ID=1\n")
    code, d = post(g.base, "/api/guests/setup", {"bot_token": TOKEN, "domain": "Example.COM"})
    assert (code, d) == (200, {"ok": True})
    env = (g.dir / ".env").read_text()
    assert f"GUEST_BOT_TOKEN={TOKEN}\n" in env and "OWNER_TG_ID=42\n" in env
    assert "GUESTS_DOMAIN=example.com\n" in env
    assert "SERVER_IP=1.2.3.4\n" in env and "# keep me" in env and "OWNER_TG_ID=1\n" not in env
    assert stat.S_IMODE(os.stat(g.dir / ".env").st_mode) == 0o600
    assert until(lambda: ["doctor", "--json"] in g.calls())


def test_setup_keeps_the_token_when_it_is_left_empty(g):
    configure(g)
    assert post(g.base, "/api/guests/setup", {"bot_token": "", "domain": "other.org"})[0] == 200
    env = (g.dir / ".env").read_text()
    assert f"GUEST_BOT_TOKEN={TOKEN}" in env and "GUESTS_DOMAIN=other.org" in env


@pytest.mark.parametrize("body", [{"bot_token": "nope"},
                                  {"domain": "http://x.com"}, {"domain": "a b.com"},
                                  {"bot_token": TOKEN + "\nEVIL=1"}])
def test_setup_refuses_bad_values(g, body):
    code, d = post(g.base, "/api/guests/setup", body)
    assert code == 400 and d["error"]
    assert not (g.dir / ".env").exists()


def test_the_admin_is_the_owner_of_your_bot(g):
    """Decision 27.10: "Let in?" goes to the admin's own bot — the wizard doesn't ask for
    an id; a different one in the request is ignored, the .env one follows your bot."""
    (g.dir / ".env").write_text("OWNER_TG_ID=7\n")
    assert post(g.base, "/api/guests/setup", {"owner_id": "555", "domain": "example.com"})[0] == 200
    env = (g.dir / ".env").read_text()
    assert "OWNER_TG_ID=42\n" in env and "555" not in env and "OWNER_TG_ID=7" not in env


@pytest.mark.parametrize("conf", [None, {"enabled": True, "token": "", "owner": 42},
                                  {"enabled": True, "token": "999:" + "y" * 35, "owner": ""}])
def test_no_bot_of_your_own_no_guests_setup(g, conf):
    if conf is None:
        g.tg_conf.unlink()
    else:
        g.tg_conf.write_text(json.dumps(conf))
    assert get(g.base)[1]["admin"]["bot_set"] is False
    code, d = post(g.base, "/api/guests/setup", {"bot_token": TOKEN, "domain": "example.com"})
    assert code == 409 and "your own" in d["error"].lower()
    assert not (g.dir / ".env").exists()


def test_actions_run_guestctl_in_the_background(g):
    configure(g)
    assert post(g.base, "/api/guests/stop", {"name": "masha"}) == (200, {"ok": True})
    d = until(lambda: (lambda j: j if j and j["done"] else None)(get(g.base)[1]["jobs"].get("masha")))
    assert d["action"] == "stop" and d["ok"] is True
    assert ["stop", "masha"] in g.calls()


def test_limit(g):
    configure(g)
    assert post(g.base, "/api/guests/limit", {"name": "masha", "limit": 5_000_000})[0] == 200
    assert until(lambda: ["limit", "masha", "5000000"] in g.calls())
    code, d = post(g.base, "/api/guests/limit", {"name": "masha", "limit": -1})
    assert code == 400 and "per week" in d["error"]              # the limit is the week's
    assert post(g.base, "/api/guests/limit", {"name": "masha", "limit": "lots"})[0] == 400


def test_failed_action_reports_why(g):
    configure(g)
    g.behave(fail=["remove"])
    post(g.base, "/api/guests/remove", {"name": "masha"})
    j = until(lambda: (lambda j: j if j and j["done"] else None)(get(g.base)[1]["jobs"].get("masha")))
    assert j["ok"] is False and "no such container" in j["output"]


def test_one_job_per_guest_at_a_time(g):
    configure(g)
    g.behave(sleep=2)
    assert post(g.base, "/api/guests/stop", {"name": "masha"})[0] == 200
    assert post(g.base, "/api/guests/start", {"name": "masha"})[0] == 409


@pytest.mark.parametrize("name", ["", "../x", "Masha", "a b", "x;rm"])
def test_bad_guest_names(g, name):
    configure(g)
    assert post(g.base, "/api/guests/stop", {"name": name})[0] == 400


def test_actions_need_the_module_configured(g):
    assert post(g.base, "/api/guests/stop", {"name": "masha"})[0] == 409


def test_unknown_action(g):
    configure(g)
    assert post(g.base, "/api/guests/explode", {"name": "masha"})[0] == 404


def test_writes_need_json_and_same_origin(g):
    configure(g)
    assert post(g.base, "/api/guests/stop", {"name": "masha"}, ctype="text/plain")[0] == 415
    assert post(g.base, "/api/guests/stop", {"name": "masha"}, origin="https://evil.example")[0] == 403
    assert post(g.base, "/api/guests/setup", {"domain": "evil.org"}, origin="https://evil.example")[0] == 403


def test_the_proxies_route_guests_to_the_status_server():
    for f in ("Caddyfile", "docker/Caddyfile"):
        assert "handle /api/guests*" in (TERMINAL_DIR / f).read_text(), f
    assert "location /api/guests" in (TERMINAL_DIR / "nginx/agents-subdomain.conf").read_text()


# ── Open: the owner signs in to a guest's dashboard (decision 29.10.2026) ─────
COOKIE = "agents_session=owner.1900000000." + "ab" * 32


def listed(g):
    configure(g)
    assert until(lambda: get(g.base)[1]["guests"])


def enter(base, ticket, host="sb-masha.example.com"):
    """GET /api/guests/enter as the guest's nginx passes it (Host = the guest's address)."""
    import http.client
    c = http.client.HTTPConnection(base.split("//")[1], timeout=5)
    c.request("GET", f"/api/guests/enter?t={ticket}", headers={"Host": host})
    r = c.getresponse()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, r.read().decode())
    c.close()
    return out


def ticket_of(url):
    from urllib.parse import parse_qs, urlparse
    u = urlparse(url)
    assert (u.scheme, u.netloc, u.path) == ("https", "sb-masha.example.com", "/_owner-entry")
    return parse_qs(u.query)["t"][0]


def test_open_gives_a_one_time_link(g):
    listed(g)
    code, d = post(g.base, "/api/guests/open", {"name": "masha"})
    assert code == 200 and set(d) == {"url"}
    t = ticket_of(d["url"])
    assert len(t) >= 40
    assert ["session", "masha"] in g.calls()
    assert "ab" * 32 not in json.dumps(d)            # the cookie itself never goes to the page
    assert ticket_of(post(g.base, "/api/guests/open", {"name": "masha"})[1]["url"]) != t


def test_the_link_signs_in_once(g):
    listed(g)
    t = ticket_of(post(g.base, "/api/guests/open", {"name": "masha"})[1]["url"])
    code, h, _ = enter(g.base, t)
    assert code == 302 and h["location"] == "/"
    cookie = h["set-cookie"]
    assert cookie.startswith(COOKIE + ";")
    for flag in ("Path=/", "Max-Age=43200", "HttpOnly", "SameSite=Lax", "Secure"):
        assert flag in cookie
    assert h["cache-control"] == "no-store" and h["referrer-policy"] == "no-referrer"
    code, h, body = enter(g.base, t)                 # spent
    assert code == 403 and "set-cookie" not in h
    assert "panel" in body.lower()


def test_the_link_works_only_on_that_guests_address(g):
    listed(g)
    t = ticket_of(post(g.base, "/api/guests/open", {"name": "masha"})[1]["url"])
    code, h, _ = enter(g.base, t, host="sb-petya.example.com")
    assert code == 403 and "set-cookie" not in h
    assert enter(g.base, t)[0] == 403                # a wrong try spends it too


def test_the_link_expires(g, monkeypatch):
    listed(g)
    t = ticket_of(post(g.base, "/api/guests/open", {"name": "masha"})[1]["url"])
    real = time.time
    monkeypatch.setattr(g.mod.time, "time", lambda: real() + g.mod.GUEST_ENTRY_TTL + 1)
    assert enter(g.base, t)[0] == 403


@pytest.mark.parametrize("t", ["", "nope", "x" * 300], ids=["empty", "unknown", "long"])
def test_no_link_no_cookie(g, t):
    listed(g)
    code, h, _ = enter(g.base, t)
    assert code == 403 and "set-cookie" not in h


def test_the_host_may_carry_a_port(g):
    listed(g)
    t = ticket_of(post(g.base, "/api/guests/open", {"name": "masha"})[1]["url"])
    assert enter(g.base, t, host="SB-Masha.example.com:443")[0] == 302


@pytest.mark.parametrize("change,code", [
    ({"url": None}, 409),                            # no address of their own yet
    ({"state": "exited"}, 409),                      # the sandbox is not running
])
def test_open_needs_a_running_guest_with_an_address(g, change, code):
    configure(g)
    g.behave(guests=[dict(GUESTS[0], **change)])
    assert until(lambda: get(g.base)[1]["guests"])
    c, d = post(g.base, "/api/guests/open", {"name": "masha"})
    assert c == code and d["error"]
    assert ["session", "masha"] not in g.calls()


def test_open_an_unknown_guest(g):
    listed(g)
    assert post(g.base, "/api/guests/open", {"name": "boris"})[0] == 404


def test_open_checks_the_name_and_the_module(g):
    assert post(g.base, "/api/guests/open", {"name": "masha"})[0] == 409    # not set up
    configure(g)
    assert post(g.base, "/api/guests/open", {"name": "../x"})[0] == 400


def test_open_reports_why_it_failed(g):
    listed(g)
    g.behave(fail=["session"])
    code, d = post(g.base, "/api/guests/open", {"name": "masha"})
    assert code == 502 and "no such container" in d["error"]


@pytest.mark.parametrize("session", [
    {"host": "sb-masha.example.com", "cookie": COOKIE + "\r\nSet-Cookie: evil=1", "max_age": 43200},
    {"host": "sb-masha.example.com", "cookie": "other=1", "max_age": 43200},
    {"host": "sb-evil.example.com", "cookie": COOKIE, "max_age": 43200},
    {"host": "sb-masha.example.com", "cookie": COOKIE, "max_age": "forever"},
    {"nothing": True},
])
def test_open_refuses_a_session_that_does_not_look_right(g, session):
    listed(g)
    g.behave(session=session)
    code, d = post(g.base, "/api/guests/open", {"name": "masha"})
    assert code == 502 and "url" not in d


def test_open_needs_json_and_same_origin(g):
    listed(g)
    assert post(g.base, "/api/guests/open", {"name": "masha"}, origin="https://evil.example")[0] == 403
    assert post(g.base, "/api/guests/open", {"name": "masha"}, ctype="text/plain")[0] == 415
