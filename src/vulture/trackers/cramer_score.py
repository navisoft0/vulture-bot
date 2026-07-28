"""Cramer scorecard: did the call hit, or was it an inverse-Cramer moment?

Every directional mention logged by the Cramer tracker (buy / sell / avoid /
trim — hold and unclear aren't judged) becomes a position at that day's close.
After CRAMER_EVAL_DAYS (14) it's resolved against SPY over the same window:

    signed alpha = (stock return - SPY return) * direction
    HIT  : signed alpha >= +CRAMER_ALPHA_PCT
    MISS : signed alpha <= -CRAMER_ALPHA_PCT   (a MISS on a buy = inverse-Cramer)
    WASH : in between

Verdicts append to the "Cramer Scorecard" sheet tab; a daily card posts the
new resolutions, open positions with running returns, and the all-time record.
Runs inside the daily Cramer job; skips gracefully without MASSIVE_API_KEY.
"""

import logging
from datetime import datetime, timedelta, timezone

from .. import clients, config, notify, sheets

log = logging.getLogger(__name__)

SCORE_HEADER = [
    "mention_at_utc", "ticker", "stance", "baseline_date", "baseline_close",
    "eval_date", "eval_close", "stock_return_pct", "spy_return_pct",
    "alpha_pct", "verdict", "quote",
]

#: stance -> direction sign; stances absent here are not judged.
_DIRECTION = {"buy": 1, "sell": -1, "avoid": -1, "trim": -1}


def _load_mentions() -> list[dict]:
    out = []
    for row in sheets.read_all(config.SHEET_CRAMER_TAB):
        if len(row) < 4:
            continue
        try:
            ts = datetime.fromisoformat(row[0])
        except (TypeError, ValueError):
            continue  # header or malformed
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        stance = row[2].strip().lower()
        if stance not in _DIRECTION:
            continue
        out.append({
            "key": (row[0], row[1].strip().upper()),
            "at": ts,
            "ticker": row[1].strip().upper(),
            "stance": stance,
            "quote": row[3],
        })
    return out


def _evaluated_keys() -> set[tuple]:
    return {
        (row[0], row[1].strip().upper())
        for row in sheets.read_all(config.SHEET_CRAMER_SCORE_TAB)
        if len(row) >= 2
    }


def _totals(new_resolved: list[dict]) -> dict:
    counts = {"HIT": 0, "MISS": 0, "WASH": 0}
    for row in sheets.read_all(config.SHEET_CRAMER_SCORE_TAB):
        if len(row) > 10 and row[10] in counts:
            counts[row[10]] += 1
    for r in new_resolved:
        counts[r["verdict"]] += 1
    return counts


def _measure(market, mention: dict) -> dict | None:
    """Stock + SPY window returns since the mention; None if data incomplete."""
    start = mention["at"].date()
    stock = market.window_prices(mention["ticker"], start)
    spy = market.window_prices("SPY", start)
    if not stock or not spy:
        return None
    return {
        **mention,
        "stock": stock,
        "spy_return_pct": spy["return_pct"],
        "alpha_pct": round(stock["return_pct"] - spy["return_pct"], 2),
    }


def _verdict(stance: str, alpha_pct: float) -> str:
    signed = alpha_pct * _DIRECTION[stance]
    if signed >= config.CRAMER_ALPHA_PCT:
        return "HIT"
    if signed <= -config.CRAMER_ALPHA_PCT:
        return "MISS"
    return "WASH"


def update() -> None:
    if not config.get("MASSIVE_API_KEY"):
        log.info("Scorecard skipped: MASSIVE_API_KEY not set.")
        return
    market = clients.market_client()
    now = datetime.now(timezone.utc)
    eval_age = timedelta(days=config.CRAMER_EVAL_DAYS)

    mentions = _load_mentions()
    if not mentions:
        log.info("Scorecard: no directional mentions logged yet.")
        return
    evaluated = _evaluated_keys()

    due = [m for m in mentions if now - m["at"] >= eval_age and m["key"] not in evaluated]
    open_mentions = [m for m in mentions if now - m["at"] < eval_age]
    # Budget: resolutions first, then open positions; one bars call per unique
    # ticker (+1 for SPY per unique mention date), all day-cached.
    budget = config.CRAMER_EVAL_TICKER_CAP

    resolved: list[dict] = []
    priced_open: list[dict] = []

    for m in due:
        if budget <= 0:
            log.warning("Scorecard ticker budget reached; %d resolutions wait "
                        "for tomorrow.", len(due) - len(resolved))
            break
        measured = _measure(market, m)
        budget -= 1
        if measured is None:
            continue  # retry tomorrow
        measured["verdict"] = _verdict(m["stance"], measured["alpha_pct"])
        resolved.append(measured)

    seen_open: set[tuple] = set()
    for m in sorted(open_mentions, key=lambda x: x["at"], reverse=True):
        open_key = (m["ticker"], m["stance"])
        if budget <= 0 or open_key in seen_open:
            continue
        seen_open.add(open_key)
        measured = _measure(market, m)
        budget -= 1
        if measured:
            measured["day"] = (now - m["at"]).days
            priced_open.append(measured)

    # Tally BEFORE writing today's rows (else they'd be counted twice).
    totals = _totals(resolved)

    if resolved:
        sheets.write_to_sheet(config.SHEET_CRAMER_SCORE_TAB, [[
            r["key"][0], r["ticker"], r["stance"],
            r["stock"]["baseline_date"], r["stock"]["baseline_close"],
            r["stock"]["last_date"], r["stock"]["last_close"],
            r["stock"]["return_pct"], r["spy_return_pct"], r["alpha_pct"],
            r["verdict"], r["quote"],
        ] for r in resolved])

    if resolved or priced_open:
        notify.post_cramer_scorecard(resolved, priced_open, totals)

    log.info("Scorecard: %d resolved, %d open positions priced.",
             len(resolved), len(priced_open))
