"""
Send a global-semiconductor-industry brief to Telegram, via a Telegram bot.

Reads a Markdown file (default ../output/semis_brief.md) written by the scheduled
"semis brief" task, converts it to Telegram's HTML subset, and posts it, split
across messages at the 4096-char limit. Standalone on purpose: the brief is free
text produced by the scheduled task each run, not a structured pipeline artifact.

Markdown handled: `#`/`##`/`###` headers -> bold, `- `/`* ` bullets -> "• ",
`**bold**`, `*italic*`, `[text](url)` links, `` `code` ``. Everything else is
passed through with HTML-escaping.

Auth: reads SGX_SCREENER_TELEGRAM_BOT_TOKEN and SGX_SCREENER_TELEGRAM_CHAT_ID
from environment variables -- the same bot/chat as the weekly screener, the
buy-back alert and the market briefs. Never hardcode them here.

Usage:
    python send_semis_brief_telegram.py [path/to/brief.md]
    python send_semis_brief_telegram.py --dry-run [path/to/brief.md]
"""
import html
import os
import re
import sys

import requests

TOKEN_ENV_VAR = "SGX_SCREENER_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV_VAR = "SGX_SCREENER_TELEGRAM_CHAT_ID"

DEFAULT_BRIEF_PATH = "../output/semis_brief.md"
CHUNK_CHAR_BUDGET = 4050  # stay under Telegram's 4096-char per-message limit
# The scheduled "semis brief" task is told to keep a whole brief under ~3400
# rendered chars so it lands in ONE message; this budget only splits an overrun.

_MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_ITALIC_US_RE = re.compile(r"(?<![\w\\])_(?!_)(.+?)(?<!_)_(?![\w])")
_CODE_RE = re.compile(r"`([^`]+)`")


def _render_inline(text):
    """Escape HTML, then re-introduce Telegram tags for links / bold / italic / code."""
    links = []

    def _stash_link(m):
        links.append((m.group(1), m.group(2)))
        return f"\x00LINK{len(links) - 1}\x00"

    text = _LINK_RE.sub(_stash_link, text)
    text = _ISO_DATE_RE.sub(_iso_to_display, text)  # after links are stashed, so URLs are safe
    text = html.escape(text, quote=False)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_STAR_RE.sub(r"<i>\1</i>", text)
    text = _ITALIC_US_RE.sub(r"<i>\1</i>", text)
    text = _CODE_RE.sub(r"<code>\1</code>", text)

    def _restore_link(m):
        label, url = links[int(m.group(1))]
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(label, quote=False)}</a>'

    return re.sub(r"\x00LINK(\d+)\x00", _restore_link, text)


def _iso_to_display(m):
    """2026-09-03 -> '3 Sep 2026' -- one date format across all Telegram messages."""
    year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
    return f"{day} {_MONTHS[month]} {year}" if 1 <= month <= 12 else m.group(0)


def markdown_to_telegram_html(md_text):
    out = []
    for line in md_text.splitlines():
        line = line.rstrip()
        stripped = line.lstrip()
        if stripped.startswith("### "):
            out.append(f"<b>{_render_inline(stripped[4:])}</b>")
        elif stripped.startswith("## "):
            out.append(f"<b>{_render_inline(stripped[3:])}</b>")
        elif stripped.startswith("# "):
            out.append(f"<b>{_render_inline(stripped[2:])}</b>")
        elif stripped.startswith(("- ", "* ")):
            indent = " " * (len(line) - len(stripped))
            out.append(f"{indent}• {_render_inline(stripped[2:])}")
        else:
            out.append(_render_inline(line))
    return "\n".join(out)


def split_messages(text, budget=CHUNK_CHAR_BUDGET):
    """Split on blank lines first, then hard-wrap any oversized block by line."""
    blocks = text.split("\n\n")
    messages, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= budget:
            current = candidate
            continue
        if current:
            messages.append(current)
            current = ""
        if len(block) <= budget:
            current = block
            continue
        for line in block.splitlines():
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= budget:
                current = candidate
            else:
                if current:
                    messages.append(current)
                current = line[:budget]
    if current:
        messages.append(current)
    return messages


def main():
    argv = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv
    brief_path = argv[0] if argv else DEFAULT_BRIEF_PATH

    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        with open(brief_path, "r", encoding="utf-8") as f:
            md_text = f.read()
    except FileNotFoundError:
        print(f"ERROR: brief not found at {brief_path}. Write it first.", file=sys.stderr)
        sys.exit(1)

    if not md_text.strip():
        print(f"ERROR: brief at {brief_path} is empty.", file=sys.stderr)
        sys.exit(1)

    messages = split_messages(markdown_to_telegram_html(md_text))

    if dry_run:
        for i, message in enumerate(messages, 1):
            print(f"----- message {i}/{len(messages)} ({len(message)} chars) -----")
            print(message)
            print()
        return

    token = os.environ.get(TOKEN_ENV_VAR)
    chat_id = os.environ.get(CHAT_ID_ENV_VAR)
    if not token or not chat_id:
        missing = [v for v, val in [(TOKEN_ENV_VAR, token), (CHAT_ID_ENV_VAR, chat_id)] if not val]
        print(f"ERROR: environment variable(s) not set: {', '.join(missing)}. Skipping Telegram send.", file=sys.stderr)
        sys.exit(1)

    for i, message in enumerate(messages, 1):
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=30,
        )
        resp.raise_for_status()
        if not resp.json().get("ok"):
            print(f"ERROR sending Telegram message {i}/{len(messages)}: {resp.json()}", file=sys.stderr)
            sys.exit(1)

    print(f"Telegram semis brief sent ({len(messages)} message(s)).")


if __name__ == "__main__":
    main()
