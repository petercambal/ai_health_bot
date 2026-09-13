"""Converts the markdown-ish text Gemini tends to produce (headings, **bold**,
bullet lists, blockquotes, links, inline code) into Telegram's HTML formatting
subset. Telegram has no heading/horizontal-rule concept, so headings become bold
text and rules are dropped rather than translated 1:1.

Order matters: bullets are converted before bold/italic (a bullet's leading `*` would
otherwise look like a stray emphasis marker to the italic regex), and bold before
italic (so a `**bold**` pair isn't first split into two bogus italics by a naive
single-`*` match).
"""

import html
import re

_HR_RE = re.compile(r"^[ \t]*([-*_])\1{2,}[ \t]*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[*-][ \t]+", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"\*(.+?)\*", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}[ \t]*(.+)$", re.MULTILINE)
# Matches against &gt;, not a literal >: html.escape() runs first (it must, so any
# literal < or > already in Gemini's text can't break our own injected tags), which
# already turns a markdown blockquote's leading `>` into `&gt;` by the time this runs.
_BLOCKQUOTE_LINE_RE = re.compile(r"^&gt;[ \t]?(.*)$", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_EXTRA_BLANK_LINES_RE = re.compile(r"\n{3,}")


def to_telegram_html(text: str) -> str:
    """Best-effort conversion - not a full markdown parser. Good enough for the
    prose Gemini actually produces in this app; callers should still fall back to
    plain text if Telegram ever rejects the result (see bot.send_reply)."""
    result = html.escape(text, quote=False)

    result = _HR_RE.sub("", result)
    result = _BULLET_RE.sub("• ", result)
    result = _BOLD_RE.sub(r"<b>\1</b>", result)
    result = _ITALIC_RE.sub(r"<i>\1</i>", result)
    result = _HEADING_RE.sub(r"<b>\1</b>", result)
    result = _collapse_blockquotes(result)
    result = _INLINE_CODE_RE.sub(r"<code>\1</code>", result)
    result = _LINK_RE.sub(r'<a href="\2">\1</a>', result)

    # Headings/rules leave behind runs of blank lines - tidy those up.
    result = _EXTRA_BLANK_LINES_RE.sub("\n\n", result)
    return result.strip()


def _collapse_blockquotes(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    quote_buf: list[str] = []

    def flush() -> None:
        if quote_buf:
            out.append("<blockquote>" + "\n".join(quote_buf) + "</blockquote>")
            quote_buf.clear()

    for line in lines:
        m = _BLOCKQUOTE_LINE_RE.match(line)
        if m:
            quote_buf.append(m.group(1))
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)
