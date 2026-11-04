"""Markdown to Telegram-compliant HTML formatter and message splitter."""

import html
import re
from typing import List

# Telegram allowed tags:
# <b>, <strong>, <i>, <em>, <u>, <s>, <strike>, <del>, <span class="tg-spoiler">,
# <a href="...">, <code>, <pre>, <blockquote>
SAFE_TAG_REGEX = re.compile(r"<(/)?([a-zA-Z0-9]+)(?:\s+[^>]*)?>")
ALLOWED_SCHEMES = ("http://", "https://", "tg://")


def sanitize_url(url: str) -> str | None:
    """Validate and sanitize URL to only allow safe schemes and avoid quote escape."""
    clean_url = url.strip()
    if any(clean_url.lower().startswith(scheme) for scheme in ALLOWED_SCHEMES):
        # Escape quotes or angle brackets in URL
        return html.escape(clean_url, quote=True)
    return None


def clean_markdown_to_plain(text: str) -> str:
    """Clean markdown and HTML formatting markers for fallback plain-text sending."""
    # First remove HTML tags if any
    cleaned = re.sub(r"<[^>]+>", "", text)
    # Unescape HTML entities
    cleaned = html.unescape(cleaned)
    # Remove code fence markers
    cleaned = re.sub(r"```[a-zA-Z0-9_\-]*\n?", "", cleaned)
    # Remove inline code backticks
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    # Remove bold/italic asterisks and underscores
    cleaned = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", cleaned)
    cleaned = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", cleaned)
    # Remove heading markers
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)
    # Convert markdown links [title](url) -> title (url)
    cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", cleaned)
    return cleaned.strip()


def markdown_to_telegram_html(text: str) -> str:
    """Convert Markdown to Telegram-supported HTML while escaping untrusted input."""
    if not text:
        return ""

    # 1. Protect multiline code blocks first
    code_blocks: list[str] = []

    def replace_code_block(match: re.Match) -> str:
        lang = match.group(1) or ""
        code = match.group(2)
        idx = len(code_blocks)
        escaped_code = html.escape(code.strip("\r\n"))
        if lang:
            safe_lang = html.escape(lang.strip(), quote=True)
            block = f'<pre><code class="language-{safe_lang}">{escaped_code}</code></pre>'
        else:
            block = f"<pre><code>{escaped_code}</code></pre>"
        code_blocks.append(block)
        return f"\x00BLOCK_{idx}\x00"

    processed = re.sub(
        r"```(?:([a-zA-Z0-9_\-]+)\r?\n)?([\s\S]*?)```",
        replace_code_block,
        text,
    )

    # 2. Protect inline code
    inline_codes: list[str] = []

    def replace_inline_code(match: re.Match) -> str:
        code = match.group(1)
        idx = len(inline_codes)
        escaped_code = html.escape(code)
        inline_codes.append(f"<code>{escaped_code}</code>")
        return f"\x00INLINE_{idx}\x00"

    processed = re.sub(r"`([^`\n]+)`", replace_inline_code, processed)

    # 3. Escape all remaining HTML entities to prevent raw HTML injection
    processed = html.escape(processed, quote=False)

    # 4. Headings: `# Heading` -> `<b>Heading</b>`
    processed = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", processed, flags=re.MULTILINE)

    # 5. Lists: clean bullet points with readable spacing
    processed = re.sub(r"^\s*[-*+]\s+(.+)$", r"• \1", processed, flags=re.MULTILINE)

    # 6. Links: [text](url) -> <a href="url">text</a> with validation
    def replace_link(match: re.Match) -> str:
        link_text = match.group(1)
        raw_url = match.group(2)
        sanitized = sanitize_url(raw_url)
        if sanitized:
            return f'<a href="{sanitized}">{link_text}</a>'
        return f"{link_text} ({raw_url})"

    processed = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", replace_link, processed)

    # 7. Bold and Italic formatting
    # Bold italic: ***text***
    processed = re.sub(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", r"<b><i>\1</i></b>", processed)
    # Bold: **text** or __text__
    processed = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", processed)
    processed = re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"<b>\1</b>", processed)
    # Italic: *text* or _text_ (non-word boundary aware to avoid breaking identifiers)
    processed = re.sub(r"(?<!\w)\*(?=\S)([^*\n]+?)(?<=\S)\*(?!\w)", r"<i>\1</i>", processed)
    processed = re.sub(r"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)", r"<i>\1</i>", processed)
    # Strikethrough: ~~text~~
    processed = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", processed)

    # 8. Restore protected code elements
    for idx, block in enumerate(inline_codes):
        processed = processed.replace(f"\x00INLINE_{idx}\x00", block)
    for idx, block in enumerate(code_blocks):
        processed = processed.replace(f"\x00BLOCK_{idx}\x00", block)

    return processed


def get_open_tags(html_segment: str) -> list[str]:
    """Return the list of currently open HTML tags in the segment."""
    stack: list[str] = []
    for match in SAFE_TAG_REGEX.finditer(html_segment):
        is_closing = match.group(1) == "/"
        tag = match.group(2).lower()
        if is_closing:
            if tag in stack:
                # Pop up to and including tag
                idx = len(stack) - 1 - list(reversed(stack)).index(tag)
                stack.pop(idx)
        else:
            stack.append(tag)
    return stack


def find_safe_split_index(text: str, max_limit: int) -> int:
    """Find a split point <= max_limit that doesn't break inside HTML tags or entities."""
    limit = min(len(text), max_limit)
    window = text[:limit]

    # Don't split inside an unclosed tag <...>
    last_open_angle = window.rfind("<")
    last_close_angle = window.rfind(">")
    if last_open_angle > last_close_angle:
        limit = last_open_angle

    # Don't split inside an unclosed entity &...;
    last_amp = window[:limit].rfind("&")
    last_semi = window[:limit].rfind(";")
    if last_amp != -1 and (last_semi == -1 or last_semi < last_amp):
        if ";" not in window[last_amp:last_amp + 10]:
            limit = last_amp

    # Try preferred split boundaries within the safe window
    safe_window = text[:limit]
    split_pos = safe_window.rfind("\n\n")
    if split_pos != -1 and split_pos > limit // 3:
        return split_pos

    split_pos = safe_window.rfind("\n")
    if split_pos != -1 and split_pos > limit // 3:
        return split_pos

    split_pos = safe_window.rfind(" ")
    if split_pos != -1 and split_pos > limit // 3:
        return split_pos

    return max(limit, 1)


def split_html_message(html_text: str, max_chunk: int = 4000) -> list[str]:
    """Split HTML text into balanced chunks within Telegram's character limit."""
    if len(html_text) <= max_chunk:
        return [html_text]

    chunks: list[str] = []
    remaining = html_text

    while remaining:
        if len(remaining) <= max_chunk:
            chunks.append(remaining)
            break

        # Reserve safety buffer for closing tags so total chunk length stays <= max_chunk
        safe_limit = max(max_chunk - 64, max_chunk // 2)
        split_idx = find_safe_split_index(remaining, safe_limit)
        chunk = remaining[:split_idx].rstrip()

        # Balance open tags in this chunk
        open_tags = get_open_tags(chunk)
        closing_tags_str = "".join(f"</{tag}>" for tag in reversed(open_tags)) if open_tags else ""

        # Ensure total chunk including closing tags does not exceed max_chunk
        while len(chunk) + len(closing_tags_str) > max_chunk and split_idx > 1:
            split_idx -= (len(chunk) + len(closing_tags_str) - max_chunk)
            split_idx = max(split_idx, 1)
            chunk = remaining[:split_idx].rstrip()
            open_tags = get_open_tags(chunk)
            closing_tags_str = "".join(f"</{tag}>" for tag in reversed(open_tags)) if open_tags else ""

        if closing_tags_str:
            chunk += closing_tags_str

        if chunk:
            chunks.append(chunk)

        # Carry over open tags into next chunk
        reopen_tags = "".join(f"<{tag}>" for tag in open_tags)
        next_part = remaining[split_idx:].lstrip()
        remaining = reopen_tags + next_part if reopen_tags else next_part

    return chunks
