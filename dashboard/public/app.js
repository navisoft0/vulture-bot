/* Vulture dashboard frontend. One file; renderer picked by <body data-page>. */

const $ = (sel, el = document) => el.querySelector(sel);

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtPct = v => v == null ? "—" : `${v > 0 ? "+" : ""}${Number(v).toFixed(1)}%`;
const scoreClass = c => c >= 7.5 ? "hi" : c >= 6 ? "med" : "low";
const when = iso => iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

async function initNav() {
  try {
    const me = await api("/api/me");
    $("#me").textContent = me.email + (me.admin ? " · admin" : "");
    if (me.admin) $("#nav-admin").style.display = "";
  } catch { $("#me").textContent = "not signed in"; }
  const page = document.body.dataset.page;
  const link = $(`#nav-${page}`);
  if (link) link.classList.add("active");
}

function playLine(p) {
  const arrow = p.direction === "bullish" ? '<span class="dir-up">▲</span>'
    : p.direction === "bearish" ? '<span class="dir-down">▼</span>' : '<span class="dir-flat">–</span>';
  const bits = [p.structure, p.strike != null ? `$${p.strike}` : null, p.expiry].filter(Boolean);
  return `${arrow} <span class="num">${esc(bits.join(" · "))}</span>${p.rationale ? " — " + esc(p.rationale) : ""}`;
}

/* Feed page state shared between renderer and event handlers. */
const feedState = { follows: new Set(), checksPending: {}, batchMin: 10 };

const isCheckRow = c => (c.post_id || "").startsWith("check-");

/* One card per ticker (per day) — `group` is every scored post for it,
   newest first. Front face = quick read; back face = full analysis. */
function tickerCard(group) {
  const lead = group.slice().sort((a, b) => b.composite - a.composite)[0];
  const badges = [];
  if (group.some(c => c.radar) && !group.some(c => c.posted)) badges.push('<span class="badge radar">radar</span>');
  if (lead.prior_mentions > 0) badges.push(`<span class="badge momentum">×${lead.prior_mentions + 1} mentions</span>`);
  if (group.some(c => c.cross_platform)) badges.push('<span class="badge">stocktwits</span>');
  if (group.some(c => c.posted)) badges.push('<span class="badge">posted</span>');

  const seen = new Set();
  const plays = [];
  const byConviction = group.slice().sort((a, b) => b.composite - a.composite);
  for (const c of byConviction) for (const p of c.plays || []) {
    const key = `${p.direction}|${p.structure}|${p.strike}|${p.expiry}`;
    if (seen.has(key)) continue;
    seen.add(key);
    plays.push(`<li>${playLine(p)}</li>`);
  }
  const flags = [...new Set(group.map(c => c.red_flags).filter(Boolean))].join(" · ");

  const source = s => isCheckRow(s)
    ? `<span class="recheck-tag">⟳ re-check</span>
       · <span class="num">${Number(s.composite).toFixed(1)}</span> · ${when(s.scored_at_utc)}`
    : `<a href="${esc(s.url)}" target="_blank" rel="noopener">r/${esc(s.subreddit)} ↗</a>
       · <span class="num">${Number(s.composite).toFixed(1)}</span> · ${when(s.scored_at_utc)}`;

  // Short line: newest row that carries a one-liner (its briefing is the
  // running story); legacy rows fall back to the lead briefing's first sentence.
  const newest = group.find(c => c.briefing_short) || lead;
  const shortLine = newest.briefing_short
    || (lead.briefing || "").split(/(?<=[.!?])\s/)[0];
  // Full analysis (back face): the newest cumulative briefing leads, then
  // every contributing source with its own debrief, newest first.
  const newestBrief = group.find(c => c.briefing) || lead;
  const rest = group.filter(c => c !== newestBrief && c.briefing);

  const T = esc(lead.ticker);
  const following = feedState.follows.has(lead.ticker);
  const pendingAt = feedState.checksPending[lead.ticker];
  const star = `<button class="star ${following ? "on" : ""}" data-ticker="${T}"
    aria-pressed="${following}" title="${following ? "unfollow" : "follow"}">${following ? "★" : "☆"}</button>`;

  const search = esc([lead.ticker, ...group.map(c => c.briefing), ...group.map(c => c.subreddit)]
    .join(" ").toLowerCase());
  const scoreCls = scoreClass(lead.composite);
  const scorePill = `<span class="score ${scoreCls}">${Number(lead.composite).toFixed(1)}</span>`;

  return `<div class="flip" data-search="${search}" data-ticker="${T}">
   <div class="flip-inner">
    <div class="face card ${scoreCls}">
      <div class="head">
        <span class="ticker">${T}</span>${scorePill}${badges.join(" ")}${star}
      </div>
      <div class="mkt" data-mkt="${T}">
        <span class="mkt-price">·&thinsp;·&thinsp;·</span><span class="mkt-chg"></span>
        <span class="mkt-side"></span>
      </div>
      <div class="brief">${esc(shortLine)}</div>
      <div class="meta">${when(lead.scored_at_utc)}${group.length > 1
        ? ` · ${group.length} sources` : ""}<span class="flip-hint">tap for analysis ⟲</span></div>
    </div>
    <div class="face back card ${scoreCls}">
      <div class="head">
        <span class="ticker">${T}</span>${scorePill}
        <span class="back-label">full analysis</span>${star}
      </div>
      <div class="subscores">
        <span>thesis <b>${lead.thesis}</b></span><span>community <b>${lead.community}</b></span>
        <span>news <b>${lead.news}</b></span><span>technicals <b>${lead.technical}</b></span>
      </div>
      ${newestBrief.briefing ? `<div class="brief">${esc(newestBrief.briefing)}</div>
        <div class="analysis-meta">${source(newestBrief)}</div>` : ""}
      ${plays.length ? `<ul class="plays">${plays.join("")}</ul>` : ""}
      ${flags ? `<div class="flags">${esc(flags)}</div>` : ""}
      ${rest.length ? `<div class="analysis-body">${rest.map(c => `
        <div class="analysis-src">
          <div class="analysis-meta">${source(c)}</div>
          <p>${esc(c.briefing)}</p>
        </div>`).join("")}</div>` : ""}
      <div class="actions">
        <button class="primary check-btn" data-ticker="${T}" ${pendingAt ? "disabled" : ""}>
          ${pendingAt ? "⟳ queued" : "⟳ Check"}</button>
        <span class="check-status">${pendingAt
          ? `re-check queued · batches ~${feedState.batchMin}m` : ""}</span>
        <span class="flip-hint">tap to flip back ⟲</span>
      </div>
    </div>
   </div>
  </div>`;
}

const fmtVol = v => v == null ? null : v >= 1e9 ? (v / 1e9).toFixed(1) + "B"
  : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : Math.round(v / 1e3) + "K";

/* Fill every card's market strip from the quotes proxy (15-min delayed). */
async function loadQuotes() {
  const strips = [...document.querySelectorAll("[data-mkt]")];
  const tickers = [...new Set(strips.map(el => el.dataset.mkt))];
  if (!tickers.length) return;
  let quotes = {};
  try { quotes = (await api(`/api/market/quotes?tickers=${tickers.join(",")}`)).quotes || {}; }
  catch { /* market strip is progressive enhancement */ }
  for (const el of strips) {
    const q = quotes[el.dataset.mkt];
    if (!q || q.price == null) { el.remove(); continue; }
    el.querySelector(".mkt-price").textContent = `$${Number(q.price).toLocaleString([], { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    const chg = el.querySelector(".mkt-chg");
    if (q.change_pct != null) {
      chg.textContent = `${q.change_pct > 0 ? "▲" : q.change_pct < 0 ? "▼" : ""} ${fmtPct(q.change_pct)}`;
      chg.className = `mkt-chg ${q.change_pct > 0 ? "ok" : q.change_pct < 0 ? "bad" : ""}`;
    }
    const side = [q.rsi != null ? `RSI ${q.rsi}` : null,
                  fmtVol(q.volume) ? `VOL ${fmtVol(q.volume)}` : null].filter(Boolean).join(" · ");
    el.querySelector(".mkt-side").textContent = side;
  }
}

/* Stagger the entrance animation of freshly rendered cards/tiles. */
function stagger() {
  document.querySelectorAll(".cards > *, .tiles .tile")
    .forEach((el, i) => el.style.setProperty("--i", Math.min(i, 12)));
}

/* Chips for followed tickers; clicking one filters the feed to it. */
function renderFollowStrip() {
  const el = $("#follow-strip");
  if (!el) return;
  const list = [...feedState.follows].sort();
  el.innerHTML = list.length
    ? `<span class="strip-label">following</span>` + list.map(t =>
        `<button class="chip" data-ticker="${esc(t)}">★ ${esc(t)}</button>`).join("")
    : "";
  el.style.display = list.length ? "" : "none";
}

const pages = {
  async today() {
    const all = new URLSearchParams(location.search).get("all") === "1";
    const data = await api(`/api/overview?days=14${all ? "&all=1" : ""}`);
    feedState.follows = new Set(data.follows || []);
    feedState.checksPending = data.checks_pending || {};
    feedState.batchMin = data.check_batch_minutes || 10;
    $("#scan-info").textContent = data.scan
      ? `Last scan ${when(data.scan.started_at)} · ${data.scan.scored} scored · ${data.scan.posted} promoted · trigger: ${data.scan.trig || data.scan.trigger || "cron"}`
      : "No scans ingested yet.";
    $("#toggle-all").href = all ? "?" : "?all=1";
    $("#toggle-all").textContent = all ? "show vetted only" : "show everything scored";

    // Segment by day, group by ticker within each day, best composite first.
    const todayKey = new Date().toISOString().slice(0, 10);
    const yesterdayKey = new Date(Date.now() - 864e5).toISOString().slice(0, 10);
    const dayLabel = k => k === todayKey ? "Today" : k === yesterdayKey ? "Yesterday"
      : new Date(k + "T12:00:00Z").toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });

    const days = new Map();
    for (const c of data.candidates) {
      const k = (c.scored_at_utc || "").slice(0, 10);
      if (!days.has(k)) days.set(k, new Map());
      const tickers = days.get(k);
      if (!tickers.has(c.ticker)) tickers.set(c.ticker, []);
      tickers.get(c.ticker).push(c);
    }
    const sections = [];
    for (const [k, tickers] of days) {
      const groups = [...tickers.values()]
        .sort((a, b) => Math.max(...b.map(c => c.composite)) - Math.max(...a.map(c => c.composite)));
      sections.push(`<section class="day">
        <h2 class="sec">${dayLabel(k)}</h2>
        <div class="cards">${groups.map(tickerCard).join("")}</div>
      </section>`);
    }
    $("#sections").innerHTML = sections.length ? sections.join("")
      : '<div class="empty">Nothing here yet — vetted picks appear after the next scan.</div>';
    renderFollowStrip();
    stagger();
    loadQuotes();

    $("#search").addEventListener("input", e => {
      const q = e.target.value.trim().toLowerCase();
      for (const card of document.querySelectorAll(".flip[data-search]"))
        card.style.display = !q || card.dataset.search.includes(q) ? "" : "none";
      for (const sec of document.querySelectorAll("section.day")) {
        const any = [...sec.querySelectorAll(".flip")].some(c => c.style.display !== "none");
        sec.style.display = any ? "" : "none";
      }
    });

    $("#follow-strip").addEventListener("click", e => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      const inp = $("#search");
      inp.value = inp.value === chip.dataset.ticker.toLowerCase() ? "" : chip.dataset.ticker.toLowerCase();
      inp.dispatchEvent(new Event("input"));
    });

    // One delegated handler: stars, check buttons, and card flips.
    $("#sections").addEventListener("click", async e => {
      const star = e.target.closest(".star");
      if (star) {
        const t = star.dataset.ticker;
        const on = !feedState.follows.has(t);
        on ? feedState.follows.add(t) : feedState.follows.delete(t);
        for (const s of document.querySelectorAll(`.star[data-ticker="${t}"]`)) {
          s.classList.toggle("on", on); s.textContent = on ? "★" : "☆";
          s.setAttribute("aria-pressed", on);
        }
        renderFollowStrip();
        try { await fetch("/api/follow", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ ticker: t, follow: on }) }); } catch {}
        return;
      }
      const check = e.target.closest(".check-btn");
      if (check) {
        const t = check.dataset.ticker;
        check.disabled = true; check.textContent = "⟳ queued";
        try {
          const r = await fetch("/api/check", { method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ ticker: t }) });
          const d = await r.json();
          feedState.checksPending[t] = new Date().toISOString();
          check.closest(".actions").querySelector(".check-status").textContent =
            `re-check queued (${d.pending} in batch) · batches ~${d.batch_minutes}m`;
        } catch {
          check.disabled = false; check.textContent = "⟳ Check";
        }
        return;
      }
      if (e.target.closest("a, button, details, summary, input")) return;
      const flip = e.target.closest(".flip");
      if (flip) {
        // Animate container height alongside the rotation so short fronts
        // grow into tall analyses (and back) without dead space.
        const toBack = !flip.classList.contains("flipped");
        const target = flip.querySelector(toBack ? ".face.back" : ".face:not(.back)");
        flip.style.height = flip.offsetHeight + "px";
        flip.classList.toggle("flipped");
        requestAnimationFrame(() => { flip.style.height = target.offsetHeight + "px"; });
        flip.addEventListener("transitionend", function clear(ev) {
          if (ev.propertyName !== "height") return;
          if (!flip.classList.contains("flipped")) flip.style.height = "";
          flip.removeEventListener("transitionend", clear);
        });
      }
    });
  },

  async tracker() {
    const d = await api("/api/tracker");
    const follows = new Set(d.follows || []);
    const render = () => {
      const mine = $("#mine-only")?.checked;
      const keep = rows => mine ? rows.filter(r => follows.has(r.ticker)) : rows;
      const open = keep(d.open), resolved = keep(d.resolved);
      const totals = mine
        ? resolved.reduce((t, r) => { t[r.verdict] = (t[r.verdict] || 0) + 1; return t; },
            { HIT: 0, MISS: 0, WASH: 0 })
        : d.totals;
      const judged = (totals.HIT || 0) + (totals.MISS || 0);
      $("#tiles").innerHTML = `
        <div class="tile"><div class="l">hits</div><div class="n ok">${totals.HIT || 0}</div></div>
        <div class="tile"><div class="l">misses</div><div class="n bad">${totals.MISS || 0}</div></div>
        <div class="tile"><div class="l">washes</div><div class="n mid">${totals.WASH || 0}</div></div>
        <div class="tile"><div class="l">hit rate</div><div class="n">${judged ? Math.round((totals.HIT || 0) / judged * 100) + "%" : "—"}</div></div>`;
      $("#open").innerHTML = open.length ? open.map(p => `<tr>
        <td class="num">${follows.has(p.ticker) ? "★ " : ""}<b>${esc(p.ticker)}</b></td><td>${playLine(p)}</td>
        <td class="num">${esc(p.expiry || "open-ended")}</td><td>${when(p.promoted_at)}</td>
        <td><a href="${esc(p.url)}" target="_blank" rel="noopener">post ↗</a></td></tr>`).join("")
        : `<tr><td colspan="5" class="empty">${mine ? "None of your followed tickers have open plays." : "No open tracked plays."}</td></tr>`;
      $("#resolved").innerHTML = resolved.length ? resolved.map(r => `<tr>
        <td class="num">${follows.has(r.ticker) ? "★ " : ""}<b>${esc(r.ticker)}</b></td><td>${playLine(r)}</td>
        <td><span class="vbadge ${r.verdict === "HIT" ? "ok" : r.verdict === "MISS" ? "bad" : "mid"}">${esc(r.verdict)}</span></td>
        <td class="num ${r.return_pct > 0 ? "ok" : r.return_pct < 0 ? "bad" : ""}">${fmtPct(r.return_pct)}</td>
        <td>${esc(r.method)}</td><td>${when(r.graded_at)}</td></tr>`).join("")
        : `<tr><td colspan="6" class="empty">${mine ? "Nothing resolved for your followed tickers." : "Nothing resolved yet."}</td></tr>`;
      stagger();
    };
    $("#mine-only")?.addEventListener("change", render);
    render();
  },

  async cramer() {
    const d = await api("/api/cramer");
    const judged = d.totals.HIT + d.totals.MISS;
    $("#tiles").innerHTML = `
      <div class="tile"><div class="l">hits</div><div class="n ok">${d.totals.HIT}</div></div>
      <div class="tile"><div class="l">misses (inverse)</div><div class="n bad">${d.totals.MISS}</div></div>
      <div class="tile"><div class="l">washes</div><div class="n mid">${d.totals.WASH}</div></div>
      <div class="tile"><div class="l">hit rate</div><div class="n">${judged ? Math.round(d.totals.HIT / judged * 100) + "%" : "—"}</div></div>`;
    const stanceDot = s => ({ buy: "ok", sell: "bad", avoid: "bad", trim: "mid", hold: "" }[s] ?? "");
    $("#mentions").innerHTML = d.mentions.length ? d.mentions.map(m => `<tr>
      <td>${when(m.extracted_at)}</td><td class="num"><b>${esc(m.ticker)}</b></td>
      <td><span class="${stanceDot(m.stance)}">●</span> ${esc(m.stance)}</td>
      <td>${m.verdict ? `<span class="vbadge ${m.verdict === "HIT" ? "ok" : m.verdict === "MISS" ? "bad" : "mid"}">${m.verdict}</span> <span class="num">α ${fmtPct(m.alpha_pct)}</span>` : "open"}</td>
      <td>${esc(m.quote)}</td></tr>`).join("")
      : '<tr><td colspan="5" class="empty">No mentions yet.</td></tr>';
    stagger();
  },

  async data() {
    const load = async () => {
      const q = new URLSearchParams();
      q.set("days", $("#f-days").value);
      if ($("#f-ticker").value) q.set("ticker", $("#f-ticker").value);
      if ($("#f-min").value) q.set("min_composite", $("#f-min").value);
      const d = await api(`/api/candidates?${q}`);
      const row = (c, extra = "", cls = "") => `<tr class="${cls}">
        <td>${when(c.scored_at_utc)}</td>
        <td class="num">${cls ? "" : '<span class="chev"></span>'}<b>${esc(c.ticker)}</b>${extra}</td>
        <td class="num ${scoreClass(c.composite)}"><b>${Number(c.composite).toFixed(2)}</b></td>
        <td class="num">${c.thesis}/${c.community}/${c.news}/${c.technical}</td>
        <td>${c.posted ? '<span class="badge">posted</span>' : c.radar ? '<span class="badge radar">radar</span>' : ""}</td>
        <td>r/${esc(c.subreddit)}</td>
        <td>${esc((c.briefing || "").slice(0, 140))}${(c.briefing || "").length > 140 ? "…" : ""}</td>
        <td><a href="${esc(c.url)}" target="_blank" rel="noopener">↗</a></td></tr>`;

      // One surfaced row per ticker (latest post); older posts expand below it.
      const groups = new Map();
      for (const c of d.candidates) {
        if (!groups.has(c.ticker)) groups.set(c.ticker, []);
        groups.get(c.ticker).push(c);
      }
      let html = "";
      for (const rows of groups.values()) {
        if (rows.length === 1) { html += row(rows[0]); continue; }
        html += row(rows[0], ` <span class="badge momentum">×${rows.length}</span>`, "ghead");
        html += rows.slice(1).map(c => row(c, "", "gchild")).join("");
      }
      $("#rows").innerHTML = html || '<tr><td colspan="8" class="empty">No rows for this filter.</td></tr>';
      $("#count").textContent = `${groups.size} tickers · ${d.candidates.length} rows`;
    };
    $("#rows").addEventListener("click", e => {
      if (e.target.closest("a")) return;
      const head = e.target.closest("tr.ghead");
      if (!head) return;
      const open = head.classList.toggle("open");
      for (let el = head.nextElementSibling; el && el.classList.contains("gchild"); el = el.nextElementSibling)
        el.classList.toggle("shown", open);
    });
    $("#apply").addEventListener("click", load);
    await load();
  },

  async admin() {
    const btn = $("#run-btn"), status = $("#run-status");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      status.textContent = "requesting…";
      try {
        const r = await fetch("/api/admin/run", { method: "POST" });
        const d = await r.json();
        status.textContent = d.ok
          ? (d.via === "engine" ? "✅ engine triggered — scan starting now"
                                : "🕐 queued — engine picks it up within its next loop")
          : `❌ failed (${JSON.stringify(d)})`;
      } catch (e) { status.textContent = `❌ ${e}`; }
      btn.disabled = false;
    });
    $("#export-btn").addEventListener("click", async () => {
      $("#export-status").textContent = "running…";
      try {
        const r = await fetch("/api/admin/export", { method: "POST" });
        const d = await r.json();
        $("#export-status").textContent = d.ok
          ? `✅ archived: ${JSON.stringify(d.archived)}` : `❌ ${JSON.stringify(d)}`;
      } catch (e) { $("#export-status").textContent = `❌ ${e}`; }
    });
  },
};

initNav().then(() => {
  const page = document.body.dataset.page;
  if (pages[page]) pages[page]().catch(e => {
    const main = $("main");
    main.insertAdjacentHTML("afterbegin",
      `<div class="notice">Failed to load: ${esc(e.message)}</div>`);
  });
});
