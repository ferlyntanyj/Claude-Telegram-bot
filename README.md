# SGX Liquidity Momentum Screener

Ranks SGX mainboard + Catalist stocks by ADTV surge ratio:

```
surge_ratio = (trailing 5-trading-day average daily traded value) / (full-year 2024 average daily traded value)
```

A liquidity floor (2024 ADTV >= SGD 100,000/day) is applied before ranking to exclude
near-untraded microcaps where a single small trade inflates the ratio off pure noise.
See `scripts/03_compute_screener.py` for the full rationale and `MIN_ADTV_2024_SGD` to adjust it.

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

## News briefs (US morning + Asia evening)

Two standalone weekday pipelines, unrelated to SGX, that deliver a concise "what happened"
headline digest to the same Telegram chat. Deterministic, no LLM. Both run on one shared engine
(`brief_engine.py`) and one shared Telegram sender (`brief_telegram.py`); each brief is just a
config module plus a two-line wrapper, so ranking / de-duplication / windowing / rendering stay
identical between them.

| Brief | Delivered | Scope | Sections | Config |
|---|---|---|---|---|
| **US morning** | 08:30 SGT (00:30 UTC) | US market, overnight | Markets / Macro, Fed & data / Stocks & earnings / Geopolitics | `morning_brief_config.py` |
| **Asia evening** | 18:15 SGT (10:15 UTC) | ASEAN, Japan, Korea, Greater China | Macro & policy / Markets / Geopolitics | `evening_brief_config.py` |

Wrappers: `morning_brief.py` / `send_morning_brief_telegram.py`, `evening_brief.py` /
`send_evening_brief_telegram.py`. Run the builder then the sender from `scripts/`.

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

Scheduled workflows often skip their **first** slot after being added (and after a repo rename);
they start firing reliably within a day, or trigger once manually via **Run workflow**.

### GitHub Actions secrets (one-time, done via github.com — never share these in chat)

Go to the repo's **Settings -> Secrets and variables -> Actions -> New repository secret** and
add each of the following (same values already set locally as environment variables):

- `SGX_SCREENER_TELEGRAM_BOT_TOKEN`
- `SGX_SCREENER_TELEGRAM_CHAT_ID`

GitHub encrypts these at rest and never exposes them in logs, even on a public repo. Once both
are added, trigger a manual run from the Actions tab to verify before waiting for Monday.

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
