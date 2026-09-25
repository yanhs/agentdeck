#!/usr/bin/env python3
"""Move the numbered slots (.sessions/agent-N.id) into the session library.

    migrate_library.py [--dry-run]            table: slot -> id -> transcript -> name -> action
    migrate_library.py --apply                do it (idempotent: a second run changes nothing)
    migrate_library.py --apply --swap-dashboard
                                              also web/index-lib.html -> web/index.html
                                              (the old page kept as web/index-legacy.html)
    migrate_library.py --apply --rollback-dashboard
                                              only put web/index-legacy.html back as index.html
    --repo DIR   the terminal repo to work on (default: this script's directory; tests
                 pass a temp copy). Registry: library.LIB_FILE (env AGENTDECK_LIBRARY).

What --apply does:
 1. Registry: one entry per slot whose transcript
    ~/.claude/projects/<slug(cwd)>/<uuid>.jsonl exists and is >= 10 KB (empty slots are
    skipped); name = agents.json `project` label, last_used = transcript mtime. Merged
    through library.update(); entries already in the registry win (never overwritten).
 2. Running legacy terminals (tmux `claude-terminal[-N]`) are NOT touched — no rename, no
    restart (owner decision 2026-09-24). They stay under the old name until unloaded;
    meanwhile `library_cli ensure <id>` refuses (exit 4) because their claude runs the
    same uuid — never two claudes on one transcript.
 3. Each migrated slot's launch script becomes a shim (original kept as
    <script>.pre-library): legacy session running -> attach to it exactly as before;
    otherwise -> exec open-session.sh <id>. The order gate (_order_gate.py) is gone from
    the shim — the library decides what loads. The file is replaced by rename, never
    rewritten in place: a running bash reads its script lazily from the old inode.
 4. Dashboard swap/rollback only with the flags above.
Nothing is ever deleted; an index.html that matches neither page is saved as
index.html.pre-swap-<time> before it is overwritten.

tmux: only `list-sessions -F`, filtered here (exact names). Never display-message —
tmux 3.2a crashes the whole server on it with a missing target.
AGENTDECK_TMUX_SOCKET=<name> -> tmux -L <name>.
"""
import argparse
import filecmp
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import library  # noqa: E402
import library_cli  # noqa: E402

MIN_TRANSCRIPT = 10 * 1024
SHIM_MARK = "# MIGRATED-BY migrate_library.py"


# ── inputs ──────────────────────────────────────────────────────────────────
def legacy_scripts(repo):
    """{slot: (script path, legacy tmux session name)} from launch-claude*.sh.
    A shimmed script's slot/name are read from its .pre-library original."""
    out = {}
    for path in glob.glob(os.path.join(repo, "launch-claude*.sh")):
        if not re.fullmatch(r"launch-claude(-\d+)?\.sh", os.path.basename(path)):
            continue
        src = path + ".pre-library" if os.path.exists(path + ".pre-library") else path
        text = open(src, encoding="utf-8", errors="replace").read()
        slot = re.search(r'^AGENT_ID="?(\d+)"?', text, re.M)
        name = re.search(r'^SESSION="?([A-Za-z0-9_.-]+)"?', text, re.M)
        if slot and name:
            out[int(slot.group(1))] = (path, name.group(1))
    return out


def tmux_sessions():
    r = subprocess.run(library_cli.tmux_argv("list-sessions", "-F", "#{session_name}"),
                       capture_output=True, text=True, env=library_cli._clean_env(), timeout=15)
    return set(r.stdout.split()) if r.returncode == 0 else set()


def human_size(n):
    if n < 1024 * 1024:
        return f"{round(n / 1024)} КБ"
    return f"{n / 1024 / 1024:.1f} МБ"


def plan(repo, home, cwd, now=None):
    """Rows [{slot, id, uuid, size, mtime, name, skip, legacy, running, script, entry}]."""
    now = int(time.time() if now is None else now)
    sessions_dir = os.path.join(repo, ".sessions")
    try:
        agents = json.load(open(os.path.join(repo, "agents.json"), encoding="utf-8"))
    except (OSError, ValueError):
        agents = {}
    # transcript mtimes first: migrate_from_slots takes last_used as {id: time}
    stats = {}
    for f in os.listdir(sessions_dir):
        if re.fullmatch(r"agent-\d+\.id", f):
            u = open(os.path.join(sessions_dir, f)).read().strip()
            try:
                st = os.stat(library_cli.transcript_path(home, cwd, u))
                stats[u] = (st.st_size, int(st.st_mtime))
            except OSError:
                stats[u] = None
    lib, report = library.migrate_from_slots(
        sessions_dir, agents, cwd, now,
        last_used={library.id_from_uuid(u): s[1] for u, s in stats.items() if s})
    scripts = legacy_scripts(repo)
    running = tmux_sessions()
    rows = []
    for rep in report:
        slot = rep["slot"]
        script, legacy = scripts.get(slot, (None, None))
        row = dict(slot=slot, id=rep.get("id"), name=rep.get("name", ""), size=None,
                   mtime=None, skip=None, script=script, legacy=legacy,
                   running=bool(legacy and legacy in running), entry=None)
        if rep["skipped"]:
            row["skip"] = rep.get("reason", "?")
            rows.append(row)
            continue
        e = library.find(lib, rep["id"])
        st = stats.get(e["uuid"])
        if st is None:
            row["skip"] = "нет переписки"
        else:
            row["size"], row["mtime"] = st
            if st[0] < MIN_TRANSCRIPT:
                row["skip"] = "< 10 КБ"
        row["entry"] = e
        row["uuid"] = e["uuid"]
        rows.append(row)
    return rows


# ── shim ────────────────────────────────────────────────────────────────────
def shim_text(slot, legacy, sid, name):
    # no topic name here: the shims are committed to a public repo and the name is
    # private; the 8-hex id is enough to find the topic
    return f"""#!/bin/bash
{SHIM_MARK} — slot {slot} is now library topic {sid}.
# The original script is kept next to this one as $(basename "$0").pre-library.
# Legacy tmux session still running -> attach to it exactly as before (its claude
# is not restarted). Otherwise -> the library opens the topic (open-session.sh
# loads cs-{sid}, unloading an idle topic at the limit). No order gate: the
# library decides what loads.
SESSION="{legacy}"
AGENT_ID="{slot}"
LIB_ID="{sid}"
HERE="$(cd "$(dirname "${{BASH_SOURCE[0]:-$0}}")" && pwd)"

unset CLAUDE_CODE_SESSION CLAUDE_SESSION_ID CLAUDE_CODE CLAUDE_CODE_RUNNING CLAUDE_PARENT_SESSION ANTHROPIC_CLAUDE_CODE
for var in $(env | cut -d= -f1 | grep -i CLAUDE | grep -vx CLAUDE_BIN); do unset "$var"; done

TMUX_CMD=(tmux)
[ -n "${{AGENTDECK_TMUX_SOCKET:-}}" ] && TMUX_CMD=(tmux -L "$AGENTDECK_TMUX_SOCKET")

# exact name match in code (list-sessions), never display-message
if "${{TMUX_CMD[@]}}" list-sessions -F '#{{session_name}}' 2>/dev/null | grep -qxF -- "$SESSION"; then
  if [ "${{DRY_RUN:-}}" = "1" ]; then
    echo "attach =$SESSION"
    exit 0
  fi
  exec "${{TMUX_CMD[@]}}" attach-session -t "=$SESSION"
fi

exec bash "$HERE/open-session.sh" "$LIB_ID"
"""


def _atomic_write(path, text, mode):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)                 # new inode: a running bash keeps the old one


def install_shim(row):
    """'шим поставлен' / 'шим уже стоит' / 'шим обновлён'."""
    path = row["script"]
    text = shim_text(row["slot"], row["legacy"], row["id"], row["name"])
    pre = path + ".pre-library"
    mode = os.stat(path).st_mode & 0o7777 | 0o111
    if not os.path.exists(pre):
        shutil.copy2(path, pre)
        _atomic_write(path, text, mode)
        return "шим поставлен"
    if open(path, encoding="utf-8", errors="replace").read() == text:
        return "шим уже стоит"
    _atomic_write(path, text, mode)
    return "шим обновлён"


# ── dashboard ───────────────────────────────────────────────────────────────
def _copy(src, dst):
    tmp = f"{dst}.{os.getpid()}.tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _save_if_unknown(index, known):
    """index.html that matches none of the known pages is saved, not lost."""
    if os.path.exists(index) and not any(os.path.exists(k) and filecmp.cmp(index, k, shallow=False)
                                         for k in known):
        dst = f"{index}.pre-swap-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(index, dst)
        return [f"сохранена правленая {os.path.basename(index)} -> {os.path.basename(dst)}"]
    return []


def swap_dashboard(web):
    index, legacy, lib = (os.path.join(web, f) for f in
                          ("index.html", "index-legacy.html", "index-lib.html"))
    if not os.path.exists(lib):
        raise RuntimeError(f"нет {lib}")
    out = []
    if not os.path.exists(legacy):
        shutil.copy2(index, legacy)
        out.append("web/index.html -> web/index-legacy.html")
    else:
        out += _save_if_unknown(index, [legacy, lib])
    if filecmp.cmp(index, lib, shallow=False):
        out.append("дашборд уже новый (index.html = index-lib.html)")
    else:
        _copy(lib, index)
        out.append("web/index-lib.html -> web/index.html")
    return out


def rollback_dashboard(web):
    index, legacy, lib = (os.path.join(web, f) for f in
                          ("index.html", "index-legacy.html", "index-lib.html"))
    if not os.path.exists(legacy):
        raise RuntimeError("нет web/index-legacy.html — откатывать нечего")
    out = _save_if_unknown(index, [legacy, lib])
    if os.path.exists(index) and filecmp.cmp(index, legacy, shallow=False):
        return out + ["дашборд уже старый"]
    _copy(legacy, index)
    return out + ["web/index-legacy.html -> web/index.html"]


# ── report ──────────────────────────────────────────────────────────────────
def action_text(row, have_ids):
    if row["skip"]:
        return f"пропуск: {row['skip']}"
    parts = ["уже в реестре" if row["id"] in have_ids else "в реестр"]
    if row["running"]:
        parts.append(f"работает — остаётся {row['legacy']} до выгрузки")
    elif row["legacy"]:
        parts.append("не запущен")
    if row["script"]:
        parts.append(f"{os.path.basename(row['script'])} -> шим")
    else:
        parts.append("launch-скрипта нет")
    return "; ".join(parts)


def table(rows, have_ids):
    lines = [f"{'слот':>4}  {'id':8}  {'переписка':>9}  {'дата':16}  {'имя':34}  действие"]
    for r in rows:
        size = human_size(r["size"]) if r["size"] is not None else "—"
        date = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"])) if r["mtime"] else "—"
        name = (r["name"] or "")[:34]
        lines.append(f"{r['slot']:>4}  {r['id'] or '—':8}  {size:>9}  {date:16}  {name:34}  "
                     f"{action_text(r, have_ids)}")
    return "\n".join(lines)


# ── main ────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=HERE)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    dash = ap.add_mutually_exclusive_group()
    dash.add_argument("--swap-dashboard", action="store_true")
    dash.add_argument("--rollback-dashboard", action="store_true")
    a = ap.parse_args(argv)
    repo = os.path.abspath(a.repo)
    web = os.path.join(repo, "web")
    home = os.path.expanduser("~")
    cwd = library_cli.WORKDIR

    if a.rollback_dashboard:
        if not a.apply:
            print("сухой прогон: --apply --rollback-dashboard вернёт web/index-legacy.html "
                  "на место web/index.html")
            return 0
        try:
            for line in rollback_dashboard(web):
                print(line)
        except RuntimeError as ex:
            print(f"откат дашборда не выполнен: {ex}", file=sys.stderr)
            return 1
        return 0

    try:
        have = {e["id"] for e in library.load(library.LIB_FILE)["sessions"]}
    except library.CorruptRegistry as ex:
        print(f"реестр тем повреждён, ничего не делаю: {ex}", file=sys.stderr)
        return 1
    rows = plan(repo, home, cwd)
    print(f"реестр: {library.LIB_FILE}\nпереписки: {os.path.join(home, '.claude', 'projects', library_cli.slug(cwd))}\n")
    print(table(rows, have))
    todo = [r for r in rows if not r["skip"]]

    if not a.apply:
        print(f"\nсухой прогон: перенести {len(todo)} из {len(rows)} слотов; "
              "ничего не изменено (боевой — --apply).")
        if a.swap_dashboard:
            print("с --apply --swap-dashboard: web/index-lib.html станет web/index.html "
                  "(старая — web/index-legacy.html).")
        return 0

    # 1. registry (write only if something is new: a no-op run leaves file and .bak alone)
    new = [r["entry"] for r in todo if r["id"] not in have]
    try:
        if new:
            with library.update(library.LIB_FILE) as lib:
                for e in new:
                    if library.find(lib, e["id"]) is None:
                        lib["sessions"].append(e)
    except library.CorruptRegistry as ex:
        print(f"реестр тем повреждён, ничего не делаю: {ex}", file=sys.stderr)
        return 1

    # 2-3. legacy sessions untouched; launch scripts -> shims
    print("\nотчёт:")
    print(f"  реестр: добавлено {len(new)}, уже было {len(todo) - len(new)}")
    for r in todo:
        bits = [f"  слот {r['slot']} -> {r['id']} «{r['name']}»"]
        if r["running"]:
            bits.append(f"работает — остаётся {r['legacy']} до выгрузки")
        if r["script"]:
            bits.append(f"{os.path.basename(r['script'])}: {install_shim(r)}")
        print("; ".join(bits))
    for r in rows:
        if r["skip"]:
            print(f"  слот {r['slot']}: пропуск ({r['skip']}), скрипт не тронут")

    # 4. dashboard
    if a.swap_dashboard:
        try:
            for line in swap_dashboard(web):
                print(f"  {line}")
        except RuntimeError as ex:
            print(f"замена дашборда не выполнена: {ex}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
