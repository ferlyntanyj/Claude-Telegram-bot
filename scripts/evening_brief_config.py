"""
Tuning knobs for the weekday evening Asia-market brief. Consumed by brief_engine
(shared with the US morning brief) via the thin evening_brief.py / send_evening_
brief_telegram.py wrappers.

Scope: ASEAN (Singapore, Malaysia, Indonesia, Thailand, Philippines, Vietnam),
Japan, Korea, Greater China (mainland + Hong Kong + Taiwan). Delivered 18:15
Asia/Singapore (10:15 UTC), after Tokyo/Seoul/Shanghai/HK have closed and with
the SGX cash session nearly done. Headlines only -- no price scoreboard by
default. The brief flags *major* macro moves (inflation, yields, FX, rate
decisions), new policy, and geopolitics; it is not a full data report.
"""

# ---------------------------------------------------------------------------
# Identity / output
# ---------------------------------------------------------------------------
BRIEF_TITLE = "Asia Market Evening Brief"
BRIEF_EMOJI = "🌇"
OUT_MD_PATH = "../output/evening_brief.md"
OUT_JSON_PATH = "../output/evening_brief.json"

# Singapore-centric Google News locale gives the best ASEAN coverage while still
# surfacing Reuters / Bloomberg / Nikkei on Japan, Korea and China.
GOOGLE_NEWS_LOCALE = ("en-SG", "SG", "SG:en")

SOURCES_FOOTER = "Google News + CNBC Asia + Straits Times + Nikkei Asia feeds"
SOURCES_FOOTER_TG = "Google News + CNBC Asia + Straits Times + Nikkei"

# ---------------------------------------------------------------------------
# Market scoreboard (off by default; kept here so INCLUDE_SCOREBOARD = True works)
# ---------------------------------------------------------------------------
INCLUDE_SCOREBOARD = False
SCOREBOARD_HEADER = "Asia close"
SCOREBOARD_EMOJI = "📊"
SCOREBOARD_STALE_NOTE = "Asian markets last traded {date} (holiday or data lag)."

MARKET_TICKERS = [
    ("index", "Nikkei 225", "^N225"),
    ("index", "Topix", "^TPX"),
    ("index", "Kospi", "^KS11"),
    ("index", "Hang Seng", "^HSI"),
    ("index", "Shanghai Comp", "000001.SS"),
    ("index", "CSI 300", "000300.SS"),
    ("index", "STI", "^STI"),
    ("index", "Jakarta Comp", "^JKSE"),
    ("index", "KLCI", "^KLSE"),
    ("index", "SET", "^SET.BK"),
    ("index", "PSEi", "PSEI.PS"),
    ("rates", "JP 10Y", "^TNX"),        # placeholder; replace if a JGB yield symbol is wired
    ("fx", "USD/JPY", "JPY=X"),
    ("fx", "USD/CNH", "CNH=X"),
    ("fx", "USD/SGD", "SGD=X"),
    ("fx", "USD/KRW", "KRW=X"),
]
PRIMARY_GROUPS = {"index", "vol", "rates", "commodity"}

# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
# 14h back from 18:15 SGT reaches ~04:00 SGT -- the full Asian trading day plus
# the pre-open. Monday widens to Saturday morning to catch weekend policy moves
# (China in particular tends to announce on weekends).
LOOKBACK_HOURS = 14
LOOKBACK_HOURS_MONDAY = 62

MAX_ITEMS_PER_SECTION = 5

# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
SECTION_ORDER = ["macro", "markets", "geopolitics"]
SECTION_TITLES = {
    "macro": "Macro & policy",
    "markets": "Markets",
    "geopolitics": "Geopolitics",
}
SECTION_EMOJI = {
    "macro": "🏦",
    "markets": "📈",
    "geopolitics": "🌏",
}

# ---------------------------------------------------------------------------
# News sources
# ---------------------------------------------------------------------------
GOOGLE_NEWS_QUERIES = [
    ("macro", "Asia central bank interest rate decision inflation when:1d"),
    ("macro", "China PBOC yuan stimulus policy loan prime rate when:1d"),
    ("macro", "Bank of Japan yen JGB yield when:1d"),
    ("macro", "Bank of Korea won Korea inflation when:1d"),
    ("macro", "Singapore Indonesia Malaysia inflation rate currency when:1d"),
    ("markets", "Asia stocks close Nikkei Hang Seng Shanghai Kospi when:1d"),
    ("markets", "Singapore Malaysia Indonesia Thailand Philippines stock market when:1d"),
    ("geopolitics", "South China Sea Taiwan tensions military when:1d"),
    ("geopolitics", "China export controls chip tariffs trade when:1d"),
    ("geopolitics", "North Korea missile nuclear weapons sanctions when:1d"),
]

# (section, name, url). Publisher feeds that survive parsing and carry real
# Asia macro/markets copy; fluff is handled by the relevance gate + source
# weights, not by excluding the feed.
DIRECT_FEEDS = [
    ("markets", "CNBC Asia Markets", "https://www.cnbc.com/id/19832390/device/rss/rss.html"),
    ("macro", "Straits Times Business", "https://www.straitstimes.com/news/business/rss.xml"),
    ("geopolitics", "Straits Times Asia", "https://www.straitstimes.com/news/asia/rss.xml"),
    ("markets", "CNA Business", "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6936"),
    ("macro", "Nikkei Asia", "https://asia.nikkei.com/rss/feed/nar"),
]

# Source-name -> weight, matched after normalisation (strip leading "The ",
# strip trailing ".com"/etc). Exact match only. Unknown source -> dropped.
SOURCE_WEIGHTS = {
    "Reuters": 10, "Bloomberg": 10, "Financial Times": 10, "Wall Street Journal": 10,
    "WSJ": 10, "The Wall Street Journal": 10, "The Economist": 9,
    "Nikkei Asia": 9, "Nikkei Asian Review": 9, "Nikkei": 8, "Nikkei Asia (nar)": 9,
    "CNBC": 9, "CNBC Asia Markets": 9,
    "South China Morning Post": 8, "SCMP": 8,
    "Straits Times": 8, "Straits Times Business": 8, "Straits Times Asia": 8,
    "Business Times": 7, "Channel NewsAsia": 8, "CNA": 8, "CNA Business": 8,
    "Associated Press": 8, "AP News": 8, "Barron's": 8,
    "Caixin": 7, "Caixin Global": 7, "Yonhap": 7, "Yonhap News Agency": 7,
    "Kyodo News": 7, "Japan Times": 7, "The Korea Herald": 6, "The Korea Times": 6,
    "Korea JoongAng Daily": 6, "Jakarta Post": 6, "Jakarta Globe": 5,
    "Bangkok Post": 6, "Nation Thailand": 5, "Philippine Daily Inquirer": 5,
    "Inquirer": 5, "Rappler": 5, "VnExpress": 5, "Vietnam News": 5,
    "The Edge Singapore": 6, "The Edge Malaysia": 6, "Nikkei Asia Review": 9,
    "CNN": 6, "Guardian": 6, "Al Jazeera": 6, "Fortune": 6, "Forbes": 5,
    "Business Insider": 5, "Investing.com": 5, "S&P Global": 6, "Politico": 6,
    "MarketWatch": 7, "Yahoo Finance": 7, "New York Times": 8,
    # State media -- kept but low so they only surface when nothing else has a story.
    "Global Times": 3, "Xinhua": 3, "China Daily": 3, "CGTN": 3,
}
DEFAULT_SOURCE_WEIGHT = 0   # unknown source -> dropped (allowlist behaviour)
MIN_SOURCE_WEIGHT = 5

# Listicle / evergreen / promo / opinion markers -- each hit subtracts
# DOWNRANK_PENALTY from the score (enough to bury a top-tier source's item).
DOWNRANK_PENALTY = 9
DOWNRANK_PATTERNS = [
    "how to", "here are", "here's why", "here is why", "heres why", "should you buy",
    "best stocks", "stocks to buy", "motley fool", "reasons to", "ways to", "could make you",
    "millionaire", "if you invested", "my top", "watch this", "is it too late", "explained",
    "what to know", "what to watch", "things to know", "how much you", "retire", "your money",
    "i'm ", "i’m ", "dear ", "quiz", "horoscope", "opinion:", "review:", "recipe",
    "here's how", "here's what", "heres what", "5 things", "3 things", "what it means for you",
    "travel guide", "things to do", "best places", "michelin",
]

# Dropped outright regardless of source: forward-looking previews and
# non-news quote/liveblog pages. (Unlike the US brief, other-country
# central-bank items are NOT denied here -- RBI / RBA / BOJ / BOK are on-topic.)
DENY_PATTERNS = [
    "week ahead", "week-ahead", "the week that was", "what to expect this week",
    "day ahead", "coming week", "premarket:", "pre-market:", "stocks to watch",
    "things to watch", "top 10 things", "10 things to watch",
    "stock quote", "quote price", "price and forecast", "quote and news",
    "as it happened", "live updates:", "live blog", "in pictures", "in photos",
]

# Each headline is classified by its own words; first section whose list matches
# wins, so order matters -- geopolitics and macro are checked before markets.
SECTION_KEYWORDS = {
    "geopolitics": [
        "south china sea", "taiwan strait", "taiwan", "north korea", "pyongyang",
        "senkaku", "diaoyu", "myanmar", "sanction", "sanctions", "tariff", "tariffs",
        "export control", "export controls", "chip ban", "chip curbs", "chip war",
        "trade war", "territorial", "military drill", "military drills", "coup",
        "border clash", "geopolit", "u.s.-china", "us-china", "china-eu",
        "missile", "warship", "incursion", "espionage", "summit",
    ],
    "macro": [
        "inflation", "cpi", "ppi", "deflation", "rate cut", "rate hike", "rate decision",
        "interest rate", "interest rates", "central bank", "pboc", "boj", "bank of japan",
        "bank of korea", "reserve bank", "rba", "rbi", "monetary policy", "stimulus",
        "rrr", "reserve ratio", "loan prime rate", "lpr", "yuan", "renminbi", "yen",
        "won", "rupiah", "ringgit", "baht", "peso", "dong", "bond yield", "bond yields",
        "jgb", "gdp", "trade balance", "trade surplus", "trade deficit", "exports",
        "imports", "factory activity", "pmi", "fiscal", "budget", "stimulus package",
        "policy rate", "property measures", "stamp duty", "subsidy", "intervention",
        "fx reserves", "foreign reserves", "current account", "retail sales",
        "industrial output", "industrial production", "unemployment", "jobless",
    ],
    "markets": [
        "nikkei", "topix", "kospi", "kosdaq", "hang seng", "hsi", "shanghai composite",
        "csi 300", "csi300", "shenzhen", "sti", "straits times index", "sensex", "nifty",
        "jakarta composite", "klci", "set index", "psei", "shares", "stocks close",
        "stocks end", "stock market", "rally", "sell-off", "selloff", "plunge", "surge",
        "slump", "rebound", "tumble", "jump", "equities", "benchmark index", "bourse",
    ],
}

# Every headline must name something Asia-specific to be kept -- this is the gate
# that keeps the brief regional and drops US/EU-centric stories the queries and
# feeds also surface (generic "Fed rate decision" belongs in the morning brief).
# Deliberately does NOT include bare "rate" / "stocks" / "inflation" / "trade".
REQUIRE_RELEVANCE = True
STRICT_DIRECT_FEEDS = True
RELEVANCE_TERMS = [
    "asia", "asian", "asean", "china", "chinese", "beijing", "shanghai", "shenzhen",
    "hong kong", "hongkong", "japan", "japanese", "tokyo", "korea", "korean", "seoul",
    "taiwan", "taipei", "singapore", "singaporean", "malaysia", "malaysian", "kuala lumpur",
    "indonesia", "indonesian", "jakarta", "thailand", "thai", "bangkok", "philippines",
    "philippine", "manila", "vietnam", "vietnamese", "hanoi", "india", "indian", "mumbai",
    "yuan", "renminbi", "yen", "won", "rupiah", "ringgit", "baht", "dong",
    "nikkei", "topix", "kospi", "kosdaq", "hang seng", "csi 300", "shanghai composite",
    "straits times index", "sti", "sensex", "nifty", "jakarta composite", "klci",
    "pboc", "boj", "bank of japan", "bank of korea", "bank of thailand", "bank indonesia",
    "reserve bank of india", "monetary authority of singapore", "jgb", "loan prime rate", "lpr",
]

# ---------------------------------------------------------------------------
# X / Twitter (best-effort, no paid API -- usually no-ops, brief omits the block)
# ---------------------------------------------------------------------------
X_ENABLED = True
X_ACCOUNTS = ["financialjuice", "LiveSquawk", "Sino_Market", "YuanTalks"]
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
