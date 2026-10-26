"""v1 (single-source, session_id keyed) -> v2 migration."""
import sqlite3

import sqlite_vec

from convo_index import db as dbm
from convo_index.config import EMBED_DIM
from convo_index.model import content_hash, KIND_TEXT

_V1_SCHEMA = """
CREATE TABLE conversations(
    session_id TEXT PRIMARY KEY, source_path TEXT, archive_path TEXT,
    title TEXT, first_prompt TEXT, category TEXT, category_source TEXT, tickets TEXT,
    cwd TEXT, git_branch TEXT, start_ts TEXT, end_ts TEXT, message_count INTEGER,
    file_mtime REAL, project_root TEXT);
CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, seq INTEGER,
    role TEXT, ts TEXT, text TEXT);
CREATE INDEX idx_messages_session ON messages(session_id);
CREATE VIRTUAL TABLE messages_fts USING fts5(text, content='messages', content_rowid='id');
CREATE TABLE conversation_state(session_id TEXT PRIMARY KEY, hidden INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE conversation_summary(session_id TEXT PRIMARY KEY, problem TEXT, solution TEXT,
    is_resolved TEXT, learning TEXT, claude_failed TEXT, summarized_mtime REAL);
"""


def _make_v1(path):
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    conn.executescript(_V1_SCHEMA)
    conn.execute(f"CREATE VIRTUAL TABLE vec_messages USING vec0(embedding float[{EMBED_DIM}])")
    conn.execute("INSERT INTO conversations VALUES('abc','/p/abc.jsonl','/arch/abc.jsonl','T','fp',"
                 "'globex','path','[\"SAAS-1\"]','/x','main','2026-06-16T00:00:00Z','2026-06-16T01:00:00Z',"
                 "2,1756300000.1234567,'/x')")
    for seq, role, text in ((0, "user", "hello kube"), (1, "assistant", "sure")):
        cur = conn.execute("INSERT INTO messages(session_id, seq, role, ts, text) VALUES('abc',?,?,?,?)",
                           (seq, role, "2026-06-16T00:00:00Z", text))
        conn.execute("INSERT INTO messages_fts(rowid, text) VALUES(?,?)", (cur.lastrowid, text))
        conn.execute("INSERT INTO vec_messages(rowid, embedding) VALUES(?,?)",
                     (cur.lastrowid, sqlite_vec.serialize_float32([0.5] * EMBED_DIM)))
    conn.execute("INSERT INTO conversation_state VALUES('abc', 1, 0)")
    conn.execute("INSERT INTO conversation_state VALUES('gone', 0, 1)")
    conn.execute("INSERT INTO conversation_summary VALUES('abc','P','S','Yes','L','No',1756300000.1234567)")
    conn.commit(); conn.close()


def test_migrates_v1_in_place_with_backup(tmp_path):
    p = tmp_path / "convos.db"
    _make_v1(p)
    conn = dbm.connect(p)
    dbm.init_schema(conn, p)
    backups = list(tmp_path.glob("convos.db.bak-*"))
    assert len(backups) == 1
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == list(range(1, dbm.SCHEMA_VERSION + 1))

    row = conn.execute("SELECT id, source, native_id, source_ref, source_revision, fidelity, project_root "
                       "FROM conversations").fetchone()
    assert row == ("claude_code:abc", "claude_code", "abc", "/p/abc.jsonl", str(1756300000.1234567), "full", "/x")
    assert "file_mtime" not in {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}

    msgs = conn.execute("SELECT id, conversation_id, kind, content_hash, native_id FROM messages ORDER BY seq").fetchall()
    assert [m[1] for m in msgs] == ["claude_code:abc"] * 2
    assert msgs[0][2] == KIND_TEXT and msgs[0][3] == content_hash("user", KIND_TEXT, "hello kube")
    assert msgs[0][4] is None
    # vectors and FTS stayed attached to the same row ids
    assert conn.execute("SELECT COUNT(*) FROM messages m JOIN vec_messages v ON v.rowid=m.id").fetchone()[0] == 2
    assert conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'kube'").fetchall() == [(msgs[0][0],)]

    assert dbm.get_hidden_ids(conn) == {"claude_code:abc"}
    assert dbm.get_deleted_ids(conn) == {"claude_code:gone"}
    assert dbm.get_summary(conn, "claude_code:abc")["problem"] == "P"
    assert dbm.get_summarized_revision(conn, "claude_code:abc") == str(1756300000.1234567)
    assert dbm.resolve_id(conn, "abc") == "claude_code:abc"

    # idempotent: a second init is a no-op
    dbm.init_schema(conn, p)
    assert len(list(tmp_path.glob("convos.db.bak-*"))) == 1
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


def test_migration_reingests_once_but_keeps_summary_for_unchanged_files(tmp_path):
    """Legacy rows keep an mtime-only revision (never equal to the adapter's mtime:size key)
    so they are re-ingested once to backfill native ids/usage; the summary revision is
    upgraded for unchanged files so no LLM call is repeated."""
    import os
    src = tmp_path / "abc.jsonl"; src.write_text("{}")
    mtime = 1756300000.1234567
    os.utime(src, (mtime, mtime))
    p = tmp_path / "convos.db"
    _make_v1(p)
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE conversations SET source_path=?", (str(src),)); conn.commit(); conn.close()
    conn = dbm.connect(p)
    dbm.init_schema(conn, p)
    from convo_index.sources.claude_code import revision_of
    assert dbm.get_revision(conn, "claude_code:abc") == str(mtime)          # != revision_of(src)
    assert dbm.get_summarized_revision(conn, "claude_code:abc") == revision_of(src)


def test_backup_is_a_complete_copy_including_uncheckpointed_wal(tmp_path):
    p = tmp_path / "convos.db"
    _make_v1(p)
    # Write more rows through a WAL connection and keep it open (no checkpoint) while migrating.
    live = sqlite3.connect(str(p)); live.execute("PRAGMA journal_mode=WAL")
    live.execute("INSERT INTO conversation_state VALUES('wal-only', 1, 0)"); live.commit()
    conn = dbm.connect(p)
    dbm.init_schema(conn, p)
    backup = next(tmp_path.glob("convos.db.bak-*"))
    b = sqlite3.connect(str(backup))
    assert b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert "session_id" in {r[1] for r in b.execute("PRAGMA table_info(conversations)")}  # still v1
    assert b.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    assert b.execute("SELECT hidden FROM conversation_state WHERE session_id='wal-only'").fetchone() == (1,)
    live.close()


def test_failed_migration_rolls_back_and_leaves_v1_intact(tmp_path, monkeypatch):
    import pytest
    p = tmp_path / "convos.db"
    _make_v1(p)
    import convo_index.model as model
    calls = {"n": 0}
    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected")
        return "h"
    monkeypatch.setattr(model, "content_hash", boom)
    conn = dbm.connect(p)
    with pytest.raises(RuntimeError, match="injected"):
        dbm.init_schema(conn, p)
    conn.close()
    conn = sqlite3.connect(str(p))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    assert "session_id" in cols and "source" not in cols
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='abc'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM conversation_summary").fetchone()[0] == 1
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone() is None
    assert len(list(tmp_path.glob("convos.db.bak-*"))) == 1  # backup was taken before the attempt


def test_concurrent_init_migrates_exactly_once(tmp_path):
    """Two processes start on a v1 db at once: exactly one backup, one migration, no error.
    Real subprocesses so the flock actually contends."""
    import subprocess
    import sys
    import textwrap
    p = tmp_path / "convos.db"
    _make_v1(p)
    code = textwrap.dedent(f"""
        from convo_index import db as dbm
        c = dbm.connect({str(p)!r}); dbm.init_schema(c, {str(p)!r})
        print(c.execute("SELECT COUNT(*) FROM conversations WHERE id LIKE 'claude_code:%'").fetchone()[0])
    """)
    procs = [subprocess.Popen([sys.executable, "-c", code],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    outs = [pr.communicate(timeout=60) for pr in procs]
    assert all(pr.returncode == 0 for pr in procs), [o[1] for o in outs]
    assert all(o[0].strip() == "1" for o in outs)
    assert len(list(tmp_path.glob("convos.db.bak-*"))) == 1


def test_migrated_messages_reconcile_without_reembedding(tmp_path):
    from convo_index.model import Conversation, Message
    p = tmp_path / "convos.db"
    _make_v1(p)
    conn = dbm.connect(p)
    dbm.init_schema(conn, p)
    conv = Conversation(source="claude_code", native_id="abc", title="T", first_prompt="hello kube",
                        source_ref="/p/abc.jsonl",
                        messages=[Message(seq=0, role="user", text="hello kube", native_id="u0"),
                                  Message(seq=1, role="assistant", text="sure", native_id="u1")])
    calls = []
    stats = dbm.upsert_conversation(conn, conv, "globex", "path", [], None, "r2",
                                    lambda texts: calls.append(texts) or [None] * len(texts))
    assert stats == {"inserted": 0, "updated": 0, "deleted": 0, "embedded": 0}
    assert calls == []
    assert conn.execute("SELECT native_id FROM messages ORDER BY seq").fetchall() == [("u0",), ("u1",)]


def test_fresh_db_is_current_without_backup(tmp_path):
    p = tmp_path / "new.db"
    conn = dbm.connect(p)
    dbm.init_schema(conn, p)
    assert list(tmp_path.glob("*.bak-*")) == []
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == list(range(2, dbm.SCHEMA_VERSION + 1))


def test_v2_to_v3_backs_up_and_marks_legacy_vectors(tmp_path):
    from convo_index.model import Conversation, Message
    p = tmp_path / "v2.db"
    conn = dbm.connect(p); dbm.init_schema(conn, p)
    c = Conversation(source="claude_code", native_id="a", title="t", first_prompt="p", source_ref="/a",
                     messages=[Message(seq=0, role="user", text="x")])
    dbm.upsert_conversation(conn, c, "globex", "path", [], None, "r1", lambda t: [[0.1] * EMBED_DIM for _ in t])
    conn.execute("DELETE FROM schema_migrations WHERE version=3")
    conn.execute("DROP TABLE derived_jobs"); conn.execute("DROP TABLE index_meta"); conn.commit(); conn.close()
    assert list(tmp_path.glob("v2.db.bak-*")) == []
    conn = dbm.connect(p); dbm.init_schema(conn, p)
    assert len(list(tmp_path.glob("v2.db.bak-*"))) == 1                       # v2 -> v3 is backed up too
    assert dbm.get_meta(conn, "embedding_fingerprint").startswith("legacy|")    # old vectors are not relabelled


def test_v2_to_v3_seeds_jobs_only_for_current_summaries(tmp_path):
    from convo_index.model import Conversation, Message
    p = tmp_path / "v2.db"
    conn = dbm.connect(p); dbm.init_schema(conn, p)
    for native, rev, srev in (("a", "r1", "r1"), ("b", "r2", "r1")):     # b's summary is stale
        c = Conversation(source="claude_code", native_id=native, title="t", first_prompt="p", source_ref="/a",
                         messages=[Message(seq=0, role="user", text="x")])
        dbm.upsert_conversation(conn, c, "globex", "path", [], None, rev, lambda t: [None] * len(t))
        dbm.set_summary(conn, c.id, {"problem": "P"}, srev)
    # pretend this is a v2 database: drop the v3 marker and table, then re-init
    conn.execute("DELETE FROM schema_migrations WHERE version=3"); conn.execute("DROP TABLE derived_jobs"); conn.commit()
    dbm.init_schema(conn, p)
    rows = conn.execute("SELECT conversation_id, status FROM derived_jobs ORDER BY 1").fetchall()
    assert rows == [("claude_code:a", "ok")]
