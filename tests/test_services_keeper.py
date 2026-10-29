"""Sandbox services keeper: programs that must outlive a terminal (Tailscale, a site's
server, a bot) live in /work/.services/<name>.sh; the keeper starts them when the
sandbox starts, restarts them when they crash, stops them when their file goes."""

import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import services_keeper as kp  # noqa: E402


def svc(d: Path, name: str, body: str) -> Path:
    p = d / f"{name}.sh"
    p.write_text("#!/usr/bin/env bash\n" + body + "\n")
    return p


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie is not alive
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def k(tmp_path):
    keeper = kp.Keeper(tmp_path, max_services=5, backoff=(0, 0, 0))
    yield keeper
    keeper.stop_all()


def test_starts_a_service_and_writes_its_log(k, tmp_path):
    svc(tmp_path, "hello", "echo started; sleep 30")
    k.tick()
    st = k.status()["hello"]
    assert st["state"] == "running" and alive(st["pid"])
    assert wait_for(lambda: "started" in (tmp_path / "hello.log").read_text())


def test_restarts_a_crashed_service(k, tmp_path):
    svc(tmp_path, "crash", "exit 3")
    k.tick()
    assert wait_for(lambda: (k.tick() or True) and k.status()["crash"]["restarts"] >= 1)
    assert k.status()["crash"]["last_exit"] == 3


def test_backoff_waits_before_a_restart(tmp_path):
    k = kp.Keeper(tmp_path, backoff=(30,))
    try:
        svc(tmp_path, "crash", "exit 1")
        k.tick()
        time.sleep(0.3)
        k.tick()
        st = k.status()["crash"]
        assert st["state"] == "waiting" and st["restarts"] == 0
    finally:
        k.stop_all()


def test_removing_the_file_stops_the_service_and_its_children(k, tmp_path):
    f = svc(tmp_path, "tree", "sleep 300 & wait")
    k.tick()
    pid = k.status()["tree"]["pid"]
    assert wait_for(lambda: len(kp.group_pids(pid)) >= 2)   # the shell + its sleep
    kids = kp.group_pids(pid)
    f.unlink()
    k.tick()
    assert "tree" not in k.status()
    assert wait_for(lambda: not any(alive(p) for p in kids))


def test_stop_and_start_by_name(k, tmp_path):
    svc(tmp_path, "web", "sleep 30")
    k.tick()
    pid = k.status()["web"]["pid"]
    assert k.disable("web") is True
    k.tick()
    assert k.status()["web"]["state"] == "stopped"
    assert wait_for(lambda: not alive(pid))
    assert (tmp_path / "web.sh.off").exists()
    assert k.enable("web") is True
    k.tick()
    assert k.status()["web"]["state"] == "running"


def test_restart_by_name_gives_a_new_process(k, tmp_path):
    svc(tmp_path, "web", "sleep 30")
    k.tick()
    old = k.status()["web"]["pid"]
    assert k.restart("web") is True
    k.tick()
    new = k.status()["web"]["pid"]
    assert new != old and alive(new)


def test_at_most_max_services_run(k, tmp_path):
    for i in range(7):
        svc(tmp_path, f"s{i}", "sleep 30")
    k.tick()
    states = [v["state"] for v in k.status().values()]
    assert states.count("running") == 5 and states.count("skipped") == 2
    skipped = [v for v in k.status().values() if v["state"] == "skipped"]
    assert all(v["limit"] == 5 for v in skipped)      # the bot shows why


def test_status_file_is_written(k, tmp_path):
    svc(tmp_path, "hello", "sleep 30")
    k.tick()
    data = json.loads((tmp_path / "status.json").read_text())
    assert data["services"]["hello"]["state"] == "running"


def test_bad_names_are_ignored(k, tmp_path):
    (tmp_path / "Bad Name.sh").write_text("sleep 30\n")
    (tmp_path / "notes.txt").write_text("x")
    k.tick()
    assert k.status() == {}


def test_http_status_and_actions(k, tmp_path):
    svc(tmp_path, "web", "sleep 30")
    k.tick()
    srv = kp.make_server("127.0.0.1", 0, k)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def req(method, path):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path)
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    try:
        code, body = req("GET", "/status")
        assert code == 200 and body["services"]["web"]["state"] == "running"
        assert req("POST", "/stop/web")[0] == 200
        k.tick()
        assert k.status()["web"]["state"] == "stopped"
        assert req("POST", "/start/web")[0] == 200
        k.tick()
        assert k.status()["web"]["state"] == "running"
        assert req("POST", "/restart/web")[0] == 200
        assert req("POST", "/restart/nope")[0] == 404
        assert req("POST", "/restart/..%2fx")[0] == 404
    finally:
        srv.shutdown()


def test_leftovers_from_a_previous_keeper_are_stopped(tmp_path):
    """If the keeper itself is restarted, the services it ran must not run twice —
    also when the script exec'd into another program (the advised `exec ...`)."""
    import subprocess
    script = svc(tmp_path, "web", "sleep 300 & exec sleep 301")
    old = subprocess.Popen(["bash", str(script)], start_new_session=True)
    assert wait_for(lambda: len(kp.group_pids(old.pid)) >= 2)
    kids = kp.group_pids(old.pid)
    (tmp_path / "status.json").write_text(json.dumps({"services": {"web": {
        "state": "running", "pid": old.pid, "start_ticks": kp.start_ticks(old.pid)}}}))
    k = kp.Keeper(tmp_path, backoff=(0,))
    try:
        k.clean_leftovers()
        assert wait_for(lambda: old.poll() is not None)
        assert wait_for(lambda: not any(alive(p) for p in kids))
    finally:
        k.stop_all()


def test_status_records_when_the_process_started(k, tmp_path):
    svc(tmp_path, "web", "sleep 30")
    k.tick()
    st = json.loads((tmp_path / "status.json").read_text())["services"]["web"]
    assert st["start_ticks"] == kp.start_ticks(st["pid"])


def test_a_stranger_with_a_recorded_pid_is_left_alone(tmp_path):
    """After a container restart an old pid may belong to someone else: don't touch it."""
    import subprocess
    stranger = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        svc(tmp_path, "web", "sleep 30")
        (tmp_path / "status.json").write_text(json.dumps({"services": {"web": {
            "state": "running", "pid": stranger.pid,
            "start_ticks": kp.start_ticks(stranger.pid) - 1}}}))     # same number, other process
        k = kp.Keeper(tmp_path, backoff=(0,))
        k.clean_leftovers()
        time.sleep(0.3)
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


def test_default_limit_is_ten(tmp_path):
    assert kp.Keeper(tmp_path).max == 10


def test_the_api_listens_on_this_machine_only_by_default(tmp_path, monkeypatch):
    """On a host install :9876 would otherwise face the internet; a sandbox that must be
    reached from its host sets SERVICES_BIND=0.0.0.0 itself."""
    monkeypatch.delenv("SERVICES_BIND", raising=False)
    assert kp.default_bind() == "127.0.0.1"
    monkeypatch.setenv("SERVICES_BIND", "0.0.0.0")
    assert kp.default_bind() == "0.0.0.0"


def test_services_live_in_the_work_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("SERVICES_DIR", raising=False)
    monkeypatch.setenv("AGENTDECK_WORKDIR", str(tmp_path))
    assert kp.default_dir() == tmp_path / ".services"
    monkeypatch.setenv("SERVICES_DIR", str(tmp_path / "x"))
    assert kp.default_dir() == tmp_path / "x"


def test_without_a_work_folder_set_it_is_agentdecks_default(monkeypatch):
    """Same as library_cli.WORKDIR: the folder above the AgentKennel checkout (a manual
    start.sh has no AGENTDECK_WORKDIR; /work does not exist there)."""
    monkeypatch.delenv("SERVICES_DIR", raising=False)
    monkeypatch.delenv("AGENTDECK_WORKDIR", raising=False)
    repo = Path(kp.__file__).resolve().parent
    assert kp.default_dir() == repo.parent / ".services"


# ── the Telegram view of the services (/services in the bridge) ──────────────
def _view_data(rows):
    return [data for row in rows for _, data in row]


def test_view_lists_each_state_with_its_buttons():
    now = 10_000.0
    text, rows = kp.telegram_view({
        "tunnel": {"state": "running", "since": now - 3 * 3600, "restarts": 0},
        "site": {"state": "waiting", "last_exit": 1, "restarts": 3},
        "bot": {"state": "stopped"},
        "extra": {"state": "skipped", "limit": 10},
    }, now=now)
    assert "tunnel" in text and "running 3 h" in text
    assert "site" in text and "crashed (exit 1)" in text and "restarts: 3" in text
    assert "bot" in text and "stopped" in text
    assert "extra" in text and "10" in text
    data = _view_data(rows)
    assert {"svc:restart:tunnel", "svc:stop:tunnel", "svc:start:bot", "svc:refresh:"} <= set(data)
    assert "svc:stop:bot" not in data and not any(d.endswith(":extra") for d in data)


def test_view_explains_services_when_there_are_none():
    text, rows = kp.telegram_view({})
    assert "No services yet" in text and _view_data(rows) == ["svc:refresh:"]


def test_view_says_when_the_keeper_is_down():
    text, _ = kp.telegram_view(None)
    assert "isn't running" in text


def test_client_talks_to_the_keeper(tmp_path):
    k = kp.Keeper(tmp_path, backoff=(0,))
    svc(tmp_path, "web", "sleep 30")
    k.tick()
    srv = kp.make_server("127.0.0.1", 0, k)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = kp.Client(f"http://127.0.0.1:{srv.server_address[1]}")
        assert c.status()["web"]["state"] == "running"
        assert c.act("restart", "web") is True and c.act("restart", "nope") is False
        assert c.act("rm", "web") is False and c.act("stop", "../x") is False
        assert kp.Client("http://127.0.0.1:1").status() is None
    finally:
        srv.shutdown()
        k.stop_all()
