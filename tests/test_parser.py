# tests/test_parser.py — Claude Code JSONL parsing and adapter
import json

from convo_index.sources.claude_code import (parse_conversation, flatten_content, clean_prompt,
                                             to_conversation, ClaudeCodeAdapter, resume_command)


def _write(tmp_path, lines, name="sess"):
    f = tmp_path / f"{name}.jsonl"
    f.write_text("".join(json.dumps(l) + "\n" for l in lines))
    return f


def test_flatten_content_string():
    assert flatten_content("hello") == "hello"


def test_flatten_content_list():
    parts = [{"type": "text", "text": "a"}, {"type": "tool_use", "name": "x"}, {"type": "text", "text": "b"}]
    assert flatten_content(parts) == "a\nb"


def test_clean_prompt_strips_command_markers():
    raw = "<command-message>soc2-audit</command-message>\n<command-name>/soc2-audit</command-name>"
    assert clean_prompt(raw) == "/soc2-audit"


def _lines():
    return [
        {"type": "mode", "mode": "normal"},
        {"type": "user", "isMeta": False, "timestamp": "2026-06-16T15:24:03.001Z", "uuid": "u-1",
         "cwd": "/Users/alice/Development/globex/widgets-hq", "gitBranch": "main", "version": "2.1.266",
         "sessionId": "abc123", "message": {"role": "user", "content": "Fix the SAAS-42 bug"}},
        # one API request written as two events (thinking block, then text block) sharing requestId
        {"type": "assistant", "timestamp": "2026-06-16T15:24:09.000Z", "uuid": "u-2", "requestId": "req-1",
         "message": {"role": "assistant", "model": "claude-fable-5-1",
                     "content": [{"type": "thinking", "thinking": "hmm"}],
                     "usage": {"input_tokens": 2, "cache_creation_input_tokens": 22482,
                               "cache_read_input_tokens": 27151, "output_tokens": 834,
                               "output_tokens_details": {"thinking_tokens": 166}}}},
        {"type": "assistant", "timestamp": "2026-06-16T15:24:10.000Z", "uuid": "u-3", "requestId": "req-1",
         "message": {"role": "assistant", "model": "claude-fable-5-1",
                     "content": [{"type": "text", "text": "Sure."}],
                     "usage": {"input_tokens": 2, "cache_creation_input_tokens": 22482,
                               "cache_read_input_tokens": 27151, "output_tokens": 834}}},
        {"type": "ai-title", "aiTitle": "Fix login bug"},
    ]


def test_parse_conversation(tmp_path):
    conv = parse_conversation(_write(tmp_path, _lines()))
    assert conv["session_id"] == "abc123"
    assert conv["cwd"] == "/Users/alice/Development/globex/widgets-hq"
    assert conv["git_branch"] == "main"
    assert conv["title"] == "Fix login bug"
    assert conv["first_prompt"] == "Fix the SAAS-42 bug"
    assert conv["start_ts"] == "2026-06-16T15:24:03.001Z"
    assert conv["end_ts"] == "2026-06-16T15:24:10.000Z"
    assert conv["version"] == "2.1.266"
    assert conv["models"] == ["claude-fable-5-1"]
    assert [(m["seq"], m["role"], m["kind"], m["text"], m["uuid"], m["model"]) for m in conv["messages"]] == [
        (0, "user", "text", "Fix the SAAS-42 bug", "u-1", None),
        (1, "assistant", "thinking", "hmm", "u-2#0", "claude-fable-5-1"),
        (2, "assistant", "text", "Sure.", "u-3", "claude-fable-5-1"),
    ]


def test_usage_deduplicated_per_request(tmp_path):
    conv = parse_conversation(_write(tmp_path, _lines()))
    assert len(conv["usage"]) == 1
    u = conv["usage"][0]
    assert u.native_request_id == "req-1"
    assert (u.input_uncached, u.cache_write, u.cache_read, u.output, u.reasoning) == (2, 22482, 27151, 834, 166)
    assert u.model == "claude-fable-5-1" and u.provider == "anthropic"
    # the request's first event was a thinking block; usage links to the text block that followed
    assert u.message_seq == 2


def test_usage_without_request_id_falls_back_to_uuid(tmp_path):
    f = _write(tmp_path, [
        {"type": "user", "sessionId": "q", "uuid": "u0", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "uuid": "u1", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "yo"}], "usage": {"input_tokens": 1, "output_tokens": 2}}},
    ])
    conv = parse_conversation(f)
    assert [(u.native_request_id, u.message_seq) for u in conv["usage"]] == [("u1", 1)]


def test_summarizer_sessions_are_excluded(tmp_path):
    from convo_index.config import SUMMARY_PROMPT_PREFIX
    from convo_index.sources.claude_code import exclude_reason
    f = _write(tmp_path, [
        {"type": "user", "sessionId": "q", "message": {"role": "user", "content": SUMMARY_PROMPT_PREFIX + ". Respond"}},
    ])
    assert exclude_reason(parse_conversation(f)) == "self-llm-call"


def test_revision_includes_size(tmp_path):
    from convo_index.sources.claude_code import revision_of
    f = _write(tmp_path, _lines())
    rev = revision_of(f)
    from convo_index.sources.claude_code import PARSER_VERSION
    assert rev == f"p{PARSER_VERSION}:{f.stat().st_mtime}:{f.stat().st_size}"   # parser version: a bump re-ingests once


def test_to_conversation(tmp_path):
    f = _write(tmp_path, _lines())
    conv = to_conversation(parse_conversation(f), f)
    assert conv.id == "claude_code:abc123"
    assert conv.provider == "anthropic" and conv.models == ["claude-fable-5-1"]
    assert conv.metadata == {"cli_version": "2.1.266"}
    assert [(m.native_id, m.kind) for m in conv.messages] == [("u-1", "text"), ("u-2#0", "thinking"), ("u-3", "text")]
    assert conv.source_ref == str(f)


def test_parse_skips_malformed_lines(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"user","message":{"role":"user","content":"hi"},"sessionId":"z"}\nNOT JSON\n')
    conv = parse_conversation(f)
    assert len(conv["messages"]) == 1


def test_title_falls_back_to_first_prompt(tmp_path):
    f = _write(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "Hello there"}, "sessionId": "q"},
    ])
    conv = parse_conversation(f)
    assert conv["title"] == "Hello there"


def test_parse_captures_entrypoint(tmp_path):
    f = _write(tmp_path, [
        {"type": "user", "entrypoint": "sdk-cli", "sessionId": "e1",
         "message": {"role": "user", "content": "hi"}},
    ])
    assert parse_conversation(f)["entrypoint"] == "sdk-cli"


def test_adapter_id_follows_file_name(tmp_path):
    projects = tmp_path / "projects"; (projects / "p").mkdir(parents=True)
    _write(projects / "p", _lines(), name="file-id")
    ad = ClaudeCodeAdapter(projects)
    ref = next(iter(ad.discover()))
    conv, revision = ad.load(ref)
    assert ref.native_id == "file-id" and conv.id == "claude_code:file-id"
    assert revision == ref.change_key


def test_resume_command():
    assert resume_command("claude_code:abc", "/x") == "claude --resume abc"


def _write_lf(path, text):
    path.write_text(text, newline="\n")   # Claude Code writes LF; Windows must not translate


def test_read_snapshot_drops_partial_trailing_line_and_pins_revision(tmp_path):
    from convo_index.sources.claude_code import read_snapshot
    f = tmp_path / "s.jsonl"
    complete = json.dumps({"type": "user", "sessionId": "z", "uuid": "u0",
                           "message": {"role": "user", "content": "hi"}}) + "\n"
    _write_lf(f, complete + '{"type":"user","message":{"role":"user","content":"half')  # mid-write
    text, revision = read_snapshot(f)
    assert text == complete
    st = f.stat()
    assert revision == f"{st.st_mtime}:{st.st_size}"
    conv = parse_conversation(f, text)
    assert len(conv["messages"]) == 1


def test_load_revision_describes_exactly_the_bytes_parsed(tmp_path, monkeypatch):
    """An append that lands after the read must not be acknowledged by the returned revision."""
    import convo_index.sources.claude_code as cc
    projects = tmp_path / "projects"; (projects / "p").mkdir(parents=True)
    f = _write(projects / "p", _lines(), name="live")
    ad = ClaudeCodeAdapter(projects)
    ref = next(iter(ad.discover()))
    real = cc.read_snapshot
    def read_then_append(path):
        out = real(path)
        with open(path, "a") as fh:
            fh.write("\n" + json.dumps({"type": "user", "message": {"role": "user", "content": "late"}}))
        return out
    monkeypatch.setattr(cc, "read_snapshot", read_then_append)
    conv, revision = ad.load(ref)
    assert not any(m.text == "late" for m in conv.messages)
    assert revision != cc.revision_of(f)     # next discovery sees a different key -> re-read


def test_all_malformed_file_is_unreadable_not_empty(tmp_path):
    import pytest
    from convo_index.sources.base import UnreadableSource
    from convo_index.sources.claude_code import exclude_reason
    f = tmp_path / "s.jsonl"; f.write_text("NOT JSON\n[1,2]\n")
    with pytest.raises(UnreadableSource):
        exclude_reason(parse_conversation(f))


def test_partially_malformed_file_is_partial_fidelity(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"user","sessionId":"z","message":{"role":"user","content":"hi"}}\nNOT JSON\n')
    conv = to_conversation(parse_conversation(f), f)
    assert conv.fidelity == "partial" and conv.warnings == ["1 unparseable lines skipped"]


def test_empty_file_is_confirmed_empty(tmp_path):
    from convo_index.sources.claude_code import exclude_reason
    f = tmp_path / "s.jsonl"; f.write_text("")
    assert exclude_reason(parse_conversation(f)) == "empty"


def test_metadata_plus_malformed_messages_is_unreadable(tmp_path):
    import pytest
    from convo_index.sources.base import UnreadableSource
    from convo_index.sources.claude_code import exclude_reason
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"ai-title","aiTitle":"Looks fine"}\n{"type":"user","message":{"role":"user","content":"hi"\n')
    with pytest.raises(UnreadableSource):
        exclude_reason(parse_conversation(f))


def test_tool_and_thinking_blocks_become_messages(tmp_path):
    from convo_index.model import KIND_TEXT, KIND_THINKING, KIND_TOOL_CALL, KIND_TOOL_RESULT
    f = _write(tmp_path, [
        {"type": "user", "sessionId": "z", "uuid": "u0", "cwd": "/x", "message": {"role": "user", "content": "Look into history"}},
        {"type": "assistant", "uuid": "a1", "requestId": "r1", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "thinking", "thinking": "hmm let me search"}],
         "usage": {"input_tokens": 1, "output_tokens": 2}}},
        {"type": "assistant", "uuid": "a2", "requestId": "r1", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "text", "text": "I'll search."}, {"type": "tool_use", "id": "toolu_1", "name": "Edit",
                     "input": {"file_path": "/x/a.py", "old_string": "a", "new_string": "b"}}],
         "usage": {"input_tokens": 1, "output_tokens": 2}}},
        {"type": "user", "uuid": "u1", "toolUseResult": {"stdout": "ok"}, "message": {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok done", "is_error": False}]}},
        {"type": "assistant", "uuid": "a3", "requestId": "r2", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Done."}], "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ])
    conv = to_conversation(parse_conversation(f), f)
    kinds = [(m.role, m.kind, m.native_id) for m in conv.messages]
    # blocks keep their written order: text before the tool call in event a2
    assert kinds == [("user", KIND_TEXT, "u0"), ("assistant", KIND_THINKING, "a1#0"),
                     ("assistant", KIND_TEXT, "a2"), ("assistant", KIND_TOOL_CALL, "a2#1"),
                     ("tool", KIND_TOOL_RESULT, "u1#0"), ("assistant", KIND_TEXT, "a3")]
    tc = conv.messages[3].tool_call
    assert tc.name == "Edit" and tc.native_id == "toolu_1" and tc.file_actions == [("edit", "/x/a.py")]
    assert conv.messages[4].tool_call.native_id == "toolu_1" and conv.messages[4].text == "ok done"
    assert conv.first_prompt == "Look into history"                   # tool results never become the first prompt
    assert [u.message_seq for u in conv.usage] == [2, 5]              # usage links to the request's text block


def test_usage_on_tool_only_turn_links_to_its_first_block(tmp_path):
    f = _write(tmp_path, [
        {"type": "user", "sessionId": "z", "uuid": "u0", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "uuid": "a1", "requestId": "r1", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
         "usage": {"input_tokens": 1, "output_tokens": 2}}},
    ])
    conv = parse_conversation(f)
    assert [u.message_seq for u in conv["usage"]] == [1]


def test_unknown_layout_is_unreadable_not_empty(tmp_path):
    import pytest
    from convo_index.sources import claude_code as cc
    from convo_index.sources.base import UnreadableSource
    f = tmp_path / "x.jsonl"; f.write_text('{"foo": 1}\n{}\n')
    with pytest.raises(UnreadableSource):
        cc.exclude_reason(cc.parse_conversation(f))
    f.write_text('{"type": "summary", "summary": "s"}\n')
    assert cc.exclude_reason(cc.parse_conversation(f)) == "empty"        # known record types, no messages


def test_event_with_non_object_message_is_a_malformed_line(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"user","sessionId":"z","message":"bad"}\n'
                 '{"type":"user","sessionId":"z","message":[]}\n'
                 '{"type":"user","sessionId":"z","message":false}\n'
                 '{"type":[],"sessionId":"z"}\n'
                 '{"type":"user","sessionId":"z","message":{"role":"user","content":"hi"}}\n')
    conv = to_conversation(parse_conversation(f), f)
    assert [m.text for m in conv.messages] == ["hi"] and conv.fidelity == "partial"
    assert conv.warnings == ["4 unparseable lines skipped"]


def test_typed_records_of_an_unknown_layout_are_unreadable(tmp_path):
    import pytest
    from convo_index.sources import claude_code as cc
    from convo_index.sources.base import UnreadableSource
    f = tmp_path / "x.jsonl"; f.write_text('{"type":"new_message","content":"keep me"}\n')
    with pytest.raises(UnreadableSource):
        cc.exclude_reason(cc.parse_conversation(f))


def test_resume_command_quotes_the_id():
    from convo_index.sources.claude_code import resume_command
    from convo_index.sources.base import shell_quote
    assert resume_command("claude_code:id; echo injected", None) == "claude --resume " + shell_quote("id; echo injected")
