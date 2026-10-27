# AgentDeck guests

An optional module: other people work on your server, each in a sandbox of their own, on
your Claude subscription with a daily limit, through one shared Telegram bot. Off until
you set it up.

- **A sandbox per person.** Each guest gets their own AgentDeck in a Docker container
  (image `agentdeck-guest:<version>`): own terminals, files and services, a dashboard at
  `https://sb-<name>.<domain>` with its own password. Guests can't see each other or your
  server: the `sbx` network lets a sandbox reach the internet and the three gates below,
  nothing else on the host or in private ranges.
- **Your subscription, a daily limit.** The Claude turnstile holds your login; a sandbox
  only has a personal pass. The turnstile counts every guest's usage and stops them at
  the daily limit (default 20M weighted tokens; it resets at midnight, Los Angeles time),
  and tells you if a guest burns more than 1% of your week in an hour.
- **One bot for everyone.** The Telegram turnstile holds the real bot token and gives each
  sandbox a Bot API of its own: a guest's agent writes only to that guest's chat.
  Someone new writes `/start` to the bot; you get "… wants access. Let them in?" with
  buttons; on "yes" the sandbox is made and the person gets the address and password.
- **Files and sites.** A guest's agent hands out files as links (`https://sbx.<domain>/f/…`)
  and can publish a site from the sandbox as `https://<site>-<guest>.<domain>` (at most 3;
  you are told about each).

## Set up

You need AgentDeck installed from a checkout (this folder lives inside it), Docker that
your user may use, systemd, and — for web addresses — nginx and certbot.

1. **Settings.** `cp guests/.env.example guests/.env` (the installer does it on the first
   run) and fill in:
   - `GUESTS_DOMAIN` — guests' addresses live under it;
   - `GUEST_BOT_TOKEN` — a new bot from @BotFather, used only for guests;
   - `OWNER_TG_ID` — your Telegram id (a number);
   - DNS: a Cloudflare API token in `~/.cf_token` (names are created per guest), or a
     wildcard A record `*.<domain>` pointing at this server;
   - optional: `SERVER_IP`, `CF_TOKEN_FILE`, `CF_ZONE`, `GUESTS_NOTIFY_CMD`, `GUEST_IMAGE`.

   The Claude turnstile uses your login from `~/.claude/oauth.env`
   (`CLAUDE_CODE_OAUTH_TOKEN=…`, from `claude setup-token`).
2. **Install.** `guests/install.sh` (`--check` only checks, `--yes` asks nothing). It sets up
   the sandbox network and firewall, builds the guest image from this checkout and starts
   the services `agentdeck-guest-turnstile`, `agentdeck-guest-tgrelay` and
   `agentdeck-guest-sitegate`. Running it again upgrades.
3. **Check.** `python3 guests/guestctl.py doctor` — Docker, bot, owner, domain, services.
4. **Shared files address (once).** `python3 guests/guestctl.py files-web`.

## Commands

```
python3 guests/guestctl.py add <name> --tg <telegram id> [--limit N]   new sandbox
python3 guests/guestctl.py web <name>              https://sb-<name>.<domain> (DNS, certificate)
python3 guests/guestctl.py list [--json]           guests, state, today's usage and limit
python3 guests/guestctl.py limit <name> <tokens>   change the daily limit
python3 guests/guestctl.py stop|start <name>       pause / resume (the pass stops working too)
python3 guests/guestctl.py recreate <name>         new container on the current image, same data
python3 guests/guestctl.py remove <name>           container, pass and address go; volumes stay
python3 guests/guestctl.py check <name>            the network walls, seen from inside
python3 guests/guestctl.py doctor [--json]         is everything set up?
python3 guests/turnstile/turnstile.py unlimited <name> --hours N   lift the limit for a while
python3 guests/sitegate/sitegate.py list | remove <host>           guests' published sites
```

`list --json` and `doctor --json` print one JSON object for scripts and the dashboard.

## Tests

`python3 -m pytest -q guests` — nothing real is touched (Docker, systemd, DNS and Telegram
are fakes). End-to-end on a throwaway guest (real container, real DNS):
`guests/scripts/e2e_services.sh` and `python3 guests/scripts/e2e_invite.py`.
