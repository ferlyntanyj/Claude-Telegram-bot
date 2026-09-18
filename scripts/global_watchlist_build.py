"""
Maintenance script for the global major-news alert (major_news_alert.py):
builds the curated large-cap watchlist it price-checks every poll cycle.

Runs weekly via GitHub Actions (.github/workflows/global_watchlist_build.yml)
-- index membership and market caps don't change fast enough to need more.
Mirrors the numbered 01_get_universe.py pipeline style, but is a standalone
maintenance step rather than part of the daily SGX pipeline. Can also be run
manually any time: python global_watchlist_build.py

Pulls constituents of the major global indices from Wikipedia (read_html --
same "no paid data feed" approach as the rest of this repo; Nikkei 225 is
the exception, see fetch_nikkei225), then fetches each ticker's market cap
via yfinance and drops anything below MIN_MARKET_CAP_USD.

Covers 13 markets: US (S&P 500), UK (FTSE 100), Germany (DAX), Hong Kong
(Hang Seng), Japan (Nikkei 225), South Korea (KOSPI 200), Australia
(ASX 200), mainland China A-shares (CSI 300), Malaysia (FTSE Bursa Malaysia
KLCI), Indonesia (IDX LQ45), Thailand (SET50), Singapore (Straits Times
Index), plus hand-typed Vietnam and Taiwan seed lists. Taiwan was missing
entirely until 2026-09-15 -- a real gap given TSMC alone is central to the
whole semiconductor supply chain this alert cares about; caught when a real
~6% Delta Electronics (Taiwan) move went unflagged simply because the
market wasn't covered. The first fix attempt scraped topforeignstocks.com's
"TAIEX components" page, but that source turned out to be fundamentally
broken -- verified 2026-09-15 that it doesn't contain TSMC, Hon Hai,
MediaTek, or Delta Electronics at all (every real blue chip was missing),
while most of what it did contain were Taipei Exchange (OTC) codes
mislabeled as main-board TWSE. Replaced with a hand-typed seed list of the
real FTSE TWSE Taiwan 50 constituents (sourced from Wikipedia's TAIEX
article, cross-checked ticker-by-ticker against yfinance company names) --
same maintenance model as Vietnam below, and one this alert's $5B floor
trims down further anyway. The Philippines (PSEi) is deliberately excluded
-- yfinance has no working ticker format for individual PSE stocks,
verified 2026-09 (see the comment above SEED_CONSTITUENTS), so a fetcher
for it would only produce tickers that can never be priced.

A source going stale or a Wikipedia table layout drifting doesn't abort the
run -- see the try/except in build_candidate_list(); extend SEED_CONSTITUENTS
directly for deeper coverage anywhere a scraped source isn't available.

Output: ../data/global_watchlist.csv
  ticker, company_name, exchange, region, currency, market_cap_usd, sector, industry

`sector`/`industry` (added 2026-09-18, via a second, .info-based enrichment
pass over just the names that clear the $5B floor -- see
enrich_with_sector_industry) are what the alert's per-cycle price scan now
groups movers by to detect sector-wide moves, replacing an LLM-inferred peer
basket with real classification data. `company_name` is also what the
per-mover targeted news search (major_news_engine.gather_related_headlines)
queries by now that detection no longer depends on matching a headline's
text against this list at all.

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
    "TWD": ("TWD=X", "inverse"),
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
# Taiwan (FTSE TWSE Taiwan 50) also has no reliable scrapable source: the
# topforeignstocks.com "TAIEX components" page looked promising (753 rows,
# clean columns) but verified 2026-09-15 to be fundamentally wrong -- it does
# not contain TSMC, Hon Hai, MediaTek, or Delta Electronics at all (every
# actual blue chip absent), and most of what it does list are Taipei
# Exchange (OTC) codes mislabeled as main-board TWSE (they 404 under ".TW"
# but resolve under ".TWO"). This seed list is the real Taiwan 50
# constituents instead, sourced from Wikipedia's TAIEX article (the
# "vte FTSE TWSE Taiwan 50 companies" navbox lists names but no tickers) and
# verified ticker-by-ticker against yfinance company names on 2026-09-15.
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
    "Taiwan (Taiwan 50, seed)": [
        ("2330.TW", "TSMC"), ("2317.TW", "Hon Hai"), ("2454.TW", "MediaTek"),
        ("2308.TW", "Delta Electronics"), ("2303.TW", "UMC"), ("2382.TW", "Quanta"),
        ("2395.TW", "Advantech"), ("2327.TW", "Yageo"), ("2408.TW", "Nanya Technology"),
        ("3008.TW", "Largan"), ("3034.TW", "Novatek"), ("3037.TW", "Unimicron"),
        ("3045.TW", "Taiwan Mobile"), ("3231.TW", "Wistron"), ("3481.TW", "InnoLux"),
        ("3711.TW", "ASE Group"), ("4904.TW", "Far EasTone"), ("4938.TW", "Pegatron"),
        ("6669.TW", "Wiwynn"), ("2379.TW", "Realtek"), ("2345.TW", "Accton"),
        ("2357.TW", "Asus"), ("1101.TW", "Taiwan Cement"), ("1216.TW", "Uni-President"),
        ("1301.TW", "Formosa Plastics"), ("1303.TW", "Nan Ya Plastics"),
        ("1326.TW", "Formosa Chemicals & Fibre"), ("1590.TW", "AirTac"),
        ("2002.TW", "China Steel"), ("2207.TW", "Hotai Motor"), ("2301.TW", "Lite-On"),
        ("2603.TW", "Evergreen Marine"), ("2412.TW", "Chunghwa Telecom"),
        ("2912.TW", "President Chain Store"), ("6505.TW", "Formosa Petrochemical"),
        ("9910.TW", "Feng Tay"), ("2801.TW", "Chang Hwa Bank"),
        ("2880.TW", "Hua Nan Financial"), ("2881.TW", "Fubon Financial"),
        ("2882.TW", "Cathay Financial"), ("2883.TW", "KGI Financial"),
        ("2884.TW", "E.SUN Financial"), ("2885.TW", "Yuanta Financial"),
        ("2886.TW", "Mega Financial"), ("2887.TW", "Taishin Financial"),
        ("2890.TW", "Bank SinoPac"), ("2891.TW", "CTBC Financial"),
        ("2892.TW", "First Financial"), ("5871.TW", "Chailease"),
        ("5876.TW", "Shanghai Commercial & Savings Bank"),
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
        "Taiwan (Taiwan 50, seed)": ("Taiwan", "TWSE"),
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
    """Returns (original_ticker, cap, currency, resolved_ticker).
    resolved_ticker is what should actually be written to the CSV (and
    later used for live price checks) -- usually the same as the input, but
    topforeignstocks' "TAIEX" scrape turned out to mix in Taipei Exchange
    (OTC/emerging-board) listings that 404 under the main-board ".TW"
    suffix on Yahoo but resolve fine under ".TWO" (confirmed 2026-09-15:
    roughly 740 of 753 scraped "TAIEX" codes were actually OTC names, not
    main-board constituents at all -- not a rate-limit issue, a permanently
    wrong suffix). A clean not-found on a ".TW" ticker is retried once
    against ".TWO" rather than burning retry passes on a 404 that will
    never resolve."""
    import time as _time

    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    def _pull(sym):
        info = yf.Ticker(sym).fast_info
        cap = info.get("market_cap") or info.get("marketCap")
        currency = info.get("currency")
        return cap, currency

    for attempt in range(3):
        try:
            cap, currency = _pull(ticker)
            return ticker, cap, currency, ticker
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
            return ticker, None, None, ticker
        except Exception:  # noqa: BLE001 -- one bad ticker must not sink the run
            if ticker.endswith(".TW"):
                alt = ticker[:-3] + ".TWO"
                try:
                    cap, currency = _pull(alt)
                    return ticker, cap, currency, alt
                except Exception:  # noqa: BLE001
                    pass
            return ticker, None, None, ticker
    return ticker, None, None, ticker


def enrich_with_market_cap(candidates, max_workers=8):
    """Runs in up to 3 passes rather than one shot. A contiguous block of
    tickers reliably trips Yahoo's rate limiter under sustained load --
    confirmed 2026-09-15: adding Taiwan's ~750 candidates pushed the total
    past ~2500, and the whole tail of regions submitted after that point
    (South Korea, Singapore, Indonesia, Thailand) came back almost entirely
    empty even with _fetch_market_cap's per-ticker retries, because those
    retries only wait a few seconds -- not long enough to outlast a
    sustained rate-limit window. A second and third full pass over just the
    tickers still missing, after a real cooldown and at reduced
    concurrency, recovers the transient failures while still letting
    genuinely bad tickers end up None after all passes. (Taiwan's own
    near-total failure turned out to be a separate, permanent wrong-suffix
    bug -- see _fetch_market_cap -- not something more passes could fix.)"""
    import time as _time

    results = {t: (None, None, t) for t in candidates}
    pending = list(candidates)
    workers = max_workers
    for pass_num in range(3):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_market_cap, t): t for t in pending}
            done = 0
            for fut in as_completed(futures):
                ticker, cap, currency, resolved = fut.result()
                results[ticker] = (cap, currency, resolved)
                done += 1
                if done % 200 == 0:
                    print(f"  market cap: {done}/{len(pending)} checked (pass {pass_num + 1})")
        pending = [t for t in pending if results[t][0] is None]
        if not pending:
            break
        if pass_num < 2:
            print(f"  market cap: {len(pending)} tickers still missing after pass "
                  f"{pass_num + 1}/3, cooling down 60s before retry...")
            _time.sleep(60)
            workers = max(3, workers - 2)
    return results


def _fetch_sector_industry(ticker):
    """Returns (ticker, sector, industry). Uses .info rather than fast_info
    -- fast_info doesn't carry sector/industry classification at all, .info
    does (at the cost of a heavier per-ticker call). Only run over tickers
    that already cleared the $5B floor (~1,400), not the full raw candidate
    pool (~1,900), and only weekly, so the extra cost per ticker is fine.
    Added 2026-09-18 so the alert's per-cycle price scan can group movers by
    real industry classification instead of an LLM-inferred peer basket."""
    import time as _time

    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    for attempt in range(3):
        try:
            info = yf.Ticker(ticker).info
            return ticker, info.get("sector") or "", info.get("industry") or ""
        except YFRateLimitError:
            if attempt < 2:
                _time.sleep(3 * (attempt + 1))
                continue
            return ticker, "", ""
        except Exception:  # noqa: BLE001 -- one bad ticker must not sink the run
            return ticker, "", ""
    return ticker, "", ""


def enrich_with_sector_industry(tickers, max_workers=8):
    """Same multi-pass-with-cooldown retry shape as enrich_with_market_cap --
    .info calls are heavier than fast_info and hit the same kind of
    sustained rate-limit wall under load."""
    import time as _time

    results = {t: ("", "") for t in tickers}
    pending = list(tickers)
    workers = max_workers
    for pass_num in range(3):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_sector_industry, t): t for t in pending}
            done = 0
            for fut in as_completed(futures):
                ticker, sector, industry = fut.result()
                results[ticker] = (sector, industry)
                done += 1
                if done % 200 == 0:
                    print(f"  sector/industry: {done}/{len(pending)} checked (pass {pass_num + 1})")
        pending = [t for t in pending if not results[t][1]]
        if not pending:
            break
        if pass_num < 2:
            print(f"  sector/industry: {len(pending)} tickers still missing after pass "
                  f"{pass_num + 1}/3, cooling down 60s before retry...")
            _time.sleep(60)
            workers = max(3, workers - 2)
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
        cap, currency, resolved_ticker = caps.get(ticker, (None, None, ticker))
        cap_usd = to_usd(cap, currency, fx_rates)
        if cap_usd is None or cap_usd < MIN_MARKET_CAP_USD:
            continue
        rows.append({
            "ticker": resolved_ticker,
            "company_name": name,
            "exchange": exchange,
            "region": region,
            "currency": currency or "",
            "market_cap_usd": round(cap_usd),
        })

    print(f"Fetching sector/industry classification for {len(rows)} names that cleared the "
          f"${MIN_MARKET_CAP_USD/1e9:.0f}B floor...")
    sector_industry = enrich_with_sector_industry([r["ticker"] for r in rows])
    for r in rows:
        sector, industry = sector_industry.get(r["ticker"], ("", ""))
        r["sector"] = sector
        r["industry"] = industry

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
