/**
 * Vulture dashboard Worker: JSON API + static frontend + retention cron.
 *
 * Auth model:
 *  - Engine (Railway) -> ingest endpoints: Authorization: Bearer INGEST_TOKEN
 *  - Humans -> everything else: Cloudflare Access in front of this Worker;
 *    identity arrives as the Cf-Access-Authenticated-User-Email header.
 *    Admin = email listed in ADMIN_EMAILS. DEV_MODE=1 bypasses (local only).
 */

const JSON_HEADERS = { "content-type": "application/json" };

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function userEmail(request, env) {
  const email = request.headers.get("Cf-Access-Authenticated-User-Email");
  if (email) return email.toLowerCase();
  if (env.DEV_MODE === "1") return (env.ADMIN_EMAILS || "dev@local").split(",")[0].trim().toLowerCase();
  return null;
}

function isAdmin(email, env) {
  return (env.ADMIN_EMAILS || "").toLowerCase().split(",").map(s => s.trim()).includes(email);
}

function engineAuthorized(request, env) {
  const auth = request.headers.get("Authorization") || "";
  return env.INGEST_TOKEN && auth === `Bearer ${env.INGEST_TOKEN}`;
}

// ---------------------------------------------------------------------------
// Ingest (engine -> D1)
// ---------------------------------------------------------------------------

async function ingestScan(request, env) {
  const body = await request.json();
  const s = body.scan || {};
  await env.DB.prepare(
    `INSERT OR REPLACE INTO scans (id, started_at, finished_at, posts_seen, scored, posted, trig)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  ).bind(s.id, s.started_at, s.finished_at || null, s.posts_seen || 0,
         s.scored || 0, s.posted || 0, s.trigger || "cron").run();

  for (const c of body.candidates || []) {
    const res = await env.DB.prepare(
      `INSERT INTO candidates (scan_id, post_id, ticker, composite, thesis, community,
         news, technical, cross_platform, prior_mentions, momentum_bonus, radar, posted,
         briefing, briefing_short, red_flags, url, subreddit, post_created_utc, scored_at_utc,
         earnings_date, earnings_em, ma_w50, ma_w200, ma_4h50, ma_4h200)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`
    ).bind(s.id, c.post_id, c.ticker, c.composite, c.thesis, c.community, c.news,
           c.technical, c.cross_platform ? 1 : 0, c.prior_mentions || 0,
           c.momentum_bonus || 0, c.radar ? 1 : 0, c.posted ? 1 : 0,
           c.briefing || "", c.briefing_short || "", c.red_flags || "", c.url || "",
           c.subreddit || "", c.post_created_utc || "", c.scored_at_utc,
           c.earnings_date ?? null, c.earnings_em_pct ?? null,
           c.ma_w50 ?? null, c.ma_w200 ?? null, c.ma_4h50 ?? null, c.ma_4h200 ?? null).run();
    const candidateId = res.meta.last_row_id;
    for (const p of c.plays || []) {
      // One open promoted play per ticker+direction: if an ungraded promoted
      // play for this thesis already exists, keep the new one un-promoted so
      // duplicate mentions don't inflate the tracker/hit rate.
      let promoted = c.posted ? 1 : 0;
      if (promoted) {
        const dup = await env.DB.prepare(
          `SELECT p.id FROM plays p LEFT JOIN play_results r ON r.play_id = p.id
           WHERE p.promoted = 1 AND r.play_id IS NULL
             AND p.ticker = ? AND IFNULL(p.direction, '') = ? LIMIT 1`
        ).bind(c.ticker, p.direction || "").first();
        if (dup) promoted = 0;
      }
      await env.DB.prepare(
        `INSERT INTO plays (candidate_id, ticker, direction, structure, strike, expiry,
           rationale, promoted, promoted_at)
         VALUES (?,?,?,?,?,?,?,?,?)`
      ).bind(candidateId, c.ticker, p.direction || null, p.structure || null,
             p.strike ?? null, p.expiry || null, p.rationale || "",
             promoted, promoted ? c.scored_at_utc : null).run();
    }
  }
  return json({ ok: true });
}

async function ingestCramer(request, env) {
  const body = await request.json();
  for (const m of body.mentions || []) {
    await env.DB.prepare(
      `INSERT INTO cramer_mentions (extracted_at, ticker, stance, quote, source_url)
       VALUES (?,?,?,?,?)`
    ).bind(m.extracted_at, m.ticker, m.stance, m.quote || "", m.source_url || "").run();
  }
  return json({ ok: true });
}

async function ingestCramerVerdicts(request, env) {
  const body = await request.json();
  for (const v of body.verdicts || []) {
    // Match the mention by (extracted_at, ticker) as the engine keys them.
    const m = await env.DB.prepare(
      `SELECT id FROM cramer_mentions WHERE extracted_at = ? AND ticker = ?
       ORDER BY id LIMIT 1`
    ).bind(v.mention_at, v.ticker).first();
    if (!m) continue;
    await env.DB.prepare(
      `INSERT OR REPLACE INTO cramer_verdicts (mention_id, baseline_date, baseline_close,
         eval_date, eval_close, stock_return_pct, spy_return_pct, alpha_pct, verdict)
       VALUES (?,?,?,?,?,?,?,?,?)`
    ).bind(m.id, v.baseline_date, v.baseline_close, v.eval_date, v.eval_close,
           v.stock_return_pct, v.spy_return_pct, v.alpha_pct, v.verdict).run();
  }
  return json({ ok: true });
}

// ---------------------------------------------------------------------------
// Stocktwits snapshot (Claude routine -> D1 -> engine)
// ---------------------------------------------------------------------------

async function ingestStocktwits(request, env) {
  const body = await request.json();
  if (!Array.isArray(body.symbols)) {
    return json({ error: "symbols[] required" }, 400);
  }
  // Server clock is authoritative: the routine's model-written fetched_at
  // has drifted hours off (bad timezone arithmetic), which corrupts both
  // the strip's "updated" label and the engine's freshness check.
  body.fetched_at = new Date().toISOString();
  await env.DB.prepare(
    `INSERT OR REPLACE INTO stocktwits_snapshot (id, fetched_at, payload) VALUES (1, ?, ?)`
  ).bind(body.fetched_at, JSON.stringify(body)).run();
  return json({ ok: true, symbols: body.symbols.length });
}

async function apiStocktwitsSnapshot(env) {
  const row = await env.DB.prepare(
    `SELECT payload FROM stocktwits_snapshot WHERE id = 1`).first();
  if (!row) return json({ fetched_at: null, symbols: [] });
  return new Response(row.payload, { headers: JSON_HEADERS });
}

async function apiStocktwitsTargets(env) {
  // Tickers vulture scored recently: the routine enriches these in its next
  // dump so repeat mentions get snapshot data, not just trending symbols.
  const cutoff = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
  const { results } = await env.DB.prepare(
    `SELECT DISTINCT ticker FROM candidates WHERE scored_at_utc >= ?
     ORDER BY ticker LIMIT 25`
  ).bind(cutoff).all();
  return json({ tickers: results.map(r => r.ticker) });
}

async function playsDue(env) {
  const today = new Date().toISOString().slice(0, 10);
  const { results } = await env.DB.prepare(
    `SELECT p.id, p.ticker, p.direction, p.structure, p.strike, p.expiry, p.promoted_at
     FROM plays p LEFT JOIN play_results r ON r.play_id = p.id
     WHERE p.promoted = 1 AND r.play_id IS NULL
       AND p.expiry IS NOT NULL AND p.expiry <= ?
     ORDER BY p.expiry LIMIT 50`
  ).bind(today).all();
  return json({ plays: results });
}

async function ingestPlayResults(request, env) {
  const body = await request.json();
  for (const r of body.results || []) {
    await env.DB.prepare(
      `INSERT OR REPLACE INTO play_results (play_id, graded_at, method, entry_underlying,
         exit_underlying, entry_contract, exit_contract, return_pct, verdict)
       VALUES (?,?,?,?,?,?,?,?,?)`
    ).bind(r.play_id, r.graded_at, r.method, r.entry_underlying ?? null,
           r.exit_underlying ?? null, r.entry_contract ?? null, r.exit_contract ?? null,
           r.return_pct ?? null, r.verdict).run();
  }
  return json({ ok: true });
}

// ---------------------------------------------------------------------------
// Read API (members)
// ---------------------------------------------------------------------------

async function apiToday(env, url) {
  const all = url.searchParams.get("all") === "1";
  const scan = await env.DB.prepare(
    `SELECT * FROM scans ORDER BY started_at DESC LIMIT 1`).first();
  if (!scan) return json({ scan: null, candidates: [] });
  const filter = all ? "" : "AND (c.posted = 1 OR c.radar = 1)";
  const { results: candidates } = await env.DB.prepare(
    `SELECT c.* FROM candidates c WHERE c.scan_id = ? ${filter}
     ORDER BY c.composite DESC LIMIT 100`).bind(scan.id).all();
  for (const c of candidates) {
    const { results: plays } = await env.DB.prepare(
      `SELECT * FROM plays WHERE candidate_id = ?`).bind(c.id).all();
    c.plays = plays;
  }
  return json({ scan, candidates });
}

async function apiOverview(env, url, email) {
  const days = Math.min(parseInt(url.searchParams.get("days") || "14", 10), 30);
  const all = url.searchParams.get("all") === "1";
  const since = new Date(Date.now() - days * 864e5).toISOString();
  const scan = await env.DB.prepare(
    `SELECT * FROM scans ORDER BY started_at DESC LIMIT 1`).first();
  const filter = all ? "" : "AND (posted = 1 OR radar = 1)";
  const { results: candidates } = await env.DB.prepare(
    `SELECT * FROM candidates WHERE scored_at_utc >= ? ${filter}
     ORDER BY scored_at_utc DESC LIMIT 400`).bind(since).all();
  const { results: follows } = await env.DB.prepare(
    `SELECT ticker FROM follows WHERE email = ?`).bind(email).all();
  const { results: pendingChecks } = await env.DB.prepare(
    `SELECT ticker, MIN(requested_at) requested_at FROM check_requests
     WHERE picked_up_at IS NULL GROUP BY ticker`).all();
  // Batch-attach plays (chunked: D1 caps bound params per statement).
  const byCand = {};
  const ids = candidates.map(c => c.id);
  for (let i = 0; i < ids.length; i += 90) {
    const chunk = ids.slice(i, i + 90);
    const { results: plays } = await env.DB.prepare(
      `SELECT * FROM plays WHERE candidate_id IN (${chunk.map(() => "?").join(",")})`
    ).bind(...chunk).all();
    for (const p of plays) (byCand[p.candidate_id] ??= []).push(p);
  }
  for (const c of candidates) c.plays = byCand[c.id] || [];
  // Stocktwits trending strip: top-ranked snapshot symbols (routine-written).
  let stocktwits = null;
  const snapRow = await env.DB.prepare(
    `SELECT payload FROM stocktwits_snapshot WHERE id = 1`).first();
  if (snapRow) {
    try {
      const snap = JSON.parse(snapRow.payload);
      const ranked = (snap.symbols || []).filter(s => s.trending_rank != null)
        .sort((a, b) => a.trending_rank - b.trending_rank);
      stocktwits = { fetched_at: snap.fetched_at, symbols: ranked.slice(0, 6) };
    } catch { /* malformed snapshot: strip just doesn't render */ }
  }
  return json({
    scan, candidates, stocktwits,
    follows: follows.map(f => f.ticker),
    checks_pending: Object.fromEntries(pendingChecks.map(p => [p.ticker, p.requested_at])),
    check_batch_minutes: parseInt(env.CHECK_BATCH_WAIT_MIN || "10", 10),
  });
}

/* Live-ish market strip for feed cards: Massive snapshot + RSI per ticker,
   proxied so the key stays server-side, edge-cached ~2 min. 15-min delayed
   data per the Stocks Starter plan. */
async function apiQuotes(env, url) {
  if (!env.MASSIVE_API_KEY) return json({ quotes: {} });
  const tickers = [...new Set((url.searchParams.get("tickers") || "")
    .split(",").map(t => t.trim().toUpperCase()).filter(t => /^[A-Z.]{1,10}$/.test(t)))].slice(0, 60);
  const auth = { Authorization: `Bearer ${env.MASSIVE_API_KEY}` };
  const quotes = {};
  await Promise.all(tickers.map(async t => {
    const cacheKey = new Request(`https://internal.quote/${t}`);
    let hit = await caches.default.match(cacheKey);
    if (!hit) {
      const [snap, rsi] = await Promise.all([
        fetch(`https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/${t}`,
          { headers: auth }).then(r => r.ok ? r.json() : null).catch(() => null),
        fetch(`https://api.massive.com/v1/indicators/rsi/${t}?timespan=day&window=14&series_type=close&limit=1`,
          { headers: auth }).then(r => r.ok ? r.json() : null).catch(() => null),
      ]);
      const k = snap?.ticker || {};
      const q = {
        price: k.min?.c ?? k.day?.c ?? null,
        change_pct: k.todaysChangePerc != null ? Math.round(k.todaysChangePerc * 100) / 100 : null,
        volume: k.day?.v ?? null,
        prev_close: k.prevDay?.c ?? null,
        rsi: rsi?.results?.values?.[0]?.value != null
          ? Math.round(rsi.results.values[0].value) : null,
      };
      hit = new Response(JSON.stringify(q),
        { headers: { ...JSON_HEADERS, "cache-control": "public, max-age=120" } });
      await caches.default.put(cacheKey, hit.clone());
    }
    quotes[t] = await hit.json();
  }));
  return json({ quotes });
}

async function apiFollow(request, env, email) {
  const body = await request.json();
  const ticker = String(body.ticker || "").toUpperCase().slice(0, 10);
  if (!ticker) return json({ error: "ticker required" }, 400);
  if (body.follow) {
    await env.DB.prepare(
      `INSERT OR REPLACE INTO follows (email, ticker, created_at) VALUES (?, ?, ?)`
    ).bind(email, ticker, new Date().toISOString()).run();
  } else {
    await env.DB.prepare(`DELETE FROM follows WHERE email = ? AND ticker = ?`)
      .bind(email, ticker).run();
  }
  return json({ ok: true, ticker, following: !!body.follow });
}

async function apiCheckEnqueue(request, env, email) {
  const body = await request.json();
  const ticker = String(body.ticker || "").toUpperCase().slice(0, 10);
  if (!ticker) return json({ error: "ticker required" }, 400);
  const existing = await env.DB.prepare(
    `SELECT id FROM check_requests WHERE ticker = ? AND picked_up_at IS NULL LIMIT 1`
  ).bind(ticker).first();
  if (!existing) {
    await env.DB.prepare(
      `INSERT INTO check_requests (ticker, requested_by, requested_at) VALUES (?, ?, ?)`
    ).bind(ticker, email, new Date().toISOString()).run();
  }
  const { results } = await env.DB.prepare(
    `SELECT COUNT(DISTINCT ticker) n FROM check_requests WHERE picked_up_at IS NULL`).all();
  return json({
    ok: true, ticker, queued: true, already: !!existing,
    pending: results[0].n,
    batch_minutes: parseInt(env.CHECK_BATCH_WAIT_MIN || "10", 10),
  });
}

/* Engine polls this. Batching policy lives here: release (and claim) the
   queue only once the oldest request has aged past CHECK_BATCH_WAIT_MIN or
   the queue has CHECK_BATCH_MAX distinct tickers. */
async function apiChecksDue(env) {
  const waitMin = parseInt(env.CHECK_BATCH_WAIT_MIN || "10", 10);
  const maxSize = parseInt(env.CHECK_BATCH_MAX || "5", 10);
  const { results: pending } = await env.DB.prepare(
    `SELECT id, ticker, requested_at FROM check_requests WHERE picked_up_at IS NULL`).all();
  if (!pending.length) return json({ tickers: [] });
  const tickers = [...new Set(pending.map(p => p.ticker))];
  const oldestMs = Math.min(...pending.map(p => Date.parse(p.requested_at)));
  const ageMin = (Date.now() - oldestMs) / 60000;
  if (ageMin < waitMin && tickers.length < maxSize) {
    return json({ tickers: [], pending: tickers.length, oldest_age_min: Math.round(ageMin) });
  }
  const nowIso = new Date().toISOString();
  for (let i = 0; i < pending.length; i += 90) {
    const chunk = pending.slice(i, i + 90);
    await env.DB.prepare(
      `UPDATE check_requests SET picked_up_at = ? WHERE id IN (${chunk.map(() => "?").join(",")})`
    ).bind(nowIso, ...chunk.map(p => p.id)).run();
  }
  return json({ tickers });
}

async function apiCandidates(env, url) {
  const days = Math.min(parseInt(url.searchParams.get("days") || "7", 10), 90);
  const ticker = (url.searchParams.get("ticker") || "").toUpperCase();
  const minComposite = parseFloat(url.searchParams.get("min_composite") || "0");
  const since = new Date(Date.now() - days * 864e5).toISOString();
  let q = `SELECT * FROM candidates WHERE scored_at_utc >= ? AND composite >= ?`;
  const binds = [since, minComposite];
  if (ticker) { q += ` AND ticker = ?`; binds.push(ticker); }
  q += ` ORDER BY scored_at_utc DESC LIMIT 500`;
  const { results } = await env.DB.prepare(q).bind(...binds).all();
  return json({ candidates: results });
}

async function apiTracker(env, email) {
  const today = new Date().toISOString().slice(0, 10);
  const { results: open } = await env.DB.prepare(
    `SELECT p.*, c.composite, c.url FROM plays p
     JOIN candidates c ON c.id = p.candidate_id
     LEFT JOIN play_results r ON r.play_id = p.id
     WHERE p.promoted = 1 AND r.play_id IS NULL
       AND (p.expiry IS NULL OR p.expiry > ?)
     ORDER BY p.expiry IS NULL, p.expiry LIMIT 100`).bind(today).all();
  const { results: resolved } = await env.DB.prepare(
    `SELECT p.ticker, p.direction, p.structure, p.strike, p.expiry, p.promoted_at,
            r.* FROM play_results r JOIN plays p ON p.id = r.play_id
     ORDER BY r.graded_at DESC LIMIT 100`).all();
  const { results: sums } = await env.DB.prepare(
    `SELECT verdict, COUNT(*) n FROM play_results GROUP BY verdict`).all();
  const { results: archived } = await env.DB.prepare(
    `SELECT key, value FROM summary_totals WHERE key LIKE 'plays_%'`).all();
  const totals = { HIT: 0, MISS: 0, WASH: 0, UNGRADEABLE: 0 };
  for (const s of sums) totals[s.verdict] = (totals[s.verdict] || 0) + s.n;
  for (const a of archived) {
    const k = a.key.replace("plays_", "");
    totals[k] = (totals[k] || 0) + a.value;
  }
  const { results: follows } = await env.DB.prepare(
    `SELECT ticker FROM follows WHERE email = ?`).bind(email).all();
  return json({ open, resolved, totals, follows: follows.map(f => f.ticker) });
}

async function apiCramer(env) {
  const { results: mentions } = await env.DB.prepare(
    `SELECT m.*, v.verdict, v.stock_return_pct, v.spy_return_pct, v.alpha_pct
     FROM cramer_mentions m LEFT JOIN cramer_verdicts v ON v.mention_id = m.id
     ORDER BY m.extracted_at DESC LIMIT 200`).all();
  const { results: sums } = await env.DB.prepare(
    `SELECT verdict, COUNT(*) n FROM cramer_verdicts GROUP BY verdict`).all();
  const { results: archived } = await env.DB.prepare(
    `SELECT key, value FROM summary_totals WHERE key LIKE 'cramer_%'`).all();
  const totals = { HIT: 0, MISS: 0, WASH: 0 };
  for (const s of sums) totals[s.verdict] = (totals[s.verdict] || 0) + s.n;
  for (const a of archived) {
    const k = a.key.replace("cramer_", "");
    totals[k] = (totals[k] || 0) + a.value;
  }
  return json({ mentions, totals });
}

// ---------------------------------------------------------------------------
// Admin
// ---------------------------------------------------------------------------

async function adminRun(env, email) {
  if (env.RUN_URL && env.RUN_TOKEN) {
    try {
      const resp = await fetch(env.RUN_URL, {
        method: "POST",
        headers: { Authorization: `Bearer ${env.RUN_TOKEN}` },
      });
      if (resp.ok) return json({ ok: true, via: "engine", detail: await resp.text() });
      return json({ ok: false, via: "engine", status: resp.status }, 502);
    } catch (e) {
      // fall through to queue
    }
  }
  await env.DB.prepare(
    `INSERT INTO runs_requested (requested_by, requested_at) VALUES (?, ?)`
  ).bind(email, new Date().toISOString()).run();
  return json({ ok: true, via: "queue", note: "engine will pick this up on its next loop" });
}

// ---------------------------------------------------------------------------
// Retention: archive rows older than RETENTION_DAYS to R2, then delete.
// Open plays (and their parent candidates) survive until graded.
// ---------------------------------------------------------------------------

function toCsv(rows) {
  if (!rows.length) return "";
  const cols = Object.keys(rows[0]);
  const esc = v => `"${String(v ?? "").replaceAll('"', '""')}"`;
  return [cols.join(","), ...rows.map(r => cols.map(c => esc(r[c])).join(","))].join("\n");
}

async function runRetention(env) {
  const days = parseInt(env.RETENTION_DAYS || "90", 10);
  const cutoff = new Date(Date.now() - days * 864e5).toISOString();
  const stamp = new Date().toISOString().slice(0, 7); // YYYY-MM
  const archived = {};

  // Candidates old enough AND with no open play attached.
  const { results: oldCandidates } = await env.DB.prepare(
    `SELECT c.* FROM candidates c
     WHERE c.scored_at_utc < ?
       AND NOT EXISTS (
         SELECT 1 FROM plays p LEFT JOIN play_results r ON r.play_id = p.id
         WHERE p.candidate_id = c.id AND p.promoted = 1 AND r.play_id IS NULL)`
  ).bind(cutoff).all();
  const candidateIds = oldCandidates.map(c => c.id);

  const { results: oldPlays } = candidateIds.length
    ? await env.DB.prepare(
        `SELECT p.*, r.method, r.return_pct, r.verdict, r.graded_at
         FROM plays p LEFT JOIN play_results r ON r.play_id = p.id
         WHERE p.candidate_id IN (${candidateIds.map(() => "?").join(",")})`
      ).bind(...candidateIds).all()
    : { results: [] };

  const { results: oldCramer } = await env.DB.prepare(
    `SELECT m.*, v.verdict, v.alpha_pct, v.stock_return_pct
     FROM cramer_mentions m LEFT JOIN cramer_verdicts v ON v.mention_id = m.id
     WHERE m.extracted_at < ?
       AND (v.verdict IS NOT NULL OR m.stance IN ('hold', 'unclear'))`
  ).bind(cutoff).all();

  if (env.ARCHIVE) {
    for (const [name, rows] of [["candidates", oldCandidates], ["plays", oldPlays],
                                 ["cramer", oldCramer]]) {
      if (rows.length) {
        archived[name] = rows.length;
        await env.ARCHIVE.put(`${stamp}/${name}-${stamp}.csv`, toCsv(rows),
          { httpMetadata: { contentType: "text/csv" } });
      }
    }
  }

  // Roll verdict counts into never-purged summaries.
  for (const p of oldPlays) {
    if (p.verdict) {
      await env.DB.prepare(
        `INSERT INTO summary_totals (key, value) VALUES (?, 1)
         ON CONFLICT(key) DO UPDATE SET value = value + 1`
      ).bind(`plays_${p.verdict}`).run();
    }
  }
  for (const c of oldCramer) {
    if (c.verdict) {
      await env.DB.prepare(
        `INSERT INTO summary_totals (key, value) VALUES (?, 1)
         ON CONFLICT(key) DO UPDATE SET value = value + 1`
      ).bind(`cramer_${c.verdict}`).run();
    }
  }

  // Delete archived rows (children first).
  if (candidateIds.length) {
    const ph = candidateIds.map(() => "?").join(",");
    await env.DB.prepare(
      `DELETE FROM play_results WHERE play_id IN
        (SELECT id FROM plays WHERE candidate_id IN (${ph}))`).bind(...candidateIds).run();
    await env.DB.prepare(
      `DELETE FROM plays WHERE candidate_id IN (${ph})`).bind(...candidateIds).run();
    await env.DB.prepare(
      `DELETE FROM candidates WHERE id IN (${ph})`).bind(...candidateIds).run();
  }
  const cramerIds = oldCramer.map(c => c.id);
  if (cramerIds.length) {
    const ph = cramerIds.map(() => "?").join(",");
    await env.DB.prepare(
      `DELETE FROM cramer_verdicts WHERE mention_id IN (${ph})`).bind(...cramerIds).run();
    await env.DB.prepare(
      `DELETE FROM cramer_mentions WHERE id IN (${ph})`).bind(...cramerIds).run();
  }
  await env.DB.prepare(`DELETE FROM scans WHERE started_at < ?
    AND id NOT IN (SELECT DISTINCT scan_id FROM candidates WHERE scan_id IS NOT NULL)`)
    .bind(cutoff).run();

  if (env.DISCORD_ARCHIVE_WEBHOOK && Object.keys(archived).length) {
    const lines = Object.entries(archived).map(([k, n]) => `${k}: ${n} rows`).join(" · ");
    await fetch(env.DISCORD_ARCHIVE_WEBHOOK, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({
        embeds: [{
          title: "🗄️ Vulture archive",
          description: `Rows older than ${days} days exported to R2 (\`${stamp}/\`) and pruned.\n${lines}`,
          color: 0x7f8c8d,
        }],
      }),
    });
  }
  return archived;
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (!path.startsWith("/api/")) {
      return env.ASSETS.fetch(request);
    }

    // Engine endpoints (token auth)
    const enginePaths = {
      "POST /api/ingest/scan": ingestScan,
      "POST /api/ingest/cramer": ingestCramer,
      "POST /api/ingest/cramer_verdicts": ingestCramerVerdicts,
      "POST /api/ingest/play_results": ingestPlayResults,
      "POST /api/ingest/stocktwits": ingestStocktwits,
    };
    // NOTE: engine GET routes live under /api/ingest/* so the Cloudflare
    // Access bypass (which covers that prefix) applies without new policies.
    const engineGets = {
      "GET /api/plays/due": () => playsDue(env),
      "GET /api/ingest/checks_due": () => apiChecksDue(env),
      "GET /api/ingest/stocktwits": () => apiStocktwitsSnapshot(env),
      "GET /api/ingest/st_targets": () => apiStocktwitsTargets(env),
    };
    const engineKey = `${request.method} ${path}`;
    if (enginePaths[engineKey] || engineGets[engineKey] || engineKey === "GET /api/runs/pending") {
      if (!engineAuthorized(request, env)) return json({ error: "unauthorized" }, 401);
      if (engineGets[engineKey]) return engineGets[engineKey]();
      if (engineKey === "GET /api/runs/pending") {
        const row = await env.DB.prepare(
          `SELECT id FROM runs_requested WHERE picked_up_at IS NULL ORDER BY id LIMIT 1`).first();
        if (row) {
          await env.DB.prepare(`UPDATE runs_requested SET picked_up_at = ? WHERE id = ?`)
            .bind(new Date().toISOString(), row.id).run();
        }
        return json({ pending: !!row });
      }
      return enginePaths[engineKey](request, env);
    }

    // Human endpoints (Cloudflare Access)
    const email = userEmail(request, env);
    if (!email) return json({ error: "no identity — is Cloudflare Access configured?" }, 401);
    const admin = isAdmin(email, env);

    if (path === "/api/me") return json({ email, admin });
    if (path === "/api/today") return apiToday(env, url);
    if (path === "/api/overview") return apiOverview(env, url, email);
    if (path === "/api/market/quotes") return apiQuotes(env, url);
    if (path === "/api/follow" && request.method === "POST") return apiFollow(request, env, email);
    if (path === "/api/check" && request.method === "POST") return apiCheckEnqueue(request, env, email);
    if (path === "/api/candidates") return apiCandidates(env, url);
    if (path === "/api/tracker") return apiTracker(env, email);
    if (path === "/api/cramer") return apiCramer(env);

    if (path.startsWith("/api/admin/")) {
      if (!admin) return json({ error: "admin only" }, 403);
      if (path === "/api/admin/run" && request.method === "POST") return adminRun(env, email);
      if (path === "/api/admin/export" && request.method === "POST") {
        const archived = await runRetention(env);
        return json({ ok: true, archived });
      }
    }
    return json({ error: "not found" }, 404);
  },

  async scheduled(_event, env, ctx) {
    ctx.waitUntil(runRetention(env));
  },
};
