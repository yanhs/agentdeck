# AgentDeck — everything in one container: status_server (Python stdlib) + ONE ttyd for
# the session library (/sess/) + the task board + Caddy (login gate + automatic HTTPS).
# `docker compose up` builds this and serves the dashboard, password-protected, ready
# for an internet-facing VPS.
FROM node:22-slim

# system deps: python3 (status_server), tmux (terminals), curl/ca-certs (ttyd download),
# procps (status detection), uuid-runtime (session ids)
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip tmux curl ca-certificates procps uuid-runtime openssl \
    && rm -rf /var/lib/apt/lists/*

# ttyd — static prebuilt binary (turns each tmux terminal into a browser WebSocket),
# picked for the build machine's CPU: x86_64 (amd64) or aarch64 (arm64, e.g. Graviton/Ampere)
ARG TTYD_VERSION=1.7.7
RUN arch="$(uname -m)"; case "$arch" in \
      x86_64|amd64) arch=x86_64 ;; aarch64|arm64) arch=aarch64 ;; \
      *) echo "unsupported CPU architecture for ttyd: $arch" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/ttyd \
      "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${arch}" \
    && chmod +x /usr/local/bin/ttyd

# the Claude Code CLI the agents run — pinned for reproducible builds. Newer one:
#   docker compose build --build-arg CLAUDE_CODE_VERSION=latest   (or an exact version)
ARG CLAUDE_CODE_VERSION=2.1.282
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

# python-telegram-bot — lets the optional Telegram bridge run inside the container
RUN pip3 install --break-system-packages --no-cache-dir "python-telegram-bot==22.6"

# Caddy (login gate + automatic HTTPS) — grab the binary from the official image
COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy

WORKDIR /app
COPY . /app

# IS_SANDBOX lets the agents run with --dangerously-skip-permissions inside the
# container (claude blocks that flag as root otherwise — the container IS the sandbox).
# LANG/LC_ALL give a UTF-8 locale so the tmux client renders Cyrillic + box-drawing
# instead of "?" (C.utf8 already ships in node:22-slim — no locale-gen needed).
# XDG_*: Caddy keeps its certificates / ACME account under $XDG_DATA_HOME/caddy — point it
# at /data, where docker-compose.yml mounts the caddy-data volume, so Let's Encrypt certs
# survive a container re-create (otherwise they land in /root/.local/share and are lost).
# TRACKER_STATE: the task board file, set image-wide so tracker.py in a `docker exec` shell
# writes the same board the agents and the guard hooks use (it lives in the sessions volume).
ENV AGENTDECK_WORKDIR=/work IS_SANDBOX=1 LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    XDG_DATA_HOME=/data XDG_CONFIG_HOME=/data/config \
    TRACKER_STATE=/app/.sessions/tasks-state.json
RUN mkdir -p /work /app/.sessions /data/config

EXPOSE 8765 80 443
ENTRYPOINT ["bash", "/app/docker/entrypoint.sh"]
