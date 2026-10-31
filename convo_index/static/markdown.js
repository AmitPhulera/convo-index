// Transcript text formatting, kept apart from app.js so it can be tested in node: it uses
// `escape` from common.js and touches no DOM.

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

if (typeof module !== "undefined") module.exports = { formatBody, inlineMarkdown };
