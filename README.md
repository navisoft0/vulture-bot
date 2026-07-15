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
