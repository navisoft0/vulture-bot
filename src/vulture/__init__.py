"""Vulture: an options-focused trending-stock scanner.

Reddit (and best-effort Stocktwits) surface candidate tickers; Massive
enriches them with market context; Claude scores them against an explicit
rubric; candidates that clear POST_THRESHOLD are posted to Discord with the
option plays being discussed.
"""

__version__ = "2.0.0"
