#!/usr/bin/env python3
"""guestctl — guest sandboxes: one AgentDeck container per person.

    guestctl.py add <name> --tg <telegram id> [--limit N]   create + start, prints access
    guestctl.py list                                         who, port, state, today's spend
    guestctl.py check <name>                                 network walls from inside
    guestctl.py stop|start <name>                            pause / resume a sandbox
    guestctl.py remove <name>                                remove container + pass (volumes kept)

Each sandbox: docker network "sbx" (see net/sbx-firewall.sh), dashboard on
127.0.0.1:<port> only, memory/CPU/process limits, Claude and Telegram reachable only
through the host turnstiles with the guest's own pass; no host folder is mounted.
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "turnstile"))
import turnstile as ts  # noqa: E402

GW = "172.31.250.1"
IMAGE = "agentdeck-guest:v1.9.0-svc"  # AgentDeck 1.9.0 + the services keeper (image/)
PORT_BASE = 18100
DATA = HERE / "turnstile" / "data"
REGISTRY = DATA / "sandboxes.json"
PUBLIC_ROOT = str(HERE / "public")  # host side of each guest's /work/public
PUBLIC_BASE_URL = "https://sbx.reimake.com/f"
SERVICES_MAX = 10  # services one sandbox may run (keeper + the agent's rule read it)
DEFAULT_LIMIT = 20_000_000  # weighted tokens per day (my default; change with `turnstile.py limit`)


def dashboard_host(name: str) -> str:
    return f"sb-{name}.reimake.com"


def dashboard_vhost(name: str, port: int) -> str:
    """nginx for the guest's own AgentDeck dashboard (its password is AgentDeck's)."""
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    return sitegate.render_vhost(dashboard_host(name), "127.0.0.1", port, tls=True)


def cmd_web(a) -> int:
    """Open https://sb-<name>.reimake.com — DNS, certificate, nginx (once per guest)."""
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
    """Is sb-<name>.reimake.com served (its vhost written by cmd_web)?"""
    sys.path.insert(0, str(HERE / "sitegate"))
    import sitegate  # noqa: E402

    return (sitegate.RealOps.VHOSTS / f"{dashboard_host(name)}.conf").exists()


def take_down_web(name: str, ops=None, delay: float = 90) -> None:
    """Take sb-<name>.reimake.com down without ever falling through to another guest:
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
        "PUBLIC_URL": f"{PUBLIC_BASE_URL}/{public_key}",
        "SERVICES_MAX": str(SERVICES_MAX),
    }
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    for volume, target in (("claude", "/root/.claude"), ("sessions", "/app/.sessions"),
                           ("data", "/data"), ("work", "/work")):
        argv += ["-v", f"sbx-{name}-{volume}:{target}"]
    # the only host folder: files the guest hands out by link (served by nginx
    # on sbx.reimake.com with symlinks and scripts disabled)
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
secret, owner = sys.argv[1], int(sys.argv[2])
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
    text += ("\n" + mark2 + "\n## Сайт в интернет: <сайт>-<гость>.reimake.com\n"
             "Подними сервер в песочнице на 0.0.0.0:<порт> (1024–65535), затем:\n"
             "`curl -s -X POST http://172.31.250.1:8794/publish -H \"Authorization: Bearer $ANTHROPIC_AUTH_TOKEN\" "
             "-d '{\"site\":\"test\",\"port\":3000}'` — ответ даст адрес https://test-<гость>.reimake.com\n"
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


def _wait_running(name: str, seconds: int = 60) -> None:
    for _ in range(seconds):
        r = _docker("inspect", "-f", "{{.State.Running}}", f"sbx-{name}", check=False)
        if r.stdout.strip() == "true":
            return
        time.sleep(1)
    raise SystemExit(f"sbx-{name} did not start")


def cmd_add(a) -> int:
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
        subprocess.run(["docker", "exec", f"sbx-{a.name}", "python3", "-c", SEED,
                        secret, str(a.tg)], check=True, capture_output=True, text=True)
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
    image = a.image or info.get("image", IMAGE)
    ip = info.get("ip") or next_ip({v.get("ip") for n, v in reg.items() if n != a.name})
    _docker("rm", "-f", f"sbx-{a.name}")
    _docker(*container_args(a.name, info["port"], secret, password, image, public_key,
                            ip=ip)[1:])
    _wait_running(a.name)
    time.sleep(3)
    subprocess.run(["docker", "exec", f"sbx-{a.name}", "python3", "-c", SEED,
                    secret, str(info["telegram_id"])], check=True, capture_output=True)
    _docker("restart", f"sbx-{a.name}")
    _wait_running(a.name)
    info.update(public_key=public_key, image=image, ip=ip)
    _save_registry(reg)
    print(f"recreated sbx-{a.name}: files -> {PUBLIC_BASE_URL}/{public_key}/")
    return 0


def cmd_list(a) -> int:
    book = ts.GuestBook(DATA / "guests.json")
    ledger = ts.Ledger(DATA / "usage.db")
    for name, info in sorted(_registry().items()):
        state = _docker("inspect", "-f", "{{.State.Status}}", f"sbx-{name}",
                        check=False).stdout.strip() or "нет контейнера"
        g = book.get(name)
        limit = g.daily_limit if g else 0
        on = "вкл" if g and g.enabled else "ВЫКЛ"
        print(f"{name}\ttg={info['telegram_id']}\t:{info['port']}\t{state}\t{on}\t"
              f"сегодня {ledger.spent_today(name)} из {limit}")
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
                          ("сеть ImmAppeal dev", "10.0.3.1", 22),
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="guestctl.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--tg", type=int, required=True)
    add.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    add.add_argument("--image", default=IMAGE)
    sub.add_parser("list")
    for c in ("check", "stop", "start", "remove"):
        sub.add_parser(c).add_argument("name")
    sub.add_parser("web").add_argument("name")
    rec = sub.add_parser("recreate")
    rec.add_argument("name")
    rec.add_argument("--image")
    rec.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    return {"add": cmd_add, "list": cmd_list, "check": cmd_check,
            "recreate": cmd_recreate, "web": cmd_web}.get(a.cmd, cmd_simple)(a)


if __name__ == "__main__":
    sys.exit(main())
