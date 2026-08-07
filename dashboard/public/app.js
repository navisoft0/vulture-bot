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

/* One card per ticker (per day) — `group` is every scored post for it,
   newest first. The highest-composite post leads; sources expand below. */
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

  const source = s => `<a href="${esc(s.url)}" target="_blank" rel="noopener">r/${esc(s.subreddit)} ↗</a>
      · <span class="num">${Number(s.composite).toFixed(1)}</span> · ${when(s.scored_at_utc)}`;
  const sources = group.length === 1
    ? `<div class="meta">scored ${when(lead.scored_at_utc)} · ${source(lead)}</div>`
    : `<details class="sources"><summary>${group.length} sources</summary>
        <ul>${byConviction.map(s => `<li>${source(s)}</li>`).join("")}</ul></details>`;

  // Short line: newest post that carries a one-liner (its briefing is the
  // running story); legacy rows fall back to the lead briefing's first sentence.
  const newest = group.find(c => c.briefing_short) || lead;
  const shortLine = newest.briefing_short
    || (lead.briefing || "").split(/(?<=[.!?])\s/)[0];
  const withText = byConviction.filter(c => c.briefing);
  const analysis = `<details class="sources analysis"><summary>full analysis</summary>
      <div class="analysis-body">${withText.map(c => `
        <div class="analysis-src">
          <div class="analysis-meta">${source(c)}</div>
          <p>${esc(c.briefing)}</p>
        </div>`).join("")}</div></details>`;

  const search = esc([lead.ticker, ...group.map(c => c.briefing), ...group.map(c => c.subreddit)]
    .join(" ").toLowerCase());
  return `<div class="card ${scoreClass(lead.composite)}" data-search="${search}">
    <div class="head">
      <span class="ticker">${esc(lead.ticker)}</span>
      <span class="score ${scoreClass(lead.composite)}">${Number(lead.composite).toFixed(1)}</span>
      ${badges.join(" ")}
    </div>
    <div class="subscores">
      <span>thesis <b>${lead.thesis}</b></span><span>community <b>${lead.community}</b></span>
      <span>news <b>${lead.news}</b></span><span>technicals <b>${lead.technical}</b></span>
    </div>
    <div class="brief">${esc(shortLine)}</div>
    ${plays.length ? `<ul class="plays">${plays.join("")}</ul>` : ""}
    ${flags ? `<div class="flags">${esc(flags)}</div>` : ""}
    ${withText.length ? analysis : ""}
    ${sources}
  </div>`;
}

/* Stagger the entrance animation of freshly rendered cards/tiles. */
function stagger() {
  document.querySelectorAll(".cards .card, .tiles .tile")
    .forEach((el, i) => el.style.setProperty("--i", Math.min(i, 12)));
}

const pages = {
  async today() {
    const all = new URLSearchParams(location.search).get("all") === "1";
    const data = await api(`/api/overview?days=14${all ? "&all=1" : ""}`);
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
    stagger();

    $("#search").addEventListener("input", e => {
      const q = e.target.value.trim().toLowerCase();
      for (const card of document.querySelectorAll(".card[data-search]"))
        card.style.display = !q || card.dataset.search.includes(q) ? "" : "none";
      for (const sec of document.querySelectorAll("section.day")) {
        const any = [...sec.querySelectorAll(".card")].some(c => c.style.display !== "none");
        sec.style.display = any ? "" : "none";
      }
    });
  },

  async tracker() {
    const d = await api("/api/tracker");
    const judged = d.totals.HIT + d.totals.MISS;
    $("#tiles").innerHTML = `
      <div class="tile"><div class="l">hits</div><div class="n ok">${d.totals.HIT}</div></div>
      <div class="tile"><div class="l">misses</div><div class="n bad">${d.totals.MISS}</div></div>
      <div class="tile"><div class="l">washes</div><div class="n mid">${d.totals.WASH}</div></div>
      <div class="tile"><div class="l">hit rate</div><div class="n">${judged ? Math.round(d.totals.HIT / judged * 100) + "%" : "—"}</div></div>`;
    $("#open").innerHTML = d.open.length ? d.open.map(p => `<tr>
      <td class="num"><b>${esc(p.ticker)}</b></td><td>${playLine(p)}</td>
      <td class="num">${esc(p.expiry || "open-ended")}</td><td>${when(p.promoted_at)}</td>
      <td><a href="${esc(p.url)}" target="_blank" rel="noopener">post ↗</a></td></tr>`).join("")
      : '<tr><td colspan="5" class="empty">No open tracked plays.</td></tr>';
    $("#resolved").innerHTML = d.resolved.length ? d.resolved.map(r => `<tr>
      <td class="num"><b>${esc(r.ticker)}</b></td><td>${playLine(r)}</td>
      <td><span class="vbadge ${r.verdict === "HIT" ? "ok" : r.verdict === "MISS" ? "bad" : "mid"}">${esc(r.verdict)}</span></td>
      <td class="num ${r.return_pct > 0 ? "ok" : r.return_pct < 0 ? "bad" : ""}">${fmtPct(r.return_pct)}</td>
      <td>${esc(r.method)}</td><td>${when(r.graded_at)}</td></tr>`).join("")
      : '<tr><td colspan="6" class="empty">Nothing resolved yet.</td></tr>';
    stagger();
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
