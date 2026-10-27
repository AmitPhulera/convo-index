"""Opencode adapter against a synthetic database shaped like Opencode 1.18's."""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from convo_index.sources import opencode as oc
from convo_index.model import KIND_TEXT, KIND_THINKING, KIND_TOOL_CALL

_SCHEMA = """
CREATE TABLE project(id text PRIMARY KEY, worktree text NOT NULL, vcs text, name text,
    time_created integer NOT NULL, time_updated integer NOT NULL, sandboxes text NOT NULL);
CREATE TABLE workspace(id text PRIMARY KEY, type text NOT NULL, name text, branch text,
    directory text, project_id text NOT NULL, time_used integer NOT NULL);
CREATE TABLE session(id text PRIMARY KEY, project_id text NOT NULL, workspace_id text,
    parent_id text, slug text NOT NULL, directory text NOT NULL, path text, title text NOT NULL,
    version text NOT NULL, share_url text, cost real DEFAULT 0 NOT NULL,
    tokens_input integer DEFAULT 0, tokens_output integer DEFAULT 0, revert text,
    agent text, model text, time_created integer NOT NULL, time_updated integer NOT NULL,
    time_archived integer);
CREATE TABLE message(id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL,
    time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE part(id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
    time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
"""

T0 = 1789277890555  # epoch ms, 2026-09-13T05:38:10.555Z


def make_opencode_db(path, with_child=False, first_prompt="Fix the SAAS-42 translation bug"):
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO project VALUES('p1','/Users/me/globex/widgets-hq','git','hq',?,?,'[]')", (T0, T0))
    conn.execute("INSERT INTO workspace VALUES('w1','worktree','ai',"
                 "'ap/ai-app-translations','/Users/me/globex/widgets-hq/.claude/worktrees/ai','p1',?)", (T0,))
    conn.execute("INSERT INTO session(id, project_id, workspace_id, parent_id, slug, directory, title, version, "
                 "agent, model, time_created, time_updated) VALUES('ses_1','p1','w1',NULL,'misty-planet',"
                 "'/Users/me/globex/widgets-hq/.claude/worktrees/ai','Diagnosing failed translations','1.18.25',"
                 "'build',?,?,?)",
                 (json.dumps({"id": "qwen3.8:27b-mlx", "providerID": "ollama"}), T0, T0 + 60_000))
    # user message: one text part
    conn.execute("INSERT INTO message VALUES('msg_u1','ses_1',?,?,?)", (T0, T0, json.dumps({
        "role": "user", "time": {"created": T0}, "agent": "build",
        "model": {"providerID": "ollama", "modelID": "qwen3.8:27b-mlx"}})))
    conn.execute("INSERT INTO part VALUES('prt_u1a','msg_u1','ses_1',?,?,?)", (T0, T0, json.dumps({
        "type": "text", "text": first_prompt})))
    # assistant message: step-start, reasoning, text, tool, step-finish (which repeats tokens)
    a = json.dumps({"parentID": "msg_u1", "role": "assistant", "mode": "build", "agent": "build",
                    "cost": 0.0123, "tokens": {"total": 10356, "input": 10201, "output": 155, "reasoning": 7,
                                               "cache": {"read": 128, "write": 0}},
                    "modelID": "qwen3.8:27b-mlx", "providerID": "ollama",
                    "time": {"created": T0 + 1000, "completed": T0 + 5000}, "finish": "tool-calls"})
    conn.execute("INSERT INTO message VALUES('msg_a1','ses_1',?,?,?)", (T0 + 1000, T0 + 5000, a))
    parts = [
        ("prt_a1", {"type": "step-start", "snapshot": "abc"}),
        ("prt_a2", {"type": "reasoning", "text": "Let me look at the command.", "time": {"start": T0 + 1100}}),
        ("prt_a3", {"type": "text", "text": "I'll investigate `translate_app`.", "time": {"start": T0 + 1200}}),
        ("prt_a4", {"type": "text", "text": "Starting with grep.", "time": {"start": T0 + 1300}}),
        ("prt_a5", {"type": "tool", "tool": "grep", "callID": "call_1",
                    "state": {"status": "completed", "input": {"pattern": "translate_app", "path": "myapp"},
                              "output": "myapp/management/commands/translate_app.py"}}),
        ("prt_a6", {"type": "tool", "tool": "read", "callID": "call_2",
                    "state": {"status": "completed", "input": {"filePath": "myapp/x.py"},
                              "output": {"lines": 1, "preview": "print(1)"}}}),
        ("prt_a7", {"type": "step-finish", "reason": "tool-calls",
                    "tokens": {"total": 10356, "input": 10201, "output": 155}, "cost": 0.0123}),
        ("prt_a8", {"type": "flux-capacitor", "x": 1}),
    ]
    for i, (pid, data) in enumerate(parts):
        conn.execute("INSERT INTO part VALUES(?,?,?,?,?,?)", (pid, "msg_a1", "ses_1", T0 + i, T0 + i, json.dumps(data)))
    if with_child:
        conn.execute("INSERT INTO session(id, project_id, workspace_id, parent_id, slug, directory, title, version, "
                     "agent, model, time_created, time_updated) VALUES('ses_child','p1',NULL,'ses_1','kid',"
                     "'/Users/me/globex/widgets-hq','Subagent: grep files','1.18.25','explore',NULL,?,?)",
                     (T0 + 2000, T0 + 3000))
        conn.execute("INSERT INTO message VALUES('msg_c1','ses_child',?,?,?)", (T0 + 2000, T0 + 2000, json.dumps(
            {"role": "user", "time": {"created": T0 + 2000}})))
        conn.execute("INSERT INTO part VALUES('prt_c1','msg_c1','ses_child',?,?,?)", (T0 + 2000, T0 + 2000, json.dumps(
            {"type": "text", "text": "find translation files"})))
    conn.commit(); conn.close()
    return path


@pytest.fixture
def ocdb(tmp_path):
    return make_opencode_db(tmp_path / "opencode.db", with_child=True)


def test_discover_lists_sessions_with_time_updated_key(ocdb):
    refs = list(oc.OpencodeAdapter(ocdb).discover())
    assert [(r.source, r.native_id, r.change_key) for r in refs] == [
        ("opencode", "ses_1", str(T0 + 60_000)), ("opencode", "ses_child", str(T0 + 3000))]
    assert refs[0].id == "opencode:ses_1"


def test_load_normalizes_messages_in_part_order(ocdb):
    ad = oc.OpencodeAdapter(ocdb)
    ref = next(r for r in ad.discover() if r.native_id == "ses_1")
    conv, revision = ad.load(ref)
    assert revision == str(T0 + 60_000)
    assert conv.id == "opencode:ses_1"
    assert conv.title == "Diagnosing failed translations"
    assert conv.first_prompt == "Fix the SAAS-42 translation bug"
    assert conv.cwd == "/Users/me/globex/widgets-hq/.claude/worktrees/ai"
    assert conv.project_root == "/Users/me/globex/widgets-hq"
    assert conv.git_branch == "ap/ai-app-translations"
    assert conv.provider == "ollama" and conv.models == ["qwen3.8:27b-mlx"]
    assert conv.start_ts == datetime.fromtimestamp(T0 / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.555Z")
    assert conv.relation is None
    assert conv.metadata["agent"] == "build" and conv.metadata["opencode_version"] == "1.18.25"
    assert conv.warnings == ["unknown part type 'flux-capacitor'"]
    assert conv.fidelity == "partial"  # unknown parts mean we may have dropped content

    kinds = [(m.role, m.kind, m.native_id) for m in conv.messages]
    assert kinds == [
        ("user", KIND_TEXT, "prt_u1a"),
        ("assistant", KIND_THINKING, "prt_a2"),
        ("assistant", KIND_TEXT, "prt_a3"),          # two consecutive text parts merged
        ("assistant", KIND_TOOL_CALL, "prt_a5"),
        ("assistant", KIND_TOOL_CALL, "prt_a6"),
    ]
    assert conv.messages[2].text == "I'll investigate `translate_app`.\n\nStarting with grep."
    assert conv.messages[2].model == "qwen3.8:27b-mlx"
    grep = conv.messages[3]
    assert grep.text.startswith('[grep] {"pattern": "translate_app"') and "translate_app.py" in grep.text
    assert grep.tool_call.name == "grep" and grep.tool_call.native_id == "call_1"
    assert conv.messages[4].tool_call.file_actions == [("read", "myapp/x.py")]
    # structured tool output is serialized, not dropped
    assert conv.messages[4].tool_call.result_text == '{"lines": 1, "preview": "print(1)"}'
    assert '"preview": "print(1)"' in conv.messages[4].text


def test_usage_comes_from_assistant_message_not_step_finish(ocdb):
    ad = oc.OpencodeAdapter(ocdb)
    conv, _ = ad.load(next(r for r in ad.discover() if r.native_id == "ses_1"))
    assert len(conv.usage) == 1
    u = conv.usage[0]
    assert (u.native_request_id, u.provider, u.model) == ("msg_a1", "ollama", "qwen3.8:27b-mlx")
    assert (u.input_uncached, u.cache_read, u.cache_write, u.output, u.reasoning) == (10201, 128, 0, 155, 7)
    assert u.reported_cost_usd == 0.0123
    assert u.message_seq == 1  # first normalized message of that assistant turn (the thinking)


def test_child_session_carries_subagent_relation(ocdb):
    ad = oc.OpencodeAdapter(ocdb)
    conv, _ = ad.load(next(r for r in ad.discover() if r.native_id == "ses_child"))
    assert conv.relation == ("subagent", "opencode:ses_1")
    assert conv.parent_id == "opencode:ses_1"
    assert conv.git_branch is None and conv.models == []


def test_load_excludes_indexers_own_prompt(tmp_path):
    from convo_index.config import CLASSIFY_PROMPT_PREFIX
    p = make_opencode_db(tmp_path / "oc.db", first_prompt=CLASSIFY_PROMPT_PREFIX + " as work or personal")
    ad = oc.OpencodeAdapter(p)
    assert ad.load(next(iter(ad.discover()))) is None


def test_load_missing_session_returns_none(ocdb):
    from convo_index.sources.base import SourceRef
    assert oc.OpencodeAdapter(ocdb).load(SourceRef("opencode", "nope", "0", str(ocdb))) is None


def test_available_false_when_db_missing(tmp_path):
    assert oc.OpencodeAdapter(tmp_path / "none.db").available() is False


def test_locate_db_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "custom.db"))
    assert oc.locate_db() == tmp_path / "custom.db"


def test_locate_db_falls_back_to_default_without_binary(monkeypatch):
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setattr(oc.shutil, "which", lambda name: None)
    assert oc.locate_db() == oc.default_db()


def test_resume_command():
    assert oc.resume_command("opencode:ses_1", "/x") == "opencode --session ses_1"


def test_revert_drops_reverted_turns(tmp_path):
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    # revert to before the assistant message: only the user prompt remains
    conn.execute("UPDATE session SET revert=? WHERE id='ses_1'", (json.dumps({"messageID": "msg_a1"}),))
    conn.commit(); conn.close()
    ad = oc.OpencodeAdapter(p)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert [(m.role, m.kind) for m in conv.messages] == [("user", KIND_TEXT)]
    assert conv.usage == []


def test_revert_with_part_boundary_keeps_earlier_parts(tmp_path):
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE session SET revert=? WHERE id='ses_1'",
                 (json.dumps({"messageID": "msg_a1", "partID": "prt_a5"}),))
    conn.commit(); conn.close()
    ad = oc.OpencodeAdapter(p)
    conv, _ = ad.load(next(iter(ad.discover())))
    kinds = [(m.role, m.kind) for m in conv.messages]
    assert kinds == [("user", KIND_TEXT), ("assistant", KIND_THINKING), ("assistant", KIND_TEXT)]


def test_tool_error_text_is_indexed(tmp_path):
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE part SET data=? WHERE id='prt_a5'", (json.dumps(
        {"type": "tool", "tool": "bash", "callID": "c", "state": {"status": "error", "input": {"command": "x"},
                                                                    "error": "command not found: x"}}),))
    conn.commit(); conn.close()
    ad = oc.OpencodeAdapter(p)
    conv, _ = ad.load(next(iter(ad.discover())))
    bash = next(m for m in conv.messages if m.tool_call and m.tool_call.name == "bash")
    assert "ERROR: command not found: x" in bash.text


def test_malformed_part_json_is_a_warning_and_partial_fidelity(tmp_path):
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE part SET data='{not json' WHERE id='prt_a4'"); conn.commit(); conn.close()
    ad = oc.OpencodeAdapter(p)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert any("malformed part json prt_a4" in w for w in conv.warnings)
    assert conv.fidelity == "partial"


def test_non_string_text_part_is_a_warning_not_silently_dropped(tmp_path):
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE part SET data=? WHERE id='prt_a4'", (json.dumps({"type": "text", "text": 42}),))
    conn.commit(); conn.close()
    ad = oc.OpencodeAdapter(p)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert "malformed part json prt_a4" in conv.warnings and conv.fidelity == "partial"
    assert conv.messages[2].text == "I'll investigate `translate_app`."


def test_session_whose_only_text_is_not_a_string_is_unreadable(tmp_path):
    from convo_index.sources.base import UnreadableSource
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("DELETE FROM message WHERE id='msg_a1'"); conn.execute("DELETE FROM part WHERE message_id='msg_a1'")
    conn.execute("UPDATE part SET data=? WHERE id='prt_u1a'", (json.dumps({"type": "text", "text": 42}),))
    conn.commit(); conn.close()
    ad = oc.OpencodeAdapter(p)
    with pytest.raises(UnreadableSource):
        ad.load(next(iter(ad.discover())))


def test_all_malformed_session_is_unreadable_not_empty(tmp_path):
    from convo_index.sources.base import UnreadableSource
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE part SET data='{broken'"); conn.commit(); conn.close()
    ad = oc.OpencodeAdapter(p)
    with pytest.raises(UnreadableSource):
        ad.load(next(iter(ad.discover())))


def test_session_with_only_step_markers_is_confirmed_empty(tmp_path):
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("DELETE FROM part WHERE id != 'prt_a1'"); conn.commit(); conn.close()   # only step-start left
    ad = oc.OpencodeAdapter(p)
    assert ad.load(next(iter(ad.discover()))) is None


def test_parts_follow_id_order_like_opencode_even_if_timestamps_disagree(tmp_path):
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    # make the later-id text part carry an EARLIER insertion timestamp
    conn.execute("UPDATE part SET time_created=? WHERE id='prt_a4'", (T0 - 5000,)); conn.commit(); conn.close()
    ad = oc.OpencodeAdapter(p)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert conv.messages[2].text == "I'll investigate `translate_app`.\n\nStarting with grep."


def test_tool_error_survives_a_huge_input(tmp_path):
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE part SET data=? WHERE id='prt_a5'", (json.dumps(
        {"type": "tool", "tool": "write", "callID": "c", "state": {"status": "error",
         "input": {"filePath": "x.py", "content": "x" * 10000}, "error": "disk full"}}),))
    conn.commit(); conn.close()
    ad = oc.OpencodeAdapter(p)
    conv, _ = ad.load(next(iter(ad.discover())))
    w = next(m for m in conv.messages if m.tool_call and m.tool_call.name == "write")
    assert w.text.endswith("STATUS: error\nERROR: disk full") and "…[tool text truncated]" in w.text
    assert len(w.text) <= oc.TOOL_TEXT_LIMIT + 40


def test_check_schema_covers_every_column_load_reads(tmp_path):
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("ALTER TABLE session DROP COLUMN agent"); conn.commit(); conn.close()
    with pytest.raises(RuntimeError, match="agent"):
        list(oc.OpencodeAdapter(p).discover())


def test_discover_rejects_unknown_schema(tmp_path):
    p = tmp_path / "weird.db"
    conn = sqlite3.connect(str(p)); conn.execute("CREATE TABLE session(id text)"); conn.commit(); conn.close()
    with pytest.raises(RuntimeError, match="missing"):
        list(oc.OpencodeAdapter(p).discover())


def test_default_db_honours_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setattr(oc.shutil, "which", lambda name: None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert oc.locate_db() == tmp_path / "xdg" / "opencode" / "opencode.db"


def test_non_object_json_columns_are_malformed_not_fatal(tmp_path):
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p))
    conn.execute("INSERT INTO message VALUES('msg_bad','ses_1',?,?,?)", (T0 + 500, T0 + 500, "null"))
    conn.execute("INSERT INTO part VALUES('prt_bad','msg_a1','ses_1',?,?,?)", (T0 + 50, T0 + 50, "[]"))
    conn.commit(); conn.close()
    ad = oc.OpencodeAdapter(p)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert any("Starting with grep." in m.text for m in conv.messages) and conv.fidelity == "partial"


def test_db_path_with_uri_metacharacters(tmp_path):
    p = make_opencode_db(tmp_path / "oc#archive.db")
    assert [r.native_id for r in oc.OpencodeAdapter(p).discover()] == ["ses_1"]


def test_project_table_without_vcs_column_loads(tmp_path):
    p = make_opencode_db(tmp_path / "oc.db")
    conn = sqlite3.connect(str(p)); conn.execute("ALTER TABLE project DROP COLUMN vcs"); conn.commit(); conn.close()
    ad = oc.OpencodeAdapter(p)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert conv.cwd.endswith("/.claude/worktrees/ai")
