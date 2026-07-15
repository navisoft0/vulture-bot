"""Environment loading, validation, and tunables.

Nothing here talks to the network. Import has no side effects beyond
reading `vulture_cred.env` into the process environment if present.
"""

import os

from dotenv import load_dotenv

load_dotenv("vulture_cred.env")

# ---------------------------------------------------------------------------
# Tunables (env-overridable)
# ---------------------------------------------------------------------------

TARGET_SUBREDDITS = [
    s.strip()
    for s in os.getenv(
        "TARGET_SUBREDDITS",
        "wallstreetbets,shortsqueeze,WallStreetbetsELITE,smallstreetbets",
    ).split(",")
    if s.strip()
]

#: Composite score a candidate must reach to be posted to Discord.
POST_THRESHOLD = float(os.getenv("POST_THRESHOLD", "7.0"))

#: Composite score at/above which the "high" forum tag is applied.
HIGH_TAG_THRESHOLD = float(os.getenv("HIGH_TAG_THRESHOLD", "8.5"))

#: Medium tag band lower bound (between POST_THRESHOLD and HIGH_TAG_THRESHOLD).
MEDIUM_TAG_THRESHOLD = float(os.getenv("MEDIUM_TAG_THRESHOLD", "7.5"))

#: Maximum posts per scan sent to Claude (cost guard).
MAX_POSTS_PER_SCAN = int(os.getenv("MAX_POSTS_PER_SCAN", "40"))

#: Maximum age of a Reddit post to consider, in days.
MAX_POST_AGE_DAYS = int(os.getenv("MAX_POST_AGE_DAYS", "2"))

#: Number of top comments fetched per post.
COMMENTS_PER_POST = int(os.getenv("COMMENTS_PER_POST", "25"))

#: Enable the best-effort Stocktwits signal.
STOCKTWITS_ENABLED = os.getenv("STOCKTWITS_ENABLED", "true").lower() in ("1", "true", "yes")

#: Processed-post state backend: "file" or "sheet".
STATE_BACKEND = os.getenv("STATE_BACKEND", "file")

#: Claude model used for scoring and extraction.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "data")

# Google Sheets worksheet (tab) names.
SHEET_SCORED_TAB = os.getenv("SHEET_SCORED_TAB", "Vulture Data")
SHEET_PROCESSED_TAB = os.getenv("SHEET_PROCESSED_TAB", "Processed")
SHEET_CRAMER_TAB = os.getenv("SHEET_CRAMER_TAB", "Cramer Watch")

# ---------------------------------------------------------------------------
# Required environment variables, per command
# ---------------------------------------------------------------------------

_REQUIRED = {
    "scan": [
        "ANTHROPIC_API_KEY",
        "MASSIVE_API_KEY",
        "CLIENT_ID",
        "CLIENT_SECRET",
        "USER_AGENT",
        "DISCORD_WEBHOOK_FORUM",
        "GOOGLE_CREDENTIALS_JSON",
        "GOOGLE_SHEET_NAME",
    ],
    "cramer": [
        "ANTHROPIC_API_KEY",
        "DISCORD_WEBHOOK_NEWS",
    ],
}

# Optional but used when present.
OPTIONAL_VARS = [
    "DISCORD_TAG_ID_LOW",
    "DISCORD_TAG_ID_MEDIUM",
    "DISCORD_TAG_ID_HIGH",
    "DISCORD_WEBHOOK_NEWS",
]


def validate_env(command: str) -> None:
    """Raise ValueError listing every missing required variable for `command`."""
    required = _REQUIRED.get(command, [])
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        raise ValueError(
            f"Missing required environment variables for '{command}': {', '.join(missing)}"
        )


def get(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)
