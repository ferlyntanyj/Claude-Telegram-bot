"""
Weekday US-market morning brief -- a concise "what happened overnight" digest
delivered to Telegram at 08:30 Asia/Singapore (00:30 UTC), after the US cash
session and after-hours have closed.

Thin driver: all logic lives in brief_engine, all tuning in morning_brief_config.
Run from the scripts/ directory:  python morning_brief.py
"""
import brief_engine
import morning_brief_config

if __name__ == "__main__":
    brief_engine.run(morning_brief_config)
