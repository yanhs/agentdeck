"""TDD: the dashboard's Services block API on the status server (2.0).

    GET  /api/services                  -> {available, services: {name: state…}, dir}
                                           (the services keeper's /status; available=false
                                           when the keeper doesn't answer)
    GET  /api/services/log/<name>       -> {name, lines: [last lines of <name>.log]}
    POST /api/services/<action>/<name>  -> {ok: true}   action: restart | stop | start

A real Keeper with its HTTP API on a free port (SERVICES_URL), a temp services folder
(SERVICES_DIR). POSTs follow the library's CSRF rule: JSON body, same Origin.
"""
from __future__ import annotations

import importlib
import json
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

import services_keeper as kp  # noqa: E402


@pytest.fixture
def svc(tmp_path, monkeypatch):
    d = tmp_path / ".services"
    keeper = kp.Keeper(d, backoff=(0,), cwd=tmp_path)
    ksrv = kp.make_server("127.0.0.1", 0, keeper)
    threading.Thread(target=ksrv.serve_forever, daemon=True).start()
    monkeypatch.setenv("SERVICES_URL", f"http://127.0.0.1:{ksrv.server_address[1]}")
    monkeypatch.setenv("SERVICES_DIR", str(d))
    monkeypatch.delenv("AGENTDECK_ORIGIN", raising=False)
    sys.modules.pop("status_server", None)
    ss = importlib.import_module("status_server")
    srv = HTTPServer(("127.0.0.1", 0), ss.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class S:
        dir = d
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        kp_server = ksrv

    S.keeper = keeper
    try:
        yield S
    finally:
        keeper.stop_all()
        srv.shutdown()
        srv.server_close()
        ksrv.shutdown()
        ksrv.server_close()


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, json.loads(r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), e.headers


def post(base, path, body=None, ctype="application/json", origin=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": ctype})
    if origin is not None:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def until(fn, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def test_listing_shows_the_keepers_services(svc):
    (svc.dir / "web.sh").write_text("exec sleep 60\n")
    (svc.dir / "old.sh.off").write_text("exec sleep 60\n")
    svc.keeper.tick()
    code, data, headers = get(svc.base, "/api/services")
    assert code == 200
    assert headers.get("Cache-Control") in ("no-cache", "no-store")
    assert data["available"] is True
    assert data["services"]["web"]["state"] == "running"
    assert data["services"]["old"]["state"] == "stopped"
    assert data["dir"] == str(svc.dir)
    assert abs(data["now"] - time.time()) < 5          # "running 3 h" counts from the server's clock
    assert "Access-Control-Allow-Origin" not in headers


def test_listing_when_the_keeper_is_down(svc, monkeypatch):
    monkeypatch.setenv("SERVICES_URL", "http://127.0.0.1:9")      # nothing listens there
    code, data, _ = get(svc.base, "/api/services")
    assert code == 200
    assert data["available"] is False and data["services"] == {}


def test_log_tail(svc):
    lines = [f"line {i}" for i in range(500)]
    (svc.dir / "web.log").write_text("\n".join(lines) + "\n")
    code, data, _ = get(svc.base, "/api/services/log/web")
    assert code == 200
    assert data["name"] == "web"
    assert data["lines"][-1] == "line 499"
    assert len(data["lines"]) == 200


def test_log_of_a_service_without_a_log_is_empty(svc):
    code, data, _ = get(svc.base, "/api/services/log/web")
    assert code == 200 and data["lines"] == []


@pytest.mark.parametrize("name", ["..", "../etc", "Web", "a b", "x" * 40])
def test_log_refuses_bad_names(svc, name):
    code, _, _ = get(svc.base, "/api/services/log/" + urllib.request.quote(name, safe=""))
    assert code in (400, 404)


def test_stop_and_start_through_the_dashboard(svc):
    (svc.dir / "web.sh").write_text("exec sleep 60\n")
    svc.keeper.tick()
    code, data = post(svc.base, "/api/services/stop/web")
    assert (code, data) == (200, {"ok": True})
    assert (svc.dir / "web.sh.off").exists()
    code, _ = post(svc.base, "/api/services/start/web")
    assert code == 200 and (svc.dir / "web.sh").exists()


def test_restart_through_the_dashboard(svc):
    (svc.dir / "web.sh").write_text("exec sleep 60\n")
    svc.keeper.tick()
    pid = svc.keeper.status()["web"]["pid"]
    code, _ = post(svc.base, "/api/services/restart/web")
    assert code == 200
    svc.keeper.tick()
    assert until(lambda: svc.keeper.status()["web"]["pid"] not in (None, pid))


def test_unknown_service_or_action(svc):
    assert post(svc.base, "/api/services/stop/nope")[0] == 404
    (svc.dir / "web.sh").write_text("exec sleep 60\n")
    assert post(svc.base, "/api/services/kill/web")[0] == 404


def test_writes_need_json_and_same_origin(svc):
    (svc.dir / "web.sh").write_text("exec sleep 60\n")
    assert post(svc.base, "/api/services/stop/web", ctype="text/plain")[0] == 415
    assert post(svc.base, "/api/services/stop/web", origin="https://evil.example")[0] == 403
    assert (svc.dir / "web.sh").exists()
    host = svc.base.split("//", 1)[1]
    assert post(svc.base, "/api/services/stop/web", origin=f"http://{host}")[0] == 200


def test_no_cors_preflight(svc):
    req = urllib.request.Request(svc.base + "/api/services/stop/web", method="OPTIONS")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.headers.get("Access-Control-Allow-Origin") is None


def test_the_proxies_route_services_to_the_status_server():
    for f in ("Caddyfile", "docker/Caddyfile"):
        text = (TERMINAL_DIR / f).read_text()
        assert "handle /api/services*" in text, f
    assert "location /api/services" in (TERMINAL_DIR / "nginx/agents-subdomain.conf").read_text()
