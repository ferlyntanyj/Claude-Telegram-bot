"""
Shared engine for the scheduled news briefs (morning US brief, evening Asia
brief). Deterministic, no LLM.

Everything here is driven by a config module passed to run(): the US brief passes
morning_brief_config, the Asia brief passes evening_brief_config. The two briefs
therefore share all ranking / de-duplication / windowing / rendering logic and
differ only in their config (feeds, queries, sections, relevance terms, window
lengths, titles, output paths).

  1. Scoreboard (OPTIONAL, cfg.INCLUDE_SCOREBOARD) -- last close vs prior close
     for cfg.MARKET_TICKERS via Yahoo Finance (yfinance). "rates" symbols are
     quoted in percent already, so their move is reported in basis points.
  2. Headlines -- pulled from Google News RSS search (`when:1d`, cfg locale) plus
     cfg.DIRECT_FEEDS publisher feeds, filtered to the lookback window, dropped
     unless they carry a cfg.RELEVANCE_TERMS term, de-duplicated across sources,
     ranked by source trust + recency (cfg.DOWNRANK_PATTERNS buried,
     cfg.DENY_PATTERNS dropped outright), and sorted into cfg.SECTION_ORDER
     blocks by their own wording (cfg.SECTION_KEYWORDS). A best-effort X (Nitter)
     pull is appended when a live instance answers, silently skipped otherwise.

Outputs (paths from cfg):
  cfg.OUT_MD_PATH   -- human-readable, committed by the workflow as an archive
  cfg.OUT_JSON_PATH -- structured, consumed by brief_telegram.send()
"""
import calendar
import datetime as dt
import json
import re
import sys
import urllib.parse

import feedparser
import requests

SGT = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": _DEFAULT_UA})


# ---------------------------------------------------------------------------
# Market scoreboard
# ---------------------------------------------------------------------------
def _dir(delta):
    if delta > 0:
        return "up"
    if delta < 0:
        return "down"
    return "flat"


def fetch_market(cfg):
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
        if (today_utc - latest).days > 1:
            asof_note = cfg.SCOREBOARD_STALE_NOTE.format(date=latest.isoformat())
    return rows, asof_note


# ---------------------------------------------------------------------------
# Headlines
# ---------------------------------------------------------------------------
_TRAIL_SOURCE_RE = re.compile(r"\s+-\s+[^-]+$")  # strip Google News' " - Publisher" tail
_NORM_RE = re.compile(r"[^a-z0-9 ]+")
_SOURCE_SUFFIX_RE = re.compile(r"\.(com|org|net|co\.uk|co|io|news)$", re.I)


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


def _build_relevance_re(terms):
    # Word-boundary match so "us" doesn't fire inside "serious", etc. Terms with
    # spaces/punctuation match as phrases; a trailing "." keeps only a leading
    # boundary (nothing follows the dot).
    alts = []
    for t in terms:
        esc = re.escape(t.strip())
        alts.append(r"\b" + esc if t.endswith(".") else r"\b" + esc + r"\b")
    return re.compile("|".join(alts), re.I)


def _parse_feed(url, timeout):
    try:
        resp = _SESSION.get(url, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding
        return feedparser.parse(resp.text)
    except Exception as e:  # noqa: BLE001
        print(f"  feed error {url}: {e}", file=sys.stderr)
        return None


def fetch_news(cfg, lookback_hours):
    now = dt.datetime.now(UTC)
    hl, gl, ceid = cfg.GOOGLE_NEWS_LOCALE
    gn_base = f"https://news.google.com/rss/search?q={{q}}&hl={hl}&gl={gl}&ceid={ceid}"

    section_res = {
        section: re.compile(
            r"\b(?:" + "|".join(re.escape(k.strip()) for k in kws) + r")\b", re.I
        )
        for section, kws in cfg.SECTION_KEYWORDS.items()
    }
    relevance_re = _build_relevance_re(cfg.RELEVANCE_TERMS)
    downrank = [p.lower() for p in cfg.DOWNRANK_PATTERNS]
    deny = [p.lower() for p in cfg.DENY_PATTERNS]
    # When True, a headline from a broad publisher feed is kept only if its own
    # wording matches a section's keywords -- the feed-level tag is not enough.
    # Google News queries are topical, so their tag always stands.
    strict_direct = getattr(cfg, "STRICT_DIRECT_FEEDS", False)

    def classify(title):
        for section, rx in section_res.items():
            if rx.search(title):
                return section
        return None

    candidates = {}  # norm_title -> best candidate dict

    def consider(section_hint, entry, feed_name, hint_reliable=True):
        title = (getattr(entry, "title", "") or "").strip()
        link = getattr(entry, "link", "") or ""
        if not title or not link:
            return
        low = title.lower()
        if any(p in low for p in deny):
            return
        aged = _entry_age_hours(entry, now)
        if aged is None:
            return
        age_h, published = aged
        if age_h > lookback_hours or age_h < -2:  # -2h tolerates clock skew
            return

        source = _source_name(entry, feed_name)
        weight = cfg.SOURCE_WEIGHTS.get(_norm_source(source), cfg.DEFAULT_SOURCE_WEIGHT)
        if weight < cfg.MIN_SOURCE_WEIGHT:
            return

        if cfg.REQUIRE_RELEVANCE and not relevance_re.search(title):
            return

        matched = classify(title)
        if matched is None and not hint_reliable:
            return  # broad feed + no keyword match -> not clearly on-topic
        section = matched or section_hint or cfg.SECTION_ORDER[0]
        clean_title = _TRAIL_SOURCE_RE.sub("", title).strip()
        recency_bonus = max(0.0, (lookback_hours - age_h) / lookback_hours) * 4.0
        penalty = sum(cfg.DOWNRANK_PENALTY for p in downrank if p in low)
        score = weight + recency_bonus - penalty

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
        feed = _parse_feed(gn_base.format(q=urllib.parse.quote(query)), cfg.HTTP_TIMEOUT_SECONDS)
        if feed:
            for entry in feed.entries:
                consider(section, entry, "Google News")

    for section, name, url in cfg.DIRECT_FEEDS:
        feed = _parse_feed(url, cfg.HTTP_TIMEOUT_SECONDS)
        if feed:
            for entry in feed.entries:
                consider(section, entry, name, hint_reliable=not strict_direct)

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
def fetch_x(cfg, lookback_hours):
    if not cfg.X_ENABLED:
        return []
    now = dt.datetime.now(UTC)
    items = []
    seen = set()
    for instance in cfg.NITTER_INSTANCES:
        got_any = False
        for user in cfg.X_ACCOUNTS:
            try:
                resp = _SESSION.get(f"{instance}/{user}/rss", timeout=cfg.X_TIMEOUT_SECONDS)
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
# Assemble + render
# ---------------------------------------------------------------------------
def build_payload(cfg):
    now_sgt = dt.datetime.now(SGT)
    is_monday = now_sgt.weekday() == 0
    lookback = cfg.LOOKBACK_HOURS_MONDAY if is_monday else cfg.LOOKBACK_HOURS

    market_rows, asof_note = fetch_market(cfg) if cfg.INCLUDE_SCOREBOARD else ([], "")
    sections, news_total = fetch_news(cfg, lookback)
    x_items = fetch_x(cfg, lookback)

    return {
        "brief_title": cfg.BRIEF_TITLE,
        "generated_at_sgt": now_sgt.strftime("%a %d %b %Y, %H:%M SGT"),
        "lookback_hours": lookback,
        "market_asof_note": asof_note,
        "market": market_rows,
        "sections": sections,
        "news_total": news_total,
        "x": x_items,
    }


def render_markdown(cfg, p):
    lines = [f"# {p['brief_title']} -- {p['generated_at_sgt']}", ""]
    if p["market_asof_note"]:
        lines += [f"_{p['market_asof_note']}_", ""]

    if p["market"]:
        lines.append(f"## {cfg.SCOREBOARD_HEADER}")
        primary = [r for r in p["market"] if r["group"] in cfg.PRIMARY_GROUPS]
        also = [r for r in p["market"] if r["group"] not in cfg.PRIMARY_GROUPS]
        for r in primary:
            lines.append(f"- {r['label']}: {r['value']}  ({r['move']})")
        if also:
            tail = " · ".join(f"{r['label']} {r['value']} ({r['move']})" for r in also)
            lines.append(f"- _Also:_ {tail}")
        lines.append("")

    if p["news_total"] == 0:
        lines += ["_Quiet session -- nothing major flagged in the window._", ""]

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

    src_prefix = "Yahoo Finance (scoreboard), " if p["market"] else ""
    lines.append(
        f"_Sources: {src_prefix}{cfg.SOURCES_FOOTER} "
        f"({p['news_total']} headlines, {p['lookback_hours']}h window)._"
    )
    return "\n".join(lines) + "\n"


def run(cfg):
    _SESSION.headers.update({"User-Agent": cfg.USER_AGENT})
    payload = build_payload(cfg)

    with open(cfg.OUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    with open(cfg.OUT_MD_PATH, "w", encoding="utf-8") as f:
        f.write(render_markdown(cfg, payload))

    print(f"Wrote {cfg.OUT_MD_PATH} and {cfg.OUT_JSON_PATH}")
    print(f"  scoreboard rows: {len(payload['market'])}")
    print(f"  headlines: {payload['news_total']} across "
          f"{sum(1 for s in payload['sections'].values() if s)} sections")
    print(f"  X items: {len(payload['x'])}")
    if payload["market_asof_note"]:
        print(f"  note: {payload['market_asof_note']}")
    return payload
