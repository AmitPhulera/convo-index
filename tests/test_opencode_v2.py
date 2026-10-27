"""Opencode adapter against a synthetic database shaped like Opencode 2.1's.

2.x keeps a session's messages in `session_message(id, session_id, type, seq, data)`: the
old `part` rows now live inside assistant `data.content`, user rows carry their prompt as
`data.text`, and `idle`, `synthetic` and `compaction` rows hold no transcript."""
import json
import sqlite3

import pytest

from convo_index.sources import opencode as oc
from convo_index.sources.base import UnreadableSource
from convo_index.model import KIND_TEXT, KIND_THINKING, KIND_TOOL_CALL

_SCHEMA = """
CREATE TABLE project(id text PRIMARY KEY, worktree text NOT NULL, vcs text, name text,
    time_created integer NOT NULL, time_updated integer NOT NULL, sandboxes text NOT NULL);
CREATE TABLE workspace(id text PRIMARY KEY, provider text NOT NULL, binding text,
    created_at integer NOT NULL, last_used_at integer NOT NULL);
CREATE TABLE session_v2(id text PRIMARY KEY, project_id text NOT NULL, workspace_id text,
    parent_id text, fork_session_id text, slug text NOT NULL, directory text NOT NULL, path text,
    title text, version text NOT NULL, cost real DEFAULT 0 NOT NULL,
    tokens_input integer DEFAULT 0 NOT NULL, revert text, permission text, agent text, model text,
    time_created integer NOT NULL, time_updated integer NOT NULL, time_archived integer);
CREATE TABLE session_message(id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL,
    seq integer NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL,
    data text NOT NULL);
"""

T0 = 1789277890555  # epoch ms, 2026-09-13T05:38:10.555Z
MODEL = {"id": "qwen3.8:27b-mlx", "providerID": "ollama", "variant": "default"}


def _row(conn, mid, session, type_, seq, created, data):
    conn.execute("INSERT INTO session_message VALUES(?,?,?,?,?,?,?)",
                 (mid, session, type_, seq, created, created, json.dumps(data) if not isinstance(data, str) else data))


def make_opencode_v2_db(path, with_child=False, first_prompt="Fix the SAAS-42 translation bug"):
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO project VALUES('p1','/Users/me/globex/widgets-hq','git','hq',?,?,'[]')", (T0, T0))
    conn.execute("INSERT INTO workspace VALUES('w1','local',NULL,?,?)", (T0, T0))
    conn.execute("INSERT INTO session_v2(id, project_id, workspace_id, parent_id, slug, directory, title, version, "
                 "agent, model, time_created, time_updated) VALUES('ses_1','p1','w1',NULL,'misty-planet',"
                 "'/Users/me/globex/widgets-hq/.claude/worktrees/ai','Diagnosing failed translations','2.1.4',"
                 "'build',?,?,?)",
                 (json.dumps({"id": "qwen3.8:27b-mlx", "providerID": "ollama"}), T0, T0 + 60_000))
    _row(conn, "msg_u1", "ses_1", "user", 0, T0, {
        "text": first_prompt, "files": [], "agents": [], "time": {"created": T0}})
    _row(conn, "msg_a1", "ses_1", "assistant", 1, T0 + 1000, {
        "agent": "build", "model": MODEL, "finish": "tool-calls", "cost": 0.0123,
        "tokens": {"input": 10201, "output": 155, "reasoning": 7, "cache": {"read": 128, "write": 0}},
        "time": {"created": T0 + 1000, "completed": T0 + 5000, "streamed": T0 + 4000},
        "content": [
            {"type": "reasoning", "text": "Let me look at the command.", "state": {"reasoningField": "r"},
             "time": {"created": T0 + 1100, "completed": T0 + 1150}},
            {"type": "text", "text": "I'll investigate `translate_app`."},
            {"type": "text", "text": "Starting with grep."},
            {"type": "tool", "id": "prt_a5", "name": "grep", "executed": True,
             "time": {"created": T0 + 1400, "ran": T0 + 1410, "completed": T0 + 1420},
             "state": {"status": "completed", "input": {"pattern": "translate_app", "path": "myapp"},
                       "content": [{"type": "text", "text": "myapp/management/commands/translate_app.py"}],
                       "metadata": {"matches": 1, "truncated": False}}},
            {"type": "tool", "id": "prt_a6", "name": "read", "executed": True,
             "time": {"created": T0 + 1500, "ran": T0 + 1510, "completed": T0 + 1520},
             "state": {"status": "completed", "input": {"path": "myapp/x.py"},
                       "content": [{"type": "text", "text": "1: print(1)"}, {"type": "text", "text": "2: print(2)"}],
                       "metadata": {"truncated": False}}},
            {"type": "flux-capacitor", "x": 1},
        ]})
    _row(conn, "msg_i1", "ses_1", "idle", 2, T0 + 5000, {"outcome": "succeeded", "time": {"created": T0 + 5000}})
    _row(conn, "msg_s1", "ses_1", "synthetic", 3, T0 + 5100, {
        "text": "<system-reminder>The user opened a file.</system-reminder>", "time": {"created": T0 + 5100}})
    _row(conn, "msg_k1", "ses_1", "compaction", 4, T0 + 5200, {
        "status": "completed", "reason": "auto", "model": MODEL, "summary": "We grepped.", "cost": 0,
        "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "time": {"created": T0 + 5200}})
    if with_child:
        conn.execute("INSERT INTO session_v2(id, project_id, workspace_id, parent_id, slug, directory, title, "
                     "version, agent, model, time_created, time_updated) VALUES('ses_child','p1',NULL,'ses_1','kid',"
                     "'/Users/me/globex/widgets-hq','Subagent: grep files','2.1.4','explore',NULL,?,?)",
                     (T0 + 2000, T0 + 3000))
        _row(conn, "msg_c1", "ses_child", "user", 0, T0 + 2000, {
            "text": "find translation files", "files": [], "agents": [], "time": {"created": T0 + 2000}})
    conn.commit(); conn.close()
    return path


@pytest.fixture
def ocdb(tmp_path):
    return make_opencode_v2_db(tmp_path / "opencode.db", with_child=True)


def _load(path, native_id="ses_1"):
    ad = oc.OpencodeAdapter(path)
    return ad.load(next(r for r in ad.discover() if r.native_id == native_id))


def _set_revert(path, revert):
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE session_v2 SET revert=? WHERE id='ses_1'", (json.dumps(revert),))
    conn.commit(); conn.close()


def _set_message(path, mid, data):
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE session_message SET data=? WHERE id=?",
                 (json.dumps(data) if not isinstance(data, str) else data, mid))
    conn.commit(); conn.close()


def test_discover_lists_sessions_with_time_updated_key(ocdb):
    refs = list(oc.OpencodeAdapter(ocdb).discover())
    assert [(r.source, r.native_id, r.change_key) for r in refs] == [
        ("opencode", "ses_1", str(T0 + 60_000)), ("opencode", "ses_child", str(T0 + 3000))]


def test_load_normalizes_messages_in_content_order(ocdb):
    conv, revision = _load(ocdb)
    assert revision == str(T0 + 60_000)
    assert conv.id == "opencode:ses_1"
    assert conv.title == "Diagnosing failed translations"
    assert conv.first_prompt == "Fix the SAAS-42 translation bug"
    assert conv.cwd == "/Users/me/globex/widgets-hq/.claude/worktrees/ai"
    assert conv.project_root == "/Users/me/globex/widgets-hq"
    assert conv.git_branch is None             # 2.x workspaces no longer record a branch
    assert conv.provider == "ollama" and conv.models == ["qwen3.8:27b-mlx"]
    assert conv.start_ts == "2026-09-13T05:38:10.555Z"
    assert conv.relation is None
    assert conv.metadata["agent"] == "build" and conv.metadata["opencode_version"] == "2.1.4"
    assert conv.warnings == ["unknown part type 'flux-capacitor'"]
    assert conv.fidelity == "partial"

    kinds = [(m.role, m.kind, m.native_id) for m in conv.messages]
    assert kinds == [
        ("user", KIND_TEXT, "msg_u1"),
        ("assistant", KIND_THINKING, "msg_a1/0"),
        ("assistant", KIND_TEXT, "msg_a1/1"),        # two consecutive text items merged
        ("assistant", KIND_TOOL_CALL, "prt_a5"),
        ("assistant", KIND_TOOL_CALL, "prt_a6"),
    ]
    assert conv.messages[1].ts == "2026-09-13T05:38:11.655Z"
    assert conv.messages[2].text == "I'll investigate `translate_app`.\n\nStarting with grep."
    assert conv.messages[2].model == "qwen3.8:27b-mlx"
    grep = conv.messages[3]
    assert grep.text.startswith('[grep] {"pattern": "translate_app"') and "translate_app.py" in grep.text
    assert grep.tool_call.name == "grep" and grep.tool_call.native_id == "prt_a5"
    assert grep.tool_call.args == {"pattern": "translate_app", "path": "myapp"}
    read = conv.messages[4]
    assert read.tool_call.file_actions == [("read", "myapp/x.py")]
    assert read.tool_call.result_text == "1: print(1)\n2: print(2)"   # content items joined


def test_usage_comes_from_the_assistant_row(ocdb):
    conv, _ = _load(ocdb)
    assert len(conv.usage) == 1
    u = conv.usage[0]
    assert (u.provider, u.model, u.input_uncached, u.cache_read, u.cache_write, u.output, u.reasoning,
            u.reported_cost_usd) == ("ollama", "qwen3.8:27b-mlx", 10201, 128, 0, 155, 7, 0.0123)
    assert u.native_request_id == "msg_a1" and u.message_seq == 1


def test_idle_synthetic_and_compaction_rows_are_not_transcript(ocdb):
    conv, _ = _load(ocdb)
    assert not any("system-reminder" in m.text or "We grepped." in m.text for m in conv.messages)
    assert conv.warnings == ["unknown part type 'flux-capacitor'"]   # bookkeeping rows raise none


def test_session_with_only_bookkeeping_rows_is_confirmed_empty(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("DELETE FROM session_message WHERE type IN ('user', 'assistant')"); conn.commit(); conn.close()
    assert _load(p) is None


def test_unknown_row_type_is_a_warning(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    _row(conn, "msg_x", "ses_1", "hologram", 9, T0 + 9000, {"time": {"created": T0 + 9000}})
    conn.commit(); conn.close()
    conv, _ = _load(p)
    assert "unknown message type 'hologram'" in conv.warnings and conv.fidelity == "partial"


def test_session_of_only_unknown_rows_is_unreadable_not_empty(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE session_message SET type='hologram' WHERE type IN ('user', 'assistant')")
    conn.commit(); conn.close()
    with pytest.raises(UnreadableSource, match="hologram"):
        _load(p)


def test_errored_assistant_turn_has_no_usage_record(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    _set_message(p, "msg_a1", {   # a provider error: no tokens, no cost, text so far kept
        "model": MODEL, "finish": "error", "error": {"type": "provider.transport", "message": "boom"},
        "time": {"created": T0 + 1000}, "content": [{"type": "text", "text": "Partial answer"}]})
    conv, _ = _load(p)
    assert [m.text for m in conv.messages if m.role == "assistant"] == ["Partial answer"]
    assert conv.usage == []


def test_text_item_that_is_not_a_string_is_skipped(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    _set_message(p, "msg_a1", {"model": MODEL, "time": {"created": T0 + 1000}, "content": [
        {"type": "text", "text": {"nested": 1}}, {"type": "text", "text": "kept"}]})
    conv, _ = _load(p)
    assert [m.text for m in conv.messages if m.role == "assistant"] == ["kept"]


def test_child_session_carries_subagent_relation(ocdb):
    conv, _ = _load(ocdb, "ses_child")
    assert conv.relation == ("subagent", "opencode:ses_1")
    assert conv.first_prompt == "find translation files"


def test_revert_drops_reverted_turns(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    _set_revert(p, {"messageID": "msg_a1"})
    conv, _ = _load(p)
    assert [(m.role, m.kind) for m in conv.messages] == [("user", KIND_TEXT)]
    assert conv.usage == []


def test_revert_with_part_boundary_keeps_earlier_items(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    _set_revert(p, {"messageID": "msg_a1", "partID": "prt_a5"})
    conv, _ = _load(p)
    assert [(m.role, m.kind) for m in conv.messages] == [
        ("user", KIND_TEXT), ("assistant", KIND_THINKING), ("assistant", KIND_TEXT)]


def test_tool_error_text_is_indexed(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    _set_message(p, "msg_a1", {
        "model": MODEL, "time": {"created": T0 + 1000}, "content": [
            {"type": "tool", "id": "prt_e", "name": "shell", "executed": True,
             "state": {"status": "error", "input": {"command": "x"},
                       "error": {"type": "tool.error", "message": "command not found: x"}}}]})
    conv, _ = _load(p)
    shell = next(m for m in conv.messages if m.tool_call and m.tool_call.name == "shell")
    assert shell.text.endswith("STATUS: error\nERROR: command not found: x")


def test_malformed_row_json_is_a_warning_and_partial_fidelity(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    _set_message(p, "msg_a1", "{not json")
    conv, _ = _load(p)
    assert "malformed message json msg_a1" in conv.warnings and conv.fidelity == "partial"
    assert [m.role for m in conv.messages] == ["user"]


def test_content_that_is_not_a_list_is_malformed(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    _set_message(p, "msg_a1", {"model": MODEL, "time": {"created": T0 + 1000}, "content": "nope"})
    conv, _ = _load(p)
    assert "malformed message json msg_a1" in conv.warnings and [m.role for m in conv.messages] == ["user"]


def test_all_malformed_session_is_unreadable_not_empty(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE session_message SET data='{broken' WHERE type IN ('user', 'assistant')")
    conn.commit(); conn.close()
    with pytest.raises(UnreadableSource):
        _load(p)


def test_load_excludes_indexers_own_prompt(tmp_path):
    from convo_index.config import CLASSIFY_PROMPT_PREFIX
    p = make_opencode_v2_db(tmp_path / "oc.db", first_prompt=CLASSIFY_PROMPT_PREFIX + " ...")
    assert _load(p) is None


def test_migrated_db_that_still_carries_1x_tables_reads_the_2x_layout(tmp_path):
    from tests.test_opencode import _SCHEMA as V1_SCHEMA
    p = make_opencode_v2_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.executescript(V1_SCHEMA.replace("CREATE TABLE project(", "CREATE TABLE IF NOT EXISTS project(")
                       .replace("CREATE TABLE workspace(", "CREATE TABLE IF NOT EXISTS workspace("))
    conn.execute("ALTER TABLE workspace ADD COLUMN branch text")   # so the 1.x layout matches completely too
    conn.execute("INSERT INTO session(id, project_id, slug, directory, title, version, time_created, time_updated)"
                 " VALUES('ses_old','p1','old','/x','Stale copy','1.18.25',?,?)", (T0, T0))
    conn.commit(); conn.close()
    assert [r.native_id for r in oc.OpencodeAdapter(p).discover()] == ["ses_1"]


def test_check_schema_covers_every_column_load_reads(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("ALTER TABLE session_v2 DROP COLUMN agent"); conn.commit(); conn.close()
    with pytest.raises(RuntimeError, match="'session_v2' is missing \\['agent'\\]"):
        list(oc.OpencodeAdapter(p).discover())


def test_user_row_without_text_is_malformed_not_silently_empty(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    _set_message(p, "msg_u1", {"text": 42, "time": {"created": T0}})
    conv, _ = _load(p)
    assert "malformed message json msg_u1" in conv.warnings and conv.fidelity == "partial"
    assert conv.first_prompt == "" and len(conv.messages) == 4


def test_item_without_a_type_is_malformed(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    _set_message(p, "msg_a1", {"model": MODEL, "time": {"created": T0 + 1000}, "content": [
        {"text": "lost"}, {"type": "text", "text": "kept"}]})
    conv, _ = _load(p)
    assert [m.text for m in conv.messages if m.role == "assistant"] == ["kept"]
    assert "malformed part json msg_a1/0" in conv.warnings


def test_wrongly_typed_nested_fields_do_not_abort_the_load(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    _set_message(p, "msg_a1", {"model": MODEL, "time": 3, "tokens": [1], "cost": "free", "content": [
        {"type": "text", "text": "before", "time": 3},
        {"type": "tool", "id": "prt_t", "name": "shell", "time": 3,
         "state": {"status": "completed", "input": {"command": "ls"}, "content": 3, "error": ["x"]}},
        {"type": "reasoning", "text": 42}]})
    conv, _ = _load(p)
    kinds = [(m.kind, m.text.split("\n")[0]) for m in conv.messages if m.role == "assistant"]
    assert kinds == [(KIND_TEXT, "before"), (KIND_TOOL_CALL, '[shell] {"command": "ls"}')]
    assert conv.usage == [] and "malformed part json msg_a1/2" in conv.warnings


def test_absent_part_boundary_cuts_at_the_first_later_tool_id(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    content = [{"type": "text", "text": f"t{i}"} for i in range(11)]
    content += [{"type": "tool", "id": "prt_b", "name": "read", "state": {"status": "completed", "input": {"path": "x"}}},
                {"type": "tool", "id": "prt_d", "name": "read", "state": {"status": "completed", "input": {"path": "y"}}}]
    _set_message(p, "msg_a1", {"model": MODEL, "time": {"created": T0 + 1000}, "content": content})
    _set_revert(p, {"messageID": "msg_a1", "partID": "prt_c"})   # the boundary part is gone
    conv, _ = _load(p)
    assistant = [m for m in conv.messages if m.role == "assistant"]
    assert assistant[0].text.endswith("t10") and [m.native_id for m in assistant[1:]] == ["prt_b"]


def test_exact_part_boundary_wins_over_the_fallback(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    _set_message(p, "msg_a1", {"model": MODEL, "time": {"created": T0 + 1000}, "content": [
        {"type": "text", "text": "first"},
        {"type": "tool", "id": "prt_b", "name": "read", "state": {"status": "completed", "input": {"path": "x"}}},
        {"type": "text", "text": "second"}]})
    _set_revert(p, {"messageID": "msg_a1", "partID": "msg_a1/2"})
    conv, _ = _load(p)
    assert [m.native_id for m in conv.messages if m.role == "assistant"] == ["msg_a1/0", "prt_b"]


def test_warnings_from_reverted_rows_do_not_make_an_empty_session_unreadable(tmp_path):
    p = make_opencode_v2_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    _row(conn, "msg_x", "ses_1", "hologram", 9, T0 + 9000, {"time": {"created": T0 + 9000}})
    conn.commit(); conn.close()
    _set_revert(p, {"messageID": "msg_u1"})   # everything reverted, the unknown row included
    assert _load(p) is None


def test_schema_error_names_both_layouts(tmp_path):
    p = tmp_path / "weird.db"
    conn = sqlite3.connect(str(p)); conn.execute("CREATE TABLE session(id text)"); conn.commit(); conn.close()
    with pytest.raises(RuntimeError, match="session_v2"):
        list(oc.OpencodeAdapter(p).discover())
