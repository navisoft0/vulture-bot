# Vulture Bot Refactor Plan

**Goal:** restructure the bot around three pillars — the **Claude API** for all AI analysis, the **existing Discord webhook functions** as a reusable notification layer, and **Massive's free tier** ([massive.com/docs](https://massive.com/docs), formerly Polygon.io) as the single market-data provider — while fixing the structural and correctness issues in the current single-file script.

---

## 1. Current State Review

Everything lives in `src/vulture.py` (480 lines), driven by `python src/vulture.py {reddit|news|calendar}`.

| Area | Today | Issues |
|---|---|---|
| AI analysis | OpenAI `gpt-4o` via `chat.completions` (`get_ai_synthesis`, `analyze_discussion_comments`) | JSON enforced only by prompt + `json_object` mode; no schema validation; the daily-sentiment system prompt is literally the stub `"You are a market sentiment analyst..."` |
| Market data | Finnhub (`general_news`) + Alpha Vantage (earnings calendar, economic calendar) | Two providers, two keys, two client libraries; the `ECONOMIC_CALENDAR` Alpha Vantage function is not a documented AV endpoint and the date filter compares a tz-naive `releaseTime` against a tz-aware `datetime.now(timezone.utc)`, which raises `TypeError` in pandas — this scan is likely silently broken |
| Discord | Three ad-hoc functions building embeds inline (`post_plays_to_discord`, `post_daily_summary`, `post_weekly_earnings_summary`) | Duplicated embed/posting logic; no retry on transient failures; forum tag mapping hardcoded inside the plays function |
| State | `data/processed_posts.txt`, `data/daily_summary_log.txt` on local disk | Ephemeral on Railway-style hosts → duplicate posts after every redeploy |
| Config | `check_environment_variables()` + all API clients constructed at import time | Import has side effects; can't unit-test any function without every credential present |
| Packaging | `requirements.txt` is UTF-16 encoded with no version pins | `pip install -r` fails on Linux (pip expects UTF-8); `openpyxl`/`google-auth-oauthlib` listed but unused |
| Misc bugs | `datetime.fromtimestamp(dt)` in news scan uses server-local tz; `daily_summary_log.txt` path constructed twice; ticker string from the LLM is trusted verbatim | |

What works well and should be preserved: the overall scan pipeline shape, the confidence-score → forum-tag mapping, the drop-duplicates ranking in `run_reddit_scan`, and the Google Sheets writer with its layered exception handling.

---

## 2. Target Architecture

```
vulture-bot/
├── requirements.txt          # UTF-8, pinned: anthropic, massive, praw, gspread, ...
├── REFACTOR_PLAN.md
└── src/
    └── vulture/
        ├── __init__.py
        ├── __main__.py       # argparse entry: python -m vulture {reddit|news|calendar}
        ├── config.py         # env loading + validation (lazy, testable)
        ├── clients.py        # lazy singletons: anthropic, massive, praw, gspread
        ├── analysis.py       # Claude API calls (Pydantic-validated)
        ├── market.py         # Massive wrapper + rate limiter + ticker cache
        ├── notify.py         # Discord layer (generalized from existing functions)
        ├── sheets.py         # write_to_sheet (moved as-is)
        ├── state.py          # processed-post tracking (pluggable backend)
        └── scans/
            ├── reddit.py     # run_reddit_scan
            ├── news.py       # run_news_scan
            └── calendar.py   # run_calendar_scan
```

No framework, no async rewrite — the same three cron entry points, just modularized so each piece is testable and replaceable.

---

## 3. Workstream A — OpenAI → Claude API

Replace the `openai` dependency with the official `anthropic` SDK. Model: **`claude-opus-4-8`**.

### 3.1 Post synthesis (`get_ai_synthesis` → `analysis.synthesize_play`)

Use `client.messages.parse()` with a Pydantic schema so malformed JSON becomes impossible instead of a silently-dropped post:

```python
import anthropic
from pydantic import BaseModel, Field

class PlayAnalysis(BaseModel):
    ticker: str
    briefing: str
    the_play: str
    confidence_score: float = Field(ge=0.0, le=10.0)

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

def synthesize_play(post: dict, comments_text: str) -> PlayAnalysis | None:
    response = client.messages.parse(
        model="claude-opus-4-8",
        max_tokens=2048,
        system=SYNTHESIS_SYSTEM_PROMPT,          # current prompt carries over nearly verbatim
        messages=[{"role": "user", "content": build_user_prompt(post, comments_text)}],
        output_format=PlayAnalysis,
    )
    return response.parsed_output
```

Notes:
- The existing system prompt (analyst persona, scoring rubric) transfers as-is; drop the "respond only with JSON" boilerplate — structured outputs handle that.
- **Market-data grounding (new):** once Workstream B lands, append a compact context block to the user prompt — previous-day OHLCV and RSI from Massive — so confidence scores are grounded in real price action, not just Reddit sentiment.
- Error handling: catch typed exceptions (`anthropic.RateLimitError`, `anthropic.APIStatusError`) instead of bare `Exception`, and check `stop_reason` before trusting output.

### 3.2 Daily sentiment summary (`analyze_discussion_comments`)

Same migration, plus actually write the system prompt (it's a stub today). Free-text output, so a plain `messages.create()` call with the standard content-block loop is enough.

### 3.3 Optional (Phase 4): Batches API

The Reddit scan analyzes up to ~100 posts per run serially and is not latency-sensitive (cron job). Moving synthesis to the **Message Batches API** halves token cost (50% discount) and removes per-request rate-limit pressure. Keep it as a follow-up — the sync loop is fine to ship first.

### 3.4 Env var changes

- Remove: `OPENAI_API_KEY`
- Add: `ANTHROPIC_API_KEY`

---

## 4. Workstream B — Finnhub + Alpha Vantage → Massive

Consolidate both market-data providers onto Massive's free **Stocks Basic** tier. Client: `pip install massive` (`from massive import RESTClient`).

### 4.1 Free-tier constraints (design inputs)

| Constraint | Value | Consequence |
|---|---|---|
| Rate limit | **5 API calls / minute** | Every Massive call goes through one throttle (see 4.3) |
| Data freshness | **End-of-day** | Fine for a Reddit-sentiment bot; embeds must be labeled "prev close", never "live" |
| History | 2 years | Plenty for RSI/SMA context |
| Included | Reference data, corporate actions, aggregates, technical indicators, snapshots, news | Financial statements/ratios are paid — don't build against them |

### 4.2 Endpoint mapping

| Current call | Replacement (Massive) | Notes |
|---|---|---|
| Finnhub `general_news('general')` | **Ticker News** (`list_ticker_news`) | Richer payload: per-article tickers, publisher, sentiment insights. Can be filtered to tickers the bot actually flagged — more relevant than Finnhub's generic firehose |
| Alpha Vantage earnings calendar | ⚠️ **No free equivalent** — see 4.4 | Massive free tier has IPOs, dividends, splits, but no earnings calendar |
| Alpha Vantage `ECONOMIC_CALENDAR` | ⚠️ Broken today; no Massive equivalent | Replace with **Market Holidays** + corporate-action events, or keep AV solely for this if it's ever fixed |
| *(none — new)* | **Ticker Overview** | Validate LLM-extracted tickers before posting; fetch company name for embeds |
| *(none — new)* | **Previous Day Bar** | Price/volume enrichment for Discord embeds and Claude prompts |
| *(none — new)* | **RSI / SMA** technical indicators | One-line momentum context in the play embed |
| *(none — new)* | **Top Market Movers** | Optional daily-briefing section |

### 4.3 `market.py` — wrapper with throttle + cache

The 5-calls/minute budget is the central design constraint. One module owns it:

```python
# market.py (sketch)
import time, threading
from massive import RESTClient

class MassiveClient:
    """Rate-limited, cached wrapper around Massive's free tier."""
    MIN_INTERVAL = 13.0  # ~4.6 calls/min, safely under 5

    def __init__(self, api_key: str):
        self._client = RESTClient(api_key)
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._cache: dict[tuple, tuple[float, object]] = {}  # (key) -> (ts, value)

    def _throttled(self, fn, *args, cache_key=None, ttl=3600, **kwargs):
        if cache_key and cache_key in self._cache:
            ts, val = self._cache[cache_key]
            if time.time() - ts < ttl:
                return val
        with self._lock:
            wait = self.MIN_INTERVAL - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
        val = fn(*args, **kwargs)
        if cache_key:
            self._cache[cache_key] = (time.time(), val)
        return val

    def validate_ticker(self, symbol: str) -> dict | None: ...
    def prev_day_bar(self, symbol: str) -> dict | None: ...
    def rsi(self, symbol: str) -> float | None: ...
    def ticker_news(self, symbol: str | None = None, limit: int = 20) -> list: ...
```

Budgeting rule of thumb per Reddit scan: with EOD data, a ticker's bar/RSI/overview never changes intraday — cache by `(endpoint, ticker, date)` so repeat mentions of the same ticker across posts cost zero calls. A typical scan flagging 5–10 unique tickers needs ~15–30 Massive calls ≈ 4–7 minutes of budget, which is acceptable for a cron job (and the sleep happens while Claude calls run anyway).

### 4.4 The earnings-calendar gap — decision needed

Massive's free tier does not include an earnings calendar. Three options, in recommended order:

1. **Keep Alpha Vantage for earnings only** (it works today) and drop it for everything else. One legacy key, minimal code.
2. Replace the weekly earnings post with a **Massive-native "week ahead" post**: upcoming IPOs + ex-dividend dates + market holidays. Fully consolidates providers but changes the product.
3. Pay for Massive Starter ($29/mo) — out of scope for a free-tier plan.

The plan below assumes **option 1** unless you'd rather change the feature.

### 4.5 Env var changes

- Remove: `FINNHUB_API_KEY` (and `ALPHA_VANTAGE_API_KEY` if option 2 is chosen)
- Add: `MASSIVE_API_KEY`

---

## 5. Workstream C — Discord Layer (`notify.py`)

The existing webhook functions work; the refactor extracts the shared mechanics and keeps the three call sites thin.

```python
# notify.py (sketch)
def send_embed(webhook_url: str, embed: dict, *, thread_name: str | None = None,
               applied_tags: list[str] | None = None, retries: int = 3) -> bool:
    """One posting path for all Discord messages: timeout, raise_for_status,
    exponential backoff on 429/5xx (respecting Retry-After), returns success."""

def confidence_style(score: float) -> tuple[str | None, int, str]:
    """score -> (forum_tag_id, embed_color, emoji) — extracted from post_plays_to_discord."""

def post_play(play: PlayAnalysis, market: MarketContext | None) -> bool: ...
def post_daily_summary(text: str) -> bool: ...
def post_week_ahead(events: WeekAhead) -> bool: ...
```

Changes vs. today:
- **Retry with backoff on 429/5xx**, honoring Discord's `Retry-After` header (currently a single failed POST just prints and drops the play).
- **Embeds enriched with Massive data** — the play embed gains a market-context field, e.g. `Prev close $4.20 (+6.9%) · Vol 12.4M · RSI 71` — this is the visible payoff of Workstream B.
- Tag/color/emoji mapping moves to one function so the thresholds (≥8.0 / ≥4.0) live in exactly one place.
- Everything else (thread naming, footer, `?wait=True`) carries over unchanged.

---

## 6. Refactored Reddit Scan Flow

```
scrape_new_posts (praw)
  → for each post: fetch top comments
  → market.validate_ticker(candidate)          # NEW: kill hallucinated tickers early
  → market context: prev bar + RSI (cached)    # NEW
  → analysis.synthesize_play(post, comments, market_ctx)   # Claude, schema-validated
  → rank + dedupe (unchanged pandas logic)
  → notify.post_play(play, market_ctx)         # enriched embed, retries
  → sheets.write_to_sheet(...)                 # unchanged
  → state.mark_processed(ids)                  # pluggable backend
```

Ticker validation is worth calling out: today, a hallucinated ticker sails straight into a Discord forum post. With Massive, `Ticker Overview` returning 404 → the play is downgraded/dropped before it ships, and a valid response supplies the real company name for the embed title.

---

## 7. Cross-Cutting Fixes (rolled into the restructure)

1. **`requirements.txt`** — rewrite as UTF-8 with pins: `anthropic`, `massive`, `praw`, `requests`, `python-dotenv`, `gspread`, `google-auth`, `pandas`. Drop `openai`, `finnhub-python`, `openpyxl`, `google-auth-oauthlib` (and `alpha_vantage` if option 4.4-2).
2. **Lazy config/clients** — `config.py` validates env on first use (or via an explicit `validate()` call in `__main__.py`), clients built in `clients.py` accessors. Import stops having side effects; functions become unit-testable.
3. **State backend** (`state.py`) — keep the flat-file implementation but behind an interface, and add a Google-Sheets-backed implementation (a "Processed" tab) since a Sheets client already exists. Fixes duplicate posts after redeploys with zero new infrastructure.
4. **Timezone bugs** — all timestamps through `datetime.fromtimestamp(dt, tz=timezone.utc)`; the calendar tz-naive/aware comparison disappears with the AV economic-calendar code.
5. **Logging** — replace `print` with the `logging` module (same messages, plus levels), so a hosted deployment can filter noise.

---

## 8. Migration Phases

| Phase | Scope | Risk |
|---|---|---|
| **1. Skeleton + fixes** | Package layout (§2), UTF-8 pinned requirements, lazy config, logging, timezone fixes. Behavior unchanged. | Low — pure restructure, verifiable by running all three scans |
| **2. Claude API** | `analysis.py` per §3; delete `openai`. Write the missing sentiment prompt. | Low — isolated to two functions; schema validation makes output *stricter* |
| **3. Massive** | `market.py` per §4; news scan switched to Massive; ticker validation + embed enrichment wired into the Reddit scan; Discord layer generalized per §5. | Medium — new provider, rate-limit discipline; mitigated by cache + throttle |
| **4. Nice-to-haves** | Batches API for synthesis (50% cost), Sheets-backed state, top-movers section in daily briefing, week-ahead post (if 4.4-2). | Low, optional |

Each phase is independently shippable; the bot keeps running between phases.

---

## 9. Open Questions

1. **Earnings calendar** (§4.4): keep Alpha Vantage for earnings only, or replace with a Massive-native IPO/dividend/holiday "week ahead" post?
2. **State**: is the host's filesystem persistent (volume attached), or should the Sheets-backed processed-post store land in Phase 3 instead of Phase 4?
3. **News scan destination**: currently Sheets-only. Since Massive news is per-ticker, should top headlines for flagged tickers also go to the Discord news channel?
