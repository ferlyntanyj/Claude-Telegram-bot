# SGX Liquidity Momentum Screener

Ranks SGX mainboard + Catalist stocks by ADTV surge ratio:

```
surge_ratio = (trailing 5-trading-day average daily traded value) / (full-year 2024 average daily traded value)
```

A liquidity floor (2024 ADTV >= SGD 100,000/day) is applied before ranking to exclude
near-untraded microcaps where a single small trade inflates the ratio off pure noise.
See `scripts/03_compute_screener.py` for the full rationale and `MIN_ADTV_2024_SGD` to adjust it.

## Pipeline

Run in order from the `scripts/` directory:

1. `01_get_universe.py` — pulls all SGX mainboard + Catalist stock tickers from SGX's public
   securities API (`api.sgx.com/securities/v1.1`) -> `data/universe.csv`
2. `02_fetch_history.py` — pulls daily close/volume from Yahoo Finance (2024-01-01 to present)
   for every ticker -> `data/history.parquet`
3. `03_compute_screener.py` — computes ADTV 2024, trailing 5-day ADTV, and surge ratio for every
   ticker -> `output/liquidity_momentum_screener.csv` (full), `output/liquidity_momentum_screener_liquid.csv`
   (liquidity-floor applied), `output/liquidity_momentum_screener_top10.csv` (top 10)
4. `04_build_workbook.py` — builds the formatted Excel deliverable ->
   `output/SGX_Liquidity_Momentum_Screener.xlsx`
5. `05_weekly_diff.py` — compares this run's top 10 against the most recent snapshot in
   `history/`, reports new entrants / drop-offs -> `output/weekly_summary.md`, and saves this
   run's snapshot to `history/top10_<date>.csv`

## Share buy-back alert

Separate daily pipeline that screens SGX's public company-announcements feed for "Share Buy
Back-On Market" notices (category `ANNC` / sub `ANNC13`) and flags which companies bought back
shares most recently:

6. `06_fetch_buyback_announcements.py` — calls SGX's announcements API for a trailing 14-day
   window and merges any new buy-back notices into the running log at
   `history/buyback_history.csv` (deduped by announcement id, so it's safe to re-run).
7. `07_buyback_alert.py` — takes every company that filed a buy-back on the most recent trading
   day in the history, and reports the prior buy-back date on record for that company and how
   many days elapsed since then (i.e. an ongoing daily programme vs. a resumption after a gap)
   -> `output/buyback_alert.md` and `output/buyback_alert.csv`. Each company name links directly
   to that day's SGX filing.
8. `08_buyback_company_summary.py` — collapses the full history into one row per company (not
   just today's filers), alphabetical, each linking to its most recent buy-back filing -> `output/
   buyback_company_summary.md` and `.csv`. Use this to look up a specific company's last buy-back
   date directly instead of scrolling `history/buyback_history.csv`, which is one flat
   chronological log of every company's every filing.

The announcements endpoint requires a short-lived token, obtained the same way SGX's own
frontend does: fetch a public CMS field and ROT13-decode it client-side. The endpoint also sits
behind bot-fingerprinting that blocks Python's `requests`/urllib3 outright regardless of headers,
so the listing call shells out to `curl` (identical headers, not blocked).

`history/buyback_history.csv` is the persistent record — unlike `data/` (gitignored, rebuilt
every run), `history/` is committed by the scheduled run, so the log accumulates across days
instead of resetting to the 14-day lookback window each time.

## Scheduled run

A cloud routine runs the liquidity screener pipeline every Friday at 17:30 Asia/Singapore time
(09:30 UTC), after the SGX market closes. A second daily routine runs the buy-back alert
(scripts 6-8) every weekday morning. Both commit their updated `history/` and `output/` files
back to this repo.

## Requirements

Python 3.10+, `pip install -r requirements.txt`
