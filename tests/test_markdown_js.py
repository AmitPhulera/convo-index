"""The transcript Markdown renderer (static/markdown.js), run in node with the page's own
`escape` from common.js. Skipped where node is not installed."""
import json
import re
import shutil
import subprocess

import pytest

from convo_index.config import STATIC_DIR

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const fs = require("fs");
const common = fs.readFileSync(process.argv[1], "utf8");
globalThis.escape = new Function(common.match(/function escape\(s\) \{[\s\S]*?\n\}/)[0] + "; return escape;")();
const md = require(process.argv[2]);
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map(([text, blocks]) => md.formatBody(text, blocks))));
"""


def render(*texts, blocks=True):
    out = subprocess.run([NODE, "-e", HARNESS, str(STATIC_DIR / "common.js"), str(STATIC_DIR / "markdown.js")],
                         input=json.dumps([[t, blocks] for t in texts]), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def squash(html):
    return re.sub(r">\s+<", "><", html)


def test_a_table_with_alignment_renders_with_header_cells_and_classes():
    (html,) = render("| Item | Qty | Price |\n|:-----|:---:|------:|\n| apple | 2 | $1 |\n| pear | 10 | $3 |")
    assert html.startswith('<div class="md-table"><table><thead><tr><th>Item</th><th class="md-c">Qty</th>'
                           '<th class="md-r">Price</th></tr></thead>')
    assert "<tr><td>apple</td><td class=\"md-c\">2</td><td class=\"md-r\">$1</td></tr>" in html
    assert html.count("<tr>") == 3


def test_a_bar_inside_inline_code_or_escaped_stays_in_its_cell():
    (html,) = render("| cmd | note |\n|---|---|\n| `a | b` | x \\| y |")
    assert "<td><code>a | b</code></td><td>x | y</td>" in html


def test_bars_in_prose_or_without_a_separator_row_are_not_a_table():
    a, b, c = render("use a | b to pipe", "| a | b |\n| c | d |", "| a | b |\n|---|---|---|")
    assert "<table" not in a + b + c
    assert b == "| a | b |\n| c | d |"


def test_a_table_ends_where_another_block_starts():
    (html,) = render("| Key |\n| --- |\n| value |\n## Run a | b | c\n- item | x")
    assert html.count("<tr>") == 2
    assert '</table></div><strong class="md-h">Run a | b | c</strong><ul><li>item | x</li></ul>' in html


def test_rows_are_padded_to_the_header_and_inline_markdown_works_in_cells():
    (html,) = render("| a | b |\n|---|---|\n| **x** |")
    assert "<tr><td><strong>x</strong></td><td></td></tr>" in html


def test_markup_in_a_cell_is_escaped():
    (html,) = render('| a |\n|---|\n| <img src=x onerror=alert(1)> |')
    assert "<img" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html


def test_a_table_inside_a_fenced_block_stays_code():
    (html,) = render("```\n| a | b |\n|---|---|\n```")
    assert html == "<pre><code>| a | b |\n|---|---|\n</code></pre>"


def test_nested_lists_keep_their_order_and_start_number():
    (html,) = render("- one\n- two\n  - two.a\n  - two.b\n- three\n\n3. third\n4. fourth")
    assert squash(html) == ("<ul><li>one</li><li>two<ul><li>two.a</li><li>two.b</li></ul></li><li>three</li></ul>"
                            '<ol start="3"><li>third</li><li>fourth</li></ol>')


def test_a_list_then_a_paragraph_and_continuation_lines():
    (html,) = render("Steps:\n\n1. Build it\n   with care\n2. Ship it\n\nDone.")
    assert html == 'Steps:<ol><li>Build it with care</li><li>Ship it</li></ol>Done.'


def test_task_items_quotes_rules_and_strikethrough():
    (html,) = render("- [x] done\n- [ ] todo\n\n> quoted **bold**\n> more\n\n---\n~~old~~ new")
    assert '<li class="md-task"><input type="checkbox" disabled checked> done</li>' in html
    assert '<li class="md-task"><input type="checkbox" disabled> todo</li>' in html
    assert "<blockquote>quoted <strong>bold</strong>\nmore</blockquote>" in html
    assert "<hr><del>old</del> new" in html


def test_a_heading_ends_a_list():
    (html,) = render("- a\n## Next")
    assert squash(html) == '<ul><li>a</li></ul><strong class="md-h">Next</strong>'


def test_tool_output_keeps_its_dash_and_bar_lines_as_text():
    (html,) = render("- removed\n+ added\n| a | b |\n|---|---|", blocks=False)
    assert "<ul>" not in html and "<table" not in html and html.startswith("- removed\n+ added")


def test_prose_without_blocks_renders_as_before():
    (html,) = render("Hello **there**,\nsee `x` and [docs](https://example.com).\n\nBye.")
    assert html == ('Hello <strong>there</strong>,\nsee <code>x</code> and '
                    '<a href="https://example.com" target="_blank" rel="noopener">docs</a>.\n\nBye.')
