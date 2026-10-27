"""The guests module's tests never read the real guests/.env (a live server keeps its bot
token there) and never inherit the module's settings from the shell: every run starts
from a throwaway settings file that names only a test domain."""
import os
import tempfile

_KEYS = ("GUESTS_DOMAIN", "GUEST_BOT_TOKEN", "OWNER_TG_ID", "SERVER_IP", "CF_TOKEN_FILE",
         "CF_ZONE", "GUESTS_NOTIFY_CMD", "GUEST_IMAGE", "SITEGATE_SERVER_IP")
for _k in _KEYS:
    os.environ.pop(_k, None)

_ENV = os.path.join(tempfile.mkdtemp(prefix="guests-env-"), ".env")
with open(_ENV, "w") as _f:
    _f.write("GUESTS_DOMAIN=example.test\n")
os.environ["GUESTS_ENV_FILE"] = _ENV
