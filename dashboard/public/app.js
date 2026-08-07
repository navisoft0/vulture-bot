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

function candidateCard(c) {
  const badges = [];
  if (c.radar) badges.push('<span class="badge radar">radar</span>');
  if (c.prior_mentions > 0) badges.push(`<span class="badge momentum">×${c.prior_mentions + 1} mentions</span>`);
  if (c.cross_platform) badges.push('<span class="badge">stocktwits</span>');
  if (c.posted) badges.push('<span class="badge">posted</span>');
  const plays = (c.plays || []).map(p => `<li>${playLine(p)}</li>`).join("");
  const flags = c.red_flags ? `<div class="flags">${esc(c.red_flags)}</div>` : "";
  return `<div class="card ${scoreClass(c.composite)}">
    <div class="head">
      <span class="ticker">${esc(c.ticker)}</span>
      <span class="score ${scoreClass(c.composite)}">${Number(c.composite).toFixed(1)}</span>
      ${badges.join(" ")}
    </div>
    <div class="subscores">
      <span>thesis <b>${c.thesis}</b></span><span>community <b>${c.community}</b></span>
      <span>news <b>${c.news}</b></span><span>technicals <b>${c.technical}</b></span>
    </div>
    <div class="brief">${esc(c.briefing)}</div>
    ${plays ? `<ul class="plays">${plays}</ul>` : ""}
    ${flags}
    <div class="meta">r/${esc(c.subreddit)} · scored ${when(c.scored_at_utc)} ·
      <a href="${esc(c.url)}" target="_blank" rel="noopener">reddit post ↗</a></div>
  </div>`;
}

const pages = {
  async today() {
    const all = new URLSearchParams(location.search).get("all") === "1";
    const data = await api(`/api/today${all ? "?all=1" : ""}`);
    $("#scan-info").textContent = data.scan
      ? `Scan ${when(data.scan.started_at)} · ${data.scan.scored} scored · ${data.scan.posted} promoted · trigger: ${data.scan.trig || data.scan.trigger || "cron"}`
      : "No scans ingested yet.";
    $("#toggle-all").href = all ? "?" : "?all=1";
    $("#toggle-all").textContent = all ? "show vetted only" : "show everything scored";
    $("#cards").innerHTML = data.candidates.length
      ? data.candidates.map(candidateCard).join("")
      : '<div class="empty">Nothing here yet — vetted picks appear after the next scan.</div>';
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
  },

  async data() {
    const load = async () => {
      const q = new URLSearchParams();
      q.set("days", $("#f-days").value);
      if ($("#f-ticker").value) q.set("ticker", $("#f-ticker").value);
      if ($("#f-min").value) q.set("min_composite", $("#f-min").value);
      const d = await api(`/api/candidates?${q}`);
      $("#rows").innerHTML = d.candidates.length ? d.candidates.map(c => `<tr>
        <td>${when(c.scored_at_utc)}</td><td class="num"><b>${esc(c.ticker)}</b></td>
        <td class="num ${scoreClass(c.composite)}"><b>${Number(c.composite).toFixed(2)}</b></td>
        <td class="num">${c.thesis}/${c.community}/${c.news}/${c.technical}</td>
        <td>${c.posted ? '<span class="badge">posted</span>' : c.radar ? '<span class="badge radar">radar</span>' : ""}</td>
        <td>r/${esc(c.subreddit)}</td>
        <td>${esc((c.briefing || "").slice(0, 140))}${(c.briefing || "").length > 140 ? "…" : ""}</td>
        <td><a href="${esc(c.url)}" target="_blank" rel="noopener">↗</a></td></tr>`).join("")
        : '<tr><td colspan="8" class="empty">No rows for this filter.</td></tr>';
      $("#count").textContent = `${d.candidates.length} rows`;
    };
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
