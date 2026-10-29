"""AgentKennel's settings from the environment: AGENTKENNEL_<NAME>, then AGENTDECK_<NAME>.

The project was called AgentDeck until v2.0.5, and its variables AGENTDECK_*. Servers and
sandboxes set up earlier still set the old names (systemd drop-ins, `docker run -e`, a
`.env`), so every read accepts both: the new name first, the old one as the fallback. An
empty value counts as unset (docker-compose passes `""` for a variable nobody set).

Where AgentKennel itself hands a variable to a child process (each terminal's
AGENTKENNEL_SESSION, a guest container's settings), it sets both names: hooks and the
owner's own tools may still read the old one.

    envcompat.get("WORKDIR", "/work")   # AGENTKENNEL_WORKDIR, else AGENTDECK_WORKDIR, else /work
    envcompat.both("SESSION", sid)      # {"AGENTKENNEL_SESSION": sid, "AGENTDECK_SESSION": sid}

Shell scripts use the same rule inline: ${AGENTKENNEL_X:-${AGENTDECK_X:-default}}.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

NEW = "AGENTKENNEL_"
OLD = "AGENTDECK_"   # compat: the name before v2.1.0, still set by earlier installs


def _suffix(name: str) -> str:
    for prefix in (NEW, OLD):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def names(name: str) -> tuple:
    """(new, old) full names for NAME (a suffix like "SITE", or either full name)."""
    s = _suffix(name)
    return NEW + s, OLD + s


def get(name: str, default: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The new name's value if set and non-empty, else the old name's, else default."""
    env = os.environ if env is None else env
    for key in names(name):
        value = env.get(key)
        if value:
            return value
    return default


def both(name: str, value: str) -> dict:
    """{new: value, old: value} — for an environment AgentKennel hands to a child."""
    new, old = names(name)
    return {new: value, old: value}
