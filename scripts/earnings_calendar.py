"""
Weekly earnings calendar: which S&P 500 (top names by market cap) and PHLX
Semiconductor Sector (SOX) companies report earnings in the coming trading
week, with each company's industry.

Runs Saturday via GitHub Actions (.github/workflows/earnings_calendar.yml).
Universe = the SP500_TOP_N largest S&P 500 names by market cap (full index
fetched live from Wikipedia, same source global_watchlist_build.py already
uses; ranked via yfinance fast_info -- the cap exists because the full ~500
names made the weekly alert too noisy) union SOX_CONSTITUENTS (hand-seeded,
see earnings_calendar_config.py; NOT subject to the top-N cap -- all ~30 SOX
names are always included), deduped by ticker. For each ticker in that
(smaller) universe, pulls the next scheduled earnings date via yfinance's
.calendar, and its industry via yfinance's .info. yfinance has no reliable
"confirmed vs estimated" flag for upcoming earnings dates (verified
2026-10-27 -- its own .calendar and get_earnings_dates() can disagree by a
day on the same ticker), so every date it returns is used as-is, unflagged.

"Coming trading week" = the Monday-Friday on/after today (if today is
already a Monday, that same week; this makes a manual workflow_dispatch run
on any weekday still produce a sensible window, not just the Saturday
schedule).

Output: ../output/earnings_calendar.csv (report_date, ticker, company_name,
industry, index_membership), consumed by send_earnings_calendar_telegram.py.
Not committed to the repo -- Telegram is the only delivery channel for this
alert (see earnings_calendar.yml).

Run from the scripts/ directory:  python earnings_calendar.py
"""
import sys
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd

from earnings_calendar_config import OUT_CSV_PATH, SOX_CONSTITUENTS, SP500_TOP_N
from global_watchlist_build import fetch_sp500


def _fetch_market_cap(ticker):
    """Returns (ticker, market_cap|None). Retries transient yfinance
    rate-limit errors; any other failure just returns None for that ticker."""
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    for attempt in range(3):
        try:
            cap = yf.Ticker(ticker).fast_info.get("marketCap")
            return ticker, cap
        except YFRateLimitError:
            if attempt < 2:
                _time.sleep(3 * (attempt + 1))
                continue
            return ticker, None
        except Exception:  # noqa: BLE001 -- one bad ticker must not sink the run
            return ticker, None
    return ticker, None


def _multi_pass(tickers, fetch_fn, label, max_workers=8):
    """Shared multi-pass ThreadPoolExecutor-with-cooldown shape, reused for
    market cap / earnings date / industry lookups -- a sustained burst of
    yfinance calls reliably hits Yahoo's rate limiter under load (same
    finding as global_watchlist_build.enrich_with_sector_industry)."""
    results = {t: None for t in tickers}
    pending = list(tickers)
    workers = max_workers
    for pass_num in range(3):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_fn, t): t for t in pending}
            done = 0
            for fut in as_completed(futures):
                ticker, value = fut.result()
                results[ticker] = value
                done += 1
                if done % 100 == 0:
                    print(f"  {label}: {done}/{len(pending)} checked (pass {pass_num + 1})")
        pending = [t for t in pending if results[t] is None]
        if not pending:
            break
        if pass_num < 2:
            print(f"  {label}: {len(pending)} tickers still missing after pass "
                  f"{pass_num + 1}/3, cooling down 60s before retry...")
            _time.sleep(60)
            workers = max(3, workers - 2)
    return results


def build_universe(top_n=SP500_TOP_N):
    """Return [(ticker, company_name, index_membership)], deduped by ticker.
    S&P 500 side is capped to the top_n largest names by market cap; SOX side
    is always included in full."""
    sp500 = {t: name for t, name, _region, _exch in fetch_sp500()}
    sox = dict(SOX_CONSTITUENTS)

    print(f"Ranking {len(sp500)} S&P 500 names by market cap...")
    market_caps = _multi_pass(list(sp500), _fetch_market_cap, "market cap")
    ranked = sorted((t for t in sp500 if market_caps.get(t)),
                     key=lambda t: market_caps[t], reverse=True)
    missing = len(sp500) - len(ranked)
    top_sp500 = {t: sp500[t] for t in ranked[:top_n]}
    print(f"  kept top {len(top_sp500)} of {len(sp500)} S&P 500 names by market cap"
          + (f" ({missing} had no market cap from yfinance)" if missing else ""))

    rows = []
    for ticker in sorted(set(top_sp500) | set(sox)):
        in_sp500 = ticker in top_sp500
        in_sox = ticker in sox
        name = top_sp500.get(ticker) or sox.get(ticker)
        if in_sp500 and in_sox:
            membership = "S&P 500 & SOX"
        elif in_sox:
            membership = "SOX"
        else:
            membership = "S&P 500"
        rows.append((ticker, name, membership))
    return rows


def _fetch_industry(ticker):
    """Returns (ticker, industry|None)."""
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    for attempt in range(3):
        try:
            industry = yf.Ticker(ticker).info.get("industry")
            return ticker, industry or None
        except YFRateLimitError:
            if attempt < 2:
                _time.sleep(3 * (attempt + 1))
                continue
            return ticker, None
        except Exception:  # noqa: BLE001 -- one bad ticker must not sink the run
            return ticker, None
    return ticker, None


def _fetch_next_earnings_date(ticker):
    """Returns (ticker, date|None). Retries transient yfinance rate-limit
    errors; any other failure (bad ticker, no upcoming date on record) just
    returns None for that ticker rather than aborting the run."""
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    for attempt in range(3):
        try:
            cal = yf.Ticker(ticker).calendar
            dates = (cal or {}).get("Earnings Date") or []
            return ticker, dates[0] if dates else None
        except YFRateLimitError:
            if attempt < 2:
                _time.sleep(3 * (attempt + 1))
                continue
            return ticker, None
        except Exception:  # noqa: BLE001 -- one bad ticker must not sink the run
            return ticker, None
    return ticker, None


def coming_trading_week(today=None):
    """Return (monday, friday) for the Monday-Friday on/after today."""
    today = today or date.today()
    days_until_monday = (0 - today.weekday()) % 7  # Monday == 0
    monday = today + timedelta(days=days_until_monday)
    friday = monday + timedelta(days=4)
    return monday, friday


def main():
    print(f"Building top {SP500_TOP_N} S&P 500 (by market cap) + SOX universe...")
    universe = build_universe()
    print(f"  {len(universe)} unique tickers "
          f"({sum(1 for *_, m in universe if m != 'SOX')} S&P 500, "
          f"{sum(1 for *_, m in universe if m != 'S&P 500')} SOX)")

    tickers = [t for t, _name, _m in universe]
    print(f"Fetching next earnings date for {len(tickers)} tickers via yfinance...")
    next_dates = _multi_pass(tickers, _fetch_next_earnings_date, "earnings dates")

    print(f"Fetching industry for {len(tickers)} tickers via yfinance...")
    industries = _multi_pass(tickers, _fetch_industry, "industry")

    monday, friday = coming_trading_week()
    print(f"Filtering to the coming trading week: {monday} - {friday}")

    name_by_ticker = {t: name for t, name, _m in universe}
    membership_by_ticker = {t: m for t, _name, m in universe}

    rows = []
    for ticker, report_date in next_dates.items():
        if report_date is None:
            continue
        if monday <= report_date <= friday:
            rows.append({
                "report_date": report_date.isoformat(),
                "ticker": ticker,
                "company_name": name_by_ticker[ticker],
                "industry": industries.get(ticker) or "",
                "index_membership": membership_by_ticker[ticker],
            })

    df = pd.DataFrame(rows, columns=["report_date", "ticker", "company_name", "industry", "index_membership"])
    df = df.sort_values(["report_date", "ticker"])
    df.to_csv(OUT_CSV_PATH, index=False)
    print(f"Wrote {OUT_CSV_PATH}: {len(df)} companies reporting {monday} - {friday}.")

    missing = sum(1 for d in next_dates.values() if d is None)
    if missing:
        print(f"  ({missing}/{len(tickers)} tickers had no upcoming earnings date from yfinance)",
              file=sys.stderr)


if __name__ == "__main__":
    main()
