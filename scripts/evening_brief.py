"""
Weekday evening Asia-market brief -- a concise "what happened in Asia today"
digest delivered to Telegram at 18:15 Asia/Singapore (10:15 UTC), after the
Tokyo / Seoul / Shanghai / Hong Kong closes.

Thin driver: all logic lives in brief_engine, all tuning in evening_brief_config.
Run from the scripts/ directory:  python evening_brief.py
"""
import brief_engine
import evening_brief_config

if __name__ == "__main__":
    brief_engine.run(evening_brief_config)
