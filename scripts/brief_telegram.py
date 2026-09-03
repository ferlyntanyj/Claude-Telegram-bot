"""
Shared Telegram sender for the scheduled news briefs. send() is called by the
thin per-brief wrappers (send_morning_brief_telegram.py, send_evening_brief_
telegram.py), each passing its own config module.

Reads the structured brief from cfg.OUT_JSON_PATH (not the markdown -- Telegram
uses its own HTML subset) and formats it as a compact message: optional
scoreboard block, then the cfg.SECTION_ORDER headline sections, then any X items.
Long briefs are split across messages at the 4096-char limit.

Auth: reads SGX_SCREENER_TELEGRAM_BOT_TOKEN and SGX_SCREENER_TELEGRAM_CHAT_ID
from environment variables -- the same bot/chat as the weekly screener and the
buy-back alert. Never hardcode them here.

Usage (via a wrapper):
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

CHUNK_CHAR_BUDGET = 3800  # stay under Telegram's 4096-char per-message limit
ARROW = {"up": "🔺", "down": "🔻", "flat": "▪️"}


def _esc(text):
    return html.escape(str(text), quote=False)


def _scoreboard_lines(cfg, payload):
    rows = payload["market"]
    primary = [r for r in rows if r["group"] in cfg.PRIMARY_GROUPS]
    also = [r for r in rows if r["group"] not in cfg.PRIMARY_GROUPS]

    out = [f"<b>{cfg.SCOREBOARD_EMOJI} {_esc(cfg.SCOREBOARD_HEADER)}</b>"]
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


def build_messages(cfg, payload):
    title = f"{cfg.BRIEF_EMOJI} <b>{_esc(payload['brief_title'])}</b>\n<i>{_esc(payload['generated_at_sgt'])}</i>"
    if payload.get("market_asof_note"):
        title += f"\n<i>⚠️ {_esc(payload['market_asof_note'])}</i>"

    parts = [title]
    if payload.get("market"):
        parts.append("\n".join(_scoreboard_lines(cfg, payload)))
    if payload["news_total"] == 0:
        parts.append("<i>Quiet session — nothing major flagged in the window.</i>")

    for section in cfg.SECTION_ORDER:
        items = payload["sections"].get(section, [])
        if not items:
            continue
        heading = f"{cfg.SECTION_EMOJI[section]} {cfg.SECTION_TITLES[section]}".strip()
        lines = [f"<b>{_esc(heading)}</b>"]
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

    src_prefix = "Yahoo Finance, " if payload.get("market") else ""
    parts.append(
        f"<i>Sources: {src_prefix}{cfg.SOURCES_FOOTER_TG} "
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


def send(cfg, argv):
    dry_run = "--dry-run" in argv
    if dry_run:  # the message contains emoji; Windows consoles default to cp1252
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        with open(cfg.OUT_JSON_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {cfg.OUT_JSON_PATH} not found. Build the brief first.", file=sys.stderr)
        sys.exit(1)

    messages = build_messages(cfg, payload)

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

    print(f"Telegram brief sent: {payload['brief_title']} "
          f"({payload['news_total']} headlines, {len(messages)} message(s)).")
