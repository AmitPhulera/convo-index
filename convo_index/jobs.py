"""Derived jobs: LLM- or embedder-produced data with their own retry state.

Ingestion records the source revision; derived work records, per (conversation, kind), the
fingerprint of the inputs it consumed plus status / attempts / last_error. A failed
provider call is therefore retried on later runs without the source having to change,
and never more than `max_attempts` times for the same inputs. A fingerprint change
(new transcript, new prompt version, new backend/model) resets the attempts.

Kinds: `summary`. Per-message vectors for search are not
jobs: `backfill_vectors` fills them against the embedder fingerprint in index_meta.
"""
from __future__ import annotations

import time

from convo_index import db as dbm
from convo_index import settings as S
from convo_index.providers import registry as R
from convo_index import summarize as SM
from convo_index.model import KIND_TEXT

SUMMARY = "summary"
TAGS = "tags"
NON_RETRYABLE = {"auth", "config"}   # retrying cannot help until the user changes something
BLOCKED = "blocked"                  # job status for those: kept out of the queue until the
                                     # backend's usability signature changes (see unblock())


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def source_filter(settings, alias="c"):
    """SQL (and its parameters) keeping conversations of disabled sources out of derived work:
    `enabled = false` means nothing of theirs leaves the machine, stored content included."""
    off = settings.disabled_sources() if settings is not None else []
    return (f" AND {alias}.source NOT IN ({','.join('?' * len(off))})" if off else ""), tuple(off)


def summary_fingerprint(revision, settings):
    """Everything that changes the request: transcript revision, prompt version, backend,
    model, endpoint (non-secret) and the head/tail truncation limits."""
    spec = settings.llm_job(SUMMARY)
    sm = settings["summary"]
    return (f"{revision}|p{SM.PROMPT_VERSION}|{spec['backend']}|{spec['model']}|{spec.get('base_url') or ''}"
            f"|h{sm['head_chars']}t{sm['tail_chars']}")


def get_job(conn, cid, kind):
    row = conn.execute(
        "SELECT input_fingerprint, status, attempts, last_error, last_run FROM derived_jobs "
        "WHERE conversation_id=? AND kind=?", (cid, kind)).fetchone()
    if not row:
        return None
    return dict(zip(("fingerprint", "status", "attempts", "last_error", "last_run"), row, strict=False))


def _record(conn, cid, kind, fingerprint, status, error=None, reset=False, exhaust_at=None, commit=True):
    """Upsert a job row. `reset` starts the attempt count over (new inputs); `exhaust_at`
    pins attempts to max_attempts so a non-retryable failure is not retried blindly."""
    attempts_new = exhaust_at if exhaust_at is not None else 1
    if not dbm.exists(conn, cid) or dbm.is_deleted(conn, cid):
        return False     # the conversation went away while the provider was working: no orphan row
    conn.execute(
        """INSERT INTO derived_jobs(conversation_id, kind, input_fingerprint, status, attempts, last_error, last_run)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(conversation_id, kind) DO UPDATE SET
             input_fingerprint=excluded.input_fingerprint, status=excluded.status,
             attempts=CASE WHEN ? IS NOT NULL THEN ? WHEN ? THEN 1 ELSE derived_jobs.attempts + 1 END,
             last_error=excluded.last_error, last_run=excluded.last_run""",
        (cid, kind, fingerprint, status, attempts_new, error, _now(),
         exhaust_at, exhaust_at, 1 if reset else 0))
    if commit:
        conn.commit()
    return True


def _record_failure(conn, cid, kind, fp, res, reset, max_attempts, settings):
    """Failed provider call: transient errors count an attempt; auth/config errors block the
    job and remember the backend's usability signature, so it is retried once the user
    changed something (consent, login, key) and not before."""
    err = f"{res.error}: {res.detail or ''}"[:500] if res else "no result"
    if res is not None and res.error in NON_RETRYABLE:
        job = "summary" if kind == SUMMARY else "classify"
        dbm.set_meta(conn, f"blocked_signature:{kind}", R.usability_signature(settings.llm_job(job), settings))
        _record(conn, cid, kind, fp, BLOCKED, err, exhaust_at=max_attempts)
    else:
        _record(conn, cid, kind, fp, "failed", err, reset=reset)


def unblock(conn, settings, force=False):
    """Re-queue blocked jobs of a kind when the backend's usability signature differs from
    the one recorded when they were blocked (the user granted consent, logged in — the CLI's
    credential file changed —, set a key or changed backend/model), or unconditionally with
    `force` (`convo-index jobs unblock`). Returns {kind: rows re-queued}."""
    out = {}
    for kind, job in ((SUMMARY, "summary"), (TAGS, "classify")):
        stored = dbm.get_meta(conn, f"blocked_signature:{kind}")
        if stored is None and not force:
            continue
        spec = settings.llm_job(job)
        if not force and (R.usability_signature(spec, settings) == stored or not R.usable(spec, settings)):
            continue
        n = conn.execute("UPDATE derived_jobs SET status='failed', attempts=0 WHERE kind=? AND status=?",
                         (kind, BLOCKED)).rowcount
        conn.execute("DELETE FROM index_meta WHERE key=?", (f"blocked_signature:{kind}",))
        conn.commit()
        if n:
            out[kind] = n
    return out


def transcript_from_db(conn, cid):
    rows = conn.execute(
        """SELECT role, text FROM messages WHERE conversation_id=? AND kind=? AND role IN ('user','assistant')
           ORDER BY seq""", (cid, KIND_TEXT)).fetchall()
    return [{"role": r[0], "text": r[1]} for r in rows]


def run_summary(conn, cid, title, revision, transcript, settings=None):
    """Run (or skip) the summary job for one conversation. Returns one of
    'ok' | 'skipped' | 'failed' | 'exhausted' | 'unconfigured'."""
    settings = settings or S.load()
    max_attempts = int(settings["summary"]["max_attempts"])
    fp = summary_fingerprint(revision, settings)
    job = get_job(conn, cid, SUMMARY)
    # A summary already made from this exact revision whose job row has no fingerprint (seeded
    # by the v3 migration) or no job row at all (written by an older path) is adopted as
    # current instead of paying for the LLM call again. A row *with* a different fingerprint
    # means the model/prompt/limits changed: that one does re-run.
    if dbm.get_summarized_revision(conn, cid) == revision and (job is None or job["fingerprint"] is None):
        _record(conn, cid, SUMMARY, fp, "ok", None, reset=True)
        return "skipped"
    same_inputs = job is not None and job["fingerprint"] == fp
    if same_inputs and job["status"] == "ok":
        return "skipped"
    if same_inputs and (job["attempts"] >= max_attempts or job["status"] == BLOCKED):
        return "exhausted"
    if not settings.llm_job(SUMMARY)["backend"]:
        return "unconfigured"
    if not transcript:
        _record(conn, cid, SUMMARY, fp, "failed", "empty transcript (no text turns)", exhaust_at=max_attempts)
        return "failed"
    summary, res = SM.summarize_conversation(transcript, title, settings)
    if summary is None:
        _record_failure(conn, cid, SUMMARY, fp, res, not same_inputs, max_attempts, settings)
        return "failed"
    if not dbm.set_summary_if_current(conn, cid, summary, revision):
        _record(conn, cid, SUMMARY, fp, "failed", "conversation changed or deleted during summarization",
                reset=not same_inputs)
        return "failed"
    _record(conn, cid, SUMMARY, fp, "ok", None, reset=True)
    return "ok"


def pending_summaries(conn, settings, limit):
    """Conversations whose summary job needs a run, in priority order: never summarized,
    failed but not exhausted, or successful with inputs that no longer match (new model,
    prompt version, limits, or a source revision the summary predates). Excludes deleted."""
    sf, sp = source_filter(settings)
    rows = conn.execute(
        f"""SELECT c.id, c.title, c.source_revision, j.input_fingerprint, j.status, j.attempts, s.summarized_revision
           FROM conversations c
           LEFT JOIN derived_jobs j ON j.conversation_id = c.id AND j.kind = ?
           LEFT JOIN conversation_summary s ON s.conversation_id = c.id
           LEFT JOIN conversation_state st ON st.conversation_id = c.id
           WHERE COALESCE(st.deleted, 0) = 0{sf}
           ORDER BY (j.conversation_id IS NULL) DESC, (j.status = 'failed') DESC, c.start_ts DESC""",
        (SUMMARY, *sp)).fetchall()
    max_attempts = int(settings["summary"]["max_attempts"])
    out = []
    for cid, title, revision, fp_stored, status, attempts, srev in rows:
        fp = summary_fingerprint(revision, settings)
        if status == "ok" and fp_stored == fp:
            continue
        if status == "ok" and fp_stored is None and srev == revision:
            _record(conn, cid, SUMMARY, fp, "ok", None, reset=True)   # migrated seed: adopt now, no LLM call,
            continue                                                  # so a later model change is noticed
        if status == BLOCKED:
            continue   # waits for unblock()
        if status == "failed" and fp_stored == fp and (attempts or 0) >= max_attempts:
            continue   # exhausted for these inputs
        out.append((cid, title, revision))
        if len(out) >= limit:
            break
    return out


def dispatch(conn, settings=None, attempted=None, embed_fn=None, embedder_fp=None):
    """One bounded pass of derived work per index run. Each conversation/kind is attempted
    at most once per run (`attempted` is the run's set). Returns counts."""
    settings = settings or S.load()
    attempted = attempted if attempted is not None else set()
    budget = int(settings["summary"].get("per_run") or 50)
    out = {"summaries": 0, "summaries_ok": 0, "vectors": 0, "vectors_ok": 0}
    unblocked = unblock(conn, settings)
    if unblocked:
        out["unblocked"] = unblocked
    if settings.llm_job(SUMMARY)["backend"]:
        for cid, title, revision in pending_summaries(conn, settings, budget):
            if (cid, SUMMARY) in attempted:
                continue
            attempted.add((cid, SUMMARY))
            out["summaries"] += 1
            if run_summary(conn, cid, title, revision, transcript_from_db(conn, cid), settings) == "ok":
                out["summaries_ok"] += 1
    out.update({"tags": 0, "tags_ok": 0})
    return out


def retry_failed(conn, settings=None, limit=25):
    """Re-run failed summary jobs whose attempts are not exhausted. Bounded per run."""
    settings = settings or S.load()
    if not settings.llm_job(SUMMARY)["backend"]:
        return {"retried": 0, "ok": 0}
    sf, sp = source_filter(settings)
    rows = conn.execute(
        f"""SELECT j.conversation_id, c.title, c.source_revision
           FROM derived_jobs j JOIN conversations c ON c.id = j.conversation_id
           LEFT JOIN conversation_state s ON s.conversation_id = c.id
           WHERE j.kind=? AND j.status='failed' AND j.attempts < ? AND COALESCE(s.deleted, 0) = 0{sf}
           ORDER BY j.last_run LIMIT ?""",
        (SUMMARY, int(settings["summary"]["max_attempts"]), *sp, limit)).fetchall()
    out = {"retried": 0, "ok": 0}
    for cid, title, revision in rows:
        out["retried"] += 1
        if run_summary(conn, cid, title, revision, transcript_from_db(conn, cid), settings) == "ok":
            out["ok"] += 1
    return out


def missing_summaries(conn, settings=None, limit=25):
    """Conversations that have never had a summary job (e.g. indexed while no backend was
    configured). Bounded per run so a first run with hundreds of sessions stays responsive."""
    settings = settings or S.load()
    if not settings.llm_job(SUMMARY)["backend"]:
        return {"retried": 0, "ok": 0}
    sf, sp = source_filter(settings)
    rows = conn.execute(
        f"""SELECT c.id, c.title, c.source_revision FROM conversations c
           LEFT JOIN derived_jobs j ON j.conversation_id = c.id AND j.kind = ?
           LEFT JOIN conversation_summary s ON s.conversation_id = c.id
           LEFT JOIN conversation_state st ON st.conversation_id = c.id
           WHERE j.conversation_id IS NULL AND s.conversation_id IS NULL AND COALESCE(st.deleted, 0) = 0{sf}
           ORDER BY c.start_ts DESC LIMIT ?""", (SUMMARY, *sp, limit)).fetchall()
    out = {"retried": 0, "ok": 0}
    for cid, title, revision in rows:
        out["retried"] += 1
        if run_summary(conn, cid, title, revision, transcript_from_db(conn, cid), settings) == "ok":
            out["ok"] += 1
    return out


def backfill_vectors(conn, embed_fn, limit=500, settings=None):
    """Embed text messages that have no vector yet (past embedder outages, a `reembed`, or
    an install that only later got an embedder). Bounded per run."""
    sf, sp = source_filter(settings)
    rows = conn.execute(
        f"""SELECT m.id, m.text FROM messages m JOIN conversations c ON c.id = m.conversation_id
           LEFT JOIN vec_messages v ON v.rowid = m.id
           WHERE m.kind = ? AND v.rowid IS NULL{sf} ORDER BY m.id LIMIT ?""", (KIND_TEXT, *sp, limit)).fetchall()
    if not rows:
        return 0
    vectors = embed_fn([r[1] for r in rows])
    done = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        import sqlite_vec
        for (mid, text), v in zip(rows, vectors, strict=False):
            if v is None:
                continue
            # Embedding was slow; the message may have been deleted or rewritten meanwhile.
            cur = conn.execute("SELECT role, kind, text FROM messages WHERE id=?", (mid,)).fetchone()
            if cur is None or cur[1] != KIND_TEXT or cur[2] != text:
                continue
            conn.execute("DELETE FROM vec_messages WHERE rowid=?", (mid,))
            conn.execute("INSERT INTO vec_messages(rowid, embedding) VALUES(?,?)",
                         (mid, sqlite_vec.serialize_float32(v)))
            done += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return done


def failed_jobs(conn, limit=20, status="failed"):
    """Failed (or, with status=BLOCKED, blocked) jobs for conversations that still exist and
    are not deleted."""
    return conn.execute(
        """SELECT j.conversation_id, j.kind, j.attempts, j.last_error, j.last_run FROM derived_jobs j
           JOIN conversations c ON c.id = j.conversation_id
           LEFT JOIN conversation_state st ON st.conversation_id = c.id
           WHERE j.status=? AND COALESCE(st.deleted, 0) = 0
           ORDER BY j.last_run DESC LIMIT ?""", (status, limit)).fetchall()


def blocked_counts(conn):
    """{kind: n} of jobs blocked on auth/config until the backend becomes usable."""
    return dict(conn.execute("SELECT kind, COUNT(*) FROM derived_jobs WHERE status=? GROUP BY kind", (BLOCKED,)).fetchall())
