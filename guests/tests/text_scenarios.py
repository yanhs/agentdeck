"""Every text the guests module shows a guest or the owner, produced through the real code
paths. golden_ru.json holds what these produced BEFORE the texts moved into texts.py
(GUESTS_LANG=ru must reproduce it byte for byte); test_texts.py also runs them in English.
Not a test module itself (no test_ prefix)."""

import json
import sys
import tempfile
from pathlib import Path

GUESTS = Path(__file__).resolve().parents[1]
for sub in ("", "turnstile", "tgrelay", "sitegate"):
    p = str(GUESTS / sub) if sub else str(GUESTS)
    if p not in sys.path:
        sys.path.insert(0, p)

import guestctl as gc  # noqa: E402
import sitegate as sg  # noqa: E402
import texts  # noqa: E402
import tgrelay as tr  # noqa: E402
import turnstile as ts  # noqa: E402

OWNER, MASHA = 4242, 5550001
NOW = 1_800_000_000.0
SERVICES = {
    "web": {"state": "running", "since": NOW - 3 * 3600 - 120, "restarts": 2},
    "bot": {"state": "running"},
    "crash": {"state": "waiting", "last_exit": 3, "restarts": 5},
    "boot": {"state": "waiting"},
    "many": {"state": "skipped", "limit": 10},
    "many2": {"state": "skipped"},
    "off": {"state": "stopped"},
}


class Recorder:
    def __init__(self):
        self.calls = []

    def call(self, method, params, files=None):
        self.calls.append([method, {k: v for k, v in params.items()}])
        return {"ok": True, "result": {"message_id": 77}}


class Prov:
    def __init__(self, ok):
        self.ok = ok

    def provision(self, name, tid):
        return (True, "") if self.ok else (False, "docker: no space left")


class Keeper:
    def status(self, guest):
        return {"web": {"state": "running"}}

    def act(self, guest, action, svc):
        return svc != "broken"


def _invitation_flow(tmp: Path, ok: bool) -> list:
    book = ts.GuestBook(tmp / "guests.json")
    book.add("anna", daily_limit=10, telegram_id=1001)
    tg = Recorder()
    inv = tr.Invitations(tmp / "requests.json", owner_tid=OWNER, provisioner=Prov(ok),
                         taken=lambda: set(book.names()), background=False)
    info = {"masha": {"url": "https://sb-masha.example.test", "password": "pw-M"}}
    relay = tr.Relay(book, tg, invites=inv, info_for=info.get)
    who = {"id": MASHA, "first_name": "Маша", "username": "masha"}
    relay.route({"update_id": 1, "message": {"message_id": 1, "chat": {"id": MASHA},
                 "from": who, "text": "/start"}})
    relay.route({"update_id": 2, "message": {"message_id": 2, "chat": {"id": MASHA},
                 "from": who, "text": "ну что?"}})
    press = {"id": "c", "from": {"id": MASHA}, "data": f"inv:ok:{MASHA}",
             "message": {"message_id": 77, "chat": {"id": OWNER}}}
    relay.route({"update_id": 3, "callback_query": press})                 # not the owner
    relay.route({"update_id": 4, "callback_query": {**press, "from": {"id": OWNER}}})
    relay.route({"update_id": 5, "callback_query": {**press, "from": {"id": OWNER}}})
    return tg.calls


def _rejection_flow(tmp: Path) -> list:
    book = ts.GuestBook(tmp / "guests.json")
    tg = Recorder()
    inv = tr.Invitations(tmp / "requests.json", owner_tid=OWNER, provisioner=Prov(True),
                         taken=lambda: set(), background=False)
    relay = tr.Relay(book, tg, invites=inv)
    relay.route({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 7},
                 "from": {"id": 7}, "text": "hi"}})
    relay.route({"update_id": 2, "callback_query": {
        "id": "c", "from": {"id": OWNER}, "data": "inv:no:7",
        "message": {"message_id": 77, "chat": {"id": OWNER}}}})
    return tg.calls


def _services_flow(tmp: Path) -> list:
    book = ts.GuestBook(tmp / "guests.json")
    book.add("anna", daily_limit=10, telegram_id=1001)
    tg = Recorder()
    relay = tr.Relay(book, tg, services=Keeper(), background=False, settle=0)
    relay.route({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 1001},
                 "from": {"id": 1001}, "text": "/services"}})
    for data in ("svc:restart:web", "svc:stop:web", "svc:start:web", "svc:restart:broken",
                 "svc:frob:web", "svc:refresh:"):
        relay.route({"update_id": 2, "callback_query": {
            "id": "c", "from": {"id": 1001}, "data": data,
            "message": {"message_id": 5, "chat": {"id": 1001}}}})
    relay.route({"update_id": 3, "message": {"message_id": 1, "chat": {"id": 99},
                 "from": {"id": 99}, "text": "hi"}})                     # a stranger
    relay.call("anna", "setMyCommands", {"commands": json.dumps([{"command": "list",
                                                                  "description": "x"}])})
    return tg.calls


class _Ledger:
    def spent_since(self, guest, since):
        return 12_345_678

    def spent_today(self, guest):
        return 30_000_000


class _Ops:
    def __init__(self):
        self.notes = []

    def __getattr__(self, name):
        if name == "notify":
            return self.notes.append
        if name == "cert_issue":
            def fail(host):
                raise RuntimeError("certbot failed")
            return fail
        return lambda *a: None


def _gate_flow(tmp: Path) -> dict:
    out = {}
    for label, call in (("bad_site", lambda: sg.validate_site("A B")),
                        ("bad_port", lambda: sg.validate_port("x")),
                        ("low_port", lambda: sg.validate_port(80)),
                        ("long_host", lambda: sg.host_for("a" * 20, "b" * 31 + "c" * 20))):
        try:
            call()
        except ValueError as exc:
            out[label] = str(exc)
    book = ts.GuestBook(tmp / "guests.json")
    book.add("anna", 10, 1)
    book.add("off", 10, 2)
    book.set_enabled("off", False)
    book.add("noip", 10, 4)
    ops = _Ops()
    gate = sg.Gate(book, ops, tmp / "sites.json", {"anna": "172.31.250.11"}.get, cleanup_delay=0)
    for label, call in (("disabled", lambda: gate.publish("off", "s", 3000)),
                        ("no_ip", lambda: gate.publish("noip", "s", 3000)),
                        ("failed", lambda: gate.publish("anna", "s", 3000)),
                        ("not_yours", lambda: gate.unpublish("anna", "zz"))):
        try:
            call()
        except sg.GateError as exc:
            out[label] = str(exc)
    reg = {f"s{i}-anna.example.test": {"guest": "anna", "site": f"s{i}", "port": 3000,
                                       "created": "x"} for i in range(3)}
    reg["t-boris.example.test"] = {"guest": "boris", "site": "t", "port": 1, "created": "x"}
    (tmp / "sites.json").write_text(json.dumps(reg))
    for label, call in (("too_many", lambda: gate.publish("anna", "s9", 3000)),):
        try:
            call()
        except sg.GateError as exc:
            out[label] = str(exc)
    book.add("boris", 10, 3)
    gate.ip_of = {"anna": "1", "boris": "2"}.get
    reg2 = {"t-anna.example.test": {"guest": "boris", "site": "t", "port": 1, "created": "x"}}
    (tmp / "sites.json").write_text(json.dumps(reg2))
    try:
        gate.publish("anna", "t", 3000)
    except sg.GateError as exc:
        out["taken"] = str(exc)
    reg3 = {"s0-anna.example.test": {"guest": "anna", "site": "s0", "port": 3000, "created": "x"}}
    (tmp / "sites.json").write_text(json.dumps(reg3))
    gate.unpublish("anna", "s0")
    gate.ops = _OkOps(ops.notes)
    gate.publish("anna", "new", 4000)
    out["notes"] = ops.notes
    return out


class _OkOps(_Ops):
    def __init__(self, notes):
        self.notes = notes

    def __getattr__(self, name):
        if name == "notify":
            return self.notes.append
        return lambda *a: None


def scenarios() -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="guest-texts-"))
    for d in "abcde":
        (tmp / d).mkdir()
    out = {
        "stranger": texts.t("stranger"),
        "invite": [texts.t(k) for k in ("invite_sent", "invite_waiting", "invite_refused",
                                        "invite_failed")],
        "commands": [tr.web_command(), tr.services_command()],
        "ago": [tr._ago(s) for s in (30, 600, 3700, 7200, 200_000)],
        "services_none": tr.services_view(None, NOW),
        "services_empty": tr.services_view({}, NOW),
        "services_full": tr.services_view(SERVICES, NOW),
        "web_none": tr.web_text(None, welcome=True),
        "web_welcome": tr.web_text({"url": "https://sb-anna.example.test", "password": "pw-1"},
                                   welcome=True, domain="example.test"),
        "web_again": tr.web_text({"url": "https://sb-anna.example.test", "password": "pw-1"},
                                 welcome=False),
        "web_no_password": tr.web_text({"url": "https://sb-anna.example.test"}, welcome=False),
        "buttons": tr.Invitations._buttons("5"),
        "invite_ok": _invitation_flow(tmp / "a", ok=True),
        "invite_fail": _invitation_flow(tmp / "b", ok=False),
        "invite_no": _rejection_flow(tmp / "c"),
        "services_flow": _services_flow(tmp / "d"),
        "gate": _gate_flow(tmp / "e"),
    }
    told = []
    ts.SpendWatch(_Ledger(), notify=told.append, background=False, clock=lambda: NOW,
                  log=lambda line: None).check("anna")
    out["spend_alert"] = told
    out["seed_blocks"] = ["\n" + mark + "\n" + text for mark, text in
                          gc.seed_rules("https://sbx.example.test/f/anna-k3y", "example.test", 10)]
    return out


if __name__ == "__main__":
    print(json.dumps(scenarios(), ensure_ascii=False, indent=1))
