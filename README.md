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

## Scheduled run

A cloud routine runs this pipeline every Friday at 17:30 Asia/Singapore time (09:30 UTC), after
the SGX market closes, and commits the updated `history/` snapshot and `output/` deliverables
back to this repo.

## Requirements

Python 3.10+, `pip install -r requirements.txt`
