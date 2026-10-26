from convo_index import summarize as sm
from convo_index.config import SUMMARY_PROMPT_PREFIX
from convo_index.providers.base import LLMResult


def test_prompt_starts_with_marker_so_adapters_exclude_our_sessions():
    assert sm._PROMPT.startswith(SUMMARY_PROMPT_PREFIX)


def test_summarize_parses_and_fills_fields(llm_settings, fake_llm):
    f = fake_llm({"problem": "Fix bug", "solution": "Patched X", "is_resolved": "Yes",
                  "learning": "Y", "claude_failed": "No"})
    s, res = sm.summarize_conversation([{"role": "user", "text": "hi"}], "T", llm_settings)
    assert s["problem"] == "Fix bug" and s["is_resolved"] == "Yes" and s["claude_failed"] == "No"
    assert res.ok and f.calls[0]["job"] == "summary" and f.calls[0]["schema"] == sm.SCHEMA
    assert "Title: T" in f.calls[0]["prompt"] and "USER: hi" in f.calls[0]["prompt"]


def test_summarize_returns_none_and_result_on_failure(llm_settings, fake_llm):
    fake_llm(LLMResult(ok=False, error="rate_limit", detail="429"))
    s, res = sm.summarize_conversation([{"role": "user", "text": "hi"}], "T", llm_settings)
    assert s is None and res.error == "rate_limit"


def test_summarize_none_for_empty(llm_settings):
    assert sm.summarize_conversation([], "T", llm_settings) == (None, None)


def test_build_transcript_truncates(settings):
    settings["summary"]["head_chars"] = 10
    settings["summary"]["tail_chars"] = 10
    msgs = [{"role": "user", "text": "x" * 100}]
    t = sm.build_transcript(msgs, settings)
    assert "truncated" in t and len(t) < 100


def test_build_transcript_with_zero_tail(settings):
    settings["summary"]["head_chars"] = 10
    settings["summary"]["tail_chars"] = 0
    t = sm.build_transcript([{"role": "user", "text": "x" * 100}], settings)
    assert t.startswith("USER: xxxx") and t.endswith("…\n\n") and len(t) < 60
