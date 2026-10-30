"""A guest's own disk (owner's decision 30.10.2026: do not allow overuse, never stop the
sandbox, warn).

The server's disk is ext4 under Docker's overlay2, where Docker can't cap a container's
size (``--storage-opt size`` needs XFS). So the guest's folders get a file system of a
fixed size instead: ``guests/disks/<name>.img`` (sparse ext4), mounted by /etc/fstab at
``guests/disks/<name>/`` before Docker starts, its folders bound into the sandbox in place
of the named volumes. A write that doesn't fit fails ("No space left on device"); the
sandbox keeps running. The guest's shared-files folder lives there too and is bound back
onto ``guests/public/<key>``, where nginx serves it.

What a guest installs into the sandbox's system (Docker's writable layer) can't be capped
on ext4; the watchman (``check``, run by ``guestctl disk-watch``) warns about it, and about
the folders at 80 % and when full — to the guest and to the owner, once per threshold.
It never stops or deletes anything.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
DISKS = HERE / "disks"
# the guest's folders: on its disk, bound into the sandbox (the shared-files folder last)
DIRS: Tuple[Tuple[str, str], ...] = (("claude", "/root/.claude"), ("sessions", "/app/.sessions"),
                                     ("data", "/data"), ("work", "/work"), ("public", "/work/public"))
DEFAULT_GB = 20
# ext4 keeps a few % for itself: when writes already fail, a small disk shows ~98 %
FOLDERS_WARN_PCT, FOLDERS_FULL_PCT = 80, 97
PROGRAMS_WARN, PROGRAMS_HIGH = 8 * 1024 ** 3, 10 * 1024 ** 3
FSTAB_TAG = "# agentkennel guest disk"


def image(name: str) -> Path:
    return DISKS / f"{name}.img"


def mount_point(name: str) -> Path:
    return DISKS / name


def fstab_lines(name: str, public_dir: Path) -> List[str]:
    """/etc/fstab: the image (loop, ext4) and its shared-files folder bound back to where
    nginx serves it — both before Docker starts the sandbox; nofail keeps a broken image
    from stopping the server's boot."""
    img, mnt = image(name), mount_point(name)
    for p in (img, mnt, public_dir):
        if re.search(r"\s", str(p)):
            raise ValueError(f"a path with whitespace can't go into /etc/fstab: {p!r}")
    return [
        # discard: a file deleted in the sandbox gives its space back to the server at once
        f"{img} {mnt} ext4 loop,discard,nofail,x-systemd.before=docker.service 0 0",
        f"{mnt}/public {public_dir} none bind,nofail,x-systemd.requires-mounts-for={mnt},"
        "x-systemd.before=docker.service 0 0",
    ]


def binds(name: str) -> List[str]:
    """docker run arguments: the folders from the guest's disk (in DIRS order)."""
    mnt = mount_point(name)
    out: List[str] = []
    for folder, target in DIRS:
        out += ["-v", f"{mnt}/{folder}:{target}"]
    return out


class RealOps:
    """The server changes (sudo -n; the module's own commands are validated names)."""

    @staticmethod
    def _run(*argv: str) -> str:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''}: "
                               f"{(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout

    def create_image(self, path: Path, gb: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run("truncate", "-s", f"{int(gb)}G", str(path))        # sparse: no space taken yet
        path.chmod(0o600)

    def mkfs(self, path: Path, label: str) -> None:
        self._run("sudo", "-n", "/usr/sbin/mkfs.ext4", "-q", "-F", "-m", "0", "-L", label[:16], str(path))

    def fstab_add(self, lines: Iterable[str]) -> None:
        have = Path("/etc/fstab").read_text()
        new = [ln for ln in lines if ln not in have.splitlines()]
        if not new:
            return
        text = have.rstrip("\n") + "\n" + FSTAB_TAG + "\n" + "\n".join(new) + "\n"
        tmp = DISKS / ".fstab.new"
        tmp.write_text(text)
        self._run("sudo", "-n", "cp", "/etc/fstab", "/etc/fstab.agentkennel-backup")
        self._run("sudo", "-n", "install", "-m", "0644", str(tmp), "/etc/fstab")
        tmp.unlink()
        self._run("sudo", "-n", "systemctl", "daemon-reload")

    def mount(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self._run("sudo", "-n", "mount", str(path))                 # by its /etc/fstab line

    def is_mounted(self, path: Path) -> bool:
        return os.path.ismount(str(path))

    def mkdirs(self, mnt: Path, names: Iterable[str]) -> None:
        for n in names:
            self._run("sudo", "-n", "install", "-d", "-m", "0755", str(Path(mnt) / n))
        self._run("sudo", "-n", "rm", "-rf", str(Path(mnt) / "lost+found"))

    def usage(self, mnt: Path) -> Tuple[int, int]:
        st = os.statvfs(str(mnt))
        total = st.f_blocks * st.f_frsize
        return total - st.f_bfree * st.f_frsize, total

    def programs_size(self, name: str) -> int:
        r = subprocess.run(["docker", "ps", "-s", "--filter", f"name=^/sbx-{name}$",
                            "--format", "{{.Size}}"], capture_output=True, text=True, timeout=300)
        return parse_size(r.stdout.strip()) if r.returncode == 0 else 0


def create(name: str, public_dir: Path, gb: int = DEFAULT_GB, ops=None,
           bind_public: bool = True) -> Path:
    """Make, register and mount the guest's disk; again on a mounted one: nothing new.
    bind_public=False leaves the shared-files folder unbound (a move copies it first)."""
    ops = ops or RealOps()
    img, mnt = image(name), mount_point(name)
    if not ops.is_mounted(mnt):
        if not img.exists():
            ops.create_image(img, gb)
            ops.mkfs(img, f"sbx-{name}")
        ops.fstab_add(fstab_lines(name, public_dir))
        ops.mount(mnt)
        ops.mkdirs(mnt, [d for d, _ in DIRS])
    else:
        ops.fstab_add(fstab_lines(name, public_dir))
    if bind_public and not ops.is_mounted(public_dir):
        Path(public_dir).mkdir(parents=True, exist_ok=True)
        ops.mount(public_dir)
    return mnt


def copy_tree(src: Path, dst: Path) -> None:
    """Everything of one folder onto the disk (owners, modes, links, hard links kept)."""
    RealOps._run("sudo", "-n", "rsync", "-aHAX", "--numeric-ids", f"{src}/", f"{dst}/")


_SIZE = re.compile(r"^\s*([0-9.]+)\s*([kKMGT]?B)\b")
_UNITS = {"B": 1, "kB": 1000, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}


def parse_size(text: str) -> int:
    """Docker's "1.17GB (virtual 1.97GB)" -> bytes of the writable layer (decimal units)."""
    m = _SIZE.match(text or "")
    if not m or m.group(2) not in _UNITS:
        return 0
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def _level(value: float, warn: float, high: float) -> int:
    return 2 if value >= high else 1 if value >= warn else 0


def _gb(n: int) -> str:
    return f"{n / 1024 ** 3:.1f}".rstrip("0").rstrip(".")


def check(guests: Dict[str, Dict], state_path: Path, ops,
          tell_owner: Callable[[str], None], tell_guest: Callable[[int, str], None],
          mounted: Optional[Callable[[str], bool]] = None,
          t: Optional[Callable[..., str]] = None) -> List[str]:
    """The watchman: one warning per threshold crossed upwards (folders 80 % / full,
    programs 8 / 10 GB), to the guest and the owner; a level that falls re-arms it.
    Returns the owner's texts sent. Never stops or deletes anything."""
    t = t or _default_t()
    mounted = mounted or (lambda name: ops.is_mounted(mount_point(name)))
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        state = {}
    sent = []
    for name, info in sorted(guests.items()):
        st = state.setdefault(name, {"folders": 0, "programs": 0})
        tid = int(info.get("telegram_id") or 0)
        if mounted(name):
            used, total = ops.usage(mount_point(name))
            pct = used / total * 100 if total else 0.0
            lvl = _level(pct, FOLDERS_WARN_PCT, FOLDERS_FULL_PCT)
            if lvl > st["folders"]:
                key = "disk_full" if lvl == 2 else "disk_warn"
                f = dict(name=name, used=_gb(used), total=_gb(total), pct=round(pct))
                if tid:
                    tell_guest(tid, t(key + "_guest", **f))
                text = t(key + "_owner", **f)
                tell_owner(text)
                sent.append(text)
            st["folders"] = lvl
        size = ops.programs_size(name)
        lvl = _level(size, PROGRAMS_WARN, PROGRAMS_HIGH)
        if lvl > st["programs"]:
            f = dict(name=name, size=_gb(size), warn=_gb(PROGRAMS_WARN), high=_gb(PROGRAMS_HIGH))
            if tid:
                tell_guest(tid, t("disk_programs_guest", **f))
            text = t("disk_programs_owner", **f)
            tell_owner(text)
            sent.append(text)
        st["programs"] = lvl
    tmp = Path(state_path).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(state_path)
    return sent


def _default_t():
    from texts import t
    return t
