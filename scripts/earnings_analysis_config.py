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
#   - Groq's free-tier chat completions (same account/GROQ_API_KEY this
#     repo's major_news_alert.py already uses, shared by choice to keep
#     setup to one key -- but on its OWN model, openai/gpt-oss-120b, since
#     2026-10-30; major_news_alert.py was switched to qwen/qwen3.8-27b that
#     day specifically because Groq tracks free-tier limits per model, not
#     per account, and the two pipelines sharing one model's 200k-tokens/day
#     bucket had been starving each other, confirmed by a real 429 during
#     testing. See _apply_detection's docstring for why a blocked report
#     still just retries on the next run rather than being lost) do the
#     actual writing/fact-checking.
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
# Fallback if LLM_MODEL is still rate-limited after its own retries (see
# _run_completion) -- added 2026-10-30 after the real-world backlog from
# major_news_alert.py sharing this exact model (before its 2026-10-30 move
# to qwen/qwen3.8-27b) left LLM_MODEL's 200k-tokens/day bucket exhausted
# for most of a day even once that cross-pipeline contention was fixed,
# since Groq's TPD window only clears 24h after each token was actually
# used. A separate model is a separate quota bucket, so this is a second,
# independent 200k/day allowance to fall through to rather than failing
# the report outright. Smaller than LLM_MODEL (20B vs 120B) -- a step down
# in quality, used only when the primary is genuinely unavailable, not the
# normal path.
LLM_FALLBACK_MODEL = "openai/gpt-oss-20b"
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
# "full report in repo" footer line added in earnings_analysis.py. The
# verify stage is told to keep the whole report under this, since (as of
# 2026-10-31) the report IS the Telegram message -- see ANALYSIS_
# INSTRUCTIONS's own FORMAT section and earnings_analysis_engine.py's
# module docstring.
TELEGRAM_DIGEST_MAX_CHARS = 3500

# The analyst brief. Reworked 2026-10-31 to match the exact Telegram format
# the user confirmed in chat -- four MANDATORY sections (Results Analysis,
# Capital Allocation, Outlook, Key Takeaways), plus an optional 5th "Others"
# for anything that genuinely doesn't fit those four -- numbers only (no
# quotes), one short line per item. The report produced here now goes
# essentially as-is to
# Telegram (see earnings_analysis_engine.py's module docstring -- the
# write/verify stages no longer produce a separate, differently-shaped
# "ultra-compact digest"), so brevity is a hard requirement, not a nice-
# to-have: this whole report must fit comfortably under Telegram's 4096-
# char limit. Edit this directly to change what every report covers --
# it's the single source of truth for the write-up stage.
ANALYSIS_INSTRUCTIONS = """\
ROLE
You are preparing a factual quarterly results brief for the company whose earnings release triggered this run, comparing the quarter against prior guidance, the street, and history (prior quarter and prior year).

FORMAT
Plain text only. Never use a markdown table, a pipe character (|), or any tabular/grid layout. Numbers only -- no quotes from management, no opinions, no predictions. Four sections are MANDATORY, exactly as named below, in that order, with those exact header words -- never rename, merge, split, or paraphrase a section header: Results Analysis, Capital Allocation, Outlook, Key Takeaways. If, and only if, something material this quarter genuinely doesn't belong in any of those four (e.g. a legal settlement, a restructuring charge, a regulatory development), add a 5th section named "Others" at the very end for it -- never force an item into one of the four mandatory sections if it doesn't belong there, but never add "Others" just to avoid deciding either; most reports should have no Others section at all. Every item is its own short line (not a long sentence) -- this is read as a Telegram message, not a report, so the whole thing must stay short. The only bullet dashes allowed are the two named, MANDATORY sub-points under Revenue (crown jewel, growth engine -- see below; never drop these two even if everything else in the section is reshuffled) -- every other item is a plain line, not a bulleted list. Skip any line entirely if the release has no information for it -- don't speculate to fill a gap, and don't explain away deterioration.

SOURCE DISCIPLINE
Use the released earnings information and this company's prior transcripts, presentations, and reports (if any) for guidance history and year-ago figures. All dates you write must be in "D Mon YYYY" format (e.g. "29 Oct 2026"), never YYYY-MM-DD.

Margins (GPM/OPM) and EPS (GAAP/non-GAAP) often aren't restated vs. the immediately prior quarter in the current release's own tables -- a company's release table typically shows current vs. year-ago, not current vs. last quarter. Where that's the case: compute GPM/OPM yourself from the quantitative snapshot's raw revenue/gross_profit/operating_income figures for all three periods (that's exactly why they're given to you as numbers, not just prose). For EPS specifically, if a block of material labeled "PRIOR QUARTER'S OWN RELEASE" is given below, pull that quarter's own stated EPS from it for the qoq comparison -- don't guess it from net income alone (share count can change).

RESULTS ANALYSIS
Revenue: one line, "Revenue: <value>, <yoy delta> yoy, <qoq delta> qoq." Then one line naming which segment(s) drove it, each segment's own value and yoy change, e.g. "Drivers: <segment> <value> <delta>% yoy; <segment> <value> <delta>% yoy."
- Crown jewel (name the segment): the segment generating the most cash/profit today -- one short line on whether it's growing, flat, or shrinking, purely from the numbers. Locked definition -- always the main cash-generating segment, never management's narrative framing of which segment matters most.
- Growth engine (name the segment): the fastest-growing segment by revenue growth rate (the up-and-coming segment, by the numbers) -- one short line with its growth rate. Locked definition -- always the segment with the highest growth rate among those broken out, never whichever segment management talks up; if two segments are close, pick the one with the higher yoy growth rate. These two sub-points are mandatory whenever the release breaks revenue out by segment at all -- skip them only if no segment-level breakdown exists anywhere in the material given.
GPM: one line, "<value>, <yoy delta> yoy, <qoq delta> qoq" (add non-GAAP too if the company reports it separately).
OPM: one line, same shape as GPM.
Net profit: one line, "<value>, <yoy delta> yoy, <qoq delta> qoq" (GAAP and non-GAAP if both reported; flag in a few words if a one-off is distorting either comparison, e.g. a divestiture gain).
EPS: one line, GAAP and non-GAAP, each vs. yoy AND qoq.
Revenue quality: one line -- mix/volume/price shift if visible from the numbers given; skip if not disclosed.
Pricing power: one line; write "not disclosed" if the release gives no price/ASP data.
Cash conversion: one line, in this exact shape: "<this quarter's ratio>, vs. <last quarter's ratio> and <a year ago's ratio>", where each ratio is that period's operating cash flow divided by that period's operating income. Compute all three yourself from the operating cash flow and operating income figures given -- do not substitute net income for operating income.
Competitive position: one line; write "not addressed in the release" if there's no share-gain/loss evidence given -- never infer this.
Key operational metric(s): MANDATORY unless the sourced material has genuinely no operational/KPI data anywhere in it -- this is NOT optional just because it takes more looking. Check specifically for a block labeled "OPERATING / BUSINESS METRICS" in the sourced material (given for SEC filers), in addition to the main narrative, before concluding there's nothing to report -- if that block exists, you MUST use it, it was fetched specifically for this line. One line -- whichever operational/KPI metric(s) this company usually discloses beyond the headline financials (e.g. Amazon: paid-unit growth, shipping costs, headcount; a telco: subscriber net adds/churn; a retailer: same-store sales; a chipmaker: wafer shipments/ASP; a SaaS company: net revenue retention/ARR). Name the metric explicitly, give this quarter's value plus yoy AND qoq delta where disclosed. Only write "not disclosed" if neither the narrative nor any OPERATING / BUSINESS METRICS block contains such a figure.
vs. guidance: one line -- beat/met/missed against the guidance THIS company itself gave last quarter for the quarter just reported, quantified where possible.
vs. consensus: one line -- beat/met/missed against the Street. Use the "EPS estimate" / "Reported EPS" / "Surprise%" figures given in the quantitative snapshot exactly as given (don't recompute or guess them) -- e.g. "EPS <reported>, vs. <estimate> consensus (<surprise%>% surprise)". Skip this line only if the snapshot gives no estimate figure at all.

Every delta above is yoy AND qoq where the underlying data given to you supports it (see the margins/EPS note above) -- don't drop qoq just because yoy is available; only drop one side if that specific comparison genuinely isn't in the material given.

CAPITAL ALLOCATION
One line covering whichever of buybacks/dividends/capex/divestment/acquisition the company reported this quarter, each item in this exact shape: "<item> <this quarter's value>, vs. <prior quarter's value> last quarter and <prior year's value> a year ago" -- semicolon-separate multiple items on the same line. Give the actual dollar figures for all three periods side by side, not just a description of what changed -- skip a period's figure only if it genuinely isn't in the material given. If capex is one of the items disclosed, this same line doubles as the capital-intensity read (it already carries both yoy and qoq change) -- don't add a separate line for that.

OUTLOOK
New guidance from management: whichever of revenue/EPS/gross margin/operating margin the company guided for next quarter, one line each, in this exact shape: "<metric>: <guided value>, implies <qoq delta>% qoq vs. this quarter's own <actual value>, <yoy delta>% yoy vs. <that same metric's value a year ago in the quarter the guidance is FOR>" -- not vs. the quarter just reported. The quantitative snapshot gives you this quarter's own actual figures (qoq base) and a separate block explicitly labeled as the yoy base for the GUIDED quarter -- use that block for yoy, never the ordinary yoy_prior block (that one is a year ago from the quarter just reported, a different quarter). Compute both deltas yourself from the raw figures given; only drop a side if that specific baseline figure genuinely isn't in the material given. Add management's own one-line qualitative assumption (e.g. a cited market condition or demand signal) after the numbers if the release gives one, numbers-only otherwise. Skip a metric entirely if the company didn't guide it this quarter.

KEY TAKEAWAYS
3-4 short plain lines synthesizing the quarter -- one each on: the headline result (beat/met/missed, both guidance and consensus), the crown jewel segment's health, the growth engine segment's momentum, and the single most important thing from Outlook (the new guidance direction, or the biggest flagged risk). Every line here must restate a figure or conclusion already given in one of the sections above -- never introduce a new figure, claim, or opinion that isn't already in the report.

OTHERS (optional -- only include this section at all if something material this quarter genuinely doesn't fit Results Analysis, Capital Allocation, Outlook, or Key Takeaways, e.g. a legal settlement, a restructuring charge, a leadership change, a regulatory development)
One short line per item, same numbers-only style as everywhere else. Omit this section entirely -- don't even write the header -- if nothing like this came up this quarter; most reports should have no Others section at all.
"""
