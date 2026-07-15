# Vulture Bot Refactor Plan (v2)

**Product goal:** a trending-stock scanner focused on **options plays**. Reddit (and, best-effort, Stocktwits) surfaces candidate tickers; a news + technical + options-context scan scores them against an explicit rubric; only tickers that clear a threshold land in Discord, with the actionable plays being discussed. Everything else (earnings calendar, economic calendar, generic news dump) is stripped.

**Pillars:** Claude API for all AI analysis · the existing Discord webhook functions as the notification layer · Massive's free tier ([massive.com/docs](https://massive.com/docs), formerly Polygon.io) for market + options data.

---

## 1. Current State Review

Everything lives in `src/vulture.py` (480 lines), driven by `python src/vulture.py {reddit|news|calendar}`.

| Area | Today | Issues |
|---|---|---|
| AI analysis | OpenAI `gpt-4o` (`get_ai_synthesis`) | JSON enforced only by prompt; no schema validation; the daily-sentiment prompt is a literal stub |
| Scoring rubric | **Embedded in the GPT-4o system prompt** (`src/vulture.py:176-195`): a single 0–10 `confidence_score` judged holistically by the LLM (8–10 strong thesis + community validation, 4–7.9 mixed, 0.1–3.9 speculative/criticized, 0 no play) | No measurable inputs (no price, news, or options data); **no posting threshold** — everything scoring > 0 gets posted, the score only picks the forum tag color |
| Market data | Finnhub news + Alpha Vantage calendars | Being removed per product decision; the economic-calendar scan also has a tz-comparison bug that crashes it |
| Discord | Three ad-hoc embed functions | Duplicated posting logic, no retry, tag mapping inline |
| State | Flat files in `data/` | Ephemeral on Railway-style hosts → duplicate posts after redeploys |
| Config/packaging | Import-time side effects; UTF-16 `requirements.txt` with no pins | `pip install -r` fails on Linux; nothing unit-testable |

Preserved: the scan pipeline shape, praw scraping, pandas rank/dedupe, Google Sheets writer, Discord forum-thread mechanics.

**Removed entirely:** `run_news_scan` (as a standalone sheet dump — news becomes a scoring input), `run_calendar_scan`, `post_weekly_earnings_summary`, Finnhub, Alpha Vantage, and their env vars.

---

## 2. Target Pipeline

```
                 ┌─ SIGNALS ──────────────────────────────┐
                 │ Reddit: wsb, shortsqueeze, elite, small │
                 │ Stocktwits: trending symbols (best-     │
                 │   effort, unofficial endpoints)         │
                 └───────────────┬─────────────────────────┘
                                 ▼
                   candidate tickers + source posts
                                 ▼
                 ┌─ ENRICHMENT (Massive, cached) ─────────┐
                 │ ticker validation (Ticker Overview)     │
                 │ prev-day bar, RSI/SMA (EOD)             │
                 │ ticker news (last 48h)                  │
                 │ options chain snapshot (EOD)            │
                 └───────────────┬─────────────────────────┘
                                 ▼
                 ┌─ SCORING (Claude, structured output) ──┐
                 │ per-dimension sub-scores + extracted    │
                 │ plays; composite computed in code       │
                 └───────────────┬─────────────────────────┘
                                 ▼
                       composite ≥ POST_THRESHOLD ?
                          │ yes                │ no
                          ▼                    ▼
                 Discord forum post      Google Sheet only
                 (play details, market   (research log, rubric
                  context, options)       tuning data)
```

Everything scored — posted or not — is logged to the Sheet with its sub-scores. That log is how the rubric weights get tuned over time.

### Target layout

```
src/vulture/
├── __main__.py       # python -m vulture {scan|cramer}
├── config.py         # env + tunables (POST_THRESHOLD, rubric weights)
├── clients.py        # lazy singletons: anthropic, massive, praw, gspread
├── signals/
│   ├── reddit.py     # praw scraping (current logic, moved)
│   └── stocktwits.py # trending symbols + per-symbol sentiment (best-effort)
├── market.py         # Massive wrapper: throttle, cache, stocks + options
├── analysis.py       # Claude scoring + play extraction (Pydantic)
├── scoring.py        # composite score from sub-scores (deterministic, tunable)
├── notify.py         # Discord layer (generalized from existing functions)
├── sheets.py         # write_to_sheet (moved as-is)
├── state.py          # processed-post tracking (pluggable backend)
└── trackers/
    └── cramer.py     # nice-to-have, Phase 5
```

---

## 3. Signal Ingestion

### 3.1 Reddit (exists — moved, not rewritten)

Current `scrape_new_posts` + `get_comments_for_post` carry over. The daily-discussion-thread sentiment summary can stay as a low-cost daily briefing or be dropped — flagged as an open question.

### 3.2 Stocktwits (new, best-effort)

There is **no official open Stocktwits developer API** anymore (partner-gated), but the public JSON endpoints the website itself uses remain accessible unauthenticated and are widely used:

- `https://api.stocktwits.com/api/2/trending/symbols.json` — ~30 trending tickers with watchlist counts
- `https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.json` — recent messages, each optionally tagged Bullish/Bearish (native sentiment labels)

Design rules, since this is undocumented and can break or throttle at any time:

1. **Treat as a bonus signal, never a dependency.** `signals/stocktwits.py` returns `[]` on any failure; the pipeline runs Reddit-only without it.
2. **Two roles:** (a) trending list seeds candidates that Reddit hasn't surfaced yet; (b) for Reddit-sourced candidates, the symbol stream provides a cross-platform confirmation datapoint (message volume + bullish/bearish ratio) that feeds the rubric.
3. Gentle usage: 1 trending call + 1 stream call per candidate ticker per run, cached for the run, generous timeouts, honest `User-Agent`.
4. If it hard-breaks later, an Apify/RapidAPI wrapper is a drop-in swap behind the same interface.

---

## 4. Enrichment — Massive Free Tier

Both **Stocks Basic** and **Options Basic** free plans exist: 5 API calls/min each, end-of-day data, 2 years history. Options Basic covers all US options tickers with contracts reference, aggregates, and snapshots; **real-time greeks/IV/open interest/trades are paid-only** — at EOD granularity we get chain pricing and volume, which is enough for "is there unusual positioning" context, not for live flow. Client: `pip install massive`.

Per-candidate enrichment budget (all cached by `(endpoint, ticker, date)` since data is EOD):

| Call | Endpoint | Feeds |
|---|---|---|
| Validate ticker | Ticker Overview | Kills hallucinated tickers before scoring; company name for embeds |
| Price/volume | Previous Day Bar | Technical sub-score input + embed context line |
| Momentum | RSI (and/or SMA) | Technical sub-score input |
| Catalyst check | Ticker News (filtered to last 48h) | News sub-score input — headlines go into the Claude prompt |
| Options context | Option Chain Snapshot (EOD) | Options sub-score input: volume concentration by expiry/strike, put/call volume skew, where the discussed strikes sit vs. the chain |

≈ 5 calls per new ticker per day. A scan surfacing 8 unique candidates ≈ 40 calls ≈ 8–9 minutes of rate-limit budget — fine for a cron job, and the throttle sleeps overlap with Claude calls. `market.py` owns a single lock-guarded throttle (~13s between calls) + per-day cache, exactly as sketched in v1 §4.3.

---

## 5. Scoring — Claude + Explicit Rubric

The single-vibe `confidence_score` is replaced by **per-dimension sub-scores from Claude + a composite computed in code**. Claude judges what LLMs are good at judging; the weighting stays deterministic and tunable without touching prompts.

### 5.1 Structured output schema

```python
class OptionPlay(BaseModel):
    direction: Literal["bullish", "bearish", "neutral"]
    structure: str            # e.g. "calls", "puts", "call debit spread"
    strike: str | None        # as discussed, e.g. "$150"
    expiry: str | None        # as discussed, e.g. "2026-08-21" or "monthlies"
    rationale: str            # one-liner: why this play, per the thread

class TickerScore(BaseModel):
    ticker: str
    thesis_quality: float        # 0-10: is there an actual thesis with a catalyst/timeline?
    community_conviction: float  # 0-10: comment validation vs. being torn apart
    news_catalyst: float         # 0-10: do the last-48h headlines support a near-term move?
    technical_setup: float       # 0-10: momentum/level context from bar + RSI data provided
    options_context: float       # 0-10: does EOD chain volume/skew corroborate the direction?
    plays_discussed: list[OptionPlay]
    briefing: str                # synthesized thesis + community reaction
    red_flags: list[str]         # pump patterns, illiquid chain, stale thesis, etc.
```

One `client.messages.parse()` call per candidate on **`claude-opus-4-8`**, with the user prompt containing: the post + top comments, Stocktwits stream stats (if available), Massive news headlines, the prev-day bar + RSI, and a compact chain summary. Each sub-score's rubric bands (what a 2 vs. a 7 vs. a 9 looks like) live in the system prompt — this is the evolved version of the current prompt's scoring rules, now anchored to concrete data.

### 5.2 Composite (in `scoring.py`, not in the prompt)

```python
WEIGHTS = {           # config-tunable
    "thesis_quality": 0.25,
    "community_conviction": 0.20,
    "news_catalyst": 0.20,
    "technical_setup": 0.15,
    "options_context": 0.20,
}
CROSS_PLATFORM_BONUS = 0.5   # ticker also trending on Stocktwits
RED_FLAG_PENALTY = 0.75      # per flag, capped

POST_THRESHOLD = 7.0         # config; only composite >= threshold posts to Discord
```

Starting weights are a proposal — the Sheet log of every scored candidate (posted or not, with sub-scores) exists precisely so these can be tuned against what actually moved.

### 5.3 Cost/latency controls

- Posts with no plausible ticker candidate (cheap regex/`$TICKER` pre-filter + Massive validation) never reach Claude.
- Optional Phase-6: move scoring to the **Batches API** (50% cost) since the scan is cron-driven and not latency-sensitive.

---

## 6. Discord — Threshold-Gated Posting

`notify.py` generalizes the existing functions (single `send_embed` path, retry/backoff honoring `Retry-After`, tag/color mapping extracted) — unchanged from v1 §5. What changes is the content:

- **Only composite ≥ `POST_THRESHOLD` posts.** Below-threshold candidates go to the Sheet only. Discord becomes "worth your time" by construction.
- Thread name: `{TICKER} | {composite:.1f} | {emoji}` (existing convention, now a meaningful score).
- Embed fields:
  - **The Plays** — rendered from `plays_discussed`: `📈 Calls $150 · Aug 21 — "IV still cheap ahead of product event"` (one line per play)
  - **Score breakdown** — `Thesis 8 · Community 7 · News 8 · Technicals 6 · Options 7`
  - **Market context** — `Prev close $142.10 (+3.2%) · Vol 18M · RSI 68 · EOD data`
  - **Red flags** — shown when present
  - Source links (Reddit permalink, Stocktwits symbol page when trending)
- Existing tag tiers (high/medium/low) can map to composite bands above the threshold, e.g. ≥8.5 high, 7.0–8.5 medium.

---

## 7. Cramer Tracker (nice-to-have, Phase 5)

Goal: track which tickers Cramer is calling direction on, post a digest to the news webhook, and flag overlap with pipeline candidates ("🎪 Cramer mentioned this week" on scored plays — useful signal in either direction).

Source options, in recommended order:

1. **CNBC Mad Money recaps + Claude extraction** (free). CNBC publishes episode recaps and lightning-round articles at cnbc.com/mad-money. `trackers/cramer.py` fetches recent recap articles, and one Claude call per article extracts `[{ticker, stance: buy/sell/trim/avoid, quote}]` via structured output. Runs on the existing stack, no new provider. Caveat: page-structure scraping is inherently brittle and subject to CNBC's ToS — same "best-effort, degrade gracefully" rules as Stocktwits.
2. **Quiver Quantitative's Cramer tracker** ([quiverquant.com/cramertracker](https://www.quiverquant.com/cramertracker/)) — cleaner data via their paid API if this feature earns a budget line.

Output: a `python -m vulture cramer` entry point (separate cron), a "Cramer Watch" embed digest, mentions stored in the Sheet, and a lookup the main scan consults to stamp overlap on play embeds.

---

## 8. Cross-Cutting Fixes

Unchanged in substance from v1 — rolled into Phase 1:

1. **`requirements.txt`** rewritten UTF-8 + pinned: `anthropic`, `massive`, `praw`, `requests`, `python-dotenv`, `gspread`, `google-auth`, `pandas`, `pydantic`. Dropped: `openai`, `finnhub-python`, `alpha_vantage`, `openpyxl`, `google-auth-oauthlib`.
2. **Lazy config/clients** — no import-time side effects; functions unit-testable.
3. **State backend** — flat file behind an interface + Sheets-backed implementation ("Processed" tab) so redeploys stop causing duplicate posts.
4. **Timezone correctness** — all UTC-aware.
5. **`logging`** instead of `print`.

Env vars: **add** `ANTHROPIC_API_KEY`, `MASSIVE_API_KEY`, `POST_THRESHOLD`; **remove** `OPENAI_API_KEY`, `FINNHUB_API_KEY`, `ALPHA_VANTAGE_API_KEY`, calendar/news sheet vars.

---

## 9. Migration Phases

| Phase | Scope | Risk |
|---|---|---|
| **1. Skeleton + strip** | Package layout, UTF-8 pinned requirements, lazy config, logging, tz fixes. **Delete** news/calendar scans, Finnhub, Alpha Vantage. Reddit scan behavior otherwise unchanged. | Low |
| **2. Claude API** | `analysis.py` with the structured-output schema (§5.1) but scoring on post+comments only (no market data yet); composite + threshold gate in `scoring.py`; Sheet logs sub-scores. Drop `openai`. | Low |
| **3. Massive enrichment** | `market.py` (throttle + cache), ticker validation, bar/RSI/news/chain feeding the Claude prompt; enriched Discord embeds. | Medium — new provider + rate-limit discipline |
| **4. Stocktwits** | `signals/stocktwits.py`: trending seeds + cross-platform confirmation, wired into candidates and the composite bonus. Best-effort by design. | Medium — unofficial endpoints |
| **5. Cramer tracker** | `trackers/cramer.py` per §7, digest post + overlap stamp. | Low, optional |
| **6. Tuning & cost** | Rubric weight tuning from the Sheet log; Batches API for scoring. | Low, optional |

Each phase is independently shippable.

---

## 10. Open Questions

1. **Threshold + weights** (§5.2): 7.0 and the proposed weights are starting points — tune from the Sheet log, or do you have priors?
2. **Daily WSB sentiment briefing**: keep the once-a-day discussion-thread summary as a separate low-cost post, or drop it with the other extras?
3. **Stocktwits appetite**: comfortable relying on the unofficial public endpoints (with graceful degradation), or prefer a paid wrapper (Apify/RapidAPI) from day one?
4. **Cramer source**: CNBC-recaps-plus-Claude (free, brittle) vs. Quiver Quantitative paid API — does this feature justify a budget line if the free route proves flaky?
