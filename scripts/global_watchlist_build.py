"""
Maintenance script for the global major-news alert (major_news_alert.py):
builds the curated large-cap watchlist it price-checks every poll cycle.

Runs weekly via GitHub Actions (.github/workflows/global_watchlist_build.yml)
-- index membership and market caps don't change fast enough to need more.
Mirrors the numbered 01_get_universe.py pipeline style, but is a standalone
maintenance step rather than part of the daily SGX pipeline. Can also be run
manually any time: python global_watchlist_build.py

Pulls constituents of the major global indices from Wikipedia (read_html --
same "no paid data feed" approach as the rest of this repo; Nikkei 225 is the
one exception, see fetch_nikkei225), then fetches each ticker's market cap via
yfinance and drops anything below MIN_MARKET_CAP_USD.

Covers 12 markets: US (S&P 500), UK (FTSE 100), Germany (DAX), Hong Kong
(Hang Seng), Japan (Nikkei 225), South Korea (KOSPI 200), Australia (ASX 200),
mainland China A-shares (CSI 300), Malaysia (FTSE Bursa Malaysia KLCI),
Indonesia (IDX LQ45), Thailand (SET50), Singapore (Straits Times Index), plus
a hand-typed Vietnam seed list (no scrapable VN30 source exists anywhere).
The Philippines (PSEi) is deliberately excluded -- yfinance has no working
ticker format for individual PSE stocks, verified 2026-09 (see the comment
above SEED_CONSTITUENTS), so a fetcher for it would only produce tickers that
can never be priced.

A source going stale or a Wikipedia table layout drifting doesn't abort the
run -- see the try/except in build_candidate_list(); extend SEED_CONSTITUENTS
directly for deeper coverage anywhere a scraped source isn't available.

Output: ../data/global_watchlist.csv
  ticker, company_name, aliases, exchange, region, currency, market_cap_usd

`aliases` is a pipe-separated, manually-extendable field (e.g. "Alphabet|Google")
so major_news_engine's headline matching doesn't depend on exact legal names --
edit the CSV directly to add aliases for names that get missed.

Run from the scripts/ directory:  python global_watchlist_build.py
"""
import io
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

OUT_CSV_PATH = "../data/global_watchlist.csv"
MIN_MARKET_CAP_USD = 5_000_000_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# FX ticker per non-USD currency yfinance reports, and how to apply it.
# "direct" tickers quote USD per unit of the currency (multiply); "inverse"
# tickers quote units of the currency per USD (divide). Missing/unknown
# currencies are skipped (market cap left as NaN -> row dropped).
FX_TICKERS = {
    "GBP": ("GBPUSD=X", "direct"),
    "GBp": ("GBPUSD=X", "direct_pence"),  # LSE often quotes pence, not pounds
    "EUR": ("EURUSD=X", "direct"),
    "AUD": ("AUDUSD=X", "direct"),
    "JPY": ("JPY=X", "inverse"),
    "HKD": ("HKD=X", "inverse"),
    "KRW": ("KRW=X", "inverse"),
    "SGD": ("SGD=X", "inverse"),
    "CNY": ("CNY=X", "inverse"),
    "MYR": ("MYR=X", "inverse"),
    "IDR": ("IDR=X", "inverse"),
    "THB": ("THB=X", "inverse"),
    "VND": ("VND=X", "inverse"),
}


# ---------------------------------------------------------------------------
# Index constituent sources
# ---------------------------------------------------------------------------
def _read_tables(url):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


def _find_table(tables, required_cols):
    """Return the first table (case-insensitively) containing all required_cols."""
    for t in tables:
        cols_lower = [str(c).strip().lower() for c in t.columns]
        if all(rc.lower() in cols_lower for rc in required_cols):
            t = t.copy()
            t.columns = cols_lower
            return t
    return None


def fetch_sp500():
    t = _find_table(_read_tables("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"),
                     ["symbol", "security"])
    rows = []
    for _, r in t.iterrows():
        ticker = str(r["symbol"]).strip().replace(".", "-")  # BRK.B -> BRK-B
        rows.append((ticker, str(r["security"]).strip(), "US", "NYSE/Nasdaq"))
    return rows


def fetch_ftse100():
    t = _find_table(_read_tables("https://en.wikipedia.org/wiki/FTSE_100_Index"), ["ticker", "company"])
    rows = []
    for _, r in t.iterrows():
        ticker = str(r["ticker"]).strip()
        if not ticker.endswith(".L"):
            ticker += ".L"
        rows.append((ticker, str(r["company"]).strip(), "UK", "LSE"))
    return rows


def fetch_dax():
    t = _find_table(_read_tables("https://en.wikipedia.org/wiki/DAX"), ["ticker", "company"])
    rows = []
    for _, r in t.iterrows():
        ticker = str(r["ticker"]).strip()
        if not ticker.endswith(".DE"):
            ticker += ".DE"
        rows.append((ticker, str(r["company"]).strip(), "Germany", "XETRA"))
    return rows


def fetch_hangseng():
    tables = _read_tables("https://en.wikipedia.org/wiki/Hang_Seng_Index")
    t = _find_table(tables, ["ticker", "constituent"]) or _find_table(tables, ["ticker", "name"])
    rows = []
    for _, r in t.iterrows():
        # Wikipedia renders this as "SEHK:\xa0700" (a non-breaking space, not a
        # regular one) -- pull the digits out directly rather than stripping
        # an exact prefix string.
        digits = re.search(r"\d+", str(r["ticker"]))
        if not digits:
            continue
        ticker = f"{int(digits.group()):04d}.HK"
        name = str(r.get("constituent") or r.get("name")).strip()
        rows.append((ticker, name, "Hong Kong", "HKEX"))
    return rows


def fetch_nikkei225():
    # Wikipedia's Nikkei 225 page has no scrapable constituent table (the 225
    # names are inline prose links, not a <table>) -- this mirror publishes
    # the same list as a genuine static table. Verified 2026-09; data dated
    # to Jan 2024 there, so expect the odd index-review miss/stale name.
    t = _find_table(
        _read_tables("https://topforeignstocks.com/indices/the-components-of-the-nikkei-225-index/"),
        ["code", "company name"],
    )
    rows = []
    for _, r in t.iterrows():
        ticker = str(r["code"]).strip()  # already carries the .T suffix
        rows.append((ticker, str(r["company name"]).strip(), "Japan", "TSE"))
    return rows


def fetch_kospi200():
    t = _find_table(_read_tables("https://en.wikipedia.org/wiki/KOSPI_200"), ["company", "symbol"])
    rows = []
    for _, r in t.iterrows():
        code = str(r["symbol"]).strip()  # 6-digit KRX code, leading zeros preserved as text
        rows.append((f"{code}.KS", str(r["company"]).strip(), "South Korea", "KRX"))
    return rows


def fetch_asx200():
    t = _find_table(_read_tables("https://en.wikipedia.org/wiki/S%26P/ASX_200"), ["code", "company"])
    rows = []
    for _, r in t.iterrows():
        ticker = str(r["code"]).strip()
        if not ticker.endswith(".AX"):
            ticker += ".AX"
        rows.append((ticker, str(r["company"]).strip(), "Australia", "ASX"))
    return rows


def fetch_csi300():
    """Mainland China A-shares (Shanghai + Shenzhen), via the CSI 300 --
    China's standard large-cap benchmark. Ticker cells read like
    "SSE: 600519" / "SZSE: 300750"; map the exchange prefix to the Yahoo
    Finance suffix."""
    t = _find_table(_read_tables("https://en.wikipedia.org/wiki/CSI_300_Index"), ["ticker", "company"])
    suffix_by_prefix = {"SSE": ".SS", "SZSE": ".SZ"}
    rows = []
    for _, r in t.iterrows():
        raw = str(r["ticker"]).strip()
        if ":" not in raw:
            continue
        prefix, code = (p.strip() for p in raw.split(":", 1))
        suffix = suffix_by_prefix.get(prefix.upper())
        if not suffix:
            continue
        rows.append((code + suffix, str(r["company"]).strip(), "China", "Shanghai/Shenzhen"))
    return rows


def fetch_klci():
    t = _find_table(_read_tables("https://en.wikipedia.org/wiki/FTSE_Bursa_Malaysia_KLCI"),
                     ["stock code", "constituent name"])
    rows = []
    for _, r in t.iterrows():
        code = str(int(r["stock code"])).strip()  # verified: no KLCI code starts with 0
        rows.append((f"{code}.KL", str(r["constituent name"]).strip(), "Malaysia", "Bursa Malaysia"))
    return rows


def fetch_lq45():
    """Indonesia's 45 most liquid large-caps. Ticker cells read like
    "IDX:\xa0AADI" (a non-breaking space after the colon) -- str.split +
    strip() handles both that and a plain space."""
    t = _find_table(_read_tables("https://en.wikipedia.org/wiki/LQ45"), ["ticker", "company"])
    rows = []
    for _, r in t.iterrows():
        code = str(r["ticker"]).split(":", 1)[-1].strip()
        rows.append((f"{code}.JK", str(r["company"]).strip(), "Indonesia", "IDX"))
    return rows


def fetch_set50():
    t = _find_table(_read_tables("https://en.wikipedia.org/wiki/SET50_Index"),
                     ["symbol", "securities name"])
    rows = []
    for _, r in t.iterrows():
        code = str(r["symbol"]).strip()
        rows.append((f"{code}.BK", str(r["securities name"]).strip(), "Thailand", "SET"))
    return rows


def fetch_sti():
    t = _find_table(_read_tables("https://en.wikipedia.org/wiki/Straits_Times_Index"),
                     ["stock symbol", "company"])
    rows = []
    for _, r in t.iterrows():
        code = str(r["stock symbol"]).split(":", 1)[-1].strip()
        rows.append((f"{code}.SI", str(r["company"]).strip(), "Singapore", "SGX"))
    return rows


# Philippines (PSEi) is deliberately NOT scraped: Yahoo Finance / yfinance has
# no working ticker format for individual PSE-listed stocks (.PS and the
# internal .XPHS suffix both resolve to dead/placeholder data, verified
# 2026-09) -- only US OTC ADRs of a few names work, at different prices. A
# PSEi fetcher would just produce tickers that can never be priced.
#
# VN30 (Vietnam) has no scrapable source anywhere (no Wikipedia page, no
# public HTML table -- HOSE's own lists are PDF-only) -- unlike the
# Philippines, yfinance DOES have working data for large-cap Vietnamese
# names under the ".VN" suffix (verified 2026-09: VCB.VN, FPT.VN both
# return live data), so a hand-typed seed list is worth keeping here.
SEED_CONSTITUENTS = {
    "Vietnam (VN30-ish, seed)": [
        ("VCB.VN", "Vietcombank"), ("BID.VN", "BIDV"), ("CTG.VN", "VietinBank"),
        ("VIC.VN", "Vingroup"), ("VHM.VN", "Vinhomes"), ("VNM.VN", "Vinamilk"),
        ("HPG.VN", "Hoa Phat Group"), ("FPT.VN", "FPT Corporation"), ("MSN.VN", "Masan Group"),
        ("TCB.VN", "Techcombank"), ("MBB.VN", "MB Bank"), ("VPB.VN", "VPBank"),
        ("GAS.VN", "PV Gas"), ("SAB.VN", "Sabeco"), ("MWG.VN", "Mobile World Investment"),
        ("PLX.VN", "Petrolimex"), ("STB.VN", "Sacombank"), ("POW.VN", "PV Power"),
        ("VJC.VN", "VietJet Aviation"), ("SSI.VN", "SSI Securities"),
    ],
}


INDEX_SOURCES = [
    ("S&P 500", fetch_sp500),
    ("FTSE 100", fetch_ftse100),
    ("DAX", fetch_dax),
    ("Hang Seng", fetch_hangseng),
    ("Nikkei 225", fetch_nikkei225),
    ("KOSPI 200", fetch_kospi200),
    ("ASX 200", fetch_asx200),
    ("CSI 300", fetch_csi300),
    ("FTSE Bursa Malaysia KLCI", fetch_klci),
    ("IDX LQ45", fetch_lq45),
    ("SET50", fetch_set50),
    ("Straits Times Index", fetch_sti),
]


def build_candidate_list():
    candidates = {}  # ticker -> (company_name, region, exchange)

    for label, fetcher in INDEX_SOURCES:
        try:
            rows = fetcher()
        except Exception as e:  # noqa: BLE001 -- Wikipedia table layouts drift; skip, don't abort
            print(f"  WARNING: could not parse {label} constituents ({e}); skipping.", file=sys.stderr)
            continue
        for ticker, name, region, exchange in rows:
            candidates.setdefault(ticker, (name, region, exchange))
        print(f"  {label}: {len(rows)} constituents")

    # label -> (region, exchange) -- exchange is shown to the user directly
    # (Telegram alert "Market Name" line), so these must be real names.
    seed_exchange = {
        "Vietnam (VN30-ish, seed)": ("Vietnam", "HOSE"),
    }
    for label, rows in SEED_CONSTITUENTS.items():
        region, exchange = seed_exchange[label]
        for ticker, name in rows:
            candidates.setdefault(ticker, (name, region, exchange))
        print(f"  {label}: {len(rows)} constituents")

    return candidates


# ---------------------------------------------------------------------------
# Market cap enrichment
# ---------------------------------------------------------------------------
def _fetch_market_cap(ticker):
    import time as _time

    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    for attempt in range(3):
        try:
            info = yf.Ticker(ticker).fast_info
            cap = info.get("market_cap") or info.get("marketCap")
            currency = info.get("currency")
            return ticker, cap, currency
        except YFRateLimitError:
            # At ~1800 tickers this reliably trips Yahoo's rate limit
            # partway through (confirmed 2026-09-14: everything queued after
            # the first ~950 tickers came back empty in one run, wiping out 8
            # entire regions with no error surfaced -- fast_info swallows it
            # into a plain empty result on the non-first ticker in a session,
            # only raising cleanly here on some). Retry with backoff rather
            # than let a transient limit silently degrade the whole list.
            if attempt < 2:
                _time.sleep(3 * (attempt + 1))
                continue
            return ticker, None, None
        except Exception:  # noqa: BLE001 -- one bad ticker must not sink the run
            return ticker, None, None
    return ticker, None, None


def enrich_with_market_cap(candidates, max_workers=8):
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_market_cap, t): t for t in candidates}
        done = 0
        for fut in as_completed(futures):
            ticker, cap, currency = fut.result()
            results[ticker] = (cap, currency)
            done += 1
            if done % 200 == 0:
                print(f"  market cap: {done}/{len(candidates)} checked")
    return results


def _download_fx_batch(symbols):
    import yfinance as yf
    return yf.download(symbols, period="5d", interval="1d", progress=False, group_by="ticker", threads=True)


def fetch_fx_rates():
    """A single missing currency here silently zeroes out every company in
    that whole region (to_usd() can't verify their market cap without it) --
    a much bigger blast radius than one missing stock price, so this retries
    harder than check_price_moves does. yf.download() doesn't raise on a
    per-symbol failure (it logs "N Failed download" and leaves that column
    all-NaN), so a retry has to re-run the whole batch and recheck which
    currencies are still missing, not catch an exception."""
    import time as _time

    rates = {"USD": 1.0}
    symbols = sorted({sym for sym, _ in FX_TICKERS.values()})
    needed = set(FX_TICKERS.keys())

    for attempt in range(4):
        data = _download_fx_batch(symbols)
        still_missing = []
        for currency in list(needed):
            sym, mode = FX_TICKERS[currency]
            try:
                close = data[sym]["Close"].dropna().iloc[-1] if len(symbols) > 1 else data["Close"].dropna().iloc[-1]
                close = float(close)
            except Exception:  # noqa: BLE001
                still_missing.append(currency)
                continue
            if mode == "direct":
                rates[currency] = close
            elif mode == "inverse":
                rates[currency] = 1.0 / close if close else None
            elif mode == "direct_pence":
                rates[currency] = close / 100.0
            needed.discard(currency)
        if not needed:
            break
        print(f"  FX rates: still missing {sorted(needed)} after attempt {attempt + 1}/4", file=sys.stderr)
        if attempt < 3:
            _time.sleep(5 * (attempt + 1))

    if needed:
        print(f"  WARNING: could not get FX rates for {sorted(needed)} after retries -- "
              f"every company priced in {sorted(needed)} will be dropped this run.", file=sys.stderr)
    return rates


def to_usd(cap, currency, fx_rates):
    if cap is None:
        return None
    if currency is None or currency == "USD":
        return cap
    rate = fx_rates.get(currency)
    if rate is None:
        return None  # unknown currency -> can't verify the cap floor, drop it
    return cap * rate


def main():
    print("Fetching index constituents...")
    candidates = build_candidate_list()
    print(f"Total unique candidate tickers: {len(candidates)}")

    print("Fetching FX rates...")
    fx_rates = fetch_fx_rates()

    print("Fetching market caps (this can take a few minutes for ~1000+ tickers)...")
    caps = enrich_with_market_cap(candidates)

    rows = []
    for ticker, (name, region, exchange) in candidates.items():
        cap, currency = caps.get(ticker, (None, None))
        cap_usd = to_usd(cap, currency, fx_rates)
        if cap_usd is None or cap_usd < MIN_MARKET_CAP_USD:
            continue
        rows.append({
            "ticker": ticker,
            "company_name": name,
            "aliases": "",
            "exchange": exchange,
            "region": region,
            "currency": currency or "",
            "market_cap_usd": round(cap_usd),
        })

    df = pd.DataFrame(rows).sort_values("market_cap_usd", ascending=False)
    df.to_csv(OUT_CSV_PATH, index=False)
    print(f"Wrote {OUT_CSV_PATH}: {len(df)} names >= ${MIN_MARKET_CAP_USD/1e9:.0f}B "
          f"(of {len(candidates)} candidates checked).")
    _warn_on_regional_dropout(df, candidates)


def _warn_on_regional_dropout(df, candidates):
    """A whole region silently disappearing (e.g. one FX rate that failed to
    fetch, since to_usd() can't verify a market cap without it -- confirmed
    to actually happen 2026-09-14, wiping out 8 regions in one run) is a much
    more likely bug than "this region genuinely has zero $5B+ companies" --
    flag it loudly rather than let a bad run silently ship a degraded CSV."""
    from collections import Counter
    raw_regions = Counter(region for _, region, _ in candidates.values())
    kept_regions = Counter(df["region"]) if len(df) else Counter()
    for region, raw_count in raw_regions.items():
        if raw_count >= 10 and kept_regions.get(region, 0) == 0:
            print(f"  WARNING: {region} had {raw_count} raw candidates but ZERO survived the "
                  f"market-cap filter -- almost certainly a data issue (FX rate, ticker "
                  f"format) this run, not reality. Investigate before trusting this CSV.",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
