"""SQLite storage: schema, numbered migrations, and native-id message reconciliation.

Conversation ids are namespaced by source (`claude_code:<uuid>`, `opencode:<id>`), message
row ids stay stable across reindexes so vectors, usage and references stay attached, and the
source revision that was actually read is recorded. Older databases are migrated in place
after a file backup; `SCHEMA_VERSION` is the current layout.
"""
from __future__ import annotations

import json
import re
import os
import sqlite3
import time
from pathlib import Path

import sqlite_vec

from convo_index.config import EMBED_DIM
from convo_index.model import TITLE_LIMIT, display_title, Conversation, KIND_TEXT

SCHEMA_VERSION = 7


def connect(path):
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)  # fresh installs: ~/.local/share/convo-index
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # Wait up to 5s for a lock instead of erroring immediately, so reads during a
    # concurrent refresh (manual or scheduled) don't fail with "database is locked".
    # Must come before the WAL switch: on a pre-WAL (v1) file that switch needs an
    # exclusive lock and would otherwise fail while another process is migrating.
    conn.execute("PRAGMA busy_timeout=5000")
    # The WAL switch on a pre-WAL (v1) file needs an exclusive lock and SQLite does not
    # always run the busy handler for it, so retry briefly while another process migrates.
    for attempt in range(50):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == 49:
                raise
            time.sleep(0.1)
    return conn


# ---------------------------------------------------------------- schema ----------

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT);

CREATE TABLE IF NOT EXISTS conversations(
    id TEXT PRIMARY KEY,                 -- "<source>:<native id>"
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    source_ref TEXT,                     -- file path or "sqlite:<db>#<row id>"
    source_revision TEXT,                -- change key actually read by the adapter
    archive_path TEXT,
    title TEXT, first_prompt TEXT,
    category TEXT, category_source TEXT,
    tickets TEXT,                        -- JSON array
    cwd TEXT, project_root TEXT, git_branch TEXT, git_origin_url TEXT,
    parent_id TEXT, relation TEXT,       -- subagent | fork
    provider TEXT, models TEXT,          -- models: JSON array
    fidelity TEXT, warnings TEXT, metadata TEXT,   -- warnings/metadata: JSON
    start_ts TEXT, end_ts TEXT,
    message_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_conversations_start ON conversations(start_ts);
CREATE INDEX IF NOT EXISTS idx_conversations_source ON conversations(source);
CREATE INDEX IF NOT EXISTS idx_conversations_parent ON conversations(parent_id);
CREATE INDEX IF NOT EXISTS idx_conversations_native ON conversations(native_id);

CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    native_id TEXT,                      -- tool's own id; identity across reindexes
    seq INTEGER, role TEXT, kind TEXT, ts TEXT, text TEXT,
    content_hash TEXT, model TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, seq);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(text, content='messages', content_rowid='id');

CREATE TABLE IF NOT EXISTS usage_records(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    native_request_id TEXT,
    message_id INTEGER,
    provider TEXT, model TEXT,
    input_uncached INTEGER, cache_read INTEGER, cache_write INTEGER,
    output INTEGER, reasoning INTEGER,
    reported_cost_usd REAL, ts TEXT,
    cache_write_1h INTEGER,               -- v7: 1-hour cache writes (Anthropic), NULL when not reported
    UNIQUE(conversation_id, native_request_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_conv ON usage_records(conversation_id);

-- Per-conversation user state, kept separate so it survives any rewrite of the
-- conversation row. `deleted` rows are a blocklist the indexer honours.
CREATE TABLE IF NOT EXISTS conversation_state(
    conversation_id TEXT PRIMARY KEY,
    hidden INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0
);
-- LLM-generated summary. summarized_revision is the source revision it was made from.
CREATE TABLE IF NOT EXISTS conversation_summary(
    conversation_id TEXT PRIMARY KEY,
    problem TEXT, solution TEXT, is_resolved TEXT,
    learning TEXT, claude_failed TEXT,
    summarized_revision TEXT
);
-- Pre-v2 ids (bare Claude Code session uuids) so old bookmarks keep resolving.
CREATE TABLE IF NOT EXISTS legacy_ids(legacy TEXT PRIMARY KEY, id TEXT NOT NULL);

-- v3: derived work (summaries; later vectors/tags) with its own retry state, independent
-- of ingestion, so a failed provider call is retried without the source changing.
CREATE TABLE IF NOT EXISTS derived_jobs(
    conversation_id TEXT NOT NULL, kind TEXT NOT NULL,
    input_fingerprint TEXT, status TEXT NOT NULL,      -- ok | failed
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, last_run TEXT,
    PRIMARY KEY(conversation_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON derived_jobs(kind, status);
-- v3: index-wide facts, e.g. the embedder fingerprint every vector was produced with.
CREATE TABLE IF NOT EXISTS index_meta(key TEXT PRIMARY KEY, value TEXT);

-- v4: user-defined tag groups (docs/design.md §7). Definitions are synced from config; assignments
-- are what the UI filters on; evidence explains them; decisions are the user's word.
CREATE TABLE IF NOT EXISTS tag_groups(
    name TEXT PRIMARY KEY, exclusive INTEGER NOT NULL DEFAULT 1, description TEXT, revision TEXT
);
CREATE TABLE IF NOT EXISTS tags(
    group_name TEXT NOT NULL, name TEXT NOT NULL, description TEXT, examples TEXT, rules TEXT,
    revision TEXT, updated_at TEXT, PRIMARY KEY(group_name, name)
);
CREATE TABLE IF NOT EXISTS tag_assignments(
    conversation_id TEXT NOT NULL, group_name TEXT NOT NULL, tag TEXT NOT NULL,
    source TEXT NOT NULL,                 -- rule | llm | user
    confidence REAL, evidence_id INTEGER, ts TEXT,
    PRIMARY KEY(conversation_id, group_name, tag)
);
CREATE INDEX IF NOT EXISTS idx_tag_assignments_tag ON tag_assignments(group_name, tag);
CREATE TABLE IF NOT EXISTS tag_evidence(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL, group_name TEXT NOT NULL, tag TEXT NOT NULL,
    method TEXT NOT NULL,                 -- rule | llm | neighbour
    confidence REAL, detail TEXT, origin_conversation_id TEXT, definition_revision TEXT, ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_tag_evidence_conv ON tag_evidence(conversation_id);
CREATE TABLE IF NOT EXISTS tag_decisions(
    conversation_id TEXT NOT NULL, group_name TEXT NOT NULL, tag TEXT,   -- tag NULL = none of these
    decision TEXT NOT NULL,               -- accept | reject
    ts TEXT,
    PRIMARY KEY(conversation_id, group_name, tag)
);

-- v5: references (docs/design.md §9). Every hit is kept with where it came from; only linkable rows
-- connect conversations. reference_decisions holds the user's linkable overrides.
CREATE TABLE IF NOT EXISTS references_(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
    namespace TEXT, message_seq INTEGER, span TEXT, method TEXT, confidence REAL,
    linkable INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_refs_conv ON references_(conversation_id);
CREATE INDEX IF NOT EXISTS idx_refs_key ON references_(key, linkable);
CREATE INDEX IF NOT EXISTS idx_refs_link_kind_key ON references_(linkable, kind, key, conversation_id);
CREATE TABLE IF NOT EXISTS reference_decisions(
    conversation_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, linkable INTEGER NOT NULL, ts TEXT,
    PRIMARY KEY(conversation_id, kind, key)
);

-- v6: one vector per conversation (docs/design.md §8). vec0 needs integer rowids, hence the id map.
CREATE TABLE IF NOT EXISTS conv_vec_ids(vid INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL UNIQUE);
"""


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def _is_legacy_v1(conn):
    return _table_exists(conn, "conversations") and "session_id" in _columns(conn, "conversations")


def _missing_versions(conn):
    applied = _applied_versions(conn)
    return {v for v in range(2, SCHEMA_VERSION + 1) if v not in applied}


def _is_current(conn):
    """Every version 2..SCHEMA_VERSION applied (not just the max) and no v1 layout."""
    return _table_exists(conn, "schema_migrations") and not _missing_versions(conn) and not _is_legacy_v1(conn)


def backup_file(db_path):
    """Copy the database with the SQLite backup API (WAL-safe, includes uncheckpointed
    pages). The destination is created exclusively so two processes can never share or
    overwrite one recovery copy. Returns the copy's path."""
    db_path = Path(db_path)
    for attempt in range(100):
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}" + (f"-{attempt}" if attempt else "")
        dest = db_path.with_name(f"{db_path.name}.bak-{stamp}")
        try:
            fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        break
    else:
        raise RuntimeError("could not allocate a backup file name")
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def init_schema(conn, db_path=None, *, have_lock=False, dim=None):
    """Create the current schema, or upgrade an older database in place.

    When `db_path` is given, an older database is backed up to a sibling file before migrating.
    Detection + backup + migration run under the cross-process index lock so the web
    server, the CLI and the scheduler starting together cannot both decide to migrate;
    callers that already hold the lock (the indexer) pass `have_lock=True`, since flock is
    not re-entrant across descriptors."""
    if _is_current(conn):
        # Fast path: already at the current version. Nothing to write, so no index lock and
        # no SQLite write lock: `serve` starting during a long refresh neither waits for it
        # nor competes with its transactions.
        return
    if db_path is not None and not have_lock:
        from convo_index.lock import index_lock
        with index_lock(db_path, blocking=True):
            _init_schema_locked(conn, db_path, dim)   # re-checks legacy state under the lock
    else:
        _init_schema_locked(conn, db_path, dim)


def _applied_versions(conn):
    if not _table_exists(conn, "schema_migrations"):
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def _init_schema_locked(conn, db_path, dim=None):
    applied = _applied_versions(conn)
    fresh = not _table_exists(conn, "conversations")
    pending = _is_legacy_v1(conn) or (not fresh and bool(_missing_versions(conn)))
    if pending and db_path is not None and Path(db_path).exists():
        backup_file(db_path)   # every migration of an existing database is preceded by a backup
    if _is_legacy_v1(conn):  # re-checked under the lock: another process may have migrated
        _migrate_v1_to_v2(conn)
    conn.executescript(_SCHEMA_V2)
    # The vector table's dimension is fixed at creation; a fresh index takes the configured
    # one, an existing table keeps its own (reembed recreates it when the embedder changes).
    d = int(vector_dim(conn) or dim or EMBED_DIM)
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages USING vec0(embedding float[{d}])")
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_conversations USING vec0(embedding float[{d}])")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if 2 not in applied:
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)", (now,))
    if 3 not in applied:
        if not fresh:
            _migrate_v2_to_v3(conn)
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)", (now,))
    if 4 not in applied:
        # v4 is tables only; definitions and the category -> workspace conversion are done by
        # tags.sync_definitions() at the start of each run, since they need the config.
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, ?)", (now,))
    if 5 not in applied:
        if not fresh:
            _migrate_v4_to_v5(conn)
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(5, ?)", (now,))
    if 6 not in applied:
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(6, ?)", (now,))
    if 7 not in applied:
        if "cache_write_1h" not in _columns(conn, "usage_records"):
            conn.execute("ALTER TABLE usage_records ADD COLUMN cache_write_1h INTEGER")
        # existing Claude Code rows hold the 5m+1h total in cache_write; the parser version bump
        # in the adapter re-ingests them with the split on the next run
        # titles that are really whole prompts (Codex threads) get the display cap once
        for cid, title in conn.execute(f"SELECT id, title FROM conversations WHERE length(title) > {TITLE_LIMIT} OR title LIKE '%' || char(10) || '%'").fetchall():
            conn.execute("UPDATE conversations SET title=? WHERE id=?", (display_title(title), cid))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(7, ?)", (now,))
    conn.commit()


def _migrate_v4_to_v5(conn):
    """Seed references_ from the legacy tickets JSON so related work exists before the next
    re-ingest (which recomputes references from the text with spans)."""
    rows = conn.execute("SELECT id, tickets FROM conversations WHERE tickets IS NOT NULL AND tickets != '[]'").fetchall()
    for cid, tj in rows:
        try:
            keys = json.loads(tj or "[]")
        except json.JSONDecodeError:
            continue
        for key in sorted(set(k for k in keys if isinstance(k, str))):
            conn.execute(
                "INSERT INTO references_(conversation_id, kind, key, namespace, span, method, confidence, linkable) "
                "VALUES(?, 'ticket', ?, '', ?, 'legacy', 1.0, 1)", (cid, key, key))


def _migrate_v2_to_v3(conn):
    """Seed derived_jobs from existing summaries so they are not re-run: status ok, no
    fingerprint (the job runner treats a summary at the current source revision as current
    and recomputes the fingerprint on the next change)."""
    conn.execute(
        """INSERT OR IGNORE INTO derived_jobs(conversation_id, kind, input_fingerprint, status, attempts, last_run)
           SELECT s.conversation_id, 'summary', NULL, 'ok', 1, ?
           FROM conversation_summary s JOIN conversations c ON c.id = s.conversation_id
           WHERE s.summarized_revision = c.source_revision""",
        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
    # Vectors made before fingerprints existed came from the old Ollama-native path. Their
    # exact preprocessing is unknown, so label them as legacy: the indexer will refuse to mix
    # new vectors in until `convo-index reembed` rebuilds the space.
    if _table_exists(conn, "vec_messages") and conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0]:
        conn.execute("INSERT OR IGNORE INTO index_meta(key, value) VALUES('embedding_fingerprint', ?)",
                     (f"legacy|ollama-native|unknown-preprocessing|dim={vector_dim(conn)}",))


def _migrate_v1_to_v2(conn):
    """v1 -> v2: namespace ids, add source/native_id/revision columns, stable message
    metadata, legacy id aliases. Runs in one transaction; vectors keep their rowids.

    Legacy rows keep their mtime-only `source_revision`, which never equals the adapter's
    "<mtime>:<size>" key, so every conversation whose source file still exists is
    re-ingested exactly once (that backfills native ids, models and usage while positional
    hash matching keeps the row ids and vectors). Summaries are the expensive part, so for
    files that are unchanged the *summary* revision is upgraded to the new form instead."""
    from convo_index.model import content_hash as _hash
    prefix = "claude_code:"
    conn.execute("BEGIN IMMEDIATE")
    try:
        # conversations -----------------------------------------------------------
        cols = _columns(conn, "conversations")
        conn.execute("ALTER TABLE conversations RENAME COLUMN session_id TO id")
        conn.execute("ALTER TABLE conversations RENAME COLUMN source_path TO source_ref")
        for col in ("source", "native_id", "source_revision", "git_origin_url", "parent_id",
                    "relation", "provider", "models", "fidelity", "warnings", "metadata"):
            if col not in cols:
                conn.execute(f"ALTER TABLE conversations ADD COLUMN {col} TEXT")
        if "project_root" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN project_root TEXT")
        rows = conn.execute("SELECT id, file_mtime, source_ref FROM conversations").fetchall()
        unchanged_files = {}
        for old_id, mtime, ref in rows:
            rev = str(mtime) if mtime is not None else None
            conn.execute(
                """UPDATE conversations SET id=?, native_id=?, source='claude_code',
                          source_revision=?, fidelity='full', models='[]', warnings='[]',
                          metadata='{}' WHERE id=?""",
                (prefix + old_id, old_id, rev, old_id))
            if ref and rev is not None:
                try:
                    st = Path(ref).stat()
                except OSError:
                    continue
                if str(st.st_mtime) == rev:
                    # the adapter's *current* key (incl. parser version): the summary of an
                    # unchanged file stays valid, only the transcript is re-ingested once
                    from convo_index.sources.claude_code import revision_of
                    unchanged_files[prefix + old_id] = (rev, revision_of(ref))
        conn.execute("ALTER TABLE conversations DROP COLUMN file_mtime")

        # messages ----------------------------------------------------------------
        mcols = _columns(conn, "messages")
        conn.execute("ALTER TABLE messages RENAME COLUMN session_id TO conversation_id")
        for col in ("native_id", "kind", "content_hash", "model"):
            if col not in mcols:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
        conn.execute("UPDATE messages SET conversation_id=? || conversation_id, kind='text'", (prefix,))
        for mid, role, text in conn.execute("SELECT id, role, text FROM messages").fetchall():
            conn.execute("UPDATE messages SET content_hash=? WHERE id=?",
                         (_hash(role or "", KIND_TEXT, text or ""), mid))
        conn.execute("DROP INDEX IF EXISTS idx_messages_session")

        # per-conversation side tables --------------------------------------------
        for table in ("conversation_state", "conversation_summary"):
            if _table_exists(conn, table):
                conn.execute(f"ALTER TABLE {table} RENAME COLUMN session_id TO conversation_id")
                conn.execute(f"UPDATE {table} SET conversation_id=? || conversation_id", (prefix,))
        if _table_exists(conn, "conversation_summary"):
            conn.execute("ALTER TABLE conversation_summary ADD COLUMN summarized_revision TEXT")
            for cid, mtime in conn.execute(
                    "SELECT conversation_id, summarized_mtime FROM conversation_summary").fetchall():
                rev = str(mtime) if mtime is not None else None
                if rev is not None and unchanged_files.get(cid, ("",))[0] == rev:
                    rev = unchanged_files[cid][1]  # summary made from the unchanged file: stays valid under the new key
                conn.execute("UPDATE conversation_summary SET summarized_revision=? WHERE conversation_id=?",
                             (rev, cid))
            conn.execute("ALTER TABLE conversation_summary DROP COLUMN summarized_mtime")

        # legacy ids (table may not exist yet on a v1 db) ---------------------------
        conn.execute("CREATE TABLE IF NOT EXISTS legacy_ids(legacy TEXT PRIMARY KEY, id TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO legacy_ids(legacy, id) SELECT native_id, id FROM conversations")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)")
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                     (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- lookups ---------

class AmbiguousId(LookupError):
    """A bare native id matches conversations from more than one source."""


def vector_dim(conn):
    """Dimension the existing vec_messages table was created with (parsed from its DDL)."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='vec_messages'").fetchone()
    if not row or not row[0]:
        return None
    import re as _re
    m = _re.search(r"float\[(\d+)\]", row[0])
    return int(m.group(1)) if m else None


def recreate_vectors(conn, dim, fingerprint=None):
    """Drop every vector (messages and conversations) and recreate the tables with `dim`
    (used by `reembed`); the vector jobs are reset so the dispatcher re-embeds. Validated
    first and done in one transaction, so a bad dimension or a failure mid-way leaves the
    existing tables (and the recorded embedder fingerprint) untouched."""
    try:
        d = int(dim)
    except (TypeError, ValueError):
        raise ValueError(f"vector dimension must be an integer, not {dim!r}") from None
    if not 1 <= d <= 65536:
        raise ValueError(f"vector dimension must be between 1 and 65536, not {d}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS vec_messages")
        conn.execute(f"CREATE VIRTUAL TABLE vec_messages USING vec0(embedding float[{d}])")
        conn.execute("DROP TABLE IF EXISTS vec_conversations")
        conn.execute(f"CREATE VIRTUAL TABLE vec_conversations USING vec0(embedding float[{d}])")
        if _table_exists(conn, "conv_vec_ids"):
            conn.execute("DELETE FROM conv_vec_ids")
        if _table_exists(conn, "derived_jobs"):
            conn.execute("DELETE FROM derived_jobs WHERE kind='vector'")
        if _table_exists(conn, "tag_evidence"):
            # similarities from another embedding space mean nothing: suggestions and their
            # calibration history go with the vectors
            conn.execute("DELETE FROM tag_evidence WHERE method='neighbour'")
        if fingerprint is not None:
            conn.execute("INSERT INTO index_meta(key, value) VALUES('embedding_fingerprint', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (fingerprint,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM index_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key, value):
    conn.execute("INSERT INTO index_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
    conn.commit()


def resolve_id(conn, id_or_legacy):
    """Canonical conversation id for a namespaced id, a pre-v2 bare session uuid, or a bare
    native id (e.g. pasted from a tool's own --resume command). A bare native id is only
    accepted when it is unambiguous; otherwise AmbiguousId lists the candidates so Hide /
    Delete can never act on the wrong conversation."""
    if conn.execute("SELECT 1 FROM conversations WHERE id=?", (id_or_legacy,)).fetchone():
        return id_or_legacy
    row = conn.execute("SELECT id FROM legacy_ids WHERE legacy=?", (id_or_legacy,)).fetchone()
    if row:
        return row[0]
    rows = conn.execute("SELECT id FROM conversations WHERE native_id=? ORDER BY id LIMIT 3",
                        (id_or_legacy,)).fetchall()
    if len(rows) > 1:
        raise AmbiguousId([r[0] for r in rows])
    return rows[0][0] if rows else id_or_legacy


def is_deleted(conn, conversation_id):
    row = conn.execute("SELECT deleted FROM conversation_state WHERE conversation_id=?",
                       (conversation_id,)).fetchone()
    return bool(row and row[0])


def exists(conn, conversation_id):
    return conn.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone() is not None


def get_revision(conn, conversation_id):
    row = conn.execute("SELECT source_revision FROM conversations WHERE id=?",
                       (conversation_id,)).fetchone()
    return row[0] if row else None


def list_sources(conn):
    return [r[0] for r in conn.execute(
        "SELECT source FROM conversations GROUP BY source ORDER BY COUNT(*) DESC").fetchall()]


def set_hidden(conn, conversation_id, hidden):
    conn.execute(
        """INSERT INTO conversation_state(conversation_id, hidden) VALUES(?, ?)
           ON CONFLICT(conversation_id) DO UPDATE SET hidden=excluded.hidden""",
        (conversation_id, 1 if hidden else 0),
    )
    conn.commit()


def get_hidden_ids(conn):
    return {r[0] for r in conn.execute(
        "SELECT conversation_id FROM conversation_state WHERE hidden=1").fetchall()}


def get_deleted_ids(conn):
    return {r[0] for r in conn.execute(
        "SELECT conversation_id FROM conversation_state WHERE deleted=1").fetchall()}


_SUMMARY_FIELDS = ("problem", "solution", "is_resolved", "learning", "claude_failed")


def mark_vector_stale(conn, conversation_id):
    """The summary or the conversation row changed: the conversation vector may be out of
    date. Found in SQL by vectors.pending_vectors; run_vector re-checks the text fingerprint."""
    if _table_exists(conn, "derived_jobs"):
        conn.execute("UPDATE derived_jobs SET status='stale' WHERE conversation_id=? AND kind='vector' AND status='ok'",
                     (conversation_id,))
        # a failed (even exhausted) job gets a fresh attempt for the changed inputs; it stays
        # failed because the stored vector, if any, is for an older text
        conn.execute("UPDATE derived_jobs SET attempts=0 WHERE conversation_id=? AND kind='vector' AND status='failed'",
                     (conversation_id,))


def set_summary(conn, conversation_id, summary, summarized_revision):
    mark_vector_stale(conn, conversation_id)
    conn.execute(
        """INSERT INTO conversation_summary
               (conversation_id, problem, solution, is_resolved, learning, claude_failed, summarized_revision)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(conversation_id) DO UPDATE SET
               problem=excluded.problem, solution=excluded.solution,
               is_resolved=excluded.is_resolved, learning=excluded.learning,
               claude_failed=excluded.claude_failed, summarized_revision=excluded.summarized_revision""",
        (conversation_id, *(summary.get(f) for f in _SUMMARY_FIELDS), summarized_revision),
    )
    conn.commit()


def set_summary_if_current(conn, conversation_id, summary, revision):
    """Store a summary only if the conversation still exists, is not deleted, and is still
    at `revision` (the transcript the summary was made from). Returns True when written.
    Summaries are produced by slow LLM calls, so the world may have moved on meanwhile."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT source_revision FROM conversations WHERE id=?",
                           (conversation_id,)).fetchone()
        if row is None or row[0] != revision or is_deleted(conn, conversation_id):
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            """INSERT INTO conversation_summary
                   (conversation_id, problem, solution, is_resolved, learning, claude_failed, summarized_revision)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(conversation_id) DO UPDATE SET
                   problem=excluded.problem, solution=excluded.solution,
                   is_resolved=excluded.is_resolved, learning=excluded.learning,
                   claude_failed=excluded.claude_failed, summarized_revision=excluded.summarized_revision""",
            (conversation_id, *(summary.get(f) for f in _SUMMARY_FIELDS), revision),
        )
        mark_vector_stale(conn, conversation_id)
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_summary(conn, conversation_id):
    row = conn.execute(
        f"SELECT {', '.join(_SUMMARY_FIELDS)} FROM conversation_summary WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    return dict(zip(_SUMMARY_FIELDS, row, strict=False)) if row else None


def get_summarized_revision(conn, conversation_id):
    row = conn.execute(
        "SELECT summarized_revision FROM conversation_summary WHERE conversation_id=?",
        (conversation_id,)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- writes ----------

def _fts_delete(conn, mid, old_text):
    # External-content FTS5 tables are updated with the special 'delete' command.
    conn.execute("INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', ?, ?)",
                 (mid, old_text or ""))


def _delete_message_row(conn, mid, old_text):
    _fts_delete(conn, mid, old_text)
    conn.execute("DELETE FROM vec_messages WHERE rowid=?", (mid,))
    conn.execute("DELETE FROM messages WHERE id=?", (mid,))


def _delete_conversation_rows(conn, conversation_id):
    for mid, text in conn.execute("SELECT id, text FROM messages WHERE conversation_id=?",
                                  (conversation_id,)).fetchall():
        _delete_message_row(conn, mid, text)
    conn.execute("DELETE FROM usage_records WHERE conversation_id=?", (conversation_id,))
    for table in ("derived_jobs", "tag_assignments", "tag_evidence", "references_"):
        if _table_exists(conn, table):
            conn.execute(f"DELETE FROM {table} WHERE conversation_id=?", (conversation_id,))
    if _table_exists(conn, "conv_vec_ids"):
        vid = conn.execute("SELECT vid FROM conv_vec_ids WHERE conversation_id=?", (conversation_id,)).fetchone()
        if vid:
            conn.execute("DELETE FROM vec_conversations WHERE rowid=?", (vid[0],))
            conn.execute("DELETE FROM conv_vec_ids WHERE vid=?", (vid[0],))
    # tag_decisions / reference_decisions are the user's word: kept across re-ingestion.
    conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))


def remove_conversation(conn, conversation_id):
    """Drop a conversation's indexed content because the source no longer has it (emptied,
    reverted, or newly recognised as the indexer's own call). No blocklist entry: if the
    source grows real content again it will be indexed again. Returns True if rows existed."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existed = exists(conn, conversation_id)
        _delete_conversation_rows(conn, conversation_id)
        conn.execute("DELETE FROM conversation_summary WHERE conversation_id=?", (conversation_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return existed


def delete_conversation(conn, conversation_id):
    """Permanently drop a conversation and blocklist it so reindex won't re-add it.
    Returns its archive_path (if any) so the caller can remove the file."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT archive_path FROM conversations WHERE id=?",
                           (conversation_id,)).fetchone()
        archive_path = row[0] if row else None
        _delete_conversation_rows(conn, conversation_id)
        conn.execute("DELETE FROM conversation_summary WHERE conversation_id=?", (conversation_id,))
        conn.execute(
            """INSERT INTO conversation_state(conversation_id, deleted) VALUES(?, 1)
               ON CONFLICT(conversation_id) DO UPDATE SET deleted=1""",
            (conversation_id,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return archive_path


def plan_messages(conn, conv: Conversation):
    """Match incoming messages to stored rows.

    Pass 1 claims rows by native id. Pass 2 falls back to (seq, content hash) but only
    against rows that carry no native id (legacy/migrated rows), so a repeated prompt such
    as "continue" can never steal another message's identity.
    Returns (pairs, to_delete): pairs is [(message, existing_row_or_None)], rows are
    (id, native_id, seq, content_hash, text)."""
    existing = conn.execute(
        "SELECT id, native_id, seq, content_hash, text, kind FROM messages WHERE conversation_id=?",
        (conv.id,)).fetchall()
    by_native = {r[1]: r for r in existing if r[1]}
    anonymous_by_pos = {(r[2], r[3]): r for r in existing if not r[1]}
    claimed = set()
    matched = {}
    for m in conv.messages:
        row = by_native.get(m.native_id) if m.native_id else None
        if row is not None and row[0] not in claimed:
            claimed.add(row[0])
            matched[m.seq] = row
    for m in conv.messages:
        if m.seq in matched:
            continue
        row = anonymous_by_pos.get((m.seq, m.content_hash))
        if row is not None and row[0] not in claimed:
            claimed.add(row[0])
            matched[m.seq] = row
    pairs = [(m, matched.get(m.seq)) for m in conv.messages]
    to_delete = [r for r in existing if r[0] not in claimed]
    return pairs, to_delete


def _needs_embedding(pairs, have_vec):
    for m, row in pairs:
        if m.kind != KIND_TEXT:
            continue
        if row is None or row[3] != m.content_hash or row[0] not in have_vec:
            yield m


def _vector_rowids(conn, conversation_id):
    return {r[0] for r in conn.execute(
        "SELECT m.id FROM messages m JOIN vec_messages v ON v.rowid = m.id WHERE m.conversation_id=?",
        (conversation_id,)).fetchall()}


_PARSER_PREFIX = re.compile(r"^p\d+:")


def same_content_revision(a, b):
    """True when two change keys differ only in the adapter's parser-version prefix (`p<n>:`),
    i.e. the source file/row is unchanged and only our parsing of it moved on."""
    if not a or not b:
        return False
    return _PARSER_PREFIX.sub("", a) == _PARSER_PREFIX.sub("", b)


def upsert_conversation(conn, conv: Conversation, category, category_source, tickets,
                        archive_path, revision, embed_fn, extra=None):
    """Write a conversation, reconciling messages against what is stored so row ids stay
    stable. `extra(conn)`, if given, runs inside the same transaction just before COMMIT.
    Only text messages without a vector are embedded; `embed_fn(texts)` returns a
    vector or None per text.

    Embedding runs *before* the write transaction so a slow embedder never holds the
    database lock; the plan is then recomputed inside the transaction, because the web
    server may have deleted or changed rows in between. Returns per-message stats, or None
    when the conversation is on the user's delete blocklist and nothing was written."""
    cid = conv.id
    pairs, _ = plan_messages(conn, conv)
    vectors = {}
    need = list(_needs_embedding(pairs, _vector_rowids(conn, cid)))
    if need:
        for m, v in zip(need, embed_fn([m.text for m in need]), strict=False):
            vectors[m.seq] = v

    stats = {"inserted": 0, "updated": 0, "deleted": 0, "embedded": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        # The user may have deleted this conversation (UI) while we were embedding; the
        # blocklist wins, otherwise the indexer would resurrect it.
        if is_deleted(conn, cid):
            conn.execute("ROLLBACK")
            return None
        prev_rev = conn.execute("SELECT source_revision FROM conversations WHERE id=?", (cid,)).fetchone()
        prev_rev = prev_rev[0] if prev_rev else None
        pairs, to_delete = plan_messages(conn, conv)   # authoritative plan, under the lock
        have_vec = _vector_rowids(conn, cid)
        for r in to_delete:
            _delete_message_row(conn, r[0], r[4])
            stats["deleted"] += 1
        # The ordered text messages are what summaries and tags read: if they are the same
        # before and after, derived work made from them is still valid.
        before = [r[3] for r in sorted([r for _, r in pairs if r is not None] + list(to_delete), key=lambda r: r[2])
                  if r[5] == KIND_TEXT]
        after = [m.content_hash for m in conv.messages if m.kind == KIND_TEXT]
        text_changed = before != after
        seq_to_id = {}
        for m, row in pairs:
            if row is None:
                cur = conn.execute(
                    """INSERT INTO messages(conversation_id, native_id, seq, role, kind, ts, text,
                                            content_hash, model) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (cid, m.native_id, m.seq, m.role, m.kind, m.ts, m.text, m.content_hash, m.model))
                mid = cur.lastrowid
                conn.execute("INSERT INTO messages_fts(rowid, text) VALUES(?,?)", (mid, m.text))
                stats["inserted"] += 1
                needs_vec = m.kind == KIND_TEXT
            else:
                mid = row[0]
                changed = row[3] != m.content_hash
                if changed:
                    _fts_delete(conn, mid, row[4])
                    conn.execute("INSERT INTO messages_fts(rowid, text) VALUES(?,?)", (mid, m.text))
                    conn.execute("DELETE FROM vec_messages WHERE rowid=?", (mid,))
                    stats["updated"] += 1
                conn.execute(
                    """UPDATE messages SET native_id=?, seq=?, role=?, kind=?, ts=?, text=?,
                                           content_hash=?, model=? WHERE id=?""",
                    (m.native_id, m.seq, m.role, m.kind, m.ts, m.text, m.content_hash, m.model, mid))
                needs_vec = m.kind == KIND_TEXT and (changed or mid not in have_vec)
            seq_to_id[m.seq] = mid
            v = vectors.get(m.seq)
            if needs_vec and v is not None:
                conn.execute("DELETE FROM vec_messages WHERE rowid=?", (mid,))
                conn.execute("INSERT INTO vec_messages(rowid, embedding) VALUES(?,?)",
                             (mid, sqlite_vec.serialize_float32(v)))
                stats["embedded"] += 1

        conn.execute(
            """INSERT INTO conversations(id, source, native_id, source_ref, source_revision,
                    archive_path, title, first_prompt, category, category_source, tickets,
                    cwd, project_root, git_branch, git_origin_url, parent_id, relation,
                    provider, models, fidelity, warnings, metadata, start_ts, end_ts, message_count)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                    source_ref=excluded.source_ref, source_revision=excluded.source_revision,
                    archive_path=excluded.archive_path, title=excluded.title,
                    first_prompt=excluded.first_prompt, category=excluded.category,
                    category_source=excluded.category_source, tickets=excluded.tickets,
                    cwd=excluded.cwd, project_root=excluded.project_root,
                    git_branch=excluded.git_branch, git_origin_url=excluded.git_origin_url,
                    parent_id=excluded.parent_id, relation=excluded.relation,
                    provider=excluded.provider, models=excluded.models, fidelity=excluded.fidelity,
                    warnings=excluded.warnings, metadata=excluded.metadata,
                    start_ts=excluded.start_ts, end_ts=excluded.end_ts,
                    message_count=excluded.message_count""",
            (cid, conv.source, conv.native_id, conv.source_ref, revision, archive_path,
             display_title(conv.title, conv.first_prompt or cid), conv.first_prompt, category, category_source, json.dumps(tickets),
             conv.cwd, conv.project_root or conv.cwd, conv.git_branch, conv.git_origin_url,
             conv.parent_id, conv.relation[0] if conv.relation else None,
             conv.provider, json.dumps(conv.models), conv.fidelity, json.dumps(conv.warnings),
             json.dumps(conv.metadata), conv.start_ts, conv.end_ts, len(conv.messages)))
        mark_vector_stale(conn, cid)
        if prev_rev and prev_rev != revision and same_content_revision(prev_rev, revision) and not text_changed:
            # Only the adapter's parser version changed, and it left every text message as it
            # was: derived work made from this content is still valid. Carry the summary and the
            # tags job to the new revision so a parser bump never re-runs the LLM over the archive.
            conn.execute("UPDATE conversation_summary SET summarized_revision=? WHERE conversation_id=? AND summarized_revision=?",
                         (revision, cid, prev_rev))
            conn.execute("UPDATE derived_jobs SET input_fingerprint = replace(input_fingerprint, ?, ?) "
                         "WHERE conversation_id=? AND kind IN ('summary', 'tags') AND input_fingerprint LIKE '%' || ? || '%'",
                         (prev_rev, revision, cid, prev_rev))

        # Usage is derived data: replace wholesale, linking to stable message ids.
        conn.execute("DELETE FROM usage_records WHERE conversation_id=?", (cid,))
        for u in conv.usage:
            conn.execute(
                """INSERT OR REPLACE INTO usage_records(conversation_id, native_request_id, message_id,
                        provider, model, input_uncached, cache_read, cache_write, output, reasoning,
                        reported_cost_usd, ts, cache_write_1h) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, u.native_request_id, seq_to_id.get(u.message_seq), u.provider, u.model,
                 u.input_uncached, u.cache_read, u.cache_write, u.output, u.reasoning,
                 u.reported_cost_usd, u.ts, u.cache_write_1h))
        if extra is not None:
            extra(conn)   # e.g. references + ticket mirror: land with the revision or not at all
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return stats
