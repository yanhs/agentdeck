#!/usr/bin/env python3
"""End-to-end check of a guest's own disk (guests/disk.py, 30.10.2026) on a THROWAWAY
sandbox from IMAGE with a 300 MB disk: the folders come from the disk, a write past its
size fails inside the sandbox while the sandbox keeps running, the sandbox's Server page
shows that disk, and the watchman warns once. /etc/fstab is NOT touched here (its lines
are unit-tested): the disk is mounted directly and unmounted at the end. Nothing of the
server's own is touched. Prints PASS/FAIL; exit code = failures.

    python3 guests/scripts/e2e_disk.py IMAGE
"""
import http.cookiejar
import json
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GUESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GUESTS))
import disk  # noqa: E402

NAME, PORT, MB = "e2edisk", 18391, 300
fails = 0


def check(ok, label):
    global fails
    print(("PASS  " if ok else "FAIL  ") + label, flush=True)
    fails += 0 if ok else 1


def sh(*argv, check_=True):
    return subprocess.run(list(argv), capture_output=True, text=True, check=check_)


class DirectOps(disk.RealOps):
    """RealOps without /etc/fstab: mount the image and the shared folder directly."""
    def __init__(self, public_dir):
        self.public_dir = public_dir

    def fstab_add(self, lines):
        pass

    def mount(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)
        if Path(path) == disk.mount_point(NAME):
            self._run("sudo", "-n", "mount", "-o", "loop", str(disk.image(NAME)), str(path))
        else:
            self._run("sudo", "-n", "mount", "--bind", f"{disk.mount_point(NAME)}/public", str(path))

    def create_image(self, path, gb):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run("truncate", "-s", f"{MB}M", str(path))


def main(image):
    tmp = Path(tempfile.mkdtemp(prefix="e2edisk-"))
    disk.DISKS = tmp / "disks"
    public_dir = tmp / "public" / f"{NAME}-k3y"
    ops = DirectOps(public_dir)
    pw = secrets.token_urlsafe(12)
    try:
        mnt = disk.create(NAME, public_dir, gb=1, ops=ops)
        check(ops.is_mounted(mnt) and ops.is_mounted(public_dir), f"disk mounted at {mnt}")
        used, total = ops.usage(mnt)
        check(250 * 2**20 < total <= MB * 2**20, f"its size: {total // 2**20} MB")
        sh("docker", "rm", "-f", "-v", f"sbx-{NAME}", check_=False)
        sh("docker", "run", "-d", "--name", f"sbx-{NAME}", "--memory", "1g", "--cpus", "1",
           "-p", f"127.0.0.1:{PORT}:8765", "-e", f"AGENTKENNEL_PASSWORD={pw}",
           "-e", "AGENTKENNEL_SITE=:8765", "-e", "AGENTKENNEL_DISK_PATH=/work",
           *disk.binds(NAME), image)
        base = f"http://127.0.0.1:{PORT}"
        for _ in range(120):
            try:
                urllib.request.urlopen(base + "/login", timeout=3)
                break
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(1)
        df = sh("docker", "exec", f"sbx-{NAME}", "df", "-B1M", "--output=size", "/work").stdout.split()
        check(df and int(df[-1]) <= MB, f"/work inside the sandbox is the disk: {df[-1] if df else '?'} MB")
        w = sh("docker", "exec", f"sbx-{NAME}", "sh", "-c",
               f"dd if=/dev/zero of=/work/big bs=1M count={MB + 100} 2>&1", check_=False)
        check(w.returncode != 0 and "No space left" in w.stdout, "a write past the size fails: "
              + (w.stdout.strip().splitlines() or ["?"])[-1][:120])
        alive = sh("docker", "exec", f"sbx-{NAME}", "sh", "-c", "echo alive", check_=False)
        check(alive.stdout.strip() == "alive" and
              sh("docker", "inspect", "-f", "{{.State.Running}}", f"sbx-{NAME}").stdout.strip() == "true",
              "the sandbox keeps running")
        sh("docker", "exec", f"sbx-{NAME}", "sh", "-c", "echo hello > /work/public/hi.txt")
        check((public_dir / "hi.txt").read_text().strip() == "hello",
              "the shared-files folder on the host sees the sandbox's file")
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        op.open(base + "/login", data=urllib.parse.urlencode({"pass": pw}).encode(), timeout=10)
        h = {}
        for _ in range(15):
            h = json.loads(op.open(base + "/api/server", timeout=10).read()).get("host", {})
            if h:
                break
            time.sleep(1)
        check(0 < h.get("disk_total_gb", 0) <= 0.4 and h.get("disk_pct", 0) >= 99,
              f"its Server page shows its disk: {h.get('disk_used_gb')} of {h.get('disk_total_gb')} GB "
              f"({h.get('disk_pct')}%)")
        told = {"owner": [], "guest": []}
        for _ in range(2):
            disk.check({NAME: {"telegram_id": 42}}, tmp / "alerts.json", ops,
                       tell_owner=told["owner"].append,
                       tell_guest=lambda tid, text: told["guest"].append((tid, text)),
                       mounted=lambda n: True)
        check(len(told["owner"]) == 1 and len(told["guest"]) == 1 and told["guest"][0][0] == 42,
              f"the watchman warned once: {told['owner'][:1]}")
    except Exception as e:
        check(False, f"the check itself stopped: {e!r}")
    finally:
        sh("docker", "rm", "-f", "-v", f"sbx-{NAME}", check_=False)
        for p in (public_dir, disk.mount_point(NAME)):
            sh("sudo", "-n", "umount", str(p), check_=False)
        sh("sudo", "-n", "rm", "-rf", str(tmp), check_=False)
    print(f"{'OK' if not fails else 'FAILED'}: {fails} failure(s)")
    return fails


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
