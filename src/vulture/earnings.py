"""Earnings signal — best-effort via Yahoo Finance (yfinance).

Massive's Stocks Starter plan has no earnings calendar and no options
prices, so the next earnings date and the options-implied expected move
come from Yahoo's unofficial endpoints instead. Same contract as
signals/stocktwits.py: every failure degrades to None and the pipeline
carries on without the data.
"""

import logging
from datetime import date, datetime

log = logging.getLogger(__name__)

#: Per-run cache (module lives as long as the process; daemon scans reuse it
#: within a day at most — earnings dates don't move that fast).
_CACHE: dict[tuple[str, str], dict | None] = {}


def snapshot(symbol: str) -> dict | None:
    """{"earnings_date": "YYYY-MM-DD", "earnings_em_pct": 7.4} for `symbol`.

    Either key may be absent (EM needs a listed options chain); None when
    Yahoo returns nothing usable.
    """
    key = (symbol, date.today().isoformat())
    if key not in _CACHE:
        _CACHE[key] = _fetch(symbol)
    return _CACHE[key]


def _fetch(symbol: str) -> dict | None:
    try:
        import yfinance as yf

        t = yf.Ticker(symbol)
        out: dict = {}
        earnings = _next_earnings_date(t)
        if earnings:
            out["earnings_date"] = earnings.isoformat()
            em = _expected_move(t, earnings)
            if em is not None:
                out["earnings_em_pct"] = em
        return out or None
    except Exception as e:
        log.warning("Earnings data for %s unavailable (continuing without): %s", symbol, e)
        return None


def _next_earnings_date(t) -> date | None:
    cal = t.calendar
    if isinstance(cal, dict):
        dates = cal.get("Earnings Date") or []
    else:  # legacy DataFrame shape
        try:
            dates = list(cal.loc["Earnings Date"])
        except Exception:
            dates = []
    today = date.today()
    future = sorted(d.date() if isinstance(d, datetime) else d
                    for d in dates if d is not None)
    future = [d for d in future if d >= today]
    return future[0] if future else None


def _expected_move(t, earnings: date) -> float | None:
    """ATM straddle price over spot for the first expiry on/after earnings."""
    expiries = [date.fromisoformat(e) for e in (t.options or ())]
    expiries = [e for e in expiries if e >= earnings]
    if not expiries:
        return None
    spot = getattr(t.fast_info, "last_price", None)
    if not spot:
        return None
    chain = t.option_chain(min(expiries).isoformat())

    def atm_price(df):
        if df is None or df.empty:
            return None
        row = df.iloc[(df["strike"] - spot).abs().argsort().iloc[0]]
        bid, ask = row.get("bid"), row.get("ask")
        if bid and ask and bid > 0 and ask > 0:
            return (bid + ask) / 2
        last = row.get("lastPrice")
        return last if last and last > 0 else None

    call, put = atm_price(chain.calls), atm_price(chain.puts)
    if call is None or put is None:
        return None
    return round((call + put) / spot * 100, 1)
