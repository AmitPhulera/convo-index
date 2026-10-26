from convo_index import db as dbm
from convo_index.model import Conversation, Message, UsageRecord, KIND_TOOL_CALL
from convo_index.config import EMBED_DIM


def _conv(native="s1", texts=("alpha bravo", "charlie delta"), native_ids=None, source="claude_code"):
    msgs = []
    for i, t in enumerate(texts):
        msgs.append(Message(seq=i, role="user" if i % 2 == 0 else "assistant", text=t,
                            ts=f"2026-06-16T00:{i:02d}:00Z",
                            native_id=native_ids[i] if native_ids else None))
    return Conversation(source=source, native_id=native, title="T " + native, first_prompt=texts[0],
                        messages=msgs, source_ref="/x/s1.jsonl", cwd="/x", git_branch="main",
                        start_ts="2026-06-16T00:00:00Z", end_ts="2026-06-16T01:00:00Z")


def _embed_zero(texts):
    return [[0.0] * EMBED_DIM for _ in texts]


def _embed_none(texts):
    return [None for _ in texts]


def _open(tmp_path):
    conn = dbm.connect(tmp_path / "t.db")
    dbm.init_schema(conn, tmp_path / "t.db")
    return conn


def test_init_and_upsert(tmp_path):
    conn = _open(tmp_path)
    stats = dbm.upsert_conversation(conn, _conv(), "globex", "path", ["SAAS-1"], "/a.jsonl", "123.0", _embed_zero)
    assert stats == {"inserted": 2, "updated": 0, "deleted": 0, "embedded": 2}
    rows = conn.execute("SELECT id, source, native_id, category, tickets, message_count, source_revision "
                        "FROM conversations").fetchall()
    assert rows == [("claude_code:s1", "claude_code", "s1", "globex", '["SAAS-1"]', 2, "123.0")]
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0] == 2
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == list(range(2, dbm.SCHEMA_VERSION + 1))


def test_get_revision_missing_and_present(tmp_path):
    conn = _open(tmp_path)
    assert dbm.get_revision(conn, "claude_code:nope") is None
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], "/a", "999", _embed_none)
    assert dbm.get_revision(conn, "claude_code:s1") == "999"


def test_reindex_keeps_message_ids_and_vectors(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], "/a", "1", _embed_zero)
    ids_before = [r[0] for r in conn.execute("SELECT id FROM messages ORDER BY seq")]
    calls = []
    def spy(texts):
        calls.append(list(texts)); return _embed_zero(texts)
    stats = dbm.upsert_conversation(conn, _conv(), "personal", "haiku", [], "/a", "2", spy)
    assert stats == {"inserted": 0, "updated": 0, "deleted": 0, "embedded": 0}
    assert calls == []  # unchanged text with a vector is never re-embedded
    ids_after = [r[0] for r in conn.execute("SELECT id FROM messages ORDER BY seq")]
    assert ids_after == ids_before
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    assert conn.execute("SELECT category FROM conversations").fetchone()[0] == "personal"


def test_reindex_appends_new_messages_only(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], "/a", "1", _embed_zero)
    calls = []
    def spy(texts):
        calls.append(list(texts)); return _embed_zero(texts)
    longer = _conv(texts=("alpha bravo", "charlie delta", "echo foxtrot"))
    stats = dbm.upsert_conversation(conn, longer, "globex", "path", [], "/a", "2", spy)
    assert stats["inserted"] == 1 and stats["deleted"] == 0 and stats["embedded"] == 1
    assert calls == [["echo foxtrot"]]
    assert conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0] == 3


def test_reindex_matches_by_native_id_when_text_changes(tmp_path):
    conn = _open(tmp_path)
    c1 = _conv(texts=("hello", "world"), native_ids=["u1", "u2"])
    dbm.upsert_conversation(conn, c1, "globex", "path", [], "/a", "1", _embed_zero)
    ids = {r[1]: r[0] for r in conn.execute("SELECT id, native_id FROM messages")}
    c2 = _conv(texts=("hello", "world, edited"), native_ids=["u1", "u2"])
    stats = dbm.upsert_conversation(conn, c2, "globex", "path", [], "/a", "2", _embed_zero)
    assert stats == {"inserted": 0, "updated": 1, "deleted": 0, "embedded": 1}
    row = conn.execute("SELECT id, text FROM messages WHERE native_id='u2'").fetchone()
    assert row == (ids["u2"], "world, edited")
    hits = conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'edited'").fetchall()
    assert hits == [(ids["u2"],)]
    assert conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'world' AND rowid=?",
                        (ids["u2"],)).fetchall() == [(ids["u2"],)]


def test_reindex_deletes_removed_messages(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(texts=("a1", "b2", "c3")), "globex", "path", [], "/a", "1", _embed_zero)
    stats = dbm.upsert_conversation(conn, _conv(texts=("a1",)), "globex", "path", [], "/a", "2", _embed_zero)
    assert stats["deleted"] == 2
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'c3'").fetchone()[0] == 0


def test_missing_vectors_are_backfilled_next_run(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], "/a", "1", _embed_none)  # e.g. Ollama down
    assert conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0] == 0
    stats = dbm.upsert_conversation(conn, _conv(), "globex", "path", [], "/a", "1", _embed_zero)
    assert stats["embedded"] == 2
    assert conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0] == 2


def test_only_text_messages_are_embedded(tmp_path):
    conn = _open(tmp_path)
    conv = _conv(texts=("ask",))
    conv.messages.append(Message(seq=1, role="assistant", text="[bash] ls", kind=KIND_TOOL_CALL))
    calls = []
    def spy(texts):
        calls.append(list(texts)); return _embed_zero(texts)
    dbm.upsert_conversation(conn, conv, "globex", "path", [], "/a", "1", spy)
    assert calls == [["ask"]]
    # but tool text is still keyword-searchable
    assert conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'bash'").fetchone()[0] == 1


def test_usage_records_link_to_message_rows(tmp_path):
    conn = _open(tmp_path)
    conv = _conv()
    conv.usage = [UsageRecord(native_request_id="req1", message_seq=1, provider="anthropic",
                              model="claude-haiku-4-5", input_uncached=10, cache_read=100,
                              cache_write=5, output=20)]
    dbm.upsert_conversation(conn, conv, "globex", "path", [], "/a", "1", _embed_none)
    mid = conn.execute("SELECT id FROM messages WHERE seq=1").fetchone()[0]
    row = conn.execute("SELECT message_id, model, input_uncached, cache_read, output, reported_cost_usd "
                       "FROM usage_records").fetchone()
    assert row == (mid, "claude-haiku-4-5", 10, 100, 20, None)
    dbm.upsert_conversation(conn, conv, "globex", "path", [], "/a", "2", _embed_none)
    assert conn.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0] == 1  # replaced, not duplicated


def test_fts_search_finds_message(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], "/a", "1", _embed_none)
    hits = conn.execute(
        "SELECT m.conversation_id FROM messages_fts f JOIN messages m ON m.id=f.rowid WHERE messages_fts MATCH 'charlie'"
    ).fetchall()
    assert hits == [("claude_code:s1",)]


def test_hidden_flag_roundtrip(tmp_path):
    conn = _open(tmp_path)
    assert dbm.get_hidden_ids(conn) == set()
    dbm.set_hidden(conn, "claude_code:s1", True)
    assert dbm.get_hidden_ids(conn) == {"claude_code:s1"}
    dbm.set_hidden(conn, "claude_code:s1", False)
    assert dbm.get_hidden_ids(conn) == set()


def test_hidden_survives_reindex(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], "/a", "1", _embed_none)
    dbm.set_hidden(conn, "claude_code:s1", True)
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], "/a", "2", _embed_none)
    assert dbm.get_hidden_ids(conn) == {"claude_code:s1"}


def test_delete_conversation_blocklists_and_removes(tmp_path):
    conn = _open(tmp_path)
    conv = _conv()
    conv.usage = [UsageRecord(native_request_id="r", message_seq=1, output=1)]
    dbm.upsert_conversation(conn, conv, "globex", "path", [], "/arch/s1.jsonl", "1", _embed_zero)
    archive = dbm.delete_conversation(conn, "claude_code:s1")
    assert archive == "/arch/s1.jsonl"
    for table in ("conversations", "messages", "vec_messages", "usage_records"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert dbm.get_deleted_ids(conn) == {"claude_code:s1"}


def test_summary_roundtrip_and_survives_reindex(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], "/a", "1", _embed_none)
    s = {"problem": "P", "solution": "S", "is_resolved": "Yes", "learning": "L", "claude_failed": "No"}
    dbm.set_summary(conn, "claude_code:s1", s, "1")
    assert dbm.get_summary(conn, "claude_code:s1") == s
    assert dbm.get_summarized_revision(conn, "claude_code:s1") == "1"
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], "/a", "2", _embed_none)
    assert dbm.get_summary(conn, "claude_code:s1") == s


def test_delete_clears_summary(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], "/a", "1", _embed_none)
    dbm.set_summary(conn, "claude_code:s1", {"problem": "P"}, "1")
    dbm.delete_conversation(conn, "claude_code:s1")
    assert dbm.get_summary(conn, "claude_code:s1") is None


def test_sources_and_namespacing(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(native="same"), "globex", "path", [], None, "1", _embed_none)
    dbm.upsert_conversation(conn, _conv(native="same", source="opencode"), "globex", "path", [], None, "1", _embed_none)
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2
    assert set(dbm.list_sources(conn)) == {"claude_code", "opencode"}
    assert dbm.resolve_id(conn, "opencode:same") == "opencode:same"
    assert dbm.resolve_id(conn, "unknown") == "unknown"


def test_resolve_bare_native_id(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(native="ses_9", source="opencode"), "globex", "path", [], None, "1", _embed_none)
    assert dbm.resolve_id(conn, "ses_9") == "opencode:ses_9"


def test_resolve_bare_native_id_refuses_ambiguity(tmp_path):
    import pytest
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(native="same", source="opencode"), "globex", "path", [], None, "1", _embed_none)
    dbm.upsert_conversation(conn, _conv(native="same", source="claude_code"), "globex", "path", [], None, "1", _embed_none)
    with pytest.raises(dbm.AmbiguousId) as ei:
        dbm.resolve_id(conn, "same")
    assert set(ei.value.args[0]) == {"opencode:same", "claude_code:same"}
    assert dbm.resolve_id(conn, "opencode:same") == "opencode:same"   # namespaced ids always work


def test_connect_creates_missing_data_dir(tmp_path):
    p = tmp_path / "fresh" / "nested" / "convos.db"
    conn = dbm.connect(p)
    dbm.init_schema(conn, p)
    assert p.exists()


def test_init_on_current_db_does_not_wait_for_index_lock_or_writers(tmp_path):
    from convo_index.lock import index_lock
    p = tmp_path / "t.db"
    conn = dbm.connect(p); dbm.init_schema(conn, p)
    conn.execute("BEGIN IMMEDIATE")     # an indexer holds a real SQLite write transaction...
    conn.execute("INSERT INTO conversation_state(conversation_id) VALUES('x')")
    with index_lock(p):                 # ...and the advisory index lock
        other = dbm.connect(p)
        other.execute("PRAGMA busy_timeout=100")
        dbm.init_schema(other, p)       # must return immediately without writing
        assert other.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == dbm.SCHEMA_VERSION
    conn.execute("ROLLBACK")


def test_upsert_refuses_conversation_deleted_meanwhile(tmp_path):
    conn = _open(tmp_path)
    conv = _conv()
    def embed_then_delete(texts):
        dbm.delete_conversation(conn, conv.id)  # user hits Delete while we embed
        return _embed_zero(texts)
    assert dbm.upsert_conversation(conn, conv, "globex", "path", [], "/a", "1", embed_then_delete) is None
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert dbm.get_deleted_ids(conn) == {conv.id}


def test_positional_fallback_cannot_steal_a_native_identity(tmp_path):
    conn = _open(tmp_path)
    # A: "continue" (seq 0) with native id a
    c1 = _conv(texts=("continue",), native_ids=["a"])
    dbm.upsert_conversation(conn, c1, "globex", "path", [], None, "1", _embed_none)
    row_a = conn.execute("SELECT id FROM messages WHERE native_id='a'").fetchone()[0]
    # B: "continue" inserted before A -> B is now seq 0 with the same hash
    c2 = _conv(texts=("continue", "continue"), native_ids=["b", "a"])
    stats = dbm.upsert_conversation(conn, c2, "globex", "path", [], None, "2", _embed_none)
    assert stats["inserted"] == 1 and stats["deleted"] == 0
    assert conn.execute("SELECT id FROM messages WHERE native_id='a'").fetchone()[0] == row_a


def test_positional_fallback_only_claims_rows_without_native_id(tmp_path):
    conn = _open(tmp_path)
    c1 = _conv(texts=("x", "y"))                       # legacy-like rows: no native ids
    dbm.upsert_conversation(conn, c1, "globex", "path", [], None, "1", _embed_none)
    ids = [r[0] for r in conn.execute("SELECT id FROM messages ORDER BY seq")]
    c2 = _conv(texts=("x", "y"), native_ids=["u1", "u2"])   # same content, now with ids
    stats = dbm.upsert_conversation(conn, c2, "globex", "path", [], None, "2", _embed_none)
    assert stats == {"inserted": 0, "updated": 0, "deleted": 0, "embedded": 0}
    assert [r[0] for r in conn.execute("SELECT id FROM messages ORDER BY seq")] == ids
    assert [r[0] for r in conn.execute("SELECT native_id FROM messages ORDER BY seq")] == ["u1", "u2"]


def test_set_summary_if_current(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], None, "r1", _embed_none)
    assert dbm.set_summary_if_current(conn, "claude_code:s1", {"problem": "old"}, "r0") is False  # stale
    assert dbm.set_summary_if_current(conn, "claude_code:s1", {"problem": "new"}, "r1") is True
    assert dbm.get_summary(conn, "claude_code:s1")["problem"] == "new"


def test_set_summary_if_current_refuses_a_deleted_conversation(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], None, "r1", _embed_none)
    dbm.delete_conversation(conn, "claude_code:s1")
    assert dbm.set_summary_if_current(conn, "claude_code:s1", {"problem": "zombie"}, "r1") is False
    assert dbm.get_summary(conn, "claude_code:s1") is None


def test_remove_conversation_drops_content_without_blocklisting(tmp_path):
    conn = _open(tmp_path)
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], None, "1", _embed_zero)
    dbm.set_summary(conn, "claude_code:s1", {"problem": "p"}, "1")
    assert dbm.remove_conversation(conn, "claude_code:s1") is True
    for table in ("conversations", "messages", "vec_messages", "conversation_summary"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert dbm.get_deleted_ids(conn) == set()
    assert dbm.remove_conversation(conn, "claude_code:s1") is False


def test_v6_conversation_vectors_take_the_existing_message_vector_dim(tmp_path):
    from convo_index import db as dbm
    db = tmp_path / "t.db"
    conn = dbm.connect(db); dbm.init_schema(conn, db, dim=384); conn.close()
    conn = dbm.connect(db)
    conn.execute("DROP TABLE vec_conversations"); conn.execute("DELETE FROM schema_migrations WHERE version=6"); conn.commit()
    dbm.init_schema(conn, db)                     # an upgrade with the default 768 configured
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='vec_conversations'").fetchone()[0]
    assert "float[384]" in ddl and dbm.vector_dim(conn) == 384


def test_recreate_vectors_validates_first_and_keeps_tables_on_bad_dim(tmp_path):
    import pytest
    from convo_index import db as dbm
    db = tmp_path / "t.db"
    conn = dbm.connect(db); dbm.init_schema(conn, db, dim=384)
    dbm.set_meta(conn, "embedding_fingerprint", "old")
    for bad in (-1, 0, "x", None, 70000):
        with pytest.raises(ValueError):
            dbm.recreate_vectors(conn, bad, fingerprint="new")
    assert dbm.vector_dim(conn) == 384 and dbm.get_meta(conn, "embedding_fingerprint") == "old"
    dbm.recreate_vectors(conn, 768, fingerprint="new")
    assert dbm.vector_dim(conn) == 768 and dbm.get_meta(conn, "embedding_fingerprint") == "new"


def test_titles_are_capped_to_one_display_line(tmp_path):
    from convo_index import db as dbm
    from convo_index.model import Conversation, Message, display_title, TITLE_LIMIT
    long = "You are reviewing SLICE 7 " * 20
    assert display_title(long).endswith("…") and len(display_title(long)) == TITLE_LIMIT
    assert display_title("multi\nline  title") == "multi line title" and display_title("", "fallback prompt") == "fallback prompt"
    db = tmp_path / "t.db"
    conn = dbm.connect(db); dbm.init_schema(conn, db)
    conv = Conversation(source="codex", native_id="t", title=long, first_prompt=long, source_ref="/a", cwd="/x",
                        messages=[Message(seq=0, role="user", text=long)])
    dbm.upsert_conversation(conn, conv, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    stored = conn.execute("SELECT title FROM conversations WHERE id=?", (conv.id,)).fetchone()[0]
    assert len(stored) == TITLE_LIMIT and stored.endswith("…")
    # rows written before the cap are fixed by the v7 step
    conn.execute("UPDATE conversations SET title=? WHERE id=?", (long, conv.id))
    conn.execute("DELETE FROM schema_migrations WHERE version=7"); conn.commit()
    dbm.init_schema(conn, db)
    assert len(conn.execute("SELECT title FROM conversations WHERE id=?", (conv.id,)).fetchone()[0]) == TITLE_LIMIT


def test_parser_version_bump_with_changed_text_does_not_carry_the_summary(tmp_path):
    """Same file, new parser: the summary moves to the new revision only when every text
    message is as it was. Messages keep their native ids so the rows are matched, not
    deleted and re-inserted; a kind change or a reorder of matched rows must still count."""
    from convo_index.model import Conversation, Message, KIND_TOOL_CALL
    db = tmp_path / "t.db"
    conn = dbm.connect(db); dbm.init_schema(conn, db)
    conv = Conversation(source="claude_code", native_id="s", title="T", first_prompt="p", source_ref="/a", cwd="/x",
                        messages=[Message(seq=0, role="user", text="The task succeeded", native_id="m0")])
    dbm.upsert_conversation(conn, conv, "unknown", None, [], None, "p1:100:200", _embed_none)
    dbm.set_summary(conn, conv.id, {"problem": "P", "solution": "S"}, "p1:100:200")
    with_tool = Conversation(source="claude_code", native_id="s", title="T", first_prompt="p", source_ref="/a", cwd="/x",
                             messages=[Message(seq=0, role="user", text="The task succeeded", native_id="m0"),
                                       Message(seq=1, role="assistant", kind=KIND_TOOL_CALL, text="[Read] x", native_id="m1")])
    dbm.upsert_conversation(conn, with_tool, "unknown", None, [], None, "p2:100:200", _embed_none)
    assert dbm.get_summarized_revision(conn, conv.id) == "p2:100:200"     # only tool messages appeared: carried
    changed = Conversation(source="claude_code", native_id="s", title="T", first_prompt="p", source_ref="/a", cwd="/x",
                           messages=[Message(seq=0, role="user", text="The task failed", native_id="m0")])
    dbm.upsert_conversation(conn, changed, "unknown", None, [], None, "p3:100:200", _embed_none)
    assert dbm.get_summarized_revision(conn, conv.id) == "p2:100:200"     # the text moved: summary stays stale
    dbm.set_summary(conn, conv.id, {"problem": "P"}, "p3:100:200")
    reclassified = Conversation(source="claude_code", native_id="s", title="T", first_prompt="p", source_ref="/a", cwd="/x",
                                messages=[Message(seq=0, role="user", kind=KIND_TOOL_CALL, text="The task failed", native_id="m0")])
    dbm.upsert_conversation(conn, reclassified, "unknown", None, [], None, "p4:100:200", _embed_none)
    assert dbm.get_summarized_revision(conn, conv.id) == "p3:100:200"     # a text message became a tool call: stale
    two = [Message(seq=0, role="user", text="a", native_id="a"), Message(seq=1, role="assistant", text="b", native_id="b")]
    dbm.upsert_conversation(conn, Conversation(source="claude_code", native_id="s2", title="T", first_prompt="p", source_ref="/b",
                                               cwd="/x", messages=two), "unknown", None, [], None, "p1:1:1", _embed_none)
    dbm.set_summary(conn, "claude_code:s2", {"problem": "P"}, "p1:1:1")
    swapped = [Message(seq=0, role="assistant", text="b", native_id="b"), Message(seq=1, role="user", text="a", native_id="a")]
    dbm.upsert_conversation(conn, Conversation(source="claude_code", native_id="s2", title="T", first_prompt="p", source_ref="/b",
                                               cwd="/x", messages=swapped), "unknown", None, [], None, "p2:1:1", _embed_none)
    assert dbm.get_summarized_revision(conn, "claude_code:s2") == "p1:1:1"  # same texts in another order: stale
