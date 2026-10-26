"""Export, MCP server, scheduler plan and the CLI round trips."""


from convo_index import db as dbm
from convo_index import export as X
from convo_index import settings as S
from convo_index.model import Conversation, Message, UsageRecord, KIND_TOOL_CALL, KIND_TOOL_RESULT
from convo_index.sources.base import resume_command
from tests.conftest import make_settings


def _seed(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    st["paths"]["db"] = str(tmp_path / "convos.db")
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    monkeypatch.setenv("CONVO_INDEX_DB", str(tmp_path / "convos.db"))
    db = tmp_path / "convos.db"
    conn = dbm.connect(db); dbm.init_schema(conn, db)
    from convo_index import tags as T, references as REF
    T.sync_definitions(conn, st)
    for i, (title, txt) in enumerate([("Kube deploy", "kubernetes deployment failing on SAAS-42"),
                                      ("Astrology chart", "draw an astrology chart")]):
        c = Conversation(source="claude_code", native_id=f"s{i}", title=title, first_prompt=txt, source_ref="/a", cwd="/Users/me/proj",
                         git_branch="main", start_ts=f"2026-06-1{i}T00:00:00Z", end_ts=f"2026-06-1{i}T01:00:00Z",
                         messages=[Message(seq=0, role="user", text=txt, ts="2026-06-10T00:00:00Z"),
                                   Message(seq=1, role="assistant", text="[bash] kubectl get pods", kind=KIND_TOOL_CALL),
                                   Message(seq=2, role="tool", text="pod-1 CrashLoopBackOff", kind=KIND_TOOL_RESULT),
                                   Message(seq=3, role="assistant", text="Restart the deployment.")],
                         usage=[UsageRecord(f"r{i}", 3, "anthropic", "claude-haiku-4-5", input_uncached=1_000_000, output=100_000)])
        dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
        REF.store(conn, c, REF.extract(c, st))
    dbm.set_summary(conn, "claude_code:s0", {"problem": "pods crash", "solution": "restart", "learning": "check limits"}, "1")
    T.set_decision(conn, "claude_code:s0", "workspace", "globex", suggest=False)
    conn.commit(); conn.close()
    return st, db


def test_export_dict_and_markdown(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    conn = dbm.connect(db)
    d = X.as_dict(conn, "claude_code:s0", st)
    assert d["title"] == "Kube deploy" and d["summary"]["problem"] == "pods crash" and d["tags"] == {"workspace": ["globex"]}
    assert [r["key"] for r in d["references"]] == ["SAAS-42"] and d["messages_total"] == 4 and len(d["messages"]) == 4
    assert abs(d["usage"]["estimated_cost"] - 1.5) < 1e-9 and d["resume_cmd"] == resume_command("claude_code:s0", "/Users/me/proj")
    slim = X.as_dict(conn, "claude_code:s0", st, include_tools=False, max_messages=1, max_chars=10)
    assert len(slim["messages"]) == 1 and slim["messages"][0]["text"].startswith("kubernetes…") and "more chars" in slim["messages"][0]["text"]
    md = X.as_markdown(conn, "claude_code:s0", st)
    assert md.startswith("# Kube deploy") and "## Summary" in md and "**Problem.** pods crash" in md
    assert "- tags: workspace:globex" in md and "- references: ticket SAAS-42" in md and "$1.50 API-equivalent estimate" in md
    assert "### Assistant → tool" not in md and "Restart the deployment." in md          # tools excluded by default
    assert "```" in X.as_markdown(conn, "claude_code:s0", st, include_tools=True)
    assert X.as_dict(conn, "claude_code:nope", st) is None and X.as_markdown(conn, "claude_code:nope", st) is None


def test_markdown_fences_outlast_backticks_in_tool_output(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    conn = dbm.connect(db)
    conn.execute("UPDATE messages SET text=? WHERE conversation_id='claude_code:s0' AND seq=2", ("out\n```py\nx\n```\n````\n",))
    conn.commit()
    md = X.as_markdown(conn, "claude_code:s0", st, include_tools=True)
    block = md.split("### Tool result")[1]
    assert block.strip().startswith("`````") and block.count("`````") == 2
    assert X._fence("plain") == "```" and X._fence("a ``` b") == "````"


def test_coverage_counts_only_token_bearing_requests(tmp_path):
    from convo_index import cost as K
    p = K.load(make_settings(tmp_path))
    rows = [("anthropic", "mystery", 1, 1000, 0, 0, 100, 0, None, 0),        # unpriced, has tokens
            ("anthropic", "<synthetic>", 9, 0, 0, 0, 0, 0, None, 0)]          # token-less
    a = K.aggregate(rows, p)
    assert a["requests"] == 10 and a["token_requests"] == 1 and a["priced_requests"] == 0 and a["coverage"] == 0.0
    assert K.aggregate([("anthropic", "<synthetic>", 3, 0, 0, 0, 0, 0, None, 0)], p)["coverage"] is None
    assert p.rates("gpt-4o-2024-08-06")["input"] == 2.5 and p.rates("claude-3-5-sonnet-20241022")["input"] == 3.0


def test_markdown_export_escapes_heading_lines_and_single_lines_the_title(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    conn = dbm.connect(db)
    conn.execute("UPDATE conversations SET title=? WHERE id='claude_code:s0'", ("multi\nline title",))
    conn.execute("UPDATE messages SET text=? WHERE conversation_id='claude_code:s0' AND seq=3", ("### Assistant · spoofed\nreal text",))
    conn.commit()
    md = X.as_markdown(conn, "claude_code:s0", st)
    assert md.startswith("# multi line title\n") and "\\### Assistant · spoofed" in md and md.count("\n### Assistant") == 1


def test_markdown_export_keeps_code_blocks_as_they_are(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    conn = dbm.connect(db)
    conn.execute("UPDATE messages SET text=? WHERE conversation_id='claude_code:s0' AND seq=3",
                 ("look:\n```python\n# a comment\nprint(1)\n```\n# not a heading\n    # indented code\n"
                  "~~~~markdown\n~~~\n# still code\n~~~~",))
    conn.commit()
    md = X.as_markdown(conn, "claude_code:s0", st)
    assert "\n# a comment\n" in md and "\n\\# not a heading\n" in md and "\n    # indented code" in md
    assert "\n# still code\n" in md                                      # a shorter fence does not close a longer one
