"""Invitations — two sides, two bots (decision 27.10.2026, docs/PLAN-2.0.md).

The guest bot is open to everyone, so it may only *record* a request: RequestBook.ask().
The admin's own bot (AgentDeck's Telegram bridge, tg_bridge.py) is where "Let in?" is
asked and answered: Desk.announcements() / Desk.decide() — and only this side can run
guestctl. The person hears back through the guest bot (Desk.tell_guest).

requests.json is shared by the two processes; every read-modify-write holds an flock
on requests.lock next to it. A record: {first_name, username, at, told, nagged,
state: pending | busy | approved | rejected, announced (message id), name, error}.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guestenv  # noqa: E402
from texts import t as _t  # noqa: E402

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")    # a sandbox name (turnstile's rule)


def who(rec: Dict) -> str:
    name = rec.get("first_name") or _t("who_noname")
    return f"{name} (@{rec['username']})" if rec.get("username") else name


class RequestBook:
    def __init__(self, path, cooldown: float = 600) -> None:
        self.path = Path(path)
        self.cooldown = cooldown

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> Dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: Dict) -> None:
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)

    def get(self, tid) -> Optional[Dict]:
        return self._load().get(str(tid))

    # -- the guest bot's side --------------------------------------------- #
    def ask(self, tid: int, first_name: str, username: str, now: Optional[float] = None) -> str:
        """A person who is not a guest wrote: new | waiting | quiet | rejected."""
        now = time.time() if now is None else now
        key = str(tid)
        with self._locked():
            data = self._load()
            rec = data.get(key)
            if rec is not None and rec.get("state") == "rejected":
                return "rejected"
            if rec is None or rec.get("state") == "approved":      # approved: removed since
                data[key] = {"first_name": str(first_name or "")[:64],
                             "username": str(username or "")[:64], "state": "pending",
                             "at": now, "told": now, "nagged": False}
                self._save(data)
                return "new"
            if rec.get("nagged") and now - rec.get("told", 0) < self.cooldown:
                return "quiet"
            rec.update(nagged=True, told=now)
            self._save(data)
            return "waiting"

    # -- the admin's side --------------------------------------------------- #
    def unannounced(self) -> List[Tuple[str, Dict]]:
        return [(k, r) for k, r in sorted(self._load().items(), key=lambda kv: kv[1].get("at", 0))
                if r.get("state") == "pending" and not r.get("announced")]

    def mark_announced(self, tid, msg_id) -> None:
        with self._locked():
            data = self._load()
            if str(tid) in data:
                data[str(tid)]["announced"] = msg_id or True
                self._save(data)

    def claim(self, tid) -> Optional[Dict]:
        """pending -> busy (one press wins); the record, or None."""
        with self._locked():
            data = self._load()
            rec = data.get(str(tid))
            if not rec or rec.get("state") != "pending":
                return None
            rec["state"] = "busy"
            self._save(data)
            return dict(rec)

    def finish(self, tid, ok: bool, name: str = "", err: str = "") -> None:
        with self._locked():
            data = self._load()
            rec = data.setdefault(str(tid), {})
            if ok:
                rec.update(state="approved", name=name)
                rec.pop("error", None)
            else:
                rec.update(state="pending", error=err[-300:])
            self._save(data)

    def reject(self, tid) -> Optional[Dict]:
        with self._locked():
            data = self._load()
            rec = data.get(str(tid))
            if not rec or rec.get("state") != "pending":
                return None
            rec["state"] = "rejected"
            self._save(data)
            return dict(rec)


class GuestctlProvisioner:
    """guestctl add (container, pass, limit) + web (address, certificate)."""

    def __init__(self, guestctl: Path = HERE / "guestctl.py", timeout: float = 900) -> None:
        self.guestctl, self.timeout = Path(guestctl), timeout

    def provision(self, name: str, tid: int) -> Tuple[bool, str]:
        for argv in (["add", name, "--tg", str(tid)], ["web", name]):
            try:
                r = subprocess.run([sys.executable, str(self.guestctl), *argv],
                                   capture_output=True, text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                return False, f"guestctl {argv[0]}: timeout"
            if r.returncode != 0:
                return False, f"guestctl {argv[0]}: " + (r.stderr or r.stdout).strip()[-300:]
        return True, ""


Buttons = List[Tuple[str, str]]


class Desk:
    """What the admin's bot shows and does. tell_guest(tid, text) speaks through the
    guest bot; info_for(name) -> {url, password} of the new sandbox's dashboard."""

    def __init__(self, book: RequestBook, provisioner, tell_guest: Callable[[int, str], None],
                 info_for: Callable[[str], Optional[Dict]], taken: Callable[[], set],
                 log=lambda line: print(line, file=sys.stderr, flush=True)) -> None:
        self.book, self.prov, self.tell_guest = book, provisioner, tell_guest
        self.info_for, self.taken, self.log = info_for, taken, log

    @staticmethod
    def buttons(tid: str) -> Buttons:
        return [(_t("btn_allow"), f"ginv:ok:{tid}"), (_t("btn_deny"), f"ginv:no:{tid}")]

    def choose_name(self, rec: Dict, tid: str) -> str:
        u = (rec.get("username") or "").lower().replace("_", "-")[:31].strip("-")
        if u and NAME_RE.match(u) and u not in self.taken():
            return u
        return f"g{tid}"

    def announcements(self) -> List[Tuple[str, str, Buttons]]:
        return [(tid, _t("invite_request", who=who(rec), uid=tid,
                         name=self.choose_name(rec, tid)), self.buttons(tid))
                for tid, rec in self.book.unannounced()]

    def creating_text(self, tid: str) -> Optional[str]:
        """What the admin's message says while "Let in" works; None if not pending."""
        rec = self.book.get(tid) if str(tid).isdigit() else None
        if not rec or rec.get("state") != "pending":
            return None
        return _t("invite_creating", who=who(rec))

    def _tell(self, tid: int, text: str) -> None:
        try:
            self.tell_guest(tid, text)
        except Exception as exc:  # the decision stands even if the message is lost
            self.log(f"INVITE-TELL-FAILED tg={tid} {exc!r}")

    def decide(self, action: str, tid: str) -> Tuple[str, Optional[Buttons]]:
        """A button: (the admin message's new text, its buttons or None). Blocking —
        a sandbox takes a minute or two; call it off the event loop."""
        if action not in ("ok", "no") or not str(tid).isdigit():
            return _t("invite_decided"), None
        if action == "no":
            rec = self.book.reject(tid)
            if rec is None:
                return _t("invite_decided"), None
            self.log(f"INVITE-REJECTED tg={tid}")
            self._tell(int(tid), _t("invite_refused"))
            return _t("invite_rejected", who=who(rec)), None
        rec = self.book.claim(tid)
        if rec is None:
            return _t("invite_decided"), None
        name = self.choose_name(rec, tid)
        try:
            ok, err = self.prov.provision(name, int(tid))
        except Exception as exc:  # never lose the request over a crash
            ok, err = False, repr(exc)
        self.book.finish(tid, ok, name=name, err=err)
        self.log(f"INVITE-{'APPROVED' if ok else 'FAILED'} tg={tid} name={name} {err[-200:]}")
        if not ok:
            self._tell(int(tid), _t("invite_failed"))
            return (_t("invite_create_failed", name=name, who=who(rec), err=err[-300:]),
                    self.buttons(tid))
        sys.path.insert(0, str(HERE / "tgrelay"))
        import tgrelay  # the welcome text is the guest bot's own /start answer
        self._tell(int(tid), tgrelay.web_text(self.info_for(name), welcome=True))
        return _t("invite_created", who=who(rec), name=name), None


def guest_bot_sender(token: str, base: str = "https://api.telegram.org"):
    """tell_guest for the real thing: sendMessage through the guest bot."""
    def send(tid: int, text: str) -> None:
        body = urllib.parse.urlencode({"chat_id": tid, "text": text}).encode()
        req = urllib.request.Request(f"{base}/bot{token}/sendMessage", data=body,
                                     headers={"User-Agent": guestenv.USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as r:
            if not json.loads(r.read()).get("ok"):
                raise RuntimeError("sendMessage not ok")
    return send


def desk_for_admin(owner_id: int, log=lambda line: print(line, file=sys.stderr, flush=True)
                   ) -> Optional[Desk]:
    """The Desk for the admin's bot, or None: the module isn't set up, or its admin
    (OWNER_TG_ID) is not this bot's owner."""
    env = guestenv.load()
    owner, token = str(env.get("OWNER_TG_ID") or ""), env.get("GUEST_BOT_TOKEN") or ""
    if not token or not owner.isdigit() or int(owner) != int(owner_id or 0):
        return None
    data = Path(os.environ.get("TURNSTILE_DATA") or HERE / "turnstile" / "data")

    def info_for(name: str) -> Optional[Dict]:
        sys.path.insert(0, str(HERE / "tgrelay"))
        import tgrelay
        return tgrelay.dashboard_info(name, data, HERE / "nginx" / "sites")

    def taken() -> set:
        names = set()
        for f in ("guests.json", "sandboxes.json"):
            try:
                names |= set(json.loads((data / f).read_text()))
            except (OSError, ValueError, TypeError):
                pass
        return names

    return Desk(RequestBook(data / "requests.json"), GuestctlProvisioner(),
                guest_bot_sender(token), info_for, taken, log=log)
