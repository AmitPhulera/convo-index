"""v1 (single-source, session_id keyed) -> v2 migration."""
import sqlite3

import sqlite_vec

from convo_index import db as dbm
from convo_index.config import EMBED_DIM

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


def test_fresh_db_is_current_without_backup(tmp_path):
    p = tmp_path / "new.db"
    conn = dbm.connect(p)
    dbm.init_schema(conn, p)
    assert list(tmp_path.glob("*.bak-*")) == []
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == list(range(2, dbm.SCHEMA_VERSION + 1))
