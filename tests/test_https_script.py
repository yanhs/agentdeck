"""./https.sh — one command to HTTPS after `docker compose up -d`, no domain needed.

The script runs in a throwaway copy of the repo root with PATH shims for curl / docker /
ss / getent: each shim records its argv and answers with canned output from env vars, so
nothing real is contacted and no container is touched.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "https.sh"

SHIM = r"""#!/usr/bin/env bash
# fake {name}: record the call, answer from env
echo "{name} $*" >> "$SHIM_LOG"
{body}
"""

CURL = r"""
args="$*"
case "$args" in
  *api.ipify.org*) [ -n "${FAKE_IPIFY:-}" ] && { printf '%s' "$FAKE_IPIFY"; exit 0; }; exit 6 ;;
  *ifconfig.me*|*icanhazip*) [ -n "${FAKE_IFCONFIG:-}" ] && { printf '%s' "$FAKE_IFCONFIG"; exit 0; }; exit 6 ;;
esac
# the HTTPS readiness probe
printf '%s' "${FAKE_HTTP_CODE:-302}"
exit "${FAKE_CURL_RC:-0}"
"""

DOCKER = r"""
case "$*" in
  "compose version"*) echo "Docker Compose version v2.30.0"; exit 0 ;;
  *" port "*) if [ -n "${FAKE_OWN_PORTS:-}" ]; then
                for p in $FAKE_OWN_PORTS; do case "$*" in *" $p") echo "0.0.0.0:$p"; exit 0;; esac; done
              fi
              exit 1 ;;
  *" logs"*) echo "CADDY-LOG-LINE: obtaining certificate failed"; exit 0 ;;
esac
exit 0
"""

SS = r"""
printf '%b' "${FAKE_SS:-}"
exit 0
"""

GETENT = r"""
[ -n "${FAKE_GETENT:-}" ] || exit 2
echo "$FAKE_GETENT STREAM $3"
exit 0
"""

PUBLIC_IP = "45.67.89.10"


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(SCRIPT, repo / "https.sh")
    shutil.copy(ROOT / "docker-compose.yml", repo / "docker-compose.yml")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("curl", CURL), ("docker", DOCKER), ("ss", SS), ("getent", GETENT)):
        p = bindir / name
        p.write_text(SHIM.format(name=name, body=body))
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    calls_file = tmp_path / "calls.log"
    calls_file.touch()
    base = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "SHIM_LOG": str(calls_file),
        "FAKE_IPIFY": PUBLIC_IP,
        "AGENTDECK_HTTPS_WAIT": "2",
        "AGENTDECK_POLL": "1",
    }

    class Env:
        dir = repo
        calls = calls_file

        def run(self, *args, **extra):
            e = dict(base)
            e.update(extra)
            return subprocess.run(["bash", str(repo / "https.sh"), *args], cwd=str(tmp_path),
                                  env=e, capture_output=True, text=True, timeout=60)

        def log(self):
            return calls_file.read_text()

        def dotenv(self):
            p = repo / ".env"
            return p.read_text() if p.exists() else ""

        def override(self):
            p = repo / "docker-compose.https.yml"
            return p.read_text() if p.exists() else None

    return Env()


def ups(log: str) -> list[str]:
    return [l for l in log.splitlines() if l.startswith("docker ") and " up" in l]


def env_lines(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for l in text.splitlines():
        if "=" in l and not l.lstrip().startswith("#"):
            k, v = l.split("=", 1)
            out.setdefault(k.strip(), []).append(v)
    return out


def test_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK)


def test_no_arg_uses_sslip_name_from_public_ip(env):
    r = env.run()
    assert r.returncode == 0, r.stdout + r.stderr
    kv = env_lines(env.dotenv())
    assert kv["AGENTDECK_SITE"] == ["45-67-89-10.sslip.io"]
    assert "https://45-67-89-10.sslip.io" in r.stdout
    assert ups(env.log()), env.log()
    # the up uses the https override
    assert "docker-compose.https.yml" in ups(env.log())[-1]


def test_ip_falls_back_when_first_service_fails(env):
    r = env.run(FAKE_IPIFY="", FAKE_IFCONFIG="98.76.54.32")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env_lines(env.dotenv())["AGENTDECK_SITE"] == ["98-76-54-32.sslip.io"]


def test_no_ip_at_all_is_a_clear_error(env):
    r = env.run(FAKE_IPIFY="", FAKE_IFCONFIG="")
    assert r.returncode != 0
    assert "public IP" in r.stdout + r.stderr
    assert not ups(env.log())


@pytest.mark.parametrize("ip", ["10.0.0.5", "192.168.1.2", "172.20.0.3", "127.0.0.1",
                                "100.64.1.1", "169.254.3.4", "not-an-ip", "300.1.2.3"])
def test_non_public_ip_refuses(env, ip):
    r = env.run(FAKE_IPIFY=ip, FAKE_IFCONFIG=ip)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "public" in out
    assert not ups(env.log())
    assert env.override() is None
    assert "AGENTDECK_SITE" not in env.dotenv()


def test_domain_arg_is_used(env):
    r = env.run("agents.example.org", FAKE_GETENT=PUBLIC_IP)
    assert r.returncode == 0, r.stdout + r.stderr
    assert env_lines(env.dotenv())["AGENTDECK_SITE"] == ["agents.example.org"]
    assert "https://agents.example.org" in r.stdout
    assert "does not point" not in r.stdout + r.stderr


def test_domain_scheme_and_case_are_normalised(env):
    r = env.run("https://Agents.Example.ORG/", FAKE_GETENT=PUBLIC_IP)
    assert r.returncode == 0, r.stdout + r.stderr
    assert env_lines(env.dotenv())["AGENTDECK_SITE"] == ["agents.example.org"]


def test_domain_pointing_elsewhere_warns_but_continues(env):
    r = env.run("agents.example.org", FAKE_GETENT="1.2.3.4")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "does not point" in out and "1.2.3.4" in out and PUBLIC_IP in out
    assert ups(env.log())


def test_unresolvable_domain_warns_but_continues(env):
    r = env.run("agents.example.org")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "does not resolve" in r.stdout + r.stderr


@pytest.mark.parametrize("bad", ["bad_domain!", "-x.com", "a..b", "1.2.3.4", "x" * 64 + ".com",
                                 "foo bar.com"])
def test_invalid_domain_refuses(env, bad):
    r = env.run(bad)
    assert r.returncode != 0
    assert "not a valid" in r.stdout + r.stderr
    assert not ups(env.log())
    assert env.override() is None
    assert not env.dotenv()


def test_busy_port_refuses_with_owner_and_no_compose_up(env):
    (env.dir / ".env").write_text("AGENTDECK_PORT=9000\n")
    ss = ('LISTEN 0 511 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=812,fd=7))\n'
          "LISTEN 0 511 0.0.0.0:8765 0.0.0.0:*\n")
    r = env.run(FAKE_SS=ss)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "443" in out and "nginx" in out
    assert "AGENTDECK_SITE=https://:8765" in out     # the self-signed fallback is named
    assert not ups(env.log())
    assert env.override() is None
    assert env.dotenv() == "AGENTDECK_PORT=9000\n"   # untouched


def test_busy_port_without_process_name_still_refuses(env):
    r = env.run(FAKE_SS="LISTEN 0 511 [::]:80 [::]:*\n")
    assert r.returncode != 0
    assert "80" in r.stdout + r.stderr
    assert not ups(env.log())


def test_port_like_8080_is_not_mistaken_for_80(env):
    r = env.run(FAKE_SS="LISTEN 0 511 0.0.0.0:8080 0.0.0.0:*\nLISTEN 0 511 0.0.0.0:4430 0.0.0.0:*\n")
    assert r.returncode == 0, r.stdout + r.stderr


def test_ports_held_by_our_own_container_are_fine(env):
    ss = ("LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:((\"docker-proxy\",pid=1,fd=4))\n"
          "LISTEN 0 4096 0.0.0.0:443 0.0.0.0:* users:((\"docker-proxy\",pid=2,fd=4))\n")
    r = env.run(FAKE_SS=ss, FAKE_OWN_PORTS="80 443")
    assert r.returncode == 0, r.stdout + r.stderr
    assert ups(env.log())


def test_env_merge_keeps_other_keys(env):
    (env.dir / ".env").write_text("# my settings\nAGENTDECK_PORT=9000\nAGENTDECK_SITE=old.example.com\n"
                                  "AGENTDECK_PASSWORD=s3cret=x\n")
    r = env.run()
    assert r.returncode == 0, r.stdout + r.stderr
    text = env.dotenv()
    kv = env_lines(text)
    assert "# my settings" in text
    assert kv["AGENTDECK_PORT"] == ["9000"]
    assert kv["AGENTDECK_PASSWORD"] == ["s3cret=x"]
    assert kv["AGENTDECK_SITE"] == ["45-67-89-10.sslip.io"]


def test_override_publishes_80_and_443_and_compose_file_is_set(env):
    r = env.run()
    assert r.returncode == 0, r.stdout + r.stderr
    ov = env.override()
    assert ov is not None
    assert '"80:80"' in ov and '"443:443"' in ov
    assert "agentdeck:" in ov
    kv = env_lines(env.dotenv())
    assert kv["COMPOSE_FILE"] == ["docker-compose.yml:docker-compose.https.yml"]


def test_alternate_host_ports_for_testing(env):
    r = env.run(AGENTDECK_HTTP_PORT="18080", AGENTDECK_HTTPS_PORT="18443",
                FAKE_SS="LISTEN 0 511 0.0.0.0:443 0.0.0.0:* users:((\"nginx\",pid=1,fd=7))\n")
    assert r.returncode == 0, r.stdout + r.stderr
    ov = env.override()
    assert '"18080:80"' in ov and '"18443:443"' in ov
    assert "https://45-67-89-10.sslip.io:18443" in r.stdout


def test_existing_compose_file_value_is_extended_not_replaced(env):
    (env.dir / ".env").write_text("COMPOSE_FILE=docker-compose.yml:my.yml\n")
    r = env.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert env_lines(env.dotenv())["COMPOSE_FILE"] == ["docker-compose.yml:my.yml:docker-compose.https.yml"]
    r = env.run("--off")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env_lines(env.dotenv())["COMPOSE_FILE"] == ["docker-compose.yml:my.yml"]


def test_idempotent(env):
    (env.dir / ".env").write_text("AGENTDECK_PORT=9000\n")
    assert env.run().returncode == 0
    first_env, first_ov = env.dotenv(), env.override()
    r = env.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.dotenv() == first_env
    assert env.override() == first_ov
    kv = env_lines(env.dotenv())
    assert all(len(v) == 1 for v in kv.values()), kv


def test_off_reverts_to_plain_http(env):
    (env.dir / ".env").write_text("AGENTDECK_PORT=9000\n")
    assert env.run().returncode == 0
    r = env.run("--off")
    assert r.returncode == 0, r.stdout + r.stderr
    kv = env_lines(env.dotenv())
    assert "AGENTDECK_SITE" not in kv and "COMPOSE_FILE" not in kv
    assert kv["AGENTDECK_PORT"] == ["9000"]
    assert env.override() is None
    last_up = ups(env.log())[-1]
    assert "docker-compose.https.yml" not in last_up
    assert "http://45.67.89.10:9000" in r.stdout


def test_off_when_never_on_is_harmless(env):
    r = env.run("--off")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.override() is None


def test_certificate_timeout_shows_caddy_log(env):
    r = env.run(FAKE_CURL_RC="35", FAKE_HTTP_CODE="000", AGENTDECK_HTTPS_WAIT="1")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "CADDY-LOG-LINE" in out
    # settings stay in place — the next `docker compose up -d` retries
    assert "AGENTDECK_SITE" in env.dotenv()


def test_readiness_probe_pins_the_name_to_this_host(env):
    env.run()
    probes = [l for l in env.log().splitlines()
              if l.startswith("curl ") and "sslip.io" in l]
    assert probes, env.log()
    assert "--resolve 45-67-89-10.sslip.io:443:127.0.0.1" in probes[0]


def test_help(env):
    r = env.run("--help")
    assert r.returncode == 0
    assert "sslip.io" in r.stdout and "--off" in r.stdout
    assert not ups(env.log())


def test_entrypoint_drops_placeholder_acme_email():
    """Let's Encrypt refuses a contact address at example.com — the compose default
    (you@example.com) would break the certificate. The entrypoint drops it."""
    text = (ROOT / "docker" / "entrypoint.sh").read_text()
    assert "@example.com" in text and "email" in text
