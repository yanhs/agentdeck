# AgentDeck — everything in one container: status_server (Python stdlib) +
# a ttyd web terminal per agent + nginx to unify it all at one origin.
# `docker compose up` builds this and serves the dashboard on http://localhost:8080.
FROM node:22-slim

# system deps: python3 (status_server), tmux (terminals), nginx (proxy),
# curl/ca-certs (ttyd download), procps (status detection), uuid-runtime (session ids)
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 tmux nginx curl ca-certificates procps uuid-runtime \
    && rm -rf /var/lib/apt/lists/*

# ttyd — static prebuilt binary (turns each tmux terminal into a browser WebSocket)
RUN curl -fsSL -o /usr/local/bin/ttyd \
      https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 \
    && chmod +x /usr/local/bin/ttyd

# the Claude Code CLI the agents run
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY . /app

# our local (no-auth) nginx vhost; drop Debian's default site
RUN cp docker/nginx.conf /etc/nginx/conf.d/agentdeck.conf \
    && rm -f /etc/nginx/sites-enabled/default

ENV AGENTDECK_WORKDIR=/work
RUN mkdir -p /work /app/.sessions

EXPOSE 80
ENTRYPOINT ["bash", "/app/docker/entrypoint.sh"]
