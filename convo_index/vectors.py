"""Conversation vectors, few-shot examples and neighbour suggestions (docs/design.md §8).

One vector per conversation, embedded from title + first prompt + summary (falling back to
the last assistant text when no summary exists yet), stored in `vec_conversations` through
an integer id map. Vectors are L2-normalized on write so sqlite-vec's L2 distance maps to
cosine similarity exactly (1 - d²/2); zero or non-finite vectors are embedding failures.
Three uses:

  neighbours(cid)            cosine-nearest *eligible* conversations (hidden/deleted excluded
                             inside the KNN through a rowid subquery, so k is exact)
  few_shot_examples(cid, g)  the user's nearest *decisions* in a group (accepts and rejects):
                             the KNN is restricted to conversations that carry a decision
  suggest(cid, group, tag)   after a manual accept: conversations above the group's
                             suggest_threshold that have no closing decision in that group
                             and do not already carry the tag get a `neighbour` evidence row
                             with the origin conversation and the similarity. Nothing is
                             auto-applied; suggestions never chain. reconcile() runs the same
                             logic when a conversation gets its vector later (both directions).

A suggestion is *open* while: the tag still exists, the origin still accepts it, and no
decision closes it (same tag, group "none", or — exclusive groups — an accept of another tag
that still exists). Replacing a conversation's vector drops its open suggestions (their
similarity is stale); re-embedding drops all neighbour evidence (a new vector space).

Staleness: db.py marks `vector` jobs `stale` whenever the conversation row or its summary is
rewritten (also failed/exhausted ones, so changed inputs revive them). store_vector()
re-checks the text fingerprint under the write lock and discards an embedding of old text.
"""
from __future__ import annotations

import hashlib
import math
import time

import sqlite_vec

from convo_index import db as dbm
from convo_index import settings as S
from convo_index.model import KIND_TEXT

VECTOR = "vector"          # derived_jobs kind
TEXT_LIMIT = 6000          # chars of the conversation text that gets embedded
EMBED_BATCH = 32           # texts per embedder call in the dispatcher

# no decision closes it, tag not already assigned
_OPEN = """NOT EXISTS (SELECT 1 FROM tag_decisions d JOIN tag_groups g ON g.name = d.group_name
                       WHERE d.conversation_id = e.conversation_id AND d.group_name = e.group_name
                         AND (d.tag = e.tag OR d.tag = ''
                              OR (g.exclusive = 1 AND d.decision = 'accept'
                                  AND EXISTS (SELECT 1 FROM tags t2 WHERE t2.group_name = d.group_name AND t2.name = d.tag))))
           AND NOT EXISTS (SELECT 1 FROM tag_assignments a WHERE a.conversation_id = e.conversation_id
                           AND a.group_name = e.group_name AND a.tag = e.tag)"""


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def learning_settings(settings=None):
    """[tags_learning] with types and ranges enforced: counts are non-negative ints (0 disables),
    thresholds are finite and clamped to [-1, 1]. Bad values fall back to the defaults."""
    settings = settings or S.load()
    raw = settings.get("tags_learning") or {}
    dflt = S.DEFAULTS["tags_learning"]

    def count(key):
        try:
            return max(0, int(raw.get(key, dflt[key])))
        except (TypeError, ValueError):
            return int(dflt[key])

    def thresh(v, fallback):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return fallback
        return max(-1.0, min(1.0, f)) if math.isfinite(f) else fallback

    base = thresh(raw.get("suggest_threshold"), float(dflt["suggest_threshold"]))
    per_group = {g: thresh(v, base) for g, v in (raw.get("suggest_threshold_by_group") or {}).items()} \
        if isinstance(raw.get("suggest_threshold_by_group"), dict) else {}
    return {"few_shot_k": count("few_shot_k"), "suggest_k": count("suggest_k"), "vectors_per_run": count("vectors_per_run"),
            "suggest_threshold": base, "suggest_threshold_by_group": per_group}


def conversation_text(conn, cid):
    """What the conversation vector represents. Deterministic so the fingerprint is stable."""
    row = conn.execute("SELECT title, first_prompt FROM conversations WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    parts = [row[0] or "", row[1] or ""]
    summary = dbm.get_summary(conn, cid)
    if summary and (summary.get("problem") or summary.get("solution")):
        parts += [summary.get("problem") or "", summary.get("solution") or ""]
    else:
        last = conn.execute(
            "SELECT text FROM messages WHERE conversation_id=? AND role='assistant' AND kind=? ORDER BY seq DESC LIMIT 1",
            (cid, KIND_TEXT)).fetchone()
        if last:
            parts.append(last[0][:1500])
    return "\n".join(p for p in parts if p)[:TEXT_LIMIT]


def fingerprint(text, embedder_fp):
    return f"{embedder_fp}|{hashlib.sha1((text or '').encode('utf-8', 'surrogatepass'), usedforsecurity=False).hexdigest()[:16]}"


def normalize(vector):
    """Unit-length copy, or None for a vector that cannot be a direction (empty, zero, NaN or
    infinite coordinates). Scaled by the largest coordinate first so huge values do not
    overflow when squared."""
    try:
        vals = [float(x) for x in vector]
    except (TypeError, ValueError):
        return None
    if not vals or any(not math.isfinite(x) for x in vals):
        return None
    m = max(abs(x) for x in vals)
    if m == 0.0:
        return None
    scaled = [x / m for x in vals]
    n = math.sqrt(sum(x * x for x in scaled))
    return [x / n for x in scaled]


def _vid(conn, cid, create=True):
    row = conn.execute("SELECT vid FROM conv_vec_ids WHERE conversation_id=?", (cid,)).fetchone()
    if row:
        return row[0]
    if not create:
        return None
    cur = conn.execute("INSERT INTO conv_vec_ids(conversation_id) VALUES(?)", (cid,))
    return cur.lastrowid


def _drop_open_suggestions_for(conn, cid):
    """The conversation's vector changed: similarities computed against the old one are stale,
    whether it was the target or the origin. Decided suggestions stay (calibration history)."""
    conn.execute(f"""DELETE FROM tag_evidence WHERE id IN (
        SELECT e.id FROM tag_evidence e WHERE e.method = 'neighbour'
          AND (e.conversation_id = ? OR e.origin_conversation_id = ?) AND {_OPEN})""", (cid, cid))


def store_vector(conn, cid, vector, fp=None, embedder_fp=None, reset=True):
    """Replace the conversation's vector (normalized) and, when `fp` is given, record the
    vector job as ok in the same transaction. Returns 'ok', or 'missing' (conversation gone),
    'stale' (the text changed since `fp` was computed: result discarded, job untouched) or
    'invalid' (not a usable vector: recorded as a failed job)."""
    unit = normalize(vector)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not dbm.exists(conn, cid) or dbm.is_deleted(conn, cid):
            conn.execute("ROLLBACK")
            return "missing"
        if fp is not None and embedder_fp is not None and fingerprint(conversation_text(conn, cid), embedder_fp) != fp:
            conn.execute("ROLLBACK")          # embedded old text while the row/summary moved on
            return "stale"
        from convo_index import jobs
        if unit is None:
            if fp is not None:
                jobs._record(conn, cid, VECTOR, fp, "failed", "embedder returned an unusable vector (zero/NaN/inf)",
                             reset=reset, commit=False)
            conn.execute("COMMIT")
            return "invalid"
        vid = _vid(conn, cid)
        conn.execute("DELETE FROM vec_conversations WHERE rowid=?", (vid,))
        conn.execute("INSERT INTO vec_conversations(rowid, embedding) VALUES(?, ?)", (vid, sqlite_vec.serialize_float32(unit)))
        if fp is not None:
            jobs._record(conn, cid, VECTOR, fp, "ok", None, reset=reset, commit=False)
        if dbm._table_exists(conn, "tag_evidence"):
            _drop_open_suggestions_for(conn, cid)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "ok"


def _plan(conn, cid, embedder_fp):
    """(text, fp, job, new_inputs) or None when the conversation is gone."""
    from convo_index import jobs
    text = conversation_text(conn, cid)
    if text is None:
        return None
    fp = fingerprint(text, embedder_fp)
    job = jobs.get_job(conn, cid, VECTOR)
    return text, fp, job, job is None or job["fingerprint"] != fp


def _current(conn, cid, fp, job):
    """The stored vector is for exactly this text: nothing to embed. A stale-marked row whose
    text did not change is flipped back to ok."""
    from convo_index import jobs
    if job and job["fingerprint"] == fp and job["status"] in ("ok", "stale") and _vid(conn, cid, create=False) is not None:
        if job["status"] == "stale":
            jobs._record(conn, cid, VECTOR, fp, "ok", None, reset=True)
        return True
    return False


def _finish(conn, cid, vec, fp, embedder_fp, new_inputs, settings):
    """Store one embedding result and run the suggestion reconciliation. Returns the status."""
    from convo_index import jobs
    if vec is None:
        jobs._record(conn, cid, VECTOR, fp, "failed", "embedder returned no vector", reset=new_inputs)
        return "failed"
    status = store_vector(conn, cid, vec, fp=fp, embedder_fp=embedder_fp, reset=new_inputs)
    if status == "ok":
        return "ok"
    if status == "stale":
        return "skipped"    # still pending: the next run embeds the new text
    return "failed"


def run_vector(conn, cid, embed_fn, embedder_fp, settings=None):
    """Embed one conversation if its inputs changed. 'ok' | 'skipped' | 'failed'. Provider or
    storage errors are recorded as a failed job, never raised into the dispatcher."""
    from convo_index import jobs
    plan = _plan(conn, cid, embedder_fp)
    if plan is None:
        return "failed"
    text, fp, job, new_inputs = plan
    if _current(conn, cid, fp, job):
        return "skipped"
    try:
        vec = embed_fn([text])[0]
        return _finish(conn, cid, vec, fp, embedder_fp, new_inputs, settings)
    except Exception as e:   # provider exception, malformed result, storage failure
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        jobs._record(conn, cid, VECTOR, fp, "failed", f"{type(e).__name__}: {e}"[:500], reset=new_inputs)
        return "failed"


def run_vectors(conn, cids, embed_fn, embedder_fp, settings=None, batch=EMBED_BATCH):
    """Embed many conversations with batched provider calls. Returns
    {"attempted", "ok", "failed", "skipped"}; one failing batch is recorded per conversation
    and the remaining batches still run."""
    from convo_index import jobs
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0}
    todo = []
    for cid in cids:
        plan = _plan(conn, cid, embedder_fp)
        if plan is None:
            out["attempted"] += 1; out["failed"] += 1
            continue
        text, fp, job, new_inputs = plan
        if _current(conn, cid, fp, job):
            out["skipped"] += 1
            continue
        out["attempted"] += 1
        todo.append((cid, text, fp, new_inputs))
    for i in range(0, len(todo), max(1, int(batch))):
        chunk = todo[i:i + max(1, int(batch))]
        try:
            vecs = list(embed_fn([t for _, t, _, _ in chunk]))
            if len(vecs) != len(chunk):
                raise ValueError(f"embedder returned {len(vecs)} vectors for {len(chunk)} texts")
        except Exception as e:
            for cid, _, fp, new_inputs in chunk:
                jobs._record(conn, cid, VECTOR, fp, "failed", f"{type(e).__name__}: {e}"[:500], reset=new_inputs)
            out["failed"] += len(chunk)
            continue
        for (cid, _, fp, new_inputs), vec in zip(chunk, vecs, strict=False):
            try:
                status = _finish(conn, cid, vec, fp, embedder_fp, new_inputs, settings)
            except Exception as e:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                jobs._record(conn, cid, VECTOR, fp, "failed", f"{type(e).__name__}: {e}"[:500], reset=new_inputs)
                status = "failed"
            out[status] += 1
    return out


def pending_vectors(conn, embedder_fp, limit, max_attempts=3, settings=None):
    """Conversations whose vector job needs a run, found in SQL: no job, marked stale, failed
    but not exhausted, or made by a different embedder. Never-embedded first, then newest.
    A non-positive limit means "none this run"."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return []
    from convo_index.jobs import source_filter
    sf, sp = source_filter(settings)
    rows = conn.execute(
        f"""SELECT c.id FROM conversations c
           LEFT JOIN derived_jobs j ON j.conversation_id = c.id AND j.kind = ?
           LEFT JOIN conversation_state st ON st.conversation_id = c.id
           WHERE COALESCE(st.deleted, 0) = 0{sf}
             AND (j.conversation_id IS NULL OR j.status = 'stale'
                  OR (j.status = 'failed' AND j.attempts < ?)
                  OR j.input_fingerprint IS NULL OR substr(j.input_fingerprint, 1, length(?)) != ?)
           ORDER BY (j.conversation_id IS NULL) DESC, (j.status = 'failed') DESC, c.start_ts DESC LIMIT ?""",
        (VECTOR, *sp, max(0, int(max_attempts)), embedder_fp + "|", embedder_fp + "|", limit)).fetchall()
    return [r[0] for r in rows]


def _cosine_from_l2(d):
    # Stored vectors are unit length: ||a-b||^2 = 2 - 2cos  ->  cos = 1 - d^2/2 (clamped for safety).
    return max(-1.0, min(1.0, 1.0 - (d * d) / 2.0))


def neighbours(conn, cid, k=10, exclude_hidden=True, decided_in=None, exclude_sources=()):
    """[(other_cid, similarity)] by cosine on the stored conversation vectors, nearest first.
    Eligibility (not hidden/deleted, not from an excluded source, and with `decided_in`:
    carrying a user decision in that group) is a rowid subquery *inside* the KNN, so exactly
    k eligible rows come back."""
    try:
        k = int(k)
    except (TypeError, ValueError):
        return []
    if k <= 0:
        return []
    vid = _vid(conn, cid, create=False)
    if vid is None:
        return []
    row = conn.execute("SELECT embedding FROM vec_conversations WHERE rowid=?", (vid,)).fetchone()
    if not row:
        return []
    eligible = """SELECT m.vid FROM conv_vec_ids m
                  LEFT JOIN conversation_state st ON st.conversation_id = m.conversation_id
                  WHERE m.vid != ? AND COALESCE(st.deleted, 0) = 0 AND (? = 0 OR COALESCE(st.hidden, 0) = 0)"""
    params = [vid, 1 if exclude_hidden else 0]
    if decided_in is not None:
        eligible += " AND EXISTS (SELECT 1 FROM tag_decisions d WHERE d.conversation_id = m.conversation_id AND d.group_name = ?)"
        params.append(decided_in)
    if exclude_sources:
        eligible += (" AND m.conversation_id NOT IN (SELECT id FROM conversations WHERE source IN ("
                     + ",".join("?" * len(exclude_sources)) + "))")
        params.extend(exclude_sources)
    rows = conn.execute(
        f"""SELECT v.rowid, v.distance FROM vec_conversations v
            WHERE v.embedding MATCH ? AND k = ? AND v.rowid IN ({eligible}) ORDER BY v.distance""",
        (row[0], k, *params)).fetchall()
    if not rows:
        return []
    ids = dict(conn.execute(
        f"SELECT vid, conversation_id FROM conv_vec_ids WHERE vid IN ({','.join('?' * len(rows))})",
        [rid for rid, _ in rows]).fetchall())
    return [(ids[rid], _cosine_from_l2(dist)) for rid, dist in rows if rid in ids]
