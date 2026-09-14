"""
Global major-news Telegram alert -- one run is one poll cycle. Runs in the
cloud via GitHub Actions (.github/workflows/major_news_alert.yml), not
continuously -- so it works even when this machine is off. The workflow's
cron asks for every major_news_alert_config.POLL_INTERVAL_MINUTES, but GitHub
does not actually honor that cadence for this account tier (real gaps are
2-6+ hours, not 20 minutes -- see the .yml's comments); the lookback window
self-heals around that (major_news_engine.effective_lookback_hours), so no
headline is silently missed, but "near-real-time" should be read as "checked
whenever GitHub gets to it," not a latency guarantee. run_major_news_alert.ps1
still exists for manual local testing, but is not the scheduled path.

Unlike the scheduled digests, this sends zero, one, or several Telegram
messages depending on how many qualifying stories it finds this cycle -- there
is no "quiet session" placeholder message.

Auth:
  SGX_SCREENER_TELEGRAM_BOT_TOKEN, SGX_SCREENER_TELEGRAM_CHAT_ID -- same
    bot and chat as the other briefs; alerts land in that same chat,
    interleaved with the scheduled digests.
  GROQ_API_KEY -- free-tier Groq API key (console.groq.com), used for
    peer-inference and the structured analysis write-up. No credit card needed.

Usage:
    python major_news_alert.py             # run a cycle and send any alerts
    python major_news_alert.py --dry-run   # run a cycle, print, don't send
    python major_news_alert.py --test-llm  # force one real Groq call to verify
                                            # GROQ_API_KEY works, independent of
                                            # whether any headline qualifies
"""
import datetime as dt
import html
import os
import sys

import requests

import major_news_alert_config as cfg
import major_news_engine as engine

TOKEN_ENV_VAR = "SGX_SCREENER_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV_VAR = "SGX_SCREENER_TELEGRAM_CHAT_ID"

SGT = dt.timezone(dt.timedelta(hours=8))
COLOR = {1: "🟢", -1: "🔴", 0: "⚪"}


def _esc(text):
    return html.escape(str(text), quote=False)


def _color(pct):
    return COLOR[1] if pct > 0 else COLOR[-1] if pct < 0 else COLOR[0]


def _fmt_move(pct):
    return f"{_color(pct)} {pct:+.1f}%"


def _fmt_time(headline):
    published = headline.get("published")
    if not published:
        return "time unknown"
    try:
        d = dt.datetime.fromisoformat(published).astimezone(SGT)
        return f"{d.day} {d:%b %Y}, {d:%H:%M} SGT"
    except (ValueError, TypeError):
        return "time unknown"


def render_telegram(alert):
    headline = alert["headline"]
    title = _esc(headline["title"])
    url = html.escape(str(headline["url"]), quote=True)
    source = _esc(headline["source"])
    market_name = _esc(alert["market_name"])
    time_str = _fmt_time(headline)

    primary = alert["primary"]
    primary_line = (
        f'{_esc(primary["company"])} ({_esc(primary["ticker"])}) | {_fmt_move(primary["pct"])}'
    )

    peers = alert["peers"]
    if peers:
        peers_block = "\n".join(
            f'{_esc(p["company"])} ({_esc(p["ticker"])}) | {_fmt_move(p["pct"])}' for p in peers
        )
    else:
        peers_block = "<i>No peers identified this cycle.</i>"

    analysis = alert.get("analysis") or {}
    why_moved = _esc(analysis.get("why_moved") or "(not available)")
    read_across = _esc(analysis.get("read_across") or "(not available)")
    look_out = _esc(analysis.get("look_out") or "(not available)")
    memory = _esc(analysis.get("memory") or "(not available)")

    return (
        f'<b>{market_name}</b>; <i>{time_str}</i>\n'
        f'<a href="{url}">{title}</a> — <i>{source}</i>\n'
        f'{primary_line}\n\n'
        f'<b>Analysis:</b>\n'
        f'<i>Why it moved:</i> {why_moved}\n'
        f'<i>Read across the industry:</i> {read_across}\n\n'
        f'<b>Peers movement:</b>\n'
        f'{peers_block}\n\n'
        f'<b>Look out:</b>\n'
        f'{look_out}\n\n'
        f'<b>Memory:</b>\n'
        f'{memory}\n'
        f'<i>(recalled from general knowledge -- not verified against a source)</i>\n\n'
        f'<i>Automated, data ~15-20 min delayed (Yahoo Finance) — not investment advice.</i>'
    )


def send_telegram(text, token, chat_id):
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def _test_llm():
    """Force one real Groq call with a synthetic alert -- bypasses headline
    fetching entirely, so you can verify GROQ_API_KEY works, and see exactly
    what the rendered Telegram card looks like, without waiting for a real
    qualifying story. Prints the result (or the exact failure) and exits;
    sends nothing to Telegram."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    dummy_alert = {
        "type": "single_stock",
        "headline": {
            "title": "Diagnostic test headline", "source": "major_news_alert --test-llm",
            "url": "https://example.com", "published": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
        "key": "test", "metric_pct": 6.0, "market_name": "US · NYSE/Nasdaq",
        "primary": {
            "ticker": "TEST", "company": "Diagnostic Test Co", "pct": 6.0,
            "last": 106.0, "prev": 100.0, "currency": "USD",
        },
        "peers": [
            {"ticker": "PEER1", "company": "Sample Peer One", "pct": 2.1},
            {"ticker": "PEER2", "company": "Sample Peer Two", "pct": -1.4},
        ],
    }
    dummy_alert["analysis"] = engine.write_analysis(dummy_alert, cfg)
    if dummy_alert["analysis"]["why_moved"].startswith("(unavailable"):
        print(f"GROQ TEST FAILED: {dummy_alert['analysis']['why_moved']}", file=sys.stderr)
        sys.exit(1)
    print("GROQ TEST OK -- rendered card:\n")
    print(render_telegram(dummy_alert))


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    if dry_run:  # the message contains emoji; Windows consoles default to cp1252
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if "--test-llm" in sys.argv[1:]:
        _test_llm()
        return

    alerts, state = engine.run_cycle(cfg)
    if not alerts:
        print("No qualifying alerts this cycle.")
        # Persist even on a quiet cycle -- state.last_run_utc is what makes
        # the next cycle's lookback window self-heal around GitHub's
        # irregular scheduling (see effective_lookback_hours). Skipped only
        # for --dry-run, matching every other persist() call in this script.
        if not dry_run:
            engine.persist(state, [], cfg)
        return

    if dry_run:
        for i, alert in enumerate(alerts, 1):
            print(f"----- alert {i}/{len(alerts)} -----")
            print(render_telegram(alert))
            print()
        return

    token = os.environ.get(TOKEN_ENV_VAR)
    chat_id = os.environ.get(CHAT_ID_ENV_VAR)
    if not token or not chat_id:
        missing = [v for v, val in [(TOKEN_ENV_VAR, token), (CHAT_ID_ENV_VAR, chat_id)] if not val]
        print(f"ERROR: environment variable(s) not set: {', '.join(missing)}. "
              f"Skipping Telegram send ({len(alerts)} alert(s) not sent).", file=sys.stderr)
        sys.exit(1)

    sent_alerts = []
    for alert in alerts:
        try:
            send_telegram(render_telegram(alert), token, chat_id)
            engine.mark_sent(alert, state)
            sent_alerts.append(alert)
        except Exception as e:  # noqa: BLE001 -- one bad send must not drop the rest
            print(f"ERROR sending alert: {e}", file=sys.stderr)

    engine.persist(state, sent_alerts, cfg)
    print(f"Sent {len(sent_alerts)}/{len(alerts)} alert(s) to Telegram.")


if __name__ == "__main__":
    main()
