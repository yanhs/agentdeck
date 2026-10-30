#!/usr/bin/env bash
# AgentKennel guests — set the module up on a server where AgentKennel is installed.
# Other people work on your server, each in a Docker sandbox of their own, on your Claude
# subscription with a daily limit, through one shared Telegram bot (see guests/README.md).
#
#   guests/install.sh            set up, or upgrade / repair (running it again is safe)
#   guests/install.sh --check    only the checks, change nothing
#   guests/install.sh --yes      never ask anything (sudo must then work without a password)
#
# What it does:
#   1. guests/.env from .env.example, once (never overwritten) — fill it in;
#   2. the sandbox network "sbx" + its firewall: sbx-firewall.service, re-applied by
#      sbx-firewall.timer (boot + every 2 minutes; net/sbx-firewall.sh);
#   3. the images: agentkennel:<version> from this checkout, agentkennel-guest:<version> on top;
#   4. systemd units that run as you, from this checkout:
#        agentkennel-guest-turnstile   Claude turnstile   (172.31.250.1:8790)
#        agentkennel-guest-tgrelay     Telegram turnstile (172.31.250.1:8792)
#        agentkennel-guest-sitegate    site gate          (172.31.250.1:8794)
#      started once guests/.env names the domain and the bot; until then only written.
#      plus agentkennel-guest-diskwatch.timer: every 5 minutes, warns about full guest disks;
#      The same units under their names before v2.1.0 (agentdeck-guest-*) are stopped,
#      disabled and removed first; the guests' containers are Docker's and keep running.
#
# Test-only env: GUESTS_SYSTEMD_DIR (default /etc/systemd/system), SUDO (default sudo),
# GUESTS_SOURCE_ONLY=1 (load the functions, run nothing).
set -Eeuo pipefail

SYSTEMD_DIR="${GUESTS_SYSTEMD_DIR:-/etc/systemd/system}"
SUDO="${SUDO-sudo}"
SERVICE_UNITS="agentkennel-guest-turnstile.service agentkennel-guest-tgrelay.service agentkennel-guest-sitegate.service"
# compat: the same services under their names before v2.1.0 (moved by setup_services)
OLD_SERVICE_UNITS="${SERVICE_UNITS//agentkennel-guest-/agentdeck-guest-}"
NET_UNITS="sbx-firewall.service sbx-firewall.timer"
# the disk watchman (guests/disk.py): a oneshot service run by its timer
WATCH_UNITS="agentkennel-guest-diskwatch.service agentkennel-guest-diskwatch.timer"
YES=0
CHECK=0
CHANGED_UNITS=()
PROBLEMS=()

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }

_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
TEMPLATES="$_here"                 # this copy's unit templates
GUESTS_DIR="${GUESTS_DIR:-$_here}"  # where the units point (the checkout itself)
REPO_DIR="$(dirname "$GUESTS_DIR")"
ENV_FILE="$GUESTS_DIR/.env"

usage() {
  cat <<EOF
Usage: guests/install.sh [--check] [--yes]

Sets up the AgentKennel guests module on this server: the sandbox network and firewall,
the guest image, and the three host services (Claude turnstile, Telegram turnstile,
site gate). Running it again upgrades / repairs; nothing is duplicated.

  --check   only the checks, change nothing
  --yes     never ask anything (sudo must then work without a password)
  --help    this text

Settings: guests/.env (copied from guests/.env.example on the first run).
After it: python3 guests/guestctl.py doctor
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --check) CHECK=1 ;;
      --yes|-y) YES=1 ;;
      -h|--help) usage; exit 0 ;;
      *) printf 'error: unknown option %s (see --help)\n' "$1" >&2; exit 2 ;;
    esac
    shift
  done
}

# ── units from the templates (unit-tested: guests/tests/test_install_sh.py) ──────────
sd_esc() { local v="${1//%/%%}"; printf '%s' "$v"; }   # systemd: % -> %%

unit_template() {
  case "$1" in
    sbx-firewall.service|sbx-firewall.timer) printf '%s\n' "$TEMPLATES/net/$1" ;;
    agentkennel-guest-*.service|agentkennel-guest-*.timer) printf '%s\n' "$TEMPLATES/systemd/$1" ;;
    *) return 1 ;;
  esac
}

render_unit() {
  local src text
  src="$(unit_template "$1")" || return 1
  [ -f "$src" ] || return 1
  text="$(cat "$src")"
  text="${text//@GUESTS_DIR@/$(sd_esc "$GUESTS_DIR")}"
  text="${text//@USER@/$(sd_esc "${GUESTS_USER:-}")}"
  text="${text//@HOME@/$(sd_esc "${GUESTS_HOME:-}")}"
  printf '%s\n' "$text"
}

# write a unit only when its text changed; changed names collect in CHANGED_UNITS
write_unit() {
  local name="$1" target="$SYSTEMD_DIR/$1" content tmp
  content="$(render_unit "$name")" || die "no template for $name"
  if [ -f "$target" ] && [ "$(cat "$target")" = "$content" ]; then
    return 0
  fi
  tmp="$(mktemp)"
  printf '%s\n' "$content" > "$tmp"
  $SUDO mkdir -p "$SYSTEMD_DIR"
  $SUDO install -m 0644 "$tmp" "$target"
  rm -f "$tmp"
  CHANGED_UNITS+=("$name")
}

env_has() {  # env_has KEY — guests/.env sets KEY to something
  [ -f "$ENV_FILE" ] && grep -Eq "^[[:space:]]*(export[[:space:]]+)?$1=[\"']?[^\"'[:space:]]" "$ENV_FILE"
}

# ── checks ───────────────────────────────────────────────────────────────────────
problem() { PROBLEMS+=("$1"); printf '  missing: %s\n' "$1"; }
ok()      { printf '  ok:      %s\n' "$1"; }

check_docker() {
  local out
  if ! command -v docker >/dev/null 2>&1; then
    problem "Docker is not installed — https://docs.docker.com/engine/install/"
    return
  fi
  if out="$(docker info --format '{{.ServerVersion}}' 2>&1)"; then
    ok "Docker ${out}"
  else
    case "$out" in
      *"permission denied"*|*"Permission denied"*)
        problem "Docker: this user may not use it — sudo usermod -aG docker $(id -un), then log in again" ;;
      *) problem "Docker is not running — sudo systemctl start docker" ;;
    esac
  fi
}

preflight() {
  PROBLEMS=()
  if [ -f "$REPO_DIR/Dockerfile" ] && [ -f "$REPO_DIR/services_keeper.py" ]; then
    ok "AgentKennel checkout: $REPO_DIR"
  else
    problem "AgentKennel itself: $REPO_DIR has no Dockerfile / services_keeper.py (guests/ lives inside an AgentKennel checkout)"
  fi
  command -v systemctl >/dev/null 2>&1 && ok "systemd" || problem "systemd (systemctl) — the services run under it"
  check_docker
  command -v python3 >/dev/null 2>&1 && ok "python3" || problem "python3"
  if command -v iptables >/dev/null 2>&1 || [ -x /usr/sbin/iptables ] || [ -x /sbin/iptables ]; then
    ok "iptables"
  else
    problem "iptables — the sandbox firewall needs it (apt-get install iptables)"
  fi
  # a setup from before 2.0 runs the same services as user units: a second copy would
  # fight it for the same ports
  local u
  for u in $SERVICE_UNITS $OLD_SERVICE_UNITS; do
    if systemctl --user is-active --quiet "$u" 2>/dev/null; then
      problem "$u already runs as a user unit (systemctl --user) — stop and disable those first"
      break
    fi
  done
  if command -v nginx >/dev/null 2>&1 && command -v certbot >/dev/null 2>&1; then
    ok "nginx + certbot (web addresses for the guests)"
  else
    warn "nginx and certbot are needed for the guests' web addresses (sb-<name>.<domain>); Telegram works without them"
  fi
  if [ -f "$HOME/.claude/oauth.env" ] || [ -n "${TURNSTILE_OAUTH_ENV:-}" ]; then
    ok "your Claude login for the turnstile (~/.claude/oauth.env)"
  else
    warn "no ~/.claude/oauth.env: the Claude turnstile needs CLAUDE_CODE_OAUTH_TOKEN=... there (claude setup-token)"
  fi
  if [ -f "$ENV_FILE" ]; then
    ok "settings: guests/.env"
  else
    say "  note:    settings: guests/.env does not exist yet — it is made from guests/.env.example on install"
  fi
  [ "${#PROBLEMS[@]}" -eq 0 ]
}

need_sudo() {
  [ -n "$SUDO" ] || return 0
  if [ "$YES" = 1 ]; then
    $SUDO -n true 2>/dev/null || die "--yes needs sudo without a password (sudo -n true failed); run without --yes to type it"
  fi
}

# ── steps ────────────────────────────────────────────────────────────────────────
ensure_env() {
  if [ -f "$ENV_FILE" ]; then
    say "guests/.env is there (kept as it is)"
  else
    install -m 0600 "$GUESTS_DIR/.env.example" "$ENV_FILE"
    say "wrote guests/.env from .env.example — fill it in"
  fi
}

setup_network() {
  local u
  for u in $NET_UNITS; do write_unit "$u"; done
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable -q --now sbx-firewall.timer
  $SUDO systemctl start sbx-firewall.service
  say "network sbx (172.31.250.0/24) and its firewall: applied, re-applied every 2 minutes"
}

build_image() {
  local tag version
  tag="$(python3 "$GUESTS_DIR/guestctl.py" image-tag)"
  [ -n "$tag" ] || die "guestctl.py image-tag printed nothing"
  version="${tag##*:}"
  docker build -t "agentkennel:$version" "$REPO_DIR"
  docker build -t "$tag" --build-arg "BASE=agentkennel:$version" "$GUESTS_DIR/image"
  say "image: $tag"
}

# a setup from before v2.1.0 has the same services as agentdeck-guest-* system units:
# stop, disable and remove them so the new names can take the ports
move_old_units() {
  local u moved=()
  for u in $OLD_SERVICE_UNITS; do
    [ -e "$SYSTEMD_DIR/$u" ] || continue
    $SUDO systemctl disable --now -q "$u" 2>/dev/null || true
    $SUDO rm -f "$SYSTEMD_DIR/$u"
    moved+=("$u")
  done
  [ "${#moved[@]}" -gt 0 ] || return 0
  $SUDO systemctl daemon-reload
  $SUDO systemctl reset-failed ${moved[*]} 2>/dev/null || true
  say "moved the services to their new names: ${moved[*]//agentdeck-guest-/} (agentdeck-guest-* → agentkennel-guest-*)"
}

setup_services() {
  local u ready=1
  move_old_units
  GUESTS_USER="$(id -un)"
  GUESTS_HOME="$(getent passwd "$GUESTS_USER" | cut -d: -f6)"
  GUESTS_HOME="${GUESTS_HOME:-$HOME}"
  for u in $SERVICE_UNITS $WATCH_UNITS; do write_unit "$u"; done
  if [ "${#CHANGED_UNITS[@]}" -gt 0 ]; then
    say "unit files written: ${CHANGED_UNITS[*]}"
  else
    say "unit files unchanged"
  fi
  $SUDO systemctl daemon-reload
  env_has GUESTS_DOMAIN || ready=0
  env_has GUEST_BOT_TOKEN || ready=0
  if [ "$ready" = 0 ]; then
    say ""
    say "Not started yet: guests/.env needs at least GUESTS_DOMAIN and GUEST_BOT_TOKEN"
    say "(and OWNER_TG_ID for invitations). Fill it in and run guests/install.sh again."
    return 0
  fi
  # shellcheck disable=SC2086
  $SUDO systemctl enable -q $SERVICE_UNITS
  # shellcheck disable=SC2086
  $SUDO systemctl restart $SERVICE_UNITS
  say "services running: ${SERVICE_UNITS//.service/}"
  $SUDO systemctl enable -q --now agentkennel-guest-diskwatch.timer
  say "disk watchman: every 5 minutes (warns about full guest disks, never stops anything)"
}

finish() {
  cat <<EOF

Done. Next:
  python3 $GUESTS_DIR/guestctl.py doctor          everything set up?
  python3 $GUESTS_DIR/guestctl.py files-web       https://sbx.<domain> for shared files (once)
  python3 $GUESTS_DIR/guestctl.py add <name> --tg <telegram id>   or: people /start the guest bot
EOF
}

main() {
  parse_args "$@"
  step "Checks"
  if ! preflight; then
    [ "$CHECK" = 1 ] && { say ""; say "--check: ${#PROBLEMS[@]} problem(s) above; nothing was changed"; exit 1; }
    die "fix the problems above, then run guests/install.sh again"
  fi
  if [ "$CHECK" = 1 ]; then
    say ""
    say "--check: ready; nothing was changed"
    exit 0
  fi
  need_sudo
  step "Settings";                       ensure_env
  step "Sandbox network and firewall";   setup_network
  step "Guest image";                    build_image
  step "Services";                       setup_services
  finish
}

[ "${GUESTS_SOURCE_ONLY:-}" = 1 ] && return 0 2>/dev/null
main "$@"
