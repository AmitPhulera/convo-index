import json

import pytest

from convo_index import db as dbm
from convo_index import indexer
from convo_index.sources.claude_code import ClaudeCodeAdapter
from convo_index.sources.opencode import OpencodeAdapter
from convo_index.lock import index_lock

from tests.conftest import make_settings
from tests.test_opencode import make_opencode_db


def _make_file(projects, sid, cwd, text, entrypoint=None, uuid=None):
    d = projects / "proj"; d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    rec = {
        "type": "user", "isMeta": False, "timestamp": "2026-06-16T00:00:00Z",
        "cwd": cwd, "gitBranch": "main", "sessionId": sid, "uuid": uuid or f"{sid}-u0",
        "message": {"role": "user", "content": text},
    }
    if entrypoint:
        rec["entrypoint"] = entrypoint
    f.write_text(json.dumps(rec) + "\n")
    return f


def _quiet(monkeypatch, tmp_path, embed=None):
    """No embedder, no LLM (settings have no backend), no archive copies. Returns settings
    that every indexer call in the test must be given."""
    settings = make_settings(tmp_path)
    fn = embed or (lambda texts: [None] * len(texts))
    monkeypatch.setattr(indexer, "default_embed_fn", lambda conn, s, w: fn)
    monkeypatch.setattr(indexer, "embed_fn_and_fingerprint", lambda conn, s, w: (fn, "test-embedder"))
    monkeypatch.setattr(indexer, "write_archive_copy", lambda *a, **k: tmp_path / "arch.jsonl")
    monkeypatch.setattr(indexer.S, "load", lambda *a, **k: settings)
    return settings


def _load(adapter, sid):
    ref = next(r for r in adapter.discover() if r.native_id == sid)
    return adapter.load(ref)


def test_adapter_skips_headless_sdk_session(tmp_path):
    projects = tmp_path / "projects"
    _make_file(projects, "h1", "/x", "hello there", entrypoint="sdk-cli")
    assert _load(ClaudeCodeAdapter(projects), "h1") is None


def test_adapter_indexes_a_headless_handoff_session(tmp_path):
    from convo_index import handoff as H
    projects = tmp_path / "projects"
    for sid, kind in (("h3", "continue"), ("h4", "review")):
        _make_file(projects, sid, "/x", H._OPENING[kind].format(harness="opencode") + "\n\n## Where and when", entrypoint="sdk-cli")
        loaded = _load(ClaudeCodeAdapter(projects), sid)
        assert loaded is not None and loaded[0].native_id == sid and loaded[0].title in ("Continued", "Second opinion")
    _make_file(projects, "h5", "/x", "You are reviewing my essay", entrypoint="sdk-cli")    # a lookalike stays out
    assert _load(ClaudeCodeAdapter(projects), "h5") is None


def test_adapter_skips_own_classification_prompt(tmp_path):
    # Backstop: a classification session written without a flushed entrypoint line
    # must still be skipped based on its prompt text.
    projects = tmp_path / "projects"
    _make_file(projects, "h2", "/x", "Classify this developer conversation as either 'globex' or 'personal'")
    assert _load(ClaudeCodeAdapter(projects), "h2") is None


def test_index_conversation_end_to_end(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    projects = tmp_path / "projects"
    _make_file(projects, "s1", "/Users/alice/Development/globex/widgets-hq", "Fix SAAS-42")
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn)
    conv, revision = _load(ClaudeCodeAdapter(projects), "s1")
    indexer.index_conversation(conn, conv, revision, settings=settings)
    row = conn.execute("SELECT category, tickets, source, source_revision FROM conversations "
                       "WHERE id='claude_code:s1'").fetchone()
    assert row[0] == "globex"
    assert json.loads(row[1]) == ["SAAS-42"]
    assert row[2] == "claude_code" and row[3] == revision


def test_discover_covers_every_project_dir(tmp_path):
    """Sessions started inside this tool's own checkout are ordinary conversations; only the
    headless sessions it runs itself are excluded, per session (see exclude_reason)."""
    projects = tmp_path / "projects"
    for d in ("real-proj", "-Users-alice-Development-convo-index"):
        (projects / d).mkdir(parents=True)
    (projects / "real-proj" / "a.jsonl").write_text("{}")
    (projects / "-Users-alice-Development-convo-index" / "b.jsonl").write_text("{}")
    assert sorted(r.native_id for r in ClaudeCodeAdapter(projects).discover()) == ["a", "b"]


def test_run_indexes_once_then_skips_unchanged(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    projects = tmp_path / "projects"
    _make_file(projects, "s1", "/x", "hi")
    db_path = tmp_path / "t.db"
    ad = ClaudeCodeAdapter(projects)
    assert indexer.run([ad], db_path, settings)["indexed"] == 1
    totals = indexer.run([ad], db_path, settings)
    assert totals["indexed"] == 0 and totals["skipped"] == 1


def test_run_skips_deleted_sessions(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    projects = tmp_path / "projects"
    _make_file(projects, "del1", "/x", "hello")
    db_path = tmp_path / "t.db"
    conn = dbm.connect(db_path); dbm.init_schema(conn)
    dbm.delete_conversation(conn, "claude_code:del1")  # blocklist before it is ever indexed
    conn.close()
    indexer.run([ClaudeCodeAdapter(projects)], db_path, settings)
    conn = dbm.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_run_indexes_opencode_alongside_claude(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    projects = tmp_path / "projects"
    _make_file(projects, "s1", "/x", "hi from claude")
    ocdb = make_opencode_db(tmp_path / "opencode.db", with_child=True)
    db_path = tmp_path / "t.db"
    totals = indexer.run([ClaudeCodeAdapter(projects), OpencodeAdapter(ocdb)], db_path, settings)
    assert totals["indexed"] == 3
    conn = dbm.connect(db_path)
    rows = conn.execute("SELECT id, source, parent_id, category FROM conversations ORDER BY id").fetchall()
    assert rows == [("claude_code:s1", "claude_code", None, "unknown"),
                    ("opencode:ses_1", "opencode", None, "globex"),
                    ("opencode:ses_child", "opencode", "opencode:ses_1", "globex")]
    assert conn.execute("SELECT COUNT(*) FROM usage_records WHERE conversation_id='opencode:ses_1'").fetchone()[0] == 1
    assert conn.execute("SELECT archive_path FROM conversations WHERE id='opencode:ses_1'").fetchone()[0] is None


def test_run_removes_content_when_session_becomes_empty_or_excluded(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    projects = tmp_path / "projects"
    _make_file(projects, "s1", "/x", "real content")
    db_path = tmp_path / "t.db"
    ad = ClaudeCodeAdapter(projects)
    indexer.run([ad], db_path, settings)
    conn = dbm.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    conn.close()
    # the file is rewritten as a headless SDK session (excluded) -> its old content must go
    _make_file(projects, "s1", "/x", "now headless", entrypoint="sdk-cli")
    totals = indexer.run([ad], db_path, settings)
    assert totals["removed"] == 1
    conn = dbm.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert dbm.get_deleted_ids(conn) == set()   # not a user deletion: may come back later


def test_unreadable_session_keeps_existing_content_and_counts_failed(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    projects = tmp_path / "projects"
    f = _make_file(projects, "s1", "/x", "real content")
    db_path = tmp_path / "t.db"
    ad = ClaudeCodeAdapter(projects)
    indexer.run([ad], db_path, settings)
    f.write_text("GARBAGE\nMORE GARBAGE\n")        # e.g. a corrupted file: not a confirmed empty
    totals = indexer.run([ad], db_path, settings)
    assert totals["failed"] == 1 and totals["removed"] == 0
    conn = dbm.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_metadata_plus_malformed_file_preserves_everything_indexed(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path, embed=lambda texts: [[0.0] * 768 for _ in texts])
    from convo_index import jobs
    def fake_dispatch(conn, s=None, attempted=None, **kw):
        for cid, rev in conn.execute("SELECT id, source_revision FROM conversations").fetchall():
            dbm.set_summary(conn, cid, {"problem": "P"}, rev)
        return {}
    monkeypatch.setattr(jobs, "dispatch", fake_dispatch)
    projects = tmp_path / "projects"
    f = _make_file(projects, "s1", "/x", "real content")
    db_path = tmp_path / "t.db"
    ad = ClaudeCodeAdapter(projects)
    indexer.run([ad], db_path, settings)
    conn = dbm.connect(db_path)
    before = (conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
              conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0],
              dbm.get_summary(conn, "claude_code:s1"), dbm.get_revision(conn, "claude_code:s1"))
    conn.close()
    f.write_text('{"type":"ai-title","aiTitle":"Still titled"}\n{"type":"user","message":{"role":"user","content":"hi"\n')
    totals = indexer.run([ad], db_path, settings)
    assert totals["failed"] == 1 and totals["removed"] == 0
    conn = dbm.connect(db_path)
    after = (conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0],
             dbm.get_summary(conn, "claude_code:s1"), dbm.get_revision(conn, "claude_code:s1"))
    assert after == before == (1, 1, {"problem": "P", "solution": None, "is_resolved": None,
                                      "learning": None, "claude_failed": None}, before[3])


def test_archive_written_before_a_refused_upsert_is_removed(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    arch = tmp_path / "arch.jsonl"
    def write_archive(*a, **k):
        arch.write_text("copy"); return arch
    monkeypatch.setattr(indexer, "write_archive_copy", write_archive)
    projects = tmp_path / "projects"
    _make_file(projects, "s1", "/x", "hi")
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn)
    conv, revision = _load(ClaudeCodeAdapter(projects), "s1")
    def embed_then_delete(texts):
        dbm.delete_conversation(conn, conv.id); return [None] * len(texts)
    assert indexer.index_conversation(conn, conv, revision, embed_fn=embed_then_delete, settings=settings) is None
    assert not arch.exists()


def test_discovery_failure_counts_as_failed(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    class Broken:
        name = "broken"
        def discover(self):
            raise RuntimeError("db locked")
    totals = indexer.run([Broken()], tmp_path / "t.db", settings)
    assert totals["failed"] == 1 and totals["sources"]["broken"]["failed"] == 1


def test_main_exit_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "run", lambda *a, **k: {"failed": 0})
    assert indexer.main() == indexer.EXIT_OK
    monkeypatch.setattr(indexer, "run", lambda *a, **k: {"failed": 2})
    assert indexer.main() == indexer.EXIT_FAILED
    monkeypatch.setattr(indexer, "run", lambda *a, **k: None)
    assert indexer.main() == indexer.EXIT_LOCKED


def test_run_migrates_legacy_db_with_backup(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    from tests.test_migration import _make_v1
    db_path = tmp_path / "convos.db"
    _make_v1(db_path)
    totals = indexer.run([], db_path, settings)
    assert totals["failed"] == 0
    assert len(list(tmp_path.glob("convos.db.bak-*"))) == 1
    conn = dbm.connect(db_path)
    assert conn.execute("SELECT id FROM conversations").fetchone() == ("claude_code:abc",)


def test_run_survives_one_bad_conversation(tmp_path, monkeypatch, capsys):
    settings = _quiet(monkeypatch, tmp_path)
    projects = tmp_path / "projects"
    _make_file(projects, "ok", "/x", "fine")
    _make_file(projects, "bad", "/x", "boom")
    ad = ClaudeCodeAdapter(projects)
    real_load = ad.load
    def flaky(ref):
        if ref.native_id == "bad":
            raise RuntimeError("corrupt")
        return real_load(ref)
    ad.load = flaky
    totals = indexer.run([ad], tmp_path / "t.db", settings)
    assert {k: totals[k] for k in ("indexed", "skipped", "removed", "failed")} == \
        {"indexed": 1, "skipped": 0, "removed": 0, "failed": 1}
    assert "corrupt" in capsys.readouterr().err


def test_run_returns_none_when_another_indexer_holds_the_lock(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    db_path = tmp_path / "t.db"
    with index_lock(db_path):
        assert indexer.run([], db_path, settings) is None


def test_summary_job_runs_through_index_conversation(tmp_path, monkeypatch, fake_llm):
    settings = _quiet(monkeypatch, tmp_path)
    settings["llm"]["summary"] = {"backend": "claude_cli", "model": "m"}
    settings["privacy"]["hosted_processing"] = "allow"
    f = fake_llm({"problem": "P", "solution": "S", "is_resolved": "Yes", "learning": "", "claude_failed": "No"},
                 {"problem": "P2", "solution": "S", "is_resolved": "Yes", "learning": "", "claude_failed": "No"})
    projects = tmp_path / "projects"
    _make_file(projects, "s1", "/Users/me/Development/globex/hq", "hi")   # path hint: no classify LLM call
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn)
    conv, revision = _load(ClaudeCodeAdapter(projects), "s1")
    assert indexer.index_conversation(conn, conv, revision, settings=settings)["summary"] == "ok"
    assert indexer.index_conversation(conn, conv, revision, settings=settings)["summary"] == "skipped"
    assert len(f.calls) == 1
    conn.execute("UPDATE conversations SET source_revision=?", (revision + "x",)); conn.commit()
    assert indexer.index_conversation(conn, conv, revision + "x", settings=settings)["summary"] == "ok"
    assert dbm.get_summary(conn, "claude_code:s1")["problem"] == "P2"


def test_exclude_cwd_glob_keeps_project_out(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    settings["sources"]["claude_code"]["exclude_cwd"] = ["/secret/*"]
    projects = tmp_path / "projects"
    _make_file(projects, "s1", "/secret/client", "shh")
    _make_file(projects, "s2", "/open/proj", "hello")
    totals = indexer.run([ClaudeCodeAdapter(projects)], tmp_path / "t.db", settings)
    assert totals["indexed"] == 1
    conn = dbm.connect(tmp_path / "t.db")
    assert [r[0] for r in conn.execute("SELECT id FROM conversations")] == ["claude_code:s2"]


def test_embedder_fingerprint_mismatch_skips_vectors_with_warning(tmp_path):
    settings = make_settings(tmp_path)
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn, tmp_path / "t.db")
    dbm.set_meta(conn, indexer.META_EMBED_FINGERPRINT, "openai_compat|http://old|other-model|dim=768|trunc=8000")
    warnings = []
    fn = indexer.default_embed_fn(conn, settings, warnings)
    assert fn(["a", "b"]) == [None, None]
    assert warnings and "reembed" in warnings[0]


def test_embedder_fingerprint_recorded_on_first_run(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn, tmp_path / "t.db")
    class Emb:
        fingerprint = "fp1"
        def embed(self, texts):
            from convo_index.providers.base import EmbedResult
            return EmbedResult(vectors=[None] * len(texts), fingerprint="fp1", error="provider", detail="down")
    monkeypatch.setattr(indexer.R, "make_embedder", lambda s: Emb())
    warnings = []
    fn = indexer.default_embed_fn(conn, settings, warnings)
    assert dbm.get_meta(conn, indexer.META_EMBED_FINGERPRINT) == "fp1"
    fn(["a"]); fn(["b"])
    assert warnings == ["embeddings: provider: down"]      # reported once


def test_project_root_strips_git_common_dir(monkeypatch):
    class R: returncode = 0; stdout = "/Users/me/repo/.git\n"
    monkeypatch.setattr(indexer.subprocess, "run", lambda *a, **k: R())
    assert indexer.project_root("/Users/me/repo/.claude/worktrees/wt") == "/Users/me/repo"


def test_project_root_falls_back_when_not_git(monkeypatch):
    class R: returncode = 128; stdout = ""
    monkeypatch.setattr(indexer.subprocess, "run", lambda *a, **k: R())
    assert indexer.project_root("/Users/me/notrepo") == "/Users/me/notrepo"


def test_project_root_falls_back_when_git_missing(monkeypatch):
    def boom(*a, **k): raise FileNotFoundError()
    monkeypatch.setattr(indexer.subprocess, "run", boom)
    assert indexer.project_root("/x") == "/x"
    assert indexer.project_root(None) is None


def test_exclude_cwd_purges_already_indexed_sessions(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    projects = tmp_path / "projects"
    _make_file(projects, "s1", "/secret/client", "shh")
    ad = ClaudeCodeAdapter(projects)
    indexer.run([ad], tmp_path / "t.db", settings)
    settings["sources"]["claude_code"]["exclude_cwd"] = ["/secret/*"]      # excluded after the fact
    totals = indexer.run([ad], tmp_path / "t.db", settings)
    assert totals["removed"] == 1
    conn = dbm.connect(tmp_path / "t.db")
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_policy_deny_blocks_remote_embedder_with_warning(tmp_path):
    settings = make_settings(tmp_path)
    settings["privacy"]["hosted_processing"] = "deny"
    settings["embeddings"].update(backend="openai_compat", base_url="https://api.openai.com/v1")
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn, tmp_path / "t.db")
    warnings = []
    fn = indexer.default_embed_fn(conn, settings, warnings)
    assert fn(["a"]) == [None] and "deny" in warnings[0]
    assert dbm.get_meta(conn, indexer.META_EMBED_FINGERPRINT) is None      # nothing recorded


def test_references_land_in_the_same_transaction_as_the_revision(tmp_path, monkeypatch):
    """If storing references fails, the revision must not advance (the next run retries)."""
    from convo_index import indexer, references as REF
    from convo_index import db as dbm
    from convo_index.model import Conversation, Message
    from tests.conftest import make_settings
    st = make_settings(tmp_path)
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn, tmp_path / "t.db")
    conv = Conversation(source="claude_code", native_id="s", title="T", first_prompt="SAAS-1", source_ref="/a", cwd="/x",
                        messages=[Message(seq=0, role="user", text="SAAS-1 please")])
    def boom(c, cv, refs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(REF, "_store_locked", boom)
    with pytest.raises(RuntimeError):
        indexer.index_conversation(conn, conv, "rev1", summarize=False, settings=st)
    assert dbm.get_revision(conn, conv.id) is None and conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    monkeypatch.undo()
    assert indexer.index_conversation(conn, conv, "rev1", summarize=False, settings=st)["references"] == 1
    assert dbm.get_revision(conn, conv.id) == "rev1"
    assert conn.execute("SELECT key FROM references_ WHERE conversation_id=?", (conv.id,)).fetchone()[0] == "SAAS-1"


def test_cwd_excluded_covers_the_project_root_itself(monkeypatch):
    """`~/client/*` must exclude a session started in ~/client, not only its subfolders."""
    monkeypatch.setenv("HOME", "/home/u"); monkeypatch.setenv("USERPROFILE", "/home/u")   # ~ on POSIX and Windows
    for pat in ("~/client/*", "~/client/**", "~/client", "~/client/", "/home/u/client/*"):
        assert indexer.cwd_excluded("/home/u/client", [pat]), pat
        assert indexer.cwd_excluded("/home/u/client/sub/dir", [pat]), pat
        assert not indexer.cwd_excluded("/home/u/client-other", [pat]), pat
        assert not indexer.cwd_excluded("/home/u", [pat]), pat
    assert indexer.cwd_excluded("/work/acme-api", ["*/acme-*"])      # plain globs still work
    for pat in ("/home/u/client/*/", "~/client/*/", "/home/u/./client/*"):   # normalised like before
        assert indexer.cwd_excluded("/home/u/client/sub", [pat]), pat
    assert not indexer.cwd_excluded("", ["*"]) and not indexer.cwd_excluded(None, ["~/client"])
    assert indexer.cwd_excluded("/home/u/[archived]/x", ["~/[archived]"])   # a path wins over glob syntax
    assert indexer.cwd_excluded("/home/u/client/x", "~/client")              # a bare string is one pattern,
    assert not indexer.cwd_excluded("/home/u/other", "~/client")            # not the characters ~, /, c...


def test_exclude_cwd_keeps_sessions_at_the_project_root_out(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    settings["sources"]["claude_code"]["exclude_cwd"] = ["/secret/*"]
    projects = tmp_path / "projects"
    _make_file(projects, "s1", "/secret", "shh")
    _make_file(projects, "s2", "/secretive/proj", "hello")
    indexer.run([ClaudeCodeAdapter(projects)], tmp_path / "t.db", settings)
    conn = dbm.connect(tmp_path / "t.db")
    assert [r[0] for r in conn.execute("SELECT id FROM conversations")] == ["claude_code:s2"]


def test_cwd_excluded_tolerates_a_pattern_for_an_unknown_user():
    assert indexer.cwd_excluded("/home/me/proj", ["~nosuchuser/secret"]) is False   # no RuntimeError
    assert indexer.cwd_excluded("/x/~nosuchuser/secret/sub", ["/x/~nosuchuser/secret"]) is True


def test_archives_are_namespaced_by_source(tmp_path, monkeypatch):
    from convo_index import archive
    from convo_index.model import Conversation, Message
    settings = _quiet(monkeypatch, tmp_path)
    monkeypatch.setattr(indexer, "write_archive_copy", archive.write_archive_copy)   # the real one
    projects = tmp_path / "projects"
    f = _make_file(projects, "shared", "/x", "hello")
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn)
    conv, revision = _load(ClaudeCodeAdapter(projects), "shared")
    indexer.index_conversation(conn, conv, revision, settings=settings)
    other = Conversation(source="codex", native_id="shared", title="T", first_prompt="p", source_ref=str(f), cwd="/x",
                         messages=[Message(seq=0, role="user", text="hello")])
    indexer.index_conversation(conn, other, "r", settings=settings)
    assert len(list(settings.archive_dir().glob("*.jsonl"))) == 2                   # same native id, two copies


def test_run_without_derived_jobs_ingests_only(tmp_path, monkeypatch):
    settings = _quiet(monkeypatch, tmp_path)
    from convo_index import jobs
    calls = []
    monkeypatch.setattr(jobs, "dispatch", lambda *a, **k: calls.append("dispatch") or {})
    monkeypatch.setattr(jobs, "backfill_vectors", lambda *a, **k: calls.append("backfill") or 0)
    projects = tmp_path / "projects"
    _make_file(projects, "s1", "/x", "real content")
    totals = indexer.run([ClaudeCodeAdapter(projects)], tmp_path / "t.db", settings, derived=False)
    assert totals["indexed"] == 1 and totals["jobs"] == {} and calls == []
    indexer.run([ClaudeCodeAdapter(projects)], tmp_path / "t.db", settings)
    assert calls == ["dispatch", "backfill"]


def test_main_no_jobs_flag(monkeypatch):
    seen = []
    monkeypatch.setattr(indexer, "run", lambda derived=True: seen.append(derived) or {"failed": 0})
    assert indexer.main(["--no-jobs"]) == indexer.EXIT_OK and indexer.main([]) == indexer.EXIT_OK
    assert seen == [False, True]


def test_a_handoff_session_is_titled_after_the_original(tmp_path):
    from convo_index import handoff as H
    projects = tmp_path / "projects"
    prompt = H._OPENING["review"].format(harness="Claude Code") + "\n\n## Where and when\n\n- conversation: `x`\n- title: Greeting in app.py\n"
    _make_file(projects, "h6", "/x", prompt, entrypoint="sdk-cli")
    assert _load(ClaudeCodeAdapter(projects), "h6")[0].title == "Second opinion: Greeting in app.py"
