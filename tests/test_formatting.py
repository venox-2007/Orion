"""Focused tests for Telegram markdown-to-HTML conversion and safe delivery."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram.constants import ParseMode

from orion.integrations.formatter import (
    clean_markdown_to_plain,
    markdown_to_telegram_html,
    split_html_message,
)
from orion.integrations.telegram_bot import send_formatted_message


def test_bold_and_italic() -> None:
    """Test bold and italic markdown conversion to safe Telegram HTML."""
    assert markdown_to_telegram_html("**bold text**") == "<b>bold text</b>"
    assert markdown_to_telegram_html("__bold text__") == "<b>bold text</b>"
    assert markdown_to_telegram_html("*italic text*") == "<i>italic text</i>"
    assert markdown_to_telegram_html("_italic text_") == "<i>italic text</i>"
    assert (
        markdown_to_telegram_html("***bold and italic***")
        == "<b><i>bold and italic</i></b>"
    )


def test_code_blocks_and_inline_code() -> None:
    """Test inline code and fenced code blocks with proper escaping."""
    inline = "Here is `x < y && z > 0` inline code."
    expected_inline = "Here is <code>x &lt; y &amp;&amp; z &gt; 0</code> inline code."
    assert markdown_to_telegram_html(inline) == expected_inline

    fenced = "```python\ndef test():\n    return a < b\n```"
    expected_fenced = (
        '<pre><code class="language-python">def test():\n    return a &lt; b</code></pre>'
    )
    assert markdown_to_telegram_html(fenced) == expected_fenced


def test_links_and_sanitization() -> None:
    """Test link conversion and sanitization of unsafe URLs."""
    valid_link = "[Orion Repo](https://github.com/venox-2007/Orion)"
    expected_valid = '<a href="https://github.com/venox-2007/Orion">Orion Repo</a>'
    assert markdown_to_telegram_html(valid_link) == expected_valid

    unsafe_link = "[Dangerous](javascript:alert(1))"
    converted_unsafe = markdown_to_telegram_html(unsafe_link)
    assert "<a href=\"javascript:" not in converted_unsafe
    assert "Dangerous (javascript:alert(1))" in converted_unsafe


def test_headings_and_lists() -> None:
    """Test headings rendered as bold and lists formatted cleanly."""
    heading = "# Main Title\n## Sub Title"
    converted_heading = markdown_to_telegram_html(heading)
    assert "<b>Main Title</b>" in converted_heading
    assert "<b>Sub Title</b>" in converted_heading

    bullet_list = "- First item\n* Second item\n+ Third item"
    converted_list = markdown_to_telegram_html(bullet_list)
    assert "• First item" in converted_list
    assert "• Second item" in converted_list
    assert "• Third item" in converted_list


def test_html_escaping_untrusted_input() -> None:
    """Ensure arbitrary HTML tags and characters are escaped."""
    raw = 'Comparison: 5 < 10 & 20 > 15 with <script>alert("xss")</script>'
    converted = markdown_to_telegram_html(raw)
    assert "&lt;script&gt;" in converted
    assert "<script>" not in converted
    assert "5 &lt; 10 &amp; 20 &gt; 15" in converted


def test_long_message_splitting_balanced_tags() -> None:
    """Ensure long messages are split into chunks <= 4000 chars without breaking tags."""
    inner_text = "Word " * 1200  # ~6000 characters
    long_markdown = f"<b>{inner_text}</b>"
    chunks = split_html_message(long_markdown, max_chunk=1000)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 1000
        # Each chunk must have balanced <b> tags
        assert chunk.count("<b>") == chunk.count("</b>")


@pytest.mark.asyncio
async def test_send_formatted_message_success() -> None:
    """Test send_formatted_message uses ParseMode.HTML."""
    mock_msg = MagicMock()
    mock_msg.reply_text = AsyncMock()

    await send_formatted_message(mock_msg, "Hello **world**")
    mock_msg.reply_text.assert_called_once_with(
        "Hello <b>world</b>",
        parse_mode=ParseMode.HTML,
    )


@pytest.mark.asyncio
async def test_plain_text_fallback_on_parse_error() -> None:
    """Test fallback to clean plain text if Telegram HTML parsing fails."""
    mock_msg = MagicMock()
    # First attempt with HTML fails, second attempt with plain text succeeds
    mock_msg.reply_text = AsyncMock(
        side_effect=[Exception("Can't parse entities in message"), None]
    )

    await send_formatted_message(mock_msg, "Here is **bold** and `code`.")
    assert mock_msg.reply_text.call_count == 2
    # Verify fallback call sends cleaned plain text without parse_mode
    fallback_call = mock_msg.reply_text.call_args_list[1]
    fallback_text = fallback_call[0][0]
    assert fallback_text == "Here is bold and code."
    assert "parse_mode" not in fallback_call[1]
