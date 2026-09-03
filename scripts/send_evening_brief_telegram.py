"""
Send the weekday evening Asia-market brief to Telegram. Run after evening_brief.py.

Thin wrapper -- all logic lives in brief_telegram, all tuning in
evening_brief_config.

    python send_evening_brief_telegram.py            # send
    python send_evening_brief_telegram.py --dry-run  # print the message(s), don't send
"""
import sys

import brief_telegram
import evening_brief_config

if __name__ == "__main__":
    brief_telegram.send(evening_brief_config, sys.argv[1:])
