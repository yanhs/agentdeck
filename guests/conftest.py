"""The guests module's tests never read the real guests/.env (a live server keeps its bot
token there) and never inherit the module's settings from the shell: every run starts
from a throwaway settings file that names only a test domain and the Russian texts (the
module's tests were written against them; tests/test_texts.py covers English, the
default)."""
import os
import tempfile

_KEYS = ("GUESTS_DOMAIN", "GUEST_BOT_TOKEN", "OWNER_TG_ID", "SERVER_IP", "CF_TOKEN_FILE",
         "CF_ZONE", "GUESTS_NOTIFY_CMD", "GUEST_IMAGE", "GUESTS_LANG", "SITEGATE_SERVER_IP")
for _k in _KEYS:
    os.environ.pop(_k, None)
# the live terminal's own settings, under either name — but not the throwaway pane
# launcher tests/conftest.py set a moment ago (a run of both folders loads it first)
for _k in [k for k in os.environ if k.startswith(("AGENTKENNEL_", "AGENTDECK_"))
           and k != "AGENTKENNEL_LAUNCHER"]:
    del os.environ[_k]

_ENV = os.path.join(tempfile.mkdtemp(prefix="guests-env-"), ".env")
with open(_ENV, "w") as _f:
    _f.write("GUESTS_DOMAIN=example.test\nGUESTS_LANG=ru\n")
os.environ["GUESTS_ENV_FILE"] = _ENV
# never the owner's real bot ("Your bot": <AgentKennel>/.sessions/telegram.json): alerts in
# tests must have nowhere real to go
os.environ["AGENTKENNEL_TG_CONF"] = os.path.join(os.path.dirname(_ENV), "no-telegram.json")
