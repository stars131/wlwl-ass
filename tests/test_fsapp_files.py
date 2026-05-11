"""Tests for the file-handling logic in frontends/fsapp.py.

Covers: outgoing path sandbox, upload size pre-check, retry wrapper,
and per-open_id incoming media save.

Run with: pytest tests/test_fsapp_files.py -v
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

pytest.importorskip("lark_oapi")
fsapp = pytest.importorskip("frontends.fsapp")


# ── _path_in_sandbox ─────────────────────────────────────────────────


def test_sandbox_accepts_temp_path(tmp_path):
    inside = os.path.join(fsapp.SANDBOX_ROOT, "temp", "feishu_media", "ok.png")
    ok, reason = fsapp._path_in_sandbox(inside)
    assert ok, reason


def test_sandbox_rejects_outside_project_root():
    outside = os.path.join(os.path.dirname(fsapp.SANDBOX_ROOT), "elsewhere", "file.txt")
    ok, reason = fsapp._path_in_sandbox(outside)
    assert not ok
    assert "项目目录外" in reason


def test_sandbox_rejects_system_secret_path():
    # /etc/passwd or C:\Windows\System32\config\SAM — neither under project root
    target = "/etc/passwd" if os.name != "nt" else r"C:\Windows\System32\config\SAM"
    ok, reason = fsapp._path_in_sandbox(target)
    assert not ok


@pytest.mark.parametrize("rel", [
    ".git/config",
    ".env",
    "memory/global_mem.txt",
    ".wlwl-ass/config.json",
    "temp/launcher_api_configs.json",
    "mykey.py",
    "mykey_local_override.py",
])
def test_sandbox_deny_list(rel):
    path = os.path.join(fsapp.SANDBOX_ROOT, *rel.split("/"))
    ok, reason = fsapp._path_in_sandbox(path)
    assert not ok, f"{rel} should be denied"
    assert "拒绝列表" in reason


def test_sandbox_path_traversal_resolved():
    """`../`-style traversal must be resolved before the check —
    realpath() handles this so the deny-list isn't tricked by `temp/../memory/...`."""
    tricky = os.path.join(fsapp.SANDBOX_ROOT, "temp", "..", "memory", "global_mem.txt")
    ok, reason = fsapp._path_in_sandbox(tricky)
    assert not ok
    assert "拒绝列表" in reason


# ── _check_size ──────────────────────────────────────────────────────


def test_size_accepts_small_file(tmp_path):
    f = tmp_path / "small.bin"
    f.write_bytes(b"x" * 1024)
    ok, reason = fsapp._check_size(str(f), fsapp.MAX_IMAGE_BYTES)
    assert ok


def test_size_rejects_oversized_image(tmp_path):
    f = tmp_path / "big.bin"
    # Cheaper to mock getsize than write 11MB to disk.
    with mock.patch.object(fsapp.os.path, "getsize", return_value=11 * 1024 * 1024):
        ok, reason = fsapp._check_size(str(f), fsapp.MAX_IMAGE_BYTES)
    assert not ok
    assert "MB" in reason and "10" in reason


def test_size_rejects_oversized_file():
    with mock.patch.object(fsapp.os.path, "getsize", return_value=31 * 1024 * 1024):
        ok, reason = fsapp._check_size("any.bin", fsapp.MAX_FILE_BYTES)
    assert not ok
    assert "30" in reason


def test_size_returns_clear_error_on_missing():
    ok, reason = fsapp._check_size("/nonexistent/path/x.bin", fsapp.MAX_FILE_BYTES)
    assert not ok
    assert reason


# ── _upload_with_retry ───────────────────────────────────────────────


def test_retry_returns_first_success():
    uploader = mock.MagicMock(return_value="key123")
    result = fsapp._upload_with_retry(uploader, "/some/path", attempts=2)
    assert result == "key123"
    assert uploader.call_count == 1


def test_retry_succeeds_after_one_failure():
    uploader = mock.MagicMock(side_effect=[None, "key456"])
    with mock.patch.object(fsapp.time, "sleep"):  # don't actually sleep
        result = fsapp._upload_with_retry(uploader, "/some/path", attempts=2)
    assert result == "key456"
    assert uploader.call_count == 2


def test_retry_exhausts_returns_none():
    uploader = mock.MagicMock(return_value=None)
    with mock.patch.object(fsapp.time, "sleep"):
        result = fsapp._upload_with_retry(uploader, "/some/path", attempts=2)
    assert result is None
    assert uploader.call_count == 2


def test_retry_tolerates_exceptions():
    uploader = mock.MagicMock(side_effect=[RuntimeError("boom"), "key789"])
    with mock.patch.object(fsapp.time, "sleep"):
        result = fsapp._upload_with_retry(uploader, "/some/path", attempts=2)
    assert result == "key789"


# ── _safe_save ───────────────────────────────────────────────────────


def test_safe_save_creates_per_user_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fsapp, "MEDIA_DIR", str(tmp_path))
    out = fsapp._safe_save("ou_alice", "report.pdf", b"data")
    assert os.path.exists(out)
    assert "ou_alice" in out
    assert out.endswith("_report.pdf")


def test_safe_save_two_users_dont_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(fsapp, "MEDIA_DIR", str(tmp_path))
    a = fsapp._safe_save("ou_alice", "report.pdf", b"alice-data")
    b = fsapp._safe_save("ou_bob", "report.pdf", b"bob-data")
    assert a != b
    with open(a, "rb") as f: assert f.read() == b"alice-data"
    with open(b, "rb") as f: assert f.read() == b"bob-data"


def test_safe_save_dedupes_within_same_second(tmp_path, monkeypatch):
    """Two saves with the exact same timestamp + name get suffixed."""
    monkeypatch.setattr(fsapp, "MEDIA_DIR", str(tmp_path))
    with mock.patch.object(fsapp.time, "strftime", return_value="20260101_120000"):
        a = fsapp._safe_save("ou_alice", "report.pdf", b"x")
        b = fsapp._safe_save("ou_alice", "report.pdf", b"y")
    assert a != b
    assert os.path.basename(a) == "20260101_120000_report.pdf"
    assert os.path.basename(b).startswith("20260101_120000_1_")


def test_safe_save_sanitizes_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(fsapp, "MEDIA_DIR", str(tmp_path))
    out = fsapp._safe_save("ou_alice", "../../etc/passwd", b"x")
    # No `..` in the saved path component (basename strips dirs; sanitizer
    # also nukes any non-word chars left).
    rel = os.path.relpath(out, str(tmp_path))
    assert ".." not in rel.split(os.sep)


def test_safe_save_unsafe_open_id_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(fsapp, "MEDIA_DIR", str(tmp_path))
    out = fsapp._safe_save("../escape", "x.txt", b"x")
    # The open_id directory name gets sanitized; no traversal.
    assert ".." not in os.path.relpath(out, str(tmp_path)).split(os.sep)
