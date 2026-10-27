#!/usr/bin/env python3
"""Telegram turnstile — one real bot token on the host, one fake "Bot API" per guest.

The guest bot's real token lives only here (``guests/.env``, GUEST_BOT_TOKEN). This process
long-polls Telegram, gives each update to the guest whose Telegram ID sent it, and
serves every guest sandbox a Bot API of its own at ``http://<relay>/bot<pass>/<method>``
(the sandbox's AgentDeck bridge points TG_BRIDGE_API_BASE here and uses its turnstile
pass as the "token"). A guest may only write to its own chat, answer its own buttons,
download files from its own messages, and set commands for its own chat; webhook /
logout calls are harmless no-ops; anything else is refused. A stranger is told once
that the bot is closed. Guests are the turnstile's guest book (same pass).
"""

from __future__ import annotations

import email.parser
import http.client
import queue
import email.policy
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "turnstile"))
import guestenv  # noqa: E402
import texts  # noqa: E402
import turnstile as ts  # noqa: E402
from texts import t as _t  # noqa: E402

CHAT_METHODS = {
    "sendMessage", "editMessageText", "editMessageReplyMarkup", "editMessageCaption",
    "editMessageMedia", "deleteMessage", "deleteMessages", "sendChatAction",
    "sendPhoto", "sendDocument", "sendVoice", "sendAudio", "sendVideo",
    "sendAnimation", "sendVideoNote", "sendMediaGroup", "sendSticker",
    "sendLocation", "pinChatMessage", "unpinChatMessage", "unpinAllChatMessages",
    "getChat", "setMessageReaction", "copyMessage", "forwardMessage",
}
NO_OPS = {"deleteWebhook", "setWebhook", "logOut", "close"}
PASS_THROUGH = {"getMe"}
COMMAND_METHODS = {"setMyCommands", "deleteMyCommands", "getMyCommands"}
QUEUE_MAX = 1000
_UPDATE_KINDS = ("message", "edited_message", "callback_query", "my_chat_member",
                 "message_reaction", "chat_member", "poll_answer", "inline_query",
                 "chosen_inline_result")


def _err(code: int, description: str) -> Dict:
    return {"ok": False, "error_code": code, "description": description}


def _sender(update: Dict) -> Optional[int]:
    for kind in _UPDATE_KINDS:
        obj = update.get(kind)
        if isinstance(obj, dict):
            who = obj.get("from") or obj.get("user")
            if isinstance(who, dict) and isinstance(who.get("id"), int):
                return who["id"]
    return None


def _file_ids(obj) -> set:
    found = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "file_id" and isinstance(value, str):
                found.add(value)
            else:
                found |= _file_ids(value)
    elif isinstance(obj, list):
        for item in obj:
            found |= _file_ids(item)
    return found


def web_command() -> Dict:
    return {"command": "web", "description": _t("cmd_web")}


def services_command() -> Dict:
    return {"command": "services", "description": _t("cmd_services")}


SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")   # the keeper's names
SERVICE_ACTIONS = ("restart", "stop", "start")


def _command(update: Dict) -> str:
    text = ((update.get("message") or {}).get("text") or "").strip()
    if not text.startswith("/"):
        return ""
    return text.split()[0].split("@")[0].lower()


def _with_extras(commands, extras=None) -> list:
    extras = (web_command(),) if extras is None else extras
    try:
        items = json.loads(commands) if isinstance(commands, str) else list(commands or [])
    except ValueError:
        items = []
    for extra in extras:
        if not any(isinstance(c, dict) and c.get("command") == extra["command"] for c in items):
            items.append(dict(extra))
    return items


def _ago(seconds: float) -> str:
    minutes = int(max(0, seconds) // 60)
    if minutes < 60:
        return _t("ago_min", m=minutes)
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return _t("ago_h_min", h=hours, m=minutes) if minutes else _t("ago_h", h=hours)
    return _t("ago_d", d=hours // 24)


def services_view(services: Optional[Dict], now: Optional[float] = None) -> Tuple[str, Dict]:
    """The /services message: one line per service + its buttons (GUESTS_LANG)."""
    now = time.time() if now is None else now
    refresh = [{"text": _t("svc_refresh"), "callback_data": "svc:refresh:"}]
    if services is None:
        return _t("svc_unavailable"), {"inline_keyboard": [refresh]}
    if not services:
        return _t("svc_none"), {"inline_keyboard": [refresh]}
    running = sum(1 for s in services.values() if s.get("state") == "running")
    lines = [_t("svc_header", running=running, total=len(services)), ""]
    rows = []
    for name, st in sorted(services.items()):
        state = st.get("state")
        restarts = st.get("restarts") or 0
        tail = _t("svc_restarts", n=restarts) if restarts else ""
        if state == "running":
            since = st.get("since")
            lines.append(_t("svc_running", name=name)
                         + (f" {_ago(now - since)}" if since else "") + tail)
        elif state == "waiting":
            code = st.get("last_exit")
            why = _t("svc_crashed", code=code) if code is not None else _t("svc_starting")
            lines.append(_t("svc_waiting", name=name, why=why, tail=tail))
        elif state == "skipped":
            limit = st.get("limit")
            lines.append(_t("svc_skipped_limit", name=name, limit=limit)
                         if limit else _t("svc_skipped", name=name))
        else:
            lines.append(_t("svc_stopped", name=name))
        if state == "stopped":
            rows.append([{"text": f"▶ {name}", "callback_data": f"svc:start:{name}"}])
        elif state != "skipped":
            rows.append([{"text": f"🔁 {name}", "callback_data": f"svc:restart:{name}"},
                         {"text": f"⏹ {name}", "callback_data": f"svc:stop:{name}"}])
    lines += ["", _t("svc_logs")]
    rows.append(refresh)
    return "\n".join(lines), {"inline_keyboard": rows}


class KeeperClient:
    """Talks to the services keeper inside a sandbox (http://<sandbox ip>:9876)."""

    def __init__(self, base_for, timeout: float = 4) -> None:
        self.base_for = base_for          # guest name -> "http://ip:9876" or None
        self.timeout = timeout

    def status(self, guest: str) -> Optional[Dict]:
        base = self.base_for(guest)
        if not base:
            return None
        try:
            with urllib.request.urlopen(f"{base}/status", timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except (OSError, ValueError):
            return None
        services = data.get("services") if isinstance(data, dict) else None
        if not isinstance(services, dict):
            return None
        return {name: st for name, st in services.items()       # what the keeper itself allows
                if isinstance(st, dict) and SERVICE_NAME_RE.match(str(name))}

    def act(self, guest: str, action: str, svc: str) -> bool:
        base = self.base_for(guest)
        if not base or action not in SERVICE_ACTIONS or not SERVICE_NAME_RE.match(svc):
            return False
        req = urllib.request.Request(f"{base}/{action}/{svc}", data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except (OSError, ValueError):
            return False


def web_text(info: Optional[Dict], welcome: bool, domain: Optional[str] = None) -> str:
    """What /start and /web tell a guest (GUESTS_LANG)."""
    if not info:
        return _t("web_not_ready")
    url, password = info["url"], info.get("password")
    lines = []
    if welcome:
        lines += _t("web_intro").split("\n")
    lines.append(_t("web_url", url=url))
    if password:
        lines += [_t("web_password", password=password), _t("web_change", url=url),
                  _t("web_kept")]
    if welcome:
        domain = domain or guestenv.domain() or _t("domain_placeholder")
        lines += ["", _t("web_sites", domain=domain)]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# invitations: a stranger asks, the owner lets in with a button, the sandbox is made
# --------------------------------------------------------------------------- #
def _who(rec: Dict, tid: str) -> str:
    name = rec.get("first_name") or _t("who_noname")
    return f"{name} (@{rec['username']})" if rec.get("username") else name


class Invitations:
    """Pending requests live in ``path`` (JSON, survives a restart). Only the owner's
    button counts; a request in progress or already decided is not acted on twice."""

    def __init__(self, path, owner_tid: int, provisioner, taken=lambda: set(),
                 background: bool = True, cooldown: float = 600,
                 log=lambda line: print(line, file=sys.stderr, flush=True)) -> None:
        self.path = Path(path)
        self.owner = owner_tid
        self.prov = provisioner
        self.taken = taken
        self.background = background
        self.cooldown = cooldown
        self.log = log
        self._lock = threading.Lock()
        self._busy: set = set()

    # -- storage ---------------------------------------------------------- #
    def _load(self) -> Dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: Dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def choose_name(self, rec: Dict, tid: str) -> str:
        u = (rec.get("username") or "").lower().replace("_", "-")[:31].strip("-")
        if u and ts.NAME_RE.match(u) and u not in self.taken():
            return u
        return f"g{tid}"

    # -- a stranger writes -------------------------------------------------- #
    def on_stranger(self, relay: "Relay", update: Dict, uid: int) -> None:
        who = (update.get("message") or {}).get("from") or {}
        now = time.time()
        tid = str(uid)
        with self._lock:
            data = self._load()
            rec = data.get(tid)
            if rec is None:
                rec = {"first_name": str(who.get("first_name") or "")[:64],
                       "username": str(who.get("username") or "")[:64],
                       "state": "pending", "at": now, "told": now, "nagged": False}
                data[tid] = rec
                self._save(data)
                new = True
            else:
                new = False
                if rec.get("state") == "rejected":
                    relay._tell_stranger(uid)
                    return
                if rec.get("nagged") and now - rec.get("told", 0) < self.cooldown:
                    return
                rec.update(nagged=True, told=now)
                self._save(data)
        if new:
            self.log(f"INVITE-REQUEST tg={uid}")
            name = self.choose_name(rec, tid)
            self._send(relay, self.owner,
                       _t("invite_request", who=_who(rec, tid), uid=uid, name=name),
                       markup=self._buttons(tid))
            self._send(relay, uid, _t("invite_sent"))
        else:
            self._send(relay, uid, _t("invite_waiting"))

    # -- the owner presses -------------------------------------------------- #
    def on_press(self, relay: "Relay", cb: Dict, uid: Optional[int]) -> None:
        parts = str(cb.get("data") or "").split(":")
        action, tid = (parts[1], parts[2]) if len(parts) == 3 else ("", "")
        if uid != self.owner:
            self.log(f"INVITE-DENY presser={uid} data={cb.get('data')!r}")
            self._answer(relay, cb, _t("invite_not_yours"))
            return
        msg_id = (cb.get("message") or {}).get("message_id")
        with self._lock:
            data = self._load()
            rec = data.get(tid)
            if (action not in ("ok", "no") or not tid.isdigit() or rec is None
                    or rec.get("state") in ("approved", "rejected") or tid in self._busy):
                self._answer(relay, cb, _t("invite_decided"))
                return
            if action == "no":
                rec["state"] = "rejected"
                self._save(data)
            else:
                self._busy.add(tid)
        self._answer(relay, cb, "")
        if action == "no":
            self.log(f"INVITE-REJECTED tg={tid}")
            self._edit(relay, msg_id, _t("invite_rejected", who=_who(rec, tid)))
            self._send(relay, int(tid), _t("invite_refused"))
            return
        self._edit(relay, msg_id, _t("invite_creating", who=_who(rec, tid)))
        if self.background:
            threading.Thread(target=self._provision, args=(relay, tid, rec, msg_id),
                             daemon=True).start()
        else:
            self._provision(relay, tid, rec, msg_id)

    def _provision(self, relay: "Relay", tid: str, rec: Dict, msg_id) -> None:
        name = self.choose_name(rec, tid)
        try:
            ok, err = self.prov.provision(name, int(tid))
        except Exception as exc:  # never lose the request over a crash
            ok, err = False, repr(exc)
        with self._lock:
            data = self._load()
            cur = data.get(tid, rec)
            cur.update(state="approved", name=name) if ok else cur.update(state="pending",
                                                                          error=err[-300:])
            data[tid] = cur
            self._save(data)
            self._busy.discard(tid)
        self.log(f"INVITE-{'APPROVED' if ok else 'FAILED'} tg={tid} name={name} {err[-200:]}")
        if ok:
            self._edit(relay, msg_id, _t("invite_created", who=_who(rec, tid), name=name))
            self._send(relay, int(tid), web_text(relay.info_for(name), welcome=True))
        else:
            self._edit(relay, msg_id, _t("invite_create_failed", name=name,
                                         who=_who(rec, tid), err=err[-300:]),
                       markup=self._buttons(tid))
            self._send(relay, int(tid), _t("invite_failed"))

    # -- Telegram ----------------------------------------------------------- #
    @staticmethod
    def _buttons(tid: str) -> Dict:
        return {"inline_keyboard": [[{"text": _t("btn_allow"), "callback_data": f"inv:ok:{tid}"},
                                     {"text": _t("btn_deny"), "callback_data": f"inv:no:{tid}"}]]}

    def _send(self, relay: "Relay", chat: int, text: str, markup: Optional[Dict] = None) -> None:
        params = {"chat_id": chat, "text": text}
        if markup:
            params["reply_markup"] = json.dumps(markup, ensure_ascii=False)
        try:
            relay.tg.call("sendMessage", params)
        except Exception as exc:
            self.log(f"INVITE-SEND-FAILED chat={chat} {exc!r}")

    def _edit(self, relay: "Relay", msg_id, text: str, markup: Optional[Dict] = None) -> None:
        if not msg_id:
            return self._send(relay, self.owner, text, markup)
        params = {"chat_id": self.owner, "message_id": msg_id, "text": text,
                  "reply_markup": json.dumps(markup or {"inline_keyboard": []},
                                             ensure_ascii=False)}
        try:
            relay.tg.call("editMessageText", params)
        except Exception as exc:
            self.log(f"INVITE-EDIT-FAILED {exc!r}")

    def _answer(self, relay: "Relay", cb: Dict, text: str) -> None:
        try:
            relay.tg.call("answerCallbackQuery", {"callback_query_id": cb.get("id"),
                                                  "text": text})
        except Exception as exc:
            self.log(f"INVITE-ANSWER-FAILED {exc!r}")


class GuestctlProvisioner:
    """The real thing: guestctl add (container, pass, limit) + web (address, certificate)."""

    def __init__(self, guestctl: Path, timeout: float = 900) -> None:
        self.guestctl, self.timeout = guestctl, timeout

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


class Relay:
    """Routing and the per-guest rules; Telegram I/O goes through ``telegram``."""

    def __init__(self, book: "ts.GuestBook", telegram, stranger_cooldown: float = 600,
                 log=lambda line: print(line, file=sys.stderr, flush=True),
                 info_for=lambda name: None, services=None, background: bool = True,
                 settle: float = 3.5, invites: Optional[Invitations] = None) -> None:
        self.book = book
        self.tg = telegram
        self.info_for = info_for  # name -> {"url", "password"} of the guest's dashboard
        self.services = services  # .status(guest) / .act(guest, action, svc); None = off
        self._background = background  # /services talks to a sandbox: not on the poll loop
        self._settle = settle          # after an action: the keeper acts on its next tick
        self.invites = invites         # strangers ask, the owner lets in; None = closed bot
        self.log = log
        self._cooldown = stranger_cooldown
        self._cond = threading.Condition()
        self._queues: Dict[str, deque] = {}
        self._callbacks: Dict[str, set] = {}
        self._files: Dict[str, set] = {}
        self._paths: Dict[str, str] = {}
        self._strangers: Dict[int, float] = {}

    # -- incoming ---------------------------------------------------------- #
    def _guest_by_tid(self, tid: int) -> Optional["ts.Guest"]:
        for name in self.book.names():
            guest = self.book.get(name)
            if guest and guest.telegram_id == tid:
                return guest
        return None

    def route(self, update: Dict) -> Optional[str]:
        uid = _sender(update)
        cb = update.get("callback_query")
        if (self.invites is not None and isinstance(cb, dict)
                and str(cb.get("data") or "").startswith("inv:")):
            self.invites.on_press(self, cb, uid)     # the presser is checked inside
            return None
        guest = self._guest_by_tid(uid) if uid is not None else None
        if guest is None:
            if uid is None:
                return None
            if self.invites is not None and uid == self.invites.owner:
                return None                          # the owner without a sandbox
            if self.invites is not None and isinstance(update.get("message"), dict):
                self.invites.on_stranger(self, update, uid)
            else:
                self._tell_stranger(uid)
            return None
        if not guest.enabled:
            self.log(f"DROP disabled guest={guest.name} update={update.get('update_id')}")
            return None
        command = _command(update)
        if command in ("/start", "/web"):  # answered here: the guest's own address
            self._tell_web(guest, welcome=(command == "/start"))
            return guest.name
        if self.services is not None:
            if command == "/services":
                self._later(self._show_services, guest)
                return guest.name
            cb = update.get("callback_query")
            if isinstance(cb, dict) and str(cb.get("data") or "").startswith("svc:"):
                self._later(self._press_service, guest, cb)
                return guest.name
        with self._cond:
            self._queues.setdefault(guest.name, deque(maxlen=QUEUE_MAX)).append(update)
            cb = update.get("callback_query")
            if isinstance(cb, dict) and cb.get("id"):
                self._callbacks.setdefault(guest.name, set()).add(cb["id"])
            self._files.setdefault(guest.name, set()).update(_file_ids(update))
            self._cond.notify_all()
        return guest.name

    def _tell_web(self, guest: "ts.Guest", welcome: bool) -> None:
        text = web_text(self.info_for(guest.name), welcome)
        try:
            self.tg.call("sendMessage", {"chat_id": guest.telegram_id, "text": text})
        except Exception as exc:
            self.log(f"WEB-NOTICE-FAILED guest={guest.name} {exc!r}")

    def _later(self, fn, *args) -> None:
        if self._background:
            threading.Thread(target=fn, args=args, daemon=True).start()
        else:
            fn(*args)

    def _show_services(self, guest: "ts.Guest") -> None:
        text, markup = services_view(self.services.status(guest.name))
        try:
            self.tg.call("sendMessage", {"chat_id": guest.telegram_id, "text": text,
                                         "reply_markup": json.dumps(markup, ensure_ascii=False)})
        except Exception as exc:
            self.log(f"SERVICES-FAILED guest={guest.name} {exc!r}")

    def _press_service(self, guest: "ts.Guest", cb: Dict) -> None:
        """A /services button: act in the presser's OWN sandbox (the guest is found by
        the sender, never by anything in the button), then redraw the message."""
        _, _, rest = str(cb.get("data")).partition(":")
        action, _, svc = rest.partition(":")
        note = ""
        if action in SERVICE_ACTIONS and SERVICE_NAME_RE.match(svc):
            ok = self.services.act(guest.name, action, svc)
            self.log(f"SERVICE guest={guest.name} {action} {svc} ok={ok}")
            note = _t(f"svc_note_{action}", svc=svc) if ok else _t("svc_note_failed")
            if ok and self._settle:
                time.sleep(self._settle)
        elif action != "refresh":
            note = _t("svc_note_unknown")
        try:
            self.tg.call("answerCallbackQuery", {"callback_query_id": cb.get("id"),
                                                 "text": note})
            message = cb.get("message") or {}
            if message.get("message_id"):
                text, markup = services_view(self.services.status(guest.name))
                self.tg.call("editMessageText", {
                    "chat_id": guest.telegram_id, "message_id": message["message_id"],
                    "text": text, "reply_markup": json.dumps(markup, ensure_ascii=False)})
        except Exception as exc:
            self.log(f"SERVICES-FAILED guest={guest.name} {exc!r}")

    def _tell_stranger(self, uid: int) -> None:
        now = time.time()
        if now - self._strangers.get(uid, 0) < self._cooldown:
            return
        self._strangers[uid] = now
        self.log(f"STRANGER tg={uid}")
        try:
            self.tg.call("sendMessage", {"chat_id": uid, "text": _t("stranger")})
        except Exception as exc:  # never let a notice break polling
            self.log(f"STRANGER-NOTICE-FAILED tg={uid} {exc!r}")

    def get_updates(self, guest: str, offset: int, limit: int, timeout: float) -> list:
        deadline = time.time() + max(0.0, float(timeout))
        limit = max(1, min(int(limit or 100), 100))
        with self._cond:
            while True:
                queue = self._queues.setdefault(guest, deque(maxlen=QUEUE_MAX))
                if offset and offset > 0:
                    while queue and queue[0].get("update_id", 0) < offset:
                        queue.popleft()
                if queue:
                    return list(queue)[:limit]
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)

    # -- outgoing ---------------------------------------------------------- #
    def call(self, guest_name: str, method: str, params: Dict,
             files: Optional[Dict] = None) -> Dict:
        guest = self.book.get(guest_name)
        if guest is None or not guest.enabled or guest.telegram_id is None:
            return _err(403, "guest is not active")
        tid = guest.telegram_id
        if method == "getUpdates":
            return {"ok": True, "result": self.get_updates(
                guest_name, int(params.get("offset") or 0),
                int(params.get("limit") or 100), float(params.get("timeout") or 0))}
        if method in NO_OPS:
            return {"ok": True, "result": True}
        if method in PASS_THROUGH:
            return self.tg.call(method, {})
        if method in COMMAND_METHODS:
            scoped = dict(params)
            scoped["scope"] = json.dumps({"type": "chat", "chat_id": tid})
            if method == "setMyCommands":
                extras = (web_command(),) + ((services_command(),) if self.services else ())
                scoped["commands"] = json.dumps(_with_extras(params.get("commands"), extras),
                                                ensure_ascii=False)
            return self.tg.call(method, scoped)
        if method in CHAT_METHODS:
            if "chat_id" not in params:
                return _err(400, "chat_id is required here")
            for key in ("chat_id", "from_chat_id"):
                if key in params and str(params[key]) != str(tid):
                    self.log(f"DENY guest={guest_name} {method} {key}={params[key]}")
                    return _err(403, "Forbidden: only your own chat")
            return self.tg.call(method, params, files)
        if method == "answerCallbackQuery":
            if params.get("callback_query_id") in self._callbacks.get(guest_name, ()):
                return self.tg.call(method, params)
            return _err(403, "Forbidden: not your button")
        if method == "getFile":
            fid = params.get("file_id")
            if fid not in self._files.get(guest_name, ()):
                return _err(403, "Forbidden: not your file")
            res = self.tg.call("getFile", {"file_id": fid})
            path = (res.get("result") or {}).get("file_path") if res.get("ok") else None
            if path:
                self._paths[path] = guest_name
            return res
        self.log(f"DENY guest={guest_name} method={method}")
        return _err(403, f"Forbidden: method {method} is not available")

    def download(self, guest_name: str, file_path: str) -> Optional[bytes]:
        if self._paths.get(file_path) != guest_name:
            return None
        return self.tg.download(file_path)


# --------------------------------------------------------------------------- #
# HTTP: the per-guest fake Bot API
# --------------------------------------------------------------------------- #
def _parse_body(ctype: str, body: bytes) -> Tuple[Dict, Dict]:
    """Return (params, files) from a JSON, urlencoded or multipart request."""
    params: Dict = {}
    files: Dict = {}
    if not body:
        return params, files
    low = ctype.lower()
    if low.startswith("application/json"):
        data = json.loads(body.decode("utf-8") or "{}")
        if isinstance(data, dict):
            params.update(data)
    elif low.startswith("multipart/form-data"):
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            f"Content-Type: {ctype}\r\n\r\n".encode() + body)
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[name] = (filename, payload, part.get_content_type())
            else:
                params[name] = payload.decode("utf-8")
    else:
        params.update(urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    return params, files


def make_server(bind: str, port: int, relay: Relay) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: Dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _guest(self, secret: str) -> Optional[str]:
            guest = relay.book.lookup(secret)
            return guest.name if guest and guest.enabled else None

        def _handle(self) -> None:
            path, _, query = self.path.partition("?")
            if path.startswith("/file/bot"):
                secret, _, file_path = path[len("/file/bot"):].partition("/")
                name = self._guest(secret)
                data = relay.download(name, urllib.parse.unquote(file_path)) if name else None
                if data is None:
                    return self._json(404, _err(404, "Not Found"))
                self.send_response(200)
                self.send_header("content-type", "application/octet-stream")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if not path.startswith("/bot"):
                return self._json(404, _err(404, "Not Found"))
            secret, _, method = path[len("/bot"):].partition("/")
            name = self._guest(secret)
            if name is None:
                return self._json(401, _err(401, "Unauthorized"))
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            try:
                params, files = _parse_body(self.headers.get("content-type", ""), body)
            except (ValueError, UnicodeDecodeError):
                return self._json(400, _err(400, "Bad Request: cannot parse body"))
            params.update(dict(urllib.parse.parse_qsl(query)))
            try:
                result = relay.call(name, method, params, files or None)
            except Exception as exc:
                relay.log(f"ERROR guest={name} {method} {exc!r}")
                return self._json(502, _err(502, "Bad Gateway"))
            status = 200 if result.get("ok") else int(result.get("error_code") or 400)
            self._json(status, result)

        do_GET = _handle
        do_POST = _handle

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


# --------------------------------------------------------------------------- #
# the real Telegram
# --------------------------------------------------------------------------- #
def _encode_multipart(params: Dict, files: Dict) -> Tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = bytearray()
    for key, value in params.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\""
                f"\r\n\r\n{text}\r\n").encode()
    for key, (filename, data, ctype) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {ctype or 'application/octet-stream'}"
                "\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class TelegramAPI:
    """The real Bot API. Keeps a few keep-alive connections (no new DNS lookup and
    TLS handshake per call — at ~0.5 s each, a burst of edits used to queue past
    the sandbox bridge's 5 s timeout and its final answer was lost) and logs how
    long every call took."""

    _RETRYABLE = (http.client.RemoteDisconnected, http.client.CannotSendRequest,
                  http.client.BadStatusLine, ConnectionResetError, BrokenPipeError)

    def __init__(self, token: str, base: str = "https://api.telegram.org",
                 log=lambda line: None) -> None:
        u = urllib.parse.urlsplit(base)
        self._https = u.scheme == "https"
        self._host = u.hostname
        self._port = u.port or (443 if self._https else 80)
        self._prefix = u.path.rstrip("/")
        self._token = token
        self._pool: "queue.LifoQueue" = queue.LifoQueue()
        self.log = log

    def _conn(self, timeout: float):
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
            conn = cls(self._host, self._port, timeout=timeout)
        conn.timeout = timeout
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        return conn

    def _request(self, verb: str, path: str, body, headers: Dict, timeout: float):
        for attempt in (1, 2):
            conn = self._conn(timeout)
            try:
                conn.request(verb, path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
            except self._RETRYABLE:
                conn.close()          # a kept-alive connection the server dropped
                if attempt == 2:
                    raise
                continue
            except Exception:
                conn.close()
                raise
            if (resp.getheader("connection") or "").lower() == "close":
                conn.close()
            else:
                self._pool.put(conn)
            return resp.status, data
        raise RuntimeError("unreachable")

    def call(self, method: str, params: Dict, files: Optional[Dict] = None,
             timeout: float = 30) -> Dict:
        if files:
            body, ctype = _encode_multipart(params, files)
        else:
            flat = {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    for k, v in params.items()}
            body, ctype = urllib.parse.urlencode(flat).encode(), "application/x-www-form-urlencoded"
        started = time.time()
        status, data = self._request(
            "POST", f"{self._prefix}/bot{self._token}/{method}", body,
            {"content-type": ctype, "content-length": str(len(body))}, timeout)
        ms = int((time.time() - started) * 1000)
        if method != "getUpdates" or ms > timeout * 1000:
            self.log(f"TG {method} {status} {ms}ms")
        try:
            return json.loads(data)
        except ValueError:
            return _err(status, "Telegram error")

    def download(self, file_path: str) -> bytes:
        status, data = self._request(
            "GET", f"{self._prefix}/file/bot{self._token}/{urllib.parse.quote(file_path)}",
            None, {}, 120)
        if status != 200:
            raise RuntimeError(f"download failed: HTTP {status}")
        return data


def poll_forever(relay: Relay, api: TelegramAPI) -> None:
    api.call("deleteWebhook", {})
    offset = 0
    while True:
        try:
            res = api.call("getUpdates", {"offset": offset, "timeout": 50}, timeout=65)
        except Exception as exc:
            relay.log(f"POLL-ERROR {exc!r}")
            time.sleep(3)
            continue
        if not res.get("ok"):
            relay.log(f"POLL-NOT-OK {res.get('error_code')} {res.get('description')}")
            time.sleep(5)
            continue
        for update in res.get("result", []):
            offset = max(offset, update.get("update_id", 0) + 1)
            who = relay.route(update)
            relay.log(f"UPDATE {update.get('update_id')} -> {who or '-'}")


def _env_value(key: str) -> Optional[str]:
    return guestenv.get(key)


def _bot_token() -> str:
    token = _env_value("GUEST_BOT_TOKEN")
    if not token:
        raise SystemExit(f"GUEST_BOT_TOKEN not found in {guestenv.env_file()}")
    return token


def dashboard_info(name: str, data: Path, sites: Path) -> Optional[Dict]:
    """sb-<name>.<domain> and its first password, once guestctl web has opened it."""
    domain = guestenv.domain()
    if not domain or not (Path(sites) / f"sb-{name}.{domain}.conf").exists():
        return None
    pw_file = Path(data) / f"{name}.dashboard-password"
    return {"url": f"https://sb-{name}.{domain}",
            "password": pw_file.read_text().strip() if pw_file.exists() else None}


def main() -> int:
    data = Path(os.environ.get("TURNSTILE_DATA", HERE.parent / "turnstile" / "data"))
    book = ts.GuestBook(data / "guests.json")
    api = TelegramAPI(_bot_token(), log=lambda line: print(line, file=sys.stderr, flush=True))

    def info_for(name: str) -> Optional[Dict]:
        return dashboard_info(name, data, HERE.parent / "nginx" / "sites")

    def keeper_base(name: str) -> Optional[str]:
        try:
            reg = json.loads((data / "sandboxes.json").read_text())
        except (OSError, ValueError):
            return None
        ip = (reg.get(name) or {}).get("ip")
        return f"http://{ip}:9876" if ip else None

    owner = _env_value("OWNER_TG_ID")             # set: strangers may ask, the owner decides
    invites = None
    if owner and owner.isdigit():
        def taken() -> set:
            try:
                reg = json.loads((data / "sandboxes.json").read_text())
            except (OSError, ValueError):
                reg = {}
            return set(book.names()) | set(reg)

        invites = Invitations(data / "requests.json", int(owner),
                              GuestctlProvisioner(HERE.parent / "guestctl.py"), taken=taken)
    relay = Relay(book, api, info_for=info_for, services=KeeperClient(keeper_base),
                  invites=invites)
    bind = os.environ.get("TGRELAY_BIND", "127.0.0.1")
    port = int(os.environ.get("TGRELAY_PORT", "8792"))
    server = make_server(bind, port, relay)
    threading.Thread(target=poll_forever, args=(relay, api), daemon=True).start()
    print(f"tgrelay on {bind}:{port}, guests: {', '.join(book.names()) or '-'}, "
          f"invitations: {'on' if invites else 'off'}",
          file=sys.stderr, flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
