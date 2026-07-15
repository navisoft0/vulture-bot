"""Claude-powered analysis: candidate scoring and Cramer-recap extraction.

All calls use structured outputs (`client.messages.parse` + Pydantic), so
malformed responses raise instead of silently corrupting the pipeline.
"""

import logging
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

from . import clients, config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class OptionPlay(BaseModel):
    direction: Literal["bullish", "bearish", "neutral"]
    structure: str = Field(description='e.g. "calls", "puts", "call debit spread", "shares"')
    strike: Optional[float] = Field(default=None, description="Strike price as a number, if stated")
    expiry: Optional[str] = Field(default=None, description="Expiry as YYYY-MM-DD if determinable, else null")
    rationale: str = Field(description="One line: why this play, per the thread")


class TickerScore(BaseModel):
    ticker: str = Field(description='The SINGLE stock ticker discussed. "N/A" if none or many.')
    thesis_quality: float = Field(ge=0, le=10)
    community_conviction: float = Field(ge=0, le=10)
    news_catalyst: float = Field(ge=0, le=10)
    technical_setup: float = Field(ge=0, le=10)
    plays_discussed: list[OptionPlay]
    briefing: str = Field(description="Synthesized thesis + community reaction, 2-4 sentences")
    red_flags: list[str]


class CramerMention(BaseModel):
    ticker: str
    stance: Literal["buy", "sell", "trim", "avoid", "hold", "unclear"]
    quote: str = Field(description="Short verbatim-ish quote or paraphrase of Cramer's take")


class CramerExtraction(BaseModel):
    mentions: list[CramerMention]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

SCORING_SYSTEM_PROMPT = """\
You are an expert retail-options analyst. You evaluate a Reddit post (plus its top
comments and supplied market data) to decide whether the discussed stock is worth a
trader's research time, and you extract the option plays being discussed.

Score each dimension 0-10 independently. Anchor bands:

thesis_quality — is there an actual thesis with a catalyst and timeline?
  8-10: specific catalyst, timeline, and mechanism (not just "it will moon")
  4-7:  plausible idea, vague on timing or mechanism
  0-3:  pure hype, a question, or no thesis at all

community_conviction — how did the comments receive it?
  8-10: substantive validation, informed agreement, people adding supporting data
  4-7:  mixed reception or low engagement
  0-3:  thesis torn apart, debunked, or mocked

news_catalyst — do the supplied recent headlines support a near-term move?
  8-10: fresh, directly relevant catalyst in the headlines
  4-7:  loosely related news, or thesis not yet reflected in news
  0-3:  no relevant news, or news contradicts the thesis
  If NO news data is supplied, output exactly 5.0.

technical_setup — does the supplied price/volume/RSI context fit the direction?
  8-10: momentum and levels clearly align with the discussed direction
  4-7:  neutral or ambiguous setup
  0-3:  data contradicts the thesis (e.g. bullish thesis, RSI 85 after a huge run
        can also justify a LOW score as chasing)
  If NO market data is supplied for the ticker you chose, output exactly 5.0.

plays_discussed — extract the concrete option plays from the post AND comments:
direction, structure (calls/puts/spreads/shares), strike as a number, and expiry
normalized to YYYY-MM-DD when determinable (current date is provided). Only include
plays actually discussed — do not invent your own.

red_flags — list concrete concerns: pump-and-dump patterns, position-less DD,
stale thesis, microcap illiquidity, contradiction with supplied data, etc.

Rules:
- If no single clear ticker: ticker="N/A" and all scores 0.
- If the market data supplied is for a different ticker than the one you choose,
  ignore it and use the 5.0 neutral rule.
- Judge only from supplied material. Do not use outside knowledge of the ticker.
"""


def score_candidate(post: dict, comments: str, market_block: str | None,
                    stocktwits_block: str | None, today: str) -> TickerScore | None:
    """One structured scoring call. Returns None on API failure."""
    sections = [
        f"Current date: {today}",
        f"**Post title:** {post['title']}",
        f"**Post body:**\n{post['selftext'][:6000]}",
        f"**Top comments:**\n{comments[:8000]}",
    ]
    sections.append(f"**Market data:**\n{market_block}" if market_block
                    else "**Market data:** none supplied")
    if stocktwits_block:
        sections.append(f"**Stocktwits:**\n{stocktwits_block}")

    try:
        response = clients.anthropic_client().messages.parse(
            model=config.CLAUDE_MODEL,
            max_tokens=2500,
            system=SCORING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n\n".join(sections)}],
            output_format=TickerScore,
        )
        if response.stop_reason == "refusal":
            log.warning("Claude refused to score post %s.", post["id"])
            return None
        return response.parsed_output
    except anthropic.RateLimitError as e:
        log.warning("Anthropic rate limit scoring post %s: %s", post["id"], e)
    except anthropic.APIStatusError as e:
        log.error("Anthropic API error (%s) scoring post %s: %s", e.status_code, post["id"], e.message)
    except anthropic.APIConnectionError as e:
        log.error("Network error scoring post %s: %s", post["id"], e)
    except Exception as e:
        log.error("Unexpected error scoring post %s: %s", post["id"], e)
    return None


# ---------------------------------------------------------------------------
# Cramer extraction
# ---------------------------------------------------------------------------

CRAMER_SYSTEM_PROMPT = """\
You extract Jim Cramer's stock calls from a CNBC Mad Money recap article.

For every stock Cramer expresses a view on, output the ticker (uppercase; infer the
ticker from the company name if only the name is given and you are confident),
his stance (buy/sell/trim/avoid/hold/unclear), and a short quote or close paraphrase
of what he said. Only include calls Cramer himself makes in this article. If the
article contains no Cramer stock calls, return an empty list.
"""


def extract_cramer_mentions(article_text: str) -> list[CramerMention]:
    try:
        response = clients.anthropic_client().messages.parse(
            model=config.CLAUDE_MODEL,
            max_tokens=2000,
            system=CRAMER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": article_text[:12000]}],
            output_format=CramerExtraction,
        )
        if response.stop_reason == "refusal":
            return []
        return response.parsed_output.mentions
    except anthropic.APIError as e:
        log.error("Anthropic error extracting Cramer mentions: %s", e)
        return []
