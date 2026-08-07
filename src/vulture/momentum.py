"""Attention momentum: the "same ticker keeps popping up" signal.

Sources the scan history already logged to the Sheet — every scored candidate
row carries ticker, subreddit, composite, posted, and scored_at — and turns
recurrence within a rolling window into a composite bonus plus an embed line.
Also owns the repost cooldown so a hot ticker doesn't spam the forum every run.

Computed in code, like the Stocktwits bonus: frequency is a fact, not a
judgment call for Claude.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config, sheets

log = logging.getLogger(__name__)

# Sheet column indexes (0-based) in the "Vulture Data" tab, per pipeline.SHEET_HEADER.
_COL_TICKER = 1
_COL_COMPOSITE = 2
_COL_POSTED = 8
_COL_BRIEFING = 9
_COL_SUBREDDIT = 13
_COL_SCORED_AT = 15

#: Prior briefings kept per ticker for the cumulative-analysis prompt block.
_MAX_PRIOR_BRIEFINGS = 3

#: Bonus by number of PRIOR mentions in the window (1 prior -> 2nd mention now).
_BONUS_BY_PRIOR = {1: 0.4, 2: 0.7}
_BONUS_MAX_BASE = 1.0
_CROSS_SUB_KICK = 0.25
BONUS_CAP = 1.25


@dataclass
class MentionHistory:
    count: int = 0
    subreddits: set = field(default_factory=set)
    last_posted_at: datetime | None = None
    last_posted_composite: float | None = None
    #: Newest-first (scored_at, subreddit, briefing) of recent prior mentions.
    briefings: list = field(default_factory=list)

    def prior_block(self) -> str | None:
        """Prompt section summarizing recent prior mentions, or None."""
        if not self.briefings:
            return None
        return "\n".join(
            f"- [{ts:%b %d %H:%M} UTC, r/{sub}] {text}"
            for ts, sub, text in self.briefings[:_MAX_PRIOR_BRIEFINGS]
        )


def _parse_ts(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(value)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def load_history(window_hours: int | None = None) -> dict[str, MentionHistory]:
    """Per-ticker mention history from the scored-candidates tab.

    Returns {} on any failure — momentum silently degrades to "no bonus".
    """
    window_hours = window_hours or config.MOMENTUM_WINDOW_H
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    history: dict[str, MentionHistory] = {}

    for row in sheets.read_all(config.SHEET_SCORED_TAB):
        if len(row) <= _COL_SCORED_AT:
            continue
        scored_at = _parse_ts(row[_COL_SCORED_AT])
        if scored_at is None or scored_at < cutoff:
            continue  # header, malformed, or outside the window
        ticker = row[_COL_TICKER].strip().upper()
        if not ticker or ticker == "N/A":
            continue
        h = history.setdefault(ticker, MentionHistory())
        h.count += 1
        if row[_COL_SUBREDDIT].strip():
            h.subreddits.add(row[_COL_SUBREDDIT].strip())
        if len(row) > _COL_BRIEFING and row[_COL_BRIEFING].strip():
            h.briefings.append((scored_at, row[_COL_SUBREDDIT].strip() or "?",
                                row[_COL_BRIEFING].strip()[:400]))
            h.briefings.sort(key=lambda b: b[0], reverse=True)
            del h.briefings[_MAX_PRIOR_BRIEFINGS:]
        if row[_COL_POSTED].strip().upper() == "TRUE":
            try:
                comp = float(row[_COL_COMPOSITE])
            except (TypeError, ValueError):
                comp = None
            if h.last_posted_at is None or scored_at > h.last_posted_at:
                h.last_posted_at = scored_at
                h.last_posted_composite = comp

    log.info("Momentum history: %d tickers mentioned in the last %dh.",
             len(history), window_hours)
    return history


_ORDINALS = {2: "2nd", 3: "3rd"}


def bonus(h: MentionHistory | None, current_subreddit: str) -> tuple[float, str | None]:
    """(composite bonus, embed line) for a candidate given its prior mentions."""
    if h is None or h.count == 0:
        return 0.0, None
    value = _BONUS_BY_PRIOR.get(h.count, _BONUS_MAX_BASE)
    subs = set(h.subreddits) | {current_subreddit}
    if len(subs) >= 2:
        value += _CROSS_SUB_KICK
    value = min(value, BONUS_CAP)

    nth = h.count + 1
    label = _ORDINALS.get(nth, f"{nth}th")
    sub_list = " + ".join(f"r/{s}" for s in sorted(subs)[:3])
    line = f"🔥 {label} mention in {config.MOMENTUM_WINDOW_H}h · {sub_list}"
    return value, line


def repost_allowed(h: MentionHistory | None, composite: float,
                   now: datetime | None = None) -> bool:
    """Cooldown gate: block reposting a ticker within REPOST_COOLDOWN_H unless
    the new composite beats the previously posted one by REPOST_MARGIN."""
    if h is None or h.last_posted_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    if now - h.last_posted_at >= timedelta(hours=config.REPOST_COOLDOWN_H):
        return True
    if h.last_posted_composite is None:
        return False
    return composite >= h.last_posted_composite + config.REPOST_MARGIN
