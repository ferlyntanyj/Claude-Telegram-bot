"""
Shared config for the daily earnings-analysis alert (earnings_analysis.py /
earnings_analysis_engine.py).

Watchlist files are plain CSVs under ../data/, one per group, each with
columns `ticker,company`. The filename (minus the `earnings_watchlist_` /
`.csv`) is used as the group label shown in Telegram. Add/remove a company
by editing the relevant CSV directly in GitHub -- no code changes needed.
Add a new group by adding another `earnings_watchlist_<group>.csv` file here
and listing it in WATCHLIST_FILES below.

Tickers are Yahoo Finance symbols (yfinance), including exchange suffixes
where needed (e.g. "558.SI" for SGX, "2330.TW" for Taiwan, "6857.T" for
Tokyo, "0128.KL" for Bursa Malaysia, "HANA.BK" for Thailand).
"""

WATCHLIST_FILES = [
    ("AI Capex", "../data/earnings_watchlist_ai_capex.csv"),
    ("SGX", "../data/earnings_watchlist_sgx.csv"),
    ("Bursa", "../data/earnings_watchlist_bursa.csv"),
    ("Thailand", "../data/earnings_watchlist_thailand.csv"),
]

STATE_PATH = "../data/earnings_analysis_state.json"
OUTPUT_DIR = "../output/earnings_analysis"

# A ticker's first-ever appearance in the watchlist is only baselined (its
# most recent reported quarter is recorded as "already seen"), never
# analyzed -- otherwise adding a new watchlist file, or running this for the
# first time, would fire the full paid pipeline for every single name at
# once. Analysis only fires for a report that is NEWER than whatever was
# baselined/last-alerted for that ticker.
#
# Guards against yfinance briefly returning a stale/partial row: a "new"
# reported date is only accepted if it falls within this many days of
# today, so a flaky read can't silently jump the baseline forward.
MAX_REPORT_AGE_DAYS = 4

# Everything here runs on free infrastructure, deliberately -- no paid API:
#   - Groq's free-tier chat completions (same model/account this repo's
#     major_news_alert.py already uses; GROQ_API_KEY is shared, no new
#     secret needed) do the actual writing/fact-checking.
#   - SEC EDGAR (submissions API + document archive) is the PRIMARY source
#     for every US-listed ticker (no yfinance suffix, including foreign
#     issuers like TSM/ASML that file via EDGAR as 20-F/6-K filers) --
#     genuinely free and script-friendly: the SEC *requires and documents* a
#     descriptive User-Agent for automated access (SEC_EDGAR_USER_AGENT
#     below) rather than blocking it, unlike every other exchange checked.
#   - Google News RSS (same free, no-key search this repo's
#     major_news_alert.py already uses via brief_engine._parse_feed) is the
#     fallback source for every non-US ticker. Not the primary exchange
#     filing -- SGX/HKEX/Bursa/SET/TDnet/MOPS either sit behind bot
#     protection (api.sgx.com returns a flat "Access Denied" to datacenter
#     traffic, confirmed by testing) or simply have no free developer feed,
#     and this repo does not build around another site's anti-bot measures.
LLM_MODEL = "openai/gpt-oss-120b"
LLM_WRITE_MAX_TOKENS = 2500
LLM_VERIFY_MAX_TOKENS = 2500
# Free tier is 8,000 tokens/minute, 30 requests/minute (see
# major_news_alert_config.py's own note on this) -- a transient 429 is
# retried once with backoff rather than dropping the report.
LLM_RATE_LIMIT_MAX_RETRIES = 1
LLM_RATE_LIMIT_RETRY_SECONDS = 20

# Required by SEC's fair-access policy: a real User-Agent string
# identifying the requester, or EDGAR returns 403. Does not need to be a
# real/personal contact -- "example.com" is the IETF-reserved placeholder
# domain that never resolves to anyone, chosen deliberately over a personal
# address since this sits in a file committed to the repo.
SEC_EDGAR_USER_AGENT = "LiquidityMomentumSGX-EarningsBot contact@example.com"

# Human label per exchange, keyed by the ticker's yfinance suffix (no
# suffix == US-listed / SEC EDGAR). Used in prompts and to log which
# grounding path (EDGAR vs Google News) a ticker took.
EXCHANGE_LABELS = {
    "SI": "SGX", "HK": "HKEX", "KL": "Bursa Malaysia",
    "BK": "SET (Thailand)", "T": "Tokyo Stock Exchange", "TW": "Taiwan Stock Exchange",
}


def exchange_label_for(ticker):
    if "." in ticker:
        suffix = ticker.rsplit(".", 1)[1]
        if suffix in EXCHANGE_LABELS:
            return EXCHANGE_LABELS[suffix]
    return "US-listed (SEC EDGAR)"


def is_edgar_ticker(ticker):
    """No yfinance suffix == US-listed, including foreign issuers (TSM,
    ASML) that file via EDGAR as 20-F/6-K filers."""
    return "." not in ticker


# Google News search (non-EDGAR tickers' grounding, and the free detection
# fallback for tickers yfinance has no data on at all). Singapore locale,
# since most non-US names in this watchlist are SGX/regional -- unlike
# major_news_alert_config's US locale, which is tuned for a different,
# globally-scoped alert.
GOOGLE_NEWS_LOCALE = ("en-SG", "SG", "SG:en")
HTTP_TIMEOUT_SECONDS = 15
# The RSS search itself reuses brief_engine's own session (already carries a
# UA). This one is for fetching the actual linked article pages afterward --
# a browser UA is standard, polite HTTP client behaviour for an ordinary
# public page, not bot-check circumvention (no bot wall here to get past,
# unlike api.sgx.com above); it just avoids being rejected as an empty/
# default python-requests UA, same as major_news_alert_config.USER_AGENT.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Earnings-specific keyword gate for the free news-based detection fallback
# (_check_reported_via_news) -- a plain company-name search surfaces lots of
# unrelated coverage; a headline must contain one of these to count as a
# results signal.
EARNINGS_KEYWORDS_RE = (
    r"\b(results|earnings|profit|net profit|revenue|dividend|half[- ]year|"
    r"full[- ]year|interim|quarterly|1h20|2h20|q[1-4]\s?20|fy20)\b"
)

# Telegram HTML message hard limit is 4096 chars; leave headroom for the
# "full report in repo" footer line added in earnings_analysis.py.
TELEGRAM_DIGEST_MAX_CHARS = 3500

# Marker the verify stage is instructed to print on its own line, separating
# the corrected full report (saved to OUTPUT_DIR) from the short digest
# (sent to Telegram) -- avoids a second LLM call just to re-summarize.
DIGEST_MARKER = "===TELEGRAM DIGEST==="

# The analyst brief, pasted verbatim from the user's "quarterly earnings
# analysis" Claude Project. Edit this directly to change what every report
# covers -- it's the single source of truth for the write-up stage.
ANALYSIS_INSTRUCTIONS = """\
ROLE
You are a senior fundamental equity analyst at a multi-manager platform monitoring the company whose earnings release triggered this run. You assess the quarter against prior guidance, the street, and history (prior quarter and prior year), and test whether the fundamentals of the business model have changed.

SOURCE DISCIPLINE
Use the released earnings information and this company's prior transcripts, presentations, and reports (if any) for guidance history and year-ago figures.

SECTION 1: RESULTS ANALYSIS
Revenue growth (yoy, qoq) -> explain which segment drove this growth and what key operating metrics (volume/ASP) drove the growth (if its relevant)
-	Crown jewel: the main cash-generating segment. Is it still growing, defending share, and funding the rest?
-	Growth engine: the segment management says drives the future. Is it scaling as promised, and are its economics improving?
Gross profit margin or operating profit margin (whichever is the highlight of this company) movements compared to prior quarter and prior year -> explain the reason for expansion or contraction
Net profit growth (yoy, qoq) -> if its growing faster/slower than revenue growth, explain why
Analyse any key operating metric (this can be quite broad: ASP, volume, AUM, GMV, take-rate, margins etc.) this company shares - what does it mean for the company?
Share any capital allocation plans company has - share buybacks, dividends, divestment, acquisition plans etc (compare to prior quarter/year to understand the impact of this new capital allocation strategy).

SECTION 2: GUIDANCE vs REALITY vs STREET
Prior guidance (quote) vs actual (quote) vs consensus: Beat / Met / Missed against each, quantified. If consensus is not available, you can put "NA".
(If any) Management's explanation (quote); has it been used before (check prior transcripts if any); data-backed or vague?
(If any) NEW guidance: raised / cut / held (quote), and the assumptions baked into it (quote)
Analyse management's tone - were there changes from prior quarter? Any new strategy highlighted and what is management emphasising this quarter.
One-line judgement on management credibility this quarter.

SECTION 3: MARKET REACTION
If the price move is available: state it, reward or punish, and whether it is proportionate to the numbers (beat but sold off, miss but rallied). Analyse the reason why the market reacted that way.
If not available at run time: write "Reaction not yet available" and instead state what the print needed to show to be rewarded vs punished, given the guidance and Q&A expectations above.

SECTION 4: AGREE OR DISAGREE
Only if the reaction is available. Agree with it? Reason it out. Who is likely on the other side and why (guidance vs consensus gap, forced sellers, multiple re-rating, consensus already ahead). Divergence: opportunity, warning, or noise. An unexplained move is a risk flag, not confirmation.

SECTION 5: FINANCIAL METRICS ANALYSIS
Test whether this quarter changed how the company makes money. For each component below, mark Strengthened / Unchanged / Weakened, with a quote, and state whether the change looks one-off or structural. Some metrics might not have information, skip it if it isn't relevant:

Revenue quality: volume vs price vs mix; recurring vs one-off; customer concentration or churn signals.
Pricing power: did they raise prices, hold them, or discount? Did volumes hold after price changes?
Cost structure and operating leverage: are margins expanding as revenue grows, or are costs rising faster?
Cash conversion and capital intensity: is profit turning into cash faster or slower, and is CAPEX rising to sustain growth (asset turn)?
Competitive position: any evidence of share gains or losses, new entrants, or customers switching.

Close with the strongest counter-argument to your flag, and 2 to 3 datapoints next quarter that would confirm or reverse it.

Keep facts quoted. Do not explain away deterioration.
"""
