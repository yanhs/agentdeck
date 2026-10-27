#!/usr/bin/env bash
# Guest sandboxes' network: the docker network "sbx" (bridge br-sbx, 172.31.250.0/24,
# containers can't talk to each other) and the firewall around it. From a sandbox:
#   - to the host: ONLY the gates (Claude :8790, Telegram :8792, sites :8794) — no ssh,
#     no nginx, no databases, no 3x-ui, nothing else on the server;
#   - to private / internal ranges (other docker networks, Tailscale, link-local,
#     the cloud metadata address): dropped;
#   - to the internet: allowed.
# Idempotent — safe to run again any time (sbx-firewall.timer re-applies it).
# Needs root.
set -euo pipefail

NET=sbx
BR=br-sbx
SUBNET=172.31.250.0/24
GW=172.31.250.1
PORTS=8790,8792,8794

if ! docker network inspect "$NET" >/dev/null 2>&1; then
  docker network create --driver bridge --subnet "$SUBNET" --gateway "$GW" \
    -o com.docker.network.bridge.name="$BR" \
    -o com.docker.network.bridge.enable_icc=false "$NET" >/dev/null
  echo "created docker network $NET ($SUBNET, $BR, icc off)"
fi

# --- sandbox -> the host itself -------------------------------------------------
iptables -N SBX-IN 2>/dev/null || true
iptables -F SBX-IN
iptables -A SBX-IN -p tcp -d "$GW" -m multiport --dports "$PORTS" -j ACCEPT
iptables -A SBX-IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A SBX-IN -j DROP
iptables -C INPUT -i "$BR" -j SBX-IN 2>/dev/null || iptables -I INPUT 1 -i "$BR" -j SBX-IN

# --- sandbox -> anything routed through the host ---------------------------------
iptables -N SBX-FWD 2>/dev/null || true
iptables -F SBX-FWD
iptables -A SBX-FWD -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for range in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 \
             127.0.0.0/8 0.0.0.0/8 224.0.0.0/4 240.0.0.0/4; do
  iptables -A SBX-FWD -d "$range" -j DROP
done
iptables -A SBX-FWD -j RETURN
iptables -N DOCKER-USER 2>/dev/null || true
iptables -C DOCKER-USER -i "$BR" -j SBX-FWD 2>/dev/null || iptables -I DOCKER-USER 1 -i "$BR" -j SBX-FWD

echo "sbx firewall applied: host only $GW:{$PORTS}, private ranges dropped, internet open"
