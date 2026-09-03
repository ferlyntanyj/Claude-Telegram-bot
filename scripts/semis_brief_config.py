"""
Tuning knobs for the twice-daily global semiconductor brief. Consumed by
brief_engine (shared with the US morning brief and the Asia evening brief) via
the thin semis_brief.py / send_semis_brief_telegram.py wrappers.

Scope: the global chip industry -- foundries (TSMC, Samsung, SMIC, GlobalFoundries,
UMC), memory (SK Hynix, Micron, Kioxia), IDMs and fabless (Intel, Nvidia, AMD,
Broadcom, Qualcomm, Texas Instruments), equipment (ASML, Applied Materials, Lam,
KLA, Tokyo Electron) and the policy layer around them. Runs at 08:30 SGT
(00:30 UTC), covering the overnight US session, and 18:30 SGT (10:30 UTC),
covering the Asia session -- every day, since chip policy and Taiwan/Korea
supply-chain news break on weekends too.

Deterministic, no LLM: headlines are pulled from Google News RSS + a few
publisher feeds, filtered to the lookback window, gated on a semiconductor
relevance term, de-duplicated, ranked by source trust + recency, and sorted into
three sections by their own wording. There is no synthesised "takeaway" line --
that needs a model; see the git history for the API-backed variant if wanted.

Outputs:
  ../output/semis_brief.md   -- human-readable, committed by the workflow
  ../output/semis_brief.json -- structured, consumed by brief_telegram.send()
"""

# ---------------------------------------------------------------------------
# Identity / output
# ---------------------------------------------------------------------------
BRIEF_TITLE = "Global Semiconductor Brief"
BRIEF_EMOJI = "🔬"
OUT_MD_PATH = "../output/semis_brief.md"
OUT_JSON_PATH = "../output/semis_brief.json"

# Global wire coverage is best on the US locale; Asia chip trade press is added
# explicitly via DIRECT_FEEDS and the source allowlist below.
GOOGLE_NEWS_LOCALE = ("en-US", "US", "US:en")

SOURCES_FOOTER = "Google News + EE Times + Tom's Hardware + Reuters Tech feeds"
SOURCES_FOOTER_TG = "Google News + EE Times + Tom's Hardware"

# ---------------------------------------------------------------------------
# Market scoreboard (off by default; kept here so INCLUDE_SCOREBOARD = True works)
# ---------------------------------------------------------------------------
INCLUDE_SCOREBOARD = False
SCOREBOARD_HEADER = "Chip scoreboard"
SCOREBOARD_EMOJI = "📊"
SCOREBOARD_STALE_NOTE = "Chip stocks last traded {date} (holiday or data lag)."

MARKET_TICKERS = [
    ("index", "PHLX Semi (SOX)", "^SOX"),
    ("index", "Nasdaq 100", "^NDX"),
    ("equity", "TSMC ADR", "TSM"),
    ("equity", "Nvidia", "NVDA"),
    ("equity", "ASML", "ASML"),
    ("equity", "Broadcom", "AVGO"),
    ("equity", "Micron", "MU"),
    ("equity", "Intel", "INTC"),
]
PRIMARY_GROUPS = {"index", "vol", "rates", "commodity"}

# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
# 13h back from 08:30 SGT reaches ~19:30 SGT the prior evening -- the whole US
# cash session (opens 21:30 SGT) plus after-hours. From 18:30 SGT it reaches
# ~05:30 SGT, i.e. the full Asian trading day. Monday widens to Saturday to catch
# weekend policy moves even though the brief now also runs Sat/Sun.
LOOKBACK_HOURS = 13
LOOKBACK_HOURS_MONDAY = 62

MAX_ITEMS_PER_SECTION = 4

# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
SECTION_ORDER = ["policy", "supply_chain", "news"]
SECTION_TITLES = {
    "news": "News — M&A, capex, capacity, earnings",
    "supply_chain": "Supply chain signals",
    "policy": "Policy & geopolitics",
}
SECTION_EMOJI = {
    "news": "📰",
    "supply_chain": "🔗",
    "policy": "🏛️",
}

# ---------------------------------------------------------------------------
# News sources
# ---------------------------------------------------------------------------
GOOGLE_NEWS_QUERIES = [
    ("news", "semiconductor chip earnings revenue guidance when:1d"),
    ("news", "TSMC OR Nvidia OR AMD OR Intel OR Micron OR Broadcom OR Qualcomm chip when:1d"),
    ("news", "Samsung OR \"SK Hynix\" OR ASML OR \"Applied Materials\" semiconductor when:1d"),
    ("news", "semiconductor acquisition merger deal stake when:1d"),
    ("news", "chip fab capex capacity expansion investment plant when:1d"),
    ("supply_chain", "DRAM NAND memory chip price when:1d"),
    ("supply_chain", "HBM high bandwidth memory supply demand when:1d"),
    ("supply_chain", "chip foundry utilization wafer price increase when:1d"),
    ("supply_chain", "semiconductor shortage lead times inventory glut when:1d"),
    ("policy", "chip export controls China semiconductor restrictions when:1d"),
    ("policy", "CHIPS Act semiconductor subsidy grant funding when:1d"),
    ("policy", "EU Chips Act OR Japan OR Korea semiconductor subsidy when:1d"),
    ("policy", "semiconductor tariff entity list sanctions when:1d"),
]

# (section, name, url). Chip-topical publisher feeds; anything off-topic is caught
# by the relevance gate + source weights, so STRICT_DIRECT_FEEDS stays False.
DIRECT_FEEDS = [
    ("news", "EE Times", "https://www.eetimes.com/feed/"),
    ("news", "Tom's Hardware", "https://www.tomshardware.com/feeds/all"),
    ("news", "The Register", "https://www.theregister.com/headlines.atom"),
    ("supply_chain", "DIGITIMES", "https://www.digitimes.com/rss/daily.xml"),
]

# Source-name -> weight, matched after normalisation (strip leading "The ",
# strip trailing ".com"/etc). Exact match only. Unknown source -> dropped
# (DEFAULT_SOURCE_WEIGHT = 0), so the chip trade press must be listed here.
SOURCE_WEIGHTS = {
    "Reuters": 10, "Bloomberg": 10, "Financial Times": 10, "Wall Street Journal": 10,
    "WSJ": 10, "The Wall Street Journal": 10, "The Economist": 9,
    "Nikkei Asia": 9, "Nikkei Asian Review": 9, "Nikkei": 8,
    "CNBC": 9, "Associated Press": 8, "AP News": 8, "Barron's": 8,
    "Reuters Technology": 10, "Bloomberg Technology": 10,
    "South China Morning Post": 8, "SCMP": 8, "The Information": 9, "Semafor": 7,
    # Chip trade press / technical
    "DIGITIMES": 8, "DIGITIMES Asia": 8, "Digitimes": 8, "TrendForce": 8,
    "EE Times": 8, "EETimes": 8, "Semiconductor Engineering": 8, "SemiAnalysis": 8,
    "IEEE Spectrum": 7, "Tom's Hardware": 6, "AnandTech": 7, "The Register": 6,
    "Ars Technica": 6, "The Verge": 6, "TechCrunch": 6, "Light Reading": 5,
    "Electronic Design": 5, "Fierce Electronics": 5, "Electronics Weekly": 6,
    "The Elec": 6, "TheElec": 6, "Commercial Times": 5, "United Daily News": 5,
    # Asia general with strong chip desks
    "Yonhap": 7, "Yonhap News Agency": 7, "The Korea Herald": 6, "The Korea Times": 6,
    "Korea JoongAng Daily": 6, "KED Global": 6, "The Korea Economic Daily": 6,
    "Focus Taiwan": 6, "Taipei Times": 6, "Taiwan News": 5, "CNA": 7,
    "Channel NewsAsia": 7, "Kyodo News": 6, "Japan Times": 6, "Nikkei Asia (nar)": 9,
    "Caixin": 7, "Caixin Global": 7, "The Straits Times": 7, "Straits Times": 7,
    "The Business Times": 6, "Business Times": 6,
    # Company newswires -- carry the primary earnings / deal releases
    "PR Newswire": 6, "PRNewswire": 6, "Business Wire": 6, "BusinessWire": 6,
    "GlobeNewswire": 5, "Globe Newswire": 5,
    # US general
    "CNN": 6, "New York Times": 8, "The New York Times": 8, "Fortune": 6,
    "Forbes": 5, "Business Insider": 5, "MarketWatch": 7, "Yahoo Finance": 6,
    "Investing.com": 5, "Seeking Alpha": 5, "The Motley Fool": 3,
    "S&P Global": 6, "Politico": 6, "Axios": 6, "Nikkei Asia Review": 9,
    # State media -- kept but low so they only surface when nothing else has it.
    "Global Times": 3, "Xinhua": 3, "China Daily": 3, "CGTN": 3,
}
DEFAULT_SOURCE_WEIGHT = 0   # unknown source -> dropped (allowlist behaviour)
MIN_SOURCE_WEIGHT = 5

# Listicle / evergreen / promo / opinion markers -- each hit subtracts
# DOWNRANK_PENALTY from the score (enough to bury a top-tier source's item).
DOWNRANK_PENALTY = 9
DOWNRANK_PATTERNS = [
    "how to", "here are", "here's why", "here is why", "heres why", "should you buy",
    "best stocks", "stocks to buy", "stock to buy", "motley fool", "reasons to", "ways to",
    "could make you", "millionaire", "if you invested", "if you'd invested", "my top",
    "watch this", "is it too late", "prediction:", "better buy", "vs.", "which stock",
    "what to know", "what to watch", "things to know", "your money", "best gpu", "best cpu",
    "review:", "hands-on", "deal:", "deals:", "discount", "on sale", "prime day",
    "buying guide", "how much", "5 things", "3 things", "7 things", "top 5", "top 10",
    "explained", "everything you need to know", "what it means for you",
]

# Dropped outright regardless of source: forward-looking previews and non-news
# quote / liveblog pages.
DENY_PATTERNS = [
    "week ahead", "week-ahead", "the week that was", "what to expect this week",
    "day ahead", "coming week", "premarket:", "pre-market:", "stocks to watch",
    "things to watch", "top 10 things", "10 things to watch", "earnings preview",
    "what to expect", "preview:", "stock quote", "quote price", "price and forecast",
    "quote and news", "as it happened", "live updates:", "live blog", "livestream",
    "in pictures", "in photos", "podcast:", "webinar",
]

# Each headline is classified by its own words; first section whose list matches
# wins, so order matters -- policy and supply_chain are checked before the
# catch-all news bucket.
SECTION_KEYWORDS = {
    "policy": [
        "export control", "export controls", "export curb", "export curbs", "export ban",
        "export licence", "export license", "entity list", "sanction", "sanctions",
        "chips act", "chip act", "eu chips act", "subsidy", "subsidies", "state aid",
        "grant", "grants", "tariff", "tariffs", "section 232", "section 301",
        "commerce department", "bureau of industry", "white house", "national security",
        "trade war", "chip war", "u.s.-china", "us-china", "beijing", "smuggling",
        "restrict", "restriction", "restrictions", "curbs", "sovereignty", "geopolit",
        "outbound investment", "screening", "ban on", "blacklist", "clawback",
    ],
    "supply_chain": [
        "dram", "nand", "hbm", "memory price", "memory prices", "spot price", "spot prices",
        "contract price", "contract prices", "price hike", "price hikes", "price increase",
        "price cut", "price cuts", "lead time", "lead times", "utilization", "utilisation",
        "capacity utilization", "inventory", "inventories", "shortage", "shortages", "glut",
        "oversupply", "undersupply", "wafer price", "wafer prices", "foundry price",
        "overbooking", "double ordering", "double-ordering", "allocation", "tight supply",
        "supply crunch", "burn rate", "restocking", "destocking", "bit growth",
    ],
    "news": [
        "earnings", "revenue", "results", "guidance", "forecast", "quarterly", "profit",
        "sales rose", "sales fell", "acquire", "acquires", "acquisition", "merger",
        "merges", "buyout", "takeover", "stake", "spin off", "spin-off", "ipo",
        "capex", "capital expenditure", "capital spending", "fab", "foundry", "plant",
        "factory", "gigafab", "expansion", "expand", "invest", "investment", "billion",
        "roadmap", "unveil", "unveils", "launch", "launches", "tape out", "tape-out",
        "2nm", "3nm", "18a", "angstrom", "gaa", "backside power", "chiplet", "packaging",
    ],
}

# Every headline must name something chip-specific to be kept -- the gate that
# drops the generic macro / trade / big-tech stories the queries and feeds also
# surface. Deliberately excludes bare "tech" / "AI" / "trade".
REQUIRE_RELEVANCE = True
STRICT_DIRECT_FEEDS = False
RELEVANCE_TERMS = [
    "semiconductor", "semiconductors", "chip", "chips", "chipmaker", "chipmakers",
    "chip maker", "foundry", "foundries", "fab", "fabs", "wafer", "wafers",
    "nanometer", "nanometre", "nm node", "process node", "lithography", "euv",
    "dram", "nand", "hbm", "sram", "memory chip", "memory chips", "logic chip",
    "gpu", "gpus", "cpu", "ai accelerator", "ai accelerators", "ai chip", "ai chips",
    "tsmc", "samsung", "sk hynix", "hynix", "intel", "nvidia", "amd", "micron",
    "asml", "broadcom", "qualcomm", "arm holdings", "smic", "globalfoundries",
    "umc", "kioxia", "western digital", "texas instruments", "analog devices",
    "nxp", "infineon", "stmicroelectronics", "stmicro", "renesas", "rapidus",
    "applied materials", "lam research", "kla corp", "tokyo electron", "advantest",
    "onsemi", "on semiconductor", "microchip technology", "marvell", "arm ltd",
    "chips act", "wafer fab equipment", "advanced packaging", "chiplet", "chiplets",
    "die shrink", "tape-out", "tape out", "node", "2nm", "3nm", "5nm", "7nm", "18a",
]

# ---------------------------------------------------------------------------
# X / Twitter -- off. Nitter is effectively dead and chip-Twitter adds noise.
# ---------------------------------------------------------------------------
X_ENABLED = False
X_ACCOUNTS = ["chipsandcheese", "dylan522p", "IanCutress", "Semianalysis_"]
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
