# Guest sandbox image = the AgentDeck sandbox image + the services keeper.
# Build:  docker build -t agentdeck-guest:v1.9.0-svc --build-arg BASE=agentdeck-guest:v1.9.0 image/
ARG BASE=agentdeck-guest:v1.9.0
FROM ${BASE}

# keeper.py runs /work/.services/<name>.sh — programs that must outlive a terminal
# (Tailscale, a site's server, a bot); HTTP :9876 serves their state to the host's bot.
COPY keeper.py /opt/sbx/keeper.py
COPY entrypoint.sh /opt/sbx/entrypoint.sh
ENTRYPOINT ["bash", "/opt/sbx/entrypoint.sh"]
