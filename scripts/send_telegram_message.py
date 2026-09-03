"""
Send the weekly liquidity momentum summary to Telegram (message + workbook
document), via a Telegram bot. Run after 05_weekly_diff.py.

Auth: reads SGX_SCREENER_TELEGRAM_BOT_TOKEN and SGX_SCREENER_TELEGRAM_CHAT_ID
from environment variables. Never hardcode them here. Set up with (PowerShell):
    setx SGX_SCREENER_TELEGRAM_BOT_TOKEN "<token from @BotFather>"
    setx SGX_SCREENER_TELEGRAM_CHAT_ID "<your chat id>"
See scripts/get_telegram_chat_id.py to discover the chat id.
"""
import os
import re
import sys
import requests

TOKEN_ENV_VAR = "SGX_SCREENER_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV_VAR = "SGX_SCREENER_TELEGRAM_CHAT_ID"

SUMMARY_PATH = "../output/weekly_summary.md"
WORKBOOK_PATH = "../output/SGX_Liquidity_Momentum_Screener.xlsx"

_MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _iso_to_display(m):
    """2026-09-03 -> '3 Sep 2026' -- one date format across all Telegram messages."""
    year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
    if 1 <= month <= 12:
        return f"{day} {_MONTHS[month]} {year}"
    return m.group(0)


def markdown_to_telegram_html(md_text):
    """Telegram HTML parse_mode supports <b>/<i>/<a>/<code> but not headers or <ul>/<li>."""
    md_text = _ISO_DATE_RE.sub(_iso_to_display, md_text)
    lines = md_text.splitlines()
    out = []
    for line in lines:
        line = line.rstrip()
        if line.startswith("## "):
            out.append(f"<b>{line[3:]}</b>")
        elif line.startswith("# "):
            out.append(f"<b>{line[2:]}</b>")
        elif line.startswith("- "):
            out.append(f"• {line[2:]}")
        else:
            out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return text


def main():
    token = os.environ.get(TOKEN_ENV_VAR)
    chat_id = os.environ.get(CHAT_ID_ENV_VAR)
    if not token or not chat_id:
        missing = [v for v, val in [(TOKEN_ENV_VAR, token), (CHAT_ID_ENV_VAR, chat_id)] if not val]
        print(f"ERROR: environment variable(s) not set: {', '.join(missing)}. Skipping Telegram send.", file=sys.stderr)
        sys.exit(1)

    with open(SUMMARY_PATH, "r", encoding="utf-8") as f:
        summary_text = f.read()

    telegram_text = markdown_to_telegram_html(summary_text)
    if len(telegram_text) > 4000:  # Telegram's 4096-char message limit, leave headroom
        telegram_text = telegram_text[:3900] + "\n\n... (truncated, see attached workbook)"

    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": telegram_text, "parse_mode": "HTML"},
        timeout=30,
    )
    resp.raise_for_status()
    if not resp.json().get("ok"):
        print(f"ERROR sending Telegram message: {resp.json()}", file=sys.stderr)
        sys.exit(1)
    print("Telegram summary message sent.")

    if os.path.exists(WORKBOOK_PATH):
        with open(WORKBOOK_PATH, "rb") as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat_id},
                files={"document": (os.path.basename(WORKBOOK_PATH), f)},
                timeout=60,
            )
        resp.raise_for_status()
        if not resp.json().get("ok"):
            print(f"ERROR sending Telegram document: {resp.json()}", file=sys.stderr)
            sys.exit(1)
        print("Telegram workbook attachment sent.")
    else:
        print(f"WARNING: workbook not found at {WORKBOOK_PATH}, sent message only.", file=sys.stderr)


if __name__ == "__main__":
    main()
