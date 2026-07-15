# Vulture Bot

Options-focused trending-stock scanner. Reddit (and best-effort Stocktwits)
surface candidate tickers; [Massive](https://massive.com) enriches them with
market context on the free tier; Claude scores them against an explicit rubric;
candidates that clear `POST_THRESHOLD` are posted to a Discord forum with the
option plays being discussed. Every scored candidate is logged to Google Sheets
for rubric tuning. See `REFACTOR_PLAN.md` for the full design.

## Setup

```bash
pip install -e .                        # or: pip install -r requirements.txt
cp vulture_cred.env.example vulture_cred.env   # fill in credentials
```

## Run

```bash
vulture scan     # main pipeline (cron this; e.g. every 30-60 min)
vulture cramer   # Cramer Watch digest from CNBC Mad Money recaps (e.g. daily)

# without installing: PYTHONPATH=src python -m vulture scan
```

## Cost controls (Anthropic API)

- **Batch scoring is on by default** (`BATCH_SCORING=true`): all scoring calls
  go through the Message Batches API at 50% of standard token prices, with a
  synchronous fallback if submission fails. A scan waits (up to
  `BATCH_TIMEOUT_S`) for results — fine for cron.
- **Model tier** is one env var: `VULTURE_MODEL=claude-sonnet-5` is ~60%
  cheaper than the default Opus, `claude-haiku-4-5` ~80% cheaper. If you use
  sonnet-5, also set `VULTURE_EFFORT=low` (its thinking is on by default).
- Cramer extraction runs on Haiku by default (`VULTURE_CRAMER_MODEL`).
- Structural guards: posts with no ticker candidates never reach Claude,
  `MAX_POSTS_PER_SCAN` caps each run, processed-post state prevents rescoring,
  and post/comment text is truncated before prompting.
- Prompt caching is deliberately not used: the shared prefix (the system
  prompt) is far below the model's minimum cacheable size, and everything
  after it is unique per post.

## Notes

- **Data freshness:** Massive's free tier is end-of-day — during market hours
  the freshest price/RSI is the previous session's close (embeds say so). The
  news feed updates hourly and reference data is not delayed.
- **Rate limits:** all Massive calls go through a single throttle (~4.6/min)
  with a per-day cache; a scan surfacing ~8 new tickers uses ~8 minutes of
  rate-limit budget.
- **Stocktwits** uses unofficial public endpoints and degrades gracefully —
  if it breaks, the pipeline runs Reddit-only.
- **State:** set `STATE_BACKEND=sheet` on hosts with ephemeral filesystems so
  redeploys don't re-post old plays.
