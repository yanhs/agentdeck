"""Settings of the guests module and the owner's alert channel.

Settings live in ``guests/.env`` (KEY=VALUE lines, see ``.env.example``; another file:
GUESTS_ENV_FILE). A key set in the process environment wins over the file. Read on every
call, so a changed .env is picked up without a restart of the command that reads it.

    GUESTS_DOMAIN      dashboards at sb-<guest>.<domain>, files at sbx.<domain>,
                       sites at <site>-<guest>.<domain>
    GUEST_BOT_TOKEN    the shared guest bot (only the host's Telegram turnstile holds it)
    OWNER_TG_ID        your Telegram id: invitations and alerts go there
    SERVER_IP          this server's public IPv4 (DNS records point here); else detected
    CF_TOKEN_FILE      Cloudflare API token file for DNS (default ~/.cf_token)
    CF_ZONE            the Cloudflare zone (default: GUESTS_DOMAIN)
    GUESTS_NOTIFY_CMD  how to tell the owner something: a bash command, the text is "$1"
                       (default: a message from the guest bot to OWNER_TG_ID)
    GUEST_IMAGE        the sandbox image (default agentdeck-guest:<AgentDeck version>)
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

HERE = Path(__file__).resolve().parent
KEYS = ("GUESTS_DOMAIN", "GUEST_BOT_TOKEN", "OWNER_TG_ID", "SERVER_IP", "CF_TOKEN_FILE",
        "CF_ZONE", "GUESTS_NOTIFY_CMD", "GUEST_IMAGE")
# Cloudflare answers "error 1010" to Python-urllib's own User-Agent
USER_AGENT = "AgentDeck-guests/2.0 (+https://github.com/yanhs/agentdeck)"


def env_file() -> Path:
    return Path(os.environ.get("GUESTS_ENV_FILE") or HERE / ".env")


def parse(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def load(path: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The module's settings: the file, then the process environment on top."""
    path = Path(path) if path is not None else env_file()
    environ = os.environ if environ is None else environ
    try:
        settings = parse(path.read_text(encoding="utf-8"))
    except OSError:
        settings = {}
    for key in KEYS:
        if environ.get(key):
            settings[key] = environ[key]
    return settings


def get(key: str, default: Optional[str] = None) -> Optional[str]:
    value = load().get(key)
    return value if value else default


def domain(settings: Optional[Mapping[str, str]] = None) -> str:
    """GUESTS_DOMAIN, lower-case, without a scheme, slashes or outer dots ("" if unset)."""
    raw = (settings if settings is not None else load()).get("GUESTS_DOMAIN") or ""
    raw = raw.strip().lower()
    for scheme in ("https://", "http://"):
        if raw.startswith(scheme):
            raw = raw[len(scheme):]
    return raw.split("/", 1)[0].strip(".")


def _post(url: str, data: Dict, timeout: float) -> Dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def notify_owner(text: str, settings: Optional[Mapping[str, str]] = None,
                 run: Callable = subprocess.run, post: Callable = _post) -> None:
    """Tell the owner: GUESTS_NOTIFY_CMD if set, else the guest bot -> OWNER_TG_ID.
    Raises when it could not (the callers log it; a guest's request never waits on it)."""
    s = load() if settings is None else settings
    cmd = s.get("GUESTS_NOTIFY_CMD")
    if cmd:
        r = run(["bash", "-c", cmd, "_", text], timeout=30, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"GUESTS_NOTIFY_CMD exit {r.returncode}")
        return
    token, owner = s.get("GUEST_BOT_TOKEN"), s.get("OWNER_TG_ID")
    if not (token and owner):
        raise RuntimeError("nowhere to send: set GUEST_BOT_TOKEN + OWNER_TG_ID "
                           "(or GUESTS_NOTIFY_CMD) in guests/.env")
    post(f"https://api.telegram.org/bot{token}/sendMessage",
         {"chat_id": owner, "text": text}, timeout=30)
