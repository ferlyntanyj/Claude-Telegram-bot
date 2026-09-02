"""
Tuning knobs for the weekday US-market morning brief (morning_brief.py + the
Telegram sender). Kept in one place so the pipeline and the message wording stay
in sync, the same way buyback_config.py serves the buy-back alert.
"""

# ---------------------------------------------------------------------------
# Market scoreboard
# ---------------------------------------------------------------------------
# (group, label, yfinance symbol). Order here is the order shown in the brief.
# "rates" symbols are quoted in percent already (^TNX = 10Y yield), so their
# move is reported in basis points rather than percent.
MARKET_TICKERS = [
    ("index", "S&P 500", "^GSPC"),
    ("index", "Nasdaq", "^IXIC"),
    ("index", "Dow", "^DJI"),
    ("index", "Russell 2000", "^RUT"),
    ("vol", "VIX", "^VIX"),
    ("rates", "US 3M", "^IRX"),      # 13-week T-bill yield (short-end proxy)
    ("rates", "US 10Y", "^TNX"),
    ("rates", "US 30Y", "^TYX"),
    ("commodity", "WTI crude", "CL=F"),
    ("commodity", "Gold", "GC=F"),
    ("commodity_minor", "Brent", "BZ=F"),
    ("fx", "Dollar index", "DX-Y.NYB"),
    ("crypto", "Bitcoin", "BTC-USD"),
    ("futures", "S&P fut", "ES=F"),
    ("futures", "Nasdaq fut", "NQ=F"),
]

# Groups always shown in full; everything else is folded into a one-line "Also"
# tail to keep the scoreboard short.
PRIMARY_GROUPS = {"index", "vol", "rates", "commodity"}

# ---------------------------------------------------------------------------
# Overnight window
# ---------------------------------------------------------------------------
# How far back a headline can be and still count as "overnight". Monday needs a
# wider window because it has to reach back over the weekend to Friday's US close.
LOOKBACK_HOURS = 20
LOOKBACK_HOURS_MONDAY = 72

MAX_ITEMS_PER_SECTION = 4

# ---------------------------------------------------------------------------
# News sources
# ---------------------------------------------------------------------------
# Section keys: "markets", "macro", "stocks", "geopolitics". They map 1:1 to the
# headed blocks in the brief, in this order.
SECTION_ORDER = ["markets", "macro", "stocks", "geopolitics"]
SECTION_TITLES = {
    "markets": "Markets",
    "macro": "Macro, Fed & data",
    "stocks": "Stocks & earnings",
    "geopolitics": "Geopolitics",
}

# Google News RSS search queries. `when:1d` filters server-side; we still
# re-filter by LOOKBACK_HOURS. Each query is tagged with the section it feeds,
# but that tag is only a fallback -- a headline is classified by its own words
# first (see SECTION_KEYWORDS).
GOOGLE_NEWS_QUERIES = [
    ("markets", "Wall Street stocks close S&P 500 Nasdaq when:1d"),
    ("markets", "US Treasury yields bond market selloff when:1d"),
    ("macro", "Federal Reserve rate cut Powell when:1d"),
    ("macro", "US inflation jobs report economic data when:1d"),
    ("stocks", "US stock movers earnings after hours when:1d"),
    ("geopolitics", "Trump tariffs trade markets when:1d"),
    ("geopolitics", "oil prices Middle East OPEC when:1d"),
]

# Every headline must look US-market-relevant to be kept -- it needs at least one
# of these terms (case-insensitive substring). The list is deliberately broad;
# the Google News queries and the CNBC feeds both surface general news that
# isn't market-moving, and this is the gate that drops it.
REQUIRE_US_RELEVANCE = True
US_RELEVANCE_TERMS = [
    "u.s.", "us", "america", "american", "wall street", "wall st", "s&p 500", "s&p500",
    "s&p", "nasdaq", "dow", "russell 2000", "treasury", "treasuries", "federal reserve",
    "the fed", "fed", "powell", "fomc", "dollar", "trump", "white house", "congress",
    "nvidia", "apple", "tesla", "microsoft", "amazon", "alphabet", "google", "meta",
    "broadcom", "netflix", "boeing", "jpmorgan", "goldman", "sec", "ftc", "antitrust",
    "cpi", "pce", "payroll", "payrolls", "jobless", "jobs report", "jobs data",
    "unemployment", "inflation", "recession", "gdp", "yield", "yields", "bond market",
    "opec", "wti", "brent", "crude", "oil price", "oil prices", "gold price",
    "stock", "stocks", "shares", "earnings", "revenue", "profit", "guidance", "ipo",
    "buyback", "dividend", "merger", "acquisition", "layoff", "layoffs", "workforce",
    "tariff", "tariffs", "trade deal", "trade war", "bitcoin", "etf",
]

# Direct RSS feeds (publisher feeds, not aggregated). (section, name, url).
# MarketWatch's own feeds are dropped -- their real-time feed is stale by weeks
# and their top-stories feed is mostly personal-finance fluff with broken
# encoding; MarketWatch articles still reach us cleanly via Google News.
DIRECT_FEEDS = [
    ("markets", "CNBC Markets", "https://www.cnbc.com/id/15839135/device/rss/rss.html"),
    ("markets", "CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("macro", "CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("macro", "Fed press releases", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("stocks", "CNBC Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
]

# Source-name -> weight. Matched against the source name AFTER normalisation
# (strip a leading "The ", strip a trailing ".com"/.co.uk/etc). Exact match only
# -- no substring fallback, so "CNBC Africa" and "BNN Bloomberg" do NOT inherit
# CNBC/Bloomberg weight. A source not listed gets DEFAULT_SOURCE_WEIGHT; anything
# below MIN_SOURCE_WEIGHT is dropped.
SOURCE_WEIGHTS = {
    "Reuters": 10, "Bloomberg": 10, "Wall Street Journal": 10, "WSJ": 10,
    "Financial Times": 10, "CNBC": 9, "New York Times": 9, "Associated Press": 9,
    "AP News": 9, "Barron's": 8, "MarketWatch": 8, "Yahoo Finance": 7,
    "The Economist": 8, "Axios": 7, "Nikkei Asia": 6, "CNN": 6, "Guardian": 6,
    "Investing.com": 5, "Federal Reserve": 9, "Politico": 6, "Fortune": 6,
    "Business Insider": 5, "Forbes": 5, "Morningstar": 5, "Investopedia": 5,
    "S&P Global": 6, "The Wall Street Journal": 10, "Financial Post": 5,
    "CNBC Markets": 9, "CNBC Economy": 9, "CNBC Finance": 9, "CNBC Top News": 9,
    "Fed press releases": 9,
}
DEFAULT_SOURCE_WEIGHT = 0   # unknown source -> dropped (allowlist behaviour)
MIN_SOURCE_WEIGHT = 5

# Headline patterns that mark low-value listicle / evergreen / promo / opinion
# content. Matched case-insensitively as substrings; each hit subtracts
# DOWNRANK_PENALTY from the score (enough to bury a top-tier source's item).
DOWNRANK_PENALTY = 9
DOWNRANK_PATTERNS = [
    "how to", "here are", "here's why", "here is why", "heres why", "should you buy",
    "best stocks", "stocks to buy", "motley fool", "reasons to", "ways to", "could make you",
    "millionaire", "if you invested", "my top", "watch this", "is it too late", "explained",
    "what to know", "what to watch", "things to know", "how much you", "retire", "your money",
    "i'm ", "i’m ", "dear ", "quiz", "horoscope", "we're buying", "we're lifting",
    "we're trimming", "we are buying", "jim cramer", "cramer", "club holding", "opinion:",
    "here's how", "here's what", "heres what", "5 things", "3 things", "what it means for you",
]

# Headlines matching any of these (case-insensitive substring) are dropped
# outright, regardless of source: forward-looking previews rather than overnight
# news, and other-country central-bank items that aren't US-market moving.
DENY_PATTERNS = [
    "week ahead", "week-ahead", "the week that was", "what to expect this week",
    "bank of canada", "bank of england", "reserve bank of", "swiss national bank",
    "day ahead", "coming week", "premarket:", "stocks to watch", "things to watch",
    "top 10 things", "10 things to watch", "cramer", "mad money", "lightning round",
    "morning squawk", "stock quote", "quote price", "price and forecast", "quote and news",
]

# Every headline is classified by its own words before any query/feed tag is
# used. First section whose list matches wins, so order matters: geopolitics and
# macro are checked before the broader stocks/markets buckets.
SECTION_KEYWORDS = {
    "geopolitics": ["tariff", "trade war", "trade deal", "sanction", "opec", "iran", "israel",
                    "russia", "ukraine", "gaza", "middle east", "strait of hormuz", "war",
                    "conflict", "geopolit", "north korea", "taiwan"],
    "macro": ["federal reserve", "the fed", "fed ", "powell", "fomc", "rate cut", "rate hike",
              "rate decision", "interest rate", "inflation", "cpi", "pce", "jobless", "payroll",
              "payrolls", "jobs report", "unemployment", "gdp", "jackson hole", "yield", "yields",
              "bond yield", "bond yields", "treasury", "treasuries", "bond sell", "bond rout",
              "bond market", "recession", "soft landing", "adp", "rate path"],
    "stocks": ["earnings", "shares", "stock ", "guidance", "downgrade", "upgrade", "ipo",
               "buyback", "dividend", "revenue", "profit", "after-hours", "after hours",
               "premarket", "pre-market", "forecast cut", "quarterly results", "surge", "slump",
               "plunge", "jump", "soar", "tumble"],
    "markets": ["s&p 500", "nasdaq", "dow ", "wall street", "wall st", "dollar", "gold",
                "crude", "oil price", "vix", "sell-off", "selloff", "rally", "record high",
                "stocks close", "stocks end", "market"],
}

# ---------------------------------------------------------------------------
# X / Twitter (best-effort, no paid API)
# ---------------------------------------------------------------------------
# Nitter RSS is the only free route and most instances are dead or auth-walled,
# so this is expected to no-op often; the brief just omits the X block when the
# pull returns nothing. Never let an X failure abort the brief.
X_ENABLED = True
X_ACCOUNTS = ["DeItaone", "FirstSquawk", "LiveSquawk", "financialjuice"]
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
]
X_MAX_ITEMS = 4
X_TIMEOUT_SECONDS = 6

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT_SECONDS = 15
