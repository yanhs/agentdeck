#!/usr/bin/env python3
"""A timer's job (30.10.2026): move a guest onto its own disk and a new image — only while
the guest is idle: no open terminal and no Claude request for QUIET minutes — then check
the result and tell the owner either way (GUESTS_NOTIFY_CMD / the owner's bot).

    python3 guests/scripts/move_when_idle.py NAME --image IMAGE [--gb 20] [--quiet 30]
                                             [--dry-run] [--timer UNIT]

Exit: 0 done (or the dry run said what it would do), 75 still waiting (nothing told, the
timer tries again), 1 failed (told; the timer is stopped). With --timer, the user timer
UNIT is stopped after 0 or 1, so it runs until the job is settled and no longer.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GUESTS = Path(__file__).resolve().parents[1]
WAIT, DONE, FAILED = 75, 0, 1


class RealWorld:
    def __init__(self):
        sys.path[:0] = [str(GUESTS), str(GUESTS / "turnstile")]
        import guestctl
        import guestenv
        self.gc, self.env = guestctl, guestenv

    def live_terminals(self, name):
        return self.gc.live_terminals(name)

    def minutes_since_request(self, name):
        db = sqlite3.connect(f"file:{self.gc.DATA / 'usage.db'}?mode=ro", uri=True)
        last = db.execute("SELECT MAX(ts) FROM usage WHERE guest=?", (name,)).fetchone()[0]
        return (time.time() - float(last)) / 60 if last else 1e9

    def move(self, name, gb, image):
        r = subprocess.run([sys.executable, str(GUESTS / "guestctl.py"), "disk-move", name,
                            "--gb", str(gb), "--image", image], capture_output=True, text=True,
                           cwd=str(GUESTS), timeout=3600)
        return r.returncode, (r.stdout + r.stderr).strip()[-400:]

    def state(self, name):
        def docker(fmt):
            r = subprocess.run(["docker", "inspect", "-f", fmt, f"sbx-{name}"],
                               capture_output=True, text=True)
            return r.stdout.strip() if r.returncode == 0 else ""
        sys.path.insert(0, str(GUESTS))
        import disk
        url = f"https://{self.gc.dashboard_host(name)}/login"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                login = resp.status
        except urllib.error.HTTPError as e:
            login = e.code
        except OSError:
            login = 0
        return {"running": docker("{{.State.Running}}") == "true",
                "image": docker("{{.Config.Image}}"),
                "disk": disk.RealOps().is_mounted(disk.mount_point(name)),
                "login": login}

    def tell(self, text):
        self.env.notify_owner(text)


def run(name, gb, image, quiet_min, world, dry=False):
    busy = world.live_terminals(name)
    if busy:
        print(f"{name}: waiting — {len(busy)} terminal(s) open")
        return WAIT
    idle = world.minutes_since_request(name)
    if idle < quiet_min:
        print(f"{name}: waiting — the last Claude request was {idle:.0f} min ago")
        return WAIT
    if dry:
        print(f"{name}: idle — would move onto its own {gb} GB disk and {image}")
        return DONE
    rc, out = world.move(name, gb, image)
    if rc != 0:
        world.tell(f"🚨 Гость {name}: перенос на свой диск и новую версию не удался.\n{out}")
        return FAILED
    st = world.state(name)
    wrong = [k for k, ok in (("песочница не запущена", st.get("running")),
                             (f"образ {st.get('image')} вместо {image}", st.get("image") == image),
                             ("диск не смонтирован", st.get("disk")),
                             (f"кабинет отвечает {st.get('login')}", st.get("login") == 200)) if not ok]
    if wrong:
        world.tell(f"🚨 Гость {name}: перенос прошёл, но проверка не сошлась: " + "; ".join(wrong))
        return FAILED
    world.tell(f"✅ Гость {name}: песочница переехала на свой диск ({gb} ГБ) и новую версию "
               f"({image}): видны расход «5h / wk» и своя страница «Сервер». Файлы на месте, "
               "кабинет открывается.")
    return DONE


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--image", required=True)
    ap.add_argument("--gb", type=int, default=20)
    ap.add_argument("--quiet", type=int, default=30, help="minutes without a Claude request")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timer", help="the user timer to stop once settled")
    a = ap.parse_args(argv)
    world = RealWorld()
    try:
        rc = run(a.name, a.gb, a.image, a.quiet, world, dry=a.dry_run)
    except Exception as e:  # noqa: BLE001 — tell, never fail silently
        world.tell(f"🚨 Гость {a.name}: перенос сорвался с ошибкой: {e!r}"[:900])
        rc = FAILED
    if a.timer and rc in (DONE, FAILED) and not a.dry_run:
        subprocess.run(["systemctl", "--user", "stop", a.timer], capture_output=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
