const $ = (id) => document.getElementById(id);
let selectedId = null;
let projectFilter = null;
let sourceFilterFromUrl = null;
let emptyPoll = null;         // timer that re-runs the search while the index is still empty
let MODE = "hybrid";
let currentIds = [];          // rendered order, for keyboard navigation
let searchSeq = 0;            // ignore responses that arrive out of order

let TICKET_URL = "https://example.atlassian.net/browse/{key}";
let TAG_GROUPS = {};
let WINDOWS = false;          // the server's platform: decides how the resume command is quoted          // {group: {exclusive, description, tags: [{name, description}]}}
let tagFiltersFromUrl = [];   // ["workspace:globex", ...]
const SOURCE_LABELS = { claude_code: "Claude Code", opencode: "Opencode", codex: "Codex", antigravity: "Antigravity", pi: "pi" };
const ROLE_LABELS = { user: "You", assistant: "Assistant", system: "System", tool: "Tool" };
const ICON = {
  copy: '<svg class="icon" viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>',
  check: '<svg class="icon" viewBox="0 0 24 24"><path d="m5 12 5 5L20 7"/></svg>',
  eye: '<svg class="icon" viewBox="0 0 24 24"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  eyeOff: '<svg class="icon" viewBox="0 0 24 24"><path d="M3 3l18 18M10.6 10.6A3 3 0 0 0 13.4 13.4M9.9 5.2A10.5 10.5 0 0 1 12 5c6 0 10 7 10 7a17 17 0 0 1-3.2 3.9M6.6 6.6C3.9 8.6 2 12 2 12s4 7 10 7a9.8 9.8 0 0 0 4.4-1"/></svg>',
  trash: '<svg class="icon" viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
  back: '<svg class="icon" viewBox="0 0 24 24"><path d="M19 12H5M11 18l-6-6 6-6"/></svg>',
  search: '<svg class="icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  inbox: '<svg class="icon" viewBox="0 0 24 24"><path d="M3 13h5l2 3h4l2-3h5M5 4h14l2 9v7H3v-7z"/></svg>',
  close: '<svg class="icon" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  stop: '<svg class="icon" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  panel: '<svg class="icon" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/></svg>',
  tools: '<svg class="icon" viewBox="0 0 24 24"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/></svg>',
  send: '<svg class="icon" viewBox="0 0 24 24"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4z"/></svg>',
};
function sourceLabel(s) { return SOURCE_LABELS[s] || s || "unknown"; }
function sourceBadge(s) { return `<span class="badge src ${escape(s || "")}">${escape(sourceLabel(s))}</span>`; }

function ticketLink(t) {
  const href = TICKET_URL.replace("{key}", encodeURIComponent(t));
  return `<a class="badge ticket" href="${escape(href)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${escape(t)}</a>`;
}

// A filter chip is a <label> around a hidden checkbox; `.on` mirrors :checked.
function chip(cls, value, label, checked, title) {
  return `<label class="chip${checked ? " on" : ""}" title="${escape(title || "")}"><input type="checkbox" class="${cls}" value="${escape(value)}"${checked ? " checked" : ""}> ${escape(label)}</label>`;
}
function wireChips(container) {
  container.querySelectorAll(".chip input").forEach(el => el.addEventListener("change", () => {
    el.closest(".chip").classList.toggle("on", el.checked);
    updateFilterState();
    runSearch();
  }));
}
function anyFilterActive() {
  return !!projectFilter || document.querySelectorAll("#filters .chip input:checked").length > 0;
}
// The filters live in a panel behind one button; what is switched on shows as removable chips
// above the results, so a closed panel never hides why the list is short.
function updateFilterState() {
  $("filters").classList.toggle("active", anyFilterActive());
  const active = Array.from(document.querySelectorAll("#filters .chip input:checked")).map(el => ({
    label: el.classList.contains("f-tag") ? el.value.replace(":", ": ") : el.closest(".chip").textContent.trim(),
    remove: () => { el.checked = false; el.dispatchEvent(new Event("change")); },
  }));
  if (projectFilter) active.unshift({ label: "Project " + shortPath(projectFilter), remove: () => { projectFilter = null; updateFilterState(); runSearch(); } });
  const box = $("active-filters");
  box.innerHTML = active.map((f, i) =>
    `<button type="button" class="active-chip" data-i="${i}" aria-label="Remove filter ${escape(f.label)}">${escape(f.label)}${ICON.close}</button>`).join("");
  box.querySelectorAll(".active-chip").forEach(b => b.addEventListener("click", () => active[Number(b.dataset.i)].remove()));
  $("filter-count").textContent = active.length ? String(active.length) : "";
  $("filters-toggle").classList.toggle("on", active.length > 0);
}
function clearFilters() {
  document.querySelectorAll("#filters .chip input:checked").forEach(el => { el.checked = false; el.closest(".chip").classList.remove("on"); });
  projectFilter = null;
  updateFilterState();
  runSearch();
}

// Tag groups come from the config file (via /api/config); one chip row per group.
async function loadConfig() {
  const res = await fetch("/api/config");
  if (!res.ok) return;
  const cfg = await res.json();
  TICKET_URL = cfg.ticket_url || TICKET_URL;
  WINDOWS = !!cfg.windows;
  TAG_GROUPS = cfg.tag_groups || {};
  const box = $("tag-filters");
  box.innerHTML = Object.entries(TAG_GROUPS).map(([g, spec]) =>
    `<span class="chip-group" title="${escape(spec.description || "")}"><span class="chip-group-label">${escape(g)}</span>` +
    spec.tags.map(t => {
      const v = `${g}:${t.name}`;
      return chip("f-tag", v, t.name, tagFiltersFromUrl.includes(v), t.description || "");
    }).join("") + `</span>`
  ).join("");
  wireChips(box);
  const sem = document.querySelector('#mode [data-mode="semantic"]');
  if (!cfg.semantic_search && sem) {
    sem.disabled = true;
    sem.title = "Semantic search unavailable: " + (cfg.semantic_reason || "");
    if (MODE === "semantic") setMode("hybrid");
  }
  updateFilterState();
}

function selectedTags() {
  return Array.from(document.querySelectorAll(".f-tag:checked")).map(el => el.value);
}

// Stable colour per tag within its group (tags are user-defined, so no per-tag CSS).
function tagBadge(group, tag, source) {
  const spec = TAG_GROUPS[group];
  const idx = spec ? Math.max(0, spec.tags.findIndex(t => t.name === tag)) : 0;
  const src = source ? ` title="${escape(group)}: ${escape(tag)} (${escape(source)})"` : ` title="${escape(group)}"`;
  return `<span class="badge cat cat-${idx % 6}"${src}>${escape(tag)}</span>`;
}

function tagBadges(tags) {
  return Object.entries(tags || {}).flatMap(([g, list]) => list.map(t => tagBadge(g, t))).join("");
}

// Source chips are rendered from /api/sources so new tools appear without UI changes.
// Nothing checked means "any source".
async function loadSources() {
  const res = await fetch("/api/sources");
  if (!res.ok) return;
  const sources = await res.json();
  const box = $("source-filters");
  box.innerHTML = sources.length
    ? `<span class="chip-group-label">Source</span>` + sources.map(s => chip("f-source", s, sourceLabel(s), s === sourceFilterFromUrl)).join("")
    : "";
  wireChips(box);
  updateFilterState();
}

function selectedSources() {
  return Array.from(document.querySelectorAll(".f-source:checked")).map(el => el.value);
}

function setMode(mode) {
  MODE = mode;
  document.querySelectorAll("#mode button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
}

function skeletons(n) {
  return Array.from({ length: n }, () =>
    `<li class="skeleton"><div class="sk w60"></div><div class="sk w40"></div><div class="sk w80"></div></li>`).join("");
}

async function runSearch() {
  const seq = ++searchSeq;
  const params = new URLSearchParams();
  const q = $("q").value.trim();
  params.set("q", q);
  params.set("mode", MODE);
  selectedTags().forEach(t => params.append("tag", t));
  const srcs = selectedSources();
  if (srcs.length) params.set("source", srcs.join(","));   // filtered server-side, before the result limit
  if ($("f-ticket").checked) params.set("ticket", "1");
  if ($("f-hidden").checked) params.set("hidden", "1");
  if ($("f-children").checked) params.set("children", "1");
  if (projectFilter) params.set("project", projectFilter);
  $("search-box").classList.toggle("has-value", !!$("q").value);
  if (!$("results").children.length) $("results").innerHTML = skeletons(6);
  $("results-hint").textContent = "Searching…";
  let res;
  try { res = await fetch("/api/search?" + params.toString()); }
  catch (e) { if (seq === searchSeq) { $("results").innerHTML = emptyState("Search failed", "The server did not answer. Is it still running?"); $("results-hint").textContent = ""; } return; }
  if (seq !== searchSeq) return;           // a newer search is already in flight
  if (!res.ok) {
    $("results").innerHTML = emptyState("Search failed", `The server returned ${res.status}.`);
    $("results-count").textContent = ""; $("results-hint").textContent = "";
    return;
  }
  const items = await res.json();
  if (seq !== searchSeq) return;
  render(items, srcs, q);
}

function emptyState(title, text, action) {
  return `<li class="empty-state">${ICON.inbox}<h4>${escape(title)}</h4><p>${text}</p>${action || ""}</li>`;
}

function render(items, srcs, q) {
  const ul = $("results");
  ul.innerHTML = "";
  updateFilterState();
  const filtered = items;
  currentIds = filtered.map(i => i.id);
  const n = filtered.length;
  $("results-count").innerHTML = q ? `<b>${n}</b> result${n === 1 ? "" : "s"}` : `<b>${n}</b> recent conversation${n === 1 ? "" : "s"}`;
  $("results-hint").textContent = q ? `${MODE} search` : (anyFilterActive() ? "filtered" : "newest first");
  if (!n) {
    if (q || anyFilterActive()) {
      const tips = [];
      if (q && MODE !== "keyword") tips.push(`try <a href="#" data-act="keyword">keyword mode</a>`);
      if (anyFilterActive()) tips.push(`<a href="#" data-act="clear">clear the filters</a>`);
      ul.insertAdjacentHTML("beforeend", emptyState("No matches", (q ? `Nothing matched “${escape(q)}”. ` : "") + (tips.length ? "You could " + tips.join(" or ") + "." : "")));
      ul.querySelectorAll("[data-act]").forEach(a => a.addEventListener("click", e => {
        e.preventDefault();
        if (a.dataset.act === "keyword") { setMode("keyword"); runSearch(); } else clearFilters();
      }));
    } else {
      ul.insertAdjacentHTML("beforeend", emptyState("Nothing indexed yet",
        "If the first build is still running this page fills in by itself. Otherwise run <code>convo-index index</code> in a terminal, or refresh the index from here.",
        `<button class="btn primary" data-act="refresh">Refresh index</button>`));
      ul.querySelector("[data-act=refresh]").addEventListener("click", () => $("refresh").click());
      // the first build runs in the background after `convo-index up`: look again until it shows
      clearTimeout(emptyPoll); emptyPoll = setTimeout(runSearch, 5000);
    }
    return;
  }
  if (emptyPoll) { clearTimeout(emptyPoll); emptyPoll = null; loadSources(); }   // the index just got its first conversations
  for (const it of filtered) {
    const li = document.createElement("li");
    li.className = "card" + (it.id === selectedId ? " selected" : "") + (it.hidden ? " hidden-card" : "");
    li.dataset.id = it.id;
    li.tabIndex = 0;
    li.onclick = () => openDetail(it.id, li);
    li.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); openDetail(it.id, li); } };
    // One line of where it ran; tags and tickets live in the conversation's details. The
    // snippet only says something when there is a query to match.
    const proj = it.project_root || it.cwd || "";
    const flags = [it.hidden && ["hidden-tag", "hidden"], it.parent_id && ["child", "sub-session"],
                   it.fidelity && it.fidelity !== "full" && ["fidelity", it.fidelity]].filter(Boolean);
    const msgs = it.message_count ? `<span class="sep">·</span><span>${it.message_count} msg${it.message_count === 1 ? "" : "s"}</span>` : "";
    li.innerHTML = `<div class="card-top"><div class="title" title="${escape(it.title)}">${escape(it.title)}</div>
        <time datetime="${escape(it.start_ts || "")}" title="${escape(fullTime(it.start_ts))}">${escape(relTime(it.start_ts))}</time></div>
      <div class="card-meta"><span class="src-dot ${escape(it.source || "")}" aria-hidden="true"></span><span>${escape(sourceLabel(it.source))}</span>${proj ? `<span class="sep">·</span><span class="proj" title="${escape(proj)}">${escape(baseName(proj))}</span>` : ""}${msgs}${flags.map(([cls, label]) => `<span class="badge ${cls}">${escape(label)}</span>`).join("")}</div>
      ${q ? `<div class="snippet">${snippetHTML(it.snippet)}</div>` : ""}`;
    ul.appendChild(li);
  }
}

// Single-quote a path for shell use, escaping embedded single quotes.
function shellQuote(s) {
  if (WINDOWS) return "'" + String(s).replace(/'/g, "''") + "'";     // PowerShell: a single-quoted string is literal
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

function copyCmd(btn) {
  const cmd = btn.dataset.cmd;
  navigator.clipboard.writeText(cmd).then(() => {
    const prev = btn.innerHTML;
    btn.innerHTML = ICON.check + `<span class="label">Copied</span>`;
    setTimeout(() => { btn.innerHTML = prev; }, 1400);
  }).catch(() => showError("Could not copy to the clipboard"));
}

// The server marks FTS matches with [..] — turn them into highlights.
function snippetHTML(s) {
  return escape(s || "").replace(/\[/g, "<mark>").replace(/\]/g, "</mark>");
}

// Messages may embed Claude Code markup: slash-command invocations
// (<command-name>/x</command-name> + args), local-command output, and caveats.
// Render commands as a compact badge and command output as a labeled block.
function renderMessageBody(text) {
  let t = (text || "").replace(/<local-command-caveat>[\s\S]*?<\/local-command-caveat>/g, "");

  let badge = "";
  const nameMatch = t.match(/<command-name>([\s\S]*?)<\/command-name>/);
  if (nameMatch) {
    const name = nameMatch[1].trim().replace(/^\//, "");
    const argsMatch = t.match(/<command-args>([\s\S]*?)<\/command-args>/);
    const args = argsMatch ? argsMatch[1].trim() : "";
    badge = `<span class="cmd-badge">${escape(name)}${args ? " " + escape(args) : ""}</span>`;
    t = t.replace(/<command-(name|message|args)>[\s\S]*?<\/command-\1>/g, "");
  }

  const body = formatWithOutputs(t).trim();
  if (badge && body) return `${badge}<div class="cmd-rest">${body}</div>`;
  return badge || body;
}

// Render <local-command-stdout>/<local-command-stderr> blocks as labeled output
// panels; format the surrounding text normally.
function formatWithOutputs(text) {
  const re = /<local-command-(stdout|stderr)>([\s\S]*?)<\/local-command-\1>/g;
  let out = "", last = 0, m;
  while ((m = re.exec(text))) {
    out += formatBody(text.slice(last, m.index));
    const content = m[2].replace(/^\n+|\n+$/g, "");
    out += content
      ? `<div class="cmd-output"><span class="out-tag">${m[1]}</span><pre>${escape(content)}</pre></div>`
      : `<span class="cmd-badge">${m[1]} (empty)</span>`;
    last = m.index + m[0].length;
  }
  out += formatBody(text.slice(last));
  return out;
}

// Lightweight formatter: fenced ```code``` blocks, inline `code`, and the small subset of
// Markdown that agents actually emit (headings, bold, italics, links). Everything is escaped
// first, so the patterns below only ever see text.
function formatBody(text) {
  const parts = (text || "").split("```");
  return parts.map((p, i) => {
    if (i % 2 === 1) {
      const code = p.replace(/^[a-zA-Z0-9_+-]*\n/, ""); // drop optional language line
      return `<pre><code>${escape(code)}</code></pre>`;
    }
    return inlineMarkdown(escape(p));
  }).join("");
}

// `escaped` is already HTML-escaped. Inline code is extracted first so its contents are
// never treated as markup.
function inlineMarkdown(escaped) {
  const code = [];
  let t = escaped.replace(/`([^`\n]+)`/g, (_, c) => `\u0000${code.push(`<code>${c}</code>`) - 1}\u0000`);
  t = t.replace(/^(#{1,6})\s+(.+)$/gm, (_, h, txt) => `<strong class="md-h">${txt}</strong>`);
  t = t.replace(/\*\*([^\n*][^*]*?)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|[\s(])\*([^\s*][^*\n]*?)\*(?=[\s.,;:)]|$)/gm, "$1<em>$2</em>");
  t = t.replace(/\[([^\]\n]+)\]\((https?:[^)\s]+)\)/g,
    (_, label, href) => `<a href="${href}" target="_blank" rel="noopener">${label}</a>`);
  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => code[Number(i)]);
}

function resolvedClass(v) {
  const s = (v || "").toLowerCase();
  if (s.startsWith("yes")) return "ok";
  if (s.startsWith("partial")) return "partial";
  if (s.startsWith("no")) return "bad";
  return "";
}

function summaryHTML(s) {
  if (!s) return `<div class="summary none">No summary yet. One is written on the next index run.</div>`;
  const failed = (s.claude_failed || "").toLowerCase().startsWith("yes");
  const row = (label, val, cls, pill) =>
    val ? `<div class="sum-row ${cls || ""}"><span class="sum-k">${label}</span><span class="sum-v">${pill ? `<span class="sum-pill">${escape(val)}</span>` : escape(val)}</span></div>` : "";
  return `<div class="summary">
    ${row("Problem", s.problem)}
    ${row("Solution", s.solution)}
    ${row("Resolved", s.is_resolved, "rs-" + resolvedClass(s.is_resolved), true)}
    ${row("Learning", s.learning)}
    ${row("Agent failed", s.claude_failed, failed ? "cf-yes" : "cf-no", true)}
  </div>`;
}

// Messages the coding tool's harness writes into the transcript under the user's role:
// background-task notifications, command and shell output, compaction summaries, interrupts.
// They are shown as "Harness", collapsed, and never start a turn. First match wins.
const HARNESS_PATTERNS = [
  [/^\s*<task-notification>/, "task notification"],
  [/^\s*<local-command-std(out|err)>/, "command output"],
  [/^\s*<bash-input>/, "shell command"],
  [/^\s*<bash-std(out|err)>/, "shell output"],
  [/^\s*<system-reminder>/, "system reminder"],
  [/^\s*Caveat: The messages below were generated by the user while running local commands/, "local command note"],
  [/^\s*This session is being continued from a previous conversation/, "context compacted"],
  [/^\s*\[Request interrupted by user/, "interrupted"],
  [/^\s*<user_action>[\s\S]*?<action>review<\/action>/, "review output"],
  [/^\s*<user_action>/, "user action"],
  [/^\s*<user_shell_command>/, "shell command"],
];
function harnessLabel(m) {
  if (m.role !== "user" || (m.kind || "text") !== "text") return null;
  const hit = HARNESS_PATTERNS.find(([re]) => re.test(m.text || ""));
  return hit ? hit[1] : null;
}
// One line that says what the message is: a <summary> or <command> element if it has one,
// otherwise its first non-empty line with the markup stripped.
function firstLine(text) {
  const t = text || "";
  const el = t.match(/<(summary|command)>\s*([\s\S]*?)\s*<\/\1>/);
  if (el && el[2].trim()) return el[2].split("\n")[0];
  return t.split("\n").map(l => l.replace(/<\/?[a-z_-]+>/g, "").trim()).find(Boolean) || "";
}

// Tool calls, thinking and harness messages are collapsed so the transcript reads as a
// conversation. The assistant's interim narration (a step that is text) is collapsible too:
// expanded in the full views, a one-line row in "Replies only" (see applyTranscriptView).
// The date is printed only when it changes from the previous message.
function messageHTML(m, prevDate, step) {
  const ts = m.ts || "";
  const date = ts.slice(0, 10), clock = ts.slice(11, 16);
  const time = date && date !== prevDate ? `${date} ${clock}` : clock;
  const kind = m.kind || "text";
  const harness = harnessLabel(m);
  const folded = (cls, open) => `<details${cls ? ` class="${cls}"` : ""}${open ? " open" : ""}><summary>${escape(firstLine(m.text).slice(0, 140))}</summary>${renderMessageBody(m.text)}</details>`;
  let body;
  if (harness || kind === "tool_call" || kind === "tool_result" || kind === "thinking") body = folded("", false);
  else if (step) body = folded("step-text", !document.body.classList.contains("hide-steps"));
  else body = renderMessageBody(m.text);
  const classes = ["msg", harness ? "harness" : m.role, kind].concat(step ? ["step"] : []).map(escape).join(" ");
  const role = harness ? "Harness" : ROLE_LABELS[m.role] || m.role;
  return `<div class="${classes}"${step ? ` data-turn="${step.turn}"` : ""}>
      <div class="msg-head"><span class="role-tag">${escape(role)}</span>${harness ? `<span class="harness-kind">${escape(harness)}</span>` : ""}<span title="${escape(fullTime(ts))}">${escape(time)}</span></div>
      <div class="msg-body">${body}</div>
    </div>`;
}

// Tokens and API-equivalent cost of one conversation (docs/design.md §10: estimate and reported
// cost are shown as two numbers; partial pricing shows as "≥ $x (n% priced)").
function usageLine(u) {
  if (!u || !u.requests) return "";
  const usd = x => x >= 1 ? "$" + x.toFixed(2) : "$" + x.toFixed(4);
  let cost = "";
  if (u.coverage) {                         // nothing priced: no "$0" estimate
    cost = (u.coverage < 1 ? "≥ " : "≈ ") + usd(u.estimated_cost || 0) + (u.coverage < 1 ? ` (${Math.round(u.coverage * 100)}% priced)` : "");
  }
  const rep = u.reported_cost !== null && u.reported_cost !== undefined ? `<span class="sep">·</span><span>reported ${usd(u.reported_cost)}</span>` : "";
  const detail = `input ${fmtTokens(u.input_uncached)} · cache read ${fmtTokens(u.cache_read)} · cache write ${fmtTokens(u.cache_write)} · output ${fmtTokens(u.output)} · ${u.requests} requests`;
  return `<div class="usage" title="${escape(detail)}"><b>${fmtTokens(u.tokens)}</b> tokens<span class="sep">·</span><span>${u.requests} requests</span>${cost ? `<span class="sep">·</span><span><b>${escape(cost)}</b> <span class="hint">API-equivalent estimate</span></span>` : ""}${rep}</div>`;
}

// A turn starts at each message the user typed (harness messages do not count). Its reply is
// the last assistant text message before the next turn; everything else in the turn (thinking,
// tool calls and results, harness messages, the assistant's interim narration) is a step.
// System messages are never steps. counts[turn] = {n, harness: {label: count}}.
function transcriptTurns(messages) {
  const steps = new Array(messages.length).fill(null), counts = [];
  const typed = m => m.role === "user" && (m.kind || "text") === "text" && !harnessLabel(m);
  let turn = 0, start = 0;
  const close = (end) => {
    let reply = -1;
    for (let i = end - 1; i >= start; i--) {
      if (messages[i].role === "assistant" && (messages[i].kind || "text") === "text") { reply = i; break; }
    }
    const c = { n: 0, harness: {} };
    for (let i = start; i < end; i++) {
      const m = messages[i];
      if (i === reply || m.role === "system" || typed(m)) continue;
      steps[i] = { turn, first: c.n === 0 }; c.n++;
      const h = harnessLabel(m);
      if (h) c.harness[h] = (c.harness[h] || 0) + 1;
    }
    counts[turn] = c;
  };
  messages.forEach((m, i) => {
    if (i > start && typed(m)) { close(i); turn++; start = i; }
  });
  close(messages.length);
  return { steps, counts };
}

// "33 steps · task notification ×2" — names what the harness did inside the folded steps.
function stepsLabel(c) {
  const h = Object.entries(c.harness).map(([k, v]) => v > 1 ? `${k} ×${v}` : k);
  return `${c.n} step${c.n === 1 ? "" : "s"}` + (h.length ? ` · <span class="harness-kind">${escape(h.join(", "))}</span>` : "");
}

// Transcript view: "replies" (the default: only what the user typed and each final reply, the
// steps between folded into a counter) or "all". The retired "no-tools" view reads as "all".
const TRANSCRIPT_VIEWS = ["replies", "all"];
function transcriptViewPref() {
  try {
    const v = localStorage.getItem("transcriptView");
    if (TRANSCRIPT_VIEWS.includes(v)) return v;
    return v === "no-tools" ? "all" : "replies";
  } catch (e) { return "replies"; }
}
function applyTranscriptView(view) {
  document.body.classList.toggle("hide-tools", view !== "all");
  document.body.classList.toggle("hide-steps", view === "replies");
  // interim narration reads in full in the full views and as one-line rows in "Replies only"
  document.querySelectorAll("details.step-text").forEach(d => { d.open = view !== "replies"; });
  try { localStorage.setItem("transcriptView", view); } catch (e) { /* ignore */ }
}

// The details panel (summary, tags, references, resume) sits beside the transcript and is
// open by default; closing it is remembered in this browser. Below 1200px it is a drawer over
// the transcript instead: closed until asked for, and never remembered.
let tagEditOpen = false;
const NARROW = window.matchMedia("(max-width: 1200px)");
function detailsPanelOpen() {
  if (NARROW.matches) return false;
  try { return localStorage.getItem("detailsPanel") !== "closed"; } catch (e) { return true; }
}
function toggleDetailsPanel(open) {
  const detail = $("detail"), btn = $("details-toggle");
  if (!btn) return;
  open = open === undefined ? !detail.classList.contains("details-open") : open;
  detail.classList.toggle("details-open", open);
  btn.setAttribute("aria-expanded", String(open));
  if (NARROW.matches) {
    // the drawer covers the transcript: focus goes into it, and back to the button on close
    if (open) $("details-close").focus();
    else if ($("details-panel").contains(document.activeElement) || document.activeElement === document.body) btn.focus();   // a re-render drops focus to the body
    return;
  }
  try { localStorage.setItem("detailsPanel", open ? "open" : "closed"); } catch (e) { /* not remembered */ }
}

// "≈ $1.23" for the header: the API-equivalent estimate, when there is one.
function costShort(u) {
  if (!u || !u.requests || !u.coverage) return "";     // nothing priced: say nothing rather than "$0"
  const x = u.estimated_cost || 0;
  return `<span class="mi" title="API-equivalent estimate">${u.coverage < 1 ? "≥ " : "≈ "}${x >= 1 ? "$" + x.toFixed(2) : "$" + x.toFixed(4)}</span>`;
}

async function openDetail(id, cardEl) {
  // re-rendering the conversation on show (after a tag, reference or hide change) keeps the
  // details panel as it was, drawer included
  const keepPanel = id === selectedId && $("detail").classList.contains("details-open");
  if (id !== selectedId) tagEditOpen = false;    // the tag editor stays open only for re-renders
  detachTurn();
  const gen = ++detailView;
  selectedId = id;
  if (location.hash.slice(1) !== id) location.hash = id;
  document.querySelectorAll(".card.selected").forEach(c => c.classList.remove("selected"));
  const card = cardEl || document.querySelector(`.card[data-id="${CSS.escape(id)}"]`);
  if (card) { card.classList.add("selected"); card.scrollIntoView({ block: "nearest" }); }
  document.body.classList.add("detail-open");

  const detail = $("detail");
  detail.className = "";
  detail.innerHTML = `<div class="detail-body"><div class="skeleton" style="border:0;padding:0"><div class="sk w60"></div><div class="sk w40"></div><div class="sk w80"></div><div class="sk w80"></div></div></div>`;

  const failed = why => { detail.innerHTML = `<div class="detail-body">${emptyState("Could not load this conversation", why).replace(/^<li/, "<div").replace(/li>$/, "div>")}</div>`; };
  let res, c;
  try {
    res = await fetch("/api/conversation/" + encodeURIComponent(id));
    if (gen !== detailView) return;                    // the user already moved on
    if (!res.ok) { failed(`The server returned ${res.status}.`); return; }
    c = await res.json();
  } catch (e) {
    if (gen === detailView) failed("The server did not answer. Is it still running?");
    return;
  }
  if (gen !== detailView) return;
  selectedId = c.id;  // legacy ids resolve server-side to the namespaced id

  let prevDate = "";
  const turns = transcriptTurns(c.messages);
  const msgs = c.messages.map((m, i) => {
    const step = turns.steps[i];
    const marker = step && step.first
      ? `<button type="button" class="steps-toggle" data-turn="${step.turn}" aria-expanded="false" title="Thinking, tool activity, harness messages and interim replies before the final reply">${stepsLabel(turns.counts[step.turn])}</button>` : "";
    const h = marker + messageHTML(m, prevDate, step);
    prevDate = (m.ts || "").slice(0, 10) || prevDate;
    return h;
  }).join("");
  const stepCount = turns.counts.reduce((a, c) => a + c.n, 0);

  const cwd = c.cwd || ".";
  const resume = !c.resume_cmd ? null
    : WINDOWS ? `Set-Location -LiteralPath ${shellQuote(cwd)} -ErrorAction Stop; ${c.resume_cmd}`   // a missing dir stops the line
    : `cd ${shellQuote(cwd)} && ${c.resume_cmd}`;
  // Links carry the target id in a data attribute; a delegated click handler opens it.
  const convLink = (id, label) => `<a href="#${encodeURIComponent(id)}" class="conv-link" data-id="${escape(id)}">${escape(label)}</a>`;
  const flags = [
    c.hidden ? `<span class="badge hidden-tag">hidden</span>` : "",
    c.fidelity && c.fidelity !== "full" ? `<span class="badge fidelity">${escape(c.fidelity)} transcript</span>` : "",
    (c.warnings || []).length ? `<span class="badge fidelity" title="${escape(c.warnings.join("; "))}">⚠ ${c.warnings.length} warning${c.warnings.length > 1 ? "s" : ""}</span>` : "",
  ].join("");
  // each item is its own element and the separators are CSS, so a phone can drop the paths
  const meta = [
    `<span class="mi"><span class="src-dot ${escape(c.source || "")}" aria-hidden="true"></span>${escape(sourceLabel(c.source))}</span>`,
    c.cwd ? `<span class="mi path" title="${escape(c.cwd)}">${escape(shortPath(c.cwd))}</span>` : "",
    c.git_branch && c.git_branch !== "HEAD" ? `<span class="mi path" title="Git branch">⎇ ${escape(c.git_branch)}</span>` : "",
    `<span class="mi">${c.messages.length} message${c.messages.length === 1 ? "" : "s"}</span>`,
    costShort(c.usage),
  ].join("");
  const view = transcriptViewPref();
  const viewBtn = (v, label, title) => `<button type="button" data-view="${v}" aria-pressed="${view === v}" title="${title}">${label}</button>`;
  const open = keepPanel || detailsPanelOpen();
  const section = (label, body) => body ? `<section class="dsec"><h3 class="dsec-label">${label}</h3>${body}</section>` : "";
  const assigned = tagBadges((c.tags || {}).assignments);
  const tagsHTML = tagEditorHTML(c);
  const refs = (c.tickets || []).map(ticketLink).join("") + referencesHTML(c);
  const session = [
    `<div>Started <span title="${escape(fullTime(c.start_ts))}">${escape(fullTime(c.start_ts))}</span></div>`,
    // the title bar drops these on small phones, so the details always carry them
    c.cwd ? `<div>Directory <span class="path" title="${escape(c.cwd)}">${escape(shortPath(c.cwd))}</span></div>` : "",
    c.git_branch && c.git_branch !== "HEAD" ? `<div>Branch <span class="path">${escape(c.git_branch)}</span></div>` : "",
    (c.models || []).length ? `<div title="Models used">${escape(c.models.join(", "))}</div>` : "",
    c.parent_id ? `<div>${escape(c.relation || "child")} of ${convLink(c.parent_id, c.parent_id)}</div>` : "",
    (c.children || []).length ? `<div>Sub-sessions: ${c.children.map(k => convLink(k, k.split(":")[1] || k)).join(" · ")}</div>` : "",
    usageLine(c.usage),
  ].join("");
  detail.className = open ? "details-open" : "";
  detail.innerHTML = `<div class="detail-bar">
      <button class="btn ghost icon-only" id="detail-back" aria-label="Back to results">${ICON.back}</button>
      <div class="detail-head">
        <h2 class="detail-title" title="${escape(c.title)}">${escape(c.title)}</h2>
        <div class="meta-line">${meta}${flags}</div>
      </div>
      <div class="toolbar">
        ${stepCount ? `<div class="segmented small" id="transcript-view" role="group" aria-label="Transcript view">
          ${viewBtn("replies", "Replies", "Only your messages and each final reply; the steps in between fold into a counter")}
          ${viewBtn("all", "All", "Every message, with tool activity and thinking collapsed")}
        </div>` : ""}
        <button class="btn" id="details-toggle" type="button" aria-expanded="${open}" aria-controls="details-panel" title="Summary, tags, references and how to resume">${ICON.panel}<span class="label">Details</span></button>
      </div>
    </div>
    <div class="detail-grid">
      <div class="conversation">
        ${msgs}
        ${c.turns_supported ? composerHTML(c) : ""}
      </div>
      <aside class="details-panel" id="details-panel" aria-label="Conversation details">
        <div class="panel-head"><h2>Details</h2><button class="btn ghost icon-only" id="details-close" type="button" aria-label="Close details">${ICON.close}</button></div>
        ${section(`Continue in ${WINDOWS ? "PowerShell" : "the terminal"}`, resume ? `<div class="resume">
          <code class="resume-cmd" title="${escape(resume)}">${escape(resume)}</code>
          <button class="btn copy-btn" data-cmd="${escape(resume)}" onclick="copyCmd(this)">${ICON.copy}<span class="label">Copy</span></button>
        </div>` : "")}
        ${section("Summary", summaryHTML(c.summary))}
        ${tagsHTML ? section("Tags", `<div class="tag-list">${assigned || `<span class="hint">None yet</span>`}</div>
          <details class="tag-edit"${tagEditOpen ? " open" : ""}><summary>Edit tags</summary>${tagsHTML}</details>`) : ""}
        ${section("References", refs ? `<div class="refs">${refs}</div>` : "")}
        ${section("Related", relatedHTML(c))}
        ${section("Session", session)}
        <div class="detail-actions">
          <button class="btn" id="btn-hide" title="${c.hidden ? "Show this conversation in results again" : "Keep this conversation out of results (reversible)"}">${c.hidden ? ICON.eye : ICON.eyeOff}<span class="label">${c.hidden ? "Unhide" : "Hide"}</span></button>
          <button class="btn danger" id="btn-delete" title="Remove permanently; it will not come back on reindex">${ICON.trash}<span class="label">Delete</span></button>
          <span id="confirm-slot"></span>
        </div>
      </aside>
    </div>`;
  $("details-toggle").addEventListener("click", () => toggleDetailsPanel());
  $("details-close").addEventListener("click", () => toggleDetailsPanel(false));
  const tagEdit = detail.querySelector(".tag-edit");
  if (tagEdit) tagEdit.addEventListener("toggle", () => { tagEditOpen = tagEdit.open; });
  $("detail-back").addEventListener("click", closeDetail);
  $("btn-hide").addEventListener("click", () => toggleHide(c.id, c.hidden ? 0 : 1));
  $("btn-delete").addEventListener("click", () => askDelete(c.id));
  const tv = $("transcript-view");
  if (tv) tv.querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    applyTranscriptView(b.dataset.view);
    tv.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  }));
  detail.querySelectorAll(".steps-toggle").forEach(b => b.addEventListener("click", () => {
    const open = b.getAttribute("aria-expanded") !== "true";
    b.setAttribute("aria-expanded", String(open));
    detail.querySelectorAll(`.msg.step[data-turn="${b.dataset.turn}"]`).forEach(m => m.classList.toggle("reveal", open));
  }));
  detail.querySelectorAll(".conv-link").forEach(a => a.addEventListener("click", e => {
    e.preventDefault(); openDetail(a.dataset.id);
  }));
  detail.querySelectorAll(".ref-toggle").forEach(b => b.addEventListener("click", async () => {
    const pane = $("detail-pane"); const scroll = pane.scrollTop;
    const moreOpen = !!detail.querySelector(".refs-more[open]");
    b.disabled = true;
    let res;
    try {
      res = await apiPost(`/api/reference/${encodeURIComponent(c.id)}`,
                          { kind: b.dataset.kind, key: b.dataset.key, linkable: b.dataset.linkable === "1" });
    } catch (e) { b.disabled = false; showError(`Reference not updated: ${e}`); return; }
    if (!res.ok) { b.disabled = false; showError(`Reference not updated (${res.status}): ${(await res.text()).slice(0, 200)}`); return; }
    if (selectedId !== c.id) { runSearch(); return; }    // the user moved on: do not reopen
    await openDetail(c.id); runSearch();
    if (moreOpen) { const d = $("detail").querySelector(".refs-more"); if (d) d.open = true; }
    pane.scrollTop = scroll;                                   // stay where the user was
  }));
  detail.querySelectorAll(".tag-chip").forEach(b => b.addEventListener("click", e => {
    const tag = b.dataset.tag === "" ? null : b.dataset.tag;
    // shift-click rejects a tag (negative example); plain click accepts it / "none"
    sendTag(c.id, b.dataset.group, tag, e.shiftKey && tag !== null ? "reject" : "accept");
  }));
  detail.querySelectorAll(".tag-clear").forEach(b => b.addEventListener("click", () =>
    sendTag(c.id, b.dataset.group, null, "clear")));               // tag null = clear the whole group, one request
  if (c.turns_supported) wireComposer(c);
  $("detail-pane").scrollTop = 0;
  // a turn still running (sent before a reload, from another tab, or before navigating away)
  // is followed again from its first event
  if (c.turns_supported && c.active_turn) followTurn(c.id, c.active_turn.turn_id, c.active_turn.edits);
}

// Linkable references as badges with links; non-linkable ones (bare "PR 123", commits, urls)
// stay behind a details toggle. The × on a badge marks it a false match (and ↩ restores).
function refLabel(r) {
  if (r.kind === "pr" || r.kind === "issue") return r.key.split("/").slice(-1)[0].replace("#", (r.kind === "pr" ? " PR #" : " #"));
  if (r.kind === "branch") return r.label || r.key.split("@").slice(-1)[0];
  return r.label || r.key;
}
function referencesHTML(c) {
  const refs = c.references || [];
  if (!refs.length) return "";
  const badge = r => {
    const label = escape(refLabel(r));
    const inner = r.url ? `<a href="${escape(r.url)}" target="_blank" rel="noopener">${label}</a>` : label;
    const title = `${escape(r.kind)} · ${escape(r.method)} · seen as “${escape(r.span || r.key)}”${r.message_seq != null ? " in message " + r.message_seq : ""}`;
    // commits and URLs never link, nor does a bare "PR 123" with no repository: no restore button
    const canLink = r.linkable || !(r.kind === "commit" || r.kind === "url" || ((r.kind === "pr" || r.kind === "issue") && !r.key.includes("/")));
    const toggle = canLink ? `<button class="ref-toggle" data-kind="${escape(r.kind)}" data-key="${escape(r.key)}" data-linkable="${r.linkable ? 0 : 1}"
        title="${r.linkable ? "Mark as a false match" : "Mark as a real link"}">${r.linkable ? "×" : "↩"}</button>` : "";
    return `<span class="ref ref-${escape(r.kind)}${r.linkable ? "" : " unlinked"}" title="${title}">${inner}${toggle}</span>`;
  };
  const linkable = refs.filter(r => r.linkable);
  const rest = refs.filter(r => !r.linkable);
  return `<div class="refs">${linkable.map(badge).join("")}` +
    (rest.length ? `<details class="refs-more"><summary>${rest.length} more (not linked)</summary>${rest.map(badge).join("")}</details>` : "") +
    `</div>`;
}

function relatedHTML(c) {
  const groups = c.related || [];
  if (!groups.length) return "";
  const rows = groups.map(g => {
    const head = g.url ? `<a href="${escape(g.url)}" target="_blank" rel="noopener">${escape(refLabel(g))}</a>` : escape(refLabel(g));
    const items = g.conversations.map(r =>
      `<div class="rel-item">${sourceBadge(r.source)}<a href="#${encodeURIComponent(r.id)}" class="conv-link" data-id="${escape(r.id)}">${escape(r.title)}</a>
        <span class="rel-meta">${escape(relTime(r.start_ts))}${r.is_resolved ? " · " + escape(r.is_resolved) : ""}</span></div>`).join("");
    return `<div class="rel-group"><div class="rel-head"><span class="tg-name">${escape(g.kind)}</span> ${head}
      <span class="rel-meta">${g.count} other conversation${g.count > 1 ? "s" : ""}</span></div>${items}</div>`;
  }).join("");
  return `<div class="related">${rows}</div>`;
}

// Per group: every tag as a toggle chip (click = accept; for exclusive groups this replaces
// the earlier pick), a "none" chip, and a clear button that drops the user's decision so the
// pipeline's own evidence shows again. Sources: user > rule > llm.
function tagEditorHTML(c) {
  const td = c.tags || {};
  const groups = td.groups || TAG_GROUPS;
  if (!groups || !Object.keys(groups).length) return "";
  const decided = {};
  (td.decisions || []).forEach(d => { (decided[d.group] = decided[d.group] || []).push(d); });
  const assigned = td.assignments || {};
  const evidence = {};
  (td.evidence || []).forEach(e => { (evidence[`${e.group}:${e.tag}`] = evidence[`${e.group}:${e.tag}`] || []).push(`${e.method}${e.detail ? " " + e.detail : ""}`); });
  const suggested = {};
  (td.suggestions || []).forEach(sg => { (suggested[`${sg.group}:${sg.tag}`] = suggested[`${sg.group}:${sg.tag}`] || []).push(sg); });
  let anySuggested = false;
  const rows = Object.entries(groups).map(([g, spec]) => {
    const current = assigned[g] || [];
    const dec = decided[g] || [];
    const none = dec.some(d => (d.tag === null || d.tag === "") && d.decision === "accept");
    const chips = spec.tags.map(t => {
      const on = current.includes(t.name);
      const rej = dec.some(d => d.tag === t.name && d.decision === "reject");
      const ev = (evidence[`${g}:${t.name}`] || []).join("; ");
      const sg = suggested[`${g}:${t.name}`];
      if (sg && !on) anySuggested = true;
      const sgTitle = sg ? ` — suggested (${sg[0].similarity.toFixed(2)} similar to “${escape(sg[0].origin_title || sg[0].origin)}”); click to accept, shift-click to reject` : "";
      return `<button class="tag-chip${on ? " on" : ""}${rej ? " rejected" : ""}${sg && !on ? " suggested" : ""}" data-group="${escape(g)}" data-tag="${escape(t.name)}"
        title="${escape(t.description || "")}${ev ? " — evidence: " + escape(ev) : ""}${sgTitle}">${escape(t.name)}${sg && !on ? " ?" : ""}</button>`;
    }).join("");
    const noneChip = `<button class="tag-chip none${none ? " on" : ""}" data-group="${escape(g)}" data-tag="" title="No tag from this group applies">none</button>`;
    const clear = dec.length ? `<button class="tag-clear" data-group="${escape(g)}" title="Drop my decision for this group and show what the pipeline decided">↺</button>` : "";
    const src = current.length ? ` <span class="tag-src">(${escape(spec.exclusive ? "one of" : "any of")}${dec.length ? ", yours" : ""})</span>` : "";
    return `<div class="tag-row"><span class="tg-name" title="${escape(spec.description || "")}">${escape(g)}</span>${src} ${chips}${noneChip}${clear}</div>`;
  }).join("");
  const help = `<div class="tag-help">Click a tag to apply it, shift-click to mark it wrong${anySuggested ? "; dashed “?” tags are suggestions from similar conversations" : ""}.</div>`;
  return `<div class="tag-editor">${rows}</div>${help}`;
}

async function sendTag(id, group, tag, decision) {
  let res;
  try {
    res = await apiPost(`/api/tag/${encodeURIComponent(id)}`, { group, tag, decision });
  } catch (e) { showError(`Tag not saved: ${e}`); return; }
  if (!res.ok) { showError(`Tag not saved (${res.status}): ${(await res.text()).slice(0, 200)}`); return; }
  // the user may have moved on while the request was in flight: never reopen a closed pane
  if (selectedId === id) await openDetail(id);
  runSearch();
}

async function toggleHide(id, hidden) {
  const res = await apiPost(`/api/hide/${encodeURIComponent(id)}?hidden=${hidden}`);
  if (!res.ok) { showError(`Could not ${hidden ? "hide" : "unhide"} (${res.status})`); return; }
  toast(hidden ? "Hidden. Turn on “Hidden” under Show to see it again." : "Visible in results again.", "success");
  await openDetail(id);   // refresh detail (button label flips)
  runSearch();            // refresh list (hidden badge / visibility)
}

// Delete asks for confirmation inline in the toolbar rather than with a modal dialog.
function askDelete(id) {
  const slot = $("confirm-slot");
  $("btn-delete").style.display = "none";
  slot.innerHTML = `<span class="confirm">Delete permanently?
    <button class="btn danger" id="confirm-del">${ICON.trash}<span class="label">Delete</span></button>
    <button class="btn ghost" id="cancel-del">Cancel</button></span>`;
  $("cancel-del").addEventListener("click", () => { slot.innerHTML = ""; $("btn-delete").style.display = ""; });
  $("confirm-del").addEventListener("click", () => deleteConv(id));
  $("confirm-del").focus();
}

async function deleteConv(id) {
  const res = await apiPost(`/api/delete/${encodeURIComponent(id)}`);
  if (!res.ok) { showError(`Could not delete (${res.status})`); return; }
  toast("Conversation deleted.", "success");
  closeDetail();
  runSearch();
}

const EMPTY_DETAIL = $("detail").innerHTML;
function closeDetail() {
  detachTurn();
  detailView++;
  document.body.classList.remove("detail-open");
  selectedId = null;
  document.querySelectorAll(".card.selected").forEach(c => c.classList.remove("selected"));
  $("detail").className = "empty";
  $("detail").innerHTML = EMPTY_DETAIL;
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
}

// Browser turns: the message box under a transcript runs one more turn of the conversation
// in the tool that owns it (server.py, turns.py) and streams what the tool does. There is one
// stream per page, owned by the detail view: opening another conversation or closing this one
// aborts the fetch, never the turn.
let turnStream = null;        // {convId, turnId, controller, seen, bubble, text, denied, finished, lost}
let detailView = 0;           // bumped whenever the detail pane is replaced: late responses check it
const turnPrompts = new Map();    // turn id -> what was typed, so a re-render can show it again

function turnHeaders(extra) {
  return Object.assign({ "X-Convo-Index": "1", "X-Convo-Index-Turn": turnToken || "" }, extra);
}

function detachTurn() {
  if (turnStream) { turnStream.controller.abort(); turnStream = null; }
}

// The Mode picker: Read-only, or Edit (files, shell commands and the allowlisted MCP tools,
// all without asking). Claude Code conversations also get a Tools button for that allowlist.
function composerHTML(c) {
  const tools = c.source === "claude_code" && turnToken;
  const send = /Mac/.test(navigator.platform) ? "⌘" : "Ctrl";
  const hint = turnToken ? `<span class="send-hint"><kbd>${send}</kbd><kbd>Enter</kbd> to send</span>`
    : "To send messages here, open the UI through the URL <code>convo-index serve</code> or <code>up</code> prints.";
  return `<div id="turn-live"></div>
    <div class="composer" id="composer">
      <div class="composer-box">
        ${tools ? `<div class="tools-panel" id="tools-panel" hidden>
          <div class="tools-head"><strong>MCP tools in Edit mode</strong><span class="hint">These run without asking. Servers come from your Claude Code config; claude.ai connectors cannot be loaded here.</span></div>
          <div id="tools-body" class="tools-body"></div>
          <div class="tools-actions"><span class="turn-status" id="tools-status" role="status"></span>
            <button class="btn ghost" id="tools-cancel" type="button">Cancel</button><button class="btn primary" id="tools-save" type="button">Save</button></div>
        </div>` : ""}
        <textarea id="turn-prompt" rows="2" aria-label="Message" placeholder="Continue this conversation in ${escape(sourceLabel(c.source))}…"${turnToken ? "" : " disabled"}></textarea>
        <div class="composer-row">
          <label class="mode-pick" title="Read-only reads files. Edit also edits files, runs shell commands and uses the allowed MCP tools, without asking.">Mode
            <select id="turn-edits"><option value="read">Read-only</option><option value="edits">Edit</option></select>
          </label>
          ${tools ? `<button class="btn ghost" id="turn-tools" type="button" aria-expanded="false" aria-controls="tools-panel" title="MCP tools Edit mode may use">${ICON.tools}<span class="label">Tools</span></button>` : ""}
          <span class="turn-status" id="turn-status" role="status">${hint}</span>
          <button class="btn danger" id="turn-stop" hidden title="Stop the tool. What it already wrote stays in the transcript.">${ICON.stop}<span class="label">Stop</span></button>
          <button class="btn primary" id="turn-send"${turnToken ? "" : " disabled"}>${ICON.send}<span class="label">Send</span></button>
        </div>
      </div>
    </div>`;
}

function wireComposer(c) {
  if ($("turn-tools")) {
    $("turn-tools").addEventListener("click", () => toggleToolsPanel());
    $("tools-cancel").addEventListener("click", () => toggleToolsPanel(false));
    $("tools-save").addEventListener("click", saveTools);
  }
  $("turn-send").addEventListener("click", () => sendTurn(c.id));
  $("turn-stop").addEventListener("click", stopTurn);
  $("turn-prompt").addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendTurn(c.id); }
  });
}

// Server messages are shown verbatim, so the status line is always set as text.
function turnStatus(text, kind) {
  const el = $("turn-status");
  if (!el) return;
  el.className = "turn-status" + (kind ? " " + kind : "");
  el.textContent = text;
}

// The allowlist panel: each configured server with "All tools" and the tools transcripts
// have called, plus free entries for the rest. Refusals from the server are shown verbatim.
async function toggleToolsPanel(open) {
  const panel = $("tools-panel"), btn = $("turn-tools");
  open = open === undefined ? panel.hidden : open;
  panel.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (!open) { btn.focus(); return; }
  const body = $("tools-body");
  body.textContent = "Loading…";
  $("tools-save").disabled = true;             // until the stored list is on screen: Save would post an empty one
  let data;
  try {
    const res = await fetch("/api/turns/tools", { headers: turnHeaders() });
    data = await res.json().catch(() => ({}));
    if (!res.ok) { body.textContent = data.error || `The server returned ${res.status}.`; return; }
  } catch (e) { body.textContent = "The server did not answer."; return; }
  if (panel.hidden) return;
  const allowed = new Set(data.allowed || []);
  const box = (entry, label) => {
    const on = allowed.delete(entry);
    return `<label class="tools-pick"><input type="checkbox" data-entry="${escape(entry)}"${on ? " checked" : ""}> ${escape(label)}</label>`;
  };
  const servers = data.servers || [];
  body.innerHTML = servers.length ? servers.map(sv => `<fieldset class="tools-server"><legend>${escape(sv.name)} <span class="hint">${escape(sv.type)}</span></legend>
      ${box(`mcp__${sv.name}__*`, "All tools")}
      ${((data.seen || {})[sv.name] || []).map(t => box(`mcp__${sv.name}__${t}`, t)).join("")}
    </fieldset>`).join("") + `<label class="tools-extra"><span>Other tools, one per line (<code>mcp__server__tool</code>)</span>
      <textarea id="tools-extra" rows="2" spellcheck="false"></textarea></label>`
    : `<p class="hint">No MCP servers in your Claude Code config. Add one with <code>claude mcp add --scope user</code>.</p>`;
  if ($("tools-extra")) $("tools-extra").value = [...allowed].join("\n");   // entries no checkbox shows
  updateToolsCount(data.allowed || []);
  $("tools-save").disabled = !servers.length;
  const first = body.querySelector("input, textarea");
  if (first) first.focus();
}

async function saveTools() {
  const entries = [...document.querySelectorAll("#tools-body input[data-entry]:checked")].map(el => el.dataset.entry)
    .concat(($("tools-extra") ? $("tools-extra").value : "").split("\n").map(x => x.trim()).filter(Boolean));
  $("tools-save").disabled = true;
  let res, body;
  try {
    res = await fetch("/api/turns/tools", { method: "POST", headers: turnHeaders({ "Content-Type": "application/json" }),
                                            body: JSON.stringify({ mcp_tools: entries }) });
    body = await res.json().catch(() => ({}));
  } catch (e) { res = null; }
  if (!$("tools-save")) return;                // the conversation was closed meanwhile
  $("tools-save").disabled = false;
  const status = $("tools-status");
  if (!res || !res.ok) { status.className = "turn-status error"; status.textContent = (body && body.error) || "The server did not answer."; return; }
  updateToolsCount(body.allowed);
  status.className = "turn-status"; status.textContent = "";
  toggleToolsPanel(false);
  toast(body.allowed.length ? `Edit mode may use ${body.allowed.length} MCP tool entr${body.allowed.length === 1 ? "y" : "ies"}.` : "Edit mode uses no MCP tools.", "success");
}

function updateToolsCount(list) {
  const label = $("turn-tools") && $("turn-tools").querySelector(".label");
  if (label) label.textContent = list.length ? `Tools · ${list.length}` : "Tools";
}

function setComposerBusy(busy) {
  if (!$("turn-send")) return;
  $("turn-send").disabled = busy || !turnToken;
  $("turn-edits").disabled = busy;
  $("turn-stop").hidden = !busy;
  $("turn-stop").disabled = false;
}

function showEdits(on) {
  if ($("turn-edits")) $("turn-edits").value = on ? "edits" : "read";
}

// The live output sits just above the pinned message box: keep it in view while the reader is
// already at the bottom, without pulling them down while they read further up.
function followLive(force) {
  const pane = $("detail-pane");
  if (force || pane.scrollHeight - pane.scrollTop - pane.clientHeight < 160) pane.scrollTop = pane.scrollHeight;
}

async function activeTurn(convId) {
  try {
    const res = await fetch("/api/conversation/" + encodeURIComponent(convId));
    return res.ok ? (await res.json()).active_turn : null;
  } catch (e) { return null; }
}

async function stopTurn() {
  const s = turnStream;
  if (!s || s.finished) return;
  $("turn-stop").disabled = true;
  turnStatus("Stopping…", "busy");
  let res = null, body = {};
  try {
    res = await fetch(`/api/turn/${encodeURIComponent(s.turnId)}`, { method: "DELETE", headers: turnHeaders() });
    if (!res.ok) body = await res.json().catch(() => ({}));
  } catch (e) { /* reported below */ }
  if (turnStream !== s || s.finished) return;  // the terminal event says how it ended
  if (res && res.ok) {
    // with the stream lost no terminal event will arrive: the stop is the end
    if (s.lost) finishTurn(s, { kind: "error", message: "stopped" });
    return;
  }
  $("turn-stop").disabled = false;
  turnStatus(`Could not stop the turn: ${body.error || (res ? res.status : "the server did not answer")}`, "error");
}

async function sendTurn(convId) {
  const box = $("turn-prompt"), prompt = box.value, edits = $("turn-edits").value === "edits";
  if (!prompt.trim() || $("turn-send").disabled) return;
  const view = detailView;
  box.value = "";                              // what is typed from here on is the next message
  $("turn-send").disabled = true;              // Stop appears once the turn has an id to stop
  $("turn-edits").disabled = true;
  turnStatus("Starting…", "busy");
  const restore = (message) => {
    if (!box.value) box.value = prompt;
    setComposerBusy(false);
    turnStatus(message, "error");
  };
  let res = null, body = null;
  try {
    res = await fetch(`/api/conversation/${encodeURIComponent(convId)}/turn`, {
      method: "POST", headers: turnHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ prompt, edits }),
    });
    body = await res.json().catch(() => null);
  } catch (e) { /* no answer: the turn may have started anyway */ }
  if (view !== detailView) return;             // reopening the conversation picks the turn up
  if (res && res.ok && body && body.turn_id) {
    turnPrompts.set(body.turn_id, prompt);
    return followTurn(convId, body.turn_id, edits);
  }
  if (res && !res.ok) return restore((body && body.error) || `The server returned ${res.status}.`);
  const active = await activeTurn(convId);     // the answer was lost: did the turn start?
  if (view !== detailView) return;
  if (active) {
    turnPrompts.set(active.turn_id, prompt);
    return followTurn(convId, active.turn_id, active.edits);
  }
  restore("The server did not answer, and no turn is running. Send again to retry.");
}

// Reads the turn's server-sent events. A dropped connection resumes from the last event seen
// (`after`), so nothing is shown twice; retries back off and start over once events flow again.
async function followTurn(convId, turnId, edits) {
  if (turnStream && turnStream.turnId === turnId && !turnStream.finished) return;   // already following it
  detachTurn();
  const s = { convId, turnId, controller: new AbortController(), seen: 0, bubble: null, text: "", denied: [], finished: false, lost: false };
  turnStream = s;
  setComposerBusy(true);
  if (edits !== undefined) showEdits(!!edits);
  turnStatus("Running…", "busy");
  if (turnPrompts.has(turnId)) liveAppend(liveMsg("user", "You", renderMessageBody(turnPrompts.get(turnId))));
  followLive(true);
  let failures = 0;
  while (!s.finished && turnStream === s) {
    const before = s.seen;
    try {
      const res = await fetch(`/api/turn/${encodeURIComponent(turnId)}?after=${s.seen}`,
                              { headers: turnHeaders(), signal: s.controller.signal });
      if (turnStream !== s) return;
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        if (turnStream !== s) return;
        return finishTurn(s, { kind: "error", message: body.error || `The server returned ${res.status}.` });
      }
      await readTurnEvents(res.body.getReader(), s);
    } catch (e) {
      if (s.controller.signal.aborted) return;
    }
    if (turnStream !== s || s.finished) return;
    failures = s.seen > before ? 0 : failures + 1;
    if (failures > 5) {
      s.lost = true;                           // Stop still works; the turn keeps running
      turnStatus("Lost the connection to the turn. It keeps running on the server; reopen the conversation to follow it.", "error");
      return;
    }
    if (failures) await new Promise(r => setTimeout(r, 250 * 2 ** failures));
  }
}

// Server-sent events, framed by a blank line; LF, CRLF and CR line ends are all accepted.
async function readTurnEvents(reader, s) {
  const decoder = new TextDecoder();
  let buf = "", cr = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done || turnStream !== s) return;
    let chunk = decoder.decode(value, { stream: true });
    if (cr && chunk.startsWith("\n")) chunk = chunk.slice(1);    // the CRLF was split across chunks
    cr = chunk.endsWith("\r");
    buf += chunk.replace(/\r\n?/g, "\n");
    let cut;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      let id = null, data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("id: ")) id = Number(line.slice(4));
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (!data) continue;                     // a keepalive comment
      if (id !== null) s.seen = id;            // the id is the count of events seen
      turnEvent(s, JSON.parse(data));
      if (s.finished) return;
    }
  }
}

function liveMsg(cls, role, bodyHTML, kind) {
  const el = document.createElement("div");
  el.className = `msg ${cls} live`;
  el.innerHTML = `<div class="msg-head"><span class="role-tag">${escape(role)}</span>${kind ? `<span class="harness-kind">${escape(kind)}</span>` : ""}</div>
    <div class="msg-body">${bodyHTML}</div>`;
  return el;
}
function liveAppend(el) {
  const live = $("turn-live");
  if (!live) return;
  live.appendChild(el);
  followLive();
}
// Tool activity is one collapsed line, like in the transcript; the next text starts a new bubble.
function liveStep(s, cls, role, summary, detail, kind) {
  s.bubble = null;
  const body = `<details><summary>${escape(summary.slice(0, 140))}</summary>${detail ? `<pre>${escape(detail)}</pre>` : ""}</details>`;
  liveAppend(liveMsg(cls, role, body, kind));
}

function turnEvent(s, ev) {
  const show = x => typeof x === "string" ? x : JSON.stringify(x, null, 2);
  switch (ev.kind) {
    case "text":
      if (!s.bubble) { s.bubble = liveMsg("assistant", "Assistant", ""); s.text = ""; liveAppend(s.bubble); }
      s.text += (s.text ? "\n\n" : "") + ev.text;
      s.bubble.querySelector(".msg-body").innerHTML = renderMessageBody(s.text);
      followLive();
      return;
    case "tool":
      return liveStep(s, "assistant tool_call", "Assistant", ev.name || "tool", show(ev.input));
    case "tool_result":
      return liveStep(s, "tool tool_result", "Tool", firstLine(ev.text) || (ev.error ? "failed" : "(no output)"), ev.text);
    case "denied":
      s.denied.push(ev.name || "tool");
      return liveStep(s, "harness", "Harness", `${ev.name || "tool"}: finish this step in the terminal`, show(ev.input), "denied");
    case "gap":
      return liveStep(s, "harness", "Harness", `${ev.missed} earlier event${ev.missed === 1 ? " was" : "s were"} dropped from the buffer`, "", "gap");
    case "done":
    case "error":
      return finishTurn(s, ev);
  }
}

// Whatever the tool wrote is indexed after every exit, a stopped or failed turn included, so
// the conversation reloads whenever the index has it; the status line and the tool's stderr
// (neither is in the transcript) are put back after the reload.
async function finishTurn(s, ev) {
  s.finished = true;
  setComposerBusy(false);
  // what the tool reports is priced at API rates; on a subscription nothing is charged per turn
  const cost = ev.cost_usd != null ? ` · ≈ ${fmtUsd(ev.cost_usd)} API-equivalent` : "";
  const failed = ev.kind === "error";
  const denied = [...new Set(s.denied)];
  let status = failed ? `${ev.message === "stopped" ? "Stopped." : ev.message || "The turn failed."}${cost}` : `Done${cost}.`;
  if (denied.length) status += ` Not allowed here: ${denied.join(", ")}; finish that step in the terminal.`;
  const kind = failed ? "error" : denied.length ? "warn" : "";
  const stderr = () => { if (ev.stderr) liveStep(s, "harness", "Harness", firstLine(ev.stderr), ev.stderr, "stderr"); };
  if (!ev.indexed) {
    stderr();
    turnStatus(ev.reason ? `${status} Not in the transcript yet: ${ev.reason}` : status, kind || "warn");
    return;
  }
  if (!failed) toast(`Turn finished${cost}.`, "success");
  runSearch();
  if (turnStream !== s) return;                // the user moved on
  const draft = $("turn-prompt") ? $("turn-prompt").value : "";
  await openDetail(s.convId);                  // the new messages are indexed by now
  if (selectedId !== s.convId || !$("composer")) return;
  if (draft && !$("turn-prompt").value) $("turn-prompt").value = draft;
  stderr();
  turnStatus(status, kind);
  followLive(true);
  $("turn-prompt").focus();
}

// Keyboard: ↑/↓ or j/k move through results, Enter opens, Esc closes, / focuses search.
function moveSelection(delta) {
  if (!currentIds.length) return;
  const idx = currentIds.indexOf(selectedId);
  const next = idx < 0 ? (delta > 0 ? 0 : currentIds.length - 1) : Math.min(currentIds.length - 1, Math.max(0, idx + delta));
  if (next === idx) return;
  openDetail(currentIds[next]);
}
document.addEventListener("keydown", e => {
  const tag = (e.target.tagName || "").toLowerCase();
  // a select uses the arrow keys and typed letters itself, like a text field
  const typing = tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable;
  // Chrome clears an <input type=search> on Escape; blur instead so the query survives.
  if (e.key === "Escape") {
    const filters = $("filters-panel");
    if (!filters.hidden && (!typing || filters.contains(e.target))) setFiltersOpen(false, true);   // the panel first
    else if (typing) { e.preventDefault(); e.target.blur(); }
    else if (NARROW.matches && $("detail").classList.contains("details-open")) toggleDetailsPanel(false);   // the drawer first
    else closeDetail();
    return;
  }
  if (typing) {
    if (e.target.id === "q" && e.key === "ArrowDown") { e.preventDefault(); moveSelection(1); }
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "/") { e.preventDefault(); $("q").focus(); $("q").select(); return; }
  if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); moveSelection(1); }
  else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); moveSelection(-1); }
});

$("q").addEventListener("input", () => { $("search-box").classList.toggle("has-value", !!$("q").value); debouncedSearch(); });
$("q-clear").addEventListener("click", () => { $("q").value = ""; $("search-box").classList.remove("has-value"); $("q").focus(); runSearch(); });
document.querySelectorAll("#mode button").forEach(b => b.addEventListener("click", () => { if (!b.disabled) { setMode(b.dataset.mode); runSearch(); } }));
wireChips(document.querySelector('#filters .chip-group[aria-label="Options"]'));
$("clear-filters").addEventListener("click", clearFilters);
function setFiltersOpen(open, refocus) {
  $("filters-panel").hidden = !open;
  $("filters-toggle").setAttribute("aria-expanded", String(open));
  if (refocus) $("filters-toggle").focus();
}
$("filters-toggle").addEventListener("click", () => setFiltersOpen($("filters-panel").hidden));
$("refresh").addEventListener("click", async () => {
  const btn = $("refresh");
  btn.disabled = true;
  btn.querySelector("svg").classList.add("spin");
  btn.querySelector(".label").textContent = "Refreshing…";
  try {
    const r = await (await apiPost("/api/refresh")).json();
    if (r.busy) toast("Another refresh is already running.", "info");
    else if (!r.ok) showError("Refresh finished with failures. See the server log.");
    else {
      const n = r.totals ? r.totals.indexed : 0;
      toast(n ? `Indexed ${n} new or changed conversation${n === 1 ? "" : "s"}.` : "Index is up to date.", "success");
      $("status").textContent = `Refreshed ${new Date().toLocaleTimeString(undefined, { timeStyle: "short" })}`;
    }
  } catch (e) { showError("Refresh failed: " + e); }
  btn.disabled = false;
  btn.querySelector("svg").classList.remove("spin");
  btn.querySelector(".label").textContent = "Refresh index";
  await loadSources();
  runSearch();
});

// The turn token arrives in the fragment of the URL `serve` and `up` print (#turns=<token>).
// It is kept for this tab (sessionStorage outlives a reload, never leaves the browser) and
// taken out of the address bar before the fragment is read as a conversation id.
let turnToken = null;
try { turnToken = sessionStorage.getItem("turnToken"); } catch (e) { /* storage blocked */ }
function takeTurnToken() {
  const m = /^#turns=([A-Za-z0-9_-]+)$/.exec(location.hash);
  if (!m) return false;
  turnToken = m[1];
  try { sessionStorage.setItem("turnToken", turnToken); } catch (e) { /* kept in memory only */ }
  history.replaceState(null, "", location.pathname + location.search);
  updateStopButton();
  return true;
}

// Stopping the server takes the turn token too: a tab not opened through the printed URL
// cannot stop it. Running turns end first; the page then says how to start it again.
function updateStopButton() {
  const btn = $("stop-server");
  btn.disabled = !turnToken;
  btn.title = turnToken ? "Stop the convo-index server"
    : "To stop the server here, open the UI through the URL `convo-index serve` or `up` prints (or run `convo-index stop`)";
}
$("stop-server").addEventListener("click", async () => {
  if (!confirm("Stop the convo-index server? Running browser turns end. Start it again with `convo-index serve`.")) return;
  const btn = $("stop-server");
  btn.disabled = true;
  try {
    const r = await fetch("/api/shutdown", { method: "POST", headers: { "X-Convo-Index": "1", "X-Convo-Index-Turn": turnToken } });
    if (r.status !== 202) {
      const body = await r.json().catch(() => ({}));
      showError("The server refused to stop: " + (body.error || r.status));
      btn.disabled = false;
      return;
    }
    document.body.innerHTML = `<main class="server-stopped"><h1>Server stopped</h1>
      <p>Start it again with <code>convo-index serve</code>, or it starts at your next login if the service is installed.</p></main>`;
  } catch (e) {
    showError("Could not stop the server: " + e);
    btn.disabled = false;
  }
});

// Back/forward and hand-edited hashes open the named conversation; an empty hash closes it.
window.addEventListener("hashchange", () => {
  if (takeTurnToken()) return;
  let id = location.hash.slice(1);
  try { id = decodeURIComponent(id); } catch (e) { /* use as-is */ }
  if (!id) { if (selectedId) closeDetail(); return; }
  if (id !== selectedId) openDetail(id);
});

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
const debouncedSearch = debounce(runSearch, 220);

// Deep link: open the conversation named in the URL hash (bookmarkable / shareable).
// Also honor ?q= / ?category= / ?source= / ?project= (used by the analytics page links).
async function init() {
  takeTurnToken();
  updateStopButton();
  applyTranscriptView(transcriptViewPref());
  const p = new URLSearchParams(location.search);
  if (p.get("q")) { $("q").value = p.get("q"); $("search-box").classList.add("has-value"); }
  tagFiltersFromUrl = p.getAll("tag");
  if (p.get("category")) tagFiltersFromUrl.push("workspace:" + p.get("category"));   // legacy links
  if (p.get("source")) sourceFilterFromUrl = p.get("source");
  if (p.get("project")) projectFilter = p.get("project");
  if (p.get("mode")) setMode(p.get("mode"));
  $("results").innerHTML = skeletons(6);
  await Promise.all([loadConfig(), loadSources()]);
  await runSearch();
  let id = location.hash.slice(1);
  try { id = decodeURIComponent(id); } catch (e) { /* malformed escape: use as-is */ }
  if (id) openDetail(id);
}
init();
