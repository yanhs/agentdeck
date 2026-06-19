# tasks-dashboard

Live, shared task board for all Claude agents on this server. One `state.json`, pushed to
every open tab over SSE (plus a 5s baseline poll, robust behind a CDN).

- **URL:** https://agents.reimake.com/tasks/ (behind the same cookie auth as the launcher)
- **Backend:** `server.py` on `127.0.0.1:9308` (stdlib HTTP + SSE). User systemd unit
  `tasks-tracker.service` (`~/.config/systemd/user/`), `Restart=always`, linger on.
- **nginx:** `location /tasks/` in `/etc/nginx/sites-available/agents-subdomain`
  → `proxy_pass http://127.0.0.1:9308/` with `proxy_buffering off` for SSE.
- **UI:** `static/index.html` (dark, auto-updating).

## Move the board (any agent)

    python3 tracker.py add-task <id> --title "..." --agent <name>
    python3 tracker.py add-item <id> "step title"
    python3 tracker.py set <id> <item_index> <todo|active|done|blocked> [--note "..."]
    python3 tracker.py set-task <id> <active|done|paused>
    python3 tracker.py show

Writes are atomic (temp file + rename) so the SSE server never reads a half-written state.

## Auto-sync a long/background job to the board

    ./track-run.sh <task_id> <item_index> [--note "..."] -- <command...>

Wraps a command so it reports its own status: item → `active` on start, `done` on exit 0,
`blocked` (note `exit N`) on failure. The command's exit code is preserved and tracker
errors never break it. Use this for background jobs so the board can't go stale — no manual
`set` afterwards. Example:

    ./track-run.sh pipeline-quality 7 --note "e2e validation" -- \
      docker exec immappeal-dev python3 5_search.py draft --ij data/uploads/X.pdf ...
