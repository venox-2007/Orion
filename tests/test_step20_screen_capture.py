"""Unit tests for Step 20: Remote Eyes Desktop Screenshot Capture & Telegram Delivery."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from PIL import Image

from orion.integrations.telegram_bot import create_telegram_app
from orion.memory.database import ConversationMemory
from orion.tools.router import check_deterministic_screen_query, route_and_execute_tool
from orion.tools.screen_capture import ScreenCaptureError, take_desktop_screenshot


def test_take_desktop_screenshot_mocked(monkeypatch):
    """Mock ImageGrab.grab returning a valid image and verify JPEG bytes and magic bytes."""
    dummy_img = Image.new("RGB", (100, 100), color="white")
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda all_screens=True: dummy_img)

    jpeg_bytes = take_desktop_screenshot()
    assert isinstance(jpeg_bytes, bytes)
    assert len(jpeg_bytes) > 0
    # JPEG magic bytes header: \xff\xd8
    assert jpeg_bytes[:2] == b"\xff\xd8"


def test_take_desktop_screenshot_failure(monkeypatch):
    """When ImageGrab.grab fails (e.g. headless/locked session), raises ScreenCaptureError."""
    def _mock_fail(all_screens=True):
        raise OSError("screen grab failed")

    monkeypatch.setattr("PIL.ImageGrab.grab", _mock_fail)

    with pytest.raises(ScreenCaptureError, match="Failed to capture screen"):
        take_desktop_screenshot()


def test_router_screenshot_queries(monkeypatch):
    """Test queries like 'take a screenshot', 'show my screen', 'screenshot', 'what's on my desktop'."""
    monkeypatch.setattr("orion.tools.screen_capture.take_desktop_screenshot", lambda: b"\xff\xd8_fake_jpeg_bytes")

    test_queries = [
        "take a screenshot",
        "show my screen",
        "screenshot",
        "what's on my desktop",
        "what is on my screen",
        "see my screen",
        "send a screenshot",
        "capture screen",
    ]

    for q in test_queries:
        res = check_deterministic_screen_query(q)
        assert res is not None, f"Query '{q}' failed to match screenshot intent."
        assert res.get("handled") is True
        assert res.get("type") == "photo"
        assert res.get("photo_bytes") == b"\xff\xd8_fake_jpeg_bytes"
        assert "📸 Desktop snapshot" in res.get("caption", "")


def test_router_unrelated_queries_pass_through():
    """Assert unrelated queries return None from check_deterministic_screen_query."""
    unrelated = [
        "how much ram am i using",
        "what is a gpu",
        "add task check screen",
        "open notepad",
        "remind me to call mom",
        "hello",
    ]

    for q in unrelated:
        assert check_deterministic_screen_query(q) is None


@pytest.mark.asyncio
async def test_telegram_photo_dispatch(tmp_path, monkeypatch):
    """Verify Telegram bot handles photo response and calls reply_photo."""
    db_file = str(tmp_path / "test_photo.db")
    mem = ConversationMemory(db_path=db_file)
    owner_id = 9999
    token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    monkeypatch.setattr("orion.tools.screen_capture.take_desktop_screenshot", lambda: b"\xff\xd8_fake_jpeg")

    app = create_telegram_app(token=token, owner_id=owner_id, memory=mem)
    message_handler_func = app.handlers[0][-1].callback

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    update_owner = MagicMock()
    update_owner.effective_chat.type = "private"
    update_owner.effective_chat.id = owner_id
    update_owner.effective_user.id = owner_id
    update_owner.effective_message.reply_photo = AsyncMock()
    update_owner.effective_message.reply_text = AsyncMock()
    update_owner.effective_message.text = "take a screenshot"

    await message_handler_func(update_owner, context)
    update_owner.effective_message.reply_photo.assert_called_once()
    _, kwargs = update_owner.effective_message.reply_photo.call_args
    assert kwargs.get("photo") == b"\xff\xd8_fake_jpeg"
    assert "📸 Desktop snapshot" in kwargs.get("caption", "")
    # Should not send regular text message
    update_owner.effective_message.reply_text.assert_not_called()
