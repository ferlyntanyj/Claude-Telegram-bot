"""
Send the daily SGX share buy-back alert to Telegram, via a Telegram bot.
Run after 07_buyback_alert.py.

Reads the structured alert from output/buyback_alert.csv (not the markdown --
Telegram has no table support) and formats one line per company: its most
recent buy-back date, the prior buy-back date on record, and the gap between
them, with the company name linking to that day's SGX filing.

Auth: reads SGX_SCREENER_TELEGRAM_BOT_TOKEN and SGX_SCREENER_TELEGRAM_CHAT_ID
from environment variables (same bot/chat as the weekly screener). Never
hardcode them here. See scripts/get_telegram_chat_id.py to discover the chat id.
"""
import html
import os
import sys

import pandas as pd
import requests

from buyback_config import EXCLUDE_IF_PRIOR_BUYBACK_WITHIN_DAYS

TOKEN_ENV_VAR = "SGX_SCREENER_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV_VAR = "SGX_SCREENER_TELEGRAM_CHAT_ID"

ALERT_CSV_PATH = "../output/buyback_alert.csv"
CHUNK_CHAR_BUDGET = 3800  # stay under Telegram's 4096-char per-message limit
TITLE = "\U0001F514 <b>SGX Share Buy-Back Alert</b>"  # 🔔


def build_messages(alert):
    """Return a list of Telegram-HTML message strings (split if too long for one)."""
    if alert.empty:
        note = (
            "No qualifying buy-back filings for the latest trading day "
            "(routine daily programmes are filtered out)."
            if EXCLUDE_IF_PRIOR_BUYBACK_WITHIN_DAYS > 0
            else "No share buy-back filings on record for the latest trading day."
        )
        return [f"{TITLE}\n\n{note}"]

    d = pd.to_datetime(alert["last_buyback_date"].iloc[0])
    latest_date = f"{d.day} {d.strftime('%b %Y')}"  # e.g. "2 Sep 2026"

    if EXCLUDE_IF_PRIOR_BUYBACK_WITHIN_DAYS > 0:
        scope = (
            f"{len(alert)} company(ies) filed a buy-back that day whose previous buy-back was more "
            f"than {EXCLUDE_IF_PRIOR_BUYBACK_WITHIN_DAYS} day(s) earlier, or have none on record "
            f"(routine daily programmes are filtered out)."
        )
    else:
        scope = f"{len(alert)} company(ies) filed a share buy-back that day."

    header = (
        f"{TITLE}\n"
        f"Latest trading day with filings: <b>{latest_date}</b>\n"
        f"{scope}"
    )

    company_lines = []
    for _, r in alert.iterrows():
        name = html.escape(str(r["issuer_name"]))
        code = html.escape(str(r["stock_code"]))
        url = r["url"]
        name_link = f'<a href="{html.escape(str(url))}">{name}</a>' if pd.notna(url) else name

        if pd.notna(r["prior_buyback_date"]):
            gap = int(r["days_since_prior_buyback"])
            pd_ = pd.to_datetime(r["prior_buyback_date"])
            tail = f"prior buy-back {pd_.day} {pd_:%b %Y} ({gap} day(s) earlier)"
        else:
            tail = "no prior buy-back on record (first seen by this alert)"

        company_lines.append(f"• {name_link} ({code}) — {tail}")

    # Pack lines into as few messages as fit the char budget; header only on the first.
    messages = []
    current = [header]
    current_len = len(header)
    for line in company_lines:
        if current_len + len(line) + 1 > CHUNK_CHAR_BUDGET and len(current) > 1:
            messages.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        messages.append("\n".join(current))
    return messages


def main():
    token = os.environ.get(TOKEN_ENV_VAR)
    chat_id = os.environ.get(CHAT_ID_ENV_VAR)
    if not token or not chat_id:
        missing = [v for v, val in [(TOKEN_ENV_VAR, token), (CHAT_ID_ENV_VAR, chat_id)] if not val]
        print(f"ERROR: environment variable(s) not set: {', '.join(missing)}. Skipping Telegram send.", file=sys.stderr)
        sys.exit(1)

    try:
        alert = pd.read_csv(ALERT_CSV_PATH)
    except FileNotFoundError:
        print(f"ERROR: {ALERT_CSV_PATH} not found. Run 07_buyback_alert.py first.", file=sys.stderr)
        sys.exit(1)

    messages = build_messages(alert)

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

    print(f"Telegram buy-back alert sent ({len(alert)} company row(s), {len(messages)} message(s)).")


if __name__ == "__main__":
    main()
