"""Claude-powered analysis: candidate scoring and Cramer-recap extraction.

Cost posture:
- Scoring goes through the Message Batches API by default (50% cheaper than
  synchronous calls; a cron scan doesn't care about the added minutes), with
  a synchronous fallback if batch submission fails.
- Structured outputs everywhere (raw JSON-schema `output_config.format`, with
  Pydantic validation on our side), so malformed responses are impossible to
  mistake for data.
- Model and effort are env-tunable (CLAUDE_MODEL / CLAUDE_EFFORT); Cramer
  extraction defaults to Haiku (CRAMER_MODEL) — it's mechanical extraction.
"""

import json
import logging
import time
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field, ValidationError

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
# JSON-schema plumbing for structured outputs
# ---------------------------------------------------------------------------

#: Constraint keywords the structured-outputs API rejects; Pydantic still
#: enforces them client-side at validation time.
_UNSUPPORTED_KEYS = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "minItems", "maxItems", "multipleOf",
}


def _clean_schema(node):
    if isinstance(node, dict):
        cleaned = {k: _clean_schema(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYS}
        if cleaned.get("type") == "object":
            cleaned["additionalProperties"] = False
        return cleaned
    if isinstance(node, list):
        return [_clean_schema(x) for x in node]
    return node


_TICKER_SCORE_SCHEMA = _clean_schema(TickerScore.model_json_schema())


def _output_config() -> dict:
    cfg = {"format": {"type": "json_schema", "schema": _TICKER_SCORE_SCHEMA}}
    if config.CLAUDE_EFFORT:
        cfg["effort"] = config.CLAUDE_EFFORT
    return cfg


def _parse_score(message, label: str) -> TickerScore | None:
    """Validate a scoring response message into a TickerScore."""
    if message.stop_reason == "refusal":
        log.warning("Claude refused to score %s.", label)
        return None
    if message.stop_reason == "max_tokens":
        log.warning("Scoring %s hit max_tokens; discarding truncated output.", label)
        return None
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        return TickerScore.model_validate_json(text)
    except ValidationError as e:
        log.warning("Invalid scoring output for %s: %s", label, e)
        return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

SCORING_SYSTEM_PROMPT = """\
You are the first-pass filter for a retail options SCANNER. The mission is to
surface tickers worth a closer look — "turn your head" moments a trading group
will investigate themselves — NOT to green-light investments. Speculative and
volatile is the normal state of this universe; do not penalize a post for being
speculative. Penalize it for being empty, debunked, or contradicted by data.
You also extract the option plays being discussed.

Score each dimension 0-10 independently. Anchor bands:

thesis_quality — is there an actual setup with a mechanism?
  8-10: specific catalyst or structural setup with a mechanism and rough timeline.
        Squeeze mechanics COUNT as mechanism (short interest %, borrow fees, FTDs,
        utilization, gamma ramp) — a well-evidenced squeeze setup scores high here.
  4-7:  plausible idea, vague on timing or mechanism
  0-3:  pure hype, a question, or no thesis at all

community_conviction — how did the comments receive it?
  8-10: substantive validation, informed agreement, people adding supporting data
  4-7:  mixed reception
  0-3:  thesis ACTIVELY torn apart, debunked, or mocked by substantive comments
  If there are few or no substantive comments yet (fresh posts often have none —
  automod notes and jokes don't count), output exactly 5.0. Absence of reaction
  is neutral, never negative.

news_catalyst — do the supplied headlines (or structural drivers) support a move?
  8-10: fresh, directly relevant headline catalyst — or the supplied market data
        corroborates a structural setup (e.g. squeeze thesis + volume spike)
  4-7:  loosely related news, or NO news yet. ABSENCE of news is neutral (5.0),
        never negative — early plays usually have no headlines; that can be the point.
  0-3:  ONLY when supplied news or data actively contradicts the thesis
  If NO news data is supplied, output exactly 5.0.

technical_setup — does the supplied market context (prev-session price/volume, RSI,
30-day trend and range position, volume spike vs 30-day average, SMA50) fit the
STRATEGY the post is proposing? Judge fit-to-strategy, not absolute health:
  - Momentum/breakout thesis: wants aligned trend and volume.
  - Squeeze/reversal thesis: a washed-out chart (deep drawdown, oversold RSI,
    price at lows) is the PRECONDITION, not a contradiction — score it on squeeze
    evidence: volume picking up, oversold extremes, the post's SI/borrow data.
  8-10: the supplied data fits the proposed strategy well
  4-7:  neutral or ambiguous fit
  0-3:  data genuinely contradicts the strategy (e.g. bullish momentum thesis on a
        collapsing chart with no squeeze mechanics; chasing RSI 85 at the top of
        the range)
  If NO market data is supplied for the ticker you chose, output exactly 5.0.

plays_discussed — extract the concrete option plays from the post AND comments:
direction, structure (calls/puts/spreads/shares), strike as a number, and expiry
normalized to YYYY-MM-DD when determinable (current date is provided). Only include
plays actually discussed — do not invent your own.

red_flags — AT MOST 3, integrity disqualifiers only. Flag: evidence of coordinated
pumping (copy-paste shill comments, brand-new accounts pushing), fabricated-looking
numbers, or claims of fact the supplied data proves false. Do NOT flag anything a
sub-score already prices: trend/momentum mismatch belongs in technical_setup, weak
comments in community_conviction, missing news in news_catalyst. Never flag:
speculation, volatility, meme status, small caps, absence of news, no position
disclosed, or vague strikes. An ordinary risky post has ZERO red flags.

Rules:
- If no single clear ticker: ticker="N/A" and all scores 0.
- If the market data supplied is for a different ticker than the one you choose,
  ignore it and use the 5.0 neutral rule.
- Judge only from supplied material. Do not use outside knowledge of the ticker.
"""


def build_scoring_prompt(post: dict, comments: str, market_block: str | None,
                         stocktwits_block: str | None, today: str) -> str:
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
    return "\n\n".join(sections)


def _request_params(prompt: str) -> dict:
    return {
        "model": config.CLAUDE_MODEL,
        "max_tokens": 2500,
        "system": SCORING_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": _output_config(),
    }


def _score_sync(jobs: list[dict]) -> dict[str, TickerScore]:
    out: dict[str, TickerScore] = {}
    client = clients.anthropic_client()
    for job in jobs:
        try:
            message = client.messages.create(**_request_params(job["prompt"]))
        except anthropic.RateLimitError as e:
            log.warning("Anthropic rate limit on %s: %s", job["id"], e)
            continue
        except anthropic.APIStatusError as e:
            log.error("Anthropic API error (%s) on %s: %s", e.status_code, job["id"], e.message)
            continue
        except anthropic.APIConnectionError as e:
            log.error("Network error scoring %s: %s", job["id"], e)
            continue
        ts = _parse_score(message, job["id"])
        if ts:
            out[job["id"]] = ts
    return out


def _score_batch(jobs: list[dict]) -> dict[str, TickerScore]:
    """Score via the Message Batches API (50% cheaper). Raises on submission
    failure so the caller can fall back to sync."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    client = clients.anthropic_client()
    batch = client.messages.batches.create(requests=[
        Request(custom_id=job["id"], params=MessageCreateParamsNonStreaming(**_request_params(job["prompt"])))
        for job in jobs
    ])
    log.info("Submitted scoring batch %s (%d requests).", batch.id, len(jobs))

    deadline = time.monotonic() + config.BATCH_TIMEOUT_S
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        if time.monotonic() > deadline:
            log.error("Scoring batch %s timed out after %ds; canceling. "
                      "This run's unscored posts are skipped.", batch.id, config.BATCH_TIMEOUT_S)
            try:
                client.messages.batches.cancel(batch.id)
            except anthropic.APIError:
                pass
            return {}
        time.sleep(15)

    out: dict[str, TickerScore] = {}
    for result in client.messages.batches.results(batch.id):
        if result.result.type == "succeeded":
            ts = _parse_score(result.result.message, result.custom_id)
            if ts:
                out[result.custom_id] = ts
        else:
            log.warning("Batch item %s: %s", result.custom_id, result.result.type)
    log.info("Batch %s complete: %d/%d scored.", batch.id, len(out), len(jobs))
    return out


def score_many(jobs: list[dict]) -> dict[str, TickerScore]:
    """Score jobs [{"id": ..., "prompt": ...}] -> {id: TickerScore}.

    Uses the Batches API when enabled and there's more than one job; falls
    back to synchronous scoring if batch submission fails.
    """
    if not jobs:
        return {}
    if config.BATCH_SCORING and len(jobs) > 1:
        try:
            return _score_batch(jobs)
        except anthropic.APIError as e:
            log.warning("Batch submission failed (%s); falling back to sync scoring.", e)
    return _score_sync(jobs)


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
            model=config.CRAMER_MODEL,
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
