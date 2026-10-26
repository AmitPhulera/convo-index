"""Antigravity CLI adapter against a synthetic home shaped like the real one."""
import json
import sqlite3

import pytest

from convo_index.sources import antigravity as ag
from convo_index.sources.base import UnreadableSource
from convo_index.model import KIND_TEXT, KIND_THINKING, KIND_TOOL_CALL, KIND_TOOL_RESULT

CID = "000befc0-a523-4281-9aec-b15af15f6faf"


def _rec(idx, typ, source, **kw):
    d = {"step_index": idx, "source": source, "type": typ, "status": "DONE", "created_at": f"2026-08-30T19:29:{40 + idx:02d}Z"}
    d.update(kw)
    return json.dumps(d)


def default_records(first="create a worktree to plan automated restarts"):
    return [
        _rec(0, "USER_INPUT", "USER_EXPLICIT", content=f"<USER_REQUEST>\n{first}\n</USER_REQUEST>"),
        _rec(1, "PLANNER_RESPONSE", "MODEL", tool_calls=[{"name": "run_command", "args": {"CommandLine": "git status", "Cwd": "/Users/me/widgets-cloud"}}]),
        _rec(2, "GENERIC", "MODEL", content="The command exited with code 0.\nOutput:\nOn branch master"),
        _rec(3, "PLANNER_RESPONSE", "MODEL", thinking="Exploring available worktrees.",
             tool_calls=[{"name": "find_by_name", "args": "{\"Pattern\": \"*\", \"SearchDirectory\": \"/Users/me/x\"}"}]),
        _rec(4, "GENERIC", "MODEL", content="a.md\nb.md"),
        _rec(5, "PLANNER_RESPONSE", "MODEL", content="I have created the worktree and drafted the plan."),
    ]


def make_home(tmp_path, records=None, cid=CID, with_summary=True, parent=None):
    home = tmp_path / "agy"
    logs = home / "brain" / cid / ".system_generated" / "logs"; logs.mkdir(parents=True)
    (logs / "transcript_full.jsonl").write_text("\n".join(records if records is not None else default_records()) + "\n")
    (home / "brain" / "no-transcript-here").mkdir()
    if with_summary:
        conn = sqlite3.connect(str(home / "conversation_summaries.db"))
        conn.execute("CREATE TABLE conversation_summaries(conversation_id text, title text NOT NULL DEFAULT '', "
                     "workspace_uris text NOT NULL, parent_conversation_id text NOT NULL DEFAULT '', PRIMARY KEY(conversation_id))")
        conn.execute("INSERT INTO conversation_summaries VALUES(?,?,?,?)",
                     (cid, "EC2 restart automation", json.dumps(["file:///Users/me/widgets-cloud"]), parent or ""))
        conn.commit(); conn.close()
    return home


def test_discover_skips_dirs_without_transcript(tmp_path):
    home = make_home(tmp_path)
    refs = list(ag.AntigravityAdapter(home).discover())
    assert [r.native_id for r in refs] == [CID] and refs[0].id == f"antigravity:{CID}"


def test_load_normalizes_transcript(tmp_path):
    home = make_home(tmp_path)
    ad = ag.AntigravityAdapter(home)
    conv, revision = ad.load(next(iter(ad.discover())))
    assert conv.title == "EC2 restart automation" and conv.cwd == "/Users/me/widgets-cloud"
    assert conv.metadata["cwd_source"] == "summaries"
    assert conv.first_prompt == "create a worktree to plan automated restarts"   # wrapper stripped
    kinds = [(m.role, m.kind) for m in conv.messages]
    assert kinds == [("user", KIND_TEXT), ("assistant", KIND_TOOL_CALL), ("tool", KIND_TOOL_RESULT),
                     ("assistant", KIND_THINKING), ("assistant", KIND_TOOL_CALL), ("tool", KIND_TOOL_RESULT),
                     ("assistant", KIND_TEXT)]
    assert conv.messages[1].tool_call.name == "run_command" and conv.messages[1].tool_call.args["Cwd"] == "/Users/me/widgets-cloud"
    assert conv.messages[4].tool_call.args == {"Pattern": "*", "SearchDirectory": "/Users/me/x"}   # string args parsed
    assert conv.messages[3].text == "Exploring available worktrees."
    assert conv.provider == "google" and conv.models == [] and conv.usage == []
    assert conv.warnings == ["source records no model or token usage"] and conv.fidelity == "full"
    assert conv.start_ts == "2026-08-30T19:29:40Z" and conv.end_ts == "2026-08-30T19:29:45Z"
    assert [m.native_id for m in conv.messages][:3] == ["0", "1:tool0", "2"]
    assert conv.messages[2].tool_call.native_id == "1:0"              # result linked to the call that produced it
    assert conv.messages[5].tool_call.native_id == "3:0"
    assert revision.endswith("|EC2 restart automation|/Users/me/widgets-cloud|")


def test_cwd_falls_back_to_tool_args_and_title_to_first_prompt(tmp_path):
    home = make_home(tmp_path, with_summary=False)
    ad = ag.AntigravityAdapter(home)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert conv.title == "create a worktree to plan automated restarts" and conv.cwd == "/Users/me/widgets-cloud"
    assert conv.metadata["cwd_source"] == "tool_args"


def test_unwrap_tolerates_missing_closing_tag():
    assert ag._unwrap("<USER_REQUEST>\nhello\n</USER_REQUEST>") == "hello"
    assert ag._unwrap("<USER_REQUEST>\nhello there") == "hello there"
    assert ag._unwrap("<USER_REQUEST>a</USER_REQUEST> trailing") == "a trailing"
    assert ag._unwrap("plain") == "plain"


def test_parent_relation_from_summaries(tmp_path):
    home = make_home(tmp_path, parent="parent-id")
    ad = ag.AntigravityAdapter(home)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert conv.relation == ("subagent", "antigravity:parent-id")


def test_own_llm_calls_are_excluded(tmp_path):
    from convo_index.config import CLASSIFY_PROMPT_PREFIX
    home = make_home(tmp_path, records=default_records(first=CLASSIFY_PROMPT_PREFIX + " into groups"))
    ad = ag.AntigravityAdapter(home)
    assert ad.load(next(iter(ad.discover()))) is None


def test_malformed_transcript(tmp_path):
    home = make_home(tmp_path, records=["garbage", "[1]"])
    ad = ag.AntigravityAdapter(home)
    with pytest.raises(UnreadableSource):
        ad.load(next(iter(ad.discover())))
    home2 = make_home(tmp_path / "b", records=default_records() + ["garbage"])
    conv, _ = ag.AntigravityAdapter(home2).load(next(iter(ag.AntigravityAdapter(home2).discover())))
    assert conv.fidelity == "partial" and any("unparseable" in w for w in conv.warnings)


def test_available_and_resume(tmp_path):
    assert ag.AntigravityAdapter(tmp_path / "none").available() is False
    assert ag.resume_command(f"antigravity:{CID}", None) == f"agy --conversation {CID}"


def test_change_key_includes_summary_row_so_late_titles_reindex(tmp_path):
    home = make_home(tmp_path, with_summary=False)
    ad = ag.AntigravityAdapter(home)
    key1 = next(iter(ad.discover())).change_key
    ag._summaries_cache.clear()
    conn = sqlite3.connect(str(home / "conversation_summaries.db"))
    conn.execute("CREATE TABLE conversation_summaries(conversation_id text, title text NOT NULL DEFAULT '', "
                 "workspace_uris text NOT NULL, parent_conversation_id text NOT NULL DEFAULT '', PRIMARY KEY(conversation_id))")
    conn.execute("INSERT INTO conversation_summaries VALUES(?,?,?,?)", (CID, "Late title", "[]", "")); conn.commit(); conn.close()
    key2 = next(iter(ad.discover())).change_key
    assert key1 != key2 and key2.endswith("|Late title||")


def test_cwd_prefers_explicit_cwd_over_early_file_paths(tmp_path):
    recs = [
        _rec(0, "USER_INPUT", "USER_EXPLICIT", content="<USER_REQUEST>hi</USER_REQUEST>"),
        _rec(1, "PLANNER_RESPONSE", "MODEL", tool_calls=[{"name": "view_file", "args": {"AbsolutePath": "/Users/me/.gemini/antigravity-cli/skills/x.md"}}]),
        _rec(2, "GENERIC", "MODEL", content="..."),
        _rec(3, "PLANNER_RESPONSE", "MODEL", tool_calls=[{"name": "view_file", "args": {"AbsolutePath": "/Users/me/proj/src/deep/file.py"}}]),
        _rec(4, "GENERIC", "MODEL", content="..."),
        _rec(5, "PLANNER_RESPONSE", "MODEL", tool_calls=[{"name": "run_command", "args": {"CommandLine": "ls", "Cwd": "/Users/me/proj"}}]),
        _rec(6, "GENERIC", "MODEL", content="..."),
    ]
    home = make_home(tmp_path, records=recs, with_summary=False)
    ad = ag.AntigravityAdapter(home)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert conv.cwd == "/Users/me/proj"


def test_load_returns_none_when_transcript_vanished(tmp_path):
    home = make_home(tmp_path)
    ad = ag.AntigravityAdapter(home)
    ref = next(iter(ad.discover()))
    (home / "brain" / CID / ".system_generated" / "logs" / "transcript_full.jsonl").unlink()
    assert ad.load(ref) is None


def test_workspace_file_uris_are_decoded():
    assert ag._workspace_path("file:///Users/me/My%20Project") == "/Users/me/My Project"
    assert ag._workspace_path("/plain/path") == "/plain/path"
    assert ag._workspace_path("vscode-remote://ssh/x") is None and ag._workspace_path("file://host/x") is None


def test_literal_closing_tag_typed_by_the_user_survives():
    assert ag._unwrap("<USER_REQUEST>\nwhat does </USER_REQUEST> mean?\n</USER_REQUEST>") == "what does </USER_REQUEST> mean?"
    assert ag._unwrap("<USER_REQUEST>a</USER_REQUEST> trailing") == "a trailing"
    assert ag._unwrap(None) == "" and ag._unwrap(["not", "text"]) == ""


def test_unknown_layout_and_bad_nested_types(tmp_path):
    home = make_home(tmp_path, records=['{"foo": 1}', '{}'])
    ad = ag.AntigravityAdapter(home)
    with pytest.raises(UnreadableSource):
        ad.load(next(iter(ad.discover())))
    recs = [_rec(0, "USER_INPUT", "USER_EXPLICIT", content="<USER_REQUEST>hi</USER_REQUEST>"),
            _rec(1, "PLANNER_RESPONSE", "MODEL", thinking={"nested": "object"}, tool_calls=["not-a-dict", {"name": "run_command", "args": {"Cwd": "/p"}}]),
            _rec(2, "GENERIC", "MODEL", content={"unexpected": "object"}),
            _rec(3, "PLANNER_RESPONSE", "MODEL", content="done")]
    home2 = make_home(tmp_path / "b", records=recs, with_summary=False)
    conv, _ = ag.AntigravityAdapter(home2).load(next(iter(ag.AntigravityAdapter(home2).discover())))
    kinds = [(m.role, m.kind) for m in conv.messages]
    assert kinds == [("user", KIND_TEXT), ("assistant", KIND_TOOL_CALL), ("assistant", KIND_TEXT)]   # bad pieces skipped, rest kept
    assert conv.fidelity == "partial" and conv.cwd == "/p"


def test_change_key_carries_the_parser_version(tmp_path):
    home = make_home(tmp_path)
    ad = ag.AntigravityAdapter(home)
    ref = next(iter(ad.discover()))
    conv, revision = ad.load(ref)
    assert ref.change_key.startswith(f"p{ag.PARSER_VERSION}:") and revision == ref.change_key


def test_summaries_notice_rows_committed_to_the_wal(tmp_path):
    home = make_home(tmp_path)
    writer = sqlite3.connect(str(home / "conversation_summaries.db"))
    writer.execute("PRAGMA journal_mode=WAL")
    assert ag._summaries(home)[CID]["title"] == "EC2 restart automation"
    writer.execute("UPDATE conversation_summaries SET title='Renamed' WHERE conversation_id=?", (CID,))
    writer.commit()                      # in the WAL: the main file's mtime/size are unchanged
    assert ag._summaries(home)[CID]["title"] == "Renamed"
    writer.close()


def test_non_string_step_type_is_malformed_not_fatal(tmp_path):
    recs = ['{"type": [], "step_index": 0}', '{"type": {}, "step_index": 1}'] + default_records()
    home = make_home(tmp_path, records=recs)
    ad = ag.AntigravityAdapter(home)
    conv, _ = ad.load(next(iter(ad.discover())))
    assert conv.fidelity == "partial" and any(m.role == "user" for m in conv.messages)


def test_odd_workspace_uris_shapes_are_ignored(tmp_path):
    home = make_home(tmp_path)
    conn = sqlite3.connect(str(home / "conversation_summaries.db"))
    conn.execute("UPDATE conversation_summaries SET workspace_uris=?", ('{"bad": 1}',))
    conn.commit(); conn.close()
    assert [r.native_id for r in ag.AntigravityAdapter(home).discover()] == [CID]
    assert ag._summaries(home)[CID]["workspace"] is None
