"""Unit tests for Step 22 & 22.1 — Remote File Bridge (Fetch & Drop Protocol), Absolute Volume Control, and Size Guards."""

from pathlib import Path
import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


def test_set_volume_percentage_fallback():
    """Verify set_volume_percentage(50) falls back to 50 down steps and 25 up steps."""
    from orion.tools.workstation import (
        VK_VOLUME_DOWN,
        VK_VOLUME_UP,
        set_volume_percentage,
    )

    with patch("ctypes.windll.user32.keybd_event") as mock_keybd:
        assert set_volume_percentage(50) is True

        down_calls = [c for c in mock_keybd.call_args_list if c == ((VK_VOLUME_DOWN, 0, 0, 0),)]
        up_calls = [c for c in mock_keybd.call_args_list if c == ((VK_VOLUME_UP, 0, 0, 0),)]

        assert len(down_calls) == 50
        assert len(up_calls) == 25


def test_file_bridge_allowed_paths():
    """Verify allowlist validation, path traversal rejections, and sensitive file blocks."""
    from orion.tools.file_bridge import (
        find_approved_file,
        is_path_allowed,
        validate_approved_path,
    )

    downloads_dir = Path.home() / "Downloads"
    doc_path = downloads_dir / "report.pdf"

    # Files inside Downloads or Documents pass validation
    assert is_path_allowed(doc_path) is True
    assert validate_approved_path(doc_path) == doc_path.resolve()

    # Path traversal attempts return False / None
    traversal_path = "../../Windows/System32/calc.exe"
    assert is_path_allowed(traversal_path) is False
    assert validate_approved_path(traversal_path) is None
    assert find_approved_file(traversal_path) is None

    # Sensitive files return False / None
    sensitive_path = downloads_dir / ".env"
    assert is_path_allowed(sensitive_path) is False
    assert validate_approved_path(sensitive_path) is None
    assert find_approved_file(".env") is None


def test_get_latest_file(tmp_path):
    """Verify get_latest_file returns the newest file matching the extension."""
    from orion.tools.file_bridge import get_latest_file

    f1 = tmp_path / "old.pdf"
    f2 = tmp_path / "new.pdf"
    f3 = tmp_path / "newest.txt"

    f1.write_text("old pdf")
    f2.write_text("new pdf")
    f3.write_text("newest text")

    os.utime(f1, (1000, 1000))
    os.utime(f2, (2000, 2000))
    os.utime(f3, (3000, 3000))

    # Pick newest matching pdf
    latest_pdf = get_latest_file(directory_alias=tmp_path, extension="pdf")
    assert latest_pdf == f2.resolve()

    # Pick newest overall when extension is None
    latest_any = get_latest_file(directory_alias=tmp_path)
    assert latest_any == f3.resolve()


def test_get_latest_file_skips_large_files(tmp_path):
    """Verify get_latest_file skips files exceeding max_bytes (50MB guard)."""
    from orion.tools.file_bridge import get_latest_file

    large_file = tmp_path / "giant_movie.mkv"
    small_file = tmp_path / "document.pdf"

    large_file.write_bytes(b"x")
    small_file.write_bytes(b"y")

    os.utime(small_file, (1000, 1000))
    os.utime(large_file, (2000, 2000))

    orig_stat = Path.stat

    def mock_stat(self, *args, **kwargs):
        st = orig_stat(self, *args, **kwargs)
        if self.name == "giant_movie.mkv":
            class MockStat:
                st_mode = st.st_mode
                st_mtime = 2000
                st_size = 55 * 1024 * 1024
            return MockStat()
        return st

    with patch.object(Path, "stat", mock_stat):
        res = get_latest_file(directory_alias=tmp_path)
        assert res == small_file.resolve()


def test_find_approved_file_stem_and_substring(tmp_path):
    """Verify find_approved_file matches stem/prefix/substring when exact name differs."""
    from orion.tools.file_bridge import find_approved_file

    aadhar_file = tmp_path / "EAadhar_9876543210.pdf"
    aadhar_file.write_text("dummy aadhar content")

    # 1. Match by stem without extension
    res1 = find_approved_file("EAadhar", directory_alias=tmp_path)
    assert res1 == aadhar_file.resolve()

    # 2. Match by query with extension
    res2 = find_approved_file("EAadhar.pdf", directory_alias=tmp_path)
    assert res2 == aadhar_file.resolve()

    # 3. Match by substring
    res3 = find_approved_file("9876543210", directory_alias=tmp_path)
    assert res3 == aadhar_file.resolve()


def test_save_incoming_file_collision(tmp_path):
    """Verify saving file avoids collision by appending suffix without overwriting."""
    from orion.tools.file_bridge import save_incoming_file

    saved1 = save_incoming_file(b"data1", "test.txt", target_dir=tmp_path)
    assert saved1.name == "test.txt"
    assert saved1.read_bytes() == b"data1"

    saved2 = save_incoming_file(b"data2", "test.txt", target_dir=tmp_path)
    assert saved2.name == "test_1.txt"
    assert saved2.read_bytes() == b"data2"
    assert saved1.read_bytes() == b"data1"


@pytest.mark.asyncio
async def test_router_volume_percentage():
    """Verify absolute volume percentage queries route deterministically."""
    from orion.tools.router import check_deterministic_workstation_query, route_and_execute_tool

    cases = [
        ("volume to 100", 100, "🔊 Volume set to 100%."),
        ("set volume to 40%", 40, "🔊 Volume set to 40%."),
        ("volume 80", 80, "🔊 Volume set to 80%."),
    ]

    with patch("ctypes.windll.user32.keybd_event"):
        for query, expected_level, expected_reply in cases:
            ctx: dict = {}
            res = check_deterministic_workstation_query(query, context=ctx)
            assert res is not None, f"Query '{query}' returned None"
            assert res.get("handled") is True
            assert res.get("reply") == expected_reply
            assert ctx.get("last_action", {}).get("level") == expected_level

            router_res = await route_and_execute_tool(query, context={})
            assert router_res is not None
            assert router_res.get("handled") is True
            assert router_res.get("reply") == expected_reply


@pytest.mark.asyncio
async def test_router_file_fetch(tmp_path):
    """Verify file fetch requests route deterministically to document replies."""
    from orion.tools.router import check_deterministic_file_fetch_query, route_and_execute_tool

    dummy_pdf = tmp_path / "report.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 dummy content")

    with patch("orion.tools.file_bridge.get_latest_file", return_value=dummy_pdf), \
         patch("orion.tools.file_bridge.find_approved_file", return_value=dummy_pdf), \
         patch("orion.tools.file_bridge.is_path_allowed", return_value=True):

        # 1. "send me the latest pdf from downloads"
        ctx1: dict = {}
        res1 = check_deterministic_file_fetch_query("send me the latest pdf from downloads", context=ctx1)
        assert res1 is not None
        assert res1.get("handled") is True
        assert res1.get("type") == "document"
        assert res1.get("file_path") == str(dummy_pdf)
        assert res1.get("caption") == f"📄 {dummy_pdf.name}"
        assert ctx1.get("last_action", {}).get("type") == "file_fetch"

        # 2. "fetch report.pdf"
        ctx2: dict = {}
        res2 = check_deterministic_file_fetch_query("fetch report.pdf", context=ctx2)
        assert res2 is not None
        assert res2.get("handled") is True
        assert res2.get("type") == "document"
        assert res2.get("file_path") == str(dummy_pdf)

        # 3. Via router
        router_res = await route_and_execute_tool("fetch report.pdf", context={})
        assert router_res is not None
        assert router_res.get("handled") is True
        assert router_res.get("type") == "document"
        assert router_res.get("file_path") == str(dummy_pdf)


@pytest.mark.asyncio
async def test_router_file_fetch_no_extension(tmp_path):
    """Verify router handles file fetch queries without extension using stem matching."""
    from orion.tools.router import check_deterministic_file_fetch_query, route_and_execute_tool

    doc = tmp_path / "EAadhar_12345.pdf"
    doc.write_bytes(b"pdf data")

    with patch("orion.tools.file_bridge.find_approved_file", return_value=doc), \
         patch("orion.tools.file_bridge.is_path_allowed", return_value=True):

        # Query without extension
        res = check_deterministic_file_fetch_query("fetch EAadhar", context={})
        assert res is not None
        assert res.get("handled") is True
        assert res.get("type") == "document"
        assert res.get("file_path") == str(doc)

        router_res = await route_and_execute_tool("send the EAadhar from downloads", context={})
        assert router_res is not None
        assert router_res.get("handled") is True
        assert router_res.get("type") == "document"


@pytest.mark.asyncio
async def test_router_file_too_large_error(tmp_path):
    """Verify router returns a clear error message when file exceeds 48MB."""
    from orion.tools.router import check_deterministic_file_fetch_query

    big_doc = tmp_path / "huge_archive.zip"
    big_doc.write_bytes(b"zip")

    class MockStat:
        st_size = 52 * 1024 * 1024  # 52 MB

    with patch("orion.tools.file_bridge.find_approved_file", return_value=big_doc), \
         patch("orion.tools.file_bridge.is_path_allowed", return_value=True), \
         patch.object(Path, "stat", return_value=MockStat()):

        res = check_deterministic_file_fetch_query("fetch huge_archive.zip", context={})
        assert res is not None
        assert res.get("handled") is True
        assert "is too large to send via Telegram" in res["reply"]
        assert "52.0 MB" in res["reply"]


def test_telegram_photo_handler_registered():
    """Verify photo handler is registered on filters.PHOTO in telegram bot app."""
    from orion.integrations.telegram_bot import create_telegram_app

    app = create_telegram_app(token="123456:dummy_test_token", owner_id=12345)
    handlers = [h for group in app.handlers.values() for h in group]
    photo_handlers = [h for h in handlers if hasattr(h, "filters") and "PHOTO" in str(h.filters)]
    assert len(photo_handlers) >= 1
