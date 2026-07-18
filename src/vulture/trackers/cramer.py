"""Cramer Watch: extract Jim Cramer's stock calls from CNBC Mad Money recaps.

Best-effort by design — CNBC page structure can change and scraping is
inherently brittle; every step degrades gracefully. Extraction itself is a
Claude structured-output call over the article text.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone

import requests

from .. import analysis, config, notify, sheets, state

log = logging.getLogger(__name__)

MAD_MONEY_URL = "https://www.cnbc.com/mad-money/"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; vulture-bot/2.0; personal research tool)"}
_TIMEOUT = 15

_ARTICLE_RE = re.compile(r"https://www\.cnbc\.com/(2\d{3})/(\d{2})/(\d{2})/[a-z0-9-]+\.html")
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.DOTALL | re.IGNORECASE)

MAX_ARTICLES_PER_RUN = 3

#: The landing page pins evergreen "guide to investing" articles from years
#: ago; only articles published within this window are considered.
MAX_ARTICLE_AGE_DAYS = 7


def _fetch(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.RequestException as e:
        log.warning("Could not fetch %s: %s", url, e)
        return None


def _strip_html(html: str) -> str:
    text = _BLOCK_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _relevant(url: str) -> bool:
    slug = url.rsplit("/", 1)[-1]
    return any(k in slug for k in ("cramer", "mad-money", "lightning"))


def _recent_articles(landing_html: str) -> list[str]:
    """Relevant article URLs from the landing page, newest first, capped to
    MAX_ARTICLE_AGE_DAYS so pinned evergreen articles never crowd out recaps."""
    cutoff = date.today() - timedelta(days=MAX_ARTICLE_AGE_DAYS)
    dated: list[tuple[date, str]] = []
    seen: set[str] = set()
    for m in _ARTICLE_RE.finditer(landing_html):
        url = m.group(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            published = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if published >= cutoff and _relevant(url):
            dated.append((published, url))
    dated.sort(key=lambda item: item[0], reverse=True)
    return [url for _, url in dated]


def run_cramer_tracker() -> None:
    config.validate_env("cramer")
    log.info("--- Cramer tracker starting ---")

    landing = _fetch(MAD_MONEY_URL)
    if not landing:
        log.error("Mad Money landing page unavailable; aborting run.")
        return

    urls = _recent_articles(landing)
    seen_store = state.cramer_seen_store()
    seen = seen_store.load()
    new_urls = [u for u in urls if u not in seen][:MAX_ARTICLES_PER_RUN]
    if not new_urls:
        log.info("No new Mad Money recap articles.")
        return

    mentions, used_urls = [], []
    for url in new_urls:
        html = _fetch(url)
        if not html:
            continue
        text = _strip_html(html)
        if len(text) < 500:
            log.warning("Article %s produced too little text; skipping.", url)
            continue
        extracted = analysis.extract_cramer_mentions(text)
        if extracted:
            mentions.extend(extracted)
            used_urls.append(url)
        log.info("Extracted %d mentions from %s", len(extracted), url)

    posted = True
    if mentions:
        posted = notify.post_cramer_digest(mentions, used_urls)
        now = datetime.now(timezone.utc).isoformat()
        sheets.write_to_sheet(
            config.SHEET_CRAMER_TAB,
            [[now, m.ticker, m.stance, m.quote, ", ".join(used_urls)] for m in mentions],
        )

    if posted:
        seen_store.add(new_urls)
    else:
        # Leave the articles unseen so the digest retries next run once the
        # webhook is fixed (the sheet may then carry duplicate mention rows).
        log.warning("Digest did not reach Discord; these articles will retry next run.")
    log.info("--- Cramer tracker complete: %d articles, %d mentions, posted=%s ---",
             len(used_urls), len(mentions), posted)


def recent_mentions(days: int = 7) -> dict[str, str]:
    """{ticker: stance} from the Cramer sheet within `days` (for overlap stamps)."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out: dict[str, str] = {}
    try:
        rows = sheets.read_column(config.SHEET_CRAMER_TAB, col=1)
        tickers = sheets.read_column(config.SHEET_CRAMER_TAB, col=2)
        stances = sheets.read_column(config.SHEET_CRAMER_TAB, col=3)
        for ts_str, ticker, stance in zip(rows, tickers, stances):
            try:
                if datetime.fromisoformat(ts_str).timestamp() >= cutoff:
                    out[ticker] = stance
            except ValueError:
                continue
    except Exception as e:
        log.warning("Could not read Cramer mentions: %s", e)
    return out
