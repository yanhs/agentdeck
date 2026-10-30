#!/usr/bin/env python3
"""Turnstile — guests reach the Claude API on the owner's subscription with a personal pass.

A guest sandbox only knows ``ANTHROPIC_BASE_URL=http://<turnstile>`` and
``ANTHROPIC_AUTH_TOKEN=<pass>``. The turnstile checks the pass, swaps it for the owner's
OAuth login (read from ``~/.claude/oauth.env``, never copied, never sent back), forwards
only ``/v1/messages*`` to api.anthropic.com, records each guest's usage and refuses a
guest who has used up the 5-hour or the weekly limit (like the subscription's; decision
30.10.2026). Only hashes of passes are stored.

    turnstile.py serve                       run the gate (env: TURNSTILE_BIND/PORT, ...)
    turnstile.py add <name> --limit N [--tg ID]   new guest (N: per week), prints the pass ONCE
    turnstile.py list | enable <n> | disable <n> | remove <n> | limit <n> <N>
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import guestenv  # noqa: E402
import texts  # noqa: E402
from texts import t as _t  # noqa: E402
OAUTH_BETA = "oauth-2025-04-20"
DAY_TZ = ZoneInfo(os.environ.get("TURNSTILE_DAY_TZ", "America/Los_Angeles"))
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
FIVE_HOURS = 5 * 3600.0
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


def five_hour_of(week_limit: int) -> int:
    """The 5-hour limit that goes with a weekly one: a seventh, rounded, never 0 out of
    something (a week of 3 tokens still lets one request in)."""
    week_limit = int(week_limit)
    return max(1, round(week_limit / 7)) if week_limit > 0 else 0


@dataclass
class Guest:
    name: str
    five_hour_limit: int
    week_limit: int
    telegram_id: Optional[int]
    enabled: bool
    unlimited_until: float = 0.0   # unix time; until then both limits are lifted

    def limit_lifted(self, now: Optional[float] = None) -> bool:
        return self.unlimited_until > (time.time() if now is None else now)


class GuestBook:
    """Guests in a JSON file (only pass hashes). Re-read when the file changes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._seen: Optional[Tuple[int, int, int]] = None   # the file's stat when read
        self._data: Dict = {"guests": {}}
        self._load()

    @staticmethod
    def _stat(path: Path) -> Tuple[int, int, int]:
        # mtime alone misses a rewrite within the same clock tick
        st = path.stat()
        return st.st_mtime_ns, st.st_size, st.st_ino

    def _load(self) -> None:
        if self.path.exists():
            seen = self._stat(self.path)
            if seen != self._seen:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
                self._data.setdefault("guests", {})
                self._seen = seen

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        self._seen = self._stat(self.path)

    @staticmethod
    def _limits(week_limit: int, five_hour_limit: Optional[int] = None) -> Dict:
        """The record's limit fields. daily_limit (= the 5 hours) is for a turnstile still
        running the code before 30.10.2026: it reads that and nothing else."""
        week = int(week_limit)
        five = five_hour_of(week) if five_hour_limit is None else int(five_hour_limit)
        return {"five_hour_limit": five, "week_limit": week, "daily_limit": five}

    def add(self, name: str, week_limit: int, telegram_id: Optional[int] = None,
            five_hour_limit: Optional[int] = None) -> str:
        if not NAME_RE.match(name):
            raise ValueError(f"bad guest name {name!r}: a-z, 0-9, '-', 2-31 chars")
        with self._lock:
            self._load()
            if name in self._data["guests"]:
                raise ValueError(f"guest {name!r} already exists")
            secret = "sbx-" + secrets.token_urlsafe(32)
            self._data["guests"][name] = {
                "pass_sha256": _hash(secret),
                **self._limits(week_limit, five_hour_limit),
                "telegram_id": telegram_id,
                "enabled": True,
                "created": datetime.now(DAY_TZ).isoformat(timespec="seconds"),
            }
            self._save()
            return secret

    def _guest(self, name: str, rec: Dict) -> Guest:
        if "week_limit" in rec:
            week = int(rec.get("week_limit") or 0)
            five = int(rec.get("five_hour_limit", five_hour_of(week)) or 0)
        else:                       # a record from before 30.10.2026: only a daily limit
            five = int(rec.get("daily_limit") or 0)
            week = 7 * five
        return Guest(
            name=name,
            five_hour_limit=five,
            week_limit=week,
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

    def set_limit(self, name: str, week_limit: int, five_hour_limit: Optional[int] = None) -> None:
        """Set the weekly limit; the 5 hours get a seventh of it unless given apart."""
        self._update(name, **self._limits(week_limit, five_hour_limit))

    def set_unlimited_until(self, name: str, until: float) -> None:
        """Lift both limits until `until` (unix time); they come back by themselves."""
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

    def spent_between(self, guest: str, start: float, end: float) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(weighted),0) FROM usage WHERE guest=? AND ts>=? AND ts<?",
                (guest, start, end),
            ).fetchone()
        return int(row[0])

    def five_hour_window(self, guest: str, now: Optional[float] = None
                         ) -> Optional[Tuple[float, float]]:
        """(start, end) of the guest's open 5-hour window, None when none is open.

        A window opens with a request when none is open and lasts 5 hours, like
        Anthropic's. Worked out from the requests alone: the chain of windows starts at the
        first request since the start of LAST week (a fixed point — counting from "5 hours
        ago" would let a guest who never pauses slide the window forever), each next one
        at the first request at or after the end of the one before."""
        now = time.time() if now is None else now
        t = week_bounds(week_bounds(now)[0] - 1)[0]
        with self._lock:
            while True:
                row = self._db.execute(
                    "SELECT MIN(ts) FROM usage WHERE guest=? AND ts>=? AND ts<=?",
                    (guest, t, now),
                ).fetchone()
                if row[0] is None:
                    return None
                start = float(row[0])
                if now < start + FIVE_HOURS:
                    return start, start + FIVE_HOURS
                t = start + FIVE_HOURS


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
        text = _t("spend_alert", guest=guest, hour_pct=spent / WEEK_PERCENT, hour_m=spent / 1e6,
                  today_pct=today / WEEK_PERCENT)
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


USAGE_PATH = "/guest/usage"


def week_bounds(now: float) -> Tuple[float, float]:
    """The calendar week around `now`: Monday 00:00 to the next Monday 00:00, DAY_TZ."""
    day = datetime.fromtimestamp(now, DAY_TZ).date()
    monday = day - timedelta(days=day.weekday())
    start = datetime.combine(monday, dtime(0), DAY_TZ)
    end = datetime.combine(monday + timedelta(days=7), dtime(0), DAY_TZ)
    return start.timestamp(), end.timestamp()


def usage_windows(guest, ledger, now: Optional[float] = None) -> Dict:
    """{"five_hour"|"seven_day": {"used", "limit", "resets_at"}} — weighted tokens in the
    open 5-hour window (none open: 0 and resets_at 0) and in this calendar week."""
    now = time.time() if now is None else now
    window = ledger.five_hour_window(guest.name, now)
    if window:
        five_used, five_reset = ledger.spent_between(guest.name, *window), math.ceil(window[1])
    else:
        five_used, five_reset = 0, 0
    week_start, week_end = week_bounds(now)
    return {
        "five_hour": {"used": int(five_used), "limit": int(guest.five_hour_limit),
                      "resets_at": int(five_reset)},
        "seven_day": {"used": int(ledger.spent_between(guest.name, week_start, week_end)),
                      "limit": int(guest.week_limit), "resets_at": math.ceil(week_end)},
    }


def _utilization(w: Dict) -> float:
    return w["used"] / w["limit"] if w["limit"] > 0 else 1.0


def guest_usage(guest, ledger, now: Optional[float] = None) -> Dict:
    """A guest's own numbers for its dashboard (GET /guest/usage, decision 30.10.2026):
    its 5-hour and weekly windows, like the subscription's."""
    now = time.time() if now is None else now
    out = {
        "guest": guest.name,
        "enabled": bool(guest.enabled),
        "unlimited_until": int(guest.unlimited_until) if guest.limit_lifted(now) else 0,
    }
    for key, w in usage_windows(guest, ledger, now).items():
        out[key] = dict(w, utilization=_utilization(w))
    return out


def over_limit(guest, ledger, now: Optional[float] = None) -> Optional[str]:
    """The refusal text when the guest has used up a limit, None to let it through.
    Over both: the week is said — it comes back later."""
    now = time.time() if now is None else now
    if guest.limit_lifted(now):
        return None
    w = usage_windows(guest, ledger, now)
    week, five = w["seven_day"], w["five_hour"]
    if week["used"] >= week["limit"]:
        return _t("gate_limit_week", spent=week["used"], limit=week["limit"])
    if five["used"] >= five["limit"]:
        at = five["resets_at"] or now + FIVE_HOURS
        return _t("gate_limit_5h", spent=five["used"], limit=five["limit"],
                  at=datetime.fromtimestamp(at, DAY_TZ).strftime("%H:%M"))
    return None


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
                return self._reply(401, _error("authentication_error", _t("gate_bad_pass")))
            if self.path.split("?", 1)[0] == USAGE_PATH:   # the guest's own numbers, from here
                if self.command != "GET":
                    return self._reply(403, _error("permission_error", _t("gate_path")))
                return self._reply(200, json.dumps(guest_usage(guest, ledger)).encode())
            if not guest.enabled:
                log(f"DENY disabled guest={guest.name}")
                return self._reply(403, _error("permission_error", _t("gate_disabled")))
            if not _allowed_path(self.path):
                log(f"DENY path guest={guest.name} {self.command} {self.path}")
                return self._reply(403, _error("permission_error", _t("gate_path")))
            refusal = over_limit(guest, ledger)
            if refusal:
                log(f"LIMIT guest={guest.name} {refusal}")
                return self._reply(429, _error("rate_limit_error", refusal))

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
                return self._reply(502, _error("api_error", _t("gate_upstream")))

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
            state = _t("ts_on") if g.enabled else _t("ts_off")
            w = usage_windows(g, ledger)
            five, week = w["five_hour"], w["seven_day"]
            until = datetime.fromtimestamp(g.unlimited_until, DAY_TZ).strftime('%d.%m %H:%M')
            lifted = _t("ts_unlimited_until", until=until) if g.limit_lifted() else ""
            print(_t("ts_list_line", name=name, state=state,
                     five_used=five["used"], five_limit=five["limit"],
                     week_used=week["used"], week_limit=week["limit"],
                     week_pct=week["used"] / WEEK_PERCENT, lifted=lifted,
                     tg=g.telegram_id or "-"))
    elif args.cmd in ("enable", "disable"):
        book.set_enabled(args.name, args.cmd == "enable")
    elif args.cmd == "remove":
        book.remove(args.name)
    elif args.cmd == "limit":
        book.set_limit(args.name, args.value)
    elif args.cmd == "unlimited":
        until = time.time() + args.hours * 3600
        book.set_unlimited_until(args.name, until)
        print(_t("ts_unlimited_set", name=args.name,
                 until=datetime.fromtimestamp(until, DAY_TZ).strftime('%d.%m %H:%M')))
    return 0


if __name__ == "__main__":
    sys.exit(main())
