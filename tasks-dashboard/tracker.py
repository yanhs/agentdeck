#!/usr/bin/env python3
"""CLI to mutate the shared task tracker (state.json) with atomic writes, so the SSE
server never reads a half-written file. Any Claude agent can call this to move the
shared dashboard.

  tracker.py add-task <id> --title "..." [--agent name] [--status active]
  tracker.py add-item <task_id> "item title"
  tracker.py set <task_id> <item_index> <todo|active|done|blocked> [--note "..."]
  tracker.py set-task <task_id> <active|done|paused>
  tracker.py rm-task <task_id>
  tracker.py seed <file.json>     # replace the whole state from a JSON file
  tracker.py show
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(os.getenv("TRACKER_STATE", str(Path(__file__).resolve().parent / "state.json")))
ITEM_STATUSES = ["todo", "active", "done", "blocked"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load():
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {"title": "Claude Task Tracker", "tasks": [], "updated": now()}


def save(s):
    s["updated"] = now()
    fd, tmp = tempfile.mkstemp(dir=str(STATE.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)


def find(s, tid):
    return next((t for t in s["tasks"] if t["id"] == tid), None)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-task")
    p.add_argument("id")
    p.add_argument("--title", default=None)
    p.add_argument("--agent", default=os.getenv("TRACKER_AGENT", "claude"))
    p.add_argument("--status", default="active")

    p = sub.add_parser("add-item")
    p.add_argument("task_id")
    p.add_argument("title")
    p.add_argument("--status", default="todo", choices=ITEM_STATUSES)

    p = sub.add_parser("set")
    p.add_argument("task_id")
    p.add_argument("index", type=int)
    p.add_argument("status", choices=ITEM_STATUSES)
    p.add_argument("--note", default=None)

    p = sub.add_parser("set-task")
    p.add_argument("task_id")
    p.add_argument("status")

    p = sub.add_parser("state")  # agent activity: am I working, stopped, or blocked on this task
    p.add_argument("task_id")
    p.add_argument("activity", choices=["working", "stopped", "blocked"])
    p.add_argument("--note", default=None)

    p = sub.add_parser("rm-task")
    p.add_argument("task_id")

    p = sub.add_parser("seed")
    p.add_argument("file")

    sub.add_parser("show")

    a = ap.parse_args()
    s = load()

    if a.cmd == "add-task":
        t = find(s, a.id)
        if not t:
            t = {"id": a.id, "items": [], "created": now()}
            s["tasks"].append(t)
        t["title"] = a.title or t.get("title") or a.id
        t["agent"] = a.agent
        t["status"] = a.status
        t["updated"] = now()

    elif a.cmd == "add-item":
        t = find(s, a.task_id) or sys.exit(f"no task {a.task_id}")
        t["items"].append({"title": a.title, "status": a.status, "note": "", "updated": now()})
        t["updated"] = now()
        print(f"item index {len(t['items']) - 1}")

    elif a.cmd == "set":
        t = find(s, a.task_id) or sys.exit(f"no task {a.task_id}")
        if not (0 <= a.index < len(t["items"])):
            sys.exit(f"bad index {a.index} (0..{len(t['items']) - 1})")
        it = t["items"][a.index]
        it["status"] = a.status
        if a.note is not None:
            it["note"] = a.note
        it["updated"] = now()
        t["updated"] = now()

    elif a.cmd == "set-task":
        t = find(s, a.task_id) or sys.exit(f"no task {a.task_id}")
        t["status"] = a.status
        t["updated"] = now()

    elif a.cmd == "state":
        t = find(s, a.task_id) or sys.exit(f"no task {a.task_id}")
        t["activity"] = a.activity
        if a.note is not None:
            t["activity_note"] = a.note
        t["updated"] = now()

    elif a.cmd == "rm-task":
        s["tasks"] = [t for t in s["tasks"] if t["id"] != a.task_id]

    elif a.cmd == "seed":
        s = json.loads(Path(a.file).read_text())

    elif a.cmd == "show":
        print(json.dumps(s, indent=2, ensure_ascii=False))
        return

    save(s)


if __name__ == "__main__":
    main()
