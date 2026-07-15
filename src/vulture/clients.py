"""Lazy, cached API client construction.

Clients are built on first use so importing the package (e.g. in tests)
requires no credentials.
"""

import json
import logging
from functools import lru_cache

from . import config

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def anthropic_client():
    import anthropic

    return anthropic.Anthropic(api_key=config.get("ANTHROPIC_API_KEY"))


@lru_cache(maxsize=1)
def reddit_client():
    import praw

    return praw.Reddit(
        client_id=config.get("CLIENT_ID"),
        client_secret=config.get("CLIENT_SECRET"),
        user_agent=config.get("USER_AGENT"),
    )


@lru_cache(maxsize=1)
def gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials

    creds_json = json.loads(config.get("GOOGLE_CREDENTIALS_JSON"))
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    return gspread.authorize(creds)


@lru_cache(maxsize=1)
def market_client():
    from .market import MarketData

    return MarketData(config.get("MASSIVE_API_KEY"))
