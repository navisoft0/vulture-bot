# Vulture Dashboard Plan

**Goal:** evolve the bot from a Discord-only pipeline into a small-community
(≤5 users) tracking dashboard: a login-protected live site showing each
morning's vetted stocks/posts, a play tracker that grades promoted plays
hit/miss, a Cramer section, an admin panel with live run triggering, and
90-day data retention with archived exports.

**Verdict on feasibility:** strong fit. The existing engine already produces
every data artifact the dashboard needs (scored candidates, extracted plays
with strike/expiry, Cramer mentions + verdicts). The user's Cloudflare account
covers hosting, auth, database, storage, and cron — all within free tiers at
this scale.

---

## 1. Architecture: split engine from surface

| Layer | Where | Why |
|---|---|---|
| **Engine** (scan, Claude scoring, Massive pricing, Cramer, grading) | Railway — unchanged | Python, heavy deps, long-running batch polls; wrong shape for Workers |
| **API + DB** | Cloudflare Worker + **D1** (SQLite) | Free tier: 100k req/day, 5GB — years of headroom at 5 users |
| **Frontend** | Cloudflare Pages (static SPA) | Same repo deploy; subdomain `vulture.<domain>` beside the portfolio site |
| **Auth** | **Cloudflare Access** (Zero Trust) | Free ≤50 users; email allowlist; OTP/Google login; identity arrives as a request header — no auth code to write |
| **Archive** | **R2** bucket | Monthly CSV exports; link posted to Discord |
| **Notifications** | Existing Discord webhooks | Unchanged; Discord becomes one consumer among two |

Data flow: the engine keeps its pipeline but swaps its sink — every scored
candidate, play, and Cramer record POSTs to the Worker's ingest endpoint
(shared-secret header) and lands in D1. Google Sheets writing stays during
transition (dual-write), then becomes export-only.

## 2. D1 schema (initial)

```sql
scans        (id, started_at, finished_at, posts_seen, scored, posted, trigger) -- trigger: cron|manual
candidates   (id, scan_id, post_id, ticker, composite, thesis, community, news,
              technical, cross_platform, prior_mentions, momentum_bonus, radar,
              posted, briefing, red_flags, url, subreddit, post_created_utc,
              scored_at_utc)
plays        (id, candidate_id, ticker, direction, structure, strike, expiry,
              rationale, promoted)          -- promoted = candidate hit Discord/dashboard
play_results (play_id, graded_at, method,   -- method: contract_eod | underlying_itm
              entry_underlying, exit_underlying, entry_contract, exit_contract,
              return_pct, verdict)          -- HIT | MISS | WASH | UNGRADEABLE
cramer_mentions (id, extracted_at, ticker, stance, quote, source_url)
cramer_verdicts (mention_id, baseline_date, baseline_close, eval_date, eval_close,
              stock_return_pct, spy_return_pct, alpha_pct, verdict)
runs_requested (id, requested_by, requested_at, picked_up_at)  -- admin "run now" queue (fallback path)
```

## 3. Auth & roles

- Cloudflare Access application on `vulture.<domain>`: allowlist of 5 emails.
- Worker trusts `Cf-Access-Authenticated-User-Email` (verified via Access JWT).
- `ADMIN_EMAILS` (Worker env var) → admin role; everyone else member.
- Members: read everything. Admin: run-now, promote/demote a candidate,
  trigger export, edit thresholds (stretch: engine reads tunables from D1).

## 4. Pages (frontend)

1. **Today** — latest scan's candidates as cards: composite + sub-score bar,
   plays, red flags, momentum/radar badges, links to Reddit/Stocktwits.
   "Vetted" = posted or radar; toggle to see everything scored.
2. **Play Tracker** — open positions (days to expiry, underlying move since
   promotion) and resolved plays (verdict, returns); community hit-rate
   stats by ticker/subreddit/structure.
3. **Cramer** — latest digest, scorecard table, all-time hit/inverse rate.
4. **Data** — filterable raw table (the spreadsheet, live); CSV download.
5. **Admin** (admin only) — Run Now button + last-run status; export trigger;
   retention preview ("what gets archived next cycle").

Stack: static HTML/JS (or a small Vue/React build) on Pages; all data via the
Worker API; no client-side secrets anywhere.

## 5. Admin "Run Now"

Primary: the daemon adds a tiny HTTP listener (Railway exposes a public URL)
with a bearer token; the Worker's `/api/run` (admin-only) calls it → scan
starts within seconds and reports `scan_id` back. Fallback (if we'd rather not
expose the daemon): Worker inserts into `runs_requested`; the daemon polls the
table each loop (adds ≤60s latency).

## 6. Play tracker grading (generalizes the Cramer scorecard)

At promotion time, each extracted play with strike+expiry is registered in
`plays`. A daily engine job grades plays whose expiry has passed:

- **Primary — contract EOD prices** (Massive Options Basic includes EOD
  aggregates for specific contracts): entry = contract close on promotion day,
  exit = close on/near expiry; `return_pct` on premium; HIT if the premium
  gained (threshold configurable), MISS if lost.
- **Fallback — underlying vs strike**: if contract bars are unavailable,
  grade ITM/OTM at expiry in the play's direction, plus underlying return
  since promotion.
- Plays without strike/expiry ("buying calls sometime") grade on underlying
  direction over 14 days (same as the Cramer method), marked accordingly.
- Rate-limit budget: graded daily under the existing throttle, capped per run;
  cached per contract per day.

## 7. Retention & archive (the "keep it clean" rule)

Monthly Worker cron:
1. Select rows older than **90 days** — excluding plays still open (expiry in
   the future) and their parent candidates.
2. Write CSVs (`candidates-YYYY-MM.csv`, `plays-…`, `cramer-…`) to R2.
3. Post the download links to the Discord news webhook (email via a free
   Resend account is an optional add-on).
4. Delete the archived rows. Aggregates worth keeping forever (hit-rate
   totals, Cramer record) live in tiny summary tables that never purge.

## 8. Phases

| Phase | Deliverable | Notes |
|---|---|---|
| **D1** | Schema + ingest Worker + engine dual-writes (Sheets + D1) | No user-visible change; validates the pipe |
| **D2** | Pages frontend (Today + Data) behind Access | First login-able dashboard |
| **D3** | Admin panel + Run Now | Daemon HTTP trigger |
| **D4** | Play tracker (engine grading + Tracker page) | Cramer-scorecard pattern generalized |
| **D5** | Cramer page | Mostly UI; data already flows |
| **D6** | Retention cron + R2 archive + Discord link | 90-day rule |
| **D7** | Cutover: Sheets becomes export-only (or retired) | Momentum/history reads move to D1 via API |

Each phase is independently shippable; Discord alerts continue throughout.

## 9. Open questions

1. **Subdomain** — `vulture.<portfolio-domain>`? Needs the domain to be on the
   Cloudflare account (it is, if the portfolio is hosted there).
2. **Hit/miss definition for options plays** — premium-based (recommended
   primary) vs underlying-ITM only? Threshold for HIT (any gain vs e.g. +20%)?
3. **Sheets fate after cutover** — retire completely, or keep receiving the
   monthly archive CSVs as a browsable backup?
4. **Member permissions** — should members be able to flag/comment on a play
   ("I'm in"), or is v1 read-only for everyone but admin?
