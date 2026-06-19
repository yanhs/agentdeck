#!/usr/bin/env python3
"""Read per-agent overrides (model / effort) from agents.json.

Usage:
    python3 _agent_config.py <agent_id> model
    python3 _agent_config.py <agent_id> effort

Prints the stored value on stdout (no trailing whitespace). When the
file/agent/field is missing or the file is malformed, prints a sensible
default and still exits 0 so launch scripts can splice the value in
without elaborate error handling:

    model  → ""      (no value → launch script omits --model, claude
                      falls back to the subscription default)
    effort → "auto"  (matches the historical `/effort auto` we sent
                      after every launch)
"""
from __future__ import annotations

import argparse
import json
import os
import sys


VALID_FIELDS = ("model", "effort")
DEFAULTS = {"model": "", "effort": "auto"}


def read_field(agent_id: str, field: str, agents_file: str) -> str:
    if field not in VALID_FIELDS:
        # Caller passed an unknown field; signal via non-zero exit.
        raise SystemExit(f"unknown field: {field!r} (expected one of {VALID_FIELDS})")

    try:
        with open(agents_file) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return DEFAULTS[field]

    entry = data.get(str(agent_id))
    if not isinstance(entry, dict):
        return DEFAULTS[field]

    value = entry.get(field)
    if isinstance(value, str) and value:
        return value
    return DEFAULTS[field]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("agent_id")
    p.add_argument("field")
    p.add_argument(
        "--agents-file",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents.json"),
    )
    args = p.parse_args(argv)
    sys.stdout.write(read_field(args.agent_id, args.field, args.agents_file))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
