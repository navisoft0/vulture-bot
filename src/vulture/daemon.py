"""Daemon mode: single long-running process for one-service hosts (Railway).

- Scans every SCAN_INTERVAL_MIN minutes.
- Runs the Cramer tracker (digest + scorecard) and play-tracker grading once
  per day after CRAMER_HOUR_UTC.
- Exposes a tiny HTTP endpoint (POST /run, bearer RUN_TOKEN) so the dashboard
  admin panel can trigger an immediate scan; also polls the dashboard's
  run-request queue as a fallback.

Each run is isolated — an exception is logged, never fatal.
"""

import logging
import threading
import time
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, store
from .pipeline import run_scan
from .trackers.cramer import run_cramer_tracker

log = logging.getLogger(__name__)

#: Set by the HTTP listener (or queue poll); daemon loop wakes and scans.
_run_now = threading.Event()


class _TriggerHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # route http.server noise to our logger
        log.debug("http: " + fmt, *args)

    def _reply(self, code, body):
        self.send_response(code)
        self.send_header("content-type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, "ok")
        else:
            self._reply(404, "not found")

    def do_POST(self):
        if self.path != "/run":
            self._reply(404, "not found")
            return
        token = config.get("RUN_TOKEN")
        auth = self.headers.get("Authorization", "")
        if not token or auth != f"Bearer {token}":
            self._reply(401, "unauthorized")
            return
        _run_now.set()
        log.info("Run-now trigger received via HTTP.")
        self._reply(202, "scan queued")


def _start_trigger_server() -> None:
    if not config.get("RUN_TOKEN"):
        log.info("Run-now endpoint disabled (RUN_TOKEN not set).")
        return
    port = int(config.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _TriggerHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Run-now endpoint listening on :%d (POST /run).", port)


def _maybe_run_daily(last_run_date: date | None) -> date | None:
    """Once a day after CRAMER_HOUR_UTC: Cramer digest+scorecard, play grading."""
    now = datetime.now(timezone.utc)
    if now.hour < config.CRAMER_HOUR_UTC or last_run_date == now.date():
        return last_run_date
    if config.get("DISCORD_WEBHOOK_NEWS"):
        try:
            run_cramer_tracker()
        except Exception:
            log.exception("Cramer run failed; will retry tomorrow.")
    try:
        from .trackers import plays_score
        plays_score.update()
    except Exception:
        log.exception("Play grading failed; will retry tomorrow.")
    return now.date()


def run_daemon() -> None:
    config.validate_env("daemon")
    _start_trigger_server()
    interval = config.SCAN_INTERVAL_MIN * 60
    log.info("Vulture daemon starting: scan every %d min, daily jobs after %02d:00 UTC%s.",
             config.SCAN_INTERVAL_MIN, config.CRAMER_HOUR_UTC,
             "" if config.get("DISCORD_WEBHOOK_NEWS") else " (Cramer digest disabled: no news webhook)")

    last_daily: date | None = None
    trigger = "cron"
    while True:
        started = time.monotonic()
        try:
            run_scan(trigger=trigger)
        except Exception:
            log.exception("Scan failed; continuing.")
        last_daily = _maybe_run_daily(last_daily)

        elapsed = time.monotonic() - started
        sleep_s = max(60.0, interval - elapsed)
        log.info("Next scan in %.0f min (or on run-now trigger).", sleep_s / 60)

        # Wake early on the HTTP trigger; check the queue fallback midway.
        triggered = _run_now.wait(timeout=min(sleep_s, 300))
        waited = min(sleep_s, 300)
        while not triggered and waited < sleep_s:
            if store.run_requested():
                triggered = True
                break
            triggered = _run_now.wait(timeout=min(300, sleep_s - waited))
            waited += 300
        _run_now.clear()
        trigger = "manual" if triggered else "cron"
