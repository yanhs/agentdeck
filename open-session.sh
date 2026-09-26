#!/bin/bash
# One ttyd for every topic-session (PM2 app "sessions"):
#   ttyd -W -a -O -i lo -p 3031 --base-path /sess bash /home/ubuntu/pr/terminal/open-session.sh
# The page /sess/?arg=<id> runs `open-session.sh <id>` (ttyd -a passes the URL's
# ?arg= values as arguments). So "$@" is untrusted: exactly one 8-hex id (or
# the literal `shell`, see below) is accepted here, then library_cli.py checks it is in the registry and not
# archived, loads the topic into tmux (unloading an idle one at the limit of 12)
# and prints its tmux name; this script then attaches the tab to it.
#
#   library_cli exit 0 -> attach · 2 -> unknown/archived id · 3 -> all 12 busy
#   · 4 -> this conversation already runs in another claude (pid/session printed)
# library_cli prints the human-readable reason; we keep it on screen for
# OPEN_SESSION_PAUSE seconds (default 5) before the tab's shell ends.
# DRY_RUN=1 prints the command the pane would run and starts nothing.
# AGENTDECK_TMUX_SOCKET=<name> -> tmux -L <name> (tests use their own server).
#
# `shell` (/sess/?arg=shell, the dashboard's "cmd" button): the ONE plain command
# line, tmux session cmd-shell (`bash -l`, started by `library_cli.py
# shell-ensure` if missing); every tab attaches to the same one.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PAUSE="${OPEN_SESSION_PAUSE:-5}"

# CLAUDE* variables are scrubbed by library_cli (tmux env) and in the pane by
# bin/agentdeck-pane; not here, so a configured CLAUDE_BIN still reaches library_cli.
unset TMUX

done_with() {                      # $1 = exit code; the message is already printed
  sleep "$PAUSE"
  exit "$1"
}

# Explicit character class: a locale's [a-f] range could admit other letters.
if [ "$#" -ne 1 ] || ! [[ "$1" =~ ^[0123456789abcdef]{8}$ || "$1" == "shell" ]]; then
  echo "unknown session: open a topic from the list on the dashboard (/sess/?arg=<8-character id>)."
  done_with 2
fi
ID="$1"

TMUX_CMD=(tmux)
[ -n "${AGENTDECK_TMUX_SOCKET:-}" ] && TMUX_CMD=(tmux -L "$AGENTDECK_TMUX_SOCKET")

if [ "$ID" = "shell" ]; then
  if [ "${DRY_RUN:-}" = "1" ]; then
    echo "cmd-shell: bash -l"
    exit 0
  fi
  NAME="$(python3 "$HERE/library_cli.py" shell-ensure)"
  rc=$?
  if [ "$rc" -ne 0 ] || [ "$NAME" != "cmd-shell" ]; then
    echo "couldn't open the command line (library_cli: ${NAME:0:40})"
    done_with 1
  fi
  "${TMUX_CMD[@]}" attach-session -t "=cmd-shell"
  rc=$?
  # `exit` typed in the shell ends cmd-shell, attach returns and — if this script
  # ended too — ttyd's page would reconnect, run us again and shell-ensure would
  # start a NEW shell: `exit` would look like a restart. So when the shell is
  # gone, keep this connection open (ttyd's SIGHUP on tab close ends the sleep).
  # Topic ids never get here: their tabs keep ending and reconnecting as before.
  if ! "${TMUX_CMD[@]}" has-session -t "=cmd-shell" 2>/dev/null; then
    echo
    echo "Command line closed — press cmd to open a new one."
    exec sleep infinity
  fi
  exit "$rc"
fi

if [ "${DRY_RUN:-}" = "1" ]; then
  exec python3 "$HERE/library_cli.py" pane-cmd "$ID"
fi

NAME="$(python3 "$HERE/library_cli.py" ensure "$ID")"
rc=$?
if [ "$rc" -ne 0 ]; then
  [ "$rc" -eq 3 ] && echo "Try again later: reload the page or press Enter."
  done_with "$rc"
fi
# cs-$ID, or — for an old number of a terminal whose conversation changed (Claude's
# consent relaunch, /clear, /resume) — the terminal's name now: attach there
if ! [[ "$NAME" =~ ^cs-[0123456789abcdef]{8}$ ]]; then
  echo "couldn't open topic $ID (library_cli said: ${NAME:0:40})"
  done_with 1
fi

exec "${TMUX_CMD[@]}" attach-session -t "=$NAME"
