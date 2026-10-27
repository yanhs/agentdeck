#!/usr/bin/env python3
"""guestctl — guest sandboxes: one AgentDeck container per person.

    guestctl.py add <name> --tg <telegram id> [--limit N]   create + start, prints access
    guestctl.py web <name>                                   https://sb-<name>.<domain>
    guestctl.py list [--json]                                who, port, state, today's spend
    guestctl.py limit <name> <tokens per day>                change the daily limit
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
import turnstile as ts  # noqa: E402

GW = "172.31.250.1"
IMAGE_NAME = "agentdeck-guest"  # tag = the AgentDeck version (install.sh builds it)
PORT_BASE = 18100
DATA = HERE / "turnstile" / "data"
REGISTRY = DATA / "sandboxes.json"
PUBLIC_ROOT = str(HERE / "public")  # host side of each guest's /work/public
SERVICES_MAX = 10  # services one sandbox may run (keeper + the agent's rule read it)
DEFAULT_LIMIT = 20_000_000  # weighted tokens per day (my default; change with `limit`)
UNITS = ("agentdeck-guest-turnstile.service", "agentdeck-guest-tgrelay.service",
         "agentdeck-guest-sitegate.service")


def repo_version() -> str:
    """This checkout's AgentDeck version (git describe), usable as an image tag."""
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
    return f"sb-{name}.{domain()}"


def dashboard_vhost(name: str, port: int) -> str:
    """nginx for the guest's own AgentDeck dashboard (its password is AgentDeck's)."""
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    return sitegate.render_vhost(dashboard_host(name), "127.0.0.1", port, tls=True)


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
    print(f"https://{host}  (пароль: {DATA / (a.name + '.dashboard-password')})")
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


def container_args(name: str, port: int, secret: str, password: str, image: str,
                   public_key: str, memory: str = "4g", cpus: str = "2",
                   pids: int = 4096, ip: str = "") -> list:
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
        "AGENTDECK_PASSWORD": password,
        "AGENTDECK_SITE": ":8765",
        "TZ": "America/Los_Angeles",
        "AGENTDECK_ORIGIN": f"https://{dashboard_host(name)}",
        "PUBLIC_DIR": "/work/public",
        "PUBLIC_URL": f"{public_base_url()}/{public_key}",
        "SERVICES_MAX": str(SERVICES_MAX),
        # the core services keeper (services_keeper.py) listens on localhost by default;
        # the host's Telegram relay asks it at <sandbox ip>:9876 (other sandboxes can't:
        # the sbx network has inter-container traffic off)
        "SERVICES_BIND": "0.0.0.0",
    }
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    for volume, target in (("claude", "/root/.claude"), ("sessions", "/app/.sessions"),
                           ("data", "/data"), ("work", "/work")):
        argv += ["-v", f"sbx-{name}-{volume}:{target}"]
    # the only host folder: files the guest hands out by link (served by nginx
    # on sbx.<domain> with symlinks and scripts disabled)
    argv += ["-v", f"{PUBLIC_ROOT}/{public_key}:/work/public"]
    argv.append(image)
    return argv


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
secret, owner, domain = sys.argv[1], int(sys.argv[2]), sys.argv[3]
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
mark = "<!-- sbx-public-files -->"
text = open(md).read() if os.path.exists(md) else ""
if mark not in text:
    url = os.environ.get("PUBLIC_URL", "")
    text += ("\n" + mark + "\n## Файлы для пользователя — интернет-ссылкой\n"
             "Чтобы отдать файл пользователю, положи его в `/work/public/` и пришли ссылку\n"
             f"`{url}/<имя файла>` последней строкой сообщения. Имена — латиницей, без пробелов.\n"
             "Всё в `/work/public/` доступно по ссылке любому, у кого она есть; секретов туда не клади.\n")
    open(md, "w").write(text)
mark2 = "<!-- sbx-sites -->"
if mark2 not in text:
    text += ("\n" + mark2 + f"\n## Сайт в интернет: <сайт>-<гость>.{domain}\n"
             "Подними сервер в песочнице на 0.0.0.0:<порт> (1024–65535), затем:\n"
             "`curl -s -X POST http://172.31.250.1:8794/publish -H \"Authorization: Bearer $ANTHROPIC_AUTH_TOKEN\" "
             "-d '{\"site\":\"test\",\"port\":3000}'` — ответ даст адрес "
             f"https://test-<гость>.{domain}\n"
             "(DNS и сертификат создаются ~1 минуту). Список: `GET /sites`, снять: `DELETE /publish/<сайт>`.\n"
             "Не больше 3 сайтов. Владелец сервера получает уведомление о каждом сайте.\n")
    open(md, "w").write(text)
mark3 = "<!-- sbx-services -->"
if mark3 not in text:
    text += ("\n" + mark3 + "\n## Службы: то, что должно работать без терминала\n"
             "Терминал выгружается после 2 часов простоя, и всё запущенное из него (`&`, nohup,\n"
             "tmux) умирает вместе с ним. Программу, которая должна работать постоянно (Tailscale,\n"
             "сервер опубликованного сайта, бот), сделай службой:\n"
             "1. Файл `/work/.services/<имя>.sh` (имя: строчная латиница, цифры, `-`, `_`). Внутри —\n"
             "   команда, которая работает на переднем плане, без `&` и без режима демона,\n"
             "   например `exec python3 /work/site/server.py`.\n"
             "2. Сторож запустит её за несколько секунд, перезапустит после падения; вывод —\n"
             "   `/work/.services/<имя>.log`; состояние — `curl -s localhost:9876/status`.\n"
             "3. `curl -X POST localhost:9876/stop/<имя>` — остановить (файл станет `<имя>.sh.off`),\n"
             "   `/start/<имя>` — запустить, `/restart/<имя>` — перезапустить. Удалить файл — убрать службу.\n"
             f"Не больше {os.environ.get('SERVICES_MAX', '10')} служб. Заведя, остановив или удалив службу, скажи об этом человеку одной\n"
             "строкой: что за служба и зачем. Свои службы человек видит командой /services в Телеграме.\n")
    open(md, "w").write(text)
print("seeded")
"""


def seed_argv(name: str, secret: str, tg: int) -> list:
    """docker exec that writes the sandbox's Telegram bridge config and the agent's rules."""
    return ["docker", "exec", f"sbx-{name}", "python3", "-c", SEED, secret, str(tg), domain()]


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
    try:
        _docker(*container_args(a.name, port, secret, password, a.image, public_key,
                                ip=ip)[1:])
        _wait_running(a.name)
        # entrypoint symlinks /root/.claude.json into the volume; give it a moment
        time.sleep(3)
        subprocess.run(seed_argv(a.name, secret, a.tg), check=True, capture_output=True,
                       text=True)
        _docker("restart", f"sbx-{a.name}")  # status_server starts the bridge on boot
        _wait_running(a.name)
    except (subprocess.CalledProcessError, SystemExit) as exc:
        book.remove(a.name)
        _docker("rm", "-f", f"sbx-{a.name}", check=False)
        err = getattr(exc, "stderr", "") or str(exc)
        raise SystemExit(f"failed, rolled back: {err.strip()[:400]}")
    reg[a.name] = {"port": port, "telegram_id": a.tg, "image": a.image,
                   "public_key": public_key, "ip": ip,
                   "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    _save_registry(reg)
    pw_file = DATA / f"{a.name}.dashboard-password"
    pw_file.write_text(password + "\n")
    pw_file.chmod(0o600)
    print(f"guest {a.name}: tg={a.tg} dashboard=127.0.0.1:{port} "
          f"limit={a.limit}/day password-file={pw_file}")
    return 0


def _public_folder(public_key: str) -> Path:
    folder = Path(PUBLIC_ROOT) / public_key
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)
    Path(PUBLIC_ROOT).chmod(0o711)  # nginx may enter, nobody may list the keys
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
        raise SystemExit(f"у {a.name} открыты терминалы ({len(open_now)}): пересоздание их оборвёт. "
                         "Подождите или --force.")
    env = _container_env(a.name)
    secret, password = env["ANTHROPIC_AUTH_TOKEN"], env["AGENTDECK_PASSWORD"]
    public_key = info.get("public_key") or new_public_key(a.name)
    _public_folder(public_key)
    image = a.image or info.get("image") or default_image()
    ip = info.get("ip") or next_ip({v.get("ip") for n, v in reg.items() if n != a.name})
    _docker("rm", "-f", f"sbx-{a.name}")
    _docker(*container_args(a.name, info["port"], secret, password, image, public_key,
                            ip=ip)[1:])
    _wait_running(a.name)
    time.sleep(3)
    subprocess.run(seed_argv(a.name, secret, info["telegram_id"]), check=True,
                   capture_output=True)
    _docker("restart", f"sbx-{a.name}")
    _wait_running(a.name)
    info.update(public_key=public_key, image=image, ip=ip)
    _save_registry(reg)
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
            "spent_today": int(ledger.spent_today(name)),
            "limit": int(g.daily_limit if g else 0),
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
        print(f"{r['name']}\ttg={r['telegram_id']}\t:{r['port']}\t"
              f"{r['state'] if r['state'] != 'missing' else 'нет контейнера'}\t"
              f"{'вкл' if r['enabled'] else 'ВЫКЛ'}\tсегодня {r['spent_today']} из {r['limit']}")
    return 0


def cmd_limit(a) -> int:
    if a.tokens < 0:
        raise SystemExit("the limit is a number of weighted tokens per day, 0 or more")
    book = ts.GuestBook(DATA / "guests.json")
    try:
        book.set_limit(a.name, a.tokens)
    except KeyError:
        raise SystemExit(f"no guest {a.name}")
    print(f"{a.name}: limit {a.tokens} per day")
    return 0


CHECK = r"""
import socket
def tcp(host, port):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, port)); return "ОТКРЫТ"
    except OSError:
        return "закрыт"
    finally:
        s.close()
for label, host, port in [("турникет Claude", "172.31.250.1", 8790),
                          ("турникет Телеграм", "172.31.250.1", 8792),
                          ("ssh сервера", "172.31.250.1", 22),
                          ("nginx сервера", "172.31.250.1", 443),
                          ("интернет (example.com:443)", "example.com", 443)]:
    print(f"{label:28} {tcp(host, port)}")
"""


def cmd_check(a) -> int:
    print(_docker("exec", f"sbx-{a.name}", "python3", "-c", CHECK).stdout, end="")
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
    down = [u.removesuffix(".service") for u in UNITS if not is_active(u)]
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
    rec = sub.add_parser("recreate")
    rec.add_argument("name")
    rec.add_argument("--image")
    rec.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    return {"add": cmd_add, "list": cmd_list, "check": cmd_check, "limit": cmd_limit,
            "recreate": cmd_recreate, "web": cmd_web, "doctor": cmd_doctor,
            "files-web": cmd_files_web, "image-tag": cmd_image_tag}.get(a.cmd, cmd_simple)(a)


if __name__ == "__main__":
    sys.exit(main())
