"""Discord notification layer.

One posting path for every message: timeout, retry with backoff honoring
Discord's Retry-After on 429, exponential backoff on 5xx. Generalized from
the v1 webhook functions.
"""

import logging
import time
from datetime import datetime, timezone

import requests

from . import config

log = logging.getLogger(__name__)

_TIMEOUT = 15


def send_embed(webhook_url, embed, *, thread_name=None, applied_tags=None, retries=3):
    """Post an embed to a Discord webhook. Returns True on success.

    thread_name/applied_tags are for forum-channel webhooks (creates a thread).
    """
    if not webhook_url:
        log.warning("Discord webhook not configured; skipping post '%s'.", embed.get("title"))
        return False

    payload = {"embeds": [embed]}
    url = webhook_url
    if thread_name:
        payload["thread_name"] = thread_name
        payload["applied_tags"] = [t for t in (applied_tags or []) if t]
        url = f"{webhook_url}?wait=true"

    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=_TIMEOUT)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("Discord rate limited; retrying in %.1fs.", retry_after)
                time.sleep(min(retry_after, 30))
                continue
            if resp.status_code >= 500:
                log.warning("Discord %s; retrying.", resp.status_code)
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            log.warning("Discord post failed (attempt %d/%d): %s", attempt + 1, retries, e)
            time.sleep(2 ** attempt)
    log.error("Giving up posting to Discord: %s", embed.get("title"))
    return False


def _style(composite: float):
    """composite -> (forum_tag_id, color, emoji)."""
    if composite >= config.HIGH_TAG_THRESHOLD:
        return config.get("DISCORD_TAG_ID_HIGH"), 0x00C775, "🚀"
    if composite >= config.MEDIUM_TAG_THRESHOLD:
        return config.get("DISCORD_TAG_ID_MEDIUM"), 0xFFFF00, "🤔"
    return config.get("DISCORD_TAG_ID_LOW"), 0xFF8C00, "👀"


def _fmt_play(play) -> str:
    arrow = {"bullish": "📈", "bearish": "📉"}.get(play.direction, "➖")
    bits = [play.structure]
    if play.strike is not None:
        bits.append(f"${play.strike:g}")
    if play.expiry:
        bits.append(play.expiry)
    return f"{arrow} {' · '.join(bits)} — {play.rationale}"


def post_play(record) -> bool:
    """Post a scored candidate to the forum webhook. `record` is a ScoredCandidate."""
    ts = record.score
    tag_id, color, emoji = _style(record.composite)

    fields = []
    if ts.plays_discussed:
        fields.append({
            "name": "The Plays",
            "value": "\n".join(_fmt_play(p) for p in ts.plays_discussed[:5])[:1024],
            "inline": False,
        })
    breakdown = (
        f"Thesis {ts.thesis_quality:.0f} · Community {ts.community_conviction:.0f} · "
        f"News {ts.news_catalyst:.0f} · Technicals {ts.technical_setup:.0f}"
        + (" · 🔁 Trending on Stocktwits" if record.cross_platform else "")
    )
    if getattr(record, "momentum_line", None):
        breakdown += f"\n{record.momentum_line}"
    fields.append({"name": "Score Breakdown", "value": breakdown, "inline": False})
    if record.market_line:
        fields.append({"name": "Market Context (prev session)", "value": record.market_line, "inline": False})
    if ts.red_flags:
        fields.append({
            "name": "⚠️ Red Flags",
            "value": "\n".join(f"- {f}" for f in ts.red_flags[:5])[:1024],
            "inline": False,
        })
    sources = [f"[Reddit post]({record.post['url']})"]
    if record.cross_platform:
        sources.append(f"[Stocktwits](https://stocktwits.com/symbol/{ts.ticker})")
    fields.append({"name": "Source", "value": f"r/{record.post['subreddit']} · " + " · ".join(sources), "inline": False})

    embed = {
        "title": record.title,
        "description": ts.briefing[:4096],
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Vulture · not financial advice · prev-session data"},
    }
    thread_name = f"{ts.ticker} | {record.composite:.1f} | {emoji}"
    return send_embed(
        config.get("DISCORD_WEBHOOK_FORUM"), embed,
        thread_name=thread_name, applied_tags=[tag_id] if tag_id else [],
    )


def post_cramer_digest(mentions, article_urls) -> bool:
    """Post a Cramer Watch digest to the news webhook. `mentions`: list[CramerMention]."""
    if not mentions:
        return True
    stance_emoji = {"buy": "🟢", "sell": "🔴", "trim": "🟠", "avoid": "🔴", "hold": "⚪", "unclear": "❔"}
    lines = [
        f"{stance_emoji.get(m.stance, '❔')} **{m.ticker}** — {m.stance.upper()}: \"{m.quote[:150]}\""
        for m in mentions[:20]
    ]
    embed = {
        "title": "🎪 Cramer Watch",
        "description": "\n".join(lines)[:4096],
        "color": 0x4A90E2,
        "fields": [{
            "name": "Sources",
            "value": "\n".join(f"[{u.split('/')[-2][:60]}]({u})" for u in article_urls[:5])[:1024],
            "inline": False,
        }],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Extracted from CNBC Mad Money recaps · not financial advice"},
    }
    return send_embed(config.get("DISCORD_WEBHOOK_NEWS"), embed)
