"""
Build the weekday US-market morning brief -- a concise "what happened overnight"
digest delivered to Telegram at 08:30 Asia/Singapore (00:30 UTC), after the US
cash session and after-hours have closed.

Deterministic, no LLM. Two halves:

  1. Scoreboard -- index / vol / rates / commodity / FX / crypto / futures moves
     from Yahoo Finance (yfinance), last close vs the prior close.
  2. Headlines -- pulled from Google News RSS search (`when:1d`) plus a handful of
     publisher feeds (CNBC sections, the Fed), filtered to the overnight window,
     dropped unless they carry a US-market-relevant term, de-duplicated across
     sources, ranked by source trust + recency (listicles / promo / opinion
     buried, some patterns dropped outright), and sorted into four blocks:
     Markets / Macro, Fed & data / Stocks & earnings / Geopolitics. An optional
     best-effort X (Nitter) pull is appended when a live instance can be reached,
     and silently skipped otherwise.

Outputs:
  output/morning_brief.md   -- human-readable, committed by the workflow as an archive
  output/morning_brief.json -- structured, consumed by send_morning_brief_telegram.py

Tuning knobs live in morning_brief_config.py.
"""
import calendar
import datetime as dt
import html
import json
import re
import sys
import urllib.parse

import feedparser
import requests

import morning_brief_config as cfg

OUT_MD_PATH = "../output/morning_brief.md"
OUT_JSON_PATH = "../output/morning_brief.json"

SGT = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": cfg.USER_AGENT})


# ---------------------------------------------------------------------------
# Market scoreboard
# ---------------------------------------------------------------------------
def fetch_market():
    """Return (rows, asof_note). rows: list of dicts with move already computed."""
    import yfinance as yf

    symbols = [s for _, _, s in cfg.MARKET_TICKERS]
    data = yf.download(
        symbols, period="7d", interval="1d", group_by="ticker",
        progress=False, auto_adjust=False, threads=True,
    )

    rows = []
    index_dates = []
    for group, label, sym in cfg.MARKET_TICKERS:
        try:
            closes = data[sym]["Close"].dropna()
            if len(closes) < 2:
                raise ValueError("need two closes")
            last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            asof = closes.index[-1].date()
        except Exception as e:  # noqa: BLE001 -- one bad symbol must not sink the brief
            print(f"  market: skipping {label} ({sym}): {e}", file=sys.stderr)
            continue

        if group == "index":
            index_dates.append(asof)

        if group == "rates":
            # ^TNX etc. are already in percent; report the move in basis points.
            bps = round((last - prev) * 100.0)
            move = "flat" if bps == 0 else f"{bps:+d} bps"
            rows.append({
                "group": group, "label": label, "symbol": sym,
                "value": f"{last:.2f}%", "move": move,
                "direction": _dir(bps), "asof": asof.isoformat(),
            })
        else:
            pct = (last / prev - 1.0) * 100.0
            value = f"{last:,.0f}" if last >= 100 else f"{last:,.2f}"
            move = "flat" if abs(pct) < 0.005 else f"{pct:+.2f}%"
            rows.append({
                "group": group, "label": label, "symbol": sym,
                "value": value, "move": move,
                "direction": _dir(round(pct, 2)), "asof": asof.isoformat(),
            })

    asof_note = ""
    if index_dates:
        latest = max(index_dates)
        today_utc = dt.datetime.now(UTC).date()
        # The brief runs ~08:30 SGT; the relevant US close is "yesterday" in UTC
        # terms most of the year. If the freshest index bar is older than that,
        # a US holiday (or a data lag) is in play -- flag it rather than show 0.00%.
        if (today_utc - latest).days > 1:
            asof_note = f"US markets last traded {latest.isoformat()} (holiday or data lag)."
    return rows, asof_note


def _dir(delta):
    if delta > 0:
        return "up"
    if delta < 0:
        return "down"
    return "flat"


# ---------------------------------------------------------------------------
# Headlines
# ---------------------------------------------------------------------------
_GN_BASE = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
_TRAIL_SOURCE_RE = re.compile(r"\s+-\s+[^-]+$")  # strip Google News' " - Publisher" tail
_NORM_RE = re.compile(r"[^a-z0-9 ]+")


def _norm_title(title):
    t = _TRAIL_SOURCE_RE.sub("", title).lower()
    t = _NORM_RE.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def _entry_age_hours(entry, now):
    tm = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not tm:
        return None
    published = dt.datetime.fromtimestamp(calendar.timegm(tm), UTC)
    return (now - published).total_seconds() / 3600.0, published


_SOURCE_SUFFIX_RE = re.compile(r"\.(com|org|net|co\.uk|co|io|news)$", re.I)


def _norm_source(name):
    n = (name or "").strip()
    n = re.sub(r"^The\s+", "", n, flags=re.I)
    n = _SOURCE_SUFFIX_RE.sub("", n)
    return n.strip()


def _source_name(entry, feed_name):
    src = getattr(entry, "source", None)
    if src is not None and getattr(src, "title", None):
        return src.title
    return feed_name


def _source_weight(name):
    return cfg.SOURCE_WEIGHTS.get(_norm_source(name), cfg.DEFAULT_SOURCE_WEIGHT)


_SECTION_RES = {
    section: re.compile(
        r"\b(?:" + "|".join(re.escape(k.strip()) for k in kws) + r")\b", re.I
    )
    for section, kws in cfg.SECTION_KEYWORDS.items()
}


def _classify(title):
    for section, rx in _SECTION_RES.items():
        if rx.search(title):
            return section
    return None


def _build_relevance_re(terms):
    # Word-boundary match so "us" doesn't fire inside "serious", "fed" inside
    # "fed up", etc. Terms already containing spaces/punctuation are matched as
    # phrases; "u.s." keeps only a leading boundary (nothing follows the dot).
    alts = []
    for t in terms:
        esc = re.escape(t.strip())
        if t.endswith("."):
            alts.append(r"\b" + esc)
        else:
            alts.append(r"\b" + esc + r"\b")
    return re.compile("|".join(alts), re.I)


_RELEVANCE_RE = _build_relevance_re(cfg.US_RELEVANCE_TERMS)


def _is_us_relevant(title):
    return bool(_RELEVANCE_RE.search(title))


def _denied(title):
    low = title.lower()
    return any(p in low for p in cfg.DENY_PATTERNS)


def _downrank_penalty(title):
    low = title.lower()
    return sum(cfg.DOWNRANK_PENALTY for p in cfg.DOWNRANK_PATTERNS if p in low)


def _parse_feed(url):
    try:
        resp = _SESSION.get(url, timeout=cfg.HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding
        return feedparser.parse(resp.text)
    except Exception as e:  # noqa: BLE001
        print(f"  feed error {url}: {e}", file=sys.stderr)
        return None


def fetch_news(lookback_hours):
    now = dt.datetime.now(UTC)
    candidates = {}  # norm_title -> best candidate dict

    def consider(section_hint, entry, feed_name):
        title = (getattr(entry, "title", "") or "").strip()
        link = getattr(entry, "link", "") or ""
        if not title or not link or _denied(title):
            return
        aged = _entry_age_hours(entry, now)
        if aged is None:
            return
        age_h, published = aged
        if age_h > lookback_hours or age_h < -2:  # -2h tolerates clock skew
            return

        source = _source_name(entry, feed_name)
        weight = _source_weight(source)
        if weight < cfg.MIN_SOURCE_WEIGHT:
            return

        if cfg.REQUIRE_US_RELEVANCE and not _is_us_relevant(title):
            return

        # A headline is classified by its own words first; the query/feed tag is
        # only a fallback when nothing matches.
        section = _classify(title) or section_hint or "markets"
        clean_title = _TRAIL_SOURCE_RE.sub("", title).strip()
        recency_bonus = max(0.0, (lookback_hours - age_h) / lookback_hours) * 4.0
        score = weight + recency_bonus - _downrank_penalty(title)

        key = _norm_title(title)
        prev = candidates.get(key)
        if prev is None or score > prev["score"]:
            candidates[key] = {
                "title": clean_title, "url": link, "source": source,
                "section": section, "score": round(score, 2),
                "age_hours": round(age_h, 1),
                "published": published.isoformat(),
            }

    for section, query in cfg.GOOGLE_NEWS_QUERIES:
        url = _GN_BASE.format(q=urllib.parse.quote(query))
        feed = _parse_feed(url)
        if not feed:
            continue
        for entry in feed.entries:
            consider(section, entry, "Google News")

    for section, name, url in cfg.DIRECT_FEEDS:
        feed = _parse_feed(url)
        if not feed:
            continue
        for entry in feed.entries:
            consider(section, entry, name)

    sections = {s: [] for s in cfg.SECTION_ORDER}
    for cand in candidates.values():
        sections.setdefault(cand["section"], []).append(cand)

    for s in sections:
        sections[s].sort(key=lambda c: (-c["score"], c["age_hours"]))
        sections[s] = sections[s][: cfg.MAX_ITEMS_PER_SECTION]

    total = sum(len(v) for v in sections.values())
    return sections, total


# ---------------------------------------------------------------------------
# X / Twitter -- best effort via Nitter RSS, silent on failure
# ---------------------------------------------------------------------------
def fetch_x(lookback_hours):
    if not cfg.X_ENABLED:
        return []
    now = dt.datetime.now(UTC)
    items = []
    seen = set()
    for instance in cfg.NITTER_INSTANCES:
        got_any = False
        for user in cfg.X_ACCOUNTS:
            url = f"{instance}/{user}/rss"
            try:
                resp = _SESSION.get(url, timeout=cfg.X_TIMEOUT_SECONDS)
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
            except Exception:  # noqa: BLE001 -- expected; Nitter is flaky
                continue
            for entry in feed.entries:
                aged = _entry_age_hours(entry, now)
                if aged is None:
                    continue
                age_h, _ = aged
                if age_h > lookback_hours or age_h < -2:
                    continue
                text = re.sub(r"<[^>]+>", "", getattr(entry, "title", "") or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                got_any = True
                items.append({
                    "text": text, "url": getattr(entry, "link", "") or "",
                    "source": f"@{user}", "age_hours": round(age_h, 1),
                })
        if got_any:
            break  # first working instance wins
    items.sort(key=lambda i: i["age_hours"])
    return items[: cfg.X_MAX_ITEMS]


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def build_payload():
    now_sgt = dt.datetime.now(SGT)
    is_monday = now_sgt.weekday() == 0
    lookback = cfg.LOOKBACK_HOURS_MONDAY if is_monday else cfg.LOOKBACK_HOURS

    market_rows, asof_note = fetch_market()
    sections, news_total = fetch_news(lookback)
    x_items = fetch_x(lookback)

    return {
        "generated_at_sgt": now_sgt.strftime("%a %d %b %Y, %H:%M SGT"),
        "lookback_hours": lookback,
        "market_asof_note": asof_note,
        "market": market_rows,
        "sections": sections,
        "news_total": news_total,
        "x": x_items,
    }


_ARROW = {"up": "▲", "down": "▼", "flat": "–"}


def render_markdown(p):
    lines = [f"# US Market Morning Brief -- {p['generated_at_sgt']}", ""]
    if p["market_asof_note"]:
        lines += [f"_{p['market_asof_note']}_", ""]

    lines.append("## Overnight scoreboard")
    primary = [r for r in p["market"] if r["group"] in cfg.PRIMARY_GROUPS]
    also = [r for r in p["market"] if r["group"] not in cfg.PRIMARY_GROUPS]
    for r in primary:
        lines.append(f"- {r['label']}: {r['value']}  ({r['move']})")
    if also:
        tail = " · ".join(f"{r['label']} {r['value']} ({r['move']})" for r in also)
        lines.append(f"- _Also:_ {tail}")
    lines.append("")

    for section in cfg.SECTION_ORDER:
        items = p["sections"].get(section, [])
        if not items:
            continue
        lines.append(f"## {cfg.SECTION_TITLES[section]}")
        for it in items:
            lines.append(f"- [{it['title']}]({it['url']}) — {it['source']}")
        lines.append("")

    if p["x"]:
        lines.append("## From X")
        for it in p["x"]:
            lines.append(f"- {it['text']} — {it['source']}")
        lines.append("")

    lines.append(
        f"_Sources: Yahoo Finance (scoreboard), Google News + CNBC + Fed feeds "
        f"({p['news_total']} headlines, {p['lookback_hours']}h window)._"
    )
    return "\n".join(lines) + "\n"


def main():
    payload = build_payload()

    with open(OUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    md = render_markdown(payload)
    with open(OUT_MD_PATH, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"Wrote {OUT_MD_PATH} and {OUT_JSON_PATH}")
    print(f"  scoreboard rows: {len(payload['market'])}")
    print(f"  headlines: {payload['news_total']} across "
          f"{sum(1 for s in payload['sections'].values() if s)} sections")
    print(f"  X items: {len(payload['x'])}")
    if payload["market_asof_note"]:
        print(f"  note: {payload['market_asof_note']}")


if __name__ == "__main__":
    main()
