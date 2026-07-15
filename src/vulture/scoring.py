"""Deterministic composite score.

Claude produces per-dimension sub-scores (analysis.TickerScore); the weighting
lives here in code so it can be tuned — against the Sheet log of every scored
candidate — without touching prompts.
"""

from .analysis import TickerScore

WEIGHTS = {
    "thesis_quality": 0.35,
    "community_conviction": 0.30,
    "news_catalyst": 0.25,
    "technical_setup": 0.10,
}

#: Added when the ticker is also trending on Stocktwits (cross-platform confirmation).
CROSS_PLATFORM_BONUS = 0.5

#: Subtracted per red flag, capped.
RED_FLAG_PENALTY = 0.75
RED_FLAG_PENALTY_CAP = 2.25


def composite(score: TickerScore, cross_platform: bool = False) -> float:
    if score.ticker in ("N/A", ""):
        return 0.0
    value = sum(getattr(score, dim) * w for dim, w in WEIGHTS.items())
    if cross_platform:
        value += CROSS_PLATFORM_BONUS
    value -= min(len(score.red_flags) * RED_FLAG_PENALTY, RED_FLAG_PENALTY_CAP)
    return round(max(0.0, min(10.0, value)), 2)
