#!/usr/bin/env python3
"""End-to-end check of invitations on a THROWAWAY guest "probe" (Telegram id 1 = nobody):
the real provisioner (guestctl add + web: container, pass, DNS, certificate), the real
guest book and address — only Telegram itself is a recorder. Then removes the guest with
its address and checks the address is gone. Prints PASS/FAIL; exit code = failures.
Needs guests/.env (GUESTS_DOMAIN; DNS as for any guest) and the image install.sh built."""

import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE / "tgrelay"), str(HERE / "turnstile")]
import guestenv  # noqa: E402
import tgrelay as tr  # noqa: E402
import turnstile as ts  # noqa: E402

DATA = HERE / "turnstile" / "data"
SITES = HERE / "nginx" / "sites"
DOMAIN = guestenv.domain() or sys.exit("GUESTS_DOMAIN is not set in guests/.env")
# Telegram is a recorder here: the owner id only has to look like one
NAME, TID, OWNER = "probe", 1, int(guestenv.get("OWNER_TG_ID") or 1000)
HOST = f"https://sb-{NAME}.{DOMAIN}"
fails = 0


def check(ok, label):
    global fails
    print(("PASS  " if ok else "FAIL  ") + label, flush=True)
    fails += 0 if ok else 1


class Recorder:
    def __init__(self):
        self.calls = []

    def call(self, method, params, files=None):
        self.calls.append((method, dict(params)))
        return {"ok": True, "result": {"message_id": 7}}

    def texts(self, chat, method="sendMessage"):
        return [p["text"] for m, p in self.calls if m == method and p.get("chat_id") == chat]


def http_status(url):
    # a browser-like agent: Cloudflare answers "error 1010" (403) to Python-urllib, which
    # made both address checks blind on the first run
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (agentdeck-e2e)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return None


def info_for(name):
    return tr.dashboard_info(name, DATA, SITES)


book = ts.GuestBook(DATA / "guests.json")
if book.get(NAME):
    sys.exit("probe already exists — clean it up first")
tg = Recorder()
with tempfile.TemporaryDirectory() as tmp:
    inv = tr.Invitations(Path(tmp) / "requests.json", OWNER,
                         tr.GuestctlProvisioner(HERE / "guestctl.py"),
                         taken=lambda: set(book.names()), background=False)
    relay = tr.Relay(book, tg, invites=inv, info_for=info_for)
    relay.route({"update_id": 1, "message": {"message_id": 1, "chat": {"id": TID},
                 "from": {"id": TID, "first_name": "Проба", "username": NAME}, "text": "/start"}})
    check(any("Пустить" in t for t in tg.texts(OWNER)), "owner asked «Пустить?»")
    started = time.time()
    relay.route({"update_id": 2, "callback_query": {
        "id": "c", "from": {"id": OWNER}, "data": f"inv:ok:{TID}",
        "message": {"message_id": 7, "chat": {"id": OWNER}}}})
    print(f"      provisioning took {time.time() - started:.0f} s", flush=True)
    done = tg.texts(OWNER, "editMessageText")[-1]
    check(done.startswith("✅"), f"owner told: {done[:90]}")
    welcome = tg.texts(TID)[-1]
    pw_file = DATA / f"{NAME}.dashboard-password"
    check(HOST in welcome and pw_file.exists() and pw_file.read_text().strip() in welcome,
          "guest got the address and password")
    check(book.get(NAME) is not None and book.get(NAME).telegram_id == TID, "guest is in the book")
    image = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", f"sbx-{NAME}"],
                           capture_output=True, text=True).stdout.strip()
    expected = subprocess.run([sys.executable, str(HERE / "guestctl.py"), "image-tag"],
                              capture_output=True, text=True).stdout.strip()
    check(image == expected, f"sandbox runs this version's image ({image})")
    keeper = subprocess.run(["docker", "exec", f"sbx-{NAME}", "curl", "-s", "--max-time", "3",
                             "localhost:9876/status"], capture_output=True, text=True).stdout
    check('"services"' in keeper, "services keeper answers")
    code = None
    for _ in range(30):                           # DNS + certificate settle
        code = http_status(f"{HOST}/login")
        if code == 200:
            break
        time.sleep(5)
    check(code == 200, f"{HOST}/login from the internet: {code}")

# --- clean up: remove takes the address down too ------------------------------- #
key = json.loads((DATA / "sandboxes.json").read_text()).get(NAME, {}).get("public_key", "")
r = subprocess.run([sys.executable, str(HERE / "guestctl.py"), "remove", NAME],
                   capture_output=True, text=True)
check(r.returncode == 0, "guestctl remove")
for v in ("work", "claude", "sessions", "data"):
    subprocess.run(["docker", "volume", "rm", f"sbx-{NAME}-{v}"], capture_output=True)
if key:
    try:
        (HERE / "public" / key).rmdir()
    except OSError:
        pass
pw_file.unlink(missing_ok=True)
check(not (SITES / f"sb-{NAME}.{DOMAIN}.conf").exists(), "vhost removed")
code = http_status(f"{HOST}/login")
check(code != 200, f"address no longer serves a login page: {code}")
print(f"== {fails} failed", flush=True)
sys.exit(fails)
