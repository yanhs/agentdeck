#!/usr/bin/env python3
"""End-to-end check of invitations on a THROWAWAY guest "probe" (Telegram id 1 = nobody):
the guest bot's side records the request, the admin's side (invites.Desk — what the
owner's own bot runs) announces it and lets in with the real provisioner (guestctl add +
web: container, pass, DNS, certificate), the real guest book and address — only Telegram
itself is a recorder. Then removes the guest with
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
import invites  # noqa: E402
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
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (agentkennel-e2e)"})
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
    requests = invites.RequestBook(Path(tmp) / "requests.json")
    relay = tr.Relay(book, tg, invites=tr.Invitations(requests, OWNER), info_for=info_for)
    relay.route({"update_id": 1, "message": {"message_id": 1, "chat": {"id": TID},
                 "from": {"id": TID, "first_name": "Probe", "username": NAME}, "text": "/start"}})
    check(tg.texts(OWNER) == [], "the guest bot does not ask the owner itself")
    told = []
    desk = invites.Desk(requests, invites.GuestctlProvisioner(HERE / "guestctl.py"),
                        tell_guest=lambda tid, text: told.append((tid, text)),
                        info_for=info_for, taken=lambda: set(book.names()))
    asked = desk.announcements()
    check(len(asked) == 1 and asked[0][2][0][1] == f"ginv:ok:{TID}",
          "the owner's bot gets the request with Let in / Refuse")
    started = time.time()
    done, _ = desk.decide("ok", str(TID))
    print(f"      provisioning took {time.time() - started:.0f} s", flush=True)
    check(done.startswith("✅"), f"owner told: {done[:90]}")
    welcome = told[-1][1] if told else ""
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
