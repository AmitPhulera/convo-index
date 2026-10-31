// Markdown for transcript text: fenced ```code``` blocks, the block elements agents write
// (pipe tables, bullet and numbered lists with nesting and task boxes, quotes, rules) and the
// inline subset (code, headings, bold, italics, strikethrough, links). Everything is escaped
// first (`escape` from common.js), so the patterns below only ever see text. No DOM access:
// tests load this file in node.

// `blocks` is false for tool calls and results, whose `- ` and `|` lines are diffs and
// listings rather than prose: those keep the inline subset only.
function formatBody(text, blocks = true) {
  const parts = (text || "").split("```");
  return parts.map((p, i) => {
    if (i % 2 === 1) {
      const code = p.replace(/^[a-zA-Z0-9_+-]*\n/, ""); // drop optional language line
      return `<pre><code>${escape(code)}</code></pre>`;
    }
    return blocks ? blockMarkdown(escape(p)) : inlineMarkdown(escape(p));
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
  t = t.replace(/~~([^\s~][^~\n]*?)~~/g, "<del>$1</del>");
  t = t.replace(/\[([^\]\n]+)\]\((https?:[^)\s]+)\)/g,
    (_, label, href) => `<a href="${href}" target="_blank" rel="noopener">${label}</a>`);
  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => code[Number(i)]);
}

const MD_LIST = /^( *)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
const MD_RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const MD_QUOTE = /^ {0,3}&gt; ?(.*)$/;                 // `>` arrives escaped
const MD_QUOTE_DEPTH = 8;                              // deeper quotes render as text, not recursion
const MD_HEADING = /^ {0,3}#{1,6}[ \t]/;
const MD_TABLE_SEP = /^[ \t]*\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;

// Cells of one table row: split on `|` outside inline code, `\|` is a literal bar.
function tableCells(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  const cells = [];
  let cur = "", inCode = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === "`") inCode = !inCode;
    if (c === "\\" && s[i + 1] === "|") { cur += "|"; i++; continue; }
    if (c === "|" && !inCode) { cells.push(cur.trim()); cur = ""; continue; }
    cur += c;
  }
  cells.push(cur.trim());
  return cells;
}

// A table starts at `i` when that line has a bar and the next is a separator row with as
// many cells (GFM's rule; without it, a bar in prose stays prose).
function isTableStart(lines, i) {
  if (i + 1 >= lines.length || !lines[i].includes("|") || !MD_TABLE_SEP.test(lines[i + 1])) return false;
  if (!lines[i + 1].includes("|") && !lines[i].trim().startsWith("|")) return false;   // `a | b` over `---` is a rule
  return tableCells(lines[i]).length === tableCells(lines[i + 1]).length;
}

function tableHTML(rows) {
  const head = tableCells(rows[0]);
  const align = tableCells(rows[1]).map(c => c.startsWith(":") && c.endsWith(":") ? "md-c" : c.endsWith(":") ? "md-r" : "");
  const cell = (tag, txt, k) => `<${tag}${align[k] ? ` class="${align[k]}"` : ""}>${inlineMarkdown(txt)}</${tag}>`;
  const body = rows.slice(2).map(r => {
    const cells = tableCells(r);
    return `<tr>${head.map((_, k) => cell("td", cells[k] || "", k)).join("")}</tr>`;
  }).join("");
  return `<div class="md-table"><table><thead><tr>${head.map((h, k) => cell("th", h, k)).join("")}</tr></thead>` +
    `<tbody>${body}</tbody></table></div>`;
}

// Items are {indent, ordered, start, text}; deeper indents nest inside the open item.
function listHTML(items) {
  const stack = [];
  let out = "";
  const open = it => it.ordered ? `<ol${it.start !== 1 ? ` start="${it.start}"` : ""}>` : "<ul>";
  for (const it of items) {
    const tag = it.ordered ? "ol" : "ul";
    while (stack.length && it.indent < stack[stack.length - 1].indent) out += `</li></${stack.pop().tag}>`;
    const top = stack[stack.length - 1];
    if (top && it.indent === top.indent && top.tag === tag) out += "</li>";
    else {
      if (top && it.indent === top.indent) out += `</li></${stack.pop().tag}>`;   // `-` then `1.` at one level
      out += open(it);
      stack.push({ indent: it.indent, tag });
    }
    const task = it.text.match(/^\[([ xX])\][ \t]+(.*)$/);
    out += task
      ? `<li class="md-task"><input type="checkbox" disabled${task[1] === " " ? "" : " checked"}> ${inlineMarkdown(task[2])}`
      : `<li>${inlineMarkdown(it.text)}`;
  }
  while (stack.length) out += `</li></${stack.pop().tag}>`;
  return out;
}

function blockStart(lines, i) {
  return isTableStart(lines, i) || MD_RULE.test(lines[i]) || MD_LIST.test(lines[i]) || MD_QUOTE.test(lines[i]) ||
    MD_HEADING.test(lines[i]);
}

// `escaped` is HTML-escaped text without fenced code. Prose keeps its line breaks (the
// message body is pre-wrap); blank lines next to a block are dropped, the block's own
// margins space it. `depth` counts enclosing quotes.
function blockMarkdown(escaped, depth = 0) {
  const lines = escaped.split("\n");
  const out = [];          // {html} for blocks, {text: [lines]} for prose
  let prose = [];
  const flush = () => { if (prose.length) out.push({ text: prose }); prose = []; };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (isTableStart(lines, i)) {
      const rows = [line, lines[i + 1]];
      i += 2;
      // rows run to a blank line, a line without a bar, or the start of another block (GFM)
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() && !blockStart(lines, i)) rows.push(lines[i++]);
      flush(); out.push({ html: tableHTML(rows) });
    } else if (MD_RULE.test(line)) {
      flush(); out.push({ html: "<hr>" }); i++;
    } else if (MD_QUOTE.test(line)) {
      const inner = [];
      while (i < lines.length && MD_QUOTE.test(lines[i])) inner.push(lines[i++].match(MD_QUOTE)[1]);
      const body = depth < MD_QUOTE_DEPTH ? blockMarkdown(inner.join("\n"), depth + 1) : inlineMarkdown(inner.join("\n"));
      flush(); out.push({ html: `<blockquote>${body}</blockquote>` });
    } else if (MD_LIST.test(line)) {
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(MD_LIST);
        if (m) {
          const marker = m[2];
          items.push({ indent: m[1].length, ordered: /\d/.test(marker), start: parseInt(marker, 10) || 1, text: m[3] });
          i++;
        } else if (lines[i].trim() && items.length && (/^[ \t]/.test(lines[i]) || !blockStart(lines, i))) {
          items[items.length - 1].text += " " + lines[i].trim();      // continuation of the item above
          i++;
        } else if (!lines[i].trim() && i + 1 < lines.length && MD_LIST.test(lines[i + 1])) {
          i++;                                                         // a blank line between items
        } else break;
      }
      flush(); out.push({ html: listHTML(items) });
    } else {
      prose.push(line); i++;
    }
  }
  flush();
  return out.map((b, k) => {
    if (b.html) return b.html;
    let t = b.text;
    if (k > 0) while (t.length && !t[0].trim()) t = t.slice(1);                  // after a block
    if (k < out.length - 1) while (t.length && !t[t.length - 1].trim()) t = t.slice(0, -1);   // before one
    return inlineMarkdown(t.join("\n"));
  }).join("");
}

if (typeof module !== "undefined") module.exports = { formatBody, inlineMarkdown, blockMarkdown, tableCells };
