"""Tests for the clipboard image-paste backend (status_server.save_paste_image)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import status_server as ss  # noqa: E402


def test_paste_ext_maps_common_image_mimes():
    assert ss._paste_ext("image/png") == ".png"
    assert ss._paste_ext("image/jpeg") == ".jpg"
    assert ss._paste_ext("image/gif") == ".gif"
    assert ss._paste_ext("image/webp") == ".webp"
    assert ss._paste_ext("IMAGE/PNG; charset=binary") == ".png"   # case + params tolerated


def test_paste_ext_rejects_non_images():
    assert ss._paste_ext("text/plain") is None
    assert ss._paste_ext("application/json") is None
    assert ss._paste_ext("") is None
    assert ss._paste_ext(None) is None


def test_save_paste_image_writes_file_and_returns_path_url(tmp_path):
    data = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"
    res = ss.save_paste_image(data, "image/png", token="abcd1234", dest_dir=str(tmp_path))
    assert res["path"] == str(tmp_path / "paste_abcd1234.png")
    assert os.path.exists(res["path"])
    assert open(res["path"], "rb").read() == data
    # public URL must be reimake.com (never the deprecated yanhs.stream)
    assert res["url"].endswith("/paste_abcd1234.png")
    assert res["url"].startswith("https://reimake.com/tgimg/")
    assert "yanhs.stream" not in res["url"]


def test_save_paste_image_rejects_non_image(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        ss.save_paste_image(b"hello", "text/plain", token="x", dest_dir=str(tmp_path))


def test_save_paste_image_rejects_empty(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        ss.save_paste_image(b"", "image/png", token="x", dest_dir=str(tmp_path))
