"""
One-time helper: after messaging your new bot at least once in Telegram,
run this to discover your chat_id (reads the bot token from the
SGX_SCREENER_TELEGRAM_BOT_TOKEN environment variable).
"""
import os
import sys
import requests

ENV_VAR = "SGX_SCREENER_TELEGRAM_BOT_TOKEN"


def main():
    token = os.environ.get(ENV_VAR)
    if not token:
        print(f"ERROR: environment variable {ENV_VAR} is not set.", file=sys.stderr)
        sys.exit(1)

    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    if not payload.get("ok"):
        print(f"ERROR from Telegram API: {payload}", file=sys.stderr)
        sys.exit(1)

    results = payload.get("result", [])
    if not results:
        print("No messages found yet. Send any message (e.g. 'hi') to your bot in Telegram, then re-run this.")
        sys.exit(1)

    seen = {}
    for update in results:
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue
        chat = msg["chat"]
        seen[chat["id"]] = chat

    print("Chat(s) found:")
    for chat_id, chat in seen.items():
        label = chat.get("username") or chat.get("first_name") or chat.get("title") or "unknown"
        print(f"  chat_id={chat_id}  ({chat.get('type')}, {label})")
    print("\nUse the chat_id above matching your own account:")
    print('  setx SGX_SCREENER_TELEGRAM_CHAT_ID "<chat_id>"')


if __name__ == "__main__":
    main()
