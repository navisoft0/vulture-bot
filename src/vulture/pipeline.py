"""The main scan pipeline: signals -> enrichment -> scoring -> gate -> notify.

Every scored candidate (posted or not) is logged to the Sheet with its
sub-scores; that log is the dataset for tuning scoring.WEIGHTS over time.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from . import analysis, clients, config, momentum, notify, scoring, sheets, state, store
from .analysis import TickerScore
from .signals import reddit, stocktwits

log = logging.getLogger(__name__)

# Column order matters: momentum.py reads this tab back by index.
SHEET_HEADER = [
    "post_id", "ticker", "composite", "thesis", "community", "news", "technical",
    "cross_platform", "posted", "briefing", "plays_json", "red_flags",
    "url", "subreddit", "post_created_utc", "scored_at_utc",
    "prior_mentions", "momentum_bonus",
]


@dataclass
class ScoredCandidate:
    post: dict
    score: TickerScore
    composite: float
    cross_platform: bool
    market_line: str | None
    prior_mentions: int = 0
    momentum_bonus: float = 0.0
    momentum_line: str | None = None
    radar: bool = False
    posted: bool = False

    @property
    def title(self) -> str:
        return self.post["title"][:250]


def _market_block(company: dict | None, bar: dict | None, rsi: float | None,
                  bars30: dict | None, sma50: float | None,
                  news: list[dict], market, snapshot: dict | None = None) -> str | None:
    """Compact market-context text for the Claude prompt."""
    if not company:
        return None
    lines = [f"Ticker: {company['ticker']} ({company.get('name') or 'unknown name'})"]
    if snapshot and snapshot.get("price"):
        move = (f" ({snapshot['day_change_pct']:+.1f}% vs prev close)"
                if snapshot.get("day_change_pct") is not None else "")
        vol = (f" · Vol so far {snapshot['day_volume'] / 1e6:.1f}M"
               if snapshot.get("day_volume") else "")
        lines.append(f"Today intraday (15-min delayed): ${snapshot['price']:,.2f}{move}{vol}")
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


def _stocktwits_block(stats: dict | None, trending: bool,
                      pulse: dict | None = None) -> str | None:
    """Prompt section from the routine snapshot (rich) or the scraper (coarse)."""
    parts = []
    if pulse:
        if pulse.get("sentiment_score") is not None:
            bull = (f", {pulse['bull_pct']:.0f}% of tagged messages bullish"
                    if pulse.get("bull_pct") is not None else "")
            parts.append(f"Sentiment score {pulse['sentiment_score']}/100 "
                         f"({pulse.get('sentiment_label') or '?'}){bull}.")
        if pulse.get("volume_score") is not None:
            parts.append(f"Message volume {pulse['volume_score']}/100 "
                         f"({pulse.get('volume_label') or '?'}).")
        if pulse.get("trending_rank"):
            watchers = (f" · {pulse['watchers']:,} watchers"
                        if pulse.get("watchers") else "")
            parts.append(f"Trending on Stocktwits at rank #{pulse['trending_rank']}{watchers}.")
        elif trending:
            parts.append("This ticker is currently on the Stocktwits trending list.")
        if pulse.get("driver"):
            parts.append(f"Why it's moving, per the Stocktwits stream: {pulse['driver']}")
    else:
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


def run_scan(trigger: str = "cron") -> None:
    config.validate_env("scan")
    scan_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    log.info("--- Vulture scan starting (%s, trigger=%s) ---", scan_id[:8], trigger)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    processed_store = state.processed_posts_store()
    processed_ids = processed_store.load()

    posts = reddit.scrape_new_posts(processed_ids)
    if not posts:
        log.info("Scan finished: no new posts.")
        return

    # Highest-engagement posts first; cap Claude spend per run.
    posts.sort(key=lambda p: (p["score"], p["num_comments"]), reverse=True)
    posts = posts[: config.MAX_POSTS_PER_SCAN]

    # Stocktwits: routine-written snapshot first (rich), scraping as fallback.
    # Trending is the union of both — the snapshot covers the top ranks with
    # rich data; the scraper list is longer and still feeds the flat bonus.
    st_snapshot: dict[str, dict] = {}
    trending: set[str] = set()
    if config.STOCKTWITS_ENABLED:
        st_snapshot = stocktwits.parse_snapshot(
            store.stocktwits_snapshot(), config.STOCKTWITS_SNAPSHOT_MAX_AGE_MIN)
        trending = ({t for t, e in st_snapshot.items() if e.get("trending_rank")}
                    | set(stocktwits.trending_symbols()))
    market = clients.market_client()
    st_stats_cache: dict[str, dict | None] = {}

    # Momentum history (prior runs, from the Sheet log) — needed in phase 1 so
    # scoring prompts can carry prior-mention briefings for cumulative analysis.
    history = momentum.load_history()

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
        snap = None
        if enriched_sym:
            bar = market.prev_day_bar(enriched_sym)
            rsi_val = market.rsi(enriched_sym)
            bars30 = market.bars_summary(enriched_sym)
            sma50 = market.sma(enriched_sym, window=50)
            news = market.recent_news(enriched_sym)
            snap = market.snapshot(enriched_sym)
            # Scrape per-symbol stats only for tickers the snapshot missed.
            if (config.STOCKTWITS_ENABLED and enriched_sym not in st_snapshot
                    and enriched_sym not in st_stats_cache):
                st_stats_cache[enriched_sym] = stocktwits.symbol_stats(enriched_sym)

        comments = reddit.get_comments(post["id"])
        hist = history.get(enriched_sym) if enriched_sym else None
        prompt = analysis.build_scoring_prompt(
            post, comments,
            market_block=_market_block(company, bar, rsi_val, bars30, sma50, news, market,
                                       snapshot=snap),
            stocktwits_block=_stocktwits_block(
                st_stats_cache.get(enriched_sym), enriched_sym in trending,
                pulse=st_snapshot.get(enriched_sym),
            ) if enriched_sym else None,
            today=today,
            prior_block=hist.prior_block() if hist else None,
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

    # Phase 3: post-process scores against the pre-run momentum history.
    scored: list[ScoredCandidate] = []
    for post_id, ts in results.items():
        if ts.ticker in ("N/A", ""):
            continue
        ctx = context[post_id]
        _check_contracts(ts, market)
        cross = ts.ticker in trending
        hist = history.get(ts.ticker)
        m_bonus, m_line = momentum.bonus(hist, ctx["post"]["subreddit"])
        comp = scoring.composite(ts, cross_platform=cross, momentum_bonus=m_bonus)
        scored.append(ScoredCandidate(
            post=ctx["post"], score=ts, composite=comp, cross_platform=cross,
            market_line=market.market_line(ctx["bar"], ctx["rsi"], ctx["bars30"])
            if ctx["enriched_sym"] == ts.ticker else None,
            prior_mentions=hist.count if hist else 0,
            momentum_bonus=m_bonus, momentum_line=m_line,
        ))
        log.info("Scored %s at %.2f (post %s%s).", ts.ticker, comp, post_id,
                 f", momentum +{m_bonus:g}" if m_bonus else "")

    # One post per ticker per run: keep the highest composite.
    best: dict[str, ScoredCandidate] = {}
    for cand in scored:
        if cand.score.ticker not in best or cand.composite > best[cand.score.ticker].composite:
            best[cand.score.ticker] = cand

    for cand in sorted(best.values(), key=lambda c: c.composite, reverse=True):
        main = cand.composite >= config.POST_THRESHOLD
        # Repeat radar: repeat-mention tickers surface even below the main
        # threshold — "worth the group's eyes", not a conviction call.
        radar = (not main
                 and cand.prior_mentions >= config.RADAR_MIN_MENTIONS
                 and cand.composite >= config.RADAR_FLOOR)
        if not (main or radar):
            continue
        if not momentum.repost_allowed(history.get(cand.score.ticker), cand.composite):
            log.info("Cooldown: %s (%.2f) already posted within %dh without a "
                     "+%.1f improvement; logging only.",
                     cand.score.ticker, cand.composite,
                     config.REPOST_COOLDOWN_H, config.REPOST_MARGIN)
            continue
        cand.radar = radar
        cand.posted = notify.post_play(cand)
        log.info("Posted %s (%.2f)%s to Discord: %s", cand.score.ticker,
                 cand.composite, " [radar]" if radar else "", cand.posted)

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
        c.prior_mentions, c.momentum_bonus,
    ] for c in scored]
    sheets.write_to_sheet(config.SHEET_SCORED_TAB, rows)

    # Dashboard dual-write (no-op unless DASHBOARD_API_URL is configured).
    store.emit_scan(
        {
            "id": scan_id, "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "posts_seen": len(posts), "scored": len(scored),
            "posted": int(sum(c.posted for c in best.values())), "trigger": trigger,
        },
        [{
            "post_id": c.post["id"], "ticker": c.score.ticker, "composite": c.composite,
            "thesis": c.score.thesis_quality, "community": c.score.community_conviction,
            "news": c.score.news_catalyst, "technical": c.score.technical_setup,
            "cross_platform": c.cross_platform, "prior_mentions": c.prior_mentions,
            "momentum_bonus": c.momentum_bonus, "radar": c.radar, "posted": c.posted,
            "briefing": c.score.briefing, "briefing_short": c.score.briefing_short,
            "red_flags": "; ".join(c.score.red_flags),
            "url": c.post["url"], "subreddit": c.post["subreddit"],
            "post_created_utc": c.post["created_utc"], "scored_at_utc": now,
            "plays": [p.model_dump() for p in c.score.plays_discussed],
        } for c in scored],
    )

    processed_store.add(newly_processed)
    log.info("--- Scan complete: %d posts processed, %d scored, %d posted ---",
             len(newly_processed), len(scored), sum(c.posted for c in best.values()))


def run_recheck(tickers: list[str]) -> None:
    """Member-requested re-scores: no new posts — prior briefings + fresh
    (15-min delayed) market data. Results land in the dashboard as new
    candidate rows (radar=1) so ticker cards update in place. Not written to
    the Sheet: rechecks must not inflate organic mention counts."""
    scan_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info("--- Re-check starting (%s): %s ---", scan_id[:8], ", ".join(tickers))

    market = clients.market_client()
    history = momentum.load_history()
    st_snapshot: dict[str, dict] = {}
    trending: set[str] = set()
    if config.STOCKTWITS_ENABLED:
        st_snapshot = stocktwits.parse_snapshot(
            store.stocktwits_snapshot(), config.STOCKTWITS_SNAPSHOT_MAX_AGE_MIN)
        trending = ({t for t, e in st_snapshot.items() if e.get("trending_rank")}
                    | set(stocktwits.trending_symbols()))

    jobs: list[dict] = []
    for raw in dict.fromkeys(t.upper() for t in tickers):
        company = market.validate_ticker(raw)
        if not company:
            log.warning("Re-check: %s did not validate; skipping.", raw)
            continue
        sym = company["ticker"]
        block = _market_block(
            company, market.prev_day_bar(sym), market.rsi(sym),
            market.bars_summary(sym), market.sma(sym, window=50),
            market.recent_news(sym), market, snapshot=market.snapshot(sym),
        )
        hist = history.get(sym)
        st_block = _stocktwits_block(
            stocktwits.symbol_stats(sym)
            if config.STOCKTWITS_ENABLED and sym not in st_snapshot else None,
            sym in trending, pulse=st_snapshot.get(sym))
        jobs.append({"id": sym, "prompt": analysis.build_recheck_prompt(
            sym, hist.prior_block() if hist else None, block, st_block, today)})

    if not jobs:
        log.info("Re-check: nothing to score.")
        return
    # Members are waiting on these — synchronous, never batched.
    results = analysis._score_sync(jobs, recheck=True)

    now = datetime.now(timezone.utc).isoformat()
    candidates = []
    for sym, ts in results.items():
        if ts.ticker in ("N/A", ""):
            continue
        hist = history.get(sym)
        comp = scoring.composite(ts, cross_platform=sym in trending,
                                 momentum_bonus=momentum.bonus(hist, "")[0] if hist else 0.0)
        candidates.append({
            "post_id": f"check-{scan_id[:8]}-{sym}", "ticker": sym, "composite": comp,
            "thesis": ts.thesis_quality, "community": ts.community_conviction,
            "news": ts.news_catalyst, "technical": ts.technical_setup,
            "cross_platform": sym in trending,
            "prior_mentions": hist.count if hist else 0,
            "momentum_bonus": 0, "radar": True, "posted": False,
            "briefing": ts.briefing, "briefing_short": ts.briefing_short,
            "red_flags": "; ".join(ts.red_flags),
            "url": "", "subreddit": "", "post_created_utc": now, "scored_at_utc": now,
            "plays": [],  # plays stay owned by the original posts
        })
        log.info("Re-checked %s at %.2f.", sym, comp)

    store.emit_scan(
        {"id": scan_id, "started_at": started_at,
         "finished_at": datetime.now(timezone.utc).isoformat(),
         "posts_seen": 0, "scored": len(candidates), "posted": 0, "trigger": "check"},
        candidates,
    )
    log.info("--- Re-check complete: %d/%d tickers scored ---", len(candidates), len(jobs))
