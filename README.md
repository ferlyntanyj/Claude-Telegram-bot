# SGX Liquidity Momentum Screener

Ranks SGX mainboard + Catalist stocks by ADTV surge ratio:

```
surge_ratio = (trailing 5-trading-day average daily traded value) / (full-year 2024 average daily traded value)
```

Two liquidity floors are applied before ranking: 2024 ADTV >= SGD 100,000/day (excludes
near-untraded microcaps where a single small trade inflates the ratio off pure noise) AND
current/trailing-5-day ADTV >= SGD 500,000/day (excludes names that aren't presently trading
at a practically tradable size, however impressive the ratio looks). See
`scripts/03_compute_screener.py` for the full rationale and `MIN_ADTV_2024_SGD` /
`MIN_ADTV_RECENT_SGD` to adjust them.

## Pipeline

Run in order from the `scripts/` directory:

1. `01_get_universe.py` — pulls all SGX mainboard + Catalist stock tickers from SGX's public
   securities API (`api.sgx.com/securities/v1.1`) -> `data/universe.csv`
2. `02_fetch_history.py` — pulls daily close/volume from Yahoo Finance (2024-01-01 to present)
   for every ticker -> `data/history.parquet`
3. `03_compute_screener.py` — computes ADTV 2024, trailing 5-day ADTV, and surge ratio for every
   ticker -> `output/liquidity_momentum_screener.csv` (full), `output/liquidity_momentum_screener_liquid.csv`
   (liquidity-floor applied), `output/liquidity_momentum_screener_top10.csv` (top 10)
4. `04_build_workbook.py` — builds the formatted Excel deliverable ->
   `output/SGX_Liquidity_Momentum_Screener.xlsx`
5. `05_weekly_diff.py` — compares this run's top 10 against the most recent snapshot in
   `history/`, reports new entrants / drop-offs -> `output/weekly_summary.md`, and saves this
   run's snapshot to `history/top10_<date>.csv`
A delivery script runs after step 5 (not part of the numbered sequence above, since it's not
part of the buy-back pipeline's own numbering below either):

- `send_telegram_message.py` — sends the summary as a Telegram message, then the workbook
  as a document attachment, via a Telegram bot. Requires `SGX_SCREENER_TELEGRAM_BOT_TOKEN`
  and `SGX_SCREENER_TELEGRAM_CHAT_ID` (see below). `get_telegram_chat_id.py` is a one-time
  helper to discover the chat id.

`send_weekly_email.py` also exists (Gmail SMTP) but is currently unused — not wired into the
GitHub Actions workflow or the local runner, and no `SGX_SCREENER_GMAIL_APP_PASSWORD` secret is
configured. Telegram is the only active delivery channel. Re-enable email by adding that secret
and adding the step back to `.github/workflows/weekly_screener.yml` if wanted later.

## Share buy-back alert

Separate daily pipeline that screens SGX's public company-announcements feed for "Share Buy
Back-On Market" notices (category `ANNC` / sub `ANNC13`) and flags which companies bought back
shares most recently:

6. `06_fetch_buyback_announcements.py` — calls SGX's announcements API for a trailing 14-day
   window and merges any new buy-back notices into the running log at
   `history/buyback_history.csv` (deduped by announcement id, so it's safe to re-run).
7. `07_buyback_alert.py` — takes every company that filed a buy-back on the most recent trading
   day in the history, and reports the prior buy-back date on record for that company and how
   many days elapsed since then (i.e. an ongoing daily programme vs. a resumption after a gap)
   -> `output/buyback_alert.md` and `output/buyback_alert.csv`. Each company name links directly
   to that day's SGX filing. Companies whose prior buy-back was within
   `EXCLUDE_IF_PRIOR_BUYBACK_WITHIN_DAYS` days (default 5, set in `buyback_config.py`) are
   filtered out as routine daily/near-daily programmes, so the alert surfaces only resumptions
   after a longer gap plus companies with no prior buy-back on record; the `.md` still notes how
   many were excluded. Set the constant to 0 to list every filer.
8. `08_buyback_company_summary.py` — collapses the full history into one row per company (not
   just today's filers), alphabetical, each linking to its most recent buy-back filing -> `output/
   buyback_company_summary.md` and `.csv`. Use this to look up a specific company's last buy-back
   date directly instead of scrolling `history/buyback_history.csv`, which is one flat
   chronological log of every company's every filing.

A delivery script runs after step 8:

- `send_buyback_telegram.py` — reads `output/buyback_alert.csv` (already filtered by step 7) and
  sends one Telegram message (auto-split if it would exceed Telegram's 4096-char limit): the
  latest trading day with filings (formatted `2 Sep 2026`) and the filter scope line, then one
  line per company with the previous buy-back date on record and the day-gap, each company name
  linking to that day's SGX filing. Reuses the same `SGX_SCREENER_TELEGRAM_BOT_TOKEN` /
  `SGX_SCREENER_TELEGRAM_CHAT_ID` as the weekly screener.

`buyback_config.py` holds the shared tuning knob (`EXCLUDE_IF_PRIOR_BUYBACK_WITHIN_DAYS`) that
both step 7 and the Telegram script read, so the filter threshold and the message wording stay
in sync.

## News briefs (US morning + Asia evening + global semis)

Standalone news pipelines, unrelated to SGX, that deliver a concise "what happened" headline
digest to the same Telegram chat. Deterministic, no LLM. All run on one shared engine
(`brief_engine.py`) and one shared Telegram sender (`brief_telegram.py`); each brief is just a
config module plus a two-line wrapper, so ranking / de-duplication / windowing / rendering stay
identical between them.

| Brief | Delivered | Scope | Sections | Config |
|---|---|---|---|---|
| **US morning** | 08:30 SGT (00:30 UTC), weekdays | US market, overnight | Markets / Macro, Fed & data / Stocks & earnings / Geopolitics | `morning_brief_config.py` |
| **Asia evening** | 18:15 SGT (10:15 UTC), weekdays | ASEAN, Japan, Korea, Greater China | Macro & policy / Markets / Geopolitics | `evening_brief_config.py` |
| **Global semis** | 08:30 & 18:30 SGT (00:30 & 10:30 UTC), **every day** | Global chip industry — foundries, memory, IDMs/fabless, equipment | Policy & geopolitics / Supply chain signals / News (M&A, capex, capacity, earnings) | `semis_brief_config.py` |

Wrappers: `morning_brief.py` / `send_morning_brief_telegram.py`, `evening_brief.py` /
`send_evening_brief_telegram.py`, `semis_brief.py` / `send_semis_brief_telegram.py`. Run the
builder then the sender from `scripts/`.

The semis brief runs 7 days a week because chip export-control moves and Taiwan/Korea
supply-chain news break on weekends. It has **no synthesised "takeaway" line** — that needs a
model; the section digest is the deliverable. `DIRECT_FEEDS` leans on DIGITIMES + Tom's Hardware
+ EE Times for chip trade coverage on top of the Google News queries.

**How the engine works (both briefs):**

1. **Scoreboard** *(optional, off by default -- set `INCLUDE_SCOREBOARD = True`)* -- last close
   vs prior close for `MARKET_TICKERS` from Yahoo Finance (`yfinance`); rate symbols in basis
   points. A stale freshest bar (holiday / data lag) is flagged rather than shown as 0.00%. Off
   = no Yahoo Finance call, headlines only.
2. **Headlines** -- Google News RSS search (`when:1d`, `GOOGLE_NEWS_LOCALE`) plus `DIRECT_FEEDS`
   publisher feeds. Each headline must be inside the window (`LOOKBACK_HOURS`, wider on Monday
   via `LOOKBACK_HOURS_MONDAY`), come from an allow-listed source (`SOURCE_WEIGHTS`, unknown =
   dropped), and carry a `RELEVANCE_TERMS` term. Survivors are de-duplicated across sources,
   ranked by source trust + recency (`DOWNRANK_PATTERNS` buried, `DENY_PATTERNS` dropped
   outright), classified into `SECTION_ORDER` blocks by their own wording (`SECTION_KEYWORDS`),
   and capped at `MAX_ITEMS_PER_SECTION`. With `STRICT_DIRECT_FEEDS = True` (Asia brief), a
   publisher-feed headline is kept only if its wording matches a section's keywords -- the
   feed-level tag alone is not enough, which keeps broad feeds like Straits Times Asia from
   dumping lifestyle copy into Geopolitics.
3. **X** -- optional best-effort Nitter RSS pull (`X_ACCOUNTS`, `NITTER_INSTANCES`); most
   instances are dead or auth-walled so the block is usually just omitted. No paid X API.

Each build writes `output/<brief>.md` (human-readable, committed by the workflow as a daily
archive) and `output/<brief>.json` (structured, for the sender, gitignored). The sender renders
Telegram HTML (bold section headers, italic sources, section emoji, headline links; scoreboard
only when enabled), auto-splits at Telegram's 4096-char limit, and reuses the same
`SGX_SCREENER_TELEGRAM_BOT_TOKEN` / `SGX_SCREENER_TELEGRAM_CHAT_ID` as the other pipelines.
`--dry-run` prints the message(s) instead of sending and needs no credentials.

The headline filter is heuristic -- expect the odd marginal item; tighten `MIN_SOURCE_WEIGHT`,
narrow `RELEVANCE_TERMS`, or extend the pattern lists in the relevant config to taste.

The announcements endpoint requires a short-lived token, obtained the same way SGX's own
frontend does: fetch a public CMS field and ROT13-decode it client-side. The endpoint also sits
behind bot-fingerprinting that blocks Python's `requests`/urllib3 outright regardless of headers,
so the listing call shells out to `curl` (identical headers, not blocked).

`history/buyback_history.csv` is the persistent record — unlike `data/` (gitignored, rebuilt
every run), `history/` is committed by the scheduled run, so the log accumulates across days
instead of resetting to the 14-day lookback window each time.

## Weekly earnings calendar (top 100 S&P 500 + SOX)

Standalone weekly pipeline, unrelated to SGX, that tells you which large-cap S&P 500 names and
PHLX Semiconductor Sector Index (SOX) companies report earnings in the coming trading week, with
each company's industry. Delivered to the same Telegram chat as everything else. Deterministic,
no LLM.

- `earnings_calendar.py` — builds the universe: S&P 500 fetched live from Wikipedia via
  `global_watchlist_build.fetch_sp500()`, ranked by market cap (`yfinance` `fast_info`) and
  trimmed to the `SP500_TOP_N` largest (100 by default, set in `earnings_calendar_config.py` —
  the full ~500 names made the weekly alert too noisy), union SOX's ~30 constituents from the
  hand-maintained `SOX_CONSTITUENTS` seed list (**not** subject to the top-N cap — all SOX names
  are always included; the PHLX Semiconductor Sector index has no scrapable constituents table on
  Wikipedia, unlike the S&P 500), deduped by ticker. For every ticker in that (now much smaller)
  universe, pulls the next scheduled earnings date and industry via `yfinance`'s `.calendar` /
  `.info` (shared multi-pass `ThreadPoolExecutor` with rate-limit retry/cooldown, same shape as
  `global_watchlist_build.enrich_with_sector_industry`), then keeps only companies reporting
  within the coming Monday–Friday (the week on/after whatever day it runs, so a manual
  `workflow_dispatch` on any weekday still gives a sensible window, not just the Saturday
  schedule) → `output/earnings_calendar.csv`
  (`report_date, ticker, company_name, industry, index_membership`). Not committed to the repo —
  Telegram is the only delivery channel for this alert.
- `send_earnings_calendar_telegram.py` — reads that CSV and sends one Telegram message (grouped
  by day, each line "Company (TICKER) — Industry", auto-split at Telegram's 4096-char limit),
  reusing the same `SGX_SCREENER_TELEGRAM_BOT_TOKEN` / `SGX_SCREENER_TELEGRAM_CHAT_ID` as the
  other pipelines.

No free data source flags an upcoming earnings date as "confirmed" vs. "estimated" —
`yfinance`'s own `.calendar` and `get_earnings_dates()` can disagree by a day on the same ticker
(verified 2026-10-27). Every date is used as-is, unflagged; since the window is only the coming
week, dates are about as reliable as they'll ever get regardless of source. SOX rebalances
quarterly, so `SOX_CONSTITUENTS` is a snapshot (sourced from Nasdaq's own factsheet, data as of
2026-08-03) — re-verify against a fresh factsheet at
[nasdaq.com/solutions/global-indexes/sox](https://www.nasdaq.com/solutions/global-indexes/sox)
if holdings look stale.

Runs via `.github/workflows/earnings_calendar.yml`, every Saturday at 00:30 UTC (08:30
Asia/Singapore). Also has `workflow_dispatch` enabled for manual runs.

## Daily earnings analysis (watchlist, free pipeline)

Deep-dive companion to the weekly calendar above: when a company on your own watchlist
(`data/earnings_watchlist_*.csv` — one CSV per group, currently AI Capex, SGX, Bursa, Thailand;
edit these files directly to add/remove names, no code changes needed) actually reports earnings,
this writes a full analyst-grade report (results vs guidance vs street, management tone, market
reaction, a structural strengthened/unchanged/weakened read on the business) and pushes a short
digest to Telegram, with the full report committed under `output/earnings_analysis/`. **Entirely
free infrastructure** — Groq's free-tier chat completions (same account `major_news_alert.py`
already uses — no new secret needed), SEC EDGAR (genuinely free and script-friendly for automated
access, unlike every other exchange checked), and Google News RSS (same free, no-key search
`major_news_alert.py` already relies on) — no paid API anywhere in this pipeline.

- `earnings_analysis_engine.py` — detection: for every watchlist ticker, pulls `yfinance`'s
  `get_earnings_dates()` (falling back to the newest quarterly-financials column if that's empty)
  and compares the most recent reported date against `data/earnings_analysis_state.json`'s
  last-seen date for that ticker. A ticker's first-ever appearance is only baselined, never
  analyzed — otherwise adding a new watchlist name, or the first run ever, would fire the pipeline
  for the whole list at once.
  A handful of smaller SGX/HK names have neither signal in `yfinance` at all (confirmed
  empirically — not a `yfinance` bug, Yahoo just doesn't carry them; the underlying data is
  findable on ordinary news sites, e.g. The Edge Singapore). For those, `detect_new_reports_via_news`
  falls back to a **free** Google News RSS search (no LLM call) asking whether the company has
  reported since its last known date. This deliberately does **not** call the exchange's own
  announcements API directly — `api.sgx.com` sits behind Akamai bot protection that rejects
  datacenter traffic outright (confirmed by testing the live endpoint; SGX has no free
  RSS/developer feed, only a paid SWIFT-delivered commercial feed), and this repo doesn't build
  around another site's anti-bot measures. Both detection paths are free, so both run every time,
  including during `--dry-run`.
- For every newly-reported ticker, grounding (`_gather_grounding`, no LLM call) finds the source
  material, then two Groq calls (`config.LLM_MODEL`, same free-tier model `major_news_alert.py`
  uses) do the writing:
  1. **Grounding** — US-listed tickers (no `yfinance` suffix, including foreign issuers like
     TSM/ASML that file via EDGAR as 20-F/6-K filers) fetch their most recent 8-K/6-K (preferred —
     that's where the actual earnings press release/CFO-commentary exhibit lives, confirmed
     against a real NVIDIA filing: cover page ~26KB vs. the press release exhibit 341KB) directly
     from SEC EDGAR, picking the largest non-cover HTML documents in that filing as the real
     exhibits (SEC's own index doesn't semantically label them). Every other ticker — and any
     EDGAR miss — falls back to a Google News RSS search (quoted-phrase query + a company-name
     title filter, confirmed by testing to matter: an unquoted search for "AEM Holdings" surfaced
     Agnico Eagle Mines, ticker AEM, ahead of the real company) for headlines plus the top 2 linked
     articles' text. Not the primary filing for non-US names — the closest free source available,
     since no other exchange here offers a free developer feed.
  2. **Write** (Groq) — turns the grounding material, plus a `yfinance` quantitative snapshot
     (revenue/margin/net income QoQ and YoY, price move around the report date), into the full
     5-section report using `earnings_analysis_config.ANALYSIS_INSTRUCTIONS` — the analyst brief,
     pasted verbatim from the "quarterly earnings analysis" Claude Project — as the fixed brief.
     Edit that constant directly to change what every report covers.
  3. **Verify** (Groq) — independent fact-check pass: cross-checks every quote/figure in the draft
     against what grounding actually found, flags anything unverifiable rather than silently
     dropping it, and appends a short Telegram digest after a marker so no third call is needed
     just to summarize.
- `earnings_analysis.py` — thin wrapper: runs a cycle, renders each digest (grouped by
  watchlist with a flag emoji, with a pointer to the full report's path), and sends it to the same
  Telegram chat as everything else. A single bad ticker (bad data, a failed fetch/API call) is
  logged and skipped, never drops the rest of the run.

Only fires when something actually reported — no "quiet day" placeholder message. Runs via
`.github/workflows/earnings_analysis.yml`, once daily at 23:00 UTC (07:00 Asia/Singapore, catching
same-morning SGX/HK/Bursa/Thailand releases plus the prior US trading day's after-hours/pre-market
reports). Also has `workflow_dispatch` enabled, with a `dry_run` input that only prints what would
be analyzed — no API calls, no state changes, so it's free to test (and since detection itself is
free either way, a dry run now shows the *same* detected tickers a real run would see, including
ones found only via the Google News fallback).

`python earnings_analysis.py --test-ticker NVDA` (run locally, or adapt as a manual step) forces
the full grounding + Groq pipeline for one ticker and prints the result — never touches
`state.json` or Telegram. Since the very first real run only baselines all watchlist tickers
(nothing to analyze yet, by design — see above), this is the way to sanity-check report quality
before trusting the daily cron.

Needs `GROQ_API_KEY` as a GitHub Actions secret (see below) — already set up if
`major_news_alert.yml` is working, since it's the exact same free-tier key/account.

## Scheduled run

**`.github/workflows/weekly_screener.yml` is the source of truth.** It runs on GitHub's own
infrastructure every Monday at 00:00 UTC (08:00 Asia/Singapore, before SGX opens): checks out the
repo, installs dependencies, runs scripts 1-5, commits/pushes `output/` and `history/` if
anything changed, then sends the Telegram message using GitHub repo Secrets. It also has
`workflow_dispatch` enabled, so it can be triggered manually from the repo's Actions tab
(Actions -> Weekly Liquidity Momentum Screener -> Run workflow) instead of waiting for Monday.
Logs are visible directly in that Actions run, unlike the cloud routine below.

A local Windows Task Scheduler job ("SGX Liquidity Momentum Screener - Weekly") running
`scripts/run_weekly.ps1` was the original mechanism and is kept as reference/fallback, but is
disabled by default now that GitHub Actions handles it — re-enable it in Task Scheduler only if
GitHub Actions is unavailable.

A cloud routine (claude.ai) previously ran a Friday version of this but was found to silently
report "succeeded" without actually fetching data or committing (likely a network egress
restriction in that sandbox) — it's disabled and superseded by GitHub Actions.

**`.github/workflows/daily_buyback_alert.yml`** is the source of truth for the buy-back alert.
It runs on GitHub's infrastructure every weekday (Mon-Fri) at 00:00 UTC (08:00 Asia/Singapore,
before SGX opens — SGX buy-back notices are filed the previous evening SGT): checks out the repo,
runs scripts 6-8, commits/pushes `output/` and `history/` if anything changed (rebasing first so
it doesn't collide with the Monday weekly job), then sends the Telegram alert using the same repo
Secrets. `workflow_dispatch` is enabled, so it can also be run on demand from the Actions tab
(Actions -> Daily SGX Share Buy-Back Alert -> Run workflow).

**`.github/workflows/morning_brief.yml`** is the source of truth for the morning US market brief.
It runs every weekday (Mon-Fri) at 00:30 UTC (08:30 Asia/Singapore) — 30 minutes after the
buy-back job so the two don't race on the push to `main`: checks out the repo, runs
`morning_brief.py`, commits/pushes `output/morning_brief.md` if it changed (rebasing first),
then sends the Telegram brief using the same repo Secrets. `workflow_dispatch` is enabled
(Actions -> US Market Morning Brief -> Run workflow).

**`.github/workflows/evening_brief.yml`** is the source of truth for the evening Asia market
brief. Same shape, running every weekday at 10:15 UTC (18:15 Asia/Singapore, after the major
Asian closes): runs `evening_brief.py`, commits/pushes `output/evening_brief.md` if it changed
(rebasing first), then sends the Telegram brief. `workflow_dispatch` enabled
(Actions -> Asia Market Evening Brief -> Run workflow).

**`.github/workflows/semis_brief.yml`** is the source of truth for the global semiconductor
brief. Same shape, but with **two** cron slots and running **every day**: 00:30 UTC (08:30
Asia/Singapore, overnight US) and 10:30 UTC (18:30 Asia/Singapore, Asia session). Runs
`semis_brief.py`, commits/pushes `output/semis_brief.md` if it changed (rebasing first — it
overlaps the morning/evening briefs and the retry loop absorbs the push race), then sends the
Telegram brief. `workflow_dispatch` enabled (Actions -> Global Semiconductor Brief -> Run
workflow). Uses the same Telegram Secrets, no extra secret needed. (An earlier version ran as
two Claude desktop scheduled tasks; those are disabled now that this workflow is the source of
truth.)

**`.github/workflows/earnings_analysis.yml`** is the source of truth for the daily earnings
analysis alert. Runs every day at 23:00 UTC (07:00 Asia/Singapore): checks out the repo, runs
`earnings_analysis.py`, commits/pushes `data/earnings_analysis_state.json` and any new files
under `output/earnings_analysis/` if changed (rebasing first), then sends a Telegram digest for
each watchlist name that newly reported — zero, one, or several messages depending on the day.
`workflow_dispatch` enabled, with a `dry_run` input for a free test run (Actions -> Daily
Earnings Analysis -> Run workflow).

Scheduled workflows often skip their **first** slot after being added (and after a repo rename);
they start firing reliably within a day, or trigger once manually via **Run workflow**.

### GitHub Actions secrets (one-time, done via github.com — never share these in chat)

Go to the repo's **Settings -> Secrets and variables -> Actions -> New repository secret** and
add each of the following (same values already set locally as environment variables):

- `SGX_SCREENER_TELEGRAM_BOT_TOKEN`
- `SGX_SCREENER_TELEGRAM_CHAT_ID`
- `GROQ_API_KEY` — free tier, console.groq.com. Needed for the major-news alert's write-up step
  and the whole daily earnings analysis pipeline — both share this one key.

GitHub encrypts these at rest and never exposes them in logs, even on a public repo. Once both
Telegram secrets are added, trigger a manual run from the Actions tab to verify before waiting for
the schedule.

### Telegram setup (one-time, run yourself — never share the bot token in chat)

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts to name it.
   BotFather replies with a token that looks like `123456789:AAExampleTokenNotReal`.
2. In PowerShell, set the token for both this session and future ones:
   ```
   $env:SGX_SCREENER_TELEGRAM_BOT_TOKEN = "<token>"
   setx SGX_SCREENER_TELEGRAM_BOT_TOKEN "<token>"
   ```
3. In Telegram, send any message (e.g. "hi") to your new bot — bots can't message you until
   you've messaged them first.
4. Still in that same PowerShell window, run:
   ```
   cd scripts
   python get_telegram_chat_id.py
   ```
   It prints your `chat_id`. Then set it the same way:
   ```
   $env:SGX_SCREENER_TELEGRAM_CHAT_ID = "<chat_id>"
   setx SGX_SCREENER_TELEGRAM_CHAT_ID "<chat_id>"
   ```
5. Log out/in (or reboot) so the scheduled task picks up both persisted variables.

## Requirements

Python 3.10+, `pip install -r requirements.txt`
