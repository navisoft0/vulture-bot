# Stocktwits routine → dashboard → engine

The rich Stocktwits signal (canonical sentiment score, normalized message
volume, trending rank, driver text) comes from the Stocktwits MCP connector,
which only Claude sessions can call — not the Python engine. A scheduled
Claude routine bridges the gap:

```
Claude routine (cron, ~35 min)          Worker (vulture.navisoft.dev)      Engine (Railway)
  Stocktwits MCP tools ──POST──▶  /api/ingest/stocktwits (D1, latest-only)
  st_targets ◀──GET──                                            ──GET──▶ scan start
```

The engine treats the snapshot as best-effort: older than
`STOCKTWITS_SNAPSHOT_MAX_AGE_MIN` (default 90) or unavailable → it falls back
to the old public-endpoint scraping, exactly as before.

## Snapshot schema (what the routine POSTs)

```json
{
  "fetched_at": "2026-08-09T15:30:00Z",
  "symbols": [
    {
      "symbol": "TSEM",
      "title": "Tower Semiconductor",   // company name (shown on feed cards)
      "exchange": "NASDAQ",
      "trending_rank": 3,          // null if not on the trending list
      "price": 252.49,
      "change_pct": 12.4,
      "watchers": 32133,
      "sentiment_score": 68,       // 0-100 canonical score
      "sentiment_label": "BULLISH",
      "bull_pct": 71.0,            // % of tagged messages bullish
      "volume_score": 80,          // 0-100 normalized message volume (now)
      "volume_label": "EXTREMELY_HIGH",
      "driver": "BofA initiated Buy, $367 PT, citing AI optical connectivity demand; breaking out of a long-term flag pattern."
    }
  ]
}
```

Only `symbol` is required; the engine renders whatever fields are present.
`driver` feeds the scoring prompt verbatim — keep it factual, one sentence,
no advice.

## Routine prompt

Replace `<INGEST_TOKEN>` with the Worker's `INGEST_TOKEN` secret (same one the
engine uses). It only authorizes ingest read/write — worst case on leak is
someone writing fake snapshots; rotate via `wrangler secret put INGEST_TOKEN`
and the Railway env if that ever happens.

```
You are the Stocktwits data feed for the vulture-bot engine. Each run,
produce one JSON snapshot and POST it to the vulture dashboard. Work
data-first; no prose report is needed beyond a one-line confirmation.

1. Call the Stocktwits tool get_trending_symbols (limit 10, equities).
2. GET https://vulture.navisoft.dev/api/ingest/st_targets with header
   "Authorization: Bearer <INGEST_TOKEN>". This returns tickers the engine
   scored in the last 24h. Merge them with the trending list, dedupe,
   cap at 20 symbols total (trending first).
3. For each symbol, call get_symbol_pulse. From its result take: company
   name (title), exchange, price, change %, watchers, sentiment
   score/label, bull %, message-volume score/label (the "now" bucket),
   and trending rank if any.
4. For each symbol write a one-sentence "driver": WHY it is moving today,
   synthesized from the pulse's top posts. Factual and specific (catalyst,
   news, pattern), no investment advice, under 200 characters. If the
   stream shows no clear driver, use null.
5. POST the snapshot as JSON to
   https://vulture.navisoft.dev/api/ingest/stocktwits with headers
   "Authorization: Bearer <INGEST_TOKEN>" and "Content-Type:
   application/json", in exactly this shape:
   {"fetched_at": "<current UTC time, ISO-8601>",
    "symbols": [{"symbol": "...", "title": "...", "exchange": "...",
      "trending_rank": 3 or null,
      "price": 0.0, "change_pct": 0.0, "watchers": 0,
      "sentiment_score": 0, "sentiment_label": "...", "bull_pct": 0.0,
      "volume_score": 0, "volume_label": "...", "driver": "..." or null}]}
6. Confirm the POST returned {"ok": true} and report only:
   "Snapshot posted: N symbols at <time>". If any Stocktwits tool is
   unavailable or the POST fails, report the error instead — do not
   invent data.
```

Schedule: every 30–40 minutes during US market hours (pre-market through
after-hours is the useful window; overnight runs mostly waste usage). The
engine scans every 45, so this keeps the snapshot always fresher than a scan.

## Deploy checklist (one-time)

1. Create the D1 table:
   `npx wrangler@4.119.0 d1 execute vulture --remote --command "CREATE TABLE IF NOT EXISTS stocktwits_snapshot (id INTEGER PRIMARY KEY CHECK (id = 1), fetched_at TEXT NOT NULL, payload TEXT NOT NULL);"`
2. `npx wrangler@4.119.0 deploy` in `dashboard/` (new routes:
   POST+GET `/api/ingest/stocktwits`, GET `/api/ingest/st_targets` — all under
   the existing Access bypass prefix; no Access changes needed).
3. Update the Claude routine with the prompt above (token filled in).
4. Deploy the engine (push to main → Railway). No new engine env vars are
   required; `STOCKTWITS_SNAPSHOT_MAX_AGE_MIN` is optional tuning.

Verify end-to-end: run the routine once, then
`curl -H "Authorization: Bearer <INGEST_TOKEN>" https://vulture.navisoft.dev/api/ingest/stocktwits`
should return the snapshot, and the next engine scan should log
"Stocktwits snapshot: N symbols, M min old."
