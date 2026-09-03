"""
Twice-daily global semiconductor brief -- a concise "what moved in chips" digest
delivered to Telegram at 08:30 SGT (00:30 UTC, overnight US) and 18:30 SGT
(10:30 UTC, Asia session), every day.

Thin driver: all logic lives in brief_engine, all tuning in semis_brief_config.
Run from the scripts/ directory:  python semis_brief.py
"""
import brief_engine
import semis_brief_config

if __name__ == "__main__":
    brief_engine.run(semis_brief_config)
