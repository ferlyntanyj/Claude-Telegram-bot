"""
Daily earnings-analysis alert -- one run is one day's check. Runs in the
cloud via GitHub Actions (.github/workflows/earnings_analysis.yml) at 07:00
SGT, so it works even when this machine is off.

For every watchlist ticker (data/earnings_watchlist_*.csv) that has newly
reported earnings since the last run, this gathers free grounding material
(SEC EDGAR for US-listed tickers, Google News otherwise) and calls Groq's
free-tier API twice (write -> verify, see earnings_analysis_engine.py's
module docstring) to produce a full 5-section analysis, saves it to
output/earnings_analysis/, and sends a short digest to Telegram with a
pointer to the full file. Sends zero, one, or several Telegram messages
depending on how many watchlist names reported that day -- there is no
"quiet day" placeholder message. Entirely free infrastructure -- no paid
API anywhere in this pipeline.

Auth:
  GROQ_API_KEY -- required for any real (non-dry-run) run. Free tier, no
    credit card -- same key/account this repo's major_news_alert.py
    already uses, so no new secret is needed if that's already set up.
  SGX_SCREENER_TELEGRAM_BOT_TOKEN, SGX_SCREENER_TELEGRAM_CHAT_ID -- same
    bot and chat as the other briefs; digests land in that same chat,
    interleaved with the scheduled briefs and the major-news alerts.

Usage:
    python earnings_analysis.py                     # run a cycle and send any digests
    python earnings_analysis.py --dry-run           # just show what WOULD be analyzed;
                                                     # no API calls, no state changes
    python earnings_analysis.py --test-ticker NVDA  # force the full pipeline for one
                                                     # ticker (any ticker, watchlist or
                                                     # not) and print the result; never
                                                     # touches state.json or Telegram --
                                                     # use this to sanity-check report
                                                     # quality/cost before trusting the
                                                     # daily cron
"""
import datetime as dt
import os
import re
import sys

import requests

import earnings_analysis_engine as engine

TOKEN_ENV_VAR = "SGX_SCREENER_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV_VAR = "SGX_SCREENER_TELEGRAM_CHAT_ID"

_GROUP_EMOJI = {"AI Capex": "🤖", "SGX": "🇸🇬", "Bursa": "🇲🇾", "Thailand": "🇹🇭"}


def render_telegram(result):
    item = result["item"]
    emoji = _GROUP_EMOJI.get(item["group"], "📊")
    header = (
        f"{emoji} <b>{item['company']} ({item['ticker']})</b> -- earnings, "
        f"{item['report_date'].isoformat()}\n\n"
    )
    digest = re.sub(r"<(?!/?[bi]>)", "&lt;", result["digest"])  # keep <b>/<i>, escape the rest
    footer = f"\n\n<i>Full 5-section report: {result['report_path']}</i>"
    return header + digest + footer


def send_telegram(text, token, chat_id):
    if len(text) > 4000:
        text = text[:3900] + "\n\n... (truncated, see full report in repo)"
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


def _run_test_ticker(ticker):
    if not os.environ.get("GROQ_API_KEY"):
        print("ERROR: GROQ_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)
    watchlist = engine.load_watchlist()
    match = next((i for i in watchlist if i["ticker"] == ticker), None)
    company = match["company"] if match else ticker
    group = match["group"] if match else "Test"
    print(f"TEST MODE: forcing the full pipeline for {ticker} ({company}). "
          f"Not touching state.json or Telegram.\n")
    outcome = engine.run_test_ticker(ticker, company=company, group=group)
    if outcome is None:
        print("Pipeline failed -- see error above.", file=sys.stderr)
        sys.exit(1)
    item, full_report, digest = outcome
    print("=" * 20, "FULL REPORT", "=" * 20)
    print(full_report)
    print("\n" + "=" * 20, "TELEGRAM DIGEST PREVIEW", "=" * 20)
    print(render_telegram({"item": item, "digest": digest, "report_path": "(not saved -- test mode)"}))


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    try:  # messages contain emoji; Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    argv = sys.argv[1:]
    if "--test-ticker" in argv:
        idx = argv.index("--test-ticker")
        if idx + 1 >= len(argv):
            print("ERROR: --test-ticker requires a ticker argument, e.g. --test-ticker NVDA",
                  file=sys.stderr)
            sys.exit(1)
        _run_test_ticker(argv[idx + 1])
        return

    results = engine.run_cycle(dry_run=dry_run)

    if dry_run:
        print("Dry run complete -- see detection output above. Nothing sent, nothing saved.")
        return

    if not results:
        print("No qualifying earnings reports this cycle.")
        return

    token = os.environ.get(TOKEN_ENV_VAR)
    chat_id = os.environ.get(CHAT_ID_ENV_VAR)
    if not token or not chat_id:
        missing = [v for v, val in [(TOKEN_ENV_VAR, token), (CHAT_ID_ENV_VAR, chat_id)] if not val]
        print(f"ERROR: environment variable(s) not set: {', '.join(missing)}. "
              f"Skipping Telegram send ({len(results)} report(s) saved but not sent).",
              file=sys.stderr)
        sys.exit(1)

    sent = 0
    for result in results:
        try:
            send_telegram(render_telegram(result), token, chat_id)
            sent += 1
        except Exception as e:  # noqa: BLE001 -- one bad send must not drop the rest
            print(f"ERROR sending digest for {result['item']['ticker']}: {e}", file=sys.stderr)

    print(f"Sent {sent}/{len(results)} digest(s) to Telegram. "
          f"Full reports saved under {engine.cfg.OUTPUT_DIR}/.")


if __name__ == "__main__":
    main()
