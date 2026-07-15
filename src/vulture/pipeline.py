"""The main scan pipeline: signals -> enrichment -> scoring -> gate -> notify.

Every scored candidate (posted or not) is logged to the Sheet with its
sub-scores; that log is the dataset for tuning scoring.WEIGHTS over time.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from . import analysis, clients, config, notify, scoring, sheets, state
from .analysis import TickerScore
from .signals import reddit, stocktwits

log = logging.getLogger(__name__)

SHEET_HEADER = [
    "post_id", "ticker", "composite", "thesis", "community", "news", "technical",
    "cross_platform", "posted", "briefing", "plays_json", "red_flags",
    "url", "subreddit", "post_created_utc", "scored_at_utc",
]


@dataclass
class ScoredCandidate:
    post: dict
    score: TickerScore
    composite: float
    cross_platform: bool
    market_line: str | None
    posted: bool = False

    @property
    def title(self) -> str:
        return self.post["title"][:250]


def _market_block(company: dict | None, bar: dict | None, rsi: float | None,
                  bars30: dict | None, sma50: float | None,
                  news: list[dict], market) -> str | None:
    """Compact market-context text for the Claude prompt."""
    if not company:
        return None
    lines = [f"Ticker: {company['ticker']} ({company.get('name') or 'unknown name'})"]
    line = market.market_line(bar, rsi)
    if line:
        lines.append(f"Previous session: {line}")
    if bars30:
        parts = [f"trend {bars30['trend_pct']:+.1f}% over {bars30['sessions']} sessions"
                 if bars30.get("trend_pct") is not None else None,
                 f"price at {bars30['range_position_pct']}% of the 30-day range"
                 if bars30.get("range_position_pct") is not None else None,
                 f"prev-session volume {bars30['volume_spike_x']}x the 30-day average"
                 if bars30.get("volume_spike_x") is not None else None]
        parts = [p for p in parts if p]
        if parts:
            lines.append("30-day context: " + "; ".join(parts))
    if sma50 is not None and bar and bar.get("close") is not None:
        rel = "above" if bar["close"] >= sma50 else "below"
        lines.append(f"SMA50: ${sma50:,.2f} (price {rel})")
    if news:
        lines.append("Recent headlines (last 48h, hourly feed):")
        for a in news:
            tag = f" [{a['sentiment']}]" if a.get("sentiment") else ""
            lines.append(f"- {a['title']} ({a['publisher']}){tag}")
    else:
        lines.append("Recent headlines: none in the last 48h")
    return "\n".join(lines)


def _stocktwits_block(stats: dict | None, trending: bool) -> str | None:
    if stats is None and not trending:
        return None
    parts = []
    if trending:
        parts.append("This ticker is currently on the Stocktwits trending list.")
    if stats and stats["messages"]:
        parts.append(
            f"Last {stats['messages']} Stocktwits messages: "
            f"{stats['bullish']} tagged Bullish, {stats['bearish']} tagged Bearish."
        )
    return "\n".join(parts) or None


def _infer_contract_type(structure: str) -> str | None:
    s = structure.lower()
    if "put" in s:
        return "put"
    if "call" in s:
        return "call"
    return None


def _check_contracts(ts: TickerScore, market) -> None:
    """Red-flag discussed strikes/expiries that don't exist as listed contracts."""
    checked = 0
    for play in ts.plays_discussed:
        if checked >= 2:
            break
        ctype = _infer_contract_type(play.structure)
        if not (ctype and play.strike and play.expiry):
            continue
        checked += 1
        exists = market.contract_exists(ts.ticker, ctype, play.strike, play.expiry)
        if exists is False:
            ts.red_flags.append(
                f"Discussed contract not found among listed options: "
                f"{ts.ticker} ${play.strike:g} {ctype} exp {play.expiry}"
            )


def run_scan() -> None:
    config.validate_env("scan")
    log.info("--- Vulture scan starting ---")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    store = state.processed_posts_store()
    processed_ids = store.load()

    posts = reddit.scrape_new_posts(processed_ids)
    if not posts:
        log.info("Scan finished: no new posts.")
        return

    # Highest-engagement posts first; cap Claude spend per run.
    posts.sort(key=lambda p: (p["score"], p["num_comments"]), reverse=True)
    posts = posts[: config.MAX_POSTS_PER_SCAN]

    trending = set(stocktwits.trending_symbols()) if config.STOCKTWITS_ENABLED else set()
    market = clients.market_client()
    st_stats_cache: dict[str, dict | None] = {}

    newly_processed: list[str] = []

    # Phase 1: enrich and build scoring jobs (Reddit + Massive bound).
    jobs: list[dict] = []
    context: dict[str, dict] = {}  # post_id -> {post, enriched_sym, bar, rsi}
    for post in posts:
        newly_processed.append(post["id"])

        candidates = reddit.extract_candidate_tickers(f"{post['title']} {post['selftext']}")
        if not candidates:
            log.debug("Post %s: no ticker candidates, skipping.", post["id"])
            continue

        # Validate candidates against Massive; enrich the first real one.
        company = bar = rsi_val = bars30 = sma50 = None
        news: list[dict] = []
        enriched_sym = None
        for sym in candidates:
            company = market.validate_ticker(sym)
            if company:
                enriched_sym = company["ticker"]
                break
        if enriched_sym:
            bar = market.prev_day_bar(enriched_sym)
            rsi_val = market.rsi(enriched_sym)
            bars30 = market.bars_summary(enriched_sym)
            sma50 = market.sma(enriched_sym, window=50)
            news = market.recent_news(enriched_sym)
            if config.STOCKTWITS_ENABLED and enriched_sym not in st_stats_cache:
                st_stats_cache[enriched_sym] = stocktwits.symbol_stats(enriched_sym)

        comments = reddit.get_comments(post["id"])
        prompt = analysis.build_scoring_prompt(
            post, comments,
            market_block=_market_block(company, bar, rsi_val, bars30, sma50, news, market),
            stocktwits_block=_stocktwits_block(
                st_stats_cache.get(enriched_sym), enriched_sym in trending
            ) if enriched_sym else None,
            today=today,
        )
        jobs.append({"id": post["id"], "prompt": prompt})
        context[post["id"]] = {
            "post": post, "enriched_sym": enriched_sym, "bar": bar, "rsi": rsi_val,
            "bars30": bars30,
        }

    enriched_count = sum(1 for c in context.values() if c["enriched_sym"])
    log.info(
        "Enrichment done: %d/%d posts had ticker candidates; %d validated against "
        "Massive (the rest score without market data; junk candidates pruned).",
        len(jobs), len(posts), enriched_count,
    )

    # Phase 2: score (Batches API when enabled — 50% cheaper; sync fallback).
    results = analysis.score_many(jobs)

    # Phase 3: post-process scores.
    scored: list[ScoredCandidate] = []
    for post_id, ts in results.items():
        if ts.ticker in ("N/A", ""):
            continue
        ctx = context[post_id]
        _check_contracts(ts, market)
        cross = ts.ticker in trending
        comp = scoring.composite(ts, cross_platform=cross)
        scored.append(ScoredCandidate(
            post=ctx["post"], score=ts, composite=comp, cross_platform=cross,
            market_line=market.market_line(ctx["bar"], ctx["rsi"], ctx["bars30"])
            if ctx["enriched_sym"] == ts.ticker else None,
        ))
        log.info("Scored %s at %.2f (post %s).", ts.ticker, comp, post_id)

    # One post per ticker per run: keep the highest composite.
    best: dict[str, ScoredCandidate] = {}
    for cand in scored:
        if cand.score.ticker not in best or cand.composite > best[cand.score.ticker].composite:
            best[cand.score.ticker] = cand

    for cand in sorted(best.values(), key=lambda c: c.composite, reverse=True):
        if cand.composite >= config.POST_THRESHOLD:
            cand.posted = notify.post_play(cand)
            log.info("Posted %s (%.2f) to Discord: %s",
                     cand.score.ticker, cand.composite, cand.posted)

    # Log every scored candidate for rubric tuning.
    now = datetime.now(timezone.utc).isoformat()
    rows = [[
        c.post["id"], c.score.ticker, c.composite,
        c.score.thesis_quality, c.score.community_conviction,
        c.score.news_catalyst, c.score.technical_setup,
        c.cross_platform, c.posted, c.score.briefing,
        json.dumps([p.model_dump() for p in c.score.plays_discussed]),
        "; ".join(c.score.red_flags),
        c.post["url"], c.post["subreddit"], c.post["created_utc"], now,
    ] for c in scored]
    sheets.write_to_sheet(config.SHEET_SCORED_TAB, rows)

    store.add(newly_processed)
    log.info("--- Scan complete: %d posts processed, %d scored, %d posted ---",
             len(newly_processed), len(scored), sum(c.posted for c in best.values()))
