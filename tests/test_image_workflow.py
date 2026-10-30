"""A ready Docker image with every release (owner, 30.10.2026: "the release has only a zip").

.github/workflows/image.yml builds the image of the released tag for amd64 and arm64 and
pushes ghcr.io/yanhs/agentkennel:<version> and :latest; it can also be run by hand for an
existing tag. docker-compose.yml names that image, so `docker compose up -d` downloads it
instead of building; `--build` still builds from the checkout, and the volumes (the data)
stay the same either way.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WF = REPO / ".github" / "workflows" / "image.yml"


def test_a_release_publishes_the_image():
    text = WF.read_text()
    assert "release:" in text and "types: [published]" in text
    assert "workflow_dispatch:" in text                  # an existing tag, by hand
    assert "packages: write" in text
    assert "registry: ghcr.io" in text
    assert "ghcr.io/yanhs/agentkennel:${{ steps.v.outputs.version }}" in text
    assert "ghcr.io/yanhs/agentkennel:latest" in text
    assert "linux/amd64,linux/arm64" in text
    assert "org.opencontainers.image.source=https://github.com/yanhs/agentkennel" in text


def test_it_builds_the_released_tag_not_the_branch():
    text = WF.read_text()
    assert "ref: ${{ github.event.release.tag_name || inputs.tag }}" in text


def test_compose_downloads_the_image_and_can_still_build():
    text = (REPO / "docker-compose.yml").read_text()
    assert "image: ghcr.io/yanhs/agentkennel:${AGENTKENNEL_VERSION:-latest}" in text
    assert "build: ." in text
    for vol in ("agentkennel-claude:/root/.claude", "agentkennel-sessions:/app/.sessions",
                "caddy-data:/data", "agentkennel-work:/work"):
        assert vol in text                                # the data stays where it was


def test_the_readme_says_no_build_is_needed():
    text = (REPO / "README.md").read_text()
    assert "ghcr.io/yanhs/agentkennel" in text
