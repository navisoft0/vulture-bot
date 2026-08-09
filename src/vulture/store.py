"""Dashboard ingest client: dual-writes engine output to the Cloudflare Worker.

Every function is a no-op unless DASHBOARD_API_URL and DASHBOARD_INGEST_TOKEN
are set, and every network failure is logged and swallowed — the dashboard is
a consumer, never a dependency; Sheets and Discord keep working without it.
"""

import logging

import requests

from . import config

log = logging.getLogger(__name__)

_TIMEOUT = 20


def enabled() -> bool:
    return bool(config.get("DASHBOARD_API_URL") and config.get("DASHBOARD_INGEST_TOKEN"))


def _request(method: str, path: str, payload: dict | None = None):
    if not enabled():
        return None
    url = config.get("DASHBOARD_API_URL").rstrip("/") + path
    headers = {"Authorization": f"Bearer {config.get('DASHBOARD_INGEST_TOKEN')}"}
    try:
        resp = requests.request(method, url, json=payload, headers=headers, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            log.warning("Dashboard %s %s -> %s: %s", method, path, resp.status_code, resp.text[:200])
            return None
        return resp.json()
    except requests.exceptions.RequestException as e:
        log.warning("Dashboard %s %s failed: %s", method, path, e)
        return None


def emit_scan(scan: dict, candidates: list[dict]) -> None:
    if _request("POST", "/api/ingest/scan", {"scan": scan, "candidates": candidates}):
        log.info("Dashboard: ingested scan %s (%d candidates).", scan["id"], len(candidates))


def emit_cramer(mentions: list[dict]) -> None:
    if mentions and _request("POST", "/api/ingest/cramer", {"mentions": mentions}):
        log.info("Dashboard: ingested %d Cramer mentions.", len(mentions))


def emit_cramer_verdicts(verdicts: list[dict]) -> None:
    if verdicts and _request("POST", "/api/ingest/cramer_verdicts", {"verdicts": verdicts}):
        log.info("Dashboard: ingested %d Cramer verdicts.", len(verdicts))


def fetch_due_plays() -> list[dict]:
    data = _request("GET", "/api/plays/due")
    return (data or {}).get("plays", [])


def emit_play_results(results: list[dict]) -> None:
    if results and _request("POST", "/api/ingest/play_results", {"results": results}):
        log.info("Dashboard: ingested %d play results.", len(results))


def checks_due() -> list[str]:
    """Tickers whose member re-check batch the dashboard has released.
    The Worker owns the batching policy; a non-empty result is already
    claimed server-side."""
    # Path lives under /api/ingest/ so the Access bypass covers it.
    data = _request("GET", "/api/ingest/checks_due")
    return list(data.get("tickers") or []) if data else []


def run_requested() -> bool:
    """Admin run-now queue fallback (used when the engine URL isn't reachable
    from the Worker). Marks the request picked-up server-side."""
    data = _request("GET", "/api/runs/pending")
    return bool(data and data.get("pending"))
