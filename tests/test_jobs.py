from convo_index import db as dbm
from convo_index import jobs
from convo_index.model import Conversation, Message
from convo_index.providers.base import LLMResult
from convo_index.config import EMBED_DIM

GOOD = {"problem": "P", "solution": "S", "is_resolved": "Yes", "learning": "", "claude_failed": "No"}


def _conv(native="s1", texts=("hi", "yo")):
    return Conversation(source="claude_code", native_id=native, title="T", first_prompt=texts[0], source_ref="/a",
                        messages=[Message(seq=i, role="user" if i % 2 == 0 else "assistant", text=t)
                                  for i, t in enumerate(texts)])


def _db(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn, tmp_path / "t.db")
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], None, "r1", lambda t: [None] * len(t))
    return conn


def test_run_summary_ok_then_skipped(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    f = fake_llm(GOOD)
    tr = jobs.transcript_from_db(conn, "claude_code:s1")
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", tr, llm_settings) == "ok"
    assert dbm.get_summary(conn, "claude_code:s1")["problem"] == "P"
    job = jobs.get_job(conn, "claude_code:s1", "summary")
    assert job["status"] == "ok" and job["attempts"] == 1
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", tr, llm_settings) == "skipped"
    assert len(f.calls) == 1


def test_run_summary_failure_retries_until_exhausted_then_resets_on_new_inputs(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    fake_llm(LLMResult(ok=False, error="timeout", detail="t"), LLMResult(ok=False, error="provider", detail="p"),
             LLMResult(ok=False, error="malformed", detail="m"), GOOD, GOOD)
    tr = jobs.transcript_from_db(conn, "claude_code:s1")
    for _ in range(3):
        assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", tr, llm_settings) == "failed"
    job = jobs.get_job(conn, "claude_code:s1", "summary")
    assert job["attempts"] == 3 and job["last_error"].startswith("malformed")
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", tr, llm_settings) == "exhausted"
    # new source revision -> new fingerprint -> attempts reset and it runs again
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r2", tr, llm_settings) == "failed"  # revision r2 not stored
    dbm.upsert_conversation(conn, _conv(), "globex", "path", [], None, "r2", lambda t: [None] * len(t))
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r2", tr, llm_settings) == "ok"
    assert jobs.get_job(conn, "claude_code:s1", "summary")["attempts"] == 1


def test_run_summary_unconfigured_makes_no_call(tmp_path, settings, fake_llm):
    conn = _db(tmp_path)
    f = fake_llm(GOOD)
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", [{"role": "user", "text": "hi"}], settings) == "unconfigured"
    assert f.calls == [] and jobs.get_job(conn, "claude_code:s1", "summary") is None


def test_fingerprint_changes_with_model_and_prompt_version(llm_settings, monkeypatch):
    a = jobs.summary_fingerprint("r1", llm_settings)
    llm_settings["llm"]["summary"]["model"] = "other"
    b = jobs.summary_fingerprint("r1", llm_settings)
    from convo_index import summarize
    monkeypatch.setattr(summarize, "PROMPT_VERSION", 99)
    c = jobs.summary_fingerprint("r1", llm_settings)
    assert len({a, b, c}) == 3


def test_retry_failed_and_missing_are_bounded_and_skip_deleted(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    dbm.upsert_conversation(conn, _conv("s2"), "globex", "path", [], None, "r1", lambda t: [None] * len(t))
    dbm.upsert_conversation(conn, _conv("s3"), "globex", "path", [], None, "r1", lambda t: [None] * len(t))
    fake_llm(LLMResult(ok=False, error="timeout"))
    jobs.run_summary(conn, "claude_code:s1", "T", "r1", jobs.transcript_from_db(conn, "claude_code:s1"), llm_settings)
    dbm.delete_conversation(conn, "claude_code:s3")
    f = fake_llm(GOOD, GOOD, GOOD)
    assert jobs.retry_failed(conn, llm_settings) == {"retried": 1, "ok": 1}
    assert jobs.missing_summaries(conn, llm_settings, limit=10) == {"retried": 1, "ok": 1}   # s2 only; s3 deleted
    assert len(f.calls) == 2
    assert jobs.missing_summaries(conn, llm_settings) == {"retried": 0, "ok": 0}


def test_backfill_vectors_only_text_without_vectors(tmp_path):
    conn = _db(tmp_path)
    from convo_index.model import KIND_TOOL_CALL
    c = _conv("s2", ("a",)); c.messages.append(Message(seq=1, role="assistant", text="[bash] ls", kind=KIND_TOOL_CALL))
    dbm.upsert_conversation(conn, c, "globex", "path", [], None, "r1", lambda t: [None] * len(t))
    seen = []
    def embed_fn(texts):
        seen.extend(texts); return [[0.5] * EMBED_DIM for _ in texts]
    assert jobs.backfill_vectors(conn, embed_fn, limit=2) == 2
    assert jobs.backfill_vectors(conn, embed_fn, limit=10) == 1
    assert sorted(seen) == ["a", "hi", "yo"]                      # never the tool call
    assert jobs.backfill_vectors(conn, embed_fn) == 0
    assert conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0] == 3


def test_seeded_job_without_fingerprint_is_adopted_not_rerun(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    dbm.set_summary(conn, "claude_code:s1", GOOD, "r1")
    conn.execute("INSERT INTO derived_jobs(conversation_id, kind, input_fingerprint, status, attempts) "
                 "VALUES('claude_code:s1','summary',NULL,'ok',1)"); conn.commit()   # as the v3 migration seeds
    f = fake_llm(GOOD)
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", [{"role": "user", "text": "hi"}], llm_settings) == "skipped"
    assert f.calls == []
    assert jobs.get_job(conn, "claude_code:s1", "summary")["fingerprint"] == jobs.summary_fingerprint("r1", llm_settings)


def test_summary_at_current_revision_without_job_row_is_adopted(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    dbm.set_summary(conn, "claude_code:s1", GOOD, "r1")
    f = fake_llm(GOOD)
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", [{"role": "user", "text": "hi"}], llm_settings) == "skipped"
    assert f.calls == [] and jobs.get_job(conn, "claude_code:s1", "summary")["status"] == "ok"


def test_non_retryable_failure_is_exhausted_immediately(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    f = fake_llm(LLMResult(ok=False, error="auth", detail="not logged in"), GOOD)
    tr = jobs.transcript_from_db(conn, "claude_code:s1")
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", tr, llm_settings) == "failed"
    assert jobs.get_job(conn, "claude_code:s1", "summary")["attempts"] == 3
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", tr, llm_settings) == "exhausted"
    assert jobs.retry_failed(conn, llm_settings) == {"retried": 0, "ok": 0}
    assert len(f.calls) == 1


def test_empty_transcript_is_not_retried(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    f = fake_llm(GOOD)
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", [], llm_settings) == "failed"
    assert jobs.get_job(conn, "claude_code:s1", "summary")["attempts"] == 3 and f.calls == []


def test_deleting_conversation_removes_its_jobs(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    fake_llm(LLMResult(ok=False, error="timeout"))
    jobs.run_summary(conn, "claude_code:s1", "T", "r1", jobs.transcript_from_db(conn, "claude_code:s1"), llm_settings)
    assert jobs.failed_jobs(conn)
    dbm.delete_conversation(conn, "claude_code:s1")
    assert jobs.failed_jobs(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM derived_jobs").fetchone()[0] == 0


def test_fingerprint_includes_endpoint_and_limits(llm_settings):
    a = jobs.summary_fingerprint("r1", llm_settings)
    llm_settings["summary"]["head_chars"] = 1
    b = jobs.summary_fingerprint("r1", llm_settings)
    llm_settings["llm"]["summary"]["base_url"] = "https://other/v1"
    c = jobs.summary_fingerprint("r1", llm_settings)
    assert len({a, b, c}) == 3


def test_dispatch_refreshes_stale_successful_summaries_after_model_change(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    fake_llm(GOOD)
    assert jobs.dispatch(conn, llm_settings) == {"summaries": 1, "summaries_ok": 1, "vectors": 0, "vectors_ok": 0, "tags": 0, "tags_ok": 0}
    assert jobs.dispatch(conn, llm_settings)["summaries"] == 0             # current: nothing to do
    llm_settings["llm"]["summary"]["model"] = "better-model"
    f = fake_llm({**GOOD, "problem": "P-new"})
    assert jobs.dispatch(conn, llm_settings)["summaries"] == 1
    assert dbm.get_summary(conn, "claude_code:s1")["problem"] == "P-new" and len(f.calls) == 1


def test_dispatch_attempts_each_conversation_once_per_run_and_respects_budget(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    for i in range(4):
        dbm.upsert_conversation(conn, _conv(f"x{i}"), "globex", "path", [], None, "r1", lambda t: [None] * len(t))
    llm_settings["summary"]["per_run"] = 3
    f = fake_llm(*[LLMResult(ok=False, error="timeout")] * 10)
    attempted = set()
    out = jobs.dispatch(conn, llm_settings, attempted)
    assert out["summaries"] == 3 and len(f.calls) == 3
    out = jobs.dispatch(conn, llm_settings, attempted)                       # same run: no second attempt
    assert out["summaries"] == 2 and len(f.calls) == 5                       # only the 2 not yet attempted


def test_pending_summaries_skips_exhausted_and_migrated_seeds(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    fake_llm(*[LLMResult(ok=False, error="timeout")] * 3)
    tr = jobs.transcript_from_db(conn, "claude_code:s1")
    for _ in range(3):
        jobs.run_summary(conn, "claude_code:s1", "T", "r1", tr, llm_settings)
    assert jobs.pending_summaries(conn, llm_settings, 10) == []              # exhausted for these inputs
    dbm.upsert_conversation(conn, _conv("s2"), "globex", "path", [], None, "r1", lambda t: [None] * len(t))
    dbm.set_summary(conn, "claude_code:s2", GOOD, "r1")
    conn.execute("INSERT INTO derived_jobs(conversation_id, kind, input_fingerprint, status, attempts) "
                 "VALUES('claude_code:s2','summary',NULL,'ok',1)"); conn.commit()
    assert jobs.pending_summaries(conn, llm_settings, 10) == []              # migrated seed at current revision


def test_backfill_skips_messages_deleted_during_embedding(tmp_path):
    conn = _db(tmp_path)
    def embed_then_delete(texts):
        dbm.delete_conversation(conn, "claude_code:s1"); return [[0.5] * EMBED_DIM for _ in texts]
    assert jobs.backfill_vectors(conn, embed_then_delete) == 0
    assert conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0] == 0


def test_auth_failures_block_until_the_backend_becomes_usable(tmp_path, llm_settings, fake_llm, monkeypatch):
    from convo_index.providers import registry as R
    conn = _db(tmp_path)
    f = fake_llm(LLMResult(ok=False, error="auth", detail="not logged in"), GOOD)
    monkeypatch.setattr(R, "usable", lambda spec, settings=None: True)
    tr = jobs.transcript_from_db(conn, "claude_code:s1")
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", tr, llm_settings) == "failed"
    job = jobs.get_job(conn, "claude_code:s1", "summary")
    assert job["status"] == jobs.BLOCKED and job["last_error"].startswith("auth")
    assert jobs.blocked_counts(conn) == {"summary": 1}
    assert jobs.pending_summaries(conn, llm_settings, 10) == []                       # blocked: not queued
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", tr, llm_settings) == "exhausted"
    assert jobs.dispatch(conn, llm_settings)["summaries"] == 0 and len(f.calls) == 1   # same signature: nothing
    # the user logs in / grants consent -> signature changes -> the job is re-queued and succeeds
    monkeypatch.setattr(R, "usability_signature", lambda spec, settings=None: "changed")
    out = jobs.dispatch(conn, llm_settings, attempted=set())
    assert out["unblocked"] == {"summary": 1} and out["summaries_ok"] == 1 and len(f.calls) == 2
    assert jobs.blocked_counts(conn) == {} and dbm.get_summary(conn, "claude_code:s1")["problem"] == "P"


def test_migrated_seed_summaries_are_adopted_and_notice_a_model_change(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    dbm.set_summary(conn, "claude_code:s1", GOOD, "r1")
    conn.execute("INSERT INTO derived_jobs(conversation_id, kind, input_fingerprint, status, attempts, last_run) "
                 "VALUES('claude_code:s1', 'summary', NULL, 'ok', 1, 't')"); conn.commit()
    f = fake_llm(GOOD)
    assert jobs.pending_summaries(conn, llm_settings, 10) == []                       # adopted, no LLM call
    assert jobs.get_job(conn, "claude_code:s1", "summary")["fingerprint"] == jobs.summary_fingerprint("r1", llm_settings)
    llm_settings["llm"]["summary"]["model"] = "other-model"
    assert [c for c, *_ in jobs.pending_summaries(conn, llm_settings, 10)] == ["claude_code:s1"]   # now re-runs
    assert f.calls == []


def test_records_are_not_written_for_deleted_conversations(tmp_path, llm_settings, fake_llm):
    conn = _db(tmp_path)
    dbm.delete_conversation(conn, "claude_code:s1")
    assert jobs._record(conn, "claude_code:s1", "summary", "fp", "failed", "late") is False
    assert jobs._record(conn, "claude_code:nope", "summary", "fp", "failed", "late") is False
    assert conn.execute("SELECT COUNT(*) FROM derived_jobs").fetchone()[0] == 0


def test_relogin_changes_the_signature_and_jobs_unblock_forces(tmp_path, llm_settings, fake_llm, monkeypatch):
    from convo_index.providers import registry as R
    conn = _db(tmp_path)
    f = fake_llm(LLMResult(ok=False, error="auth", detail="expired"), GOOD)
    monkeypatch.setattr(R, "usable", lambda spec, settings=None: True)
    cred = tmp_path / "creds.json"; cred.write_text("old")
    monkeypatch.setattr(R, "CREDENTIAL_FILES", {"claude_cli": (str(cred),)})
    tr = jobs.transcript_from_db(conn, "claude_code:s1")
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r1", tr, llm_settings) == "failed"
    assert jobs.dispatch(conn, llm_settings, attempted=set())["summaries"] == 0             # still blocked
    import os
    import time
    os.utime(cred, (time.time() + 100, time.time() + 100))                                  # `claude login` rewrote the file
    assert jobs.dispatch(conn, llm_settings, attempted=set())["unblocked"] == {"summary": 1} and len(f.calls) == 2
    fake_llm(LLMResult(ok=False, error="auth", detail="expired again"))
    assert jobs.run_summary(conn, "claude_code:s1", "T", "r2", tr, llm_settings) == "failed"
    assert jobs.unblock(conn, llm_settings, force=True) == {"summary": 1}                     # convo-index jobs unblock
