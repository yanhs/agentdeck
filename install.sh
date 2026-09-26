#!/usr/bin/env bash
# AgentDeck — one-command install on your own server. The agents get the whole machine:
# they run as you, with your sudo, so they can install packages, databases, nginx,
# HTTPS, Docker… exactly like you at the keyboard. Use a dedicated VPS, not your laptop.
#
#   curl -fsSL https://raw.githubusercontent.com/yanhs/agentdeck/master/install.sh | bash
#   ./install.sh                 (from a clone: installs that clone as it is)
#
#   --yes            never ask anything (sudo must then work without a password)
#   --https [domain] HTTPS on ports 80/443 with a free Let's Encrypt certificate;
#                    no domain → <your-ip>.sslip.io (a name that resolves to your IP)
#   --http           back to plain http on :8765 (after --https)
#   --check          only the preflight checks, change nothing
#   --telegram       also print how to connect a Telegram bot (set up in the dashboard)
#   --uninstall      stop and remove the services (terminals, board, password stay)
#   --purge          with --uninstall: also delete ~/agentdeck and its data
#
# Supported: Ubuntu 22.04 / 24.04, Debian 12 — x86_64 or aarch64. Anything else: the
# Docker sandbox (docker compose up -d), which runs anywhere Docker does.
#
# What it installs: apt packages (tmux, python3, git, curl, …), ttyd + Caddy (release
# binaries in /usr/local/bin), Node.js 22 (NodeSource, only if no Node 18+ is present),
# Claude Code (npm -g, the version pinned in the Dockerfile; CLAUDE_CODE_VERSION=… to
# override), the repo in ~/agentdeck, guard hooks in ~/.claude (AGENTDECK_GUARDS=0 skips),
# and systemd services that start at boot:
#   agentdeck-status    backend + login gate (status_server.py)
#   agentdeck-tasks     task board (/tasks/)
#   agentdeck-sessions  the one ttyd behind /sess/ (every terminal)
#   agentdeck-caddy     proxy + login + HTTPS, on :8765 (or 80/443 with --https)
#   agentdeck-reaper    .timer: idle_reaper.py once a minute
# Why system units with User=<you> and not `systemctl --user` + linger: they need no user
# D-Bus session or XDG_RUNTIME_DIR (a fresh SSH-less boot has neither), Caddy can get the
# capability to bind 80/443 without root, and `systemctl status agentdeck-*` just works.
#
# Running it again upgrades / repairs; nothing is duplicated. It stops at the first error
# and says what to do; after fixing that, run it again and it continues.
set -Eeuo pipefail

REPO_URL="${AGENTDECK_REPO_URL:-https://github.com/yanhs/agentdeck.git}"
BRANCH="${AGENTDECK_BRANCH:-master}"
CADDY_VERSION="${CADDY_VERSION:-2.11.4}"
NODE_MAJOR=22
NODE_MIN=18                       # Claude Code runs on Node 18+; an existing one is kept
SYSTEMD_DIR="${AGENTDECK_SYSTEMD_DIR:-/etc/systemd/system}"
PORT="${AGENTDECK_PORT:-8765}"
HTTP_PORT="${AGENTDECK_HTTP_PORT:-80}"
HTTPS_PORT="${AGENTDECK_HTTPS_PORT:-443}"
SUDO="${SUDO-sudo}"
UNITS="agentdeck-status.service agentdeck-tasks.service agentdeck-sessions.service agentdeck-caddy.service agentdeck-reaper.service agentdeck-reaper.timer"
# the units that run (the reaper .service is started by its timer)
RUN_UNITS="agentdeck-status.service agentdeck-tasks.service agentdeck-sessions.service agentdeck-caddy.service agentdeck-reaper.timer"
SANDBOX_HINT="git clone https://github.com/yanhs/agentdeck && cd agentdeck && docker compose up -d"
CHANGED_UNITS=()
STEP="start"
# What this installer set up is recorded here (outside the repo): the install folder,
# whether the installer cloned it itself (only then may --purge delete it), the tmux
# folder of the agents' own tmux server, and the site. --uninstall acts ONLY on these.
CONF_FILE="${AGENTDECK_CONF:-${XDG_CONFIG_HOME:-$HOME/.config}/agentdeck/install.env}"
MARKER=".agentdeck-managed"      # in a folder the installer cloned itself
TMUX_SUBDIR=".sessions/tmux"     # TMUX_TMPDIR of the agents: a tmux server of their own

say()  { printf '%s\n' "$*"; }
step() { STEP="$*"; printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  {
  printf '\nerror: %s\n' "$*" >&2
  exit 1
}

# ── pure helpers (unit-tested: tests/test_install_sh.py) ─────────────────────────────
# "ubuntu 22.04" | "ubuntu 24.04" | "debian 12" on stdout, or exit 1
detect_os() {
  local f="${AGENTDECK_OS_RELEASE:-/etc/os-release}" id ver
  [ -r "$f" ] || return 1
  id="$(. "$f" 2>/dev/null; printf '%s' "${ID:-}")"
  ver="$(. "$f" 2>/dev/null; printf '%s' "${VERSION_ID:-}")"
  case "$id $ver" in
    "ubuntu 22.04"|"ubuntu 24.04"|"debian 12") printf '%s %s\n' "$id" "$ver" ;;
    *) return 1 ;;
  esac
}

os_name() {  # a readable name for messages
  local f="${AGENTDECK_OS_RELEASE:-/etc/os-release}"
  [ -r "$f" ] || { printf 'an unknown system (no /etc/os-release)'; return; }
  (. "$f" 2>/dev/null; printf '%s' "${PRETTY_NAME:-${ID:-unknown} ${VERSION_ID:-}}")
}

ttyd_asset() {
  case "$1" in
    x86_64|amd64) echo ttyd.x86_64 ;;
    aarch64|arm64) echo ttyd.aarch64 ;;
    *) return 1 ;;
  esac
}

caddy_arch() {
  case "$1" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *) return 1 ;;
  esac
}

valid_ipv4() { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }

public_ip() {  # AGENTDECK_PUBLIC_IP, else ask the internet; exit 1 if unknown
  local ip="${AGENTDECK_PUBLIC_IP:-}" u
  if [ -z "$ip" ]; then
    for u in https://api.ipify.org https://ifconfig.me https://icanhazip.com; do
      ip="$(curl -4 -fsS --max-time 5 "$u" 2>/dev/null | tr -d '[:space:]')" || ip=""
      valid_ipv4 "$ip" && break
      ip=""
    done
  fi
  valid_ipv4 "$ip" || return 1
  printf '%s\n' "$ip"
}

# the site name for --https [domain]: the domain, or <ip-with-dashes>.sslip.io
https_site() {
  local d="${1:-}" ip
  if [ -n "$d" ]; then
    [[ "$d" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] \
      || return 1
    printf '%s\n' "$d"
    return 0
  fi
  ip="$(public_ip)" || return 1
  printf '%s.sslip.io\n' "${ip//./-}"
}

port_busy() {  # exit 0 when something listens on the port
  local p="$1"
  if command -v ss >/dev/null 2>&1 && [ -n "$(ss -ltnH "sport = :$p" 2>/dev/null)" ]; then
    return 0
  fi
  (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null
}

port_is_ours() {  # a re-run: our own running Caddy serves this port already
  local site ports
  [ -f "$SYSTEMD_DIR/agentdeck-caddy.service" ] || return 1
  systemctl is-active --quiet agentdeck-caddy.service 2>/dev/null || return 1
  site="$(sed -n 's/^Environment="\{0,1\}AGENTDECK_SITE=\([^"]*\)"\{0,1\}$/\1/p' \
          "$SYSTEMD_DIR/agentdeck-caddy.service" | head -n 1)"
  case "$site" in :*) ports="${site#:}" ;; "") return 1 ;; *) ports="$HTTP_PORT $HTTPS_PORT" ;; esac
  case " $ports " in *" $1 "*) return 0 ;; esac
  return 1
}

# systemd value quoting: % → %%; a value with spaces goes in double quotes
sd_esc() { local v="${1//%/%%}"; printf '%s' "$v"; }
sd_arg() { printf '"%s"' "$(sd_esc "$1")"; }
sd_env() {
  local kv; kv="$(sd_esc "$1=$2")"
  case "$kv" in *" "*) printf 'Environment="%s"\n' "$kv" ;; *) printf 'Environment=%s\n' "$kv" ;; esac
}

unit_service_common() {
  local d="$AGENTDECK_DIR" h="$AGENTDECK_HOME"
  printf 'User=%s\n' "$AGENTDECK_USER"
  printf 'WorkingDirectory=%s\n' "$(sd_esc "$d")"
  sd_env HOME "$h"
  sd_env PATH "${AGENTDECK_PATH:-$h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
  sd_env LANG C.UTF-8
  sd_env LC_ALL C.UTF-8
  sd_env AGENTDECK_WORKDIR "$h"
  sd_env AGENTDECK_PASSFILE "$d/.sessions/.dashpass"
  sd_env AGENTDECK_AUTH_SECRET "$d/.sessions/.agents_auth_secret"
  sd_env TRACKER_STATE "$d/.sessions/tasks-state.json"
  sd_env AGENTDECK_SITE "${AGENTDECK_SITE:-:8765}"
  # the agents' tmux server gets a folder of its own: every tmux call of AgentDeck (and
  # of the agents) talks to it, and none touches your own tmux sessions
  sd_env TMUX_TMPDIR "$d/$TMUX_SUBDIR"
  sd_env XDG_DATA_HOME "$h/.local/share"
  sd_env XDG_CONFIG_HOME "$h/.config"
  [ -n "${AGENTDECK_CLAUDE_BIN:-}" ] && sd_env CLAUDE_BIN "$AGENTDECK_CLAUDE_BIN"
  # claude refuses --dangerously-skip-permissions as root unless told it is on purpose
  [ "$AGENTDECK_USER" = root ] && sd_env IS_SANDBOX 1
  # your own settings (AGENTDECK_PASSWORD, AGENTDECK_ORIGIN, AGENTDECK_EMAIL, …) win
  printf 'EnvironmentFile=-%s\n' "$(sd_esc "$d/.env")"
}

render_unit() {
  local d="$AGENTDECK_DIR" py=/usr/bin/python3
  case "$1" in
    agentdeck-status.service)
      cat <<EOF
[Unit]
Description=AgentDeck backend (status_server.py: API + login gate)
After=network-online.target
Wants=network-online.target

[Service]
$(unit_service_common)
ExecStart=$py $(sd_arg "$d/status_server.py")
Restart=always
RestartSec=2
# the tmux server with every agent is forked from here: a restart must not kill it
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
      ;;
    agentdeck-tasks.service)
      cat <<EOF
[Unit]
Description=AgentDeck task board (/tasks/)
After=network-online.target

[Service]
$(unit_service_common)
ExecStart=$py $(sd_arg "$d/tasks-dashboard/server.py")
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
      ;;
    agentdeck-sessions.service)
      cat <<EOF
[Unit]
Description=AgentDeck terminals (one ttyd for /sess/ -> open-session.sh)
After=network-online.target

[Service]
$(unit_service_common)
ExecStart=/usr/local/bin/ttyd -W -a -O -i lo -p 3031 --base-path /sess /bin/bash $(sd_arg "$d/open-session.sh")
Restart=always
RestartSec=2
# the agents' tmux server lives in this unit too: restarting ttyd must not kill them
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
      ;;
    agentdeck-caddy.service)
      cat <<EOF
[Unit]
Description=AgentDeck proxy + login + HTTPS (Caddy)
After=network-online.target agentdeck-status.service
Wants=network-online.target

[Service]
$(unit_service_common)
# Let's Encrypt refuses an @example.* contact address (the Caddyfile default): drop it then
ExecStartPre=/bin/sh -c 'case "\$\${AGENTDECK_EMAIL:-}" in ""|*@example.com|*@example.org|*@example.net) grep -v "^[[:space:]]*email " Caddyfile ;; *) cat Caddyfile ;; esac > .sessions/Caddyfile.run'
ExecStart=/usr/local/bin/caddy run --config .sessions/Caddyfile.run --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config .sessions/Caddyfile.run --adapter caddyfile
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
      ;;
    agentdeck-reaper.service)
      cat <<EOF
[Unit]
Description=AgentDeck idle reaper (unloads terminals nobody uses)

[Service]
Type=oneshot
$(unit_service_common)
ExecStart=$py $(sd_arg "$d/idle_reaper.py")
EOF
      ;;
    agentdeck-reaper.timer)
      cat <<EOF
[Unit]
Description=AgentDeck idle reaper, once a minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF
      ;;
    *) return 1 ;;
  esac
}

# write a unit only when its text changed; changed names collect in CHANGED_UNITS
write_unit() {
  local name="$1" target="$SYSTEMD_DIR/$1" content tmp
  content="$(render_unit "$name")"
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

conf_get() {  # conf_get KEY — a value from the install record ("" if none)
  [ -f "$CONF_FILE" ] || return 0
  sed -n "s/^$1=//p" "$CONF_FILE" | tail -n 1
}

# the folder --purge may delete: recorded as installer-cloned AND carrying the marker,
# absolute, not / and not $HOME. Prints it, or exits 1 with nothing printed.
purge_target() {
  local d; d="$(conf_get AGENTDECK_DIR)"
  [ -n "$d" ] && [ "$(conf_get AGENTDECK_MANAGED)" = 1 ] || return 1
  case "$d" in /*) ;; *) return 1 ;; esac
  d="${d%/}"
  [ -n "$d" ] && [ "$d" != "${HOME%/}" ] && [ -d "$d" ] && [ ! -L "$d" ] || return 1
  [ -f "$d/$MARKER" ] || return 1
  printf '%s\n' "$d"
}


# ── the installer ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<'EOF'
AgentDeck installer — agents get the whole server (use a dedicated VPS).

  curl -fsSL https://raw.githubusercontent.com/yanhs/agentdeck/master/install.sh | bash
  ./install.sh [options]          (from a clone)

  --yes, -y          never ask anything (sudo must work without a password)
  --https [domain]   HTTPS on 80/443 with Let's Encrypt; no domain → <ip>.sslip.io
  --http             back to plain http on :8765
  --check            preflight checks only, change nothing
  --telegram         also print how to connect a Telegram bot
  --uninstall        stop + remove the services (keeps terminals, board, password)
  --purge            with --uninstall: also delete ~/agentdeck and its data
  -h, --help         this help

Env: AGENTDECK_GUARDS=0 (no guard hooks), CLAUDE_CODE_VERSION=<ver|latest>,
     AGENTDECK_DIR (default ~/agentdeck), AGENTDECK_PORT (default 8765),
     AGENTDECK_ALLOW_ROOT=1 (install as root — agents then run as root).
Supported: Ubuntu 22.04 / 24.04, Debian 12 (x86_64, aarch64).
EOF
}

YES=0 CHECK=0 UNINSTALL=0 PURGE=0 TELEGRAM=0 HTTPS=0 HTTPS_OFF=0 DOMAIN=""
ARGS=("$@")
parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      -y|--yes) YES=1 ;;
      --check) CHECK=1 ;;
      --uninstall) UNINSTALL=1 ;;
      --purge) PURGE=1 ;;
      --telegram) TELEGRAM=1 ;;
      --http) HTTPS_OFF=1 ;;
      --https)
        HTTPS=1
        if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then DOMAIN="$2"; shift; fi ;;
      --https=*) HTTPS=1; DOMAIN="${1#--https=}" ;;
      -h|--help) usage; exit 0 ;;
      *) printf 'error: unknown option: %s (see --help)\n' "$1" >&2; exit 2 ;;
    esac
    shift
  done
  if [ "$PURGE" = 1 ] && [ "$UNINSTALL" = 0 ]; then
    die "--purge goes with --uninstall"
  fi
}

refuse_unsupported() {
  cat >&2 <<EOF

AgentDeck's installer runs on Ubuntu 22.04 / 24.04 and Debian 12 (x86_64 or aarch64).
This machine is $1: not supported — nothing was changed.

Use the Docker sandbox instead (runs anywhere Docker does):
  $SANDBOX_HINT
EOF
  exit 1
}

on_error() {
  local rc=$? line=$1 cmd=$2
  # inside $(…) or a pipeline part: just pass the failure up — the caller decides
  [ "$BASHPID" = "$$" ] || exit "$rc"
  trap - ERR
  printf '\nerror: the step "%s" failed (exit %s at line %s: %s).\n' "$STEP" "$rc" "$line" "$cmd" >&2
  printf 'Fix the cause shown above, then run the installer again — it continues where it stopped.\n' >&2
  exit "$rc"
}

# the clone this script belongs to (./install.sh), or "" when piped (curl | bash)
self_dir() {
  local src="${BASH_SOURCE[0]:-}" d
  [ -n "$src" ] && [ -f "$src" ] || return 0
  d="$(cd "$(dirname "$src")" && pwd)"
  [ -f "$d/status_server.py" ] && [ -f "$d/open-session.sh" ] && printf '%s\n' "$d"
  return 0
}

preflight() {
  local arch os
  arch="$(uname -m)"
  os="$(detect_os)" || refuse_unsupported "$(os_name) ($arch)"
  ttyd_asset "$arch" >/dev/null || refuse_unsupported "$(os_name) on a $arch CPU"
  ARCH="$arch" OS="$os"

  if [ "$(id -u)" = 0 ]; then
    if [ "${AGENTDECK_ALLOW_ROOT:-}" != 1 ]; then
      cat >&2 <<'EOF'
error: you are running this as root. The agents would run as root as well — nothing
between them and the machine, not even sudo. Run it as a normal user who has sudo:

  adduser deck && usermod -aG sudo deck      # once, as root
  su - deck                                  # then install as deck

To install as root anyway (the agents WILL run as root): AGENTDECK_ALLOW_ROOT=1
EOF
      exit 1
    fi
    SUDO=""
  else
    SUDO=sudo
    command -v sudo >/dev/null 2>&1 \
      || die "this needs sudo (the user installs packages and system services). Install sudo and add $(id -un) to the sudo group, or run the Docker sandbox: $SANDBOX_HINT"
    if ! sudo -n true 2>/dev/null; then
      if [ "$YES" = 1 ]; then
        die "sudo asks for a password, and --yes means nobody can type it. Run without --yes (it asks once), or allow passwordless sudo for $(id -un)."
      elif [ "$CHECK" = 1 ]; then
        say "note: sudo will ask for your password once"
      else
        say "sudo needs your password once (for apt and the system services):"
        sudo -v </dev/tty || die "sudo failed — $(id -un) needs sudo rights"
      fi
    fi
  fi

  if [ "$UNINSTALL" = 1 ]; then
    if [ "$PURGE" = 1 ] && ! purge_target >/dev/null; then
      die "--purge deletes only a folder this installer cloned itself (recorded in $CONF_FILE, with a $MARKER marker inside). There is none here — nothing was changed. Run --uninstall without --purge; delete your own clone yourself if you want it gone."
    fi
    return 0
  fi

  # which address Caddy serves: --https [domain] / --http / the last install's choice
  SITE=":$PORT"
  local prev
  prev="$(conf_get AGENTDECK_SITE)"
  if [ "$HTTPS" = 1 ]; then
    SITE="$(https_site "$DOMAIN")" || {
      if [ -n "$DOMAIN" ]; then die "\"$DOMAIN\" is not a domain name (just the name, e.g. deck.example.com)"
      else die "couldn't find this server's public IPv4 address for <ip>.sslip.io — pass your domain: --https your-domain.com (or set AGENTDECK_PUBLIC_IP)"; fi
    }
  elif [ "$HTTPS_OFF" = 0 ] && [ -n "$prev" ]; then
    SITE="$prev"
  fi

  local p ports
  case "$SITE" in :*) ports="${SITE#:}" ;; *) ports="$HTTP_PORT $HTTPS_PORT" ;; esac
  for p in $ports; do
    if port_busy "$p" && ! port_is_ours "$p"; then
      if [ "$ports" = "$PORT" ]; then
        die "port $p is busy — another program listens on it. Free it, or pick another port: AGENTDECK_PORT=8766 ./install.sh"
      fi
      die "port $p is busy — HTTPS needs ports 80 and 443 free (another web server such as nginx/apache/caddy is on it). Stop it, or install without --https (plain http on :$PORT)."
    fi
  done
}

apt_install() {  # only what is missing; apt waits for a lock (a fresh VPS runs unattended-upgrades)
  local missing=() p
  for p in "$@"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
  done
  [ "${#missing[@]}" = 0 ] && { say "already installed: $*"; return 0; }
  say "installing: ${missing[*]}"
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 update -q
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q \
    --no-install-recommends "${missing[@]}"
}

dockerfile_arg() {  # a version pinned in the Dockerfile (one source for both installs)
  sed -n "s/^ARG $1=//p" "$DIR/Dockerfile" 2>/dev/null | head -n 1
}

install_ttyd() {
  local ver asset tmp
  ver="${TTYD_VERSION:-$(dockerfile_arg TTYD_VERSION)}"; ver="${ver:-1.7.7}"
  if [ -x /usr/local/bin/ttyd ] && /usr/local/bin/ttyd --version 2>/dev/null | grep -q "$ver"; then
    say "ttyd $ver already installed"; return 0
  fi
  asset="$(ttyd_asset "$ARCH")"
  tmp="$(mktemp)"
  curl -fsSL --retry 3 -o "$tmp" "https://github.com/tsl0922/ttyd/releases/download/$ver/$asset"
  $SUDO install -m 0755 "$tmp" /usr/local/bin/ttyd
  rm -f "$tmp"
  say "ttyd $ver -> /usr/local/bin/ttyd"
}

install_caddy() {
  # the release binary, not the apt package: that one also enables its own caddy.service
  # on port 80, which would take the port --https needs and serve a default page
  local v="$CADDY_VERSION" a tmp
  if [ -x /usr/local/bin/caddy ] && /usr/local/bin/caddy version 2>/dev/null | grep -q "^v$v"; then
    say "Caddy $v already installed"; return 0
  fi
  a="$(caddy_arch "$ARCH")"
  tmp="$(mktemp -d)"
  curl -fsSL --retry 3 -o "$tmp/caddy.tgz" \
    "https://github.com/caddyserver/caddy/releases/download/v$v/caddy_${v}_linux_$a.tar.gz"
  tar -xzf "$tmp/caddy.tgz" -C "$tmp" caddy
  $SUDO install -m 0755 "$tmp/caddy" /usr/local/bin/caddy
  rm -rf "$tmp"
  say "Caddy $v -> /usr/local/bin/caddy"
}

node_major() {  # "" when there is no node
  command -v node >/dev/null 2>&1 || return 0
  node -v 2>/dev/null | sed -n 's/^v\([0-9]*\).*/\1/p' || true
}

install_node() {
  local m; m="$(node_major)"
  if [ -n "$m" ] && [ "$m" -ge "$NODE_MIN" ]; then
    say "Node.js $(node -v) found — kept"; return 0
  fi
  say "installing Node.js $NODE_MAJOR (NodeSource apt repo)"
  $SUDO install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | $SUDO gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
  printf 'deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_%s.x nodistro main\n' \
    "$NODE_MAJOR" > "$DIR/.sessions/nodesource.list.tmp"
  $SUDO install -m 0644 "$DIR/.sessions/nodesource.list.tmp" /etc/apt/sources.list.d/nodesource.list
  rm -f "$DIR/.sessions/nodesource.list.tmp"
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 update -q
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q nodejs
  hash -r
  m="$(node_major)"
  [ -n "$m" ] && [ "$m" -ge "$NODE_MIN" ] || die "Node.js install did not give a node >= $NODE_MIN (got: $(node -v 2>&1))"
}

install_claude() {
  local pin want have prefix npm sudo_npm=""
  npm="$(command -v npm)" || die "npm not found after installing Node.js"
  prefix="$("$npm" prefix -g)"
  # a user-owned Node (nvm, …) installs globals without sudo; a system one needs it
  [ -w "$prefix/lib" ] || [ -w "$prefix" ] || sudo_npm="$SUDO"
  have=""
  if [ -f "$prefix/lib/node_modules/@anthropic-ai/claude-code/package.json" ]; then
    have="$(sed -n 's/^ *"version": *"\([^"]*\)".*/\1/p' \
            "$prefix/lib/node_modules/@anthropic-ai/claude-code/package.json" | head -n 1)"
  fi
  pin="$(dockerfile_arg CLAUDE_CODE_VERSION)"; pin="${pin:-latest}"
  want="${CLAUDE_CODE_VERSION:-}"
  if [ -z "$want" ] && [ -n "$have" ]; then
    say "Claude Code $have already installed — kept (CLAUDE_CODE_VERSION=… to change it)"
  elif [ -n "$have" ] && [ "$want" = "$have" ]; then
    say "Claude Code $have already installed"
  else
    want="${want:-$pin}"
    say "installing Claude Code $want (npm -g)"
    $sudo_npm env PATH="$(dirname "$npm"):$PATH" "$npm" install -g --no-fund --no-audit \
      "@anthropic-ai/claude-code@$want"
  fi
  hash -r
  CLAUDE="$(command -v claude || true)"
  [ -n "$CLAUDE" ] || CLAUDE="$prefix/bin/claude"
  [ -x "$CLAUDE" ] || die "claude not found after npm install (looked in PATH and $prefix/bin)"
  NODE_DIR="$(dirname "$(command -v node)")"
}

get_repo() {
  local s; s="$(self_dir)"
  if [ -n "$s" ]; then
    DIR="$s"; say "using this clone as it is: $DIR"; return 0
  fi
  DIR="${AGENTDECK_DIR:-$HOME/agentdeck}"
  if [ -d "$DIR/.git" ]; then
    say "updating $DIR"
    git -C "$DIR" pull --ff-only -q </dev/null \
      || warn "couldn't fast-forward $DIR (local changes?) — installing the code that is there"
  elif [ -e "$DIR" ]; then
    die "$DIR exists but is not a git clone of AgentDeck. Move it away, or set AGENTDECK_DIR=/another/path"
  else
    say "cloning $REPO_URL -> $DIR"
    git clone -q -b "$BRANCH" "$REPO_URL" "$DIR" </dev/null
    printf 'This folder was cloned by install.sh; `install.sh --uninstall --purge` may delete it.\n' \
      > "$DIR/$MARKER"
  fi
}

prepare_data() {
  mkdir -p "$DIR/.sessions"
  chmod 700 "$DIR/.sessions"
  local state="$DIR/.sessions/tasks-state.json" pf="$DIR/.sessions/.dashpass"
  [ -f "$state" ] || echo '{"title":"Task Tracker","tasks":[]}' > "$state"
  if [ -n "${AGENTDECK_PASSWORD:-}" ] && [ ! -s "$pf" ]; then
    python3 - "$pf" "$AGENTDECK_PASSWORD" <<'PY'
import sys, os, hashlib
p, pw = sys.argv[1], sys.argv[2]
salt = os.urandom(16); h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
os.umask(0o077); open(p, "w").write(f"{salt.hex()}${h.hex()}")
PY
    say "dashboard password seeded from AGENTDECK_PASSWORD"
  fi
  mkdir -p "$DIR/$TMUX_SUBDIR"
  chmod 700 "$DIR/$TMUX_SUBDIR"
  mkdir -p "$(dirname "$CONF_FILE")"
  {
    printf 'AGENTDECK_DIR=%s\n' "$DIR"
    printf 'AGENTDECK_MANAGED=%s\n' "$([ -f "$DIR/$MARKER" ] && echo 1 || echo 0)"
    printf 'AGENTDECK_TMUX_TMPDIR=%s\n' "$DIR/$TMUX_SUBDIR"
    printf 'AGENTDECK_SITE=%s\n' "$SITE"
  } > "$CONF_FILE"
}

install_guards() {
  if [ "${AGENTDECK_GUARDS:-1}" = 0 ]; then
    say "guard hooks: skipped (AGENTDECK_GUARDS=0)"; return 0
  fi
  mkdir -p "$HOME/.claude"
  python3 "$DIR/hooks/install_guards.py" --settings "$HOME/.claude/settings.json" \
    --hooks-dir "$DIR/hooks" --tracker-state "$DIR/.sessions/tasks-state.json" \
    --claude-md "$HOME/.claude/CLAUDE.md" --tracker "$DIR/tasks-dashboard/tracker.py"
  say "guard hooks: on in ~/.claude/settings.json (task board before edits, no silent mid-task stop)"
}

install_services() {
  local u
  AGENTDECK_DIR="$DIR" AGENTDECK_USER="$(id -un)" AGENTDECK_SITE="$SITE"
  AGENTDECK_HOME="$(getent passwd "$AGENTDECK_USER" | cut -d: -f6)"; AGENTDECK_HOME="${AGENTDECK_HOME:-$HOME}"
  AGENTDECK_CLAUDE_BIN="$CLAUDE"
  AGENTDECK_PATH="$AGENTDECK_HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  case ":$AGENTDECK_PATH:" in *":$NODE_DIR:"*) ;; *) AGENTDECK_PATH="$NODE_DIR:$AGENTDECK_PATH" ;; esac
  for u in $UNITS; do write_unit "$u"; done
  if [ "${#CHANGED_UNITS[@]}" -gt 0 ]; then
    say "unit files written: ${CHANGED_UNITS[*]}"
  else
    say "unit files unchanged"
  fi
  $SUDO systemctl daemon-reload
  # shellcheck disable=SC2086
  $SUDO systemctl enable -q $RUN_UNITS
  # restart = pick up new code / settings; KillMode=process keeps running agents alive
  # shellcheck disable=SC2086
  $SUDO systemctl restart $RUN_UNITS
}

wait_healthy() {
  local url code i
  case "$SITE" in :*) url="http://127.0.0.1:$PORT/login" ;; *) url="http://127.0.0.1:$HTTP_PORT/" ;; esac
  for i in $(seq 1 45); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" || true)"
    case "$code" in 200|301|302|308) return 0 ;; esac
    sleep 1
  done
  $SUDO systemctl --no-pager status $RUN_UNITS | tail -n 40 >&2 || true
  die "the dashboard did not answer on $url. Logs: sudo journalctl -u 'agentdeck-*' -n 50"
}

firewall_hint() {
  local p="$1"
  if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
    $SUDO ufw status 2>/dev/null | grep -qE "^$p(/tcp)?[[:space:]]+ALLOW" \
      || say "firewall: ufw is on and port $p is not open — sudo ufw allow $p/tcp"
  fi
}

finish() {
  local url ip
  case "$SITE" in
    :*) ip="$(public_ip 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)"
        url="http://${ip:-<this-server-ip>}:$PORT" ;;
    *) url="https://$SITE" ;;
  esac
  echo
  say "AgentDeck is running:  $url"
  say "  1. Open it — the first visit sets the password."
  say "  2. Then + New terminal, and sign in to Claude once (every terminal reuses the login)."
  case "$SITE" in
    :*) say "  HTTPS instead (no domain needed): $DIR/install.sh --https"; firewall_hint "$PORT" ;;
    *) say "  The certificate is fetched on the first visit (can take a minute)."
       firewall_hint 80; firewall_hint 443 ;;
  esac
  if [ "$TELEGRAM" = 1 ]; then
    say "  Telegram: in the dashboard open Telegram (/telegram), paste your bot token from"
    say "  @BotFather there — the installer never handles the token."
  fi
  echo
  say "Agents run as $(id -un) with full access to this machine (sudo included)."
  say "Status: systemctl status 'agentdeck-*' · logs: journalctl -u agentdeck-status -f"
  say "The agents' tmux server is their own: TMUX_TMPDIR=$DIR/$TMUX_SUBDIR tmux ls"
  say "Update: run the same command again · remove: $DIR/install.sh --uninstall"
}

uninstall() {
  local u dir tdir purge_dir=""
  dir="$(conf_get AGENTDECK_DIR)"
  if [ -z "$dir" ] && [ -f "$SYSTEMD_DIR/agentdeck-status.service" ]; then
    dir="$(sed -n 's/^WorkingDirectory=//p' "$SYSTEMD_DIR/agentdeck-status.service" | head -n 1)"
  fi
  [ "$PURGE" = 1 ] && purge_dir="$(purge_target)"
  step "Stopping and removing the services"
  for u in $UNITS; do
    $SUDO systemctl disable --now -q "$u" 2>/dev/null || true
    if [ -e "$SYSTEMD_DIR/$u" ]; then $SUDO rm -f "$SYSTEMD_DIR/$u"; fi
  done
  $SUDO systemctl daemon-reload || true
  $SUDO systemctl reset-failed 'agentdeck-*' 2>/dev/null || true
  # the agents' own tmux server (KillMode=process kept it through the stop) — it lives in
  # the recorded TMUX_TMPDIR, so no other tmux server of this user is ever touched
  tdir="$(conf_get AGENTDECK_TMUX_TMPDIR)"
  case "$tdir" in
    /*/"$TMUX_SUBDIR")
      if [ -d "$tdir" ]; then
        TMUX_TMPDIR="$tdir" tmux kill-server 2>/dev/null || true
        say "stopped the agents' tmux server ($tdir)"
      fi ;;
  esac
  if [ -n "$dir" ] && [ -f "$dir/hooks/install_guards.py" ] && [ -f "$HOME/.claude/settings.json" ]; then
    python3 "$dir/hooks/install_guards.py" --settings "$HOME/.claude/settings.json" \
      --hooks-dir "$dir/hooks" --remove >/dev/null || true
    say "guard hooks removed from ~/.claude/settings.json"
  fi
  if [ "$PURGE" = 1 ]; then
    step "Deleting AgentDeck's files"
    if [ -n "$purge_dir" ]; then
      rm -rf -- "$purge_dir"; say "deleted $purge_dir"
      rm -f -- "$CONF_FILE"
    fi
    say "kept: ttyd, Caddy, Node.js, Claude Code and your Claude login in ~/.claude"
  else
    say "kept: ${dir:-the AgentDeck folder} with your terminals, task board and password — --uninstall --purge deletes them"
  fi
  say "AgentDeck services removed."
}

main() {
  parse_args "$@"
  step "Checking this machine"
  preflight
  say "OK: $OS on $ARCH, user $(id -un)"
  if [ "$CHECK" = 1 ]; then say "preflight ok — nothing was changed"; return 0; fi
  [ "$UNINSTALL" = 1 ] && { uninstall; return 0; }
  trap 'on_error $LINENO "$BASH_COMMAND"' ERR

  step "System packages (apt)"
  apt_install tmux python3 git curl ca-certificates procps uuid-runtime openssl gnupg iproute2 tar

  step "AgentDeck code"
  local was_piped=0; [ -z "$(self_dir)" ] && was_piped=1
  get_repo
  if [ "$was_piped" = 1 ] && [ -z "${AGENTDECK_REEXEC:-}" ]; then
    # continue with the installer from the code just fetched (always the matching version)
    AGENTDECK_REEXEC=1 exec bash "$DIR/install.sh" "${ARGS[@]}" </dev/null
  fi
  mkdir -p "$DIR/.sessions"

  step "ttyd (terminals in the browser)"
  install_ttyd
  step "Caddy (proxy, login, HTTPS)"
  install_caddy
  step "Node.js"
  install_node
  step "Claude Code"
  install_claude

  step "Data folder + guard hooks"
  prepare_data
  install_guards

  step "systemd services"
  install_services
  wait_healthy
  finish
}

# tests source the functions only (tests/test_install_sh.py, always on a copy)
[ "${AGENTDECK_SOURCE_ONLY:-}" = 1 ] && return 0 2>/dev/null
main "$@" </dev/null
