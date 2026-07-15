"""Stocktwits signal — best-effort, never a dependency.

Uses the public unauthenticated JSON endpoints the website itself uses.
There is no official open developer API, so every function here degrades
to an empty/None result on any failure and the pipeline runs Reddit-only.
"""

import logging

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
