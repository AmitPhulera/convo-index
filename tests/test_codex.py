"""Codex CLI adapter against a synthetic CODEX_HOME shaped like Codex 0.154's."""
import json
import sqlite3

import pytest

from convo_index.sources import codex as cx
from convo_index.sources.base import UnreadableSource
from convo_index.model import KIND_TEXT, KIND_THINKING, KIND_TOOL_CALL, KIND_TOOL_RESULT

TID = "01a0aa07-8d00-7c60-a8ca-271bdc2380e5"
T0 = "2026-09-16T11:43:36.794Z"


def _ev(t, payload, ts=T0, ordinal=0):
    return json.dumps({"timestamp": ts, "ordinal": ordinal, "type": t, "payload": payload})


def make_home(tmp_path, rollout_lines=None, first_prompt="Fix the SAAS-9 bug please", with_rollout=True):
    home = tmp_path / "codex"; (home / "sessions" / "2026" / "09" / "16").mkdir(parents=True)
    rollout = home / "sessions" / "2026" / "09" / "16" / f"rollout-2026-09-16T17-13-36-{TID}.jsonl"
    conn = sqlite3.connect(str(home / "state_5.sqlite"))
    conn.execute("""CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER, updated_at INTEGER,
        source TEXT, model_provider TEXT, cwd TEXT, title TEXT, tokens_used INTEGER, archived INTEGER DEFAULT 0,
        git_sha TEXT, git_branch TEXT, git_origin_url TEXT, cli_version TEXT, first_user_message TEXT, model TEXT,
        history_mode TEXT, created_at_ms INTEGER, updated_at_ms INTEGER, originator TEXT)""")
    conn.execute("INSERT INTO threads(id, rollout_path, source, model_provider, cwd, title, git_branch, git_origin_url, "
                 "cli_version, first_user_message, model, history_mode, created_at_ms, updated_at_ms, originator) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (TID, str(rollout), "exec", "openai", "/Users/me/proj", first_prompt[:40], "main",
                  "https://github.com/o/r", "0.154.0", first_prompt, "gpt-6-astra", "paginated",
                  1789559016000, 1789559258000, "codex_exec"))
    conn.commit(); conn.close()
    if with_rollout:
        lines = rollout_lines if rollout_lines is not None else default_rollout(first_prompt)
        rollout.write_text("\n".join(lines) + "\n")
    return home


def default_rollout(first_prompt):
    return [
        _ev("session_meta", {"session_id": TID, "cwd": "/Users/me/proj", "originator": "codex_exec", "cli_version": "0.154.0",
                             "model_provider": "openai", "git": {"commit_hash": "410b9fc", "branch": "feat/x",
                                                                 "repository_url": "https://github.com/o/r"}}),
        _ev("response_item", {"type": "message", "id": "msg_dev", "role": "developer",
                              "content": [{"type": "input_text", "text": "<skills_instructions>..."}]}, ordinal=2),
        _ev("response_item", {"type": "message", "id": "msg_env", "role": "user",
                              "content": [{"type": "input_text", "text": "<environment_context>...</environment_context>"}],
                              "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["environments.environment_context"]}}, ordinal=5),
        _ev("turn_context", {"turn_id": "t1", "cwd": "/Users/me/proj", "model": "gpt-6-astra"}, ordinal=7),
        _ev("response_item", {"type": "message", "id": "msg_user", "role": "user",
                              "content": [{"type": "input_text", "text": first_prompt}],
                              "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["unknown"]}}, ordinal=8),
        _ev("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": first_prompt}]}}, ordinal=9),
        _ev("response_item", {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "Plan: read the file"}]},
            ts="2026-09-16T11:43:40.000Z", ordinal=10),
        _ev("response_item", {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "gAAAA"},
            ts="2026-09-16T11:43:40.500Z", ordinal=11),
        _ev("response_item", {"type": "custom_tool_call", "id": "ctc_1", "call_id": "call_1", "name": "exec",
                              "input": "{\"cmd\": \"cat README.md\"}"}, ts="2026-09-16T11:43:41.000Z", ordinal=12),
        _ev("token_usage_record", {"response_id": "resp_1", "turn_id": "t1",
                                   "usage": {"input_tokens": 15508, "cached_input_tokens": 12160, "cache_write_input_tokens": 0,
                                             "output_tokens": 116, "reasoning_output_tokens": 7}}, ts="2026-09-16T11:43:41.500Z", ordinal=13),
        _ev("response_item", {"type": "custom_tool_call_output", "id": "ctco_1", "call_id": "call_1",
                              "output": [{"type": "input_text", "text": "Script completed\n# README"}]}, ts="2026-09-16T11:43:42.000Z", ordinal=15),
        _ev("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 99999, "output_tokens": 999},
                                                          "last_token_usage": {"input_tokens": 15508, "output_tokens": 116}}}, ordinal=16),
        _ev("response_item", {"type": "message", "id": "msg_ai", "role": "assistant", "phase": "final_answer",
                              "content": [{"type": "output_text", "text": "The README says hello."}]}, ts="2026-09-16T11:43:45.000Z", ordinal=20),
        _ev("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "The README says hello."}, ts="2026-09-16T11:43:45.100Z", ordinal=21),
    ]


def test_discover_uses_thread_update_and_rollout_stat(tmp_path):
    home = make_home(tmp_path)
    refs = list(cx.CodexAdapter(home).discover())
    assert [r.native_id for r in refs] == [TID] and refs[0].change_key.startswith(f"p{cx.PARSER_VERSION}:1789559258000:")
    assert refs[0].id == f"codex:{TID}"


def test_load_normalizes_rollout(tmp_path):
    home = make_home(tmp_path)
    ad = cx.CodexAdapter(home)
    conv, revision = ad.load(next(iter(ad.discover())))
    assert conv.id == f"codex:{TID}" and revision.startswith(f"p{cx.PARSER_VERSION}:1789559258000:")
    assert [(m.role, m.kind) for m in conv.messages] == [
        ("user", KIND_TEXT), ("assistant", KIND_THINKING), ("assistant", KIND_TOOL_CALL),
        ("tool", KIND_TOOL_RESULT), ("assistant", KIND_TEXT)]
    assert conv.first_prompt == "Fix the SAAS-9 bug please"          # developer + environment messages skipped
    assert conv.messages[1].text == "Plan: read the file"
    tc = conv.messages[2].tool_call
    assert tc.name == "exec" and tc.native_id == "call_1" and tc.args == {"cmd": "cat README.md"}
    assert conv.messages[3].tool_call.native_id == "call_1" and "# README" in conv.messages[3].text
    assert conv.messages[4].model == "gpt-6-astra"
    assert conv.git_branch == "feat/x" and conv.git_origin_url == "https://github.com/o/r" and conv.cwd == "/Users/me/proj"
    assert conv.models == ["gpt-6-astra"] and conv.provider == "openai"
    assert conv.metadata["cli_version"] == "0.154.0" and conv.metadata["originator"] == "codex_exec"
    assert conv.warnings == ["reasoning encrypted"] and conv.fidelity == "full"
    assert conv.start_ts == T0 and conv.end_ts == "2026-09-16T11:43:45.100Z"


def test_usage_from_token_usage_record_only(tmp_path):
    home = make_home(tmp_path)
    ad = cx.CodexAdapter(home)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert len(conv.usage) == 1                                        # token_count (cumulative) is ignored
    u = conv.usage[0]
    assert (u.native_request_id, u.input_uncached, u.cache_read, u.output, u.reasoning) == ("resp_1", 3348, 12160, 116, 7)
    assert u.provider == "openai" and u.model == "gpt-6-astra"
    assert u.message_seq == 2                                          # the tool call the record was written after


def test_own_llm_calls_and_empty_sessions_are_excluded(tmp_path):
    from convo_index.config import SUMMARY_PROMPT_PREFIX
    home = make_home(tmp_path, first_prompt=SUMMARY_PROMPT_PREFIX + ". Respond")
    ad = cx.CodexAdapter(home)
    assert ad.load(next(iter(ad.discover()))) is None


def test_missing_rollout_gives_partial_conversation_from_state_db(tmp_path):
    home = make_home(tmp_path, with_rollout=False)
    ad = cx.CodexAdapter(home)
    conv, rev = ad.load(next(iter(ad.discover())))
    assert conv.fidelity == "partial" and [m.text for m in conv.messages] == ["Fix the SAAS-9 bug please"]
    assert conv.git_origin_url == "https://github.com/o/r" and "rollout file missing" in conv.warnings[0]


def test_all_malformed_rollout_is_unreadable(tmp_path):
    home = make_home(tmp_path, rollout_lines=["not json", "{broken"])
    ad = cx.CodexAdapter(home)
    with pytest.raises(UnreadableSource):
        ad.load(next(iter(ad.discover())))


def test_state_db_selection_and_missing_columns(tmp_path):
    home = tmp_path / "codex"; home.mkdir()
    for n in (3, 10):
        conn = sqlite3.connect(str(home / f"state_{n}.sqlite")); conn.execute("CREATE TABLE threads(id TEXT)"); conn.commit(); conn.close()
    assert cx.state_db_path(home).name == "state_10.sqlite"
    with pytest.raises(RuntimeError, match="missing"):
        list(cx.CodexAdapter(home).discover())
    assert cx.CodexAdapter(tmp_path / "nowhere").available() is False


def test_resume_command():
    assert cx.resume_command(f"codex:{TID}", None) == f"codex resume {TID}"


def test_missing_rollout_own_call_is_excluded(tmp_path):
    from convo_index.config import CLASSIFY_PROMPT_PREFIX
    home = make_home(tmp_path, first_prompt=CLASSIFY_PROMPT_PREFIX + " x", with_rollout=False)
    ad = cx.CodexAdapter(home)
    assert ad.load(next(iter(ad.discover()))) is None


def test_texts_and_args_tolerate_other_shapes():
    assert cx._texts({"status": "ok"}) == '{"status": "ok"}'
    assert cx._texts(["a", "b"]) == "a\nb"
    assert cx._texts([{"type": "output_text", "text": "x"}, {"weird": 1}]) == 'x\n{"weird": 1}'
    msgs, usage, meta = cx.parse_rollout("\n".join([
        _ev("response_item", {"type": "function_call", "id": "f1", "call_id": "c1", "name": "shell", "arguments": {"cmd": "ls"}}),
        _ev("response_item", {"type": "function_call_output", "id": "o1", "call_id": "c1", "output": {"stdout": "a.txt"}}),
    ]), {"model": "gpt-x"})
    assert msgs[0].tool_call.args == {"cmd": "ls"} and msgs[0].model == "gpt-x"
    assert "a.txt" in msgs[1].text and msgs[1].tool_call.native_id == "c1"


def test_boilerplate_filter_keeps_the_recorded_first_message(tmp_path):
    home = make_home(tmp_path)
    lines = default_rollout("Fix the SAAS-9 bug please")
    # a real prompt mistakenly tagged with a boilerplate kind must survive when it is the thread's first message
    lines[4] = _ev("response_item", {"type": "message", "id": "msg_user", "role": "user",
                                     "content": [{"type": "input_text", "text": "Fix the SAAS-9 bug please"}],
                                     "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["plugins.recommendations"]}})
    rollout = next(home.glob("sessions/**/rollout-*.jsonl")); rollout.write_text("\n".join(lines) + "\n")
    ad = cx.CodexAdapter(home)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert conv.first_prompt == "Fix the SAAS-9 bug please"


def test_threads_cached_per_state_db_stamp(tmp_path):
    home = make_home(tmp_path)
    ad = cx.CodexAdapter(home)
    a = ad._threads(); b = ad._threads()
    assert a is b


def _load(tmp_path, lines):
    home = make_home(tmp_path, rollout_lines=lines)
    ad = cx.CodexAdapter(home)
    return ad.load(next(iter(ad.discover())))


def test_usage_links_to_the_first_text_of_its_response_by_event_order(tmp_path):
    T = "2026-09-16T11:43:50.000Z"          # every event shares one timestamp: order must decide
    lines = default_rollout("Fix it")[:6] + [
        _ev("event_msg", {"type": "task_started", "turn_id": "t1"}, ts=T, ordinal=10),
        _ev("response_item", {"type": "message", "id": "msg_a", "role": "assistant", "content": [{"type": "output_text", "text": "First answer"}]}, ts=T, ordinal=11),
        _ev("response_item", {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "shell", "arguments": "{\"cmd\": \"ls\"}"}, ts=T, ordinal=12),
        _ev("token_usage_record", {"response_id": "resp_a", "usage": {"input_tokens": 10, "output_tokens": 5}}, ts=T, ordinal=13),
        _ev("response_item", {"type": "function_call_output", "id": "fco_1", "call_id": "call_9", "output": "a.txt\nb.txt"}, ts=T, ordinal=14),
        _ev("response_item", {"type": "message", "id": "msg_b", "role": "assistant", "content": [{"type": "output_text", "text": "Second"}, {"type": "output_text", "text": "answer"}]}, ts=T, ordinal=15),
        _ev("token_usage_record", {"response_id": "resp_b", "usage": {"input_tokens": 20, "output_tokens": 6}}, ts=T, ordinal=16),
        _ev("response_item", {"type": "function_call", "call_id": "call_10", "name": "shell", "arguments": "{}"}, ts=T, ordinal=17),   # no id
        _ev("token_usage_record", {"response_id": "resp_c", "usage": {"input_tokens": 1, "output_tokens": 1}}, ts=T, ordinal=18),
        _ev("token_usage_record", {"response_id": "resp_d", "usage": {"input_tokens": 1, "output_tokens": 1}}, ts=T, ordinal=19),   # no item: unlinked
    ]
    conv, _ = _load(tmp_path, lines)
    by_id = {m.native_id: m for m in conv.messages}
    linked = {u.native_request_id: u.message_seq for u in conv.usage}
    assert linked["resp_a"] == by_id["msg_a"].seq                     # the text, not the later tool call
    assert linked["resp_b"] == by_id["msg_b"].seq and by_id["msg_b"].text == "Second\nanswer"
    assert linked["resp_c"] == by_id["call_10:call"].seq              # tool-only response: its call, with a fallback id
    assert linked["resp_d"] is None
    assert by_id["fco_1"].kind == KIND_TOOL_RESULT and by_id["fco_1"].tool_call.native_id == "call_9"   # explicit id wins
    assert by_id["fc_1"].tool_call.args == {"cmd": "ls"} and all(u.provider == "openai" for u in conv.usage)


def test_mixed_boilerplate_items_are_filtered_individually(tmp_path):
    lines = default_rollout("Fix it")[:4] + [
        _ev("response_item", {"type": "message", "id": "msg_mix", "role": "user",
                              "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"},
                                          {"type": "input_text", "text": "real question"}],
                              "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["environments.environment_context", "unknown"]}}, ordinal=8),
        _ev("response_item", {"type": "message", "id": "msg_odd", "role": "user",
                              "content": [{"type": "input_text", "text": "kept: kinds and items do not align"}],
                              "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["environments.a", "unknown", None]}}, ordinal=9),
        _ev("response_item", {"type": "message", "id": "msg_all", "role": "user",
                              "content": [{"type": "input_text", "text": "<plugins>"}],
                              "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["plugins.x", "plugins.y"]}}, ordinal=10),
        _ev("response_item", "not an object", ordinal=11),
    ]
    conv, _ = _load(tmp_path, lines)
    texts = [m.text for m in conv.messages if m.role == "user"]
    assert texts == ["real question", "kept: kinds and items do not align"]
    assert conv.fidelity == "partial" and any("1 unparseable" in w for w in conv.warnings)   # the string payload


def test_unknown_layout_is_unreadable_not_empty(tmp_path):
    home = make_home(tmp_path, rollout_lines=['{"foo": 1}', '{}', '{"type": "mystery", "payload": {}}'])
    ad = cx.CodexAdapter(home)
    with pytest.raises(UnreadableSource):
        ad.load(next(iter(ad.discover())))
    home2 = make_home(tmp_path / "b", rollout_lines=[_ev("session_meta", {"cwd": "/x"}), _ev("world_state", {})])
    ad2 = cx.CodexAdapter(home2)
    assert ad2.load(next(iter(ad2.discover()))) is None                # known layout, genuinely nothing to show


def test_threads_cache_notices_rows_committed_to_the_wal(tmp_path):
    home = make_home(tmp_path)
    writer = sqlite3.connect(str(home / "state_5.sqlite"))
    writer.execute("PRAGMA journal_mode=WAL")
    ad = cx.CodexAdapter(home)
    assert len(ad._threads()) == 1
    writer.execute("INSERT INTO threads(id, rollout_path, created_at_ms, updated_at_ms) VALUES('t2', ?, 1, 2)",
                   (str(home / "sessions" / "2026" / "09" / "16" / "rollout-x.jsonl"),))
    writer.commit()                      # lands in the WAL; the main file's mtime/size do not change
    assert len(ad._threads()) == 2
    writer.close()


def test_null_kind_keeps_its_position_so_real_text_survives(tmp_path):
    lines = default_rollout("Fix it")[:4] + [
        _ev("response_item", {"type": "message", "id": "msg_null", "role": "user",
                              "content": [{"type": "input_text", "text": "real question"},
                                          {"type": "input_text", "text": "<environment_context>x</environment_context>"}],
                              "internal_chat_message_metadata_passthrough": {"content_item_kinds": [None, "environments.environment_context"]}}, ordinal=8),
    ]
    conv, _ = _load(tmp_path, lines)
    texts = [m.text for m in conv.messages if m.role == "user"]
    assert "real question" in texts and not any("environment_context" in t for t in texts)
