"""
Core logic for the global major-news Telegram alert. One call to run_cycle()
is one poll cycle: fetch headlines, check which watchlist names they moved (or
ask Claude which peers a thematic/sector headline likely moved), evaluate
against major_news_alert_config's thresholds, dedup/cooldown against prior
alerts, and (for whatever survives) write a short significance/outlook blurb.

Headline fetching reuses brief_engine.fetch_news as-is -- same Google News RSS
+ direct-feed + source-trust-ranking + de-dup machinery as the scheduled
briefs, just pointed at major_news_alert_config's broader, non-sector-specific
queries.

Two independent paths per headline:
  - Single-stock: the headline names a company already on the curated
    data/global_watchlist.csv (>= USD 5B market cap, built by
    global_watchlist_build.py). Its live intraday move is checked directly --
    no LLM call needed.
  - Sector-wide: the headline looks thematic/macro (SECTOR_TRIGGER_KEYWORDS)
    rather than naming one company. Claude is asked which large-cap peers are
    most likely affected; their MEDIAN move (not a plain average -- see
    major_news_alert_config's docstring) decides whether it qualifies.

State (data/alert_state.json) is a flat map of alert key -> {last_alert_utc,
last_move_pct}, used only to avoid re-sending the same story every cycle
(dedup_and_cooldown / should_alert below).
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
_CORP_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "plc", "group", "holdings", "holding", "nv", "sa", "ag", "se", "llc", "lp", "spa", "kk",
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
# Sector-peer inference (Claude) + evaluation
# ---------------------------------------------------------------------------
def _looks_sector_worthy(title, cfg):
    low = title.lower()
    return any(kw in low for kw in cfg.SECTOR_TRIGGER_KEYWORDS)


def _llm_client():
    import anthropic
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment


def _extract_json_array(text):
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON array found in LLM response: {text[:200]!r}")
    return json.loads(match.group(0))


def infer_sector_peers(title, cfg):
    """Ask Claude which large-cap peers a thematic headline likely moved.
    Returns [(ticker, company), ...], empty on any failure -- a missing API
    key or a bad response must not crash the whole poll cycle."""
    prompt = (
        f'Headline: "{title}"\n\n'
        "List 4 to 8 publicly traded, large-cap (market cap at least USD 5 billion) "
        "companies most directly and immediately affected by this news -- the ones "
        "whose share price would plausibly move because of it. Use tickers exactly as "
        'they appear on Yahoo Finance (e.g. "AAPL", "7203.T", "005930.KS", "0700.HK", "BP.L").\n\n'
        "Respond with ONLY a JSON array, no other text, in this exact form:\n"
        '[{"ticker": "AAPL", "company": "Apple Inc"}, ...]'
    )
    try:
        client = _llm_client()
        resp = client.messages.create(
            model=cfg.LLM_MODEL,
            max_tokens=cfg.LLM_PEER_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _extract_json_array(resp.content[0].text)
        return [(d["ticker"].strip(), d.get("company", d["ticker"]).strip())
                for d in data if d.get("ticker")]
    except Exception as e:  # noqa: BLE001
        print(f"  LLM peer-inference failed: {e}", file=sys.stderr)
        return []


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
# Significance write-up (Claude)
# ---------------------------------------------------------------------------
def write_significance(alert, cfg):
    headline = alert["headline"]
    if alert["type"] == "single_stock":
        m = alert["move"]
        move_desc = (
            f'{alert["company"]} ({alert["ticker"]}) moved {m["pct"]:+.1f}% intraday '
            f'(last {m["last"]:.2f} {m.get("currency") or ""}, prior close {m["prev"]:.2f}).'
        )
    else:
        result = alert["result"]
        top = sorted(result["moves"].items(), key=lambda kv: -abs(kv[1]["pct"]))[:6]
        detail = "; ".join(
            f'{result["peer_names"].get(t, t)} ({t}) {m["pct"]:+.1f}%' for t, m in top
        )
        move_desc = (
            f'Peer basket median move {result["median_pct"]:+.1f}% '
            f'({result["breadth_share_pct"]:.0f}% of peers moving together): {detail}'
        )

    prompt = (
        f'Headline: "{headline["title"]}" (source: {headline["source"]})\n\n'
        f"Price reaction: {move_desc}\n\n"
        "In 2-4 concise sentences: explain why this news is significant for markets/"
        "investors, and what specifically to watch for next (e.g. an upcoming event, "
        "data point, follow-through risk, or read-through to other names). Be specific "
        "and avoid generic filler. No preamble, just the analysis."
    )
    try:
        client = _llm_client()
        resp = client.messages.create(
            model=cfg.LLM_MODEL,
            max_tokens=cfg.LLM_SIGNIFICANCE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:  # noqa: BLE001
        print(f"  LLM significance write-up failed: {e}", file=sys.stderr)
        return "(Significance write-up unavailable — LLM call failed; see logs.)"


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


def should_alert(state, key, move_pct, cfg):
    entry = state["alerts"].get(key)
    if entry is None:
        return True
    last_time = dt.datetime.fromisoformat(entry["last_alert_utc"])
    hours_since = (dt.datetime.now(UTC) - last_time).total_seconds() / 3600.0
    if hours_since >= cfg.COOLDOWN_HOURS:
        return True
    return abs(move_pct) - abs(entry["last_move_pct"]) >= cfg.RE_ALERT_DELTA_PCT


def _record_alert(state, alert):
    move_pct = (
        alert["move"]["pct"] if alert["type"] == "single_stock"
        else alert["result"]["median_pct"]
    )
    state["alerts"][alert["key"]] = {
        "last_alert_utc": dt.datetime.now(UTC).isoformat(),
        "last_move_pct": move_pct,
    }


# ---------------------------------------------------------------------------
# Audit log -- output/ (unlike data/, this is meant to be human-browsable /
# committed, same convention as the other briefs' output/*.md and *.json)
# ---------------------------------------------------------------------------
_LOG_MAX_ENTRIES = 2000


def _log_entry(alert):
    entry = {
        "alert_utc": dt.datetime.now(UTC).isoformat(),
        "type": alert["type"],
        "headline": alert["headline"]["title"],
        "source": alert["headline"]["source"],
        "url": alert["headline"]["url"],
        "significance": alert.get("significance"),
    }
    if alert["type"] == "single_stock":
        entry["ticker"] = alert["ticker"]
        entry["company"] = alert["company"]
        entry["move_pct"] = round(alert["move"]["pct"], 2)
    else:
        entry["median_move_pct"] = round(alert["result"]["median_pct"], 2)
        entry["breadth_share_pct"] = round(alert["result"]["breadth_share_pct"], 1)
        entry["peers"] = alert["result"]["peer_names"]
    return entry


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
    lookback_hours = (cfg.POLL_INTERVAL_MINUTES + cfg.LOOKBACK_OVERLAP_MINUTES) / 60.0

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
                if not should_alert(state, key, move["pct"], cfg):
                    continue
                company = next((c for t, c in matched if t == ticker), ticker)
                alerts.append({
                    "type": "single_stock", "headline": item, "ticker": ticker,
                    "company": company, "move": move, "key": key,
                })
        elif _looks_sector_worthy(item["title"], cfg):
            peers = infer_sector_peers(item["title"], cfg)
            if len(peers) < cfg.SECTOR_MIN_PEERS:
                continue
            result = evaluate_sector_move(peers, cfg)
            if not result or not result["qualifies"]:
                continue
            key = f"sector:{norm[:80]}"
            if not should_alert(state, key, result["median_pct"], cfg):
                continue
            alerts.append({"type": "sector", "headline": item, "result": result, "key": key})

    for alert in alerts:
        alert["significance"] = write_significance(alert, cfg)

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
