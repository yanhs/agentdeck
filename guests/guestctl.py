#!/usr/bin/env python3
"""guestctl — guest sandboxes: one AgentKennel container per person.

    guestctl.py add <name> --tg <telegram id> [--limit N]   create + start, prints access (N: per week)
    guestctl.py web <name>                                   https://sb-<name>.<domain>
    guestctl.py session <name>                               the owner's login to it (JSON)
    guestctl.py disk-move <name> [--gb 20] [--image I]       an existing guest onto its own disk
    guestctl.py disk-watch                                   warn about full disks (a timer runs it)
    guestctl.py list [--json]                                who, port, state, 5-hour + weekly use
    guestctl.py limit <name> <tokens per week>               change the weekly limit (5 h: a seventh)
    guestctl.py check <name>                                 network walls from inside
    guestctl.py stop|start <name>                            pause / resume a sandbox
    guestctl.py recreate <name> [--image I] [--force]        new container, same volumes
    guestctl.py remove <name>                                remove container + pass (volumes kept)
    guestctl.py files-web                                    https://sbx.<domain> (shared files)
    guestctl.py doctor [--json]                              is everything set up?
    guestctl.py image-tag                                    the image install.sh builds

Each sandbox: docker network "sbx" (see net/sbx-firewall.sh), dashboard on
127.0.0.1:<port> only, memory/CPU/process limits, Claude and Telegram reachable only
through the host turnstiles with the guest's own pass; the only host folder mounted is
the guest's public-files folder. Settings: guests/.env (see guestenv.py, .env.example).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "turnstile"))
import guestenv  # noqa: E402
import disk as guest_disk  # noqa: E402  (a guest's own fixed-size disk)
import envcompat  # noqa: E402  (on the path via guestenv: AGENTKENNEL_*, then AGENTDECK_*)
from texts import t as _t  # noqa: E402
import turnstile as ts  # noqa: E402

GW = "172.31.250.1"
IMAGE_NAME = "agentkennel-guest"  # tag = the AgentKennel version (install.sh builds it)
PORT_BASE = 18100
DATA = HERE / "turnstile" / "data"
REGISTRY = DATA / "sandboxes.json"
PUBLIC_ROOT = str(HERE / "public")  # host side of each guest's /work/public
SERVICES_MAX = 10  # services one sandbox may run (keeper + the agent's rule read it)
# weighted tokens per week (= 7 old days of 20M; the 5 hours get a seventh, 20M);
# change with `limit`
DEFAULT_LIMIT = 140_000_000
UNITS = ("agentkennel-guest-turnstile.service", "agentkennel-guest-tgrelay.service",
         "agentkennel-guest-sitegate.service")
# compat: the same services under their names before v2.1.0 (setups not moved yet)
UNIT_PREFIXES = ("agentkennel-guest-", "agentdeck-guest-")


def repo_version() -> str:
    """This checkout's AgentKennel version (git describe), usable as an image tag."""
    try:
        r = subprocess.run(["git", "-C", str(HERE.parent), "describe", "--tags", "--always"],
                           capture_output=True, text=True, timeout=10)
        version = r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        version = ""
    version = re.sub(r"[^A-Za-z0-9_.-]", "-", version)[:128].lstrip(".-")
    return version or "dev"


def default_image() -> str:
    return guestenv.get("GUEST_IMAGE") or f"{IMAGE_NAME}:{repo_version()}"


def domain() -> str:
    """GUESTS_DOMAIN; a sandbox, an address or a link can't be made without it."""
    d = guestenv.domain()
    if not d:
        raise SystemExit(f"GUESTS_DOMAIN is not set in {guestenv.env_file()} "
                         "(copy guests/.env.example to guests/.env and fill it in)")
    return d


def public_base_url() -> str:
    return f"https://sbx.{domain()}/f"


def dashboard_host(name: str) -> str:
    domain()  # a clear error without one
    return guestenv.dashboard_host(name, _registry())


ADDRESS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def check_address(name: str, address: str, reg: dict) -> str:
    """A guest's own address name: DNS-safe and nobody else's name or address."""
    if not ADDRESS_RE.match(address or ""):
        raise SystemExit(f"address {address!r}: latin letters, digits and '-', up to 40")
    for other, info in reg.items():
        if other != name and address in (other, (info or {}).get("address")):
            raise SystemExit(f"address {address!r} is {other}'s")
    return address


def set_address(name: str, address: str, ops) -> None:
    """sb-<address>.<domain> for the guest; the old address sends everyone there (only
    once the new one works). The sandbox keeps running."""
    import copy
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    reg = _registry()
    if name not in reg:
        raise SystemExit(f"no guest {name}")
    check_address(name, address, reg)
    before = copy.deepcopy(reg)
    old = dashboard_host(name)
    if address == name:
        reg[name].pop("address", None)
    else:
        reg[name]["address"] = address
    _save_registry(reg)
    new = dashboard_host(name)
    if new == old or not ops.vhost_exists(old):
        return
    try:
        _serve_new(ops, new, dashboard_vhost(name, reg[name]["port"]))
    except Exception:
        _save_registry(before)
        raise
    ops.write_vhost(old, sitegate.render_moved(old, new))
    ops.reload()
    print(f"https://{old} -> https://{new}")


def cmd_address(a) -> int:
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    set_address(a.name, a.address, sitegate.RealOps())
    return 0


# The owner opens a guest's dashboard without the guest's password (decision 29.10.2026).
# `session` signs a dashboard login with the sandbox's own cookie key; the owner's panel
# hands it over on the guest's address at OWNER_ENTRY, the one path of that address that
# goes to the panel (127.0.0.1:3011, status_server.py) instead of the sandbox.
OWNER_ENTRY = "/_owner-entry"
OWNER_PANEL_ENTRY = "http://127.0.0.1:3011/api/guests/enter"
OWNER_USER = "owner"
OWNER_TTL = 12 * 3600  # first expiry; the dashboard then renews it like its own logins
SANDBOX_AUTH_SECRET = "/app/.sessions/.agents_auth_secret"  # docker/entrypoint.sh's default
SESSION_COOKIE = "agents_session"


def owner_entry_location() -> str:
    return (
        f"    location = {OWNER_ENTRY} {{\n"
        f"        proxy_pass {OWNER_PANEL_ENTRY};\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Forwarded-Proto https;\n"
        "    }\n"
    )


def sandbox_origin(name: str) -> str:
    """The address sbx-<name> was made for ("" without a container or the setting)."""
    try:
        env = _container_env(name)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return ""
    return envcompat.get("ORIGIN", env=env) or ""


def dashboard_vhost(name: str, port: int, container_origin: str | None = None) -> str:
    """nginx for the guest's own AgentKennel dashboard (its password is AgentKennel's).
    A sandbox made for another address (the guests' domain moved, it wasn't recreated
    since) takes writes only from that address: the browser's Origin is swapped for it."""
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    host = dashboard_host(name)
    known = sandbox_origin(name) if container_origin is None else container_origin
    here = f"https://{host}"
    return sitegate.render_vhost(host, "127.0.0.1", port, tls=True,
                                 extra=owner_entry_location(),
                                 origin=(here, known) if known and known != here else None)


def refresh_web(name: str, port: int) -> None:
    """The dashboard's nginx written again for the container as it is now (after a recreate)."""
    if not web_exists(name):
        return
    ops = _site_ops()
    ops.write_vhost(dashboard_host(name), dashboard_vhost(name, port))
    ops.reload()


def sandbox_auth_secret(name: str) -> bytes:
    """The key the guest's dashboard signs its logins with, read inside the sandbox."""
    # the sandbox's own setting: AGENTKENNEL_* from 2.1, AGENTDECK_* before; else the default
    r = subprocess.run(["docker", "exec", f"sbx-{name}", "sh", "-c",
                        'cat "${AGENTKENNEL_AUTH_SECRET:-${AGENTDECK_AUTH_SECRET:-'
                        + SANDBOX_AUTH_SECRET + '}}"'],
                       capture_output=True, timeout=20)
    key = (r.stdout or b"").strip()
    if r.returncode != 0 or not key or len(key) > 1024:
        why = (r.stderr or b"").decode(errors="replace").strip()[-200:] or "no key"
        raise SystemExit(f"{name}: the dashboard's login key can't be read ({why}); "
                         "is the sandbox running?")
    return key


def owner_session(name: str, now: float | None = None, ttl: int = OWNER_TTL,
                  secret: bytes | None = None) -> dict:
    """A login to sb-<name>.<domain> for the owner, as the dashboard itself signs one
    (status_server._sign_token): <user>.<expiry>.<hmac-sha256>."""
    key = sandbox_auth_secret(name) if secret is None else secret
    exp = int(time.time() if now is None else now) + ttl
    sig = hmac.new(key, f"{OWNER_USER}.{exp}".encode(), hashlib.sha256).hexdigest()
    return {"host": dashboard_host(name), "cookie": f"{SESSION_COOKIE}={OWNER_USER}.{exp}.{sig}",
            "max_age": ttl}


def _site_ops():
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    return sitegate.RealOps()


def ensure_owner_entry(name: str, port: int) -> None:
    """Dashboards opened before the Open button get OWNER_ENTRY once. Only a served
    dashboard (https to its sandbox port): a page still waiting for its certificate is
    left to `web`, and the 404 stub of a guest being removed is never brought back."""
    if not guestenv.domain() or not port:
        return
    ops = _site_ops()
    conf = ops.VHOSTS / f"{dashboard_host(name)}.conf"
    try:
        text = conf.read_text()
    except OSError:
        return
    served = "listen 443" in text and f"proxy_pass http://127.0.0.1:{port};" in text
    if not served or f"location = {OWNER_ENTRY} " in text:
        return
    ops.write_vhost(dashboard_host(name), dashboard_vhost(name, port))
    ops.reload()


def cmd_session(a) -> int:
    info = _registry().get(a.name)
    if not info:
        raise SystemExit(f"no guest {a.name}")
    ensure_owner_entry(a.name, int(info.get("port") or 0))
    print(json.dumps(owner_session(a.name)))
    return 0


def cmd_web(a) -> int:
    """Open https://sb-<name>.<domain> — DNS, certificate, nginx (once per guest)."""
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    info = _registry().get(a.name)
    if not info:
        raise SystemExit(f"no guest {a.name}")
    host = dashboard_host(a.name)
    ops = sitegate.RealOps()
    ops.dns_create(host)
    ops.write_vhost(host, sitegate.render_vhost(host, "127.0.0.1", info["port"], tls=False))
    ops.reload()
    ops.cert_issue(host)
    ops.write_vhost(host, dashboard_vhost(a.name, info["port"]))
    ops.reload()
    print(_t("ctl_web_done", host=host, pw_file=DATA / (a.name + ".dashboard-password")))
    return 0


def web_exists(name: str) -> bool:
    """Is sb-<name>.<domain> served (its vhost written by cmd_web)?"""
    if not guestenv.domain():
        return False
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    return (sitegate.RealOps.VHOSTS / f"{dashboard_host(name)}.conf").exists()


def take_down_web(name: str, ops=None, delay: float = 90) -> None:
    """Take sb-<name>.<domain> down without ever falling through to another guest:
    a 404 stub first (Cloudflare keeps routing the name for a while after the DNS record
    goes; with no vhost nginx answers with its FIRST server block — another dashboard),
    then DNS, and after `delay` the stub and the certificate."""
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    ops = ops or sitegate.RealOps()
    host = dashboard_host(name)
    ops.write_vhost(host, sitegate.render_gone(host))
    ops.reload()
    ops.dns_delete(host)
    if delay:
        time.sleep(delay)
    ops.remove_vhost(host)
    ops.reload()
    ops.cert_delete(host)


def next_ip(used: set) -> str:
    """Lowest free fixed address in the sandbox network (.11 and up)."""
    for last in range(11, 250):
        ip = f"172.31.250.{last}"
        if ip not in used:
            return ip
    raise SystemExit("no free sandbox addresses")


def new_public_key(name: str) -> str:
    """Folder name for the guest's public files: name + an unguessable part."""
    return f"{name}-{secrets.token_urlsafe(16)}"


def host_public_ips() -> str:
    """This server's public IPv4s, for guard_secrets inside the sandbox (whose own
    interfaces are private). From hooks/guard_secrets.py; any trouble -> none."""
    try:
        sys.path.insert(0, str(HERE.parent / "hooks"))
        import guard_secrets
        return ",".join(sorted(guard_secrets.discover_global_ipv4()))
    except Exception:
        return ""


def container_args(name: str, port: int, secret: str, password: str, image: str,
                   public_key: str, memory: str = "4g", cpus: str = "2",
                   pids: int = 4096, ip: str = "", public_ips=None, disk: bool = False) -> list:
    if not ts.NAME_RE.match(name):
        raise ValueError(f"bad guest name {name!r}")
    argv = [
        "docker", "run", "-d", "--name", f"sbx-{name}", "--hostname", name,
        "--network", "sbx", "--restart", "unless-stopped", "--init",
        "--memory", memory, "--memory-swap", memory, "--cpus", cpus,
        "--pids-limit", str(pids), "--security-opt", "no-new-privileges:true",
        "-p", f"127.0.0.1:{port}:8765",
    ]
    if ip:  # fixed address: published sites point here (sitegate)
        argv += ["--ip", ip]
    env = {
        "ANTHROPIC_BASE_URL": f"http://{GW}:8790",
        "ANTHROPIC_AUTH_TOKEN": secret,
        "ANTHROPIC_MODEL": "opus",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "TG_BRIDGE_API_BASE": f"http://{GW}:8792",
        # both names (envcompat.both): an image from before v2.1.0 reads the old ones
        **envcompat.both("PASSWORD", password),
        **envcompat.both("SITE", ":8765"),
        "TZ": "America/Los_Angeles",
        **envcompat.both("ORIGIN", f"https://{dashboard_host(name)}"),
        "PUBLIC_DIR": "/work/public",
        "PUBLIC_URL": f"{public_base_url()}/{public_key}",
        "SERVICES_MAX": str(SERVICES_MAX),
        # the core services keeper (services_keeper.py) listens on localhost by default;
        # the host's Telegram relay asks it at <sandbox ip>:9876 (other sandboxes can't:
        # the sbx network has inter-container traffic off)
        "SERVICES_BIND": "0.0.0.0",
    }
    public_ips = host_public_ips() if public_ips is None else public_ips
    if public_ips:  # a commit with the server's IP is blocked in the sandbox too
        env.update(envcompat.both("PUBLIC_IPS", public_ips))
    if disk:   # its Server page shows the sandbox's own disk (guests/disk.py)
        env["AGENTKENNEL_DISK_PATH"] = "/work"
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    if disk:
        # the guest's folders on its own fixed-size disk (owner's decision 30.10.2026);
        # the shared-files folder is on it too, bound back to where nginx serves it
        argv += guest_disk.binds(name)
    else:
        for volume, target in (("claude", "/root/.claude"), ("sessions", "/app/.sessions"),
                               ("data", "/data"), ("work", "/work")):
            argv += ["-v", f"sbx-{name}-{volume}:{target}"]
        # the only host folder: files the guest hands out by link (served by nginx
        # on sbx.<domain> with symlinks and scripts disabled)
        argv += ["-v", f"{PUBLIC_ROOT}/{public_key}:/work/public"]
    argv.append(image)
    return argv


def disk_ready(name: str, info: dict) -> bool:
    """The guest has its own disk: True when it is mounted; a registered disk that isn't
    mounted is an error — the sandbox would start on empty volumes instead of its files."""
    if not info.get("disk_gb"):
        return False
    if not guest_disk.RealOps().is_mounted(guest_disk.mount_point(name)):
        raise SystemExit(f"{name}: its disk {guest_disk.image(name)} is not mounted at "
                         f"{guest_disk.mount_point(name)} (sudo mount {guest_disk.mount_point(name)})")
    return True


def _volume_dir(volume: str) -> Path:
    return Path(_docker("volume", "inspect", "-f", "{{.Mountpoint}}", volume).stdout.strip())


def cmd_disk_move(a) -> int:
    """An existing guest onto a disk of its own: needs no open terminals (the sandbox is
    stopped while its files are copied); the old Docker volumes stay as a backup."""
    reg = _registry()
    info = reg.get(a.name)
    if not info:
        raise SystemExit(f"no guest {a.name}")
    if info.get("disk_gb"):
        raise SystemExit(f"{a.name} already has its own disk ({info['disk_gb']} GB)")
    open_now = live_terminals(a.name)
    if open_now:
        raise SystemExit(_t("ctl_recreate_busy", name=a.name, n=len(open_now)))
    public_key = info.get("public_key") or new_public_key(a.name)
    public_dir = Path(PUBLIC_ROOT) / public_key
    ops = guest_disk.RealOps()
    mnt = guest_disk.create(a.name, public_dir, gb=a.gb, ops=ops, bind_public=False)
    _docker("stop", f"sbx-{a.name}")
    for folder in ("claude", "sessions", "data", "work"):
        guest_disk.copy_tree(_volume_dir(f"sbx-{a.name}-{folder}"), mnt / folder)
    guest_disk.copy_tree(public_dir, mnt / "public")
    guest_disk.create(a.name, public_dir, gb=a.gb, ops=ops)       # now bind it back
    info.update(disk_gb=int(a.gb), public_key=public_key)
    _save_registry(reg)
    rc = cmd_recreate(argparse.Namespace(name=a.name, image=getattr(a, "image", None), force=False))
    print(f"{a.name}: moved onto its own {a.gb} GB disk {guest_disk.image(a.name)}; "
          f"the old volumes sbx-{a.name}-* are kept")
    return rc


def cmd_disk_watch(a) -> int:
    """The watchman (a timer runs it): warnings at 80 % / full and for big programs."""
    import invites  # noqa: E402  (the guest bot's sender)
    token = guestenv.get("GUEST_BOT_TOKEN") or ""
    send = invites.guest_bot_sender(token) if token else (lambda tid, text: None)

    def tell_guest(tid, text):
        try:
            send(tid, text)
        except Exception as e:  # noqa: BLE001 — the owner still hears about it
            print(f"guest {tid}: not told ({e})", file=sys.stderr)
    reg = _registry()
    ops = guest_disk.RealOps()
    guests = {n: v for n, v in reg.items() if container_state(n) == "running"}
    sent = guest_disk.check(guests, DATA / "disk-alerts.json", ops,
                            tell_owner=guestenv.notify_owner, tell_guest=tell_guest,
                            mounted=lambda n: bool(reg.get(n, {}).get("disk_gb"))
                            and ops.is_mounted(guest_disk.mount_point(n)))
    for line in sent:
        print(line)
    return 0


# --------------------------------------------------------------------------- #
def _registry() -> dict:
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


def _save_registry(reg: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2, ensure_ascii=False))
    tmp.chmod(0o600)
    tmp.replace(REGISTRY)


def _docker(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *argv], capture_output=True, text=True, check=check)


def _exec_py(name: str, code: str) -> str:
    return _docker("exec", f"sbx-{name}", "python3", "-c", code).stdout.strip()


SEED = r"""
import json, os, sys
secret, owner, rules = sys.argv[1], int(sys.argv[2]), sys.argv[3]
os.makedirs("/app/.sessions", exist_ok=True)
old = os.umask(0o077)
json.dump({"enabled": True, "token": secret, "owner": owner},
          open("/app/.sessions/telegram.json", "w"))
os.umask(old)
p = "/root/.claude/.claude.json"
cfg = json.load(open(p)) if os.path.exists(p) else {}
cfg.update(hasCompletedOnboarding=True, theme="dark", bypassPermissionsModeAccepted=True)
cfg.setdefault("projects", {}).setdefault("/work", {})["hasTrustDialogAccepted"] = True
os.makedirs("/root/.claude", exist_ok=True)
json.dump(cfg, open(p, "w"), indent=2)
md = "/root/.claude/CLAUDE.md"
text = open(md).read() if os.path.exists(md) else ""
for mark, rule in json.loads(rules):          # [[mark, text], ...] rendered by guestctl
    if mark not in text:
        text += "\n" + mark + "\n" + rule
        open(md, "w").write(text)
print("seeded")
"""


def seed_rules(public_url: str, domain_: str, services_max: int = SERVICES_MAX) -> list:
    """The agent's rules for the sandbox's CLAUDE.md, each under its own mark (added once),
    in GUESTS_LANG."""
    return [["<!-- sbx-public-files -->", _t("rule_files", url=public_url)],
            ["<!-- sbx-sites -->", _t("rule_sites", domain=domain_)],
            ["<!-- sbx-services -->", _t("rule_services", max=services_max)]]


def seed_argv(name: str, secret: str, tg: int, public_key: str) -> list:
    """docker exec that writes the sandbox's Telegram bridge config and the agent's rules."""
    rules = seed_rules(f"{public_base_url()}/{public_key}", domain())
    return ["docker", "exec", f"sbx-{name}", "python3", "-c", SEED, secret, str(tg),
            json.dumps(rules, ensure_ascii=False)]


def _wait_running(name: str, seconds: int = 60) -> None:
    for _ in range(seconds):
        r = _docker("inspect", "-f", "{{.State.Running}}", f"sbx-{name}", check=False)
        if r.stdout.strip() == "true":
            return
        time.sleep(1)
    raise SystemExit(f"sbx-{name} did not start")


def cmd_add(a) -> int:
    domain()  # before anything is created
    a.image = a.image or default_image()
    book = ts.GuestBook(DATA / "guests.json")
    reg = _registry()
    if a.name in reg or book.get(a.name):
        raise SystemExit(f"guest {a.name} already exists")
    used = {v["port"] for v in reg.values()}
    port = next(p for p in range(PORT_BASE + 1, PORT_BASE + 900) if p not in used)
    secret = book.add(a.name, a.limit, a.tg)
    password = secrets.token_urlsafe(12)
    public_key = new_public_key(a.name)
    _public_folder(public_key)
    ip = next_ip({v.get("ip") for v in reg.values()})
    disk_gb = int(getattr(a, "disk_gb", 0) or 0)
    try:
        if disk_gb:   # a disk of its own (30.10.2026): the folders can't outgrow it
            guest_disk.create(a.name, Path(PUBLIC_ROOT) / public_key, gb=disk_gb)
        _docker(*container_args(a.name, port, secret, password, a.image, public_key,
                                ip=ip, disk=bool(disk_gb))[1:])
        _wait_running(a.name)
        # entrypoint symlinks /root/.claude.json into the volume; give it a moment
        time.sleep(3)
        subprocess.run(seed_argv(a.name, secret, a.tg, public_key), check=True, capture_output=True,
                       text=True)
        _docker("restart", f"sbx-{a.name}")  # status_server starts the bridge on boot
        _wait_running(a.name)
    except (subprocess.CalledProcessError, SystemExit, RuntimeError, OSError) as exc:  # the disk too
        book.remove(a.name)
        _docker("rm", "-f", f"sbx-{a.name}", check=False)
        err = getattr(exc, "stderr", "") or str(exc)
        raise SystemExit(f"failed, rolled back: {err.strip()[:400]}")
    reg[a.name] = {"port": port, "telegram_id": a.tg, "image": a.image,
                   "public_key": public_key, "ip": ip,
                   "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    if disk_gb:
        reg[a.name]["disk_gb"] = disk_gb
    _save_registry(reg)
    pw_file = DATA / f"{a.name}.dashboard-password"
    pw_file.write_text(password + "\n")
    pw_file.chmod(0o600)
    print(f"guest {a.name}: tg={a.tg} dashboard=127.0.0.1:{port} "
          f"limit={a.limit}/week password-file={pw_file}")
    return 0


def _public_folder(public_key: str) -> Path:
    folder = Path(PUBLIC_ROOT) / public_key
    folder.mkdir(parents=True, exist_ok=True)
    # a guest with its own disk has this folder as a mount, owned by root: chmod only
    # what isn't right yet
    for path, mode in ((folder, 0o755), (Path(PUBLIC_ROOT), 0o711)):  # nginx may enter,
        if path.stat().st_mode & 0o7777 != mode:                       # nobody may list keys
            path.chmod(mode)
    return folder


def _container_env(name: str) -> dict:
    raw = _docker("inspect", "-f", "{{json .Config.Env}}", f"sbx-{name}").stdout
    return dict(e.split("=", 1) for e in json.loads(raw))


def live_terminals(name: str) -> list:
    """tmux sessions open in the sandbox right now (a recreate would close them)."""
    r = _docker("exec", f"sbx-{name}", "sh", "-c", "tmux ls 2>/dev/null || true", check=False)
    return [line for line in r.stdout.splitlines()
            if line.strip() and not line.startswith("hold-")]   # hold-*: tmux placeholder


def cmd_recreate(a) -> int:
    """New container with the current settings; volumes, pass and password kept.
    Refuses while the guest has terminals open (they would be cut off) unless --force."""
    reg = _registry()
    info = reg.get(a.name)
    if not info:
        raise SystemExit(f"no guest {a.name}")
    open_now = live_terminals(a.name)
    if open_now and not getattr(a, "force", False):
        raise SystemExit(_t("ctl_recreate_busy", name=a.name, n=len(open_now)))
    env = _container_env(a.name)
    # a container made before v2.1.0 has only AGENTDECK_PASSWORD (envcompat reads either)
    secret, password = env["ANTHROPIC_AUTH_TOKEN"], envcompat.get("PASSWORD", env=env)
    if not password:
        raise SystemExit(f"sbx-{a.name} has no dashboard password in its environment")
    public_key = info.get("public_key") or new_public_key(a.name)
    _public_folder(public_key)
    image = a.image or info.get("image") or default_image()
    ip = info.get("ip") or next_ip({v.get("ip") for n, v in reg.items() if n != a.name})
    use_disk = disk_ready(a.name, info)          # before anything is removed
    _docker("rm", "-f", f"sbx-{a.name}")
    _docker(*container_args(a.name, info["port"], secret, password, image, public_key,
                            ip=ip, disk=use_disk)[1:])
    _wait_running(a.name)
    time.sleep(3)
    subprocess.run(seed_argv(a.name, secret, info["telegram_id"], public_key), check=True,
                   capture_output=True)
    _docker("restart", f"sbx-{a.name}")
    _wait_running(a.name)
    info.update(public_key=public_key, image=image, ip=ip)
    _save_registry(reg)
    refresh_web(a.name, info["port"])
    print(f"recreated sbx-{a.name}: files -> {public_base_url()}/{public_key}/")
    return 0


def container_state(name: str) -> str:
    """docker's state of sbx-<name> (running, exited, ...), "missing" without one."""
    r = _docker("inspect", "-f", "{{.State.Status}}", f"sbx-{name}", check=False)
    state = (r.stdout or "").strip() if r.returncode == 0 else ""
    return state or "missing"


def guests_report() -> list:
    """Everything `list --json` prints, one dict per guest (sorted by name)."""
    reg = _registry()
    if not reg:
        return []
    book = ts.GuestBook(DATA / "guests.json")
    ledger = ts.Ledger(DATA / "usage.db")
    has_domain = bool(guestenv.domain())
    out = []
    for name, info in sorted(reg.items()):
        g = book.get(name)
        out.append({
            "name": name,
            "telegram_id": int(info.get("telegram_id") or (g.telegram_id if g else 0) or 0),
            "port": int(info.get("port") or 0),
            "state": container_state(name),
            "enabled": bool(g and g.enabled),
            # "five_hour", "seven_day": {used, limit, resets_at} (decision 30.10.2026)
            **ts.usage_windows(g or ts.Guest(name, 0, 0, None, False), ledger),
            "url": f"https://{dashboard_host(name)}" if has_domain and web_exists(name) else None,
            "created": str(info.get("created") or ""),
        })
    return out


def cmd_list(a) -> int:
    rows = guests_report()
    if getattr(a, "json", False):
        print(json.dumps({"guests": rows}, ensure_ascii=False))
        return 0
    for r in rows:
        print(_t("ctl_list_line", name=r["name"], tg=r["telegram_id"], port=r["port"],
                 state=r["state"] if r["state"] != "missing" else _t("ctl_no_container"),
                 on=_t("ts_on") if r["enabled"] else _t("ts_off"),
                 five_used=r["five_hour"]["used"], five_limit=r["five_hour"]["limit"],
                 week_used=r["seven_day"]["used"], week_limit=r["seven_day"]["limit"]))
    return 0


def cmd_limit(a) -> int:
    if a.tokens < 0:
        raise SystemExit("the limit is a number of weighted tokens per week, 0 or more")
    book = ts.GuestBook(DATA / "guests.json")
    try:
        book.set_limit(a.name, a.tokens)
    except KeyError:
        raise SystemExit(f"no guest {a.name}")
    print(f"{a.name}: limit {a.tokens} per week, {ts.five_hour_of(a.tokens)} per 5 hours")
    return 0


CHECK = r"""
import json, socket, sys
opened, closed, targets = json.loads(sys.argv[1])
def tcp(host, port):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, port)); return opened
    except OSError:
        return closed
    finally:
        s.close()
for label, host, port in targets:
    print(f"{label:28} {tcp(host, port)}")
"""


def check_targets() -> list:
    """What `check` probes from inside a sandbox: [label, host, port] (labels in GUESTS_LANG)."""
    return [[_t("ctl_wall_claude"), GW, 8790], [_t("ctl_wall_telegram"), GW, 8792],
            [_t("ctl_wall_ssh"), GW, 22], [_t("ctl_wall_nginx"), GW, 443],
            [_t("ctl_wall_internet"), "example.com", 443]]


def cmd_check(a) -> int:
    spec = json.dumps([_t("ctl_open"), _t("ctl_closed"), check_targets()], ensure_ascii=False)
    print(_docker("exec", f"sbx-{a.name}", "python3", "-c", CHECK, spec).stdout, end="")
    return 0


def cmd_simple(a) -> int:
    book = ts.GuestBook(DATA / "guests.json")
    if a.cmd == "stop":
        _docker("stop", f"sbx-{a.name}")
        book.set_enabled(a.name, False)
    elif a.cmd == "start":
        book.set_enabled(a.name, True)
        _docker("start", f"sbx-{a.name}")
    elif a.cmd == "remove":
        if web_exists(a.name):           # before the port is freed for someone else
            take_down_web(a.name)
        _docker("rm", "-f", f"sbx-{a.name}", check=False)
        book.remove(a.name)
        reg = _registry()
        reg.pop(a.name, None)
        _save_registry(reg)
        print(f"removed sbx-{a.name}; volumes sbx-{a.name}-* kept")
    return 0


# --------------------------------------------------------------------------- #
# sbx.<domain>: the files guests hand out by link
# --------------------------------------------------------------------------- #
def files_host() -> str:
    return f"sbx.{domain()}"


def files_vhost() -> str:
    text = (HERE / "nginx" / "sbx.conf.template").read_text()
    return text.replace("@DOMAIN@", domain()).replace("@PUBLIC_ROOT@", PUBLIC_ROOT)


def cmd_files_web(a) -> int:
    """Open https://sbx.<domain> — DNS, certificate, nginx (once per server)."""
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    host = files_host()
    Path(PUBLIC_ROOT).mkdir(parents=True, exist_ok=True)
    Path(PUBLIC_ROOT).chmod(0o711)
    ops = sitegate.RealOps()
    ops.dns_create(host)
    ops.write_vhost(host, sitegate.render_vhost(host, "127.0.0.1", 1, tls=False))
    ops.reload()
    ops.cert_issue(host)
    ops.write_vhost(host, files_vhost())
    ops.reload()
    print(f"https://{host}/f/<guest>-<key>/<file>")
    return 0


# --------------------------------------------------------------------------- #
# move-domain: the guests' addresses onto GUESTS_DOMAIN, no sandbox restarted
# --------------------------------------------------------------------------- #
def rules_on_new_domain(text: str, old: str, new: str) -> str:
    """The agent's rules (CLAUDE.md) with the module's own addresses on the new domain:
    the files link and the sites pattern; whatever else names `old` is the guest's."""
    return text.replace(f"sbx.{old}/", f"sbx.{new}/").replace(f">.{old}", f">.{new}")


def fix_rules_in_sandbox(name: str, old: str, new: str) -> None:
    if container_state(name) != "running":
        print(f"{name}: not running — its rules get the new domain on the next recreate")
        return
    md = "/root/.claude/CLAUDE.md"
    r = _docker("exec", f"sbx-{name}", "cat", md, check=False)
    out = rules_on_new_domain(r.stdout, old, new)
    if r.returncode != 0 or out == r.stdout:
        print(f"{name}: rules unchanged")
        return
    subprocess.run(["docker", "exec", "-i", f"sbx-{name}", "sh", "-c", f"cat > {md}"],
                   input=out, text=True, capture_output=True, check=True)
    print(f"{name}: rules -> {new}")


def _serve_new(ops, host: str, final: str) -> None:
    """DNS, a certificate, then the full config for a new address."""
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    ops.dns_create(host)
    ops.write_vhost(host, sitegate.render_vhost(host, "127.0.0.1", 1, tls=False))
    ops.reload()
    ops.cert_issue(host)
    ops.write_vhost(host, final)
    ops.reload()


def move_domain(old: str, ops, guests: dict, fix_rules) -> None:
    """Every address the module served on `old` is served on GUESTS_DOMAIN; the old one
    sends everyone there (only once the new one works). Sandboxes keep running."""
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    new = domain()
    old = old.strip().strip(".").lower()
    if not old or old == new:
        raise SystemExit(f"move-domain: --from must be the old domain, not {new!r}")
    pairs = []
    if ops.vhost_exists(f"sbx.{old}"):
        _serve_new(ops, files_host(), files_vhost())
        pairs.append((f"sbx.{old}", files_host()))
    for name, info in sorted(guests.items()):
        was = f"sb-{guestenv.address_of(name, guests)}.{old}"
        if ops.vhost_exists(was):
            _serve_new(ops, dashboard_host(name), dashboard_vhost(name, info["port"]))
            pairs.append((was, dashboard_host(name)))
        fix_rules(name, old, new)
    for was, now in pairs:
        ops.write_vhost(was, sitegate.render_moved(was, now))
        print(f"https://{was} -> https://{now}")
    ops.reload()


def cmd_move_domain(a) -> int:
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    move_domain(a.old, sitegate.RealOps(), _registry(), fix_rules_in_sandbox)
    return 0


# --------------------------------------------------------------------------- #
# doctor: what the setup wizard checks (every outside call can be swapped in tests)
# --------------------------------------------------------------------------- #
def _check(id_: str, title: str, ok: bool, detail: str, fix: str = "") -> dict:
    return {"id": id_, "title": title, "ok": bool(ok), "detail": detail,
            "fix": "" if ok else fix}


def _env_name() -> str:
    return str(guestenv.env_file())


def check_docker(run=subprocess.run) -> dict:
    title = "Docker"
    try:
        r = run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                text=True, timeout=20)
    except FileNotFoundError:
        return _check("docker", title, False, "docker is not installed",
                      "Install Docker (https://docs.docker.com/engine/install/), then run "
                      "guests/install.sh again")
    except (OSError, subprocess.SubprocessError) as exc:
        return _check("docker", title, False, f"docker info failed: {exc}",
                      "Start Docker: sudo systemctl start docker")
    if r.returncode == 0:
        return _check("docker", title, True, f"Docker {(r.stdout or '').strip() or 'is running'}")
    err = (r.stderr or r.stdout or "").strip()
    if "permission denied" in err.lower():
        return _check("docker", title, False, "this user may not use Docker",
                      "sudo usermod -aG docker $USER, then log in again")
    return _check("docker", title, False, err[-200:] or "docker info failed",
                  "Start Docker: sudo systemctl start docker")


def telegram_get_me(token: str, opener=urllib.request.urlopen, timeout: float = 8) -> str:
    """The bot's @username (without @); raises when Telegram refuses or is unreachable."""
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/getMe",
                                 headers={"User-Agent": guestenv.USER_AGENT})
    try:
        with opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram answered {exc.code} {exc.reason}")
    if not data.get("ok"):
        raise RuntimeError(f"Telegram: {data.get('description') or 'not ok'}")
    return str(data["result"].get("username") or "")


def check_bot(settings: dict, get_me=telegram_get_me) -> dict:
    title = "Guest bot"
    token = settings.get("GUEST_BOT_TOKEN") or ""
    if not token:
        return _check("bot", title, False, "no bot token",
                      f"Create a bot with @BotFather and put its token into {_env_name()} "
                      "as GUEST_BOT_TOKEN=...")
    try:
        name = get_me(token)
    except Exception as exc:  # noqa: BLE001 — any failure is the check's answer
        return _check("bot", title, False, f"getMe failed: {exc}",
                      f"Check GUEST_BOT_TOKEN in {_env_name()} (the token from @BotFather)")
    return _check("bot", title, True, f"@{name}")


def check_owner(settings: dict) -> dict:
    title = "Owner's Telegram ID"
    value = (settings.get("OWNER_TG_ID") or "").strip()
    if value.isdigit():
        return _check("owner", title, True, value)
    return _check("owner", title, False, f"not a number: {value!r}" if value else "not set",
                  f"Put your Telegram user id (a number; @userinfobot tells it) into "
                  f"{_env_name()} as OWNER_TG_ID=...")


def resolve_ipv4(host: str) -> list:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None, socket.AF_INET)})
    except OSError:
        return []


def check_domain(domain_: str, server_ip: str, resolve=resolve_ipv4, cf_zone=None) -> dict:
    """GUESTS_DOMAIN is usable: sb-test.<domain> (a wildcard record) or the domain itself
    points at this server — or Cloudflare manages the zone (names are made per guest)."""
    title = "Domain"
    if not domain_:
        return _check("domain", title, False, "not set",
                      f"Put the domain for the guests' addresses into {_env_name()} as "
                      "GUESTS_DOMAIN=example.com (dashboards: sb-<name>.example.com)")
    if not server_ip:
        return _check("domain", title, False, "this server's public IP is unknown",
                      f"Set SERVER_IP=<this server's public IPv4> in {_env_name()}")
    probe = f"sb-test.{domain_}"
    for host in (probe, domain_):
        ips = resolve(host)
        if server_ip in ips:
            return _check("domain", title, True, f"{host} -> {server_ip} (this server)")
        if ips:
            break
    zone = cf_zone(domain_) if cf_zone else None
    if zone:
        return _check("domain", title, True,
                      f"DNS by Cloudflare (zone {zone}): each guest's name is created on demand")
    where = f"{host} -> {', '.join(ips)}" if ips else f"{probe} and {domain_} do not resolve"
    return _check("domain", title, False, f"{where}; this server is {server_ip}",
                  f"Point *.{domain_} (a wildcard A record) at {server_ip}, or put a Cloudflare "
                  "API token (Zone.DNS edit) into the file named by CF_TOKEN_FILE "
                  "(default ~/.cf_token)")


def cloudflare_zone(domain_: str) -> "str | None":
    """The Cloudflare zone for the domain when a token is there and works, else None."""
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    if not sitegate.cf_token_file().exists():
        return None
    try:
        res = sitegate.cloudflare_api("GET", f"zones?name={sitegate.cf_zone()}", timeout=8)
        zones = res.get("result") or []
        return zones[0]["name"] if res.get("success") and zones else None
    except Exception:  # noqa: BLE001
        return None


def unit_active(unit: str, run=subprocess.run) -> bool:
    """A system unit (install.sh), or a user unit (older setups) — either counts."""
    for argv in (["systemctl", "is-active", "--quiet", unit],
                 ["systemctl", "--user", "is-active", "--quiet", unit]):
        try:
            if run(argv, capture_output=True, timeout=10).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    return False


def check_services(is_active=unit_active) -> dict:
    title = "Guest services"
    new, old = UNIT_PREFIXES
    down = [u.removesuffix(".service") for u in UNITS
            if not (is_active(u) or is_active(u.replace(new, old, 1)))]
    if not down:
        return _check("services", title, True, "turnstile, Telegram relay and site gate running")
    return _check("services", title, False, "not running: " + ", ".join(down),
                  "Run guests/install.sh (it writes and starts the units)")


def detect_server_ip() -> str:
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    return sitegate.server_ip()


def doctor(run=subprocess.run, get_me=telegram_get_me, resolve=resolve_ipv4,
           detect_ip=detect_server_ip, cf_zone=cloudflare_zone, is_active=unit_active) -> list:
    s = guestenv.load()
    d = guestenv.domain(s)
    ip = (s.get("SERVER_IP") or "").strip() or (detect_ip() if d else "")
    return [check_docker(run), check_bot(s, get_me), check_owner(s),
            check_domain(d, ip, resolve, cf_zone), check_services(is_active)]


def cmd_doctor(a) -> int:
    checks = doctor()
    if a.json:
        print(json.dumps({"checks": checks}, ensure_ascii=False))
        return 0          # the JSON carries the answer; the wizard reads "ok" per check
    for c in checks:
        print(f"{'OK  ' if c['ok'] else 'FAIL'}  {c['title']}: {c['detail']}")
        if not c["ok"]:
            print(f"      -> {c['fix']}")
    return 0 if all(c["ok"] for c in checks) else 1


def cmd_image_tag(a) -> int:
    print(default_image())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="guestctl.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--tg", type=int, required=True)
    add.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    add.add_argument("--image", default=None)
    add.add_argument("--disk-gb", type=int, default=guest_disk.DEFAULT_GB,
                     help="the guest's own disk in GB (0: Docker volumes, no cap)")
    sub.add_parser("list").add_argument("--json", action="store_true")
    lim = sub.add_parser("limit")
    lim.add_argument("name")
    lim.add_argument("tokens", type=int)
    sub.add_parser("doctor").add_argument("--json", action="store_true")
    sub.add_parser("files-web")
    sub.add_parser("image-tag")
    for c in ("check", "stop", "start", "remove"):
        sub.add_parser(c).add_argument("name")
    sub.add_parser("web").add_argument("name")
    sub.add_parser("session").add_argument("name")
    rec = sub.add_parser("recreate")
    rec.add_argument("name")
    rec.add_argument("--image")
    rec.add_argument("--force", action="store_true")
    mv = sub.add_parser("disk-move")
    mv.add_argument("name")
    mv.add_argument("--gb", type=int, default=guest_disk.DEFAULT_GB)
    mv.add_argument("--image", help="recreate on this image too (one stop for the guest)")
    sub.add_parser("disk-watch")
    ad = sub.add_parser("address")
    ad.add_argument("name")
    ad.add_argument("address", help="sb-<address>.<domain> instead of sb-<name>")
    sub.add_parser("move-domain").add_argument("--from", dest="old", required=True,
                                               help="the domain the guests were on")
    a = ap.parse_args(argv)
    return {"add": cmd_add, "list": cmd_list, "check": cmd_check, "limit": cmd_limit,
            "recreate": cmd_recreate, "web": cmd_web, "session": cmd_session, "doctor": cmd_doctor,
            "files-web": cmd_files_web, "image-tag": cmd_image_tag, "disk-move": cmd_disk_move,
            "disk-watch": cmd_disk_watch, "move-domain": cmd_move_domain,
            "address": cmd_address}.get(a.cmd, cmd_simple)(a)


if __name__ == "__main__":
    sys.exit(main())
