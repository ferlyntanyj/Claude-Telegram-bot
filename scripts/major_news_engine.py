"""
Core logic for the global major-news Telegram alert. One call to run_cycle()
is one poll cycle: fetch headlines, check which watchlist names they moved (or
ask Groq which peers a thematic/sector headline likely moved), evaluate
against major_news_alert_config's thresholds, dedup/cooldown against prior
alerts, and (for whatever survives) write a structured analysis.

Headline fetching reuses brief_engine.fetch_news as-is -- same Google News RSS
+ direct-feed + source-trust-ranking + de-dup machinery as the scheduled
briefs, just pointed at major_news_alert_config's broader, non-sector-specific
queries.

Two independent paths per headline, both ending in the same alert shape:
  - Single-stock: the headline names a company already on the curated
    data/global_watchlist.csv (>= USD 5B market cap, built by
    global_watchlist_build.py). Its live intraday move (no LLM needed) sets
    the "primary" mover; infer_peers() then finds up to
    cfg.MAX_DISPLAY_PEERS read-across names purely for display.
  - Sector-wide: the headline looks thematic/macro (SECTOR_TRIGGER_KEYWORDS)
    rather than naming one company. infer_peers() finds the likely-affected
    large-caps; their MEDIAN move (not a plain average -- see
    major_news_alert_config's docstring) decides whether it qualifies, and
    the single biggest mover in that basket becomes the "primary" line.

Every alert therefore carries: headline, key (cooldown), metric_pct (the
number should_alert()/cooldown compares against), market_name, primary
({ticker, company, pct, last, prev, currency}), peers (up to
MAX_DISPLAY_PEERS {ticker, company, pct} dicts), and -- once write_analysis()
runs -- analysis ({sentiment, why_moved, read_across, look_out, memory}).

State (data/alert_state.json) is a flat map of alert key -> {last_alert_utc,
last_move_pct}, used only to avoid re-sending the same story every cycle
(dedup_and_cooldown / should_alert below), plus a top-level last_run_utc used
by effective_lookback_hours().
"""
import datetime as dt
import json
import os
import re
import statistics
import sys

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


# Legal-entity suffixes stripped off company_name to get the name headlines
# actually use -- "Apple Inc." never appears in a headline, "Apple" does.
# "berhad"/"bhd" (Malaysia), "tbk"/"persero" (Indonesia) added 2026-09-14 when
# the watchlist expanded to those markets -- confirmed via a live example:
# "CIMB Group Holdings announces deal" didn't match "CIMB GROUP HOLDINGS
# BERHAD" until "berhad" was added here.
_CORP_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "plc", "group", "holdings", "holding", "nv", "sa", "ag", "se", "llc", "lp", "spa", "kk",
    "berhad", "bhd", "tbk", "persero",
}
# Short names that collide with common English words -- kept as full-legal-name
# matches only (never as the bare short form), to cut false positives.
_AMBIGUOUS_SHORT_NAMES = {"target", "gap", "block", "match", "square", "chart"}


def _short_name(norm_name):
    tokens = norm_name.split()
    while tokens and tokens[-1] in _CORP_SUFFIXES:
        tokens.pop()
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    return " ".join(tokens)


def load_watchlist(cfg):
    try:
        df = pd.read_csv(cfg.WATCHLIST_CSV_PATH)
    except FileNotFoundError:
        print(f"ERROR: {cfg.WATCHLIST_CSV_PATH} not found. Run global_watchlist_build.py first.",
              file=sys.stderr)
        return []

    rows = []
    for _, r in df.iterrows():
        aliases = [a.strip() for a in str(r.get("aliases") or "").split("|") if a.strip()]
        names = [str(r["company_name"]).strip()] + aliases

        match_terms = set()
        for name in names:
            norm_name = _norm(name)
            if len(norm_name) >= 4:
                match_terms.add(norm_name)
            short = _short_name(norm_name)
            if len(short) >= 4 and short not in _AMBIGUOUS_SHORT_NAMES:
                match_terms.add(short)

        patterns = [re.compile(r"\b" + re.escape(term) + r"\b") for term in match_terms]
        if not patterns:
            continue
        rows.append({
            "ticker": str(r["ticker"]).strip(),
            "company_name": str(r["company_name"]).strip(),
            "patterns": patterns,
        })
    return rows


def match_watchlist(title, watchlist):
    """Return [(ticker, company_name), ...] for every watchlist row named in title."""
    norm_title = _norm(title)
    matches = []
    for row in watchlist:
        if any(p.search(norm_title) for p in row["patterns"]):
            matches.append((row["ticker"], row["company_name"]))
    return matches


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
# Price checks
# ---------------------------------------------------------------------------
def check_price_moves(tickers):
    """Return {ticker: {last, prev, pct, currency}} -- skips any ticker that
    fails to resolve (bad symbol, no trade today, data outage), same
    defensive pattern as brief_engine.fetch_market. One retry on a Yahoo
    rate-limit response (single-ticker fast_info calls hit that fairly easily
    when run back-to-back)."""
    import time as _time

    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    results = {}
    for ticker in tickers:
        for attempt in range(2):
            try:
                fi = yf.Ticker(ticker).fast_info
                # FastInfo's real keys are camelCase (lastPrice/previousClose);
                # snake_case fallback kept in case a future yfinance version changes it.
                last = fi.get("lastPrice") or fi.get("last_price")
                prev = (fi.get("previousClose") or fi.get("previous_close")
                        or fi.get("regularMarketPreviousClose"))
                if last is None or prev is None or prev == 0:
                    break
                pct = (float(last) / float(prev) - 1.0) * 100.0
                results[ticker] = {
                    "last": float(last), "prev": float(prev), "pct": pct,
                    "currency": fi.get("currency"),
                }
                break
            except YFRateLimitError:
                if attempt == 0:
                    _time.sleep(3)
                    continue
                print(f"  price check: rate-limited on {ticker}, skipping", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"  price check: skipping {ticker} ({e})", file=sys.stderr)
                break
    return results


# ---------------------------------------------------------------------------
# Peer inference (Groq) + evaluation. Used for BOTH paths now: sector-wide
# stories use it to find the peer basket that decides qualification; single-
# stock stories use it purely for read-across display (up to
# cfg.MAX_DISPLAY_PEERS peers shown alongside the primary mover).
# ---------------------------------------------------------------------------
# Catches "[anything] stocks/shares slump/tumble/plunge/..." regardless of
# what the "[anything]" sector/theme actually is -- a literal keyword list
# (SECTOR_TRIGGER_KEYWORDS) can only ever cover topics someone thought to
# enumerate in advance (tariffs, rate decisions, ...), and confirmed missed a
# real ~8-name >5% semiconductor selloff on 2026-09-14 driven by an "AI
# safety" narrative: "AI-linked stocks slump after top lab CEOs call for
# slowing technology's development" matched no topic keyword and named no
# single company, so it fell through both detection paths entirely. This
# regex is a general shape check, not a topic list, so it doesn't have that
# blind spot.
_STOCK_MOVE_VERB_RE = re.compile(
    r"\b(stocks?|shares?)\b[^.]{0,25}\b(slump\w*|tumbl\w*|plung\w*|sink|sinks|sank|sunk|"
    r"fall\w*|fell|drop\w*|slid\w*|surg\w*|soar\w*|rall(?:y|ies|ied)|jump\w*|sell[- ]?off)\b",
    re.I,
)


def _looks_sector_worthy(title, cfg):
    low = title.lower()
    if any(kw in low for kw in cfg.SECTOR_TRIGGER_KEYWORDS):
        return True
    return bool(_STOCK_MOVE_VERB_RE.search(title))


def _llm_client():
    from groq import Groq
    return Groq()  # reads GROQ_API_KEY from the environment


def _run_completion(prompt, cfg, max_tokens, json_object=False):
    """One Groq chat-completion call. Raises on any failure (missing key, bad
    response) -- callers decide how to degrade, same defensive shape as the
    rest of this module."""
    client = _llm_client()
    extra = {"response_format": {"type": "json_object"}} if json_object else {}
    resp = client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        **extra,
    )
    return resp.choices[0].message.content


def _extract_json_array(text):
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON array found in LLM response: {text[:200]!r}")
    return json.loads(match.group(0))


def _extract_json_object(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object found in LLM response: {text[:200]!r}")
    return json.loads(match.group(0))


def infer_peers(title, cfg):
    """Ask Groq which large-cap peers/companies a headline is most relevant
    to. Returns [(ticker, company), ...], empty on any failure -- a missing
    API key or a bad response must not crash the whole poll cycle."""
    prompt = (
        f'Headline: "{title}"\n\n'
        "List 4 to 8 publicly traded, large-cap (market cap at least USD 5 billion) "
        "companies most directly and immediately affected by this news -- the ones "
        "whose share price would plausibly move because of it (competitors, supply "
        "chain, or the company the headline is about). Use tickers exactly as they "
        'appear on Yahoo Finance (e.g. "AAPL", "7203.T", "005930.KS", "0700.HK", "BP.L").\n\n'
        "Respond with ONLY a JSON array, no other text, in this exact form:\n"
        '[{"ticker": "AAPL", "company": "Apple Inc"}, ...]'
    )
    try:
        text = _run_completion(prompt, cfg, cfg.LLM_PEER_MAX_TOKENS)
        data = _extract_json_array(text)
        return [(d["ticker"].strip(), d.get("company", d["ticker"]).strip())
                for d in data if d.get("ticker")]
    except Exception as e:  # noqa: BLE001
        print(f"  LLM peer-inference failed: {e}", file=sys.stderr)
        return []


def top_peer_moves(peer_pairs, exclude_ticker, max_count):
    """peer_pairs: [(ticker, company), ...] (e.g. from infer_peers). Prices
    them, drops exclude_ticker (the primary mover, if it's in the list) and
    anything that fails to price, and returns up to max_count
    {ticker, company, pct} dicts sorted by |move| descending."""
    peer_pairs = [(t, c) for t, c in peer_pairs if t != exclude_ticker]
    peer_names = dict(peer_pairs)
    moves = check_price_moves([t for t, _ in peer_pairs])
    rows = [
        {"ticker": t, "company": peer_names.get(t, t), "pct": m["pct"]}
        for t, m in moves.items()
    ]
    rows.sort(key=lambda r: -abs(r["pct"]))
    return rows[:max_count]


def evaluate_sector_move(peers, cfg):
    """peers: [(ticker, company), ...]. Returns None if too few peers priced,
    else a dict with median_pct, breadth_share_pct, qualifies, moves, peer_names."""
    peer_names = dict(peers)
    moves = check_price_moves([t for t, _ in peers])
    if len(moves) < cfg.SECTOR_MIN_PEERS:
        return None

    signed = [m["pct"] for m in moves.values()]
    median_pct = statistics.median(signed)
    dominant_sign = 1 if median_pct >= 0 else -1
    concordant_at_bar = sum(
        1 for m in signed if (m * dominant_sign) >= cfg.SECTOR_BREADTH_MOVE_PCT
    )
    breadth_share_pct = 100.0 * concordant_at_bar / len(signed)

    qualifies = (
        abs(median_pct) >= cfg.SECTOR_MEDIAN_MOVE_PCT
        or breadth_share_pct >= cfg.SECTOR_BREADTH_SHARE_PCT
    )
    return {
        "median_pct": median_pct,
        "breadth_share_pct": breadth_share_pct,
        "qualifies": qualifies,
        "moves": moves,
        "peer_names": peer_names,
    }


# ---------------------------------------------------------------------------
# Related-coverage gathering (sector-wide alerts only) -- reuses the same
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
        age_h, _ = aged
        if age_h > lookback_hours:
            continue
        clean_title = brief_engine._TRAIL_SOURCE_RE.sub("", title).strip()
        key = _norm(clean_title)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "title": clean_title,
            "source": brief_engine._source_name(entry, "Google News"),
        })
        if len(results) >= max_results:
            break
    return results


# ---------------------------------------------------------------------------
# Structured analysis write-up (Groq) -- five named sections rather than one
# blob, so the Telegram render can lay them out under fixed headers.
# ---------------------------------------------------------------------------
_ANALYSIS_UNAVAILABLE = "(unavailable — LLM call failed; see logs.)"
_NO_MEMORY = "No clear historical parallel comes to mind."
_ANALYSIS_FIELDS = ("sentiment", "why_moved", "read_across", "look_out", "memory")


def write_analysis(alert, cfg):
    """Returns {sentiment, why_moved, read_across, look_out, memory}, all
    strings. On any failure returns the same shape with a clear unavailable
    marker in each field rather than raising -- a bad LLM call must not drop
    the alert itself, only degrade its commentary.

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
        "Respond with ONLY a JSON object, no other text, with exactly these five "
        "string keys:\n"
        "{\n"
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


def effective_lookback_hours(state, cfg):
    """How far back to fetch headlines this cycle. GitHub Actions doesn't
    honor the cron's nominal cadence for this account tier (runs land 2-6+
    hours apart in practice, not every POLL_INTERVAL_MINUTES) -- so rather
    than assume a fixed gap and silently miss whatever aged out of the
    scheduler's queue in between, look back to whenever the last run actually
    completed, capped at cfg.MAX_LOOKBACK_HOURS."""
    floor_hours = (cfg.POLL_INTERVAL_MINUTES + cfg.LOOKBACK_OVERLAP_MINUTES) / 60.0
    last_run = state.get("last_run_utc")
    if not last_run:
        return floor_hours
    gap_hours = (dt.datetime.now(UTC) - dt.datetime.fromisoformat(last_run)).total_seconds() / 3600.0
    return min(cfg.MAX_LOOKBACK_HOURS, max(floor_hours, gap_hours))


def should_alert(state, key, cfg):
    """One alert per story/ticker per COOLDOWN_HOURS, full stop -- no
    same-day re-alert on a deepening move, even a large one. Confirmed
    2026-09-14: the old same-day delta-escalation bypass (re-alert if the
    move deepened by RE_ALERT_DELTA_PCT further) was exactly what caused
    Fujitsu and Applied Materials to each fire multiple times in one day as
    they drifted further past the threshold intraday -- unwanted noise, not
    a feature. COOLDOWN_HOURS is set long enough to span a full trading day,
    so a fresh move the next day still alerts normally once it expires."""
    entry = state["alerts"].get(key)
    if entry is None:
        return True
    last_time = dt.datetime.fromisoformat(entry["last_alert_utc"])
    hours_since = (dt.datetime.now(UTC) - last_time).total_seconds() / 3600.0
    return hours_since >= cfg.COOLDOWN_HOURS


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
    lookback_hours = effective_lookback_hours(state, cfg)
    state["last_run_utc"] = dt.datetime.now(UTC).isoformat()

    sections, total = brief_engine.fetch_news(cfg, lookback_hours)
    candidates = [item for items in sections.values() for item in items]
    print(f"Fetched {total} candidate headline(s) (lookback {lookback_hours:.2f}h).")

    alerts = []
    seen = set()
    for item in candidates:
        norm = _norm(item["title"])
        if norm in seen:
            continue
        seen.add(norm)

        matched = match_watchlist(item["title"], watchlist)
        if matched:
            moves = check_price_moves([t for t, _ in matched])
            for ticker, move in moves.items():
                if abs(move["pct"]) < cfg.SINGLE_STOCK_MOVE_PCT:
                    continue
                key = f"single:{ticker}"
                if not should_alert(state, key, cfg):
                    continue
                company = next((c for t, c in matched if t == ticker), ticker)
                peer_pairs = infer_peers(item["title"], cfg)
                peers = top_peer_moves(peer_pairs, exclude_ticker=ticker,
                                        max_count=cfg.MAX_DISPLAY_PEERS)
                alerts.append({
                    "type": "single_stock", "headline": item, "key": key,
                    "metric_pct": move["pct"],
                    "market_name": market_name_for_ticker(ticker),
                    "primary": {
                        "ticker": ticker, "company": company, "pct": move["pct"],
                        "last": move["last"], "prev": move["prev"],
                        "currency": move.get("currency"),
                    },
                    "peers": peers,
                })
        elif _looks_sector_worthy(item["title"], cfg):
            # Cooldown gated *before* the Groq call, not after -- a
            # sector-worthy headline typically sits in the lookback window
            # for many hours and reappears in every ~15-minute cycle, so
            # checking should_alert() only after infer_peers() (as this used
            # to) burned a real LLM call every single cycle on a story
            # that's already in cooldown and gets discarded moments later.
            # Confirmed 2026-09-18 as the likely driver of Groq free-tier
            # quota exhaustion (200k tokens/day) causing later, genuinely
            # new alerts' write_analysis() calls to fail that same day.
            key = f"sector:{norm[:80]}"
            if not should_alert(state, key, cfg):
                continue
            peer_pairs = infer_peers(item["title"], cfg)
            if len(peer_pairs) < cfg.SECTOR_MIN_PEERS:
                continue
            result = evaluate_sector_move(peer_pairs, cfg)
            if not result or not result["qualifies"]:
                continue
            # The peer basket has no single "subject" the way a single-stock
            # headline does -- use its biggest mover as the primary line, and
            # the rest (up to MAX_DISPLAY_PEERS) as the peers list.
            ranked = sorted(result["moves"].items(), key=lambda kv: -abs(kv[1]["pct"]))
            top_ticker, top_move = ranked[0]
            peers = [
                {"ticker": t, "company": result["peer_names"].get(t, t), "pct": m["pct"]}
                for t, m in ranked[1:1 + cfg.MAX_DISPLAY_PEERS]
            ]
            alerts.append({
                "type": "sector", "headline": item, "key": key,
                "metric_pct": result["median_pct"],
                "market_name": market_name_for_ticker(top_ticker),
                "primary": {
                    "ticker": top_ticker, "company": result["peer_names"].get(top_ticker, top_ticker),
                    "pct": top_move["pct"], "last": top_move["last"], "prev": top_move["prev"],
                    "currency": top_move.get("currency"),
                },
                "peers": peers,
                "median_pct": result["median_pct"],
                "breadth_share_pct": result["breadth_share_pct"],
            })

    for alert in alerts:
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
