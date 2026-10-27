#!/usr/bin/env python3
"""Turnstile — guests reach the Claude API on the owner's subscription with a personal pass.

A guest sandbox only knows ``ANTHROPIC_BASE_URL=http://<turnstile>`` and
``ANTHROPIC_AUTH_TOKEN=<pass>``. The turnstile checks the pass, swaps it for the owner's
OAuth login (read from ``~/.claude/oauth.env``, never copied, never sent back), forwards
only ``/v1/messages*`` to api.anthropic.com, records each guest's usage and refuses a
guest who has used up the daily limit. Only hashes of passes are stored.

    turnstile.py serve                       run the gate (env: TURNSTILE_BIND/PORT, ...)
    turnstile.py add <name> --limit N [--tg ID]   new guest, prints the pass ONCE
    turnstile.py list | enable <n> | disable <n> | remove <n> | limit <n> <N>
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import guestenv  # noqa: E402
OAUTH_BETA = "oauth-2025-04-20"
DAY_TZ = ZoneInfo(os.environ.get("TURNSTILE_DAY_TZ", "America/Los_Angeles"))
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
# Request headers that must not reach upstream (the guest's pass, hop-by-hop, length).
_DROP_REQUEST = {
    "host",
    "x-api-key",
    "authorization",
    "content-length",
    "accept-encoding",
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "anthropic-beta",
}
_DROP_RESPONSE = {"transfer-encoding", "content-length", "connection", "keep-alive"}


# --------------------------------------------------------------------------- #
# guests
# --------------------------------------------------------------------------- #
def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass
class Guest:
    name: str
    daily_limit: int
    telegram_id: Optional[int]
    enabled: bool
    unlimited_until: float = 0.0   # unix time; until then the daily limit is lifted

    def limit_lifted(self, now: Optional[float] = None) -> bool:
        return self.unlimited_until > (time.time() if now is None else now)


class GuestBook:
    """Guests in a JSON file (only pass hashes). Re-read when the file changes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime = -1.0
        self._data: Dict = {"guests": {}}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
                self._data.setdefault("guests", {})
                self._mtime = mtime

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime

    def add(self, name: str, daily_limit: int, telegram_id: Optional[int] = None) -> str:
        if not NAME_RE.match(name):
            raise ValueError(f"bad guest name {name!r}: a-z, 0-9, '-', 2-31 chars")
        with self._lock:
            self._load()
            if name in self._data["guests"]:
                raise ValueError(f"guest {name!r} already exists")
            secret = "sbx-" + secrets.token_urlsafe(32)
            self._data["guests"][name] = {
                "pass_sha256": _hash(secret),
                "daily_limit": int(daily_limit),
                "telegram_id": telegram_id,
                "enabled": True,
                "created": datetime.now(DAY_TZ).isoformat(timespec="seconds"),
            }
            self._save()
            return secret

    def _guest(self, name: str, rec: Dict) -> Guest:
        return Guest(
            name=name,
            daily_limit=int(rec.get("daily_limit", 0)),
            telegram_id=rec.get("telegram_id"),
            enabled=bool(rec.get("enabled", True)),
            unlimited_until=float(rec.get("unlimited_until") or 0),
        )

    def lookup(self, secret: str) -> Optional[Guest]:
        if not secret:
            return None
        digest = _hash(secret)
        with self._lock:
            self._load()
            for name, rec in self._data["guests"].items():
                if hmac.compare_digest(rec.get("pass_sha256", ""), digest):
                    return self._guest(name, rec)
        return None

    def get(self, name: str) -> Optional[Guest]:
        with self._lock:
            self._load()
            rec = self._data["guests"].get(name)
            return self._guest(name, rec) if rec else None

    def names(self) -> list:
        with self._lock:
            self._load()
            return sorted(self._data["guests"])

    def _update(self, name: str, **fields) -> None:
        with self._lock:
            self._load()
            if name not in self._data["guests"]:
                raise KeyError(name)
            self._data["guests"][name].update(fields)
            self._save()

    def set_enabled(self, name: str, enabled: bool) -> None:
        self._update(name, enabled=bool(enabled))

    def set_limit(self, name: str, daily_limit: int) -> None:
        self._update(name, daily_limit=int(daily_limit))

    def set_unlimited_until(self, name: str, until: float) -> None:
        """Lift the daily limit until `until` (unix time); it comes back by itself."""
        self._update(name, unlimited_until=float(until))

    def remove(self, name: str) -> None:
        with self._lock:
            self._load()
            self._data["guests"].pop(name, None)
            self._save()


# --------------------------------------------------------------------------- #
# usage
# --------------------------------------------------------------------------- #
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


# The owner's weekly measure (memory: reference_subscription_usage): price weights,
# 1% of the Max 20x week ~ 8.7M weighted tokens.
WEIGHTS = {
    "input_tokens": 1.0,
    "cache_creation_input_tokens": 1.25,
    "cache_read_input_tokens": 0.1,
    "output_tokens": 5.0,
}
WEEK_PERCENT = 8_700_000  # weighted tokens per 1% of the owner's week


def weighted(usage: Dict) -> int:
    """One number per request, in the same weights as the owner's weekly measure."""
    return int(round(sum(w * usage.get(k, 0) for k, w in WEIGHTS.items())))


def _numeric(d: Dict) -> Dict:
    return {k: v for k, v in (d or {}).items() if isinstance(v, int) and not isinstance(v, bool)}


class UsageParser:
    """Pulls model + usage out of a Messages API response, streamed (SSE) or JSON."""

    def __init__(self) -> None:
        self.model: Optional[str] = None
        self.usage: Dict = {}
        self._buf = b""
        self._mode: Optional[str] = None  # "sse" | "json"

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        if self._mode is None:
            head = self._buf.lstrip()
            if not head:
                return
            self._mode = "json" if head[:1] == b"{" else "sse"
        if self._mode == "sse":
            *lines, self._buf = self._buf.split(b"\n")
            for line in lines:
                self._sse_line(line.strip())

    def _sse_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            return
        kind = event.get("type")
        if kind == "message_start":
            msg = event.get("message") or {}
            self.model = msg.get("model") or self.model
            self.usage.update(_numeric(msg.get("usage")))
        elif kind == "message_delta":
            self.usage.update(_numeric(event.get("usage")))

    def close(self) -> None:
        if self._mode == "sse":
            if self._buf.strip():
                self._sse_line(self._buf.strip())
        elif self._mode == "json":
            try:
                body = json.loads(self._buf)
            except ValueError:
                return
            if isinstance(body, dict):
                self.model = body.get("model") or self.model
                self.usage.update(_numeric(body.get("usage")))
        self._buf = b""


class Ledger:
    """Per-request usage in SQLite; the day boundary is the owner's local midnight."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS usage (ts REAL, day TEXT, guest TEXT,"
                " model TEXT, input INT, output INT, cache_create INT, cache_read INT,"
                " weighted INT)"
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS usage_day ON usage(day, guest)")
            self._db.execute("CREATE INDEX IF NOT EXISTS usage_guest_ts ON usage(guest, ts)")
            self._db.commit()

    @staticmethod
    def today() -> str:
        return datetime.now(DAY_TZ).date().isoformat()

    def record(self, guest: str, model: Optional[str], usage: Dict,
               ts_: Optional[float] = None) -> int:
        w = weighted(usage)
        when = time.time() if ts_ is None else ts_
        with self._lock:
            self._db.execute(
                "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    when,
                    datetime.fromtimestamp(when, DAY_TZ).date().isoformat(),
                    guest,
                    model or "",
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("cache_creation_input_tokens", 0),
                    usage.get("cache_read_input_tokens", 0),
                    w,
                ),
            )
            self._db.commit()
        return w

    def spent_today(self, guest: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(weighted),0) FROM usage WHERE day=? AND guest=?",
                (self.today(), guest),
            ).fetchone()
        return int(row[0])

    def spent_since(self, guest: str, since: float) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(weighted),0) FROM usage WHERE ts>=? AND guest=?",
                (since, guest),
            ).fetchone()
        return int(row[0])


class SpendWatch:
    """Tells the owner when a guest spends more than ``threshold`` (1% of the week) within
    ``window`` seconds — a runaway loop or a service calling Claude in circles. At most one
    alert per guest per ``cooldown``; a failing notify never touches the guest's request."""

    def __init__(self, ledger: Ledger, notify: Callable[[str], None],
                 threshold: int = WEEK_PERCENT, window: float = 3600, cooldown: float = 3600,
                 clock: Callable[[], float] = time.time, background: bool = True,
                 log: Callable[[str], None] = lambda line: print(line, file=sys.stderr, flush=True)) -> None:
        self.ledger, self.notify, self.clock, self.log = ledger, notify, clock, log
        self.threshold, self.window, self.cooldown = threshold, window, cooldown
        self.background = background
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, guest: str) -> None:
        now = self.clock()
        spent = self.ledger.spent_since(guest, now - self.window)
        if spent < self.threshold:
            return
        with self._lock:
            if now - self._last.get(guest, float("-inf")) < self.cooldown:
                return
            self._last[guest] = now
        today = self.ledger.spent_today(guest)
        text = (f"⚠️ Песочница {guest}: за последний час потрачено ≈{spent / WEEK_PERCENT:.1f}% "
                f"недели подписки ({spent / 1e6:.1f} млн). Сегодня всего ≈{today / WEEK_PERCENT:.1f}%.\n"
                f"Если это не ожидалось — посмотреть, что крутится: службы гостя (/services) "
                f"или `turnstile.py disable {guest}`.")
        self.log(f"ALERT guest={guest} hour={spent} today={today}")
        if self.background:
            threading.Thread(target=self._send, args=(text,), daemon=True).start()
        else:
            self._send(text)

    def _send(self, text: str) -> None:
        try:
            self.notify(text)
        except Exception as exc:
            self.log(f"ALERT-NOTIFY-FAILED {exc!r}")


def tg_notify(text: str) -> None:
    """The owner's Telegram: GUESTS_NOTIFY_CMD, else the guest bot (guests/.env)."""
    guestenv.notify_owner(text)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def rewrite_headers(headers, owner: str) -> Dict[str, str]:
    """Drop the guest's pass and hop-by-hop headers; put the owner's login in."""
    out: Dict[str, str] = {}
    betas: list = []
    for key, value in headers.items():
        low = key.lower()
        if low == "anthropic-beta":
            betas += [b.strip() for b in value.split(",") if b.strip()]
        if low in _DROP_REQUEST:
            continue
        out[key] = value
    if OAUTH_BETA not in betas:
        betas.append(OAUTH_BETA)
    out["authorization"] = f"Bearer {owner}"
    out["anthropic-beta"] = ",".join(betas)
    out["accept-encoding"] = "identity"
    return out


def _allowed_path(path: str) -> bool:
    bare = path.split("?", 1)[0]
    return bare == "/v1/messages" or bare.startswith("/v1/messages/")


def _error(kind: str, message: str) -> bytes:
    return json.dumps(
        {"type": "error", "error": {"type": kind, "message": message}}, ensure_ascii=False
    ).encode()


def make_server(
    bind: str,
    port: int,
    book: GuestBook,
    ledger: Ledger,
    owner_token: Callable[[], str],
    upstream: Tuple[str, str, int] = ("https", "api.anthropic.com", 443),
    log: Callable[[str], None] = lambda line: print(line, file=sys.stderr, flush=True),
    watch: Optional["SpendWatch"] = None,
) -> ThreadingHTTPServer:
    scheme, up_host, up_port = upstream

    class Gate(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _pass(self) -> str:
            auth = self.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
            return self.headers.get("x-api-key", "").strip()

        def _handle(self) -> None:
            guest = book.lookup(self._pass())
            if guest is None:
                log(f"DENY unknown-pass {self.command} {self.path}")
                return self._reply(401, _error("authentication_error", "Неверный пропуск."))
            if not guest.enabled:
                log(f"DENY disabled guest={guest.name}")
                return self._reply(403, _error("permission_error", "Доступ гостя выключен."))
            if not _allowed_path(self.path):
                log(f"DENY path guest={guest.name} {self.command} {self.path}")
                return self._reply(403, _error("permission_error", "Этот адрес закрыт."))
            spent = ledger.spent_today(guest.name)
            if spent >= guest.daily_limit and not guest.limit_lifted():
                log(f"LIMIT guest={guest.name} spent={spent} limit={guest.daily_limit}")
                return self._reply(
                    429,
                    _error(
                        "rate_limit_error",
                        f"Дневной лимит исчерпан ({spent} из {guest.daily_limit}). "
                        "Сбросится в полночь по Лос-Анджелесу.",
                    ),
                )

            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else None
            conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            parser = UsageParser()
            try:
                conn = conn_cls(up_host, up_port, timeout=900)
                conn.request(self.command, self.path, body=body,
                             headers=rewrite_headers(self.headers, owner_token()))
                resp = conn.getresponse()
            except OSError as exc:
                log(f"UPSTREAM-ERROR guest={guest.name} {exc!r}")
                return self._reply(502, _error("api_error", "Сервер Claude недоступен."))

            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() not in _DROP_RESPONSE:
                    self.send_header(key, value)
            self.send_header("connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    parser.feed(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except OSError as exc:  # guest hung up mid-stream: still count what came
                log(f"CLIENT-GONE guest={guest.name} {exc!r}")
            finally:
                parser.close()
                conn.close()
            used = ledger.record(guest.name, parser.model, parser.usage) if parser.usage else 0
            log(f"PASS guest={guest.name} {self.command} {self.path} {resp.status} "
                f"model={parser.model} weighted={used}")
            if used and watch is not None:
                try:
                    watch.check(guest.name)
                except Exception as exc:
                    log(f"WATCH-ERROR guest={guest.name} {exc!r}")

        do_POST = _handle
        do_GET = _handle

        def log_message(self, *args) -> None:  # our own one-line log instead
            pass

    server = ThreadingHTTPServer((bind, port), Gate)
    server.daemon_threads = True
    return server


def owner_token_from(path: Path) -> Callable[[], str]:
    """Read CLAUDE_CODE_OAUTH_TOKEN from the owner's env file (re-read when it changes)."""
    cache = {"mtime": None, "token": ""}

    def get() -> str:
        mtime = path.stat().st_mtime
        if mtime != cache["mtime"]:
            m = re.search(r"CLAUDE_CODE_OAUTH_TOKEN=(\S+)", path.read_text(encoding="utf-8"))
            if not m:
                raise RuntimeError(f"no CLAUDE_CODE_OAUTH_TOKEN in {path}")
            cache.update(mtime=mtime, token=m.group(1).strip("'\""))
        return cache["token"]

    return get


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    data = Path(os.environ.get("TURNSTILE_DATA", HERE / "data"))
    data.mkdir(parents=True, exist_ok=True)
    book = GuestBook(data / "guests.json")
    ap = argparse.ArgumentParser(prog="turnstile.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--limit", type=int, required=True)
    a.add_argument("--tg", type=int)
    sub.add_parser("list")
    for c in ("enable", "disable", "remove"):
        sub.add_parser(c).add_argument("name")
    lim = sub.add_parser("limit")
    lim.add_argument("name")
    lim.add_argument("value", type=int)
    unl = sub.add_parser("unlimited")
    unl.add_argument("name")
    unl.add_argument("--hours", type=float, required=True)
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        ledger = Ledger(data / "usage.db")
        token = owner_token_from(Path(os.environ.get(
            "TURNSTILE_OAUTH_ENV", Path.home() / ".claude" / "oauth.env")))
        token()  # fail fast if the owner's login is missing
        bind = os.environ.get("TURNSTILE_BIND", "127.0.0.1")
        port = int(os.environ.get("TURNSTILE_PORT", "8790"))
        server = make_server(bind, port, book, ledger, token,
                             watch=SpendWatch(ledger, notify=tg_notify))
        print(f"turnstile on {bind}:{port}, guests: {', '.join(book.names()) or '-'}",
              file=sys.stderr, flush=True)
        server.serve_forever()
    elif args.cmd == "add":
        print(book.add(args.name, args.limit, args.tg))
    elif args.cmd == "list":
        ledger = Ledger(data / "usage.db")
        for name in book.names():
            g = book.get(name)
            state = "вкл" if g.enabled else "ВЫКЛ"
            spent = ledger.spent_today(name)
            lifted = (f" БЕЗ ЛИМИТА до {datetime.fromtimestamp(g.unlimited_until, DAY_TZ).strftime('%d.%m %H:%M')}"
                      if g.limit_lifted() else "")
            print(f"{name}\t{state}\tсегодня {spent} из {g.daily_limit} "
                  f"(≈{spent / WEEK_PERCENT:.2f}% недели, лимит ≈{g.daily_limit / WEEK_PERCENT:.1f}%){lifted}"
                  f"\ttg={g.telegram_id or '-'}")
    elif args.cmd in ("enable", "disable"):
        book.set_enabled(args.name, args.cmd == "enable")
    elif args.cmd == "remove":
        book.remove(args.name)
    elif args.cmd == "limit":
        book.set_limit(args.name, args.value)
    elif args.cmd == "unlimited":
        until = time.time() + args.hours * 3600
        book.set_unlimited_until(args.name, until)
        print(f"{args.name}: без лимита до "
              f"{datetime.fromtimestamp(until, DAY_TZ).strftime('%d.%m %H:%M')} (Лос-Анджелес)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
