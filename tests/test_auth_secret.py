"""TDD: the cookie-signing secret must be configurable so it can live in the
persistent volume — not hardcoded to <module dir>/.agents_auth_secret.

Bug: AUTH_SECRET_FILE was fixed at <module dir>/.agents_auth_secret. In Docker that is
/app/.agents_auth_secret — an ephemeral image-layer path: regenerated on every container
recreate (logs everyone out) and, if present at build time, baked into the image (a shared
signing key). Fix: honor AGENTDECK_AUTH_SECRET so the entrypoint can point it at the
/app/.sessions volume (per-deploy, persistent), while the default stays next to the module
so the existing host service is unaffected.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

TERMINAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TERMINAL_DIR))


def _load():
    sys.modules.pop("status_server", None)
    return importlib.import_module("status_server")


def test_auth_secret_file_honors_env(tmp_path, monkeypatch):
    sp = tmp_path / "vol" / ".agents_auth_secret"
    sp.parent.mkdir()
    monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(sp))
    mod = _load()
    assert mod.AUTH_SECRET_FILE == str(sp)
    assert sp.exists(), "secret should be generated at the configured path on first load"


def test_auth_secret_persists_across_reload(tmp_path, monkeypatch):
    sp = tmp_path / ".agents_auth_secret"
    monkeypatch.setenv("AGENTDECK_AUTH_SECRET", str(sp))
    s1 = _load().AUTH_SECRET
    s2 = _load().AUTH_SECRET            # same file → same secret (read, not regenerated)
    assert s1 == s2


def test_auth_secret_defaults_next_to_module(monkeypatch):
    monkeypatch.delenv("AGENTDECK_AUTH_SECRET", raising=False)
    mod = _load()
    assert mod.AUTH_SECRET_FILE.endswith(".agents_auth_secret")
    assert str(TERMINAL_DIR) in mod.AUTH_SECRET_FILE   # host behaviour unchanged
