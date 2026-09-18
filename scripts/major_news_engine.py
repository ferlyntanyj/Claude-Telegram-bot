"""
Core logic for the global major-news Telegram alert. One call to run_cycle()
is one poll cycle.

Price-first (redesigned 2026-09-18): every cycle, scan_watchlist_moves()
batch-checks the ENTIRE curated watchlist (data/global_watchlist.csv, built
by global_watchlist_build.py) for price moves, rather than scanning
headlines first and trying to match them to watchlist names. The prior
headline-first design spent most of a session getting patched for exactly
the failure mode this replaces: a real move only got caught if a matching
headline was also seen AND correctly parsed (missed: Fujitsu -- headline
lacked finance vocabulary; Malaysia/Indonesia -- thin news feeds; a broad
semis selloff -- keyword list too narrow; Taiwan/Delta Electronics -- market
not even in the watchlist yet). Scanning price data directly makes detection
independent of whether any headline was ever found at all; a headline is now
only *looked up afterwards*, per qualifying mover, for display/grounding.

Two independent paths per cycle, both ending in the same alert shape:
  - Single-stock: any watchlist ticker whose move clears
    cfg.SINGLE_STOCK_MOVE_PCT. same_group_peers() finds up to
    cfg.MAX_DISPLAY_PEERS other watchlist names in the same industry
    (from the SAME scan, no extra fetch) purely for display.
  - Sector-wide: scanned movers are grouped by the watchlist's `industry`
    column; a group's MEDIAN move (not a plain average -- see
    major_news_alert_config's docstring) or breadth decides whether it
    qualifies, and the group's single biggest mover becomes the "primary"
    line. This replaces an LLM-inferred peer basket with real
    classification data -- deterministic, free, and not dependent on a
    sector-keyword prefilter ever matching.

Every alert therefore carries: headline, key (cooldown), metric_pct (the
number should_alert()/cooldown compares against), market_name, primary
({ticker, company, pct, last, prev, currency}), peers (up to
MAX_DISPLAY_PEERS {ticker, company, pct} dicts), and -- once write_analysis()
runs -- analysis ({sentiment, why_moved, read_across, look_out, memory}).
write_analysis() is the ONLY LLM (Groq) call left anywhere in the pipeline,
once per qualifying alert -- infer_peers() and its sector-keyword prefilter
are gone, which also removes the root cause behind a prior bug where
sector-candidate LLM calls fired before their own cooldown check and burned
through Groq's free-tier daily quota.

A headline is still useful context, just not the trigger: _find_headline()
does a targeted Google News search (gather_related_headlines(), reused from
the old sector-grounding step) per qualifying mover/industry, for the
Telegram card and as LLM grounding. If nothing turns up, the alert still
fires with a clear placeholder rather than being suppressed -- suppressing a
verified, real price move for lack of a headline was exactly the old
design's structural weakness.

State (data/alert_state.json) is a flat map of alert key -> {last_alert_utc,
last_move_pct}, used only to avoid re-sending the same story every cycle
(should_alert below), plus a top-level last_run_utc for observability.
"""
import datetime as dt
import json
import os
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import brief_engine

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------
def _norm(text):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_watchlist(cfg):
    """{ticker: {company_name, sector, industry, currency}} for the whole
    curated watchlist. No more name/alias regex-pattern building -- price
    scanning keys directly off the ticker, and detection no longer depends
    on matching a headline's text against a company name at all."""
    try:
        df = pd.read_csv(cfg.WATCHLIST_CSV_PATH)
    except FileNotFoundError:
        print(f"ERROR: {cfg.WATCHLIST_CSV_PATH} not found. Run global_watchlist_build.py first.",
              file=sys.stderr)
        return {}

    watchlist = {}
    for _, r in df.iterrows():
        ticker = str(r["ticker"]).strip()
        watchlist[ticker] = {
            "company_name": str(r["company_name"]).strip(),
            "sector": str(r.get("sector") or "").strip(),
            "industry": str(r.get("industry") or "").strip(),
            "currency": str(r.get("currency") or "").strip(),
        }
    return watchlist


# ---------------------------------------------------------------------------
# Market name -- resolved from the ticker's own suffix rather than threaded
# through from the watchlist, so it works uniformly for watchlist hits AND
# for arbitrary tickers the LLM names as peers (which may not be on the
# curated watchlist at all).
# ---------------------------------------------------------------------------
_EXCHANGE_BY_SUFFIX = {
    ".T": "Japan · TSE", ".HK": "Hong Kong · HKEX", ".KS": "South Korea · KRX",
    ".SI": "Singapore · SGX", ".AX": "Australia · ASX", ".L": "UK · LSE",
    ".DE": "Germany · XETRA", ".PA": "France · Euronext Paris",
    ".AS": "Netherlands · Euronext Amsterdam", ".SW": "Switzerland · SIX",
    ".TO": "Canada · TSX", ".NS": "India · NSE", ".BO": "India · BSE",
    ".SS": "China · Shanghai", ".SZ": "China · Shenzhen", ".MI": "Italy · Borsa Italiana",
    ".KL": "Malaysia · Bursa Malaysia", ".JK": "Indonesia · IDX", ".BK": "Thailand · SET",
    ".VN": "Vietnam · HOSE", ".TW": "Taiwan · TWSE", ".TWO": "Taiwan · TPEx",
}


def market_name_for_ticker(ticker):
    if "." not in ticker:
        return "US · NYSE/Nasdaq"
    suffix = "." + ticker.rsplit(".", 1)[-1]
    return _EXCHANGE_BY_SUFFIX.get(suffix, f"International · {suffix.lstrip('.')}")


# ---------------------------------------------------------------------------
# Price scan -- the trigger itself now, not a lookup for a headline-matched
# handful of tickers. Has to cover the FULL ~1,400-name watchlist every
# cycle, which the old per-ticker check_price_moves() (one yf.Ticker()
# .fast_info call per name, sequential) was far too slow and rate-limit-
# fragile for at this volume -- uses chunked yf.download() batching instead
# (same idea as global_watchlist_build._download_fx_batch).
#
# A per-ticker fast_info-based rewrite was tried on 2026-09-18 in the belief
# that lastPrice/previousClose would "self-correct" to ~0% while a market is
# closed, unlike a daily-bar comparison -- verified false and reverted:
# fast_info's regularMarketPreviousClose (the field that actually matches
# real historical closes; bare previousClose does NOT and would have
# silently suppressed genuine moves) gives the IDENTICAL number to the
# daily-bar approach. There is no bug in this calculation -- "Intel is
# +7.67% versus its last real close" stays true and unchanged for as long as
# nothing has traded since that close, which is simply how prices work. See
# _tradeable_now() below for the actual fix to the real issue (WHEN a true,
# accurate figure like that should be allowed to trigger a new alert).
# ---------------------------------------------------------------------------
def _scan_chunk(chunk):
    import yfinance as yf

    data = yf.download(chunk, period="5d", interval="1d", group_by="ticker",
                        threads=True, progress=False)
    resolved, unresolved = {}, []
    for ticker in chunk:
        try:
            closes = data[ticker]["Close"].dropna() if len(chunk) > 1 else data["Close"].dropna()
            if len(closes) < 2:
                unresolved.append(ticker)
                continue
            prev, last = float(closes.iloc[-2]), float(closes.iloc[-1])
            if prev == 0:
                continue
            resolved[ticker] = {"last": last, "prev": prev, "pct": (last / prev - 1.0) * 100.0}
        except Exception:  # noqa: BLE001 -- one bad ticker must not sink the chunk
            unresolved.append(ticker)
    return resolved, unresolved


def scan_watchlist_moves(watchlist, cfg):
    """Returns {ticker: {last, prev, pct, currency}} for whatever resolves.
    Chunks the watchlist, retries a chunk's still-unresolved tickers (not
    the whole chunk) across up to cfg.PRICE_SCAN_MAX_PASSES passes with a
    cooldown, same shape as global_watchlist_build.enrich_with_market_cap --
    anything still unresolved after that is just skipped this cycle (picked
    up again next cycle), not fatal. Currency comes from the watchlist CSV
    itself (already captured at build time), not re-fetched here."""
    import time as _time

    tickers = list(watchlist.keys())
    chunk_size = cfg.PRICE_SCAN_CHUNK_SIZE
    pending = [tickers[i:i + chunk_size] for i in range(0, len(tickers), chunk_size)]
    results = {}

    for attempt in range(cfg.PRICE_SCAN_MAX_PASSES):
        still_missing = []
        for chunk in pending:
            try:
                resolved, unresolved = _scan_chunk(chunk)
            except Exception as e:  # noqa: BLE001 -- one bad chunk must not sink the scan
                print(f"  price scan: chunk download failed ({e}), will retry", file=sys.stderr)
                unresolved, resolved = chunk, {}
            results.update(resolved)
            if unresolved:
                still_missing.append(unresolved)
        pending = still_missing
        if not pending:
            break
        if attempt < cfg.PRICE_SCAN_MAX_PASSES - 1:
            _time.sleep(cfg.PRICE_SCAN_RETRY_COOLDOWN_SECONDS)

    for ticker, move in results.items():
        move["currency"] = watchlist[ticker].get("currency") or None
    print(f"  price scan: resolved {len(results)}/{len(tickers)} tickers")
    return results


# ---------------------------------------------------------------------------
# Trading-hours gate -- the actual fix for a real move (e.g. Intel's genuine
# +7.67% Sept 17 regular-session close) surfacing as a "new" alert hours
# into the FOLLOWING closed-market gap (confirmed 2026-09-18: fired at 08:02
# UTC, hours before NYSE's 13:30 UTC/9:30am ET open). The move itself was
# never wrong -- a stock's price relative to its last close is genuinely
# constant until new trading occurs, that's not a data bug. The real problem
# is that our own poll+cooldown timing has no relationship to when that
# market is actually live, so a real-but-day-old number can get surfaced at
# an arbitrary, misleadingly "breaking-news-looking" moment. Gating
# qualification (not the scan itself, which still needs every ticker's data
# for peer display) to "this ticker's home market is currently in its
# regular session" ties alert timing back to genuine market activity.
#
# Lunch breaks (several Asian markets) and holidays are NOT modeled -- a
# known simplification. The goal is "don't alert 5+ hours before/after a
# market's own session," not minute-perfect calendar accuracy.
# ---------------------------------------------------------------------------
_EXCHANGE_SESSIONS = {
    "": (dt.time(9, 30), dt.time(16, 0), "America/New_York"),        # bare ticker = US
    ".T": (dt.time(9, 0), dt.time(15, 0), "Asia/Tokyo"),
    ".HK": (dt.time(9, 30), dt.time(16, 0), "Asia/Hong_Kong"),
    ".KS": (dt.time(9, 0), dt.time(15, 30), "Asia/Seoul"),
    ".SI": (dt.time(9, 0), dt.time(17, 0), "Asia/Singapore"),
    ".AX": (dt.time(10, 0), dt.time(16, 0), "Australia/Sydney"),
    ".L": (dt.time(8, 0), dt.time(16, 30), "Europe/London"),
    ".DE": (dt.time(9, 0), dt.time(17, 30), "Europe/Berlin"),
    ".SS": (dt.time(9, 30), dt.time(15, 0), "Asia/Shanghai"),
    ".SZ": (dt.time(9, 30), dt.time(15, 0), "Asia/Shanghai"),
    ".KL": (dt.time(9, 0), dt.time(17, 0), "Asia/Kuala_Lumpur"),
    ".JK": (dt.time(9, 0), dt.time(15, 0), "Asia/Jakarta"),
    ".BK": (dt.time(10, 0), dt.time(16, 30), "Asia/Bangkok"),
    ".VN": (dt.time(9, 0), dt.time(15, 0), "Asia/Ho_Chi_Minh"),
    ".TW": (dt.time(9, 0), dt.time(13, 30), "Asia/Taipei"),
    ".TWO": (dt.time(9, 0), dt.time(13, 30), "Asia/Taipei"),
}


def _tradeable_now(ticker, now_utc):
    """Best-effort check: is ticker's home exchange currently inside its
    regular Mon-Fri trading session? Unknown suffix -> assumed tradeable
    (fails open, matching this module's general bias toward not silently
    dropping a real move over being maximally precise about market
    calendars)."""
    import zoneinfo

    suffix = "." + ticker.rsplit(".", 1)[-1] if "." in ticker else ""
    session = _EXCHANGE_SESSIONS.get(suffix)
    if session is None:
        return True
    open_t, close_t, tz_name = session
    local = now_utc.astimezone(zoneinfo.ZoneInfo(tz_name))
    return local.weekday() < 5 and open_t <= local.time() <= close_t


# ---------------------------------------------------------------------------
# US pre-market -- a deliberately narrow, bounded extension (2026-09-18) of
# the trading-hours gate above, not a general extended-hours feature. Scoped
# to the US only: it's the market this was verified against (Intel's real
# live pre-market move, +3.03% on top of its already-known regular-session
# close), and the only one confirmed to expose reliable pre/post-market data
# via yfinance at all. Deliberately regular-hours-only for every other
# market and for US after-hours too -- extending this further was
# considered and explicitly declined (thin extended-hours liquidity elsewhere
# makes the moves noisier, and checking .info -- a much heavier call than
# fast_info or a batched daily-bar download -- for every watchlist name
# every cycle isn't worth it for signal this marginal). Only runs the extra
# per-ticker fetch during the ~5.5h US pre-market window itself, not on
# every cycle of the day, keeping the added cost bounded to when it's
# actually useful.
# ---------------------------------------------------------------------------
_US_PREMARKET_OPEN = dt.time(4, 0)
_US_PREMARKET_CLOSE = dt.time(9, 30)


def _is_us_premarket_now(now_utc):
    import zoneinfo

    local = now_utc.astimezone(zoneinfo.ZoneInfo("America/New_York"))
    return local.weekday() < 5 and _US_PREMARKET_OPEN <= local.time() < _US_PREMARKET_CLOSE


def _fetch_premarket_move(ticker):
    """Only trusts Yahoo's own marketState=='PRE' confirmation rather than
    just checking whether preMarketPrice is present -- .info can carry a
    stale preMarketPrice field outside the actual pre-market window."""
    import time as _time

    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    for attempt in range(2):
        try:
            info = yf.Ticker(ticker).info
            if info.get("marketState") != "PRE":
                return ticker, None
            last = info.get("preMarketPrice")
            prev = info.get("regularMarketPrice")
            pct = info.get("preMarketChangePercent")
            if last is None or prev is None or pct is None:
                return ticker, None
            return ticker, {"last": float(last), "prev": float(prev), "pct": float(pct)}
        except YFRateLimitError:
            if attempt == 0:
                _time.sleep(3)
                continue
            return ticker, None
        except Exception:  # noqa: BLE001 -- one bad ticker must not sink the scan
            return ticker, None
    return ticker, None


def scan_premarket_moves(watchlist, cfg):
    """Same shape and retry pattern as scan_watchlist_moves, but restricted
    to US tickers (bare, no suffix) and only meaningful to call during
    _is_us_premarket_now() -- see that function and the section comment
    above for why."""
    import time as _time

    us_tickers = [t for t in watchlist if "." not in t]
    results = {}
    pending = list(us_tickers)
    workers = cfg.PREMARKET_SCAN_MAX_WORKERS
    for pass_num in range(cfg.PRICE_SCAN_MAX_PASSES):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_premarket_move, t): t for t in pending}
            for fut in as_completed(futures):
                ticker, move = fut.result()
                if move is not None:
                    results[ticker] = move
        pending = [t for t in pending if t not in results]
        if not pending:
            break
        if pass_num < cfg.PRICE_SCAN_MAX_PASSES - 1:
            _time.sleep(cfg.PRICE_SCAN_RETRY_COOLDOWN_SECONDS)
            workers = max(3, workers - 2)

    for ticker, move in results.items():
        move["currency"] = watchlist[ticker].get("currency") or None
    print(f"  pre-market scan: {len(results)}/{len(us_tickers)} US tickers actually in pre-market")
    return results


def _group_by_industry(watchlist, scanned_moves):
    """{industry: {ticker: move}} -- tickers with no industry classification
    (a stale watchlist row, or yfinance simply had none for that name) are
    skipped rather than lumped into one meaningless catch-all group."""
    groups = {}
    for ticker, move in scanned_moves.items():
        industry = watchlist.get(ticker, {}).get("industry")
        if not industry:
            continue
        groups.setdefault(industry, {})[ticker] = move
    return groups


def _median_breadth(moves, cfg):
    """moves: {ticker: {pct, ...}}. Same median/breadth math the old
    LLM-peer-basket evaluate_sector_move used, now fed a real industry
    group instead -- median rather than a plain average so one outlier
    can't drag a large group over the line."""
    signed = [m["pct"] for m in moves.values()]
    median_pct = statistics.median(signed)
    dominant_sign = 1 if median_pct >= 0 else -1
    concordant_at_bar = sum(1 for m in signed if (m * dominant_sign) >= cfg.SECTOR_BREADTH_MOVE_PCT)
    breadth_share_pct = 100.0 * concordant_at_bar / len(signed)
    qualifies = (
        abs(median_pct) >= cfg.SECTOR_MEDIAN_MOVE_PCT
        or breadth_share_pct >= cfg.SECTOR_BREADTH_SHARE_PCT
    )
    return {"median_pct": median_pct, "breadth_share_pct": breadth_share_pct, "qualifies": qualifies}


def same_group_peers(ticker, industry, watchlist, scanned_moves, max_count):
    """Other watchlist names in the same industry that also moved this
    cycle, ranked by |move| descending -- replaces the old LLM-guessed peer
    basket (infer_peers/top_peer_moves) with something deterministic and
    free, computed from data already fetched for the scan itself. Limited
    to names already on the curated watchlist, unlike an LLM's broader
    real-world knowledge of true competitors -- a known, accepted trade-off
    for consistency and zero extra cost."""
    if not industry:
        return []
    peers = [
        {"ticker": t, "company": watchlist[t]["company_name"], "pct": m["pct"]}
        for t, m in scanned_moves.items()
        if t != ticker and watchlist.get(t, {}).get("industry") == industry
    ]
    peers.sort(key=lambda p: -abs(p["pct"]))
    return peers[:max_count]


def _llm_client():
    from groq import Groq
    return Groq()  # reads GROQ_API_KEY from the environment


def _run_completion(prompt, cfg, max_tokens, json_object=False):
    """One Groq chat-completion call, retried with backoff on a 429 -- the
    price-first redesign's very first cycle surfaced 52 qualifying alerts at
    once (every cooldown key was new), which burned through Groq's free-tier
    limits (verified via their docs 2026-09-15: 30 RPM, 8,000 TPM) almost
    immediately and failed 60/66 write-ups that cycle. A burst that size is
    mostly a one-time cold-start effect (cooldown suppresses repeats for 18h
    after), but a genuinely volatile day could still produce a similar burst
    in steady state -- this alone can't fully absorb that (a sustained burst
    still exceeds TPM even paced), but combined with the pacing in run_cycle
    it meaningfully reduces how often a transient rate limit turns into a
    dropped write-up. Raises on any other failure (missing key, bad
    response) -- callers decide how to degrade, same defensive shape as the
    rest of this module."""
    import time as _time

    from groq import RateLimitError

    client = _llm_client()
    extra = {"response_format": {"type": "json_object"}} if json_object else {}
    for attempt in range(cfg.LLM_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=cfg.LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                **extra,
            )
            return resp.choices[0].message.content
        except RateLimitError:
            if attempt >= cfg.LLM_RATE_LIMIT_MAX_RETRIES:
                raise
            _time.sleep(cfg.LLM_RATE_LIMIT_RETRY_SECONDS * (attempt + 1))


def _extract_json_object(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object found in LLM response: {text[:200]!r}")
    return json.loads(match.group(0))


# ---------------------------------------------------------------------------
# Related-coverage gathering -- reuses the same
# Google News RSS search brief_engine already uses, just with the qualifying
# headline's own text as the query and a much looser source bar, since this
# is grounding context for the LLM to synthesize from, not the primary
# trust-gated trigger. Added 2026-09-14 per the user's request to have a
# wide sell-off checked against more than a single headline: "use news
# first... try to find more information online" -- this is that, done with
# the free infrastructure already in place rather than a paid search API.
# ---------------------------------------------------------------------------
def gather_related_headlines(query_text, cfg, max_results=5, lookback_hours=48):
    import urllib.parse

    hl, gl, ceid = cfg.GOOGLE_NEWS_LOCALE
    url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(query_text)}"
           f"&hl={hl}&gl={gl}&ceid={ceid}")
    try:
        feed = brief_engine._parse_feed(url, cfg.HTTP_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 -- context-gathering must not sink the alert
        print(f"  related-headline search failed: {e}", file=sys.stderr)
        return []
    if not feed:
        return []

    now = dt.datetime.now(UTC)
    seen = set()
    results = []
    for entry in feed.entries:
        title = (getattr(entry, "title", "") or "").strip()
        if not title:
            continue
        aged = brief_engine._entry_age_hours(entry, now)
        if aged is None:
            continue
        age_h, published = aged
        if age_h > lookback_hours:
            continue
        source_name = brief_engine._source_name(entry, "Google News")
        weight = cfg.SOURCE_WEIGHTS.get(brief_engine._norm_source(source_name), cfg.DEFAULT_SOURCE_WEIGHT)
        if weight < cfg.MIN_SOURCE_WEIGHT:
            continue
        clean_title = brief_engine._TRAIL_SOURCE_RE.sub("", title).strip()
        key = _norm(clean_title)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "title": clean_title,
            "source": source_name,
            "url": getattr(entry, "link", "") or "",
            "published": published.isoformat(),
        })
        if len(results) >= max_results:
            break
    return results


_NO_HEADLINE_TITLE = "(no specific news story found — price move only)"


def _find_headline(query_text, cfg):
    """Per-mover targeted lookup -- now that detection no longer depends on
    having already seen a matching headline, this reuses the same Google
    News search (gather_related_headlines) queried by the specific
    company/industry name instead of the triggering headline's own text.
    Always returns a usable headline dict, with a clear placeholder if
    nothing turns up, so a real, verified price move is never suppressed
    just because no story was found -- the whole point of this redesign."""
    related = gather_related_headlines(query_text, cfg, max_results=1)
    if related:
        r = related[0]
        return {"title": r["title"], "source": r["source"], "url": r["url"], "published": r["published"]}
    return {
        "title": _NO_HEADLINE_TITLE, "source": "price data", "url": "",
        "published": dt.datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Structured analysis write-up (Groq) -- six named sections rather than one
# blob, so the Telegram render can lay them out under fixed headers.
# ---------------------------------------------------------------------------
_ANALYSIS_UNAVAILABLE = "(unavailable — LLM call failed; see logs.)"
_NO_MEMORY = "No clear historical parallel comes to mind."
_ANALYSIS_FIELDS = ("business_model", "sentiment", "why_moved", "read_across", "look_out", "memory")


def write_analysis(alert, cfg):
    """Returns {business_model, sentiment, why_moved, read_across, look_out,
    memory}, all strings. On any failure returns the same shape with a clear
    unavailable marker in each field rather than raising -- a bad LLM call
    must not drop the alert itself, only degrade its commentary.

    business_model added 2026-09-18 per user request -- price-first
    detection surfaces far more, and far less familiar, names than the old
    headline-matching design ever did (small/mid-caps across 14 markets,
    not just the companies that happened to make trusted-source news), so a
    reader often has no idea what the primary mover even does.

    Sector-wide alerts get extra grounding: gather_related_headlines() pulls
    a handful of other recent headlines about the same story (not just the
    one that triggered the alert) so the write-up -- especially "memory",
    which is otherwise just the model's own recall -- has more than one
    data point to reason from. Single-stock alerts skip this (one extra
    Google News round-trip per alert isn't worth it when the story is
    already anchored to one specific, named company)."""
    headline = alert["headline"]
    primary = alert["primary"]
    peer_desc = "; ".join(
        f'{p["company"]} ({p["ticker"]}) {p["pct"]:+.1f}%' for p in alert["peers"]
    ) or "none identified"

    context_block = ""
    if alert["type"] == "sector":
        related = gather_related_headlines(headline["title"], cfg)
        if related:
            lines = "\n".join(f'- "{r["title"]}" ({r["source"]})' for r in related)
            context_block = f"\nOther recent coverage of this story:\n{lines}\n"

    prompt = (
        f'Headline: "{headline["title"]}" (source: {headline["source"]})\n'
        f"{context_block}\n"
        f'Primary mover: {primary["company"]} ({primary["ticker"]}) {primary["pct"]:+.1f}% '
        f'intraday (last {primary["last"]:.2f} {primary.get("currency") or ""}, '
        f'prior close {primary["prev"]:.2f}).\n'
        f"Peer/read-across candidates and their moves: {peer_desc}\n\n"
        "Respond with ONLY a JSON object, no other text, with exactly these six "
        "string keys:\n"
        "{\n"
        f'  "business_model": "1-2 plain-English sentences on what {primary["company"]} '
        'actually does -- its core business/revenue driver, not history or stock '
        'performance -- for a reader who has never heard of the company",\n'
        '  "sentiment": "One word, either Bullish, Bearish, Mixed, or Neutral, then '
        "a dash then a reason clause under 12 words -- for example: "
        'Bearish - investors reassessing AI capex growth assumptions",\n'
        '  "why_moved": "1-2 sentences on why the price moved, specific to this news '
        '(use the other coverage above if given, not just the single headline)",\n'
        '  "read_across": "1-2 sentences on what this means for domestic/international '
        'peers, supply chain, or the wider industry",\n'
        '  "look_out": "1-2 sentences on what to watch for next -- an upcoming event, '
        'data point, or follow-through risk",\n'
        '  "memory": "1-2 sentences on whether something like this -- this kind of '
        'shock, or this specific company/sector under similar pressure -- has happened '
        f'before, and how it played out, or exactly this sentence if nothing solid comes '
        f'to mind: \'{_NO_MEMORY}\' -- do not force a weak or vague analogy"\n'
        "}\n\n"
        "Be specific and concrete, avoid generic filler, no preamble."
    )
    try:
        text = _run_completion(prompt, cfg, cfg.LLM_ANALYSIS_MAX_TOKENS, json_object=True)
        data = _extract_json_object(text)
        result = {f: str(data.get(f) or "").strip() for f in _ANALYSIS_FIELDS}
        if not result["memory"]:
            result["memory"] = _NO_MEMORY
        for f in _ANALYSIS_FIELDS:
            if f != "memory" and not result[f]:
                result[f] = _ANALYSIS_UNAVAILABLE
        return result
    except Exception as e:  # noqa: BLE001
        print(f"  LLM analysis write-up failed: {e}", file=sys.stderr)
        return {f: _ANALYSIS_UNAVAILABLE for f in _ANALYSIS_FIELDS}


# ---------------------------------------------------------------------------
# Dedup / cooldown state
# ---------------------------------------------------------------------------
def load_state(cfg):
    try:
        with open(cfg.STATE_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"alerts": {}}


def save_state(state, cfg):
    os.makedirs(os.path.dirname(cfg.STATE_JSON_PATH), exist_ok=True)
    with open(cfg.STATE_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def should_alert(state, key, cfg, cooldown_hours=None):
    """One alert per story/ticker per cooldown_hours (defaults to
    cfg.COOLDOWN_HOURS), full stop -- no same-day re-alert on a deepening
    move, even a large one. Confirmed 2026-09-14: the old same-day
    delta-escalation bypass (re-alert if the move deepened by
    RE_ALERT_DELTA_PCT further) was exactly what caused Fujitsu and Applied
    Materials to each fire multiple times in one day as they drifted further
    past the threshold intraday -- unwanted noise, not a feature.
    COOLDOWN_HOURS is set long enough to span a full trading day, so a fresh
    move the next day still alerts normally once it expires.

    The override lets a distinct key namespace (e.g. "premarket:{ticker}",
    added 2026-09-18) run its own, shorter cooldown independent of that
    ticker's regular single:{ticker} cooldown -- a pre-market move and its
    later regular-session confirmation are different signals worth tracking
    separately, not one blocking the other."""
    entry = state["alerts"].get(key)
    if entry is None:
        return True
    last_time = dt.datetime.fromisoformat(entry["last_alert_utc"])
    hours_since = (dt.datetime.now(UTC) - last_time).total_seconds() / 3600.0
    return hours_since >= (cfg.COOLDOWN_HOURS if cooldown_hours is None else cooldown_hours)


def _record_alert(state, alert):
    state["alerts"][alert["key"]] = {
        "last_alert_utc": dt.datetime.now(UTC).isoformat(),
        "last_move_pct": alert["metric_pct"],
    }


# ---------------------------------------------------------------------------
# Audit log -- output/ (unlike data/, this is meant to be human-browsable /
# committed, same convention as the other briefs' output/*.md and *.json)
# ---------------------------------------------------------------------------
_LOG_MAX_ENTRIES = 2000


def _log_entry(alert):
    return {
        "alert_utc": dt.datetime.now(UTC).isoformat(),
        "type": alert["type"],
        "headline": alert["headline"]["title"],
        "source": alert["headline"]["source"],
        "url": alert["headline"]["url"],
        "market_name": alert["market_name"],
        "primary": alert["primary"],
        "peers": alert["peers"],
        "analysis": alert.get("analysis"),
    }


def append_log(alerts, cfg):
    if not alerts:
        return
    try:
        with open(cfg.OUT_LOG_JSON_PATH, "r", encoding="utf-8") as f:
            log = json.load(f)
    except FileNotFoundError:
        log = []
    log.extend(_log_entry(a) for a in alerts)
    log = log[-_LOG_MAX_ENTRIES:]
    os.makedirs(os.path.dirname(cfg.OUT_LOG_JSON_PATH), exist_ok=True)
    with open(cfg.OUT_LOG_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------
def run_cycle(cfg):
    """Evaluate one poll cycle and return (alerts, state). Does NOT persist
    anything -- the caller must call mark_sent() for each alert Telegram
    actually delivered, then persist() once, so a failed send doesn't burn
    the cooldown on a story the user never actually received."""
    watchlist = load_watchlist(cfg)
    state = load_state(cfg)
    now = dt.datetime.now(UTC)
    state["last_run_utc"] = now.isoformat()

    scanned = scan_watchlist_moves(watchlist, cfg)
    # Qualification only looks at tickers whose home market is currently in
    # its regular session (_tradeable_now) -- a move's size relative to its
    # last close is real and correct even while that market is shut, but
    # letting it qualify a NEW alert at an arbitrary point during the
    # following closed-market gap is what actually needs fixing (see that
    # function's docstring). scanned (unfiltered) is still used below for
    # peer DISPLAY, so a qualifying alert can still show a same-industry
    # name whose own market happens to be closed right now.
    tradeable = {t: m for t, m in scanned.items() if _tradeable_now(t, now)}

    alerts = []

    # -- Single-stock: any watchlist ticker that itself cleared the bar -----
    for ticker, move in tradeable.items():
        if abs(move["pct"]) < cfg.SINGLE_STOCK_MOVE_PCT:
            continue
        key = f"single:{ticker}"
        if not should_alert(state, key, cfg):
            continue
        info = watchlist[ticker]
        headline = _find_headline(info["company_name"], cfg)
        peers = same_group_peers(ticker, info["industry"], watchlist, scanned, cfg.MAX_DISPLAY_PEERS)
        alerts.append({
            "type": "single_stock", "headline": headline, "key": key,
            "metric_pct": move["pct"],
            "market_name": market_name_for_ticker(ticker),
            "primary": {
                "ticker": ticker, "company": info["company_name"], "pct": move["pct"],
                "last": move["last"], "prev": move["prev"], "currency": move.get("currency"),
            },
            "peers": peers,
        })

    # -- US pre-market: a live, still-forming move, not a regular-session ---
    # one -- only checked during the pre-market window itself (see
    # scan_premarket_moves's docstring for the cost reasoning), and tracked
    # under its own "premarket:{ticker}" cooldown key so it doesn't block
    # (or get blocked by) that ticker's regular single:{ticker} alert.
    if _is_us_premarket_now(now):
        premarket = scan_premarket_moves(watchlist, cfg)
        for ticker, move in premarket.items():
            if abs(move["pct"]) < cfg.SINGLE_STOCK_MOVE_PCT:
                continue
            key = f"premarket:{ticker}"
            if not should_alert(state, key, cfg, cooldown_hours=cfg.PREMARKET_COOLDOWN_HOURS):
                continue
            info = watchlist[ticker]
            headline = _find_headline(info["company_name"], cfg)
            peers = same_group_peers(ticker, info["industry"], watchlist, scanned, cfg.MAX_DISPLAY_PEERS)
            alerts.append({
                "type": "single_stock", "headline": headline, "key": key,
                "metric_pct": move["pct"],
                "market_name": f"{market_name_for_ticker(ticker)} (pre-market)",
                "primary": {
                    "ticker": ticker, "company": info["company_name"], "pct": move["pct"],
                    "last": move["last"], "prev": move["prev"], "currency": move.get("currency"),
                },
                "peers": peers,
            })

    # -- Sector-wide: group this cycle's movers by real industry tag -------
    # Grouped from `tradeable`, not `scanned` -- a group spans many markets
    # at once (e.g. "Semiconductors" mixes US/Taiwan/Japan/Korea names), so
    # without this a story could "qualify" on the strength of several
    # already-closed markets' stale prior-session moves rather than
    # anything actually live right now.
    for industry, moves in _group_by_industry(watchlist, tradeable).items():
        if len(moves) < cfg.SECTOR_MIN_PEERS:
            continue
        result = _median_breadth(moves, cfg)
        if not result["qualifies"]:
            continue
        # The group has no single "subject" the way a single-stock ticker
        # does -- use its biggest mover as the primary line, and the rest
        # (up to MAX_DISPLAY_PEERS) as the peers list. Median/breadth can
        # qualify a group even when no single member has cleared 5% itself
        # (confirmed 2026-09-18: Petronas Chemical at -3.29%, Symrise at
        # -2.51% both surfaced as a sector alert's "primary" mover) -- per
        # user request, only show a sector story whose biggest mover is
        # itself a genuine >=5% move, same bar as a single-stock alert.
        ranked = sorted(moves.items(), key=lambda kv: -abs(kv[1]["pct"]))
        top_ticker, top_move = ranked[0]
        if abs(top_move["pct"]) < cfg.SINGLE_STOCK_MOVE_PCT:
            continue
        key = f"sector:{_norm(industry)}"
        if not should_alert(state, key, cfg):
            continue
        peers = [
            {"ticker": t, "company": watchlist[t]["company_name"], "pct": m["pct"]}
            for t, m in ranked[1:1 + cfg.MAX_DISPLAY_PEERS]
        ]
        headline = _find_headline(f"{industry} stocks", cfg)
        alerts.append({
            "type": "sector", "headline": headline, "key": key,
            "metric_pct": result["median_pct"],
            "market_name": market_name_for_ticker(top_ticker),
            "primary": {
                "ticker": top_ticker, "company": watchlist[top_ticker]["company_name"],
                "pct": top_move["pct"], "last": top_move["last"], "prev": top_move["prev"],
                "currency": top_move.get("currency"),
            },
            "peers": peers,
            "median_pct": result["median_pct"],
            "breadth_share_pct": result["breadth_share_pct"],
        })

    # Paced, not back-to-back -- Groq's free tier is 8,000 tokens/minute
    # (verified via their docs), tighter than it sounds once you count each
    # call's ~700-token max output plus its prompt: more than ~6-8 calls in
    # a minute reliably 429s. A quiet cycle (the common case, cooldown
    # suppresses repeats for 18h) pays nothing extra; a busy one spends time
    # here rather than silently degrading most of its write-ups.
    import time as _time
    for i, alert in enumerate(alerts):
        if i > 0:
            _time.sleep(cfg.LLM_CALL_PACING_SECONDS)
        alert["analysis"] = write_analysis(alert, cfg)

    print(f"Qualifying alert(s) this cycle: {len(alerts)}")
    return alerts, state


def mark_sent(alert, state):
    """Call once an alert has actually been delivered to Telegram -- this is
    what starts its cooldown. Never call this for an alert whose send failed."""
    _record_alert(state, alert)


def persist(state, sent_alerts, cfg):
    """Save cooldown state and append the audit log -- call once per cycle,
    after all sends have been attempted, with only the alerts that mark_sent
    was called for."""
    save_state(state, cfg)
    append_log(sent_alerts, cfg)
