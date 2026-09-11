"""
Global major-news Telegram alert -- one run is one poll cycle. Runs in the
cloud every major_news_alert_config.POLL_INTERVAL_MINUTES via GitHub Actions
(.github/workflows/major_news_alert.yml), not continuously -- so it works even
when this machine is off. run_major_news_alert.ps1 still exists for manual
local testing, but is not the scheduled path.

Unlike the scheduled digests, this sends zero, one, or several Telegram
messages depending on how many qualifying stories it finds this cycle -- there
is no "quiet session" placeholder message.

Auth:
  SGX_SCREENER_TELEGRAM_BOT_TOKEN, SGX_SCREENER_TELEGRAM_CHAT_ID -- same
    bot and chat as the other briefs; alerts land in that same chat,
    interleaved with the scheduled digests.
  ANTHROPIC_API_KEY -- separate from Claude Code; this script calls the
    Claude API directly for peer-inference and significance write-ups.

Usage:
    python major_news_alert.py            # run a cycle and send any alerts
    python major_news_alert.py --dry-run  # run a cycle, print, don't send
"""
import html
import os
import sys

import requests

import major_news_alert_config as cfg
import major_news_engine as engine

TOKEN_ENV_VAR = "SGX_SCREENER_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV_VAR = "SGX_SCREENER_TELEGRAM_CHAT_ID"

ARROW = {1: "🔺", -1: "🔻", 0: "▪️"}


def _esc(text):
    return html.escape(str(text), quote=False)


def _arrow(pct):
    return ARROW[1] if pct > 0 else ARROW[-1] if pct < 0 else ARROW[0]


def render_telegram(alert):
    headline = alert["headline"]
    title = _esc(headline["title"])
    url = html.escape(str(headline["url"]), quote=True)
    source = _esc(headline["source"])

    if alert["type"] == "single_stock":
        kicker = "🚨 <b>SINGLE-STOCK MOVE</b>"
        m = alert["move"]
        price_block = (
            f'{_arrow(m["pct"])} <b>{_esc(alert["company"])}</b> ({_esc(alert["ticker"])})  '
            f'{m["last"]:,.2f} {_esc(m.get("currency") or "")}  <i>{m["pct"]:+.1f}%</i>'
        )
    else:
        kicker = "📊 <b>SECTOR-WIDE MOVE</b>"
        result = alert["result"]
        top = sorted(result["moves"].items(), key=lambda kv: -abs(kv[1]["pct"]))[:6]
        lines = [
            f'{_arrow(m["pct"])} {_esc(result["peer_names"].get(t, t))} ({_esc(t)})  <i>{m["pct"]:+.1f}%</i>'
            for t, m in top
        ]
        price_block = (
            f'Peer median move: <b>{result["median_pct"]:+.1f}%</b> '
            f'({result["breadth_share_pct"]:.0f}% of peers moving together)\n' + "\n".join(lines)
        )

    significance = _esc(alert["significance"])

    return (
        f'{kicker}\n'
        f'<a href="{url}">{title}</a> — <i>{source}</i>\n\n'
        f'{price_block}\n\n'
        f'{significance}\n\n'
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


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    if dry_run:  # the message contains emoji; Windows consoles default to cp1252
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    alerts, state = engine.run_cycle(cfg)
    if not alerts:
        print("No qualifying alerts this cycle.")
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
