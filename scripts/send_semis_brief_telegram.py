"""
Send the twice-daily global semiconductor brief to Telegram. Run after
semis_brief.py.

Thin wrapper -- all logic lives in brief_telegram, all tuning in
semis_brief_config.

    python send_semis_brief_telegram.py            # send
    python send_semis_brief_telegram.py --dry-run  # print the message(s), don't send
"""
import sys

import brief_telegram
import semis_brief_config

if __name__ == "__main__":
    brief_telegram.send(semis_brief_config, sys.argv[1:])
