#!/usr/bin/env python3
"""Site gate — a guest publishes a site from its sandbox as <site>-<guest>.reimake.com.

The guest's agent calls (from inside its sandbox, with its turnstile pass):

    curl -s -X POST http://172.31.250.1:8794/publish \\
         -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" -d '{"site":"test","port":3000}'
    curl -s http://172.31.250.1:8794/sites -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN"
    curl -s -X DELETE http://172.31.250.1:8794/publish/test -H "Authorization: Bearer ..."

The host then creates the DNS record (Cloudflare, proxied), an nginx vhost that proxies
ONLY to that guest's sandbox address and port, a Let's Encrypt certificate, and tells
the owner in Telegram. A guest can have MAX_SITES sites and remove only its own.
Names and ports are validated before they reach a command line or a config file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "turnstile"))
import turnstile as ts  # noqa: E402

DOMAIN = "reimake.com"
MAX_SITES = 3
SITE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?$")
SERVER_IP = os.environ.get("SITEGATE_SERVER_IP", "203.0.113.10")


class GateError(Exception):
    pass


def validate_site(name) -> str:
    if not isinstance(name, str) or not SITE_RE.match(name):
        raise ValueError("имя сайта: латиница a-z, цифры и дефис, 1–20 знаков")
    return name


def validate_port(port) -> int:
    if isinstance(port, bool) or not isinstance(port, (int, str)) or not str(port).isdigit():
        raise ValueError("порт — число от 1024 до 65535")
    value = int(port)
    if not 1024 <= value <= 65535:
        raise ValueError("порт — число от 1024 до 65535")
    return value


def host_for(site: str, guest: str) -> str:
    host = f"{site}-{guest}.{DOMAIN}"
    if len(host.split(".")[0]) > 63:
        raise ValueError("слишком длинное имя")
    return host


def render_vhost(host: str, ip: str, port: int, tls: bool) -> str:
    """nginx config for one guest site. tls=False: only what the first certificate needs."""
    http = (
        "server {\n"
        "    listen 80;\n    listen [::]:80;\n"
        f"    server_name {host};\n"
        "    location ^~ /.well-known/acme-challenge/ { root /var/www/html; }\n"
        + ("    location / { return 301 https://$host$request_uri; }\n" if tls
           else "    location / { return 404; }\n")
        + "}\n"
    )
    if not tls:
        return f"# guest site {host} (certificate pending)\n" + http
    https = (
        "server {\n"
        "    listen 443 ssl;\n    listen [::]:443 ssl;\n"
        f"    server_name {host};\n"
        f"    ssl_certificate /etc/letsencrypt/live/{host}/fullchain.pem;\n"
        f"    ssl_certificate_key /etc/letsencrypt/live/{host}/privkey.pem;\n"
        "    include /etc/letsencrypt/options-ssl-nginx.conf;\n"
        "    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;\n"
        "    client_max_body_size 50m;\n"
        "    location / {\n"
        f"        proxy_pass http://{ip}:{port};\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Upgrade $http_upgrade;\n"
        "        proxy_set_header Connection $http_connection;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto https;\n"
        "        proxy_read_timeout 300s;\n"
        "    }\n"
        "}\n"
    )
    return f"# guest site {host} -> sandbox {ip}:{port} (managed by sitegate)\n" + http + "\n" + https


def render_gone(host: str) -> str:
    """A taken-down site: answers 404 on its own name until the name is gone."""
    return (
        f"# guest site {host} (removed; stub until DNS is gone)\n"
        "server {\n    listen 80;\n    listen [::]:80;\n"
        f"    server_name {host};\n    location / {{ return 404; }}\n}}\n"
        "server {\n    listen 443 ssl;\n    listen [::]:443 ssl;\n"
        f"    server_name {host};\n"
        f"    ssl_certificate /etc/letsencrypt/live/{host}/fullchain.pem;\n"
        f"    ssl_certificate_key /etc/letsencrypt/live/{host}/privkey.pem;\n"
        "    include /etc/letsencrypt/options-ssl-nginx.conf;\n"
        "    location / { return 404; }\n}\n"
    )


class Gate:
    def __init__(self, book: "ts.GuestBook", ops, registry: Path,
                 ip_of: Callable[[str], Optional[str]], cleanup_delay: float = 90) -> None:
        self.book = book
        self.ops = ops
        self.path = Path(registry)
        self.ip_of = ip_of
        self.cleanup_delay = cleanup_delay
        self._lock = threading.Lock()

    def _load(self) -> Dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self, reg: Dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, indent=2, ensure_ascii=False))
        tmp.replace(self.path)

    def sites(self, guest: str) -> list:
        return [{"url": f"https://{h}", "site": v["site"], "port": v["port"]}
                for h, v in sorted(self._load().items()) if v["guest"] == guest]

    def publish(self, guest: str, site, port) -> Dict:
        site, port = validate_site(site), validate_port(port)
        g = self.book.get(guest)
        if g is None or not g.enabled:
            raise GateError("гость выключен")
        ip = self.ip_of(guest)
        if not ip:
            raise GateError("у песочницы нет постоянного адреса — нужен guestctl recreate")
        host = host_for(site, guest)
        with self._lock:
            reg = self._load()
            if host in reg:
                if reg[host]["guest"] != guest:
                    raise GateError("этот адрес занят")
                self.ops.write_vhost(host, render_vhost(host, ip, port, tls=True))
                self.ops.reload()
                reg[host]["port"] = port
                self._save(reg)
                return {"url": f"https://{host}", "host": host, "port": port}
            if sum(1 for v in reg.values() if v["guest"] == guest) >= MAX_SITES:
                raise GateError(f"не больше {MAX_SITES} сайтов на гостя — сначала снимите старый")
            try:
                self.ops.dns_create(host)
                self.ops.write_vhost(host, render_vhost(host, ip, port, tls=False))
                self.ops.reload()
                self.ops.cert_issue(host)
                self.ops.write_vhost(host, render_vhost(host, ip, port, tls=True))
                self.ops.reload()
            except Exception as exc:
                for undo in (lambda: self.ops.remove_vhost(host), self.ops.reload,
                             lambda: self.ops.dns_delete(host)):
                    try:
                        undo()
                    except Exception:
                        pass
                raise GateError(f"не получилось опубликовать: {exc}")
            reg[host] = {"guest": guest, "site": site, "port": port,
                         "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            self._save(reg)
        self.ops.notify(f"🌐 Гость {guest} опубликовал сайт https://{host} "
                        f"(порт {port} в его песочнице). Снять: sitegate.py remove {host}")
        return {"url": f"https://{host}", "host": host, "port": port}

    def unpublish(self, guest: str, site) -> Dict:
        site = validate_site(site)
        host = host_for(site, guest)
        with self._lock:
            reg = self._load()
            if host not in reg or reg[host]["guest"] != guest:
                raise GateError("такого вашего сайта нет")
            self._remove(host)
            del reg[host]
            self._save(reg)
        self.ops.notify(f"🌐 Гость {guest} снял сайт https://{host}")
        return {"removed": f"https://{host}"}

    def _remove(self, host: str) -> None:
        """Take a site down without ever falling through to another site.

        Cloudflare keeps routing the name for a short while after the DNS record is
        deleted; with no vhost, nginx would answer with its FIRST server block (another
        dashboard's login page). So: 404 stub first, then DNS, and only after
        cleanup_delay the stub and the certificate go.
        """
        self.ops.write_vhost(host, render_gone(host))
        self.ops.reload()
        self.ops.dns_delete(host)

        def finish() -> None:
            self.ops.remove_vhost(host)
            self.ops.reload()
            self.ops.cert_delete(host)

        if self.cleanup_delay <= 0:
            finish()
        else:
            threading.Timer(self.cleanup_delay, finish).start()


# --------------------------------------------------------------------------- #
class RealOps:
    """The actual server changes. Every name here was validated by the Gate."""

    VHOSTS = ROOT / "nginx" / "sites"

    def __init__(self) -> None:
        self.VHOSTS.mkdir(parents=True, exist_ok=True)
        self._zone: Optional[str] = None

    @staticmethod
    def _run(*argv: str) -> str:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''}: "
                               f"{(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout

    def _cf(self, method: str, path: str, data: Optional[Dict] = None) -> Dict:
        token = (Path.home() / ".cf_token").read_text().strip()
        argv = ["curl", "-4", "-s", "-X", method, "-H", f"Authorization: Bearer {token}",
                "-H", "Content-Type: application/json",
                f"https://api.cloudflare.com/client/v4/{path}"]
        if data is not None:
            argv += ["--data", json.dumps(data)]
        res = json.loads(self._run(*argv) or "{}")
        if not res.get("success"):
            raise RuntimeError(f"cloudflare: {res.get('errors')}")
        return res

    def _zone_id(self) -> str:
        if not self._zone:
            self._zone = self._cf("GET", f"zones?name={DOMAIN}")["result"][0]["id"]
        return self._zone

    def dns_create(self, host: str) -> None:
        z = self._zone_id()
        if self._cf("GET", f"zones/{z}/dns_records?name={host}")["result"]:
            return
        self._cf("POST", f"zones/{z}/dns_records", {
            "type": "A", "name": host, "content": SERVER_IP, "proxied": True, "ttl": 1,
            "comment": "AgentDeck guest site (sitegate)"})

    def dns_delete(self, host: str) -> None:
        z = self._zone_id()
        for rec in self._cf("GET", f"zones/{z}/dns_records?name={host}")["result"]:
            self._cf("DELETE", f"zones/{z}/dns_records/{rec['id']}")

    def write_vhost(self, host: str, text: str) -> None:
        conf = self.VHOSTS / f"{host}.conf"
        conf.write_text(text)
        self._run("sudo", "-n", "ln", "-sfn", str(conf), f"/etc/nginx/sites-enabled/sbx-site-{host}")

    def remove_vhost(self, host: str) -> None:
        self._run("sudo", "-n", "rm", "-f", f"/etc/nginx/sites-enabled/sbx-site-{host}")
        (self.VHOSTS / f"{host}.conf").unlink(missing_ok=True)

    def reload(self) -> None:
        self._run("sudo", "-n", "nginx", "-t")
        self._run("sudo", "-n", "systemctl", "reload", "nginx")

    def cert_issue(self, host: str) -> None:
        time.sleep(3)  # let nginx pick up the http vhost
        self._run("sudo", "-n", "certbot", "certonly", "--webroot", "-w", "/var/www/html",
                  "-d", host, "--non-interactive", "--keep-until-expiring")

    def cert_delete(self, host: str) -> None:
        try:
            self._run("sudo", "-n", "certbot", "delete", "--cert-name", host, "--non-interactive")
        except RuntimeError:
            pass  # no certificate yet — nothing to delete

    def notify(self, text: str) -> None:
        try:
            subprocess.run(["bash", "-c", 'source "$HOME/.immappeal-scheduled/lib_notify.sh"; tg "$1"',
                            "_", text], timeout=30, capture_output=True)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
def make_server(bind: str, port: int, gate: Gate) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: Dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _guest(self) -> Optional[str]:
            auth = self.headers.get("authorization", "")
            secret = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            g = gate.book.lookup(secret)
            return g.name if g else None

        def _body(self) -> Dict:
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}

        def _route(self, method: str) -> None:
            guest = self._guest()
            if guest is None:
                return self._json(401, {"error": "нужен пропуск: Authorization: Bearer <пропуск>"})
            try:
                if method == "GET" and self.path == "/sites":
                    return self._json(200, {"sites": gate.sites(guest)})
                if method == "POST" and self.path == "/publish":
                    body = self._body()
                    return self._json(200, gate.publish(guest, body.get("site"), body.get("port")))
                if method == "DELETE" and self.path.startswith("/publish/"):
                    return self._json(200, gate.unpublish(guest, self.path[len("/publish/"):]))
                return self._json(404, {"error": "есть: GET /sites, POST /publish, DELETE /publish/<site>"})
            except (ValueError, GateError) as exc:
                return self._json(400, {"error": str(exc)})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    data = Path(os.environ.get("TURNSTILE_DATA", ROOT / "turnstile" / "data"))
    book = ts.GuestBook(data / "guests.json")
    sandboxes = data / "sandboxes.json"

    def ip_of(name: str) -> Optional[str]:
        reg = json.loads(sandboxes.read_text()) if sandboxes.exists() else {}
        return (reg.get(name) or {}).get("ip")

    gate = Gate(book, RealOps(), data / "sites.json", ip_of)
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["remove"] and len(args) == 2:  # owner: take any site down
        host = args[1]
        reg = gate._load()
        if host not in reg:
            print(f"no site {host}")
            return 1
        gate._remove(host)
        del reg[host]
        gate._save(reg)
        print(f"removed {host}")
        return 0
    if args[:1] == ["list"]:
        for host, v in sorted(gate._load().items()):
            print(f"https://{host}\tguest={v['guest']}\tport={v['port']}\t{v['created']}")
        return 0
    bind = os.environ.get("SITEGATE_BIND", "127.0.0.1")
    port = int(os.environ.get("SITEGATE_PORT", "8794"))
    print(f"sitegate on {bind}:{port}", file=sys.stderr, flush=True)
    make_server(bind, port, gate).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
