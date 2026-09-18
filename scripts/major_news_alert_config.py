"""
Tuning knobs for the global major-news Telegram alert. Consumed by
major_news_engine.py via the thin major_news_alert.py wrapper.

Unlike the scheduled digests (morning/evening/semis brief), this is meant to
be a near-real-time alert stream: one run = one poll cycle via GitHub Actions
(.github/workflows/major_news_alert.yml) so it doesn't depend on this machine
being on. It only sends a Telegram message when a qualifying move is found,
rather than always producing a digest.

Price-first (redesigned 2026-09-18, see major_news_engine's module
docstring for the full "why"): every cycle scans the ENTIRE curated
watchlist (data/global_watchlist.csv, built by global_watchlist_build.py)
for price moves via Yahoo Finance, rather than scanning headlines first and
trying to match them to watchlist names. A headline is only looked up
afterwards, per qualifying mover, for display/grounding -- it can no longer
suppress a real move just because it used unexpected phrasing or wasn't in
a covered feed, which was the old design's structural weakness.

Two trigger paths, both ending in the same Telegram format (market/time,
primary mover, a "Sentiment" line, an "Analysis" section with why-it-moved +
industry read-across, up to MAX_DISPLAY_PEERS peer movements, a "Look out"
forward-looking line, and a "Memory" line recalling a historical parallel if
the model has one -- see major_news_alert.render_telegram):
  1. Single-stock: any watchlist ticker whose own move clears
     SINGLE_STOCK_MOVE_PCT. Peers shown alongside it come from other
     watchlist names in the same industry (major_news_engine.same_group_peers),
     computed for free from the same scan -- no LLM call.
  2. Sector-wide: this cycle's movers are grouped by the watchlist's real
     `industry` classification (not an LLM-guessed peer basket); if a
     group's MEDIAN move clears SECTOR_MEDIAN_MOVE_PCT (or enough of it
     individually clears SECTOR_BREADTH_MOVE_PCT), it qualifies. Median/
     breadth rather than a plain average, deliberately -- a single outlier
     in a large group shouldn't be able to drag a mean over the line. The
     group's biggest mover becomes the "primary" line; the rest become its
     peers.

write_analysis() (Groq) is the only LLM call left anywhere in this
pipeline, once per qualifying alert -- it also grounds sector-wide write-ups
in a handful of other recent headlines about the same story
(major_news_engine.gather_related_headlines), same as before.
"""

# ---------------------------------------------------------------------------
# Output / state
# ---------------------------------------------------------------------------
WATCHLIST_CSV_PATH = "../data/global_watchlist.csv"
STATE_JSON_PATH = "../data/alert_state.json"
OUT_LOG_JSON_PATH = "../output/major_news_alert_log.json"  # append-only run log, for auditing

GOOGLE_NEWS_LOCALE = ("en-US", "US", "US:en")

# Peers shown in the Telegram message's "Peers movement" section, for BOTH
# alert types (see major_news_engine.same_group_peers / the sector-group
# ranking in run_cycle) -- not the same as SECTOR_MIN_PEERS below, which
# gates qualification, not display.
MAX_DISPLAY_PEERS = 4

# ---------------------------------------------------------------------------
# Price scan -- chunked yf.download() batching over the full watchlist every
# cycle (major_news_engine.scan_watchlist_moves), not per-ticker calls; see
# that function's docstring for why (~1,400 names, every ~15-20 min, would
# be far too slow/rate-limit-fragile one ticker at a time).
# ---------------------------------------------------------------------------
PRICE_SCAN_CHUNK_SIZE = 250
PRICE_SCAN_MAX_PASSES = 2          # retry only a chunk's still-missing tickers, not the whole chunk
PRICE_SCAN_RETRY_COOLDOWN_SECONDS = 20

# ---------------------------------------------------------------------------
# Move thresholds
# ---------------------------------------------------------------------------
SINGLE_STOCK_MOVE_PCT = 5.0
SECTOR_MEDIAN_MOVE_PCT = 3.0
SECTOR_BREADTH_MOVE_PCT = 2.0     # per-peer bar used for the breadth check
SECTOR_BREADTH_SHARE_PCT = 60.0   # % of the industry group that must clear it
SECTOR_MIN_PEERS = 3              # fewer than this and "median"/"breadth" isn't meaningful

# Cooldown: one alert per story/ticker per this many hours, full stop -- even
# a further-deepening move doesn't re-trigger within the window (that used to
# be allowed via a delta-escalation bypass; removed 2026-09-14 after it caused
# Fujitsu and Applied Materials to each fire multiple times in one day as they
# drifted further past the threshold intraday). Set to span a full trading
# day (not exactly 24h, so a fresh move at the next day's open isn't blocked
# by an alert that fired late in the prior session) so "once per day, unless
# it moves >=5% again the next day" is the practical behaviour.
COOLDOWN_HOURS = 18.0

# ---------------------------------------------------------------------------
# LLM (Groq API -- free tier, no credit card required; needs GROQ_API_KEY
# set, from a key created at console.groq.com. Gemini's free tier was tried
# first but requires paid billing for requests originating from the EEA/UK/
# Switzerland, which GitHub Actions runners can't reliably avoid.)
#
# llama-3.3-70b-versatile was deprecated by Groq on 2026-06-17 (confirmed via
# a live 404 model_not_found from the API on 2026-09-11 -- Groq's own docs
# pages were still claiming it was current when checked the same day, so
# trust runs against the real API over their docs if this drifts again).
# ---------------------------------------------------------------------------
LLM_MODEL = "openai/gpt-oss-120b"
# The analysis call returns 5 JSON-structured sections (sentiment/why_moved/
# read_across/look_out/memory) instead of one blob -- more headroom than the
# old single-paragraph LLM_SIGNIFICANCE_MAX_TOKENS=300. Sector-wide alerts'
# prompt also grows with related-coverage context (gather_related_headlines),
# so this has a bit of margin beyond the 4-field version.
LLM_ANALYSIS_MAX_TOKENS = 700
# Free tier is 8,000 tokens/minute (verified via Groq's docs 2026-09-15) --
# tighter than the 30 requests/minute cap once each call's ~700-token max
# output plus its prompt is counted. Confirmed 2026-09-18: the price-first
# redesign's very first cycle surfaced 52 qualifying alerts at once (every
# cooldown key was new that first time), firing write_analysis() back-to-back
# and failing 60/66 write-ups that cycle to 429s. Cooldown means a burst
# that size should be rare going forward, but a genuinely volatile day could
# still produce a similar one -- pacing calls (run_cycle) and retrying a 429
# with backoff (_run_completion) together absorb that instead of silently
# degrading most of a busy cycle's alerts.
LLM_CALL_PACING_SECONDS = 10
LLM_RATE_LIMIT_MAX_RETRIES = 2
LLM_RATE_LIMIT_RETRY_SECONDS = 20

# ---------------------------------------------------------------------------
# HTTP -- used by gather_related_headlines()'s targeted per-mover/per-industry
# Google News search (the only news-fetching this alert does now).
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT_SECONDS = 15
