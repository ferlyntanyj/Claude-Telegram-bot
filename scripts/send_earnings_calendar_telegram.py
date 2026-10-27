"""
Send the weekly top-100-S&P-500 + SOX earnings calendar to Telegram, via a
Telegram bot. Run after earnings_calendar.py.

Reads output/earnings_calendar.csv and groups companies by report date
(Monday -> Friday), one line per company within each day: company name,
ticker, and industry (where yfinance has one on record).

Auth: reads SGX_SCREENER_TELEGRAM_BOT_TOKEN and SGX_SCREENER_TELEGRAM_CHAT_ID
from environment variables (same bot/chat as the other briefs/alerts in this
repo). Never hardcode them here.
"""
import html
import os
import sys

import pandas as pd
import requests

TOKEN_ENV_VAR = "SGX_SCREENER_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV_VAR = "SGX_SCREENER_TELEGRAM_CHAT_ID"

CALENDAR_CSV_PATH = "../output/earnings_calendar.csv"
CHUNK_CHAR_BUDGET = 3800  # stay under Telegram's 4096-char per-message limit
TITLE = "\U0001F4C5 <b>Weekly Earnings Calendar — Top 100 S&P 500 + SOX</b>"  # 📅


def _fmt_day(d):
    """'Wed 5 Nov' -- avoids strftime's non-portable '%-d' (Windows-unsafe)."""
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def build_messages(calendar):
    """Return a list of Telegram-HTML message strings (split if too long for one)."""
    if calendar.empty:
        return [f"{TITLE}\n\nNo S&P 500 or SOX companies have a confirmed/estimated "
                f"earnings date in the coming trading week."]

    dates = pd.to_datetime(calendar["report_date"])
    first_day = dates.min()
    last_day = dates.max()
    window = f"{_fmt_day(first_day)} – {_fmt_day(last_day)} {last_day.year}"

    header = (
        f"{TITLE}\n"
        f"Week of {window}\n"
        f"{len(calendar)} compan(y/ies) reporting."
    )

    day_blocks = []
    for report_date, group in calendar.groupby("report_date", sort=True):
        d = pd.to_datetime(report_date)
        day_header = f"<b>{d.strftime('%A')}, {d.day} {d.strftime('%b')}</b>"
        lines = [day_header]
        for _, r in group.sort_values("ticker").iterrows():
            name = html.escape(str(r["company_name"]))
            ticker = html.escape(str(r["ticker"]))
            industry = r.get("industry")
            tail = f" — {html.escape(str(industry))}" if pd.notna(industry) and str(industry).strip() else ""
            lines.append(f"• {name} ({ticker}){tail}")
        day_blocks.append("\n".join(lines))

    # Pack header + day blocks into as few messages as fit the char budget.
    messages = []
    current = [header]
    current_len = len(header)
    for block in day_blocks:
        if current_len + len(block) + 2 > CHUNK_CHAR_BUDGET and len(current) > 1:
            messages.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(block)
        current_len += len(block) + 2
    if current:
        messages.append("\n\n".join(current))
    return messages


def main():
    token = os.environ.get(TOKEN_ENV_VAR)
    chat_id = os.environ.get(CHAT_ID_ENV_VAR)
    if not token or not chat_id:
        missing = [v for v, val in [(TOKEN_ENV_VAR, token), (CHAT_ID_ENV_VAR, chat_id)] if not val]
        print(f"ERROR: environment variable(s) not set: {', '.join(missing)}. Skipping Telegram send.", file=sys.stderr)
        sys.exit(1)

    try:
        calendar = pd.read_csv(CALENDAR_CSV_PATH)
    except FileNotFoundError:
        print(f"ERROR: {CALENDAR_CSV_PATH} not found. Run earnings_calendar.py first.", file=sys.stderr)
        sys.exit(1)

    messages = build_messages(calendar)

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

    print(f"Telegram earnings calendar sent ({len(calendar)} compan(y/ies), {len(messages)} message(s)).")


if __name__ == "__main__":
    main()
