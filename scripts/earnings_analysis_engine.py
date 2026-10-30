"""
Core logic for the daily earnings-analysis alert. One call to run_cycle() is
one day's check. Entirely free infrastructure -- see the top of
earnings_analysis_config.py for why (Groq free tier, SEC EDGAR, Google News
RSS; no Anthropic/paid API anywhere in this pipeline).

Detection: for every ticker across the watchlist CSVs (earnings_analysis_
config.WATCHLIST_FILES), pull yfinance's get_earnings_dates() and find the
most recent row that already has a "Reported EPS" (i.e. actually happened,
not an upcoming estimate), falling back to the newest quarterly-financials
column if that's empty (detect_new_reports). A handful of smaller SGX/HK
names have neither signal in yfinance at all (confirmed empirically, not a
yfinance bug -- Yahoo just doesn't carry them); for those,
detect_new_reports_via_news uses a free Google News RSS search to check
whether the company has reported since its last known date (no LLM call
needed for detection itself -- just a keyword-filtered feed read). Both
paths are free, so both run every time, including during --dry-run.

Either way, the resolved date is compared against data/earnings_analysis_
state.json's last-seen date for that ticker -- a newer date means a new
report to analyze. A ticker's very first appearance in state is only
baselined (recorded, never analyzed) so adding a new name, or the
first-ever run, doesn't fire the pipeline for the whole watchlist at once --
see MAX_REPORT_AGE_DAYS in the config module.

Per newly-reported ticker:
  1. grounding (_gather_grounding, no LLM call) -- US-listed tickers (no
     yfinance suffix) fetch their most recent 8-K/6-K text directly from
     SEC EDGAR (config.is_edgar_ticker), the genuine primary filing. Every
     other ticker falls back to a Google News RSS search for the
     company's results coverage (headlines, sources, and the top couple of
     linked articles' text) -- the closest free source available, not the
     primary filing, since no other exchange here offers a free developer
     feed and this repo does not build around another site's anti-bot
     measures (api.sgx.com returns a flat "Access Denied" to datacenter
     traffic, confirmed by testing).
  2. write (Groq, config.LLM_MODEL) -- turns the grounding material plus a
     yfinance quantitative snapshot into the full 5-section report using
     config.ANALYSIS_INSTRUCTIONS verbatim as the brief.
  3. verify (Groq) -- independent fact-check pass over the draft: cross-
     checks every quote/figure against what grounding actually found,
     flags anything unverifiable, and appends a short Telegram digest after
     config.DIGEST_MARKER so no third call is needed just to summarize.

A failure at any stage, for any one ticker, is caught and logged -- it does
not stop the rest of the watchlist from being checked, matching how the
other alerts in this repo treat a single bad ticker as non-fatal. A
failure here does NOT advance that ticker's last_analyzed_date, so it gets
retried on every subsequent run until it succeeds (see _apply_detection's
docstring). This was confirmed necessary by a real Groq 429 during
testing, back when this pipeline shared GROQ_API_KEY with
major_news_alert.py's much more frequent polling -- it now uses its own
separate account/key (GROQ_API_KEY_EARNINGS) specifically to avoid that
contention, but the retry-safety stays regardless, as a second line of
defense against any other transient failure (an outage, a bad response).

State (data/earnings_analysis_state.json) is a flat map of
ticker -> {"last_reported_date": "YYYY-MM-DD", "last_analyzed_date":
"YYYY-MM-DD"}, plus a top-level
last_run_utc for observability (same shape convention as
data/alert_state.json in major_news_engine.py).
"""
import csv
import datetime as dt
import json
import os
import re
import time as _time
import traceback
import urllib.parse

import requests
import yfinance as yf

import brief_engine
import earnings_analysis_config as cfg


def load_watchlist():
    """Returns [{"ticker", "company", "group"}], deduped by ticker (first
    occurrence wins, so listing the same ticker in two groups is harmless
    but only alerts once under the first group it's found in)."""
    rows = []
    seen = set()
    for group, path in cfg.WATCHLIST_FILES:
        if not os.path.exists(path):
            print(f"WARNING: watchlist file not found, skipping: {path}")
            continue
        with open(path, "r", encoding="utf-8", newline="") as f:
            lines = [ln for ln in f if not ln.lstrip().startswith("#")]
        for row in csv.DictReader(lines):
            ticker = (row.get("ticker") or "").strip()
            company = (row.get("company") or "").strip()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            rows.append({"ticker": ticker, "company": company, "group": group})
    return rows


def load_state():
    if os.path.exists(cfg.STATE_PATH):
        with open(cfg.STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"tickers": {}}


def save_state(state):
    state["last_run_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(os.path.dirname(cfg.STATE_PATH) or ".", exist_ok=True)
    with open(cfg.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def _fallback_quarterly_date(ticker):
    """get_earnings_dates() has poor coverage for smaller SGX/HK/Bursa names
    (verified empirically -- ~1 in 5 of this watchlist returns "no earnings
    dates found" from it, all smaller-cap regional names, none of the large
    US/Japan/Taiwan names). Falls back to the newest column in yfinance's
    quarterly financials as a much rougher proxy: that's the fiscal
    PERIOD-END date, not the announcement date, and only appears once Yahoo
    has actually processed the filing -- so detection via this path lags the
    real announcement by however long that takes, with no EPS/estimate data
    at all. Still strictly better than never firing for these names."""
    try:
        qf = yf.Ticker(ticker).quarterly_financials
    except Exception:  # noqa: BLE001
        return None
    if qf is None or qf.empty or not len(qf.columns):
        return None
    newest_col = qf.columns[0]
    period_end = newest_col.date() if hasattr(newest_col, "date") else None
    if period_end is None:
        return None
    return (period_end, None, None, None)


def _latest_reported_row(ticker):
    """Returns (date, eps_estimate, reported_eps, surprise_pct) for the most
    recent actually-reported quarter, or None if yfinance has nothing at all
    for this ticker (bad ticker, delisted, no data from either source)."""
    try:
        df = yf.Ticker(ticker).get_earnings_dates(limit=8)
    except Exception:  # noqa: BLE001 -- one bad ticker must not sink the run
        df = None
    reported = df[df["Reported EPS"].notna()] if df is not None and not df.empty else None
    if reported is None or reported.empty:
        return _fallback_quarterly_date(ticker)
    reported = reported.sort_index(ascending=False)
    idx = reported.index[0]
    row = reported.iloc[0]
    report_date = idx.tz_localize(None).date() if idx.tzinfo else idx.date()
    return (
        report_date,
        row.get("EPS Estimate"),
        row.get("Reported EPS"),
        row.get("Surprise(%)"),
    )


def _apply_detection(item, latest, tickers_state, today):
    """Shared detection logic for one ticker, given a resolved
    (date, eps_estimate, reported_eps, surprise_pct) tuple from whichever
    path (yfinance or the Google News fallback) found it. Returns a
    new-report entry dict to analyze, or None if there's nothing pending.

    Tracks two separate dates per ticker, deliberately not one -- confirmed
    necessary by a real Groq 429 (daily token cap, shared with
    major_news_alert.py) during testing: with a single "last_reported_date"
    that doubles as both the detection baseline AND the "already handled"
    marker, a single failed analyze_ticker() call would have permanently
    marked that report as done, silently dropping it forever even though
    it was never actually analyzed or sent.
      - last_reported_date: the newest report date detection has ever seen
        (prevents re-treating the same report as "new" every run).
      - last_analyzed_date: the newest report date actually, successfully
        analyzed. Whenever this trails last_reported_date, that report is
        still pending and gets retried on every subsequent run -- with no
        retry cap, deliberately: every stage below the Groq calls already
        degrades gracefully instead of raising (gather_financial_snapshot,
        _gather_grounding), so a persistent failure here is far more likely
        to be transient (rate limit, an outage) than the "permanently bad
        per-ticker data" case the old single-date design was guarding
        against, and the cost of trying again is just one more cheap call,
        not a fresh full re-scan."""
    ticker = item["ticker"]
    report_date, eps_est, reported_eps, surprise_pct = latest
    stored = tickers_state.get(ticker)

    if stored is None:
        # First time ever seeing this ticker -- baseline only, never fires.
        tickers_state[ticker] = {
            "last_reported_date": report_date.isoformat(),
            "last_analyzed_date": report_date.isoformat(),
        }
        return None

    if report_date.isoformat() > stored["last_reported_date"]:
        # A genuinely new report. Only accept it if it's recent enough --
        # guards against yfinance briefly returning a stale/partial row
        # jumping the baseline forward by an implausible amount. This gate
        # applies only to accepting a brand-new report, never to retrying
        # one already accepted (see docstring) -- a report that's still
        # pending keeps retrying regardless of how many days have now
        # passed since it first appeared.
        if (today - report_date).days <= cfg.MAX_REPORT_AGE_DAYS:
            stored["last_reported_date"] = report_date.isoformat()
        else:
            return None

    if (stored.get("last_analyzed_date") or "") >= stored["last_reported_date"]:
        return None  # nothing pending

    return {
        **item,
        "report_date": report_date,
        "eps_estimate": eps_est,
        "reported_eps": reported_eps,
        "surprise_pct": surprise_pct,
    }


def detect_new_reports(watchlist, state):
    """Returns (new_reports, undetected). new_reports is
    [{"ticker", "company", "group", "report_date", "eps_estimate",
    "reported_eps", "surprise_pct"}] for tickers with a reported-earnings
    date (via yfinance) newer than what's in state. undetected is the
    watchlist items yfinance had NOTHING for at all (no get_earnings_dates,
    no quarterly_financials) -- see detect_new_reports_via_search for the
    paid fallback that covers those. Mutates state's baseline for every
    yfinance-resolved ticker (caller saves state once the run finishes) --
    a ticker only gets flagged for analysis once; after that, its baseline
    moves to the new date regardless of whether the analysis pipeline
    itself later succeeds, so a persistent per-ticker failure (e.g.
    permanently bad data) doesn't retry forever and burn API cost every
    day. This whole function is free (no Anthropic calls), so it runs the
    same in --dry-run and in a real run."""
    today = dt.date.today()
    new_reports = []
    undetected = []
    tickers_state = state.setdefault("tickers", {})

    for item in watchlist:
        latest = _latest_reported_row(item["ticker"])
        if latest is None:
            undetected.append(item)
            continue
        entry = _apply_detection(item, latest, tickers_state, today)
        if entry:
            new_reports.append(entry)

    return new_reports, undetected


def _google_news_entries(query, max_results=8, lookback_days=400):
    """Free Google News RSS search, no API key -- reuses the exact feed
    primitives major_news_engine.py's own gather_related_headlines already
    relies on (brief_engine._parse_feed runs on a session with a UA already
    set, so no bot-wall concern here unlike api.sgx.com). Returns up to
    max_results entries as {title, source, url, published (date)}, newest
    first, deduped by title. lookback_days is generous -- the query itself
    is already scoped to one company, so a wide window just means more
    candidates for the keyword filter, not more noise."""
    hl, gl, ceid = cfg.GOOGLE_NEWS_LOCALE
    url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}"
           f"&hl={hl}&gl={gl}&ceid={ceid}")
    feed = brief_engine._parse_feed(url, cfg.HTTP_TIMEOUT_SECONDS)
    if not feed:
        return []
    now = dt.datetime.now(dt.timezone.utc)
    seen = set()
    results = []
    for entry in feed.entries:
        title = (getattr(entry, "title", "") or "").strip()
        if not title:
            continue
        age_h, published = brief_engine._entry_age_hours(entry, now)
        if age_h > lookback_days * 24:
            continue
        clean_title = brief_engine._TRAIL_SOURCE_RE.sub("", title).strip()
        key = clean_title.lower()
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "title": clean_title,
            "source": brief_engine._source_name(entry, "Google News"),
            "url": getattr(entry, "link", "") or "",
            "published": published.date(),
        })
    results.sort(key=lambda r: r["published"], reverse=True)
    return results[:max_results]


def _news_query(company):
    """Quoted exact-phrase query -- confirmed by testing to matter a lot:
    an unquoted "AEM Holdings results" search surfaced Agnico Eagle Mines
    (NYSE: AEM) and an unrelated "Aeluma" results release ahead of any real
    AEM Holdings story, since Google News' own relevance ranking matches
    loosely on an unquoted multi-word query. Quoting the company name fixed
    it in that same test -- every result became genuinely about the right
    company."""
    return f'"{company}" results'


def _title_matches_company(title, company):
    """Second, cheap safety net on top of the quoted query above: the
    title must contain the company name's first distinctive word. Guards
    against the rare case where Google News' RSS still returns a loosely
    related result despite the quoted phrase."""
    first_word = re.sub(r"[^\w]", "", company.split()[0]) if company.split() else ""
    return bool(first_word) and first_word.lower() in title.lower()


def _check_reported_via_news(item, known_date):
    """Free Google News RSS fallback detector for tickers yfinance has
    nothing on at all (confirmed empirically: several of this watchlist's
    smaller SGX names have no get_earnings_dates() AND no
    quarterly_financials data -- not a yfinance bug, Yahoo just doesn't
    carry them; the underlying data is findable on ordinary news/finance
    sites). Deliberately NOT a direct SGX API call: api.sgx.com sits behind
    Akamai bot protection that rejects datacenter traffic outright
    (confirmed by testing against the live endpoint), and this repo does
    not build around another site's anti-bot measures. No LLM call needed
    here, just a keyword-filtered feed read -- returns a
    (date, None, None, None) tuple (no EPS data available this way) or None
    if nothing newer than known_date was found."""
    entries = _google_news_entries(_news_query(item["company"]), max_results=10)
    keyword_re = re.compile(cfg.EARNINGS_KEYWORDS_RE, re.I)
    matches = [e for e in entries if keyword_re.search(e["title"])
               and _title_matches_company(e["title"], item["company"])]
    if not matches:
        return None
    newest = matches[0]  # _google_news_entries already sorts newest-first
    if known_date and newest["published"].isoformat() <= known_date:
        return None
    return (newest["published"], None, None, None)


def detect_new_reports_via_news(undetected_items, state):
    """Runs _check_reported_via_news for each item detect_new_reports
    couldn't resolve via yfinance. Free (no LLM call) -- unlike the paid
    web-search fallback this replaced, this runs the same during --dry-run
    and a real run."""
    today = dt.date.today()
    new_reports = []
    tickers_state = state.setdefault("tickers", {})
    for item in undetected_items:
        known_date = tickers_state.get(item["ticker"], {}).get("last_reported_date")
        try:
            latest = _check_reported_via_news(item, known_date)
        except Exception:  # noqa: BLE001 -- one bad ticker must not sink the run
            print(f"ERROR checking {item['ticker']} via Google News:")
            traceback.print_exc()
            continue
        if latest is None:
            continue
        entry = _apply_detection(item, latest, tickers_state, today)
        if entry:
            new_reports.append(entry)
    return new_reports


def _quarterly_snapshot(ticker):
    """Best-effort revenue/net income for the latest reported quarter vs the
    prior quarter (QoQ) and the same quarter a year ago (YoY), from
    yfinance's quarterly financials. Returns a plain dict of what it could
    find -- missing rows/columns just come back as None, never raise."""
    snapshot = {}
    try:
        qf = yf.Ticker(ticker).quarterly_financials
    except Exception:  # noqa: BLE001
        qf = None
    if qf is not None and not qf.empty and len(qf.columns) >= 1:
        cols = list(qf.columns)  # newest first
        for label, col_idx in (("current", 0), ("qoq_prior", 1), ("yoy_prior", 4)):
            if col_idx >= len(cols):
                snapshot[label] = None
                continue
            col = qf[cols[col_idx]]
            snapshot[label] = {
                "period_end": cols[col_idx].strftime("%Y-%m-%d"),
                "total_revenue": col.get("Total Revenue"),
                "gross_profit": col.get("Gross Profit"),
                "operating_income": col.get("Operating Income"),
                "net_income": col.get("Net Income"),
            }
    return snapshot


def _price_reaction(ticker, report_date):
    """Best-effort % price move in the 3 trading days straddling report_date
    (pre-report close vs post-report close). Returns None if unavailable."""
    try:
        start = report_date - dt.timedelta(days=5)
        end = report_date + dt.timedelta(days=5)
        hist = yf.Ticker(ticker).history(start=start.isoformat(), end=end.isoformat())
    except Exception:  # noqa: BLE001
        return None
    if hist is None or hist.empty:
        return None
    hist = hist.sort_index()
    before = hist[hist.index.date < report_date]
    after = hist[hist.index.date >= report_date]
    if before.empty or after.empty:
        return None
    pre_close = before["Close"].iloc[-1]
    post_close = after["Close"].iloc[-1]
    if not pre_close:
        return None
    return round((post_close - pre_close) / pre_close * 100, 2)


def gather_financial_snapshot(ticker, report_date):
    return {
        "quarterly": _quarterly_snapshot(ticker),
        "price_reaction_pct": _price_reaction(ticker, report_date),
    }


def _snapshot_to_text(item, snapshot):
    q = snapshot.get("quarterly") or {}
    lines = [
        f"Ticker: {item['ticker']} ({item['company']}, group: {item['group']})",
        f"Reported date (yfinance): {item['report_date'].isoformat()}",
        f"EPS estimate: {item.get('eps_estimate')}  Reported EPS: {item.get('reported_eps')}  "
        f"Surprise%: {item.get('surprise_pct')}",
    ]
    for label, title in (("current", "Latest quarter"), ("qoq_prior", "Prior quarter (QoQ base)"),
                          ("yoy_prior", "Same quarter last year (YoY base)")):
        row = q.get(label)
        if not row:
            continue
        lines.append(
            f"{title} ({row['period_end']}): revenue={row['total_revenue']}, "
            f"gross_profit={row['gross_profit']}, operating_income={row['operating_income']}, "
            f"net_income={row['net_income']}"
        )
    pr = snapshot.get("price_reaction_pct")
    lines.append(f"Price reaction around report date: {pr}%" if pr is not None
                  else "Price reaction around report date: not available from yfinance")
    return "\n".join(lines)


_EDGAR_TICKER_MAP = None  # lazily loaded, cached for the life of the process


def _edgar_ticker_map():
    """ticker(upper) -> CIK(int), from SEC's own published mapping file.
    Loaded once per run regardless of how many EDGAR tickers need it."""
    global _EDGAR_TICKER_MAP
    if _EDGAR_TICKER_MAP is not None:
        return _EDGAR_TICKER_MAP
    resp = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers={"User-Agent": cfg.SEC_EDGAR_USER_AGENT},
        timeout=cfg.HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    _EDGAR_TICKER_MAP = {v["ticker"].upper(): v["cik_str"] for v in resp.json().values()}
    return _EDGAR_TICKER_MAP


def _edgar_fetch_text(url, headers):
    import lxml.html
    resp = requests.get(url, headers=headers, timeout=cfg.HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return " ".join(lxml.html.fromstring(resp.content).text_content().split())


_R_PAGE_RE = re.compile(r"^R\d+\.html?$", re.I)


def _edgar_filing_exhibits(cik, accession_nodash, headers, exclude_name, max_docs=2):
    """Returns up to max_docs [(name, text)] for the largest non-cover .htm
    documents in this filing's accession folder. SEC's index.json doesn't
    semantically label which file is the earnings press release/commentary
    exhibit vs. the cover form vs. an XBRL-rendered-report stub (R#.htm),
    so this picks by SIZE instead -- confirmed empirically against a real
    NVIDIA 8-K that this reliably works: cover page ~26KB vs. the actual
    press release exhibit 341KB and CFO-commentary exhibit 257KB. The
    primary document (the cover page, already handled by the caller) and
    R#.htm stubs are excluded so they can't crowd out the real exhibits."""
    resp = requests.get(
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/index.json",
        headers=headers, timeout=cfg.HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    candidates = []
    for item in resp.json().get("directory", {}).get("item", []):
        name = item.get("name", "")
        if not re.search(r"\.html?$", name, re.I):
            continue
        if name == exclude_name or "index" in name.lower() or _R_PAGE_RE.match(name):
            continue
        try:
            size = int(item.get("size") or 0)
        except ValueError:
            size = 0
        candidates.append((size, name))
    candidates.sort(reverse=True)

    base_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/"
    docs = []
    for _, name in candidates[:max_docs]:
        try:
            docs.append((name, _edgar_fetch_text(base_url + name, headers)))
        except Exception:  # noqa: BLE001 -- one bad exhibit must not sink the rest
            continue
    return docs


def _edgar_filing_grounding(item):
    """Fetches the most recent 8-K/6-K (preferred -- these are where the
    actual earnings press release/CFO-commentary exhibit lives) or
    10-Q/20-F filed within 10 days of report_date directly from SEC EDGAR
    -- the genuine primary filing, free and script-friendly (SEC requires/
    documents a descriptive User-Agent for automated access,
    config.SEC_EDGAR_USER_AGENT, rather than blocking it -- unlike every
    other exchange checked for this watchlist). Returns
    (grounding_text, source_label), or None if the ticker isn't in SEC's
    own ticker list or no matching filing turns up in that window (caller
    falls back to Google News either way)."""
    headers = {"User-Agent": cfg.SEC_EDGAR_USER_AGENT}
    cik = _edgar_ticker_map().get(item["ticker"].upper())
    if cik is None:
        return None
    resp = requests.get(
        f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
        headers=headers, timeout=cfg.HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    recent = resp.json().get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    report_date = item["report_date"]
    # 8-K/6-K (the vehicle for an earnings press release) ranks ahead of
    # 10-Q/20-F (dense financial-statement filings, no management
    # commentary) when both land near the same date -- confirmed
    # empirically: NVIDIA files both the same day, and only the 8-K's
    # accession folder holds the actual press release/CFO-commentary
    # exhibits.
    form_priority = {"8-K": 0, "6-K": 0, "10-Q": 1, "20-F": 1}
    candidates = []
    for i, form in enumerate(forms):
        if form not in form_priority:
            continue
        try:
            filing_date = dt.date.fromisoformat(dates[i])
        except (ValueError, IndexError):
            continue
        age = abs((filing_date - report_date).days)
        if age <= 10:
            candidates.append((form_priority[form], age, i, filing_date))
    if not candidates:
        return None
    candidates.sort()
    _, _, idx, filing_date = candidates[0]

    accession_nodash = accessions[idx].replace("-", "")
    base_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/"
    docs = _edgar_filing_exhibits(cik, accession_nodash, headers, exclude_name=primary_docs[idx])
    if not docs:
        # No separate exhibit found (e.g. a 10-Q is often just the cover +
        # the statements themselves, no distinct exhibit) -- fall back to
        # the primary document itself.
        text = _edgar_fetch_text(base_url + primary_docs[idx], headers)[:6000]
        label = f"SEC EDGAR {forms[idx]} filed {filing_date.isoformat()} ({base_url + primary_docs[idx]})"
        return text, label

    combined = "\n\n".join(f"--- {name} ---\n{text[:5000]}" for name, text in docs)
    label = (f"SEC EDGAR {forms[idx]} filed {filing_date.isoformat()}, exhibits: "
             f"{', '.join(name for name, _ in docs)} ({base_url})")
    return combined, label


def _fetch_article_text(url):
    """Best-effort plain-text fetch of an ordinary public news page -- a
    failure here (paywall, odd markup, timeout) just means less grounding
    text, never a hard error."""
    if not url:
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": cfg.USER_AGENT},
                             timeout=cfg.HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        import lxml.html
        return " ".join(lxml.html.fromstring(resp.content).text_content().split())
    except Exception:  # noqa: BLE001 -- best-effort only
        return None


def _news_grounding(item):
    """Google News RSS grounding for non-US tickers (and the fallback for
    any US ticker EDGAR couldn't resolve) -- the closest free source
    available, NOT the primary exchange filing (see module docstring for
    why no other exchange here offers one for free). Fetches headlines plus
    the top 2 linked articles' page text, best-effort.

    Uses the same quoted-phrase query and company-name title filter as
    _check_reported_via_news -- confirmed by testing to matter: an
    unquoted/unfiltered search surfaced a different company entirely
    (Agnico Eagle Mines, ticker AEM, for a query about AEM Holdings), which
    would otherwise feed the write-up stage wrong-company "sourced
    material" to hallucinate from."""
    entries = _google_news_entries(_news_query(item["company"]), max_results=8)
    entries = [e for e in entries if _title_matches_company(e["title"], item["company"])]
    if not entries:
        return None
    lines = [f'- "{e["title"]}" ({e["source"]}, {e["published"].isoformat()}) {e["url"]}'
             for e in entries]
    article_texts = []
    for e in entries[:2]:
        text = _fetch_article_text(e["url"])
        if text:
            article_texts.append(f'From "{e["title"]}" ({e["source"]}):\n{text[:3000]}')
    grounding = "Recent headlines:\n" + "\n".join(lines)
    if article_texts:
        grounding += "\n\nArticle excerpts:\n" + "\n\n".join(article_texts)
    label = f"Google News search ({len(entries)} headlines, {len(article_texts)} article(s) fetched)"
    return grounding, label


def _gather_grounding(item):
    """Returns (grounding_text, source_label). US-listed tickers try SEC
    EDGAR first (the genuine primary filing); everyone else, and any EDGAR
    miss, falls back to Google News. Never raises -- a total miss returns a
    clear "nothing found" marker, which config.ANALYSIS_INSTRUCTIONS'
    write-up prompt is already instructed to render as NA/unavailable
    rather than invented content."""
    if cfg.is_edgar_ticker(item["ticker"]):
        try:
            result = _edgar_filing_grounding(item)
        except Exception:  # noqa: BLE001
            result = None
        if result:
            return result
    try:
        result = _news_grounding(item)
    except Exception:  # noqa: BLE001
        result = None
    if result:
        return result
    return ("No grounding material found via SEC EDGAR or Google News for this release.",
            "none found")


def _groq_client():
    """Its OWN key (GROQ_API_KEY_EARNINGS), a separate Groq account from
    the GROQ_API_KEY major_news_alert.py uses -- confirmed necessary by a
    real 429 during testing: sharing one account's 200k tokens/day
    free-tier cap with major_news_alert.py's much more frequent polling
    meant this pipeline regularly got nothing left of the quota."""
    from groq import Groq
    return Groq(api_key=os.environ["GROQ_API_KEY_EARNINGS"])


def _run_completion(prompt, max_tokens):
    """One Groq chat-completion call, retried with backoff on a 429 --
    same shape as major_news_engine._run_completion, same free-tier model/
    account this repo's major_news_alert.py already uses."""
    from groq import RateLimitError

    client = _groq_client()
    for attempt in range(cfg.LLM_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=cfg.LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content
        except RateLimitError:
            if attempt >= cfg.LLM_RATE_LIMIT_MAX_RETRIES:
                raise
            _time.sleep(cfg.LLM_RATE_LIMIT_RETRY_SECONDS * (attempt + 1))


def _run_write_stage(item, snapshot_text, grounding_text, source_label):
    prompt = (
        f"{cfg.ANALYSIS_INSTRUCTIONS}\n\n"
        f"--- QUANTITATIVE SNAPSHOT (yfinance) ---\n{snapshot_text}\n\n"
        f"--- SOURCED MATERIAL ({source_label}) ---\n{grounding_text}\n\n"
        f"Write the full report now, following the ROLE/SECTION structure above exactly. "
        f"Use the quantitative snapshot for the hard numbers and the sourced material for "
        f"quotes/guidance/tone/reaction. Ground every claim strictly in the material given "
        f"above -- do not invent quotes, dates, or figures that are not present in it. If the "
        f"sourced material says something is unavailable, follow the instructions' fallback "
        f"for that case (e.g. Section 3)."
    )
    return _run_completion(prompt, cfg.LLM_WRITE_MAX_TOKENS)


def _run_verify_stage(item, grounding_text, draft_text):
    prompt = (
        f"You are fact-checking an equity analysis draft before it ships. Below is the sourced "
        f"material it was supposed to be grounded in, and the draft itself.\n\n"
        f"--- SOURCED MATERIAL ---\n{grounding_text}\n\n"
        f"--- DRAFT ---\n{draft_text}\n\n"
        f"Cross-check every quote and figure in the draft against the sourced material. If "
        f"something in the draft is not actually supported by the sourced material, correct it "
        f"or strike it (don't invent a fix) -- flag it inline as [UNVERIFIED: ...] rather than "
        f"silently deleting it, so the reader knows what to double check. Do not soften any "
        f"flagged deterioration while correcting.\n\n"
        f"Output the corrected full report, then on its own line write exactly "
        f"'{cfg.DIGEST_MARKER}' followed by a Telegram-ready digest in 4-6 short lines, "
        f"figures only, no opinions or judgment calls: ticker/company, revenue and net profit "
        f"growth (yoy, quantified), beat/met/missed vs street (quantified), the price reaction "
        f"if available (or 'reaction not yet available'), and one standout figure from the "
        f"Financial Metrics section (e.g. the one-off-adjusted growth rate, or the operating "
        f"leverage numbers). Keep the digest under {cfg.TELEGRAM_DIGEST_MAX_CHARS} characters "
        f"total, plain text, no markdown headers."
    )
    text = _run_completion(prompt, cfg.LLM_VERIFY_MAX_TOKENS)
    if cfg.DIGEST_MARKER in text:
        full_report, digest = text.split(cfg.DIGEST_MARKER, 1)
    else:
        full_report, digest = text, text[:cfg.TELEGRAM_DIGEST_MAX_CHARS]
    return full_report.strip(), digest.strip()


def analyze_ticker(item):
    """Runs the free grounding + 2-call Groq pipeline for one
    newly-reported ticker. Returns (full_report_text, digest_text) or None
    if any stage failed (logged, not raised -- a bad ticker must not sink
    the rest of the cycle)."""
    try:
        snapshot = gather_financial_snapshot(item["ticker"], item["report_date"])
        snapshot_text = _snapshot_to_text(item, snapshot)
        grounding_text, source_label = _gather_grounding(item)
        draft_text = _run_write_stage(item, snapshot_text, grounding_text, source_label)
        full_report, digest = _run_verify_stage(item, grounding_text, draft_text)
        return full_report, digest
    except Exception:  # noqa: BLE001 -- one bad ticker must not sink the run
        print(f"ERROR analyzing {item['ticker']}:")
        traceback.print_exc()
        return None


def save_report(item, full_report_text):
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)
    fname = f"{item['ticker'].replace('.', '_')}_{item['report_date'].isoformat()}.md"
    path = os.path.join(cfg.OUTPUT_DIR, fname)
    header = (
        f"# {item['company']} ({item['ticker']}) -- earnings analysis\n\n"
        f"Reported: {item['report_date'].isoformat()}  |  Group: {item['group']}  |  "
        f"Generated: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n---\n\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + full_report_text + "\n")
    return path


def run_test_ticker(ticker, company=None, group="Test"):
    """Diagnostic only: forces the full grounding + Groq pipeline for one
    ticker, bypassing detection and state entirely -- lets you see real
    report quality before trusting the daily cron. Never touches
    state.json, never saves a report file, never sends to Telegram; the
    caller (earnings_analysis.py --test-ticker) just prints what comes
    back. report_date is whatever yfinance's most recent reported-earnings
    row says (today, if that's unavailable too) -- it's only prompt
    context here, not compared against anything."""
    latest = _latest_reported_row(ticker)
    report_date = latest[0] if latest else dt.date.today()
    eps_est, reported_eps, surprise_pct = latest[1:] if latest else (None, None, None)
    item = {
        "ticker": ticker, "company": company or ticker, "group": group,
        "report_date": report_date, "eps_estimate": eps_est,
        "reported_eps": reported_eps, "surprise_pct": surprise_pct,
    }
    outcome = analyze_ticker(item)
    if outcome is None:
        return None
    full_report, digest = outcome
    return item, full_report, digest


def run_cycle(dry_run=False):
    """Returns a list of {"item", "digest", "report_path"} for every ticker
    successfully analyzed this cycle (empty if nothing new reported, or
    dry_run). Both detection paths (yfinance and the Google News fallback)
    are free, so both always run, even during --dry-run -- only the Groq
    write/verify pipeline, report saving, Telegram send, and state.json
    save are skipped for --dry-run. State is saved exactly once at the end,
    after all tickers have been checked, so a crash partway through a long
    run doesn't leave state half-written."""
    watchlist = load_watchlist()
    print(f"Loaded {len(watchlist)} watchlist tickers across {len(cfg.WATCHLIST_FILES)} groups.")

    state = load_state()
    new_reports, undetected = detect_new_reports(watchlist, state)
    print(f"{len(new_reports)} newly reported since last run (via yfinance): "
          f"{', '.join(r['ticker'] for r in new_reports) or '(none)'}")

    if undetected:
        extra_reports = detect_new_reports_via_news(undetected, state)
        print(f"{len(undetected)} ticker(s) with no yfinance earnings data at all "
              f"(checked via Google News instead): {', '.join(i['ticker'] for i in undetected)}")
        if extra_reports:
            print(f"  -> {len(extra_reports)} of those newly reported: "
                  f"{', '.join(r['ticker'] for r in extra_reports)}")
        new_reports += extra_reports

    if dry_run:
        print("Dry run: not calling the Groq pipeline and not saving state.")
        return []

    results = []
    tickers_state = state.setdefault("tickers", {})
    for item in new_reports:
        print(f"Analyzing {item['ticker']} ({item['company']})...")
        outcome = analyze_ticker(item)
        if outcome is None:
            print(f"  {item['ticker']} failed -- will retry next run (last_analyzed_date not "
                  f"advanced).")
            continue
        full_report, digest = outcome
        report_path = save_report(item, full_report)
        results.append({"item": item, "digest": digest, "report_path": report_path})
        # Only a SUCCESSFUL analysis advances last_analyzed_date -- see
        # _apply_detection's docstring for why a failure must not.
        tickers_state[item["ticker"]]["last_analyzed_date"] = item["report_date"].isoformat()

    save_state(state)
    return results
