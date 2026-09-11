"""
Tuning knobs for the global major-news Telegram alert. Consumed by
major_news_engine.py via the thin major_news_alert.py wrapper.

Unlike the scheduled digests (morning/evening/semis brief), this is a
near-real-time alert stream: one run = one poll cycle, run in the cloud every
POLL_INTERVAL_MINUTES via GitHub Actions (.github/workflows/major_news_alert.yml)
so it doesn't depend on this machine being on. It only sends a Telegram message
when a qualifying story is found, rather than always producing a digest.

Two trigger paths:
  1. Single-stock: a headline names a company on data/global_watchlist.csv
     (built by global_watchlist_build.py, already filtered to >= USD 5B
     market cap) whose intraday move clears SINGLE_STOCK_MOVE_PCT.
  2. Sector-wide: a headline looks thematic/macro (SECTOR_TRIGGER_KEYWORDS),
     so Claude is asked to name the likely-affected large-cap peers; if their
     MEDIAN intraday move clears SECTOR_MEDIAN_MOVE_PCT (or enough of them
     individually clear SECTOR_BREADTH_MOVE_PCT), it qualifies. Median/breadth
     rather than a plain average, deliberately -- see the plan this was built
     from: a single outlier in an LLM-picked peer list shouldn't be able to
     drag a mean over the line.

Headline sourcing reuses brief_engine.fetch_news as-is (same Google News RSS +
direct-feed + source-trust-ranking + de-dup machinery as the other briefs);
only the config below differs.
"""

# ---------------------------------------------------------------------------
# Output / state
# ---------------------------------------------------------------------------
WATCHLIST_CSV_PATH = "../data/global_watchlist.csv"
STATE_JSON_PATH = "../data/alert_state.json"
OUT_LOG_JSON_PATH = "../output/major_news_alert_log.json"  # append-only run log, for auditing

GOOGLE_NEWS_LOCALE = ("en-US", "US", "US:en")
SOURCES_FOOTER_TG = "Google News (Bloomberg/Reuters/Nikkei/SCMP/WSJ/FT tier)"

# ---------------------------------------------------------------------------
# Cadence / window
# ---------------------------------------------------------------------------
POLL_INTERVAL_MINUTES = 20
# Overlap beyond the poll interval so a headline can't fall through the crack
# between two cycles (feed lag, clock drift, a cycle that ran slightly late).
LOOKBACK_OVERLAP_MINUTES = 15
MAX_ITEMS_PER_SECTION = 40

# ---------------------------------------------------------------------------
# Move thresholds
# ---------------------------------------------------------------------------
SINGLE_STOCK_MOVE_PCT = 5.0
SECTOR_MEDIAN_MOVE_PCT = 3.0
SECTOR_BREADTH_MOVE_PCT = 2.0     # per-peer bar used for the breadth check
SECTOR_BREADTH_SHARE_PCT = 60.0   # % of the peer basket that must clear it
SECTOR_MIN_PEERS = 3              # fewer than this and "median"/"breadth" isn't meaningful

# Cooldown: don't re-alert the same story/ticker within this many hours...
COOLDOWN_HOURS = 4.0
# ...unless the move has deepened by at least this many additional points.
RE_ALERT_DELTA_PCT = 3.0

# ---------------------------------------------------------------------------
# LLM (Gemini API -- free tier, no billing account required; needs
# GEMINI_API_KEY set, from a key created at aistudio.google.com)
# ---------------------------------------------------------------------------
LLM_MODEL = "gemini-3.8-flash"
LLM_PEER_MAX_TOKENS = 400
LLM_SIGNIFICANCE_MAX_TOKENS = 300

# ---------------------------------------------------------------------------
# News sources -- broad market-moving queries, not sector-specific
# ---------------------------------------------------------------------------
GOOGLE_NEWS_QUERIES = [
    ("news", "stock shares surge OR plunge OR soar OR tumble when:1h"),
    ("news", "earnings guidance profit warning beat miss when:1h"),
    ("news", "acquisition merger deal billion stake buyout when:1h"),
    ("news", "credit rating downgrade upgrade default when:1h"),
    ("news", "Federal Reserve OR ECB OR Bank of Japan rate decision when:1h"),
    ("news", "tariff export controls sanctions trade war when:1h"),
    ("news", "antitrust regulator fine investigation ruling when:1h"),
    ("news", "recall lawsuit cyberattack outage disruption when:1h"),
    ("news", "OPEC oil price shock supply when:1h"),
    ("news", "market selloff rally record high stocks close when:1h"),
]

# Chip-topical direct feeds aren't relevant here; a handful of general wire/
# business feeds with known-stable public RSS, matching the trust tier below.
# (AP's public RSS feeds were retired -- feeds.apnews.com no longer resolves --
# so AP is only reachable here via Google News, not a direct feed.)
DIRECT_FEEDS = [
    ("news", "Nikkei Asia", "https://asia.nikkei.com/rss/feed/nar"),
    ("news", "SCMP Business", "https://www.scmp.com/rss/92/feed"),
    ("news", "CNBC Markets", "https://www.cnbc.com/id/15839135/device/rss/rss.html"),
]

# Restricted primarily to the user's named tier; a secondary tier of major
# wires is kept for global breadth. Everything else is dropped (allowlist
# behaviour, same as the other briefs).
#
# Google News RSS is inconsistent about how it tags some of these: Reuters,
# FT, and WSJ usually get a proper title ("Reuters", "Financial Times", "WSJ"),
# but Bloomberg (and occasionally FT) frequently comes through as the bare
# domain instead (e.g. "bloomberg.com"), which _norm_source strips down to a
# lowercase word ("bloomberg"). Lowercase fallback keys below catch that --
# verified empirically against live Google News RSS output, not assumed.
SOURCE_WEIGHTS = {
    # Primary tier
    "Bloomberg": 10, "bloomberg": 10, "Bloomberg Technology": 10, "Reuters": 10, "Reuters Technology": 10,
    "Nikkei Asia": 10, "Nikkei Asian Review": 10, "Nikkei": 9,
    "South China Morning Post": 10, "SCMP": 10, "scmp": 10,
    "Wall Street Journal": 10, "WSJ": 10, "The Wall Street Journal": 10,
    "Washington Post": 10, "The Washington Post": 10, "washingtonpost": 10,
    "Financial Times": 10, "ft": 10,
    # Secondary tier -- major wires / desks, kept for global breadth
    "Associated Press": 7, "AP News": 7, "AP Business": 7,
    "Yonhap": 6, "Yonhap News Agency": 6, "Kyodo News": 6,
    "Caixin": 6, "Caixin Global": 6,
    "The Straits Times": 6, "Straits Times": 6, "The Business Times": 6, "Business Times": 6,
    "CNBC": 6, "CNBC Markets": 6, "MarketWatch": 6, "Barron's": 6,
    "The Economist": 7,
}
DEFAULT_SOURCE_WEIGHT = 0   # unknown source -> dropped
MIN_SOURCE_WEIGHT = 6       # higher bar than the digests -- this is an alert, not a scan

DOWNRANK_PENALTY = 9
DOWNRANK_PATTERNS = [
    "how to", "here are", "here's why", "here is why", "heres why", "should you buy",
    "best stocks", "stocks to buy", "motley fool", "reasons to", "ways to", "could make you",
    "millionaire", "if you invested", "my top", "watch this", "is it too late", "prediction:",
    "better buy", "vs.", "which stock", "what to know", "what to watch", "things to know",
    "your money", "review:", "opinion:", "5 things", "3 things", "top 5", "top 10",
    "explained", "everything you need to know", "what it means for you",
]
DENY_PATTERNS = [
    "week ahead", "week-ahead", "the week that was", "what to expect this week",
    "day ahead", "coming week", "premarket:", "pre-market:", "stocks to watch",
    "things to watch", "earnings preview", "what to expect", "preview:",
    "stock quote", "quote price", "price and forecast", "quote and news",
    "as it happened", "live updates:", "live blog", "livestream", "podcast:", "webinar",
]

# Classification here is only used as a fallback tag; single-stock vs
# sector-wide is actually decided by major_news_engine against the watchlist
# and the sector-trigger keywords below, not by this section machinery. One
# catch-all section keeps brief_engine.fetch_news happy.
SECTION_ORDER = ["news"]
SECTION_TITLES = {"news": "Major news"}
SECTION_KEYWORDS = {
    "news": [
        "stock", "stocks", "shares", "share price", "market", "markets", "earnings",
        "revenue", "profit", "guidance", "merger", "acquisition", "deal", "tariff",
        "sanctions", "rate", "fed", "regulator", "downgrade", "upgrade", "recall",
        "lawsuit", "cyberattack", "outage", "oil", "opec", "selloff", "rally", "surge",
        "plunge", "soar", "tumble", "record high",
    ],
}

REQUIRE_RELEVANCE = True
STRICT_DIRECT_FEEDS = False
RELEVANCE_TERMS = [
    "stock", "stocks", "share", "shares", "market", "markets", "nasdaq", "s&p", "dow",
    "ftse", "nikkei", "hang seng", "kospi", "dax", "index", "indices",
    "earnings", "revenue", "profit", "guidance", "quarterly", "forecast",
    "merger", "acquisition", "acquire", "stake", "buyout", "takeover", "ipo",
    "tariff", "tariffs", "sanction", "sanctions", "export control", "trade war",
    "federal reserve", "the fed", "ecb", "bank of japan", "rate cut", "rate hike",
    "interest rate", "central bank",
    "credit rating", "downgrade", "downgraded", "upgrade", "upgraded", "default",
    "antitrust", "regulator", "regulators", "fine", "fined", "investigation", "ruling",
    "recall", "lawsuit", "cyberattack", "data breach", "outage",
    "oil price", "oil prices", "crude", "opec",
    "selloff", "sell-off", "rally", "record high", "plunge", "plunges", "soar", "soars",
    "surge", "surges", "tumble", "tumbles", "slump", "slumps", "jump", "jumps",
    "billion", "market cap",
]

# Cheap pre-filter before spending an LLM call: a headline only goes down the
# "infer sector peers" path if it also carries one of these thematic/macro
# markers -- otherwise a random single-company headline that didn't match the
# watchlist would trigger an LLM call for nothing.
SECTOR_TRIGGER_KEYWORDS = [
    "tariff", "tariffs", "export control", "sanction", "sanctions", "trade war",
    "federal reserve", "the fed", "ecb", "bank of japan", "rate cut", "rate hike",
    "interest rate", "central bank", "opec", "oil price", "oil prices",
    "chipmakers", "automakers", "airlines", "banks", "lenders", "retailers",
    "homebuilders", "miners", "oil majors", "utilities", "insurers",
    "industry", "sector-wide", "across the sector", "peers", "rivals",
    "regulation", "regulators", "antitrust", "recall", "shortage", "supply chain",
    "credit rating", "sovereign debt", "currency", "inflation", "recession",
]

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT_SECONDS = 15
