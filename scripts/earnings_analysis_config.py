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
#     major_news_alert.py already uses; GROQ_API_KEY is shared, by choice,
#     to keep setup to one key -- that account's 200k tokens/day cap can
#     run out before this pipeline gets a turn, confirmed by a real 429
#     during testing, since major_news_alert.py polls far more often; see
#     _apply_detection's docstring for why a blocked report just retries on
#     the next run rather than being lost) do the actual writing/
#     fact-checking.
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

# The analyst brief. Originally pasted verbatim from the user's "quarterly
# earnings analysis" Claude Project, then deliberately stripped of every
# judgment/opinion call (management tone, credibility, agree-or-disagree,
# strengthened/unchanged/weakened labels, counter-arguments) at the user's
# request, keeping only what's comparison or calculation -- a smaller free
# model (Groq) grounded in thin material (news headlines, not always full
# transcripts) is far more likely to hold up on "compute this delta" than
# on "read whether management was credible this quarter." Edit this
# directly to change what every report covers -- it's the single source
# of truth for the write-up stage.
ANALYSIS_INSTRUCTIONS = """\
ROLE
You are preparing a factual quarterly results brief for the company whose earnings release triggered this run, comparing the quarter against prior guidance, the street, and history (prior quarter and prior year). State figures and direct quotes; do not offer opinions, predictions, interpretations of tone, or judgment calls on what any of it means.

SOURCE DISCIPLINE
Use the released earnings information and this company's prior transcripts, presentations, and reports (if any) for guidance history and year-ago figures.

SECTION 1: RESULTS ANALYSIS
Revenue growth (yoy, qoq), with the segment breakdown if the company reports one -- state each segment's revenue and growth. Don't infer which segment "drove" the growth beyond what the breakdown itself shows.
Gross profit margin or operating profit margin (whichever the company itself highlights) movements vs prior quarter and prior year, in bps. State the company's own stated driver ONLY if explicitly given (quote it) -- do not infer a reason.
Net profit growth (yoy, qoq) vs revenue growth (yoy, qoq) -- state both side by side. No explanation of any gap between them unless explicitly quoted from the company.
Key operating metric(s) the company reports (ASP, volume, AUM, GMV, take-rate, etc.) -- state the figures and their yoy/qoq change.
Capital allocation: buybacks, dividends, divestment, acquisition plans this quarter vs prior quarter/year -- state the figures (amounts, dates, rates).

SECTION 2: GUIDANCE vs REALITY vs STREET
Prior guidance (quote) vs actual (quote) vs consensus: Beat / Met / Missed against each, quantified. If consensus is not available, put "NA".
(If any) NEW guidance: raised / cut / held (quote), and the assumptions given (quote).

SECTION 3: MARKET REACTION
If the price move is available: state it (%), and whether EPS/revenue beat or missed consensus (cross-reference Section 2).
If not available at run time: write "Reaction not yet available".

SECTION 4: FINANCIAL METRICS
For each component below, state the numbers found. Skip a component entirely if there's no information for it -- don't speculate to fill the gap.

Revenue quality: break down volume vs price vs mix (if reported); identify any one-off/non-recurring items this quarter (quote them) with $ amount; recurring/underlying revenue and net profit with one-offs stripped out, plus their own yoy/qoq growth; customer concentration % or churn figures if reported.
Pricing power: price changes made this quarter (amount/%, quote) and volume change over the same period -- state both figures side by side.
Operating leverage: revenue growth rate vs operating cost growth rate (or opex ratio change) this quarter -- state whether revenue grew faster than costs purely from the numbers, with the actual growth rates/margin change in bps.
Cash conversion and capital intensity: operating cash flow vs net income (cash conversion ratio) this quarter and prior quarter/year; capex amount and capex/revenue ratio vs prior quarter/year.

Keep facts quoted. Do not explain away deterioration.
"""
