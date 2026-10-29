# Upgrading from numbered slots (v1.3.0 and earlier)

[← back to the README](../README.md)

Before v1.4, terminals were numbered slots (`launch-claude-N.sh`, `agents.json`). Since
then every terminal is a named entry in the session library. `migrate_library.py` moves
the old slots over:

```bash
python3 migrate_library.py --dry-run     # a table: slot → code → name → what will happen
python3 migrate_library.py --apply       # do it (safe to run twice)
python3 migrate_library.py --apply --swap-dashboard   # also switch web/index.html to the new page
```

Each slot with a real conversation becomes a named terminal (the name comes from
`agents.json`). Each old `launch-claude-N.sh` becomes a small script that opens that
terminal; the original is kept as `launch-claude-N.sh.pre-library`. **Terminals that are
running are left alone** — no restart, no rename; they move over once they are unloaded.
Nothing is deleted. `--apply --rollback-dashboard` puts the old page back.

For every later version, upgrading is the install command again (or `git pull` and
`./install.sh`); with Docker, `git pull && docker compose up -d --build`. What changed:
[CHANGELOG](../CHANGELOG.md).
