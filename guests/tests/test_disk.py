"""A guest's sandbox has a disk of its own (owner's decision 30.10.2026: "do not allow
overuse, do not stop the sandbox, warn").

The guest's folders — /root/.claude, /app/.sessions, /data, /work and the shared-files
folder /work/public — live on one ext4 file system in a file of a fixed size
(guests/disks/<name>.img, mounted at guests/disks/<name>/ by /etc/fstab, before Docker
starts). It can't grow past its size: a write that doesn't fit fails ("No space left on
device"), the sandbox keeps running. A watchman (`guestctl disk-watch`, every few
minutes) tells the guest and the owner at 80 % and when it is full; it also watches the
programs a guest installs into the sandbox's system (Docker's writable layer, which an
ext4 server can't cap) and warns at 8 and 10 GB. Nothing is ever stopped or deleted.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

GUESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GUESTS))
import disk  # noqa: E402
import guestctl as gc  # noqa: E402

GB = 1024 ** 3


# ── the layout ──────────────────────────────────────────────────────────────────
def test_fstab_mounts_the_image_and_the_shared_folder_before_docker():
    lines = disk.fstab_lines("anna", Path("/srv/g/public/anna-k3y"))
    img, pub = lines
    assert img.split()[:3] == [str(disk.image("anna")), str(disk.mount_point("anna")), "ext4"]
    assert "loop" in img and "nofail" in img and "x-systemd.before=docker.service" in img
    # owner, 30.10.2026: a deleted file gives its space back to the server at once
    assert "discard" in img.split()[3].split(",")
    assert pub.split()[:3] == [f"{disk.mount_point('anna')}/public", "/srv/g/public/anna-k3y", "none"]
    assert "bind" in pub and f"x-systemd.requires-mounts-for={disk.mount_point('anna')}" in pub
    assert "x-systemd.before=docker.service" in pub


def test_no_whitespace_in_an_fstab_path():
    with pytest.raises(ValueError):
        disk.fstab_lines("anna", Path("/srv/my files/anna-k3y"))


def test_the_sandbox_gets_its_folders_from_its_disk():
    argv = gc.container_args("anna", port=18101, secret="s", password="p", image="i",
                             public_key="anna-k3y", disk=True)
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    mnt = disk.mount_point("anna")
    assert mounts == [f"{mnt}/claude:/root/.claude", f"{mnt}/sessions:/app/.sessions",
                      f"{mnt}/data:/data", f"{mnt}/work:/work", f"{mnt}/public:/work/public"]
    env = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-e")
    assert env["AGENTKENNEL_DISK_PATH"] == "/work"        # its Server page shows this disk


def test_without_a_disk_the_volumes_stay():
    argv = gc.container_args("anna", port=18101, secret="s", password="p", image="i",
                             public_key="anna-k3y")
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert "sbx-anna-work:/work" in mounts


# ── making one ─────────────────────────────────────────────────────────────────
class FakeOps:
    def __init__(self):
        self.done, self.fstab, self.mounted = [], [], set()
        self.used, self.total, self.layer = {}, {}, {}

    def create_image(self, path, gb):
        self.done.append(("image", str(path), gb))

    def mkfs(self, path, label):
        self.done.append(("mkfs", str(path), label))

    def fstab_add(self, lines):
        for line in lines:
            if line not in self.fstab:
                self.fstab.append(line)

    def mount(self, path):
        self.mounted.add(str(path))
        self.done.append(("mount", str(path)))

    def is_mounted(self, path):
        return str(path) in self.mounted

    def mkdirs(self, mnt, names):
        self.done.append(("mkdirs", str(mnt), tuple(names)))

    def usage(self, mnt):
        name = Path(mnt).name
        return self.used.get(name, 0), self.total.get(name, 20 * GB)

    def programs_size(self, name):
        return self.layer.get(name, 0)


def test_create_makes_formats_registers_and_mounts(tmp_path):
    ops = FakeOps()
    mnt = disk.create("anna", tmp_path / "public" / "anna-k3y", gb=20, ops=ops)
    assert mnt == disk.mount_point("anna")
    kinds = [d[0] for d in ops.done]
    assert kinds[:2] == ["image", "mkfs"] and ops.done[0][2] == 20
    assert len(ops.fstab) == 2
    assert str(disk.mount_point("anna")) in ops.mounted
    assert ("mkdirs", str(mnt), ("claude", "sessions", "data", "work", "public")) in ops.done
    assert str(tmp_path / "public" / "anna-k3y") in ops.mounted


def test_create_again_changes_nothing(tmp_path):
    ops = FakeOps()
    disk.create("anna", tmp_path / "pub", gb=20, ops=ops)
    before = list(ops.done)
    disk.create("anna", tmp_path / "pub", gb=20, ops=ops)       # mounted: nothing to make
    assert [d for d in ops.done[len(before):] if d[0] in ("image", "mkfs")] == []
    assert len(ops.fstab) == 2


# ── the watchman ───────────────────────────────────────────────────────────────
def test_docker_sizes_are_read():
    assert disk.parse_size("1.17GB (virtual 1.97GB)") == int(1.17 * 1000 ** 3)
    assert disk.parse_size("407kB (virtual 799MB)") == 407_000
    assert disk.parse_size("0B") == 0
    assert disk.parse_size("garbage") == 0


@pytest.fixture
def watch(tmp_path):
    ops = FakeOps()
    told = {"owner": [], "guest": []}

    def run():
        return disk.check({"anna": {"telegram_id": 111}}, tmp_path / "disk-alerts.json", ops,
                          tell_owner=lambda text: told["owner"].append(text),
                          tell_guest=lambda tid, text: told["guest"].append((tid, text)),
                          mounted=lambda name: True)
    return ops, told, run


def test_quiet_below_80(watch):
    ops, told, run = watch
    ops.used["anna"] = 10 * GB
    run()
    assert told == {"owner": [], "guest": []}


def test_80_percent_is_said_once_to_both(watch):
    ops, told, run = watch
    ops.used["anna"] = 16 * GB + 1
    run()
    run()                                           # still 80 %: no repeat
    assert len(told["owner"]) == 1 and len(told["guest"]) == 1
    assert told["guest"][0][0] == 111
    assert "anna" in told["owner"][0] and "80" in told["owner"][0]


def test_full_is_said_and_after_freeing_space_it_can_be_said_again(watch):
    ops, told, run = watch
    ops.used["anna"] = 20 * GB
    run()
    assert len(told["guest"]) == 1                  # straight to "full": one message
    ops.used["anna"] = 5 * GB
    run()                                           # freed: quiet, and re-armed
    ops.used["anna"] = 20 * GB
    run()
    assert len(told["guest"]) == 2


def test_full_is_said_when_writes_already_fail_at_98_percent(watch):
    """ext4 keeps a little for itself: the e2e's 300 MB disk refused writes at 98 %."""
    ops, told, run = watch
    ops.used["anna"] = int(20 * GB * 0.98)
    run()
    assert len(told["owner"]) == 1 and told["owner"][0].startswith("🛑")   # "full", not "80 %"


def test_the_programs_layer_is_watched_too(watch):
    ops, told, run = watch
    ops.layer["anna"] = 8 * GB + 1
    run()
    ops.layer["anna"] = 10 * GB + 1
    run()
    ops.layer["anna"] = 10 * GB + 2
    run()
    assert len(told["owner"]) == 2 and len(told["guest"]) == 2


def test_the_watchman_never_stops_or_deletes(watch):
    ops, told, run = watch
    ops.used["anna"], ops.layer["anna"] = 20 * GB, 30 * GB
    run()
    assert all(d[0] not in ("stop", "remove", "rm") for d in ops.done)


def test_a_guest_without_a_disk_is_skipped_for_folders(tmp_path):
    ops = FakeOps()
    ops.used["anna"] = 20 * GB
    told = []
    disk.check({"anna": {"telegram_id": 1}}, tmp_path / "s.json", ops,
               tell_owner=told.append, tell_guest=lambda tid, text: told.append(text),
               mounted=lambda name: False)
    assert told == []


# ── moving a live guest onto a disk ─────────────────────────────────────────────
def test_move_refuses_while_terminals_are_open(monkeypatch):
    monkeypatch.setattr(gc, "_registry", lambda: {"anna": {"port": 1, "telegram_id": 2,
                                                           "public_key": "anna-k3y"}})
    monkeypatch.setattr(gc, "live_terminals", lambda name: ["cs-1: 1 windows"])
    calls = []
    monkeypatch.setattr(gc, "_docker", lambda *a, **k: calls.append(a))
    with pytest.raises(SystemExit):
        gc.main(["disk-move", "anna"])
    assert calls == []


def test_move_copies_every_volume_and_keeps_the_old_ones(monkeypatch, tmp_path):
    reg = {"anna": {"port": 18101, "telegram_id": 2, "public_key": "anna-k3y"}}
    monkeypatch.setattr(gc, "_registry", lambda: reg)
    monkeypatch.setattr(gc, "_save_registry", lambda r: None)
    monkeypatch.setattr(gc, "live_terminals", lambda name: [])
    ops = FakeOps()
    monkeypatch.setattr(disk, "RealOps", lambda: ops)
    copied, docker_calls, recreated = [], [], []
    monkeypatch.setattr(disk, "copy_tree", lambda src, dst: copied.append((str(src), str(dst))))
    monkeypatch.setattr(gc, "_docker", lambda *a, **k: docker_calls.append(a))
    monkeypatch.setattr(gc, "_volume_dir", lambda vol: Path(f"/var/lib/docker/volumes/{vol}/_data"))
    monkeypatch.setattr(gc, "cmd_recreate", lambda a: recreated.append(a.name) or 0)
    assert gc.main(["disk-move", "anna", "--gb", "20"]) == 0
    mnt = disk.mount_point("anna")
    assert ("/var/lib/docker/volumes/sbx-anna-work/_data", f"{mnt}/work") in copied
    assert ("/var/lib/docker/volumes/sbx-anna-claude/_data", f"{mnt}/claude") in copied
    assert ("/var/lib/docker/volumes/sbx-anna-sessions/_data", f"{mnt}/sessions") in copied
    assert ("/var/lib/docker/volumes/sbx-anna-data/_data", f"{mnt}/data") in copied
    assert (f"{gc.PUBLIC_ROOT}/anna-k3y", f"{mnt}/public") in copied     # shared files too
    assert ("stop", "sbx-anna") in docker_calls
    assert not any(a[:2] == ("volume", "rm") for a in docker_calls)      # old volumes stay
    assert recreated == ["anna"] and reg["anna"]["disk_gb"] == 20


def test_a_failed_disk_rolls_the_new_guest_back(monkeypatch, tmp_path):
    """add: if the disk can't be made (sudo refused, no space), nothing of the guest stays."""
    monkeypatch.setattr(gc, "DATA", tmp_path)
    monkeypatch.setattr(gc, "REGISTRY", tmp_path / "sandboxes.json")
    monkeypatch.setattr(gc, "PUBLIC_ROOT", str(tmp_path / "public"))
    monkeypatch.setattr(gc, "_docker", lambda *a, **k: None)

    def broken(*a, **k):
        raise RuntimeError("sudo: a password is required")
    monkeypatch.setattr(disk, "create", broken)
    with pytest.raises(SystemExit) as e:
        gc.main(["add", "anna", "--tg", "111", "--image", "img"])
    assert "sudo" in str(e.value)
    assert gc.ts.GuestBook(tmp_path / "guests.json").get("anna") is None
    assert not (tmp_path / "sandboxes.json").exists() or "anna" not in json.loads(
        (tmp_path / "sandboxes.json").read_text())


def test_move_can_take_the_new_image_in_the_same_recreate(monkeypatch, tmp_path):
    """One stop for the guest: onto its disk AND onto the new image at once."""
    reg = {"anna": {"port": 18101, "telegram_id": 2, "public_key": "anna-k3y", "image": "old:1"}}
    monkeypatch.setattr(gc, "_registry", lambda: reg)
    monkeypatch.setattr(gc, "_save_registry", lambda r: None)
    monkeypatch.setattr(gc, "live_terminals", lambda name: [])
    monkeypatch.setattr(disk, "RealOps", lambda: FakeOps())
    monkeypatch.setattr(disk, "copy_tree", lambda src, dst: None)
    monkeypatch.setattr(gc, "_docker", lambda *a, **k: None)
    monkeypatch.setattr(gc, "_volume_dir", lambda vol: Path("/v") / vol)
    images = []
    monkeypatch.setattr(gc, "cmd_recreate", lambda a: images.append(a.image) or 0)
    assert gc.main(["disk-move", "anna", "--image", "agentkennel-guest:new"]) == 0
    assert images == ["agentkennel-guest:new"]
