"""
Maintenance script for the global major-news alert (major_news_alert.py):
builds the curated large-cap watchlist it price-checks every poll cycle.

Runs weekly via GitHub Actions (.github/workflows/global_watchlist_build.yml)
-- index membership and market caps don't change fast enough to need more.
Mirrors the numbered 01_get_universe.py pipeline style, but is a standalone
maintenance step rather than part of the daily SGX pipeline. Can also be run
manually any time: python global_watchlist_build.py

Pulls constituents of the major global indices from Wikipedia (read_html --
same "no paid data feed" approach as the rest of this repo), then fetches each
ticker's market cap via yfinance and drops anything below MIN_MARKET_CAP_USD.
Some smaller indices don't have a clean, stable Wikipedia table, so those use a
small hand-maintained seed list instead -- extend SEED_CONSTITUENTS directly if
you want deeper coverage there.

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


# Wikipedia doesn't reliably tabulate these -- hand-maintained seed lists.
# Not exhaustive; extend as needed (or just add rows directly to the output CSV).
SEED_CONSTITUENTS = {
    "Japan (Nikkei 225, seed)": [
        ("7203.T", "Toyota Motor"), ("6758.T", "Sony Group"), ("9984.T", "SoftBank Group"),
        ("8306.T", "Mitsubishi UFJ Financial Group"), ("6501.T", "Hitachi"),
        ("8035.T", "Tokyo Electron"), ("6098.T", "Recruit Holdings"), ("9432.T", "NTT"),
        ("9433.T", "KDDI"), ("6861.T", "Keyence"), ("7974.T", "Nintendo"),
        ("8058.T", "Mitsubishi Corp"), ("7267.T", "Honda Motor"), ("4063.T", "Shin-Etsu Chemical"),
        ("6702.T", "Fujitsu"), ("6367.T", "Daikin Industries"), ("8316.T", "Sumitomo Mitsui Financial Group"),
        ("9983.T", "Fast Retailing"), ("4568.T", "Daiichi Sankyo"), ("6178.T", "Japan Post Holdings"),
    ],
    "South Korea (KOSPI, seed)": [
        ("005930.KS", "Samsung Electronics"), ("000660.KS", "SK Hynix"),
        ("373220.KS", "LG Energy Solution"), ("207940.KS", "Samsung Biologics"),
        ("005380.KS", "Hyundai Motor"), ("012330.KS", "Hyundai Mobis"),
        ("035420.KS", "Naver"), ("051910.KS", "LG Chem"),
        ("006400.KS", "Samsung SDI"), ("105560.KS", "KB Financial Group"),
        ("055550.KS", "Shinhan Financial Group"), ("035720.KS", "Kakao"),
        ("068270.KS", "Celltrion"), ("003670.KS", "POSCO Holdings"),
    ],
    "Singapore (STI, seed)": [
        ("D05.SI", "DBS Group Holdings"), ("O39.SI", "Oversea-Chinese Banking Corp"),
        ("U11.SI", "United Overseas Bank"), ("C6L.SI", "Singapore Airlines"),
        ("Z74.SI", "Singtel"), ("C38U.SI", "CapitaLand Integrated Commercial Trust"),
        ("A17U.SI", "Ascendas REIT"), ("BN4.SI", "Keppel"),
        ("C09.SI", "City Developments"), ("Y92.SI", "Thai Beverage"),
    ],
    "Australia (ASX 200, seed)": [
        ("BHP.AX", "BHP Group"), ("CBA.AX", "Commonwealth Bank of Australia"),
        ("CSL.AX", "CSL Limited"), ("NAB.AX", "National Australia Bank"),
        ("WBC.AX", "Westpac Banking Corp"), ("ANZ.AX", "ANZ Group Holdings"),
        ("WES.AX", "Wesfarmers"), ("MQG.AX", "Macquarie Group"),
        ("FMG.AX", "Fortescue"), ("WDS.AX", "Woodside Energy Group"),
        ("GMG.AX", "Goodman Group"), ("TLS.AX", "Telstra Group"),
    ],
}


INDEX_SOURCES = [
    ("S&P 500", fetch_sp500),
    ("FTSE 100", fetch_ftse100),
    ("DAX", fetch_dax),
    ("Hang Seng", fetch_hangseng),
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

    for label, rows in SEED_CONSTITUENTS.items():
        for ticker, name in rows:
            region = label.split(" (")[0]
            candidates.setdefault(ticker, (name, region, "seed list"))
        print(f"  {label}: {len(rows)} constituents")

    return candidates


# ---------------------------------------------------------------------------
# Market cap enrichment
# ---------------------------------------------------------------------------
def _fetch_market_cap(ticker):
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).fast_info
        cap = info.get("market_cap") or info.get("marketCap")
        currency = info.get("currency")
        return ticker, cap, currency
    except Exception:  # noqa: BLE001 -- one bad ticker must not sink the run
        return ticker, None, None


def enrich_with_market_cap(candidates, max_workers=12):
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


def fetch_fx_rates():
    import yfinance as yf
    rates = {"USD": 1.0}
    symbols = sorted({sym for sym, _ in FX_TICKERS.values()})
    data = yf.download(symbols, period="5d", interval="1d", progress=False, group_by="ticker", threads=True)
    for currency, (sym, mode) in FX_TICKERS.items():
        try:
            close = data[sym]["Close"].dropna().iloc[-1] if len(symbols) > 1 else data["Close"].dropna().iloc[-1]
            close = float(close)
        except Exception:  # noqa: BLE001
            continue
        if mode == "direct":
            rates[currency] = close
        elif mode == "inverse":
            rates[currency] = 1.0 / close if close else None
        elif mode == "direct_pence":
            rates[currency] = close / 100.0
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


if __name__ == "__main__":
    main()
