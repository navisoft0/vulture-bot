"""Massive (formerly Polygon.io) market-data wrapper for the free tier.

The free Basic plans allow 5 API calls/minute with end-of-day price data
(the freshest bar during market hours is the previous session's close;
reference data and the hourly news feed are not delayed). This module owns
the rate-limit discipline: a single lock-guarded throttle plus a per-day
cache, so repeat mentions of the same ticker cost zero calls.

Every public method returns None/[] on failure — a Massive outage degrades
scoring, it never kills the scan.
"""

import itertools
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

from massive import RESTClient

log = logging.getLogger(__name__)

#: ~4.6 calls/minute, safely under the 5/min free-tier limit.
_MIN_INTERVAL = 13.0


class MarketData:
    def __init__(self, api_key: str):
        self._client = RESTClient(api_key)
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._cache: dict[tuple, object] = {}

    # ------------------------------------------------------------------
    # Throttle + cache plumbing
    # ------------------------------------------------------------------

    def _call(self, cache_key: tuple | None, fn, *args, **kwargs):
        if cache_key is not None:
            key = cache_key + (date.today().isoformat(),)
            if key in self._cache:
                return self._cache[key]
        with self._lock:
            wait = _MIN_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            log.warning("Massive call %s failed: %s", getattr(fn, "__name__", fn), e)
            result = None
        if cache_key is not None:
            self._cache[key] = result
        return result

    # ------------------------------------------------------------------
    # Stocks
    # ------------------------------------------------------------------

    def validate_ticker(self, symbol: str) -> dict | None:
        """Ticker details if `symbol` is a real listed ticker, else None."""
        details = self._call(("details", symbol), self._client.get_ticker_details, symbol)
        if details is None:
            return None
        return {
            "ticker": getattr(details, "ticker", symbol),
            "name": getattr(details, "name", None),
            "market_cap": getattr(details, "market_cap", None),
        }

    def prev_day_bar(self, symbol: str) -> dict | None:
        """Previous session's OHLCV (the freshest bar on the free tier intraday)."""
        aggs = self._call(("prev_bar", symbol), self._client.get_previous_close_agg, symbol)
        if not aggs:
            return None
        bar = aggs[0] if isinstance(aggs, (list, tuple)) else aggs
        o, c = getattr(bar, "open", None), getattr(bar, "close", None)
        return {
            "open": o,
            "high": getattr(bar, "high", None),
            "low": getattr(bar, "low", None),
            "close": c,
            "volume": getattr(bar, "volume", None),
            "day_move_pct": round((c - o) / o * 100, 2) if o and c else None,
        }

    def rsi(self, symbol: str, window: int = 14) -> float | None:
        """Latest daily RSI value."""
        result = self._call(
            ("rsi", symbol, window),
            self._client.get_rsi,
            ticker=symbol, timespan="day", window=window, series_type="close", limit=1,
        )
        try:
            values = getattr(result, "values", None) or []
            return round(values[0].value, 1) if values else None
        except Exception:
            return None

    def recent_news(self, symbol: str, hours: int = 48, limit: int = 5) -> list[dict]:
        """Recent headlines for `symbol` (hourly-updated feed, free tier)."""
        def fetch():
            it = self._client.list_ticker_news(ticker=symbol, order="desc", limit=10)
            return list(itertools.islice(it, 10))  # first page only: one API call

        articles = self._call(("news", symbol), fetch) or []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        out = []
        for a in articles:
            published = getattr(a, "published_utc", None)
            try:
                published_dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                published_dt = None
            if published_dt and published_dt < cutoff:
                continue
            insights = getattr(a, "insights", None) or []
            sentiment = next(
                (i.sentiment for i in insights if getattr(i, "ticker", None) == symbol),
                None,
            )
            out.append({
                "title": getattr(a, "title", ""),
                "publisher": getattr(getattr(a, "publisher", None), "name", ""),
                "published_utc": str(published) if published else "",
                "sentiment": sentiment,
            })
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------
    # Options (reference only — no pricing/greeks on the free tier)
    # ------------------------------------------------------------------

    def contract_exists(self, underlying: str, contract_type: str,
                        strike: float, expiry: str) -> bool | None:
        """True/False if a listed contract matches; None if the check failed.

        contract_type: "call" or "put"; expiry: YYYY-MM-DD.
        """
        def fetch():
            it = self._client.list_options_contracts(
                underlying_ticker=underlying,
                contract_type=contract_type,
                strike_price=strike,
                expiration_date=expiry,
                limit=1,
            )
            return list(itertools.islice(it, 1))

        result = self._call(("contract", underlying, contract_type, strike, expiry), fetch)
        if result is None:
            return None
        return len(result) > 0

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def market_line(bar: dict | None, rsi_value: float | None) -> str | None:
        """One-line embed/prompt summary, e.g. 'Prev close $4.20 (+6.9%) · Vol 12.4M · RSI 71'."""
        if not bar:
            return None
        bits = []
        if bar.get("close") is not None:
            move = f" ({bar['day_move_pct']:+.1f}%)" if bar.get("day_move_pct") is not None else ""
            bits.append(f"Prev close ${bar['close']:,.2f}{move}")
        if bar.get("volume"):
            v = bar["volume"]
            bits.append(f"Vol {v / 1e6:.1f}M" if v >= 1e6 else f"Vol {v / 1e3:.0f}K")
        if rsi_value is not None:
            bits.append(f"RSI {rsi_value:g}")
        return " · ".join(bits) if bits else None
