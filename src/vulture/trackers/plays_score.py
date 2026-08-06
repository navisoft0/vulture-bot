"""Play tracker grading: did the promoted play hit?

Pulls due plays (promoted, expiry passed, ungraded) from the dashboard API,
grades each, and posts results back:

- contract_eod  : entry/exit on the option contract's own EOD closes
                  (Massive Options Basic includes EOD aggregates). HIT when the
                  premium returned >= +PLAY_HIT_PCT, MISS <= -PLAY_HIT_PCT.
- underlying_itm: fallback when contract bars are unavailable — ITM at expiry
                  in the contract's direction is a HIT, OTM a MISS.
- underlying_move: plays without a strike — underlying return since promotion
                  in the play's direction (±2% wash band).
- UNGRADEABLE   : no usable data at all.
"""

import logging
from datetime import date, datetime, timezone

from .. import clients, config, store
from ..pipeline import _infer_contract_type

log = logging.getLogger(__name__)


def _parse_date(value) -> date | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None


def _grade(market, play: dict) -> dict | None:
    promoted = _parse_date(play.get("promoted_at")) or _parse_date(play.get("expiry"))
    expiry = _parse_date(play.get("expiry"))
    if promoted is None or expiry is None:
        return None
    ticker = play["ticker"].upper()
    ctype = _infer_contract_type(play.get("structure") or "") or (
        "call" if play.get("direction") == "bullish"
        else "put" if play.get("direction") == "bearish" else None)

    under = market.window_prices(ticker, promoted, end=expiry)
    result = {
        "play_id": play["id"],
        "graded_at": datetime.now(timezone.utc).isoformat(),
        "entry_underlying": under["baseline_close"] if under else None,
        "exit_underlying": under["last_close"] if under else None,
    }

    # Primary: the contract's own EOD prices.
    if ctype and play.get("strike"):
        occ = market.occ_symbol(ticker, expiry.isoformat(), ctype, float(play["strike"]))
        contract = market.window_prices(occ, promoted, end=expiry)
        if contract:
            ret = contract["return_pct"]
            verdict = ("HIT" if ret >= config.PLAY_HIT_PCT
                       else "MISS" if ret <= -config.PLAY_HIT_PCT else "WASH")
            return {**result, "method": "contract_eod",
                    "entry_contract": contract["baseline_close"],
                    "exit_contract": contract["last_close"],
                    "return_pct": ret, "verdict": verdict}

    # Fallback: underlying vs strike (ITM/OTM at expiry).
    if under and ctype and play.get("strike"):
        strike = float(play["strike"])
        itm = under["last_close"] > strike if ctype == "call" else under["last_close"] < strike
        return {**result, "method": "underlying_itm",
                "return_pct": under["return_pct"],
                "verdict": "HIT" if itm else "MISS"}

    # No strike: direction on the underlying, small wash band.
    if under and play.get("direction") in ("bullish", "bearish"):
        signed = under["return_pct"] * (1 if play["direction"] == "bullish" else -1)
        verdict = "HIT" if signed >= 2.0 else "MISS" if signed <= -2.0 else "WASH"
        return {**result, "method": "underlying_move",
                "return_pct": under["return_pct"], "verdict": verdict}

    return {**result, "method": "underlying_itm", "return_pct": None,
            "verdict": "UNGRADEABLE"}


def update() -> None:
    if not store.enabled():
        log.info("Play tracker skipped: dashboard not configured.")
        return
    if not config.get("MASSIVE_API_KEY"):
        log.info("Play tracker skipped: MASSIVE_API_KEY not set.")
        return
    due = store.fetch_due_plays()
    if not due:
        log.info("Play tracker: nothing due.")
        return

    market = clients.market_client()
    results = []
    for play in due[: config.PLAY_GRADE_CAP]:
        try:
            graded = _grade(market, play)
        except Exception:
            log.exception("Grading play %s failed; will retry tomorrow.", play.get("id"))
            continue
        if graded:
            results.append(graded)
            log.info("Play %s %s %s -> %s (%s)", play["ticker"],
                     play.get("structure"), play.get("expiry"),
                     graded["verdict"], graded["method"])
    store.emit_play_results(results)
    log.info("Play tracker: graded %d/%d due plays.", len(results), len(due))
