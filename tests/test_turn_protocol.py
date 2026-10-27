"""The turn registry in sources.base: which sources can run a browser turn, and the id guard
that keeps anything but a plain session id off the harness command line."""
import pytest

from convo_index.sources import base


def test_sources_without_a_turn_module_are_unsupported():
    assert base.supports_turns("codex:abc") is False
    assert base.turn_command("codex:abc", "hi", "/x") is None
    assert base.turn_events("codex:abc", '{"type": "text"}') == []
    assert base.last_activity("codex:abc", "/nowhere") is None


def test_registered_sources_are_supported(monkeypatch):
    monkeypatch.setattr(base, "is_windows", lambda: False)
    assert base.supports_turns("claude_code:0f1e2d3c-1234-4abc-8def-0123456789ab") is True
    assert base.supports_turns("opencode:ses_abc123") is True
    assert base.turn_launcher("codex:abc") is None


@pytest.mark.parametrize("native", ["", "a b", "x;rm -rf /", "$(id)", "id'quote", "é", "a/b", "`x`", "-x", "--help", "."])
def test_ids_that_are_not_plain_text_are_refused(native):
    cid = f"claude_code:{native}"
    assert base.supports_turns(cid) is False
    if native:
        with pytest.raises(ValueError):
            base.turn_command(cid, "hi", "/x")
    assert base.last_activity(cid, "/x") is None


def test_real_ids_pass():
    for native in ("0f1e2d3c-1234-4abc-8def-0123456789ab", "ses_e838634eeffe20JjGTPVFXPr2Z", "01a022b3-44ab-718e-8136-e4a0753d0242"):
        assert base.NATIVE_ID_RE.fullmatch(native)
