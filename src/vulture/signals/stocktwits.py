"""Stocktwits signal — best-effort, never a dependency.

Preferred source: a snapshot written to the dashboard by a scheduled Claude
routine using the Stocktwits MCP connector (parse_snapshot). Fallback: the
public unauthenticated JSON endpoints the website itself uses. There is no
official open developer API, so every function here degrades to an
empty/None result on any failure and the pipeline runs Reddit-only.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

_BASE = "https://api.stocktwits.com/api/2"
# Stocktwits fronts with Cloudflare, which rejects non-browser user agents.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://stocktwits.com/",
}
_TIMEOUT = 10


def parse_snapshot(snap: dict | None, max_age_min: int) -> dict[str, dict]:
    """Index a dashboard-relayed snapshot by ticker; {} when absent or stale.

    The snapshot is written by a scheduled Claude routine with the Stocktwits
    MCP connector — richer than the public endpoints here: canonical sentiment
    score (0-100), normalized message volume, trending rank, watcher count,
    and a one-line "driver" synthesized from the post stream. Entries carry
    those keys; see STOCKTWITS_ROUTINE.md for the schema.
    """
    if not snap:
        return {}
    try:
        fetched = datetime.fromisoformat(str(snap["fetched_at"]).replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return {}
    age_min = (datetime.now(timezone.utc) - fetched).total_seconds() / 60
    if age_min > max_age_min:
        log.info("Stocktwits snapshot is %.0f min old (max %d); falling back to scraping.",
                 age_min, max_age_min)
        return {}
    out = {}
    for entry in snap.get("symbols", []):
        ticker = (entry.get("symbol") or "").strip().upper()
        if ticker:
            out[ticker] = entry
    log.info("Stocktwits snapshot: %d symbols, %.0f min old.", len(out), age_min)
    return out


def trending_symbols() -> list[str]:
    """Tickers currently trending on Stocktwits ([] on any failure)."""
    try:
        resp = requests.get(f"{_BASE}/trending/symbols.json", headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        symbols = resp.json().get("symbols", [])
        out = [s.get("symbol", "").upper() for s in symbols if s.get("symbol")]
        log.info("Stocktwits trending: %d symbols.", len(out))
        return out
    except Exception as e:
        log.warning("Stocktwits trending unavailable (continuing without): %s", e)
        return []


def symbol_stats(ticker: str) -> dict | None:
    """Recent message stats for a symbol: volume + bullish/bearish split.

    Returns {"messages": n, "bullish": x, "bearish": y} or None on failure.
    """
    try:
        resp = requests.get(
            f"{_BASE}/streams/symbol/{ticker}.json", headers=_HEADERS, timeout=_TIMEOUT
        )
        resp.raise_for_status()
        messages = resp.json().get("messages", [])
        bullish = bearish = 0
        for m in messages:
            sentiment = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            if sentiment == "Bullish":
                bullish += 1
            elif sentiment == "Bearish":
                bearish += 1
        return {"messages": len(messages), "bullish": bullish, "bearish": bearish}
    except Exception as e:
        log.warning("Stocktwits stream for %s unavailable: %s", ticker, e)
        return None
