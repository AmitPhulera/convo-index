// Analytics page. Shared helpers (esc, fmtTokens, fmtUsd, toast, theme) live in common.js.

// rows: [[label, count], ...]; linkFn(label) -> search URL (or null for non-clickable).
function bars(rows, linkFn, fmt) {
  if (!rows || !rows.length) return `<p class="hint">Nothing recorded yet.</p>`;
  const max = Math.max(1, ...rows.map(r => r[1]));
  return rows.map(([label, n]) => {
    const href = linkFn ? linkFn(label) : null;
    const text = esc(label);
    const labelEl = href
      ? `<a class="bar-label" href="${esc(href)}" title="${text}">${text}</a>`
      : `<span class="bar-label" title="${text}">${text}</span>`;
    return `<div class="bar-row">
        ${labelEl}
        <div class="bar-track"><div class="bar-fill" style="width:${(n / max) * 100}%"></div></div>
        <span class="bar-count">${fmt ? fmt(n) : n}</span>
      </div>`;
  }).join("");
}

// "≥ $x (72% priced)" when some requests have no rate (docs/design.md §10).
function costCell(a) {
  if (!a || a.coverage === null || a.coverage === undefined) return "—";
  const est = fmtUsd(a.estimated_cost);
  const partial = a.coverage < 1 ? `≥ ${est} <span class="hint">(${Math.round(a.coverage * 100)}% priced)</span>` : est;
  const rep = a.reported_cost !== null && a.reported_cost !== undefined ? ` <span class="hint">· reported ${fmtUsd(a.reported_cost)}</span>` : "";
  return partial + rep;
}

// rows: [[label, summary], ...]
function usageTable(rows, linkFn, firstHead) {
  if (!rows || !rows.length) return '<p class="hint">No usage recorded for this breakdown.</p>';
  const body = rows.map(([label, a]) => {
    const href = linkFn ? linkFn(label) : null;
    const l = href ? `<a href="${esc(href)}" title="${esc(label)}">${esc(label)}</a>` : esc(label);
    return `<tr><td class="lbl">${l}</td><td>${fmtTokens(a.tokens)}</td>
      <td>${fmtTokens(a.input_uncached)}</td><td>${fmtTokens(a.cache_read)}</td>
      <td>${fmtTokens(a.cache_write)}</td><td>${fmtTokens(a.output)}</td><td>${fmtInt(a.requests)}</td><td>${costCell(a)}</td></tr>`;
  }).join("");
  return `<table class="usage-table"><thead><tr><th>${esc(firstHead || "")}</th><th>tokens</th><th>input</th><th>cache read</th>
    <th>cache write</th><th>output</th><th>requests</th><th>est. cost</th></tr></thead><tbody>${body}</tbody></table>`;
}

function renderKpis(d) {
  const u = (d.usage || {}).totals || {};
  const convos = (d.by_source || []).reduce((n, r) => n + r[1], 0);
  const months = (d.by_month || []).length;
  const busiest = (d.by_month || []).slice().sort((a, b) => b[1] - a[1])[0];
  const cards = [
    { k: "Conversations", v: fmtInt(convos), s: months ? `across ${months} month${months === 1 ? "" : "s"}` : "" },
    { k: "Tokens", v: fmtTokens(u.tokens || 0), s: u.requests ? `${fmtInt(u.requests)} requests` : "no token data recorded" },
    { k: "Estimated cost", v: u.coverage === null || u.coverage === undefined ? "—" : (u.coverage < 1 ? "≥ " : "") + fmtUsd(u.estimated_cost), s: "API-equivalent, at list prices" },
    { k: "Busiest month", v: busiest ? busiest[0] : "—", s: busiest ? `${fmtInt(busiest[1])} conversations` : "" },
  ];
  document.getElementById("kpis").innerHTML = cards.map(c =>
    `<div class="kpi"><div class="k">${esc(c.k)}</div><div class="v">${c.v}</div><div class="s">${esc(c.s)}</div></div>`).join("");
}

function renderUsage(u) {
  const panel = document.getElementById("usage-panel");
  if (!u || !u.totals) { panel.style.display = "none"; return; }
  document.getElementById("usage-note").textContent =
    `API-equivalent estimate at list prices (pricing ${u.revision}); subscriptions are not billed this way.`;
  const t = u.totals;
  document.getElementById("usage-unpriced").innerHTML = (t.unpriced_models || []).length
    ? `<div class="unpriced">No price for ${t.unpriced_models.map(m => `<code>${esc(m)}</code>`).join(", ")}. Add them to <code>~/.config/convo-index/pricing.toml</code> to include them in the estimate.</div>`
    : "";
  document.getElementById("usage-by-model").innerHTML = usageTable(u.by_model, null, "model");
  document.getElementById("usage-by-source").innerHTML = usageTable(u.by_source, s => "/?source=" + encodeURIComponent(s), "source");
  document.getElementById("usage-by-month").innerHTML = usageTable(u.by_month, null, "month");
  document.getElementById("usage-by-project").innerHTML = usageTable(u.by_project, p => "/?project=" + encodeURIComponent(p), "project");
  document.getElementById("usage-by-reference").innerHTML = usageTable(u.by_reference, null, "ticket / PR / branch");
  // The table is keyed by title; the id only exists on the source rows, so the first cell
  // of each row is relinked afterwards.
  const top = (u.top_conversations || []).map(c => [c.title || c.id, c]);
  document.getElementById("usage-top").innerHTML = usageTable(top, null, "conversation");
  const rows = document.querySelectorAll("#usage-top tbody tr");
  (u.top_conversations || []).forEach((c, i) => {
    const cell = rows[i] && rows[i].querySelector(".lbl");
    if (cell) cell.innerHTML = `<a href="/#${encodeURIComponent(c.id)}" title="${esc(c.title || c.id)}">${esc(c.title || c.id)}</a>`;
  });
  if (!u.by_model.length && !u.by_source.length) {
    document.getElementById("usage-by-model").innerHTML =
      `<p class="hint">No token usage is recorded yet. Sources that report usage fill this in on the next index run.</p>`;
  }
}

function wireTabs() {
  const tabs = document.getElementById("usage-tabs");
  tabs.addEventListener("click", e => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    tabs.querySelectorAll("button").forEach(b => b.setAttribute("aria-selected", String(b === btn)));
    document.querySelectorAll("#usage-panel .tab-panel").forEach(p => p.classList.toggle("active", p.id === btn.dataset.tab));
  });
}

// Project paths are long; show the last two segments and keep the full path as a title.
function projLabel(p) {
  const parts = shortPath(p).split("/");
  return parts.length > 3 ? ".../" + parts.slice(-2).join("/") : shortPath(p);
}

async function load() {
  wireTabs();
  let d;
  try { d = await (await fetch("/api/analytics")).json(); }
  catch (e) { showError("Could not load analytics: " + e); return; }
  renderKpis(d);
  renderUsage(d.usage);
  document.getElementById("by-source").innerHTML =
    bars(d.by_source, s => "/?source=" + encodeURIComponent(s));
  document.getElementById("by-tags").innerHTML = Object.entries(d.by_tag_group || {}).map(([g, rows]) =>
    `<section class="panel"><h3>By ${esc(g)}</h3>${bars(rows, t => "/?tag=" + encodeURIComponent(g + ":" + t))}</section>`
  ).join("");
  document.getElementById("by-month").innerHTML = bars(d.by_month, null);
  document.getElementById("top-tickets").innerHTML =
    bars(d.top_tickets, t => "/?q=" + encodeURIComponent(t));
  document.getElementById("top-references").innerHTML =
    bars((d.top_references || []).map(([k, key, n]) => [`${k} ${key}`, n]), null);
  const projects = (d.top_projects || []).map(([p, n]) => [projLabel(p), n]);
  document.getElementById("top-projects").innerHTML = projects.length
    ? projects.map(([label, n], i) => {
        const max = Math.max(1, ...projects.map(r => r[1]));
        const full = d.top_projects[i][0];
        return `<div class="bar-row"><a class="bar-label" href="/?project=${encodeURIComponent(full)}" title="${esc(full)}">${esc(label)}</a>
          <div class="bar-track"><div class="bar-fill" style="width:${(n / max) * 100}%"></div></div><span class="bar-count">${n}</span></div>`;
      }).join("")
    : `<p class="hint">Nothing recorded yet.</p>`;
}
load();
