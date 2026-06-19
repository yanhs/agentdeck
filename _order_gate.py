#!/usr/bin/env python3
"""Check whether an agent id is enabled in the dashboard list (`_order`).

Usage:
    python3 _order_gate.py <agent_id> [--agents-file PATH]

Exit codes:
    0  enabled — agent is in `_order` of agents.json
       (or agents.json is missing / has no `_order` — fail-open so a fresh
       install can still launch)
    1  disabled — `_order` exists but does not contain this id; the launch
       script should print a "not enabled" message and exit.

Kept tiny and dependency-free so launch-claude-*.sh can shell out cheaply.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def is_enabled(agent_id: str, agents_file: str) -> bool:
    """Return True if the agent is enabled, or if we should fail-open."""
    try:
        with open(agents_file) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # No state file yet → behave as before (allow launch).
        return True

    order = data.get("_order")
    if not isinstance(order, list):
        return True  # no _order recorded → allow launch (back-compat)

    return str(agent_id) in [str(x) for x in order]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("agent_id")
    p.add_argument(
        "--agents-file",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents.json"),
    )
    args = p.parse_args(argv)
    return 0 if is_enabled(args.agent_id, args.agents_file) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
