"""
Send the weekday US-market morning brief to Telegram. Run after morning_brief.py.

Thin wrapper -- all logic lives in brief_telegram, all tuning in
morning_brief_config.

    python send_morning_brief_telegram.py            # send
    python send_morning_brief_telegram.py --dry-run  # print the message(s), don't send
"""
import sys

import brief_telegram
import morning_brief_config

if __name__ == "__main__":
    brief_telegram.send(morning_brief_config, sys.argv[1:])
