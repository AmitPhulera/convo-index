"""pi coding agent adapter against synthetic session files shaped like the real ones."""
import json

import pytest

from convo_index.model import KIND_TEXT, KIND_THINKING, KIND_TOOL_CALL, KIND_TOOL_RESULT
from convo_index.sources import pi
from convo_index.sources.base import UnreadableSource

SID = "01a0aaaa-1111-7222-8333-444455556666"
FILE = f"2026-08-21T05-02-49-003Z_{SID}.jsonl"
CWD = "/Users/alice/Development/globex/widgets-hq"


def _entry(typ, eid, parent, ts, **kw):
    d = {"type": typ, "id": eid, "parentId": parent, "timestamp": ts}
    d.update(kw)
    return d


def _ts(n):
    return f"2026-08-21T05:03:{n:02d}.000Z"


def header(sid=SID, cwd=CWD):
    return {"type": "session", "version": 3, "id": sid, "timestamp": "2026-08-21T05:02:49.003Z", "cwd": cwd}


def default_entries():
    usage = {"input": 1797, "output": 124, "cacheRead": 10, "cacheWrite": 5, "reasoning": 7, "totalTokens": 1943,
             "cost": {"input": 0.001, "output": 0.002, "cacheRead": 0, "cacheWrite": 0, "total": 0.003}}
    return [
        header(),
        _entry("model_change", "e1", None, _ts(0), provider="anthropic", modelId="claude-sonnet-5-5"),
        _entry("thinking_level_change", "e2", "e1", _ts(0), thinkingLevel="medium"),
        _entry("message", "e3", "e2", _ts(1),
               message={"role": "user", "content": [{"type": "text", "text": "Fix the SAAS-42 bug"}], "timestamp": 1}),
        _entry("message", "e4", "e3", _ts(2),
               message={"role": "assistant", "provider": "anthropic", "model": "claude-sonnet-5-5", "api": "anthropic-messages",
                        "usage": usage, "stopReason": "toolUse", "responseId": "resp-1", "timestamp": 2,
                        "content": [{"type": "thinking", "thinking": "Need the file first.", "thinkingSignature": "sig"},
                                    {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "/x/app.py"}}]}),
        _entry("message", "e5", "e4", _ts(3),
               message={"role": "toolResult", "toolCallId": "call_1", "toolName": "read", "isError": False, "timestamp": 3,
                        "content": [{"type": "text", "text": "def main(): pass"}]}),
        _entry("message", "e6", "e5", _ts(4),
               message={"role": "assistant", "provider": "anthropic", "model": "claude-sonnet-5-5", "api": "anthropic-messages",
                        "usage": usage, "stopReason": "stop", "responseId": "resp-2", "timestamp": 4,
                        "content": [{"type": "text", "text": "Found it."}, {"type": "text", "text": "The bug is in main()."}]}),
    ]


def write_session(directory, entries=None, name=FILE):
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / name
    f.write_text("".join(json.dumps(e) + "\n" for e in (entries if entries is not None else default_entries())),
                 newline="\n")
    return f


def test_native_id_prefers_the_header_then_the_file_name(tmp_path):
    f = write_session(tmp_path, [header("header-id")], name="custom.jsonl")   # `pi --session <path>` keeps any name
    assert pi.native_id_of(f) == "header-id"
    assert pi.to_conversation(pi.parse_session(f), f).native_id == "header-id"
    assert pi.native_id_of(write_session(tmp_path, [], name=FILE)) == SID
    assert pi.native_id_of(write_session(tmp_path, [], name="plain.jsonl")) == "plain"
    assert pi.native_id_of(tmp_path / "missing.jsonl") == "missing"
    # pi skips blank and unparseable leading lines and allows a header longer than 4 KiB
    f = tmp_path / "custom2.jsonl"
    f.write_text("\n\nNOT JSON\n" + json.dumps(dict(header("spaced-id"), padding="x" * 5000)) + "\n")
    assert pi.native_id_of(f) == "spaced-id"
    f.write_text(json.dumps(_entry("message", "e1", None, _ts(1), message={"role": "user", "content": "no header"})) + "\n"
                 + json.dumps(header("late-header")) + "\n")
    assert pi.native_id_of(f) == "custom2"          # the first entry is not a header: not pi's id


def test_parse_header_models_and_blocks(tmp_path):
    f = write_session(tmp_path)
    parsed = pi.parse_session(f)
    assert parsed["session_id"] == SID and parsed["cwd"] == CWD and parsed["version"] == 3
    assert parsed["start_ts"] == "2026-08-21T05:02:49.003Z" and parsed["end_ts"] == _ts(4)
    assert parsed["title"] == "Fix the SAAS-42 bug" and parsed["first_prompt"] == "Fix the SAAS-42 bug"
    assert parsed["models"] == ["anthropic/claude-sonnet-5-5"]
    kinds = [(m["role"], m["kind"]) for m in parsed["messages"]]
    assert kinds == [("user", KIND_TEXT), ("assistant", KIND_THINKING), ("assistant", KIND_TOOL_CALL),
                     ("tool", KIND_TOOL_RESULT), ("assistant", KIND_TEXT)]
    call = parsed["messages"][2]
    assert call["text"] == '[read] {"path": "/x/app.py"}'
    assert call["tool_call"].native_id == "call_1" and call["tool_call"].file_actions == [("read", "/x/app.py")]
    result = parsed["messages"][3]
    assert result["tool_call"].native_id == "call_1" and result["text"] == "def main(): pass"
    assert parsed["messages"][4]["text"] == "Found it.\nThe bug is in main()."   # consecutive text blocks merge
    assert parsed["messages"][4]["native_id"] == "e6" and call["native_id"] == "e4#1"


def test_usage_is_one_record_per_assistant_message_linked_to_its_text(tmp_path):
    parsed = pi.parse_session(write_session(tmp_path))
    assert [(u.native_request_id, u.message_seq) for u in parsed["usage"]] == [("resp-1", 1), ("resp-2", 4)]
    u = parsed["usage"][0]
    assert (u.provider, u.model, u.input_uncached, u.cache_read, u.cache_write, u.output, u.reasoning) == \
        ("anthropic", "claude-sonnet-5-5", 1797, 10, 5, 124, 7)
    assert u.reported_cost_usd == 0.003 and u.ts == _ts(2)


def test_to_conversation(tmp_path):
    f = write_session(tmp_path)
    conv = pi.to_conversation(pi.parse_session(f), f)
    assert conv.id == f"pi:{SID}" and conv.source_ref == str(f)
    assert conv.provider == "anthropic" and conv.models == ["anthropic/claude-sonnet-5-5"]
    assert conv.fidelity == "full" and conv.warnings == [] and conv.metadata == {"session_version": 3}
    assert [m.model for m in conv.messages] == [None, "claude-sonnet-5-5", "claude-sonnet-5-5", None, "claude-sonnet-5-5"]
    assert len(conv.usage) == 2


def test_string_user_content_and_error_tool_result(tmp_path):
    entries = [header(),
               _entry("message", "e1", None, _ts(1), message={"role": "user", "content": "plain string prompt"}),
               _entry("message", "e2", "e1", _ts(2),
                      message={"role": "assistant", "provider": "openai", "model": "gpt-5",
                               "content": [{"type": "toolCall", "id": "c", "name": "bash", "arguments": {"command": "ls"}}]}),
               _entry("message", "e3", "e2", _ts(3),
                      message={"role": "toolResult", "toolCallId": "c", "toolName": "bash", "isError": True,
                               "content": [{"type": "text", "text": "No such file"}]})]
    parsed = pi.parse_session(write_session(tmp_path, entries))
    assert parsed["first_prompt"] == "plain string prompt" and parsed["messages"][0]["text"] == "plain string prompt"
    assert parsed["messages"][2]["text"] == "ERROR: No such file"
    assert parsed["models"] == ["openai/gpt-5"] and parsed["usage"] == []   # no usage object on this message


def test_fork_keeps_only_the_path_to_the_last_entry(tmp_path):
    entries = [header(),
               _entry("message", "e1", None, _ts(1), message={"role": "user", "content": "first question"}),
               _entry("message", "e2", "e1", _ts(2), message={"role": "assistant", "content": [{"type": "text", "text": "answer A"}]}),
               _entry("message", "e3", "e2", _ts(3), message={"role": "user", "content": "follow-up on A"}),
               _entry("message", "e4", "e3", _ts(4), message={"role": "assistant", "content": [{"type": "text", "text": "more on A"}]}),
               # the user branched back to e1 and continued from there
               _entry("message", "e5", "e1", _ts(5), message={"role": "user", "content": "actually, something else"}),
               _entry("message", "e6", "e5", _ts(6), message={"role": "assistant", "content": [{"type": "text", "text": "answer B"}]})]
    f = write_session(tmp_path, entries)
    parsed = pi.parse_session(f)
    assert [m["text"] for m in parsed["messages"]] == ["first question", "actually, something else", "answer B"]
    assert parsed["branched_messages"] == 3
    conv = pi.to_conversation(parsed, f)
    assert conv.warnings == ["3 messages on abandoned branches skipped"]
    assert conv.end_ts == _ts(6)


def test_session_name_is_the_title_and_skipped_entries_are_counted(tmp_path):
    entries = [header(),
               _entry("message", "e1", None, _ts(1), message={"role": "user", "content": "do the thing"}),
               _entry("session_info", "e2", "e1", _ts(2), name="  Thing doer  "),
               _entry("label", "e3", "e2", _ts(2), targetId="e1", label="start"),
               _entry("compaction", "e4", "e3", _ts(3), summary="earlier context", firstKeptEntryId="e1", tokensBefore=1000),
               _entry("custom", "e5", "e4", _ts(3), customType="ext", data={}),
               _entry("message", "e6", "e5", _ts(4), message={"role": "system", "content": "tools added", "sections": []}),
               _entry("message", "e7", "e6", _ts(5), message={"role": "bashExecution", "command": "git status", "output": "clean", "exitCode": 0}),
               _entry("message", "e8", "e7", _ts(6), message={"role": "assistant", "content": [{"type": "text", "text": "done"}]})]
    f = write_session(tmp_path, entries)
    conv = pi.to_conversation(pi.parse_session(f), f)
    assert conv.title == "Thing doer" and conv.first_prompt == "do the thing"
    assert [(m.role, m.kind, m.text) for m in conv.messages] == [
        ("user", KIND_TEXT, "do the thing"), ("user", KIND_TOOL_CALL, "[bash] $ git status\nclean"),
        ("assistant", KIND_TEXT, "done")]
    assert conv.messages[1].tool_call.name == "bash" and conv.messages[1].tool_call.result_text == "clean"
    assert conv.warnings == ["skipped entries: compaction x1, custom x1, label x1"]


def test_empty_and_header_only_files_are_confirmed_empty(tmp_path):
    f = write_session(tmp_path, [])
    assert pi.exclude_reason(pi.parse_session(f)) == "empty"
    f = write_session(tmp_path, [header(), _entry("model_change", "e1", None, _ts(0), provider="p", modelId="m")])
    assert pi.exclude_reason(pi.parse_session(f)) == "empty"


def test_header_plus_corrupt_messages_is_unreadable(tmp_path):
    f = tmp_path / FILE
    f.write_text(json.dumps(header()) + "\n" + '{"type":"message","id":"e1","parentId":null,"message":{"role":"user"\n')
    with pytest.raises(UnreadableSource):
        pi.exclude_reason(pi.parse_session(f))
    f.write_text(json.dumps(header()) + "\n" + json.dumps({"type": "message", "id": "e1", "parentId": None, "message": "not an object"}) + "\n")
    with pytest.raises(UnreadableSource):
        pi.exclude_reason(pi.parse_session(f))


def test_malformed_entries_are_counted_not_crashed(tmp_path):
    """Entries without an id, roles that are not strings and messages without the content their
    role carries are unparseable records: counted, and unreadable when nothing else survives."""
    bad = [header(),
           {"type": "message", "parentId": None, "timestamp": _ts(1), "message": {"role": "user", "content": "no id"}},
           _entry("message", "e2", None, _ts(2), message={"role": ["user"], "content": "list role"}),
           _entry("message", "e3", None, _ts(3), message={"role": "user"}),
           _entry("message", "e4", None, _ts(4), message={"role": "toolResult", "toolCallId": "c", "content": None}),
           _entry("message", "e5", None, _ts(5), message={"role": "bashExecution", "output": "no command"})]
    f = write_session(tmp_path, bad)
    parsed = pi.parse_session(f)
    assert parsed["malformed_lines"] == 5 and parsed["messages"] == []
    with pytest.raises(UnreadableSource):
        pi.exclude_reason(parsed)
    f = write_session(tmp_path, bad + [_entry("message", "e6", None, _ts(6), message={"role": "user", "content": "ok"})])
    conv = pi.to_conversation(pi.parse_session(f), f)
    assert [m.text for m in conv.messages] == ["ok"] and conv.fidelity == "partial"


def test_malformed_message_still_links_its_ancestors(tmp_path):
    entries = [header(),
               _entry("message", "e1", None, _ts(1), message={"role": "user", "content": "first"}),
               _entry("message", "e2", "e1", _ts(2), message={"role": "assistant"}),            # no content
               _entry("message", "e3", "e2", _ts(3), message={"role": "user", "content": "tail"})]
    f = write_session(tmp_path, entries)
    parsed = pi.parse_session(f)
    assert [m["text"] for m in parsed["messages"]] == ["first", "tail"]
    assert parsed["malformed_lines"] == 1 and parsed["branched_messages"] == 0


def test_usage_keeps_the_one_hour_cache_split_and_empty_responses(tmp_path):
    entries = [header(),
               _entry("message", "e1", None, _ts(1), message={"role": "user", "content": "go"}),
               _entry("message", "e2", "e1", _ts(2),     # aborted before any content block: usage still counts
                      message={"role": "assistant", "provider": "anthropic", "model": "claude-haiku-5-5", "content": [],
                               "stopReason": "aborted", "usage": {"input": 100, "output": 0, "cacheRead": 0,
                                                                   "cacheWrite": 100, "cacheWrite1h": 40}}),
               _entry("message", "e3", "e2", _ts(3),
                      message={"role": "assistant", "provider": "anthropic", "model": "claude-haiku-5-5",
                               "content": [{"type": "text", "text": "done"}],
                               "usage": {"input": 5, "output": 7, "cacheWrite": 10, "cacheWrite1h": 11}})]
    parsed = pi.parse_session(write_session(tmp_path, entries))
    assert parsed["models"] == ["anthropic/claude-haiku-5-5"]
    assert [(u.message_seq, u.cache_write, u.cache_write_1h) for u in parsed["usage"]] == [(None, 60, 40), (1, 10, None)]


def test_unknown_layout_is_unreadable(tmp_path):
    f = tmp_path / FILE
    f.write_text('{"kind":"something","payload":1}\n{"kind":"else"}\n')
    with pytest.raises(UnreadableSource):
        pi.exclude_reason(pi.parse_session(f))


def test_partially_malformed_file_is_partial_fidelity(tmp_path):
    f = tmp_path / FILE
    f.write_text(json.dumps(header()) + "\n" + "NOT JSON\n" +
                 json.dumps(_entry("message", "e1", None, _ts(1), message={"role": "user", "content": "hi"})) + "\n")
    conv = pi.to_conversation(pi.parse_session(f), f)
    assert conv.fidelity == "partial" and conv.warnings == ["1 unparseable lines skipped"]
    assert [m.text for m in conv.messages] == ["hi"]


def test_dangling_parent_keeps_the_tail(tmp_path):
    """A file whose early entries were lost still yields the entries that lead to the leaf."""
    entries = [header(),
               _entry("message", "e5", "missing", _ts(5), message={"role": "user", "content": "late question"}),
               _entry("message", "e6", "e5", _ts(6), message={"role": "assistant", "content": [{"type": "text", "text": "late answer"}]})]
    parsed = pi.parse_session(write_session(tmp_path, entries))
    assert [m["text"] for m in parsed["messages"]] == ["late question", "late answer"]
