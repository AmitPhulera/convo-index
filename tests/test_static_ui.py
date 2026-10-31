"""Static UI checks that need no browser."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "convo_index" / "static"
FILES = ["index.html", "analytics.html", "app.js", "common.js", "analytics.js"]
BUTTON = re.compile(r"<button\b[^>]*>", re.S)


def _buttons():
    for name in FILES:
        for tag in BUTTON.findall((STATIC / name).read_text(encoding="utf-8")):
            yield name, tag


def test_icon_only_buttons_have_a_tooltip():
    # A button that shows only an icon (or a bare ×) has nothing for a mouse user to read but
    # its title; aria-label alone reaches screen readers, not the pointer.
    icon_only = [(n, t) for n, t in _buttons()
                 if "icon-only" in t or 'id="q-clear"' in t or 'class="active-chip"' in t]
    assert icon_only, "the pattern no longer matches any button; update the test"
    missing = [f"{n}: {t}" for n, t in icon_only if "title=" not in t]
    assert not missing, "icon-only buttons without a title:\n" + "\n".join(missing)
