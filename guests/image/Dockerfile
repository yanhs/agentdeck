# Guest sandbox image = the AgentDeck image of the same version, nothing added: its
# entrypoint already runs the services keeper (services_keeper.py, /work/.services) next
# to the dashboard. A separate name keeps the guests' containers apart from your own
# AgentDeck container and lets the guests module add to the image later.
# guests/install.sh builds both:
#   docker build -t agentdeck:<version> .
#   docker build -t agentdeck-guest:<version> --build-arg BASE=agentdeck:<version> guests/image
ARG BASE=agentdeck:latest
FROM ${BASE}
