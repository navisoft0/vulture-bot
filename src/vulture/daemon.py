"""Daemon mode: single long-running process for one-service hosts (Railway).

Loops the scan every SCAN_INTERVAL_MIN minutes and runs the Cramer tracker
once per day after CRAMER_HOUR_UTC (skipped entirely if the news webhook
isn't configured). Each run is isolated — an exception is logged, never
fatal — so one bad scan can't take the service down.
"""

import logging
import time
from datetime import date, datetime, timezone

from . import config
from .pipeline import run_scan
from .trackers.cramer import run_cramer_tracker

log = logging.getLogger(__name__)


def _maybe_run_cramer(last_run_date: date | None) -> date | None:
    if not config.get("DISCORD_WEBHOOK_NEWS"):
        return last_run_date
    now = datetime.now(timezone.utc)
    if now.hour < config.CRAMER_HOUR_UTC or last_run_date == now.date():
        return last_run_date
    try:
        run_cramer_tracker()
    except Exception:
        log.exception("Cramer run failed; will retry tomorrow.")
    return now.date()


def run_daemon() -> None:
    config.validate_env("daemon")
    interval = config.SCAN_INTERVAL_MIN * 60
    log.info("Vulture daemon starting: scan every %d min, Cramer daily after %02d:00 UTC%s.",
             config.SCAN_INTERVAL_MIN, config.CRAMER_HOUR_UTC,
             "" if config.get("DISCORD_WEBHOOK_NEWS") else " (Cramer disabled: no news webhook)")

    last_cramer: date | None = None
    while True:
        started = time.monotonic()
        try:
            run_scan()
        except Exception:
            log.exception("Scan failed; continuing.")
        last_cramer = _maybe_run_cramer(last_cramer)

        elapsed = time.monotonic() - started
        sleep_s = max(60.0, interval - elapsed)
        log.info("Next scan in %.0f min.", sleep_s / 60)
        time.sleep(sleep_s)
