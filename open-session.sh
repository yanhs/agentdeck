#!/bin/bash
# One ttyd for every topic-session (PM2 app "sessions"):
#   ttyd -W -a -O -i lo -p 3031 --base-path /sess bash /home/ubuntu/pr/terminal/open-session.sh
# The page /sess/?arg=<id> runs `open-session.sh <id>` (ttyd -a passes the URL's
# ?arg= values as arguments). So "$@" is untrusted: exactly one 8-hex id is
# accepted here, then library_cli.py checks it is in the registry and not
# archived, loads the topic into tmux (unloading an idle one at the limit of 12)
# and prints its tmux name; this script then attaches the tab to it.
#
#   library_cli exit 0 -> attach · 2 -> unknown/archived id · 3 -> all 12 busy
#   · 4 -> this conversation already runs in another claude (pid/session printed)
# library_cli prints the human-readable reason; we keep it on screen for
# OPEN_SESSION_PAUSE seconds (default 5) before the tab's shell ends.
# DRY_RUN=1 prints the command the pane would run and starts nothing.
# AGENTDECK_TMUX_SOCKET=<name> -> tmux -L <name> (tests use their own server).

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PAUSE="${OPEN_SESSION_PAUSE:-5}"

# CLAUDE* variables are scrubbed by library_cli (tmux env) and by the pane
# command itself; not here, so a configured CLAUDE_BIN still reaches library_cli.
unset TMUX

done_with() {                      # $1 = exit code; the message is already printed
  sleep "$PAUSE"
  exit "$1"
}

# Explicit character class: a locale's [a-f] range could admit other letters.
if [ "$#" -ne 1 ] || ! [[ "$1" =~ ^[0123456789abcdef]{8}$ ]]; then
  echo "unknown session: откройте тему из списка на дашборде (/sess/?arg=<8-значный код>)."
  done_with 2
fi
ID="$1"

TMUX_CMD=(tmux)
[ -n "${AGENTDECK_TMUX_SOCKET:-}" ] && TMUX_CMD=(tmux -L "$AGENTDECK_TMUX_SOCKET")

if [ "${DRY_RUN:-}" = "1" ]; then
  exec python3 "$HERE/library_cli.py" pane-cmd "$ID"
fi

NAME="$(python3 "$HERE/library_cli.py" ensure "$ID")"
rc=$?
if [ "$rc" -ne 0 ]; then
  [ "$rc" -eq 3 ] && echo "Повторите позже: обновите страницу или нажмите Enter."
  done_with "$rc"
fi
if [ "$NAME" != "cs-$ID" ]; then
  echo "не удалось открыть тему $ID (library_cli ответил: ${NAME:0:40})"
  done_with 1
fi

exec "${TMUX_CMD[@]}" attach-session -t "=cs-$ID"
