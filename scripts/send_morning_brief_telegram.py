"""
Send the weekday US-market morning brief to Telegram. Run after morning_brief.py.

Reads the structured brief from output/morning_brief.json (not the markdown --
Telegram uses its own HTML subset) and formats it as a compact message:
scoreboard block, then the four headline sections, then any X items.

Auth: reads SGX_SCREENER_TELEGRAM_BOT_TOKEN and SGX_SCREENER_TELEGRAM_CHAT_ID
from environment variables -- the same bot/chat as the weekly screener and the
buy-back alert. Never hardcode them here.

Usage:
    python send_morning_brief_telegram.py            # send
    python send_morning_brief_telegram.py --dry-run  # print the message(s), don't send
"""
import html
import json
import os
import sys

import requests

TOKEN_ENV_VAR = "SGX_SCREENER_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV_VAR = "SGX_SCREENER_TELEGRAM_CHAT_ID"

BRIEF_JSON_PATH = "../output/morning_brief.json"
CHUNK_CHAR_BUDGET = 3800  # stay under Telegram's 4096-char per-message limit

SECTION_ORDER = ["markets", "macro", "stocks", "geopolitics"]
SECTION_TITLES = {
    "markets": "📈 Markets",
    "macro": "🏦 Macro, Fed &amp; data",
    "stocks": "🏢 Stocks &amp; earnings",
    "geopolitics": "🌍 Geopolitics",
}
ARROW = {"up": "🔺", "down": "🔻", "flat": "▪️"}
PRIMARY_GROUPS = {"index", "vol", "rates", "commodity"}


def _esc(text):
    return html.escape(str(text), quote=False)


def _scoreboard_lines(payload):
    rows = payload["market"]
    primary = [r for r in rows if r["group"] in PRIMARY_GROUPS]
    also = [r for r in rows if r["group"] not in PRIMARY_GROUPS]

    out = ["<b>📊 Overnight scoreboard</b>"]
    for r in primary:
        arrow = ARROW.get(r["direction"], "")
        out.append(f"{arrow} <b>{_esc(r['label'])}</b>  {_esc(r['value'])}  <i>{_esc(r['move'])}</i>")
    if also:
        tail = " · ".join(f"{_esc(r['label'])} {_esc(r['value'])} ({_esc(r['move'])})" for r in also)
        out.append(f"<i>Also:</i> {tail}")
    return out


def _headline_line(item):
    title = _esc(item["title"])
    url = html.escape(str(item["url"]), quote=True)
    source = _esc(item["source"])
    return f'• <a href="{url}">{title}</a> — <i>{source}</i>'


def build_messages(payload):
    blocks = []  # list of (header_text, [body_lines]) -- header repeated only if a block splits

    title = f"🌅 <b>US Market Morning Brief</b>\n<i>{_esc(payload['generated_at_sgt'])}</i>"
    if payload.get("market_asof_note"):
        title += f"\n<i>⚠️ {_esc(payload['market_asof_note'])}</i>"

    parts = [title, "\n".join(_scoreboard_lines(payload))]

    for section in SECTION_ORDER:
        items = payload["sections"].get(section, [])
        if not items:
            continue
        lines = [f"<b>{SECTION_TITLES[section]}</b>"]
        lines += [_headline_line(it) for it in items]
        parts.append("\n".join(lines))

    if payload.get("x"):
        lines = ["<b>🐦 From X</b>"]
        for it in payload["x"]:
            text = _esc(it["text"])
            url = html.escape(str(it.get("url", "")), quote=True)
            src = _esc(it["source"])
            lines.append(f'• <a href="{url}">{text}</a> — <i>{src}</i>' if url else f"• {text} — <i>{src}</i>")
        parts.append("\n".join(lines))

    parts.append(
        f"<i>Sources: Yahoo Finance, Google News + CNBC + Fed "
        f"({payload['news_total']} headlines, {payload['lookback_hours']}h window)</i>"
    )

    # Pack parts into as few messages as fit the budget, never splitting a part.
    messages = []
    current = []
    current_len = 0
    for part in parts:
        add_len = len(part) + 2
        if current and current_len + add_len > CHUNK_CHAR_BUDGET:
            messages.append("\n\n".join(current))
            current, current_len = [], 0
        current.append(part)
        current_len += add_len
    if current:
        messages.append("\n\n".join(current))
    return messages


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    if dry_run:  # the message contains emoji; Windows consoles default to cp1252
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        with open(BRIEF_JSON_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {BRIEF_JSON_PATH} not found. Run morning_brief.py first.", file=sys.stderr)
        sys.exit(1)

    messages = build_messages(payload)

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

    print(f"Telegram morning brief sent ({payload['news_total']} headlines, {len(messages)} message(s)).")


if __name__ == "__main__":
    main()
