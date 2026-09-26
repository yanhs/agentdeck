#!/usr/bin/env bash
# One command to HTTPS for the Docker setup — no domain needed.
#
#   ./https.sh                  use <your-ip>.sslip.io (a free name that resolves to your IP)
#   ./https.sh your-domain.com  use your own domain (its A record should point at this server)
#   ./https.sh --off            back to plain http on :8765 (or AGENTDECK_PORT)
#
# What it does: writes AGENTDECK_SITE=<name> and COMPOSE_FILE (adds docker-compose.https.yml,
# which publishes host ports 80 + 443) into .env — other lines in .env are kept — then runs
# `docker compose up -d` and waits until Caddy has a Let's Encrypt certificate. A plain
# `docker compose up -d` later keeps HTTPS on (compose reads COMPOSE_FILE from .env).
# Running it twice is fine.
#
# Needs host ports 80 and 443 free (no other web server on them) and open in the firewall.
# If they're taken, the fallback is a self-signed certificate on the normal port:
#   AGENTDECK_SITE=https://:8765 docker compose up -d
#
# Env (mostly for testing): AGENTDECK_HTTP_PORT / AGENTDECK_HTTPS_PORT (host ports, default
# 80 / 443), AGENTDECK_PUBLIC_IP (skip the lookup), AGENTDECK_HTTPS_WAIT (seconds, default 90),
# AGENTDECK_CHECK_INSECURE=1 (don't verify the certificate in the readiness check — only for a
# test CA such as Caddy's `tls internal`).
set -uo pipefail
cd "$(dirname "$0")" || exit 1

OVERRIDE=docker-compose.https.yml
HTTP_PORT="${AGENTDECK_HTTP_PORT:-80}"
HTTPS_PORT="${AGENTDECK_HTTPS_PORT:-443}"
WAIT="${AGENTDECK_HTTPS_WAIT:-90}"
POLL="${AGENTDECK_POLL:-3}"
FALLBACK="AGENTDECK_SITE=https://:8765 docker compose up -d"

say()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: ./https.sh [your-domain.com | --off]

  ./https.sh                  HTTPS on <your-ip>.sslip.io — no domain needed
  ./https.sh your-domain.com  HTTPS on your own domain (A record -> this server)
  ./https.sh --off            back to plain http on :8765

Caddy gets a free Let's Encrypt certificate. Host ports 80 and 443 must be free and open.
Fallback without them (self-signed, browser warns once):
  $FALLBACK
EOF
}

# ---- .env helpers (keep every other line as it is) ----------------------------------------
env_get() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n 1; }
env_set() {  # env_set KEY VALUE — replace the key's line(s) with one, or append
  local key=$1 val=$2 tmp
  tmp=$(mktemp) || die "cannot create a temp file"
  if [ -f .env ]; then
    awk -v k="$key" -v v="$val" 'BEGIN{d=0} index($0, k"=")==1 {if(!d){print k"="v; d=1}; next} {print} END{if(!d) print k"="v}' .env > "$tmp"
    cat "$tmp" > .env
  else
    printf '%s=%s\n' "$key" "$val" > .env
    chmod 600 .env
  fi
  rm -f "$tmp"
}
env_unset() {
  local key=$1 tmp
  [ -f .env ] || return 0
  tmp=$(mktemp) || die "cannot create a temp file"
  awk -v k="$key" 'index($0, k"=")!=1' .env > "$tmp"
  cat "$tmp" > .env
  rm -f "$tmp"
}

# compose -f args from a COMPOSE_FILE value (a:b:c)
compose_args() {
  local IFS=: f
  for f in $1; do [ -n "$f" ] && printf -- '-f\n%s\n' "$f"; done
}
compose() {  # compose "<COMPOSE_FILE value or empty>" args...
  local files=$1; shift
  local -a a=()
  [ -n "$files" ] && mapfile -t a < <(compose_args "$files")
  docker compose "${a[@]}" "$@"
}

# ---- public IP ----------------------------------------------------------------------------
is_public_ipv4() {
  local ip=$1 a b c d
  [[ $ip =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  a=${BASH_REMATCH[1]}; b=${BASH_REMATCH[2]}; c=${BASH_REMATCH[3]}; d=${BASH_REMATCH[4]}
  for o in "$a" "$b" "$c" "$d"; do [ "$((10#$o))" -le 255 ] || return 1; done
  a=$((10#$a)); b=$((10#$b))
  [ "$a" -eq 0 ] || [ "$a" -eq 10 ] || [ "$a" -eq 127 ] || [ "$a" -ge 224 ] && return 1
  [ "$a" -eq 172 ] && [ "$b" -ge 16 ] && [ "$b" -le 31 ] && return 1
  [ "$a" -eq 192 ] && [ "$b" -eq 168 ] && return 1
  [ "$a" -eq 169 ] && [ "$b" -eq 254 ] && return 1
  [ "$a" -eq 100 ] && [ "$b" -ge 64 ] && [ "$b" -le 127 ] && return 1
  return 0
}

public_ip() {  # prints the first answer; validation is the caller's job
  local ip url
  if [ -n "${AGENTDECK_PUBLIC_IP:-}" ]; then printf '%s' "$AGENTDECK_PUBLIC_IP"; return 0; fi
  for url in https://api.ipify.org https://ifconfig.me https://icanhazip.com; do
    ip=$(curl -4 -s --max-time 6 "$url" 2>/dev/null | tr -d '[:space:]')
    [ -n "$ip" ] && { printf '%s' "$ip"; return 0; }
  done
  return 1
}

valid_hostname() {
  local h=$1 label
  [ "${#h}" -ge 1 ] && [ "${#h}" -le 253 ] || return 1
  [[ $h =~ ^[0-9.]+$ ]] && return 1          # a bare IP is not a name
  [[ $h == *..* || $h == .* || $h == *. ]] && return 1
  local IFS=.
  for label in $h; do
    [[ $label =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || return 1
  done
  return 0
}

# ---- ports --------------------------------------------------------------------------------
# prints "<port> <process-or-empty>" for each of our host ports someone listens on
busy_ports() {
  command -v ss >/dev/null 2>&1 || { warn "ss not found — skipping the free-port check"; return 0; }
  local lines; lines=$(ss -ltnpH 2>/dev/null || ss -ltnH 2>/dev/null)
  local p
  for p in "$HTTP_PORT" "$HTTPS_PORT"; do
    awk -v p="$p" '{
        n = split($4, x, ":"); if (x[n] != p) next
        proc = ""; if (match($0, /users:\(\("[^"]+"/)) { proc = substr($0, RSTART+9, RLENGTH-10) }
        print p, proc; exit
      }' <<<"$lines"
  done
}

# is host port $1 published by our own agentdeck container (container port $2)?
ours() {
  local out
  out=$(compose "$(env_get COMPOSE_FILE)" port agentdeck "$2" 2>/dev/null) || return 1
  [[ $out == *":$1" ]]
}

check_ports() {
  local port proc msg="" who cport
  while read -r port proc; do
    [ -n "$port" ] || continue
    if [ "$port" = "$HTTP_PORT" ]; then cport=80; else cport=443; fi
    ours "$port" "$cport" && continue
    who=${proc:-another program}
    [ "$proc" = docker-proxy ] && who="another Docker container"
    msg+="  port $port is used by $who"$'\n'
  done < <(busy_ports)
  if [ -n "$msg" ]; then
    printf 'error: HTTPS needs host ports %s and %s free, but:\n%s' "$HTTP_PORT" "$HTTPS_PORT" "$msg" >&2
    cat >&2 <<EOF
(\`sudo ss -ltnp\` shows which program.) Stop it (or move it to other ports) and run
./https.sh again — nothing was changed.
Or use a self-signed certificate on the normal port instead (the browser warns once):
  $FALLBACK
EOF
    exit 1
  fi
}

# ---- main ---------------------------------------------------------------------------------
MODE=on; NAME=""
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --off) MODE=off ;;
  -*.*) NAME=$1 ;;             # looks like a (bad) domain — let the name check explain
  -*) printf 'error: unknown option %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  "") ;;
  *) NAME=$1 ;;
esac
[ $# -le 1 ] || { usage >&2; exit 2; }

command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 \
  || die "docker compose not found — install Docker (with the compose plugin) first"
[ -f docker-compose.yml ] || die "run this from the AgentDeck folder (docker-compose.yml not found)"

if [ "$MODE" = off ]; then
  cf=$(env_get COMPOSE_FILE)
  if [ -n "$cf" ]; then
    new=$(printf '%s' "$cf" | tr ':' '\n' | grep -vx "$OVERRIDE" | paste -sd: -)
    if [ -z "$new" ] || [ "$new" = docker-compose.yml ]; then env_unset COMPOSE_FILE; else env_set COMPOSE_FILE "$new"; fi
  fi
  env_unset AGENTDECK_SITE
  rm -f "$OVERRIDE"
  say "HTTPS off — restarting on plain http..."
  compose "$(env_get COMPOSE_FILE)" up -d || die "docker compose up failed"
  port=${AGENTDECK_PORT:-$(env_get AGENTDECK_PORT)}; port=${port:-8765}
  ip=$(public_ip) && is_public_ipv4 "$ip" || ip="<your-vps-ip>"
  say ""
  say "Done: http://$ip:$port"
  exit 0
fi

IP=""
if ip=$(public_ip); then IP=$ip; fi

if [ -z "$NAME" ]; then
  [ -n "$IP" ] || die "could not find this server's public IP (tried api.ipify.org, ifconfig.me, icanhazip.com). Pass a domain instead: ./https.sh your-domain.com"
  is_public_ipv4 "$IP" || die "this server's IP ($IP) is not a public IPv4 address (behind NAT?), so a free sslip.io certificate can't be issued. Use a domain that points here (./https.sh your-domain.com) or the self-signed fallback: $FALLBACK"
  NAME="${IP//./-}.sslip.io"
else
  NAME=$(printf '%s' "$NAME" | tr '[:upper:]' '[:lower:]')
  NAME=${NAME#http://}; NAME=${NAME#https://}; NAME=${NAME%/}
  valid_hostname "$NAME" || die "'$1' is not a valid domain name (example: agents.example.com). Leave it out to use a free <your-ip>.sslip.io name."
  if [ -n "$IP" ] && is_public_ipv4 "$IP"; then
    if command -v getent >/dev/null 2>&1; then
      addrs=$(getent ahostsv4 "$NAME" 2>/dev/null | awk '{print $1}' | sort -u | paste -sd' ' -)
      if [ -z "$addrs" ]; then
        warn "$NAME does not resolve yet — add an A record pointing to $IP (Let's Encrypt needs it)"
      elif ! grep -qw -- "$IP" <<<"$addrs"; then
        warn "$NAME does not point to this server: it resolves to $addrs, this server is $IP — the certificate will fail until the A record points here"
      fi
    fi
  fi
fi

check_ports

cat > "$OVERRIDE" <<EOF
# Written by ./https.sh — publishes the ports Caddy needs for Let's Encrypt (HTTP challenge on
# 80, the site on 443). ./https.sh --off removes it.
services:
  agentdeck:
    ports:
      - "$HTTP_PORT:80"
      - "$HTTPS_PORT:443"
EOF

cf=$(env_get COMPOSE_FILE)
if [ -z "$cf" ]; then
  env_set COMPOSE_FILE "docker-compose.yml:$OVERRIDE"
elif ! printf '%s' "$cf" | tr ':' '\n' | grep -qx "$OVERRIDE"; then
  env_set COMPOSE_FILE "$cf:$OVERRIDE"
fi
env_set AGENTDECK_SITE "$NAME"

URL="https://$NAME"; [ "$HTTPS_PORT" = 443 ] || URL="$URL:$HTTPS_PORT"
say "Site name: $NAME — starting AgentDeck with HTTPS..."
compose "$(env_get COMPOSE_FILE)" up -d || die "docker compose up failed (settings are in .env; ./https.sh --off undoes them)"

say "Waiting for the certificate (up to ${WAIT}s)..."
k=(); [ "${AGENTDECK_CHECK_INSECURE:-}" = 1 ] && k=(-k)
deadline=$((SECONDS + WAIT))
while :; do
  code=$(curl -s "${k[@]}" -o /dev/null -w '%{http_code}' --max-time 5 \
           --resolve "$NAME:$HTTPS_PORT:127.0.0.1" "https://$NAME:$HTTPS_PORT/login" 2>/dev/null) \
    && [ -n "$code" ] && [ "$code" != 000 ] && break
  if [ "$SECONDS" -ge "$deadline" ]; then
    printf 'error: no valid HTTPS on %s after %ss. Caddy log (last lines):\n' "$URL" "$WAIT" >&2
    compose "$(env_get COMPOSE_FILE)" logs --tail 25 agentdeck 2>&1 | sed 's/^/  /' >&2
    cat >&2 <<EOF
Common causes: ports $HTTP_PORT/$HTTPS_PORT closed in the firewall / cloud security group, or the
name doesn't point to this server yet. Caddy keeps retrying on its own; run ./https.sh again
to re-check, or ./https.sh --off to go back to http.
EOF
    exit 1
  fi
  sleep "$POLL"
done

say ""
say "Done: $URL"
