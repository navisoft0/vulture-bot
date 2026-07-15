"""Reddit signal: post scraping, comment fetching, candidate ticker extraction.

Scraping logic is ported from v1; candidate extraction is new (it gates
which posts reach Claude and which symbols get Massive enrichment).
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone

from .. import clients, config

log = logging.getLogger(__name__)

# Uppercase tokens that look like tickers but aren't (or are too noisy to enrich).
_STOPWORDS = {
    "A", "I", "AI", "AM", "AN", "ALL", "AND", "ANY", "APE", "APES", "ARE", "AT", "ATH",
    "ATM", "BE", "BIG", "BRO", "BUY", "BS", "CALL", "CALLS", "CEO", "CFO", "CPI", "DAY",
    "DD", "DID", "DIP", "DO", "DOJ", "EDIT", "EOD", "EOW", "EOY", "EPS", "ETF", "EU",
    "EV", "FAQ", "FD", "FDA", "FED", "FOMO", "FOR", "FTC", "FY", "GAIN", "GDP", "GO",
    "GG", "GUH", "HAS", "HE", "HODL", "HOLD", "HUGE", "IF", "IMO", "IN", "IPO", "IRA",
    "IRS", "IS", "IT", "ITM", "IV", "IVR", "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "SEPT", "OCT", "NOV", "DEC", "LFG", "LLC", "LOL", "LONG",
    "LOSS", "MC", "ME", "MEME", "MOON", "MY", "NEW", "NFA", "NO", "NOT", "NOW", "NYSE",
    "OF", "OG", "OK", "ON", "ONE", "OP", "OR", "OTC", "OTM", "PC", "PE", "PM", "PSA",
    "PUT", "PUTS", "PT", "Q1", "Q2", "Q3", "Q4", "RH", "RIP", "ROI", "SEC", "SHORT",
    "SO", "SP", "STILL", "TA", "THE", "TIL", "TLDR", "TO", "TOS", "UK", "UP", "US",
    "USA", "USD", "VS", "WSB", "WTF", "YOLO", "YOY", "YTD",
}

_DOLLAR_TICKER = re.compile(r"\$([A-Za-z]{1,5})\b")
_BARE_TICKER = re.compile(r"\b([A-Z]{2,5})\b")


def extract_candidate_tickers(text: str, max_candidates: int = 3) -> list[str]:
    """Cheap pre-filter: likely ticker symbols mentioned in `text`.

    $-prefixed symbols are trusted; bare uppercase tokens must survive the
    stopword list. Massive validation downstream prunes remaining junk.
    """
    dollar = [m.upper() for m in _DOLLAR_TICKER.findall(text)]
    bare = [m for m in _BARE_TICKER.findall(text) if m not in _STOPWORDS]

    seen, out = set(), []
    for sym in dollar + bare:
        if sym in _STOPWORDS:
            continue
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
        if len(out) >= max_candidates:
            break
    return out


def scrape_new_posts(processed_ids: set[str]) -> list[dict]:
    """Fetch recent, unseen, text-bearing posts from the target subreddits."""
    reddit = clients.reddit_client()
    all_posts = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=config.MAX_POST_AGE_DAYS)

    for sub in config.TARGET_SUBREDDITS:
        log.info("Fetching posts from r/%s...", sub)
        try:
            posts = list(reddit.subreddit(sub).new(limit=100))
        except Exception as e:
            log.warning("Could not fetch r/%s: %s", sub, e)
            continue
        for p in posts:
            if p.id in processed_ids:
                continue
            created = datetime.fromtimestamp(p.created_utc, timezone.utc)
            if created < cutoff:
                continue
            if p.url.endswith((".jpeg", ".jpg", ".png", ".gif")) or "v.redd.it" in p.url:
                continue
            all_posts.append({
                "id": p.id,
                "subreddit": sub,
                "title": p.title,
                "selftext": p.selftext or "",
                "url": f"https://reddit.com{p.permalink}",
                "created_utc": created.isoformat(),
                "score": p.score,
                "num_comments": p.num_comments,
            })
    log.info("Found %d new candidate posts.", len(all_posts))
    return all_posts


def get_comments(post_id: str) -> str:
    """Top comments for a post, newline-joined ('' on failure)."""
    try:
        submission = clients.reddit_client().submission(id=post_id)
        submission.comment_sort = "top"
        comments = [
            c.body for c in submission.comments[: config.COMMENTS_PER_POST]
            if hasattr(c, "body") and not getattr(c, "stickied", False)
        ]
        time.sleep(1)  # be gentle with Reddit
        return "\n".join(comments)
    except Exception as e:
        log.warning("Could not fetch comments for post %s: %s", post_id, e)
        return ""
