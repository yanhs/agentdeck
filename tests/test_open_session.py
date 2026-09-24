"""open-session.sh — what the single ttyd (/sess/?arg=<id>) runs per browser tab.

The id comes straight from a URL (ttyd -a), so the script must refuse
anything but ONE known 8-hex id before it reaches python, tmux or a shell;
then `library_cli.py ensure` loads the topic and the script attaches to it.
Also pins the deployment wiring: the PM2 app (ttyd flags) and the nginx
locations (/sess/ websocket -> 3031, /api/library -> 3011, both behind the
server-level cookie auth).

Isolation as in test_library_cli: temp registry/HOME, fake claude, private
tmux socket agentdeck-test-cli-*.
"""
import os
import re
import subprocess
import time

import pytest

from tests.test_library_cli import BAD_IDS, U1, U2, U3, Deck, wait_for

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "open-session.sh")


@pytest.fixture
def deck(tmp_path):
    d = Deck(tmp_path)
    yield d
    d.close()


def run(deck, *args, **extra):
    env = dict(deck.env, **extra)
    return subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, env=env,
                          timeout=30)


# ── refusals ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("args", [[], ["aaaaaaaa", "bbbbbbbb"]] + [[b] for b in BAD_IDS])
def test_refuses_anything_but_one_known_id(deck, args):
    deck.add("живая", uuid=U1)
    r = run(deck, *args)
    assert r.returncode == 2, (args, r.stdout, r.stderr)
    assert "unknown session" in (r.stdout + r.stderr)
    assert not deck.server_up()


def test_unknown_valid_id_is_refused_without_creating_anything(deck):
    r = run(deck, "deadbeef")
    assert r.returncode == 2 and "unknown session" in (r.stdout + r.stderr)
    assert not os.path.exists(deck.lib)
    assert not deck.server_up()


def test_archived_id_is_refused(deck):
    e = deck.add("архив", uuid=U1, archived=True)
    r = run(deck, e["id"])
    assert r.returncode == 2 and "unknown session" in (r.stdout + r.stderr)


def test_pauses_so_the_message_stays_readable_in_the_tab(deck):
    t = time.time()
    run(deck, "deadbeef", OPEN_SESSION_PAUSE="1")
    assert time.time() - t >= 1


# ── dry run ─────────────────────────────────────────────────────────────────
def test_dry_run_prints_the_pane_command_and_starts_nothing(deck):
    e = deck.add("новая", uuid=U1)
    r = run(deck, e["id"], DRY_RUN="1")
    assert r.returncode == 0, r.stderr
    assert f"--session-id {U1}" in r.stdout and "AGENTDECK_SESSION=aaaaaaaa" in r.stdout
    assert f"{deck.claude} --session-id" in r.stdout            # CLAUDE_BIN reaches library_cli
    assert not deck.server_up()


def test_dry_run_resume_when_transcript_exists(deck):
    e = deck.add("старая", uuid=U2)
    deck.transcript(e)
    r = run(deck, e["id"], DRY_RUN="1")
    assert r.returncode == 0 and f"--resume {U2}" in r.stdout


def test_dry_run_still_refuses_unknown_ids(deck):
    r = run(deck, "deadbeef", DRY_RUN="1")
    assert r.returncode == 2 and "unknown session" in (r.stdout + r.stderr)


# ── the real thing: ensure + attach ─────────────────────────────────────────
def test_opens_the_topic_and_attaches_a_tab(deck):
    e = deck.add("ImmAppeal", uuid=U1)
    tab = subprocess.Popen(["script", "-qfc", f"bash {SCRIPT} {e['id']}", "/dev/null"],
                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=deck.env)
    deck.clients.append(tab)
    assert wait_for(lambda: deck.has("cs-aaaaaaaa"))
    assert wait_for(lambda: deck.active().get("aaaaaaaa", {}).get("attached"))
    assert len(wait_for(deck.calls)) == 1
    tab.kill()                                                 # closing the tab ...
    time.sleep(0.3)
    assert deck.has("cs-aaaaaaaa")                             # ... leaves the topic loaded


def test_all_busy_prints_a_clear_message_and_exits_3(deck):
    a, b, c = deck.add("A", uuid=U1), deck.add("B", uuid=U2), deck.add("C", uuid=U3)
    for x in (a, b):
        assert deck.cli("ensure", x["id"], AGENTDECK_MAX_ACTIVE="2").returncode == 0
    r = run(deck, c["id"], AGENTDECK_MAX_ACTIVE="2", AGENTDECK_WORKING_SECONDS="3600")
    assert r.returncode == 3
    assert "заняты" in (r.stdout + r.stderr)
    assert not deck.has("cs-c0ffee00")


# ── deployment wiring ───────────────────────────────────────────────────────
def test_pm2_app_runs_one_ttyd_with_url_args_on_localhost_3031():
    cfg = open(os.path.join(REPO, "sessions.pm2.config.js")).read()
    assert re.search(r'name:\s*"sessions"', cfg)
    assert '"/usr/bin/ttyd"' in cfg or "'ttyd'" in cfg or '"ttyd"' in cfg
    for flag in ('"-W"', '"-a"', '"-i", "lo"', '"-p", "3031"', '"--base-path", "/sess"'):
        assert flag in cfg, flag
    # -O: websocket Origin must equal Host. The login cookie is SameSite=Lax, so any
    # *.reimake.com page is "same-site" and would otherwise get a shell here.
    assert '"-O"' in cfg
    assert "open-session.sh" in cfg and '"bash"' in cfg


NGINX = os.path.join(REPO, "nginx", "agents-subdomain.conf")


def _location(conf, head):
    m = re.search(r"location\s+" + re.escape(head) + r"\s*\{([^}]*)\}", conf)
    assert m, f"no location {head}"
    return m.group(1)


def test_nginx_proxies_sess_websocket_to_3031():
    body = _location(open(NGINX).read(), "/sess/")
    assert "proxy_pass http://127.0.0.1:3031;" in body
    assert "Upgrade $http_upgrade" in body and "Connection 'upgrade'" in body
    assert "auth_request off" not in body


def test_nginx_proxies_api_library_to_status_server_keeping_the_path():
    body = _location(open(NGINX).read(), "/api/library")
    assert "proxy_pass http://127.0.0.1:3011;" in body          # no URI part: path kept
    assert "auth_request off" not in body


def test_nginx_keeps_server_level_cookie_auth_and_the_tasks_block():
    conf = open(NGINX).read()
    server = conf.split("server {")[1]
    assert re.search(r"^\s{4}auth_request /__auth;", server, re.M)
    assert "location /tasks/" in conf                          # synced from the live file
