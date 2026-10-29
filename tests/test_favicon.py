"""The dashboard's own icon (the paw from agentkennel.com) in the tab, bookmarks and the
phone home screen, instead of the browser's grey globe. The icon files are reachable
without a session: the login page and a logged-out bookmark show it too."""
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
CADDYFILES = {"docker": ROOT / "docker" / "Caddyfile", "root": ROOT / "Caddyfile"}
ICONS = ("/favicon.svg", "/favicon.ico", "/apple-touch-icon.png")


def png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_svg_icon_is_the_paw():
    svg = (WEB / "favicon.svg").read_text()
    assert svg.startswith("<svg") and 'viewBox="0 0 64 64"' in svg
    assert svg.count("<ellipse") == 4          # four toes
    assert "<path" in svg                      # and the pad


def test_ico_has_16_and_32():
    data = (WEB / "favicon.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1) and count >= 2
    sizes = {data[6 + 16 * i] or 256 for i in range(count)}
    assert {16, 32} <= sizes


def test_apple_touch_icon_180():
    assert png_size((WEB / "apple-touch-icon.png").read_bytes()) == (180, 180)


@pytest.mark.parametrize("page", ["index.html", "index-lib.html"])
def test_dashboard_pages_link_the_icons(page):
    head = (WEB / page).read_text().split("</head>", 1)[0]
    assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml">' in head
    assert '<link rel="icon" href="/favicon.ico" sizes="32x32">' in head
    assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in head


def test_login_and_setup_pages_link_the_icon():
    import status_server
    assert 'href="/favicon.svg"' in status_server.login_html()
    assert 'href="/favicon.svg"' in status_server._AUTH_HEAD


@pytest.mark.parametrize("name", CADDYFILES)
def test_caddy_serves_icons_before_the_gate(name):
    src = CADDYFILES[name].read_text()
    gate = src.index("forward_auth")
    m = re.search(r"@icons\s+path\s+([^\n]+)\n\s*handle\s+@icons\s*\{\s*file_server\s*\}", src)
    assert m and m.start() < gate, "icon files must be public, before forward_auth"
    assert set(m.group(1).split()) == set(ICONS)


def test_nginx_template_serves_icons_without_auth():
    src = (ROOT / "nginx" / "agents-subdomain.conf").read_text()
    m = re.search(r"location ~ \^/\(favicon\\\.svg\|favicon\\\.ico\|apple-touch-icon\\\.png\)\$ \{([^}]*)\}", src)
    assert m, "a public location for the icon files"
    assert "auth_request off" in m.group(1)
