#!/usr/bin/env python3
"""Drives the "one number per terminal" scenario against an installed AgentDeck in a
test container (tests/install/convo_e2e.sh boots it). Stdlib only.

    convo_e2e.py BASE_URL --container NAME --password PW [--label TEXT]

From the host: the dashboard over BASE_URL (login, /api/library, /sess/ websocket,
/tasks/state.json); inside the container, as user ubuntu: its OWN tmux server
(TMUX_TMPDIR=~/agentdeck/.sessions/tmux — never the host's), /proc and ~/.claude.
Keys go to the container's tmux with send-keys; the screen is read with capture-pane.

Every check prints PASS/FAIL with the evidence; the last lines are a table row per
step (what the dashboard / tmux / task board / claude command line show). Exit 0 =
all passed.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoke import NoRedirect, ttyd_attach  # noqa: E402

TMUX_TMPDIR = "/home/ubuntu/agentdeck/.sessions/tmux"
TRACKER = "/home/ubuntu/agentdeck/tasks-dashboard/tracker.py"

FAILED: list[str] = []
TABLE: list[tuple[str, str]] = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILED.append(name)
    return ok


def note(msg):
    print(f"      {msg}", flush=True)


class Box:
    """The container: commands as user ubuntu, its own tmux server."""

    def __init__(self, name):
        self.name = name

    def sh(self, script, check_rc=False, timeout=60):
        r = subprocess.run(["docker", "exec", "-u", "ubuntu", "-w", "/home/ubuntu",
                            "-e", "HOME=/home/ubuntu", "-e", "LC_ALL=C.UTF-8",
                            self.name, "bash", "-c", script],
                           capture_output=True, text=True, timeout=timeout)
        if check_rc and r.returncode != 0:
            raise RuntimeError(f"{script[:80]!r}: rc {r.returncode}: {r.stderr[-300:]}")
        return r

    def tmux(self, *args):
        # a UTF-8 locale, as AgentDeck's services have: without one tmux 3.4 prints a
        # tab in -F output as "_"
        return subprocess.run(["docker", "exec", "-u", "ubuntu", "-e", f"TMUX_TMPDIR={TMUX_TMPDIR}",
                               "-e", "LC_ALL=C.UTF-8", self.name, "tmux", *args],
                              capture_output=True, text=True, timeout=30)

    def sessions(self):
        r = self.tmux("list-sessions", "-F", "#{session_name}")
        return sorted(r.stdout.split()) if r.returncode == 0 else []

    def screen(self, sess):
        r = self.tmux("capture-pane", "-p", "-t", f"={sess}:")
        return r.stdout if r.returncode == 0 else ""

    def keys(self, sess, *keys):
        return self.tmux("send-keys", "-t", f"={sess}:", *keys)

    def type_line(self, sess, text):
        """Type text literally, then Enter (separately: a paste of text+Enter can
        land as one bracketed paste that Claude doesn't submit)."""
        self.tmux("send-keys", "-t", f"={sess}:", "-l", text)
        time.sleep(1.0)
        return self.tmux("send-keys", "-t", f"={sess}:", "Enter")

    def pane_pid(self, sess):
        r = self.tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}")
        for line in r.stdout.splitlines():
            n, _, p = line.partition("\t")
            if n == sess and p.isdigit():
                return int(p)
        return None

    def claudes(self):
        """[(pid, argv)] of every claude process (argv[0] basename claude/claude.exe)."""
        r = self.sh(r'''for p in /proc/[0-9]*; do
  a=$(tr '\0' '\037' < $p/cmdline 2>/dev/null) || continue
  b=${a%%$'\037'*}; b=${b##*/}
  case "$b" in claude|claude.exe) printf '%s\t%s\n' "${p#/proc/}" "$a";; esac
done''')
        out = []
        for line in r.stdout.splitlines():
            pid, _, a = line.partition("\t")
            out.append((int(pid), [x for x in a.split("\x1f") if x]))
        return sorted(out)

    def ppids(self):
        """{pid: ppid} of every process (the command name may hold spaces: the
        fields are read after its closing parenthesis)."""
        r = self.sh('for p in /proc/[0-9]*; do s=$(cat "$p/stat" 2>/dev/null) || continue; '
                    's=${s##*) }; set -- $s; echo "${p#/proc/} $2"; done')
        out = {}
        for line in r.stdout.splitlines():
            a, _, b = line.partition(" ")
            if a.isdigit() and b.isdigit():
                out[int(a)] = int(b)
        return out

    def pidfiles(self):
        """{pid: json} of ~/.claude/sessions/<pid>.json."""
        r = self.sh('cd ~/.claude/sessions 2>/dev/null && for f in [0-9]*.json; do '
                    '[ -f "$f" ] && { printf "%s\\t" "$f"; tr -d "\\n" < "$f"; echo; }; done')
        out = {}
        for line in r.stdout.splitlines():
            f, _, j = line.partition("\t")
            try:
                out[int(f.split(".")[0])] = json.loads(j)
            except ValueError:
                pass
        return out


class Deck:
    """The dashboard, from outside, like a browser."""

    def __init__(self, base, insecure=True):
        self.base = base.rstrip("/")
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                              NoRedirect,
                                              urllib.request.HTTPSHandler(context=self.ctx))

    def req(self, method, path, data=None, headers=None):
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers=headers or {})
        try:
            with self.op.open(r, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def wait_up(self, seconds=90):
        end = time.time() + seconds
        while time.time() < end:
            try:
                code, body = self.req("GET", "/login")
                if code == 200:
                    return body
            except OSError:
                pass
            time.sleep(2)
        return None

    def login(self, password, first_run):
        form = ({"pass": password, "pass2": password} if first_run
                else {"user": "admin", "pass": password})
        self.req("POST", "/login", urllib.parse.urlencode(form).encode(),
                 {"Content-Type": "application/x-www-form-urlencoded"})
        return len(self.jar) > 0

    def post(self, route, body):
        code, raw = self.req("POST", "/api/library/" + route, json.dumps(body).encode(),
                             {"Content-Type": "application/json", "Origin": self.base})
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, {"raw": raw[:200].decode("utf-8", "replace")}

    def library(self, archived=True):
        code, raw = self.req("GET", "/api/library" + ("?archived=1" if archived else ""))
        if code != 200:
            return []
        return json.loads(raw).get("sessions") or []

    def ids(self):
        return [r["id"] for r in self.library()]

    def open(self, sid, seconds=12, stay=False):
        """What a click on the terminal does: /sess/?arg=<id> over the websocket
        (open-session.sh -> library_cli ensure -> tmux attach). Returns the output.
        stay=True: the tab stays open for all `seconds`."""
        self.req("GET", f"/sess/?arg={sid}")
        return ttyd_attach(self.base, self.jar, sid, self.ctx, seconds=seconds, stay=stay)

    def tasks(self):
        code, raw = self.req("GET", "/tasks/state.json")
        try:
            return json.loads(raw).get("tasks") or [] if code == 200 else []
        except ValueError:
            return []


def wait(pred, seconds, step=1.0):
    end = time.time() + seconds
    while True:
        v = pred()
        if v or time.time() >= end:
            return v
        time.sleep(step)


# ── the scenario ─────────────────────────────────────────────────────────────
NAME = "e2e"                           # the terminal's name on the dashboard
FAKE_PORT = 18080
REPL = "bypass permissions on"         # Claude's footer once the prompt is up


def lines_of(screen):
    return [l for l in screen.splitlines() if l.strip()]


def show(screen, n=12):
    for l in lines_of(screen)[-n:]:
        note("│ " + l.rstrip()[:118])


def choose(box, sess, target):
    """Move a dialog's ❯ cursor to the option whose text is `target`, then Enter."""
    ls = box.screen(sess).splitlines()

    def text(l):
        return re.sub(r"^[\s❯]*(\d+\.\s*)?", "", l).replace("✔", "").strip()
    want = [i for i, l in enumerate(ls) if text(l) == target]
    cur = [i for i, l in enumerate(ls) if l.lstrip().startswith("❯")]
    if not want or not cur:
        return False
    at = min(cur, key=lambda i: abs(i - want[0]))   # the dialog's cursor, not the prompt's
    d = want[0] - at
    for _ in range(abs(d)):
        box.keys(sess, "Down" if d > 0 else "Up")
        time.sleep(0.3)
    time.sleep(0.5)
    box.keys(sess, "Enter")
    return True


def onboarding(box, sess, stop, seconds=90):
    """Answer Claude's first-run screens (theme, the API key, security notes, folder
    trust) until stop(screen) is true. Returns (screen, [screens answered])."""
    seen = []
    end = time.time() + seconds
    while time.time() < end:
        s = box.screen(sess)
        if stop(s):
            return s, seen
        if "Choose the text style" in s:
            seen.append("theme")
            box.keys(sess, "Enter")
        elif "Detected a custom API key" in s:
            seen.append("api-key: Yes")
            choose(box, sess, "Yes")
        elif "Security notes" in s:
            seen.append("security notes")
            box.keys(sess, "Enter")
        elif "Yes, I trust this folder" in s:
            seen.append("trust ~/projects: Yes")
            choose(box, sess, "Yes, I trust this folder")
        elif "Press Enter to continue" in s:
            seen.append("enter")
            box.keys(sess, "Enter")
        time.sleep(2.5)
    return box.screen(sess), seen


def claude_in(box, sess):
    """(pid, argv) of the Claude running in tmux session `sess`: the pane's process
    or a descendant of it."""
    pane = box.pane_pid(sess)
    if pane is None:
        return None, []
    parent = box.ppids()
    for pid, argv in box.claudes():
        cur = pid
        for _ in range(8):
            if cur == pane:
                return pid, argv
            cur = parent.get(cur, 0)
            if cur <= 1:
                break
    return None, []


def live_uuid(box, pid):
    d = box.pidfiles().get(pid) if pid else None
    return d.get("sessionId") if d else None


def one_var(box, pid, var):
    """One named variable of a process's environment (never the whole environment)."""
    r = box.sh(f"tr '\\0' '\\n' < /proc/{int(pid)}/environ | grep -m1 '^{var}=' | cut -d= -f2-")
    return r.stdout.strip()


def terminal(deck):
    """The row the user works in: named NAME (an earlier conversation of it is
    "NAME (earlier)")."""
    return next((r for r in deck.library() if r.get("name") == NAME), None)


def term_id(deck, default=""):
    return (terminal(deck) or {}).get("id") or default


def run(deck, box, password, label):
    import threading
    print(f"=== one number per terminal — {label}", flush=True)
    note("claude: " + box.sh("claude --version").stdout.strip())
    if deck.wait_up() is None:
        check("dashboard answers", False)
        return 1
    check("first run: password set, logged in", deck.login(password, first_run=True))

    # 0. a new terminal on a server where nobody has signed in to Claude yet
    code, row = deck.post("new", {"name": NAME})
    A = row.get("id", "")
    check("new terminal", code == 200 and len(A) == 8, f"A = {A}")
    deck.open(A, seconds=8)
    sa = f"cs-{A}"
    scr, seen = onboarding(box, sa, lambda s: "Select login method" in s or "Bypass Permissions" in s,
                           seconds=60)
    login_first = "Select login method" in scr and "Bypass Permissions" not in scr
    check("before sign-in Claude shows its login, not the bypass consent", login_first,
          f"screens: {seen}")
    show(scr, 5)
    TABLE.append(("bypass consent before sign-in?", "no: the login screen comes first"
                  if login_first else "see log"))

    # 1. "sign in": a made-up API key + a stand-in API inside the container, so the
    #    real binary gets past login with no account and no network
    box.sh(f"nohup python3 /opt/fake_anthropic_api.py {FAKE_PORT} {TRACKER} "
           ">/tmp/fake-api.log 2>&1 &")
    box.sh("umask 077; { printf 'export ANTHROPIC_API_KEY=sk-ant-api03-%s\\n' "
           "\"$(head -c 60 /dev/urandom | base64 | tr -dc A-Za-z0-9 | head -c 80)AA\"; "
           f"echo 'export ANTHROPIC_BASE_URL=http://127.0.0.1:{FAKE_PORT}'; "
           # Claude's own "Try the new fullscreen renderer?" offer, now rather than
           # whenever Claude would pick: its "Yes, try it" relaunches Claude
           "echo 'export CLAUDE_CODE_FORCE_FULLSCREEN_UPSELL=1'; } > ~/.claude/oauth.env")
    deck.post("close", {"id": A, "force": True})
    wait(lambda: not box.claudes(), 20)
    deck.open(A, seconds=8)
    pid, argv = claude_in(box, sa)
    check(f"terminal {A} starts claude --session-id <A>",
          "--session-id" in argv and any(x.startswith(A) for x in argv), " ".join(argv[1:]))
    scr, seen = onboarding(box, sa, lambda s: "Bypass Permissions mode" in s, seconds=90)
    check("the real Claude shows its bypass-permissions consent", "Bypass Permissions mode" in scr,
          f"screens before it: {seen}")
    show(scr, 11)
    choose(box, sa, "Yes, I accept")
    time.sleep(6)
    pid2, argv2 = claude_in(box, sa)
    u2 = live_uuid(box, pid2)
    on_consent = bool(u2) and not u2.startswith(A)
    note(f"after 'Yes, I accept': pid {pid2} {' '.join(argv2[1:])}; conversation {u2}")
    TABLE.append(("'Yes, I accept' (consent) relaunches Claude?",
                  f"{'yes' if on_consent else 'no'}: conversation {(u2 or '?')[:8]}"))

    # 2. Claude relaunches itself without the --session-id AgentDeck gave it: a new
    #    conversation B in terminal A. A "browser tab" stays attached meanwhile.
    tab = threading.Thread(target=deck.open, args=(A,), kwargs={"seconds": 150, "stay": True},
                           daemon=True)                  # ends by itself (or at the unload)
    tab.start()
    time.sleep(4)
    clients0 = box.tmux("list-clients", "-F", "#{client_session}").stdout.split()
    note(f"a browser tab is attached to {clients0}")
    how = "the consent"
    if not on_consent:
        scr = wait(lambda: (lambda s: s if ("Try the new fullscreen renderer" in s or REPL in s)
                            else None)(box.screen(sa)), 30) or ""
        if "Try the new fullscreen renderer" in scr:
            show(scr, 8)
            choose(box, sa, "Yes, try it")
            how = "Claude's own 'Try the new fullscreen renderer?' -> 'Yes, try it'"
        else:
            box.type_line(sa, "/tui fullscreen")
            how = "/tui fullscreen"
    box.sh("sed -i /FORCE_FULLSCREEN/d ~/.claude/oauth.env")

    def relaunched():
        u = live_uuid(box, pid2 or claude_in(box, sa)[0])
        return u if u and not u.startswith(A) else None
    B_uuid = wait(relaunched, 30) or ""
    B = B_uuid[:8]
    pidr, argvr = next(iter(box.claudes()), (None, []))
    check(f"Claude relaunched itself ({how}) into a new conversation", bool(B_uuid),
          f"A = {A}, now B = {B_uuid}")
    check("its command line lost --session-id (as on the owner's demo)",
          "--session-id" not in argvr and "--permission-mode" in argvr, " ".join(argvr))
    TABLE.append(("relaunch trigger", how))
    TABLE.append(("Claude after the relaunch (cmdline)",
                  " ".join([os.path.basename(argvr[0]) if argvr else "?"] + argvr[1:])))
    TABLE.append(("conversation live in the terminal", f"{B} (the terminal was opened as {A})"))

    wait(lambda: term_id(deck) == B, 20)
    shown, ids = term_id(deck), deck.ids()
    check("the dashboard shows the terminal as B", shown == B and A not in ids,
          f"row {NAME!r} = {shown}; all ids {ids}")
    TABLE.append(("number on the dashboard", shown))
    sess = box.sessions()
    check("tmux session is cs-B (cs-A gone)", f"cs-{B}" in sess and sa not in sess, f"{sess}")
    TABLE.append(("tmux session", ", ".join(sess)))
    clients = box.tmux("list-clients", "-F", "#{client_session}").stdout.split()
    check("the attached browser tab stayed attached, now on cs-B", f"cs-{B}" in clients,
          f"clients on {clients}")
    TABLE.append(("the tab attached before the relaunch is now on",
                  f"{', '.join(clients) or '-'} (was {', '.join(clients0) or '-'})"))

    # the old link /sess/?arg=A: one more tab, on the terminal the link names
    from collections import Counter
    before = len(box.claudes())
    tab2 = threading.Thread(target=deck.open, args=(A,), kwargs={"seconds": 10, "stay": True},
                            daemon=True)
    tab2.start()
    time.sleep(6)
    now_clients = box.tmux("list-clients", "-F", "#{client_session}").stdout.split()
    tab2.join(15)
    new = list((Counter(now_clients) - Counter(clients)).elements())
    check("old link /sess/?arg=A opens B (no second Claude)",
          new == [f"cs-{B}"] and len(box.claudes()) == before,
          f"the new tab is on {new}; claude processes {before} -> {len(box.claudes())}")
    TABLE.append(("old link /sess/?arg=A lands on", ", ".join(new) or "-"))

    # 3. a task added from inside the pane, by Claude's own Bash tool
    csess = f"cs-{term_id(deck, A)}"
    tid = f"e2e-{int(time.time()) % 100000}"
    box.type_line(csess, f"please add the board task: E2E-TASK {tid}")
    task = wait(lambda: next((t for t in deck.tasks() if t["id"] == tid), None), 40)
    stamp = (task or {}).get("session")
    check("a task added in the pane carries the terminal's number",
          task is not None and stamp == term_id(deck),
          f"task {tid}: session {stamp}; dashboard {term_id(deck)}")
    TABLE.append(("task board: task added in the pane", f"session {stamp}"))
    p, _ = claude_in(box, csess)
    if p:
        note(f"claude's own AGENTDECK_SESSION={one_var(box, p, 'AGENTDECK_SESSION')} (exported "
             "once at launch: stale after the switch; the tracker stamps CLAUDE_CODE_SESSION_ID, "
             "which Claude sets for its shells)")

    # 4. unload + reopen resumes the conversation
    cur = term_id(deck, A)
    deck.post("close", {"id": cur, "force": True})
    wait(lambda: not box.claudes(), 20)
    out = deck.open(cur, seconds=10)
    csess = f"cs-{term_id(deck, cur)}"
    p, argv = claude_in(box, csess)
    time.sleep(3)
    scr = box.screen(csess)
    check("unload + reopen: claude --resume <B uuid>",
          "--resume" in argv and bool(B_uuid) and B_uuid in argv,
          " ".join(argv[1:]) or out[-200:].decode("utf-8", "replace"))
    check("the reopened terminal shows the conversation (the task prompt)",
          f"E2E-TASK {tid}" in scr)
    TABLE.append(("unload + reopen: claude runs", " ".join(argv[1:]) or "-"))
    TABLE.append(("reopened screen shows the earlier prompt", "yes" if f"E2E-TASK {tid}" in scr
                  else "no (an empty conversation)"))

    # 5. /clear: a new conversation C in the same terminal; B stays as its own terminal
    before_uuid = live_uuid(box, p)
    box.type_line(csess, "/clear")
    C_uuid = wait(lambda: (lambda u: u if u and u != before_uuid else None)(live_uuid(box, p)),
                  20) or ""
    C = C_uuid[:8]
    wait(lambda: term_id(deck) == C, 20)
    rows = {r["id"]: r for r in deck.library()}
    now = term_id(deck)
    check("/clear: the terminal takes the new number C", bool(C) and now == C,
          f"conversation {C}; dashboard {now}")
    earlier = rows.get(B) or {}
    check("B stays in the list as its own terminal",
          earlier.get("name") == f"{NAME} (earlier)", f"{B}: {earlier.get('name')!r}")
    sess = box.sessions()
    check("tmux: cs-C", bool(C) and f"cs-{C}" in sess, f"{sess}")
    TABLE.append(("after /clear: conversation", f"{C} (was {before_uuid and before_uuid[:8]})"))
    TABLE.append(("after /clear: dashboard", "; ".join(f"{r['id']} {r['name']!r} {r['status']}"
                                                      for r in rows.values())))
    TABLE.append(("after /clear: tmux", ", ".join(sess)))

    # the earlier conversation opens on its own, next to C
    if earlier:
        deck.open(B, seconds=8)
        _, argvb = claude_in(box, f"cs-{B}")
        pc, _ = claude_in(box, f"cs-{C}")
        check("B opens as its own terminal (claude --resume <B uuid>); C still runs C",
              B_uuid in argvb and live_uuid(box, pc) == C_uuid,
              f"cs-{B}: {' '.join(argvb[1:])}; cs-{C}: {live_uuid(box, pc)}")
        TABLE.append(("open the earlier one", f"cs-{B}: {' '.join(argvb[1:])}; cs-{C} runs {C}"))

        # 6. /resume B inside cs-C while B is open in cs-B: two terminals must not end
        #    up as one number — nothing merged, renamed or killed
        box.type_line(f"cs-{C}", f"/resume {B_uuid}")
        time.sleep(8)
        deck.library()                                   # the dashboard's poll syncs
        time.sleep(2)
        rows = {r["id"]: r for r in deck.library()}
        sess = box.sessions()
        runs = {s: live_uuid(box, claude_in(box, s)[0]) for s in sess}
        logged = box.sh("sudo journalctl -u agentdeck-status --no-pager 2>/dev/null "
                        f"| grep -m1 'conversation {B} is open in'").stdout.strip()
        check("/resume of a conversation open elsewhere: both terminals kept, nothing merged",
              f"cs-{B}" in sess and f"cs-{C}" in sess and B in rows and C in rows,
              f"tmux {sess}; runs {({k: (v or '')[:8] for k, v in runs.items()})}; "
              f"rows {sorted(rows)}")
        note(f"log: {logged[-200:] or '(no line)'}")
        TABLE.append(("/resume B in cs-C while B is open in cs-B",
                      f"tmux {', '.join(sess)}; rows {', '.join(sorted(rows))}; "
                      f"log: {'yes' if logged else 'no'}"))

    print("\n=== table: " + label)
    for k, v in TABLE:
        print(f"| {k} | {v} |")
    print(f"RESULT {label}: {'PASS' if not FAILED else 'FAIL (' + str(len(FAILED)) + ')'}")
    return 0 if not FAILED else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--container", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    raise SystemExit(run(Deck(a.base), Box(a.container), a.password, a.label))


if __name__ == "__main__":
    main()


