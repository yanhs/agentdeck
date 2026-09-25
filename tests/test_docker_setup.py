"""The self-hosted stack (docker/entrypoint.sh + Dockerfile + docker/Caddyfile for
`docker compose up`, start.sh + the root Caddyfile without Docker) must run the SAME
layout production runs (nginx/agents-subdomain.conf is the reference):

  * ONE ttyd on 127.0.0.1:3031 serving open-session.sh under /sess
    (-a: ?arg=<id> reaches the script; -O: websocket Origin must equal Host)
  * status_server.py: /api/library*, /telegram, /change-password, /login, /check ...
  * the task board (tasks-dashboard/server.py, :9308) on /tasks/
  * idle_reaper.py once a minute
  * NO numbered slot terminals (/terminal, /terminal2.., launch-claude*.sh, :3005..)

Static checks only (nothing is started): the files are parsed and asserted on.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = {"entrypoint": ROOT / "docker" / "entrypoint.sh", "start": ROOT / "start.sh"}
CADDYFILES = {"docker": ROOT / "docker" / "Caddyfile", "root": ROOT / "Caddyfile"}
OLD_SLOT_PORTS = ("3005", "3006", "3008", "3009", "3012", "3013", "3015", "3016",
                  "3017", "3018", "3019", "3020")


def code_lines(path: Path) -> list[str]:
    """Non-comment, non-empty lines (a trailing `# ...` comment is dropped too)."""
    out = []
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(re.sub(r"\s+#\s.*$", "", s))
    return out


def code(path: Path) -> str:
    return "\n".join(code_lines(path))


# ── shell scripts ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", SCRIPTS)
def test_script_is_valid_bash(name):
    r = subprocess.run(["bash", "-n", str(SCRIPTS[name])], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_runs_exactly_one_ttyd_the_sessions_one(name):
    lines = [l for l in code_lines(SCRIPTS[name])
             if not l.startswith("echo ") and re.search(r"(^|[\s;&|(])ttyd\s", l)]
    assert len(lines) == 1, lines
    ttyd = lines[0]
    for flag in ("-W", "-a", "-O"):
        assert re.search(rf"(^|\s){flag}(\s|$)", ttyd), (flag, ttyd)
    assert re.search(r"-i\s+lo\b", ttyd), ttyd                    # localhost only
    assert re.search(r"-p\s+3031\b", ttyd), ttyd
    assert re.search(r"--base-path\s+/sess\b", ttyd), ttyd
    assert "open-session.sh" in ttyd, ttyd


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_starts_no_numbered_slot_terminals(name):
    src = code(SCRIPTS[name])
    assert "launch-claude" not in src
    assert not re.search(r"/terminal\d*\b", src)
    for port in OLD_SLOT_PORTS:
        assert not re.search(rf"\b{port}\b", src), port


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_runs_backend_task_board_and_reaper(name):
    src = code(SCRIPTS[name])
    assert re.search(r"python3\s+status_server\.py\b", src)
    assert re.search(r"python3\s+tasks-dashboard/server\.py\b", src)
    # idle_reaper once a minute: a loop around it with `sleep 60`
    loop = re.search(r"while\s+(true|:)\s*;\s*do(.*?)done", src, re.S)
    assert loop and "idle_reaper.py" in loop.group(2) and re.search(r"sleep\s+60\b", loop.group(2)), src


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_keeps_the_login_gate_and_persisted_secrets(name):
    src = code(SCRIPTS[name])
    for var in ("AGENTDECK_PASSFILE", "AGENTDECK_AUTH_SECRET", "TRACKER_STATE"):
        assert re.search(rf"export\s+{var}=", src), var
    assert "AGENTDECK_PASSWORD" in src                      # optional pre-seed kept
    assert re.search(r"caddy\s+run\b", src)


# ── Caddyfiles ───────────────────────────────────────────────────────────────
def caddy_routes(path: Path) -> list[tuple[str, str, str, int]]:
    """[(directive, matcher, upstream, position)] for `handle[_path] <m> { reverse_proxy <u> }`."""
    src = code(path)
    return [(m.group(1), m.group(2), m.group(3), m.start())
            for m in re.finditer(r"\b(handle_path|handle)\s+(\S+)\s*\{\s*reverse_proxy\s+(\S+)", src)]


def route_for(path: Path, matcher: str):
    hits = [r for r in caddy_routes(path) if r[1] == matcher]
    assert len(hits) == 1, (matcher, caddy_routes(path))
    return hits[0]


@pytest.mark.parametrize("name", CADDYFILES)
def test_caddy_sessions_ttyd_behind_the_gate(name):
    p = CADDYFILES[name]
    src = code(p)
    gate = src.index("forward_auth")
    directive, _, upstream, pos = route_for(p, "/sess/*")
    # ttyd serves under --base-path /sess -> keep the prefix (handle, not handle_path)
    assert directive == "handle" and upstream == "127.0.0.1:3031" and pos > gate
    assert re.search(r"redir\s+/sess\s+/sess/", src)


@pytest.mark.parametrize("name", CADDYFILES)
def test_caddy_library_api_and_pages(name):
    p = CADDYFILES[name]
    gate = code(p).index("forward_auth")
    d, _, up, pos = route_for(p, "/api/library*")
    assert d == "handle" and up == "127.0.0.1:3011" and pos > gate     # path kept: /api/library/...
    d, _, up, pos = route_for(p, "/telegram*")
    assert d == "handle" and up == "127.0.0.1:3011" and pos > gate
    for m in ("/login*", "/logout*", "/change-password*"):
        d, _, up, _ = route_for(p, m)
        assert d == "handle" and up == "127.0.0.1:3046", m
    assert re.search(r"forward_auth\s+127\.0\.0\.1:3046\s*\{[^}]*uri\s+/check", code(p))


@pytest.mark.parametrize("name", CADDYFILES)
def test_caddy_task_board_and_status_apis(name):
    p = CADDYFILES[name]
    src = code(p)
    assert re.search(r"redir\s+/tasks\s+/tasks/", src)
    assert route_for(p, "/tasks/*")[:3] == ("handle_path", "/tasks/*", "127.0.0.1:9308")
    for m, up in (("/api/terminal-status*", "127.0.0.1:3011"), ("/api/tmux-buffer/*", "127.0.0.1:3045"),
                  ("/api/page-version*", "127.0.0.1:3014"), ("/api/paste-image*", "127.0.0.1:3047")):
        assert route_for(p, m)[2] == up, m


@pytest.mark.parametrize("name", CADDYFILES)
def test_caddy_has_no_numbered_terminals(name):
    src = code(CADDYFILES[name])
    assert not re.search(r"/terminal\d*(?![-\w])", src)     # /api/terminal-status stays
    for port in OLD_SLOT_PORTS:
        assert f":{port}" not in src, port


def test_both_caddyfiles_route_the_same():
    strip = lambda p: [(d, m, u) for d, m, u, _ in caddy_routes(p)]
    assert strip(CADDYFILES["docker"]) == strip(CADDYFILES["root"])


# ── image + compose ──────────────────────────────────────────────────────────
def test_dockerfile_ships_the_library_runtime():
    src = code(ROOT / "Dockerfile")
    assert "launch-claude" not in src
    assert re.search(r"\btmux\b", src) and "ttyd" in src and "caddy" in src
    assert "docker/entrypoint.sh" in src


def test_dockerignore_keeps_the_library_files():
    patterns = [l for l in code_lines(ROOT / ".dockerignore") if not l.startswith("!")]
    needed = ["open-session.sh", "library.py", "library_cli.py", "idle_reaper.py",
              "status_server.py", "tg_bridge.py", "_agent_config.py",
              "tasks-dashboard/server.py", "web/index.html", "docker/entrypoint.sh",
              "docker/Caddyfile"]
    for f in needed:
        assert (ROOT / f).exists(), f
        hit = [p for p in patterns if fnmatch.fnmatch(f, p) or f.startswith(p.rstrip("/") + "/")]
        assert not hit, (f, hit)


def test_compose_passes_the_optional_origin_through():
    src = code(ROOT / "docker-compose.yml")
    assert re.search(r"AGENTDECK_ORIGIN:\s*\"\$\{AGENTDECK_ORIGIN:-\}\"", src)


# ── fresh-user Docker test fixes (2026-09-25) ────────────────────────────────
NGINX = ROOT / "nginx" / "agents-subdomain.conf"


def nginx_status_server_locations() -> list[tuple[str, bool]]:
    """[(path, strips_prefix)] for every nginx location proxied to status_server :3011.
    `proxy_pass http://127.0.0.1:3011/;` (trailing slash, no URI) strips the location
    prefix -> Caddy handle_path; without a URI (or with the path itself) the path is kept."""
    out = []
    for m in re.finditer(r"location\s+(=\s*)?(/[^\s{]*)\s*\{([^}]*)\}", NGINX.read_text()):
        path, body = m.group(2), m.group(3)
        pp = re.search(r"proxy_pass\s+http://127\.0\.0\.1:3011(\S*?);", body)
        if not pp:
            continue
        out.append((path, pp.group(1) == "/"))
    return out


def test_nginx_reference_has_status_server_locations():
    paths = [p for p, _ in nginx_status_server_locations()]
    assert "/api/server" in paths and "/api/library" in paths, paths


@pytest.mark.parametrize("name", CADDYFILES)
def test_caddy_routes_every_status_server_path_nginx_does(name):
    """Derived from nginx so a new API path can't be forgotten in the Caddyfiles again
    (the Server tab 404'd in Docker: /api/server existed only in nginx)."""
    p = CADDYFILES[name]
    gate = code(p).index("forward_auth")
    routes = caddy_routes(p)
    for path, strips in nginx_status_server_locations():
        hits = [r for r in routes if r[2] == "127.0.0.1:3011"
                and (r[1] == path or fnmatch.fnmatch(path, r[1]))]
        assert len(hits) == 1, (path, routes)
        d, _, _, pos = hits[0]
        assert pos > gate, path                                   # behind the login
        assert d == ("handle_path" if strips else "handle"), (path, d)


@pytest.mark.parametrize("name", CADDYFILES)
def test_caddy_api_server_before_telegram(name):
    p = CADDYFILES[name]
    assert route_for(p, "/api/server")[3] < route_for(p, "/telegram*")[3]


def test_dockerfile_points_caddy_storage_at_the_data_volume():
    src = code(ROOT / "Dockerfile")
    assert re.search(r"\bXDG_DATA_HOME=/data\b", src), src
    assert re.search(r"\bXDG_CONFIG_HOME=/data/config\b", src), src
    assert re.search(r"-\s*caddy-data:/data\b", code(ROOT / "docker-compose.yml"))


def test_compose_persists_the_agents_workdir():
    src = code(ROOT / "docker-compose.yml")
    assert re.search(r"-\s*agentdeck-work:/work\b", src), src
    assert re.search(r"^agentdeck-work:\s*$", src, re.M), src
    assert "./my-project:/work" in (ROOT / "docker-compose.yml").read_text()   # hint kept


def test_dockerfile_picks_ttyd_by_arch():
    src = code(ROOT / "Dockerfile")
    assert "aarch64" in src and "x86_64" in src
    assert re.search(r"TARGETARCH|uname\s+-m", src)
    assert not re.search(r"releases/download/[\d.]+/ttyd\.x86_64\s", src)   # no hard-wired arch


def test_dockerfile_pins_claude_code_via_build_arg():
    src = code(ROOT / "Dockerfile")
    m = re.search(r"^ARG\s+CLAUDE_CODE_VERSION=(\S+)", src, re.M)
    assert m and m.group(1), src
    assert re.search(r"npm\s+install\s+-g\s+\"?@anthropic-ai/claude-code@\$\{?CLAUDE_CODE_VERSION\}?", src), src


# ── pre-release fresh-install findings (2026-09-25) ───────────────────────────
@pytest.mark.parametrize("page", ["index.html", "index-lib.html", "server.html"])
def test_page_title_is_the_product_not_the_authors_domain(page):
    html = (ROOT / "web" / page).read_text()
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    assert "AgentDeck" in title, title
    assert "ianprog" not in html and "reimake" not in title


@pytest.mark.parametrize("name", CADDYFILES)
def test_html_is_revalidated_so_logout_shows_the_login_page(name):
    """Without Cache-Control the browser reused its cached dashboard after logout
    (a dead, empty shell instead of the login page)."""
    src = "\n".join(code_lines(CADDYFILES[name]))
    assert re.search(r'header\s+@html\s+Cache-Control\s+"no-cache"', src), name
    assert re.search(r"@html\s+path\s+/\s+/\*\.html", src), name


def test_nginx_dashboard_html_is_revalidated():
    src = (ROOT / "nginx" / "agents-subdomain.conf").read_text()
    block = re.search(r"# Dashboard UI\s*location / \{(.*?)\}", src, re.S).group(1)
    assert 'add_header Cache-Control "no-cache"' in block


def test_compose_does_not_pin_a_container_name():
    """A fixed container_name clashes between two installs even with -p."""
    assert "container_name" not in "\n".join(code_lines(ROOT / "docker-compose.yml"))


def test_nginx_example_has_no_numbered_slot_terminals():
    """The numbered /terminalN ttyds are gone (session library: one ttyd on /sess/)."""
    src = (ROOT / "nginx" / "agents-subdomain.conf").read_text()
    assert not re.search(r"location /terminal\d* \{", src)
    assert "location /sess/" in src or "location ^~ /sess/" in src
