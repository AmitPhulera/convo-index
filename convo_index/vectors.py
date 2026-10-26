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
PER_NEIGHBOUR_EXAMPLES = 2 # few-shot: at most this many decisions from one neighbour
EMBED_BATCH = 32           # texts per embedder call in the dispatcher

# tag still defined, origin still accepts it
_ORIGIN_OK = """oc.id IS NOT NULL AND COALESCE(ost.deleted, 0) = 0 AND COALESCE(ost.hidden, 0) = 0
    AND EXISTS (SELECT 1 FROM tags t WHERE t.group_name = e.group_name AND t.name = e.tag)
    AND EXISTS (SELECT 1 FROM tag_decisions od WHERE od.conversation_id = e.origin_conversation_id
                AND od.group_name = e.group_name AND od.tag = e.tag AND od.decision = 'accept')"""
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
        try:
            reconcile(conn, cid, settings)
        except Exception:
            pass      # suggestions are best-effort; the vector is stored
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


def few_shot_examples(conn, cid, group, k=6, settings=None):
    """Nearest conversations that carry a *user* decision in `group` on a tag that still exists:
    [{title, first_prompt, tag|None, decision}] — accepts as positives, rejects and "none" as
    negatives. The KNN is restricted to decision-bearing conversations, so a sparse handful of
    decisions is found wherever it sits in the corpus. At most PER_NEIGHBOUR_EXAMPLES from one
    conversation. Examples go into a provider prompt, so disabled sources never supply any."""
    if int(k) <= 0:
        return []
    active = {r[0] for r in conn.execute("SELECT name FROM tags WHERE group_name=?", (group,))}
    off = settings.disabled_sources() if settings is not None else ()
    ex = []
    for other, sim in neighbours(conn, cid, k=int(k) * 2, decided_in=group, exclude_sources=off):
        rows = conn.execute(
            "SELECT tag, decision FROM tag_decisions WHERE conversation_id=? AND group_name=? ORDER BY ts DESC",
            (other, group)).fetchall()
        rows = [(t, d) for t, d in rows if t == "" or t in active][:PER_NEIGHBOUR_EXAMPLES]
        if not rows:
            continue
        head = conn.execute("SELECT title, first_prompt FROM conversations WHERE id=?", (other,)).fetchone()
        if not head:
            continue
        for tag, decision in rows:
            ex.append({"title": head[0] or "", "first_prompt": (head[1] or "")[:300],
                       "tag": tag if tag != "" else None, "decision": decision, "similarity": round(sim, 3)})
        if len(ex) >= k:
            break
    return ex[:int(k)]


def _closed(conn, cid, group, tag):
    """Would a suggestion of `tag` on `cid` be closed already (mirror of _OPEN)?"""
    return conn.execute(
        """SELECT 1 FROM tag_decisions d JOIN tag_groups g ON g.name = d.group_name
           WHERE d.conversation_id = ? AND d.group_name = ?
             AND (d.tag = ? OR d.tag = ''
                  OR (g.exclusive = 1 AND d.decision = 'accept'
                      AND EXISTS (SELECT 1 FROM tags t2 WHERE t2.group_name = d.group_name AND t2.name = d.tag)))
           UNION ALL
           SELECT 1 FROM tag_assignments a WHERE a.conversation_id = ? AND a.group_name = ? AND a.tag = ?
           LIMIT 1""", (cid, group, tag, cid, group, tag)).fetchone() is not None


def _tag_exists(conn, group, tag):
    return conn.execute("SELECT 1 FROM tags WHERE group_name=? AND name=?", (group, tag)).fetchone() is not None


def _origin_ok(conn, origin, group, tag):
    """The suggestion's origin still stands: live, visible and still accepting the tag."""
    return conn.execute(
        """SELECT 1 FROM conversations oc LEFT JOIN conversation_state ost ON ost.conversation_id = oc.id
           WHERE oc.id = ? AND COALESCE(ost.deleted, 0) = 0 AND COALESCE(ost.hidden, 0) = 0
             AND EXISTS (SELECT 1 FROM tag_decisions od WHERE od.conversation_id = oc.id
                         AND od.group_name = ? AND od.tag = ? AND od.decision = 'accept')""",
        (origin, group, tag)).fetchone() is not None


def _propose(conn, target, group, tag, origin, sim, definition_revision):
    """Inside a transaction: write or improve one neighbour suggestion. Returns True when a new
    row was written. The target is re-checked here because it may have gone away between the
    KNN and the write."""
    if not dbm.exists(conn, target) or dbm.is_deleted(conn, target) or target in dbm.get_hidden_ids(conn):
        return False
    if _closed(conn, target, group, tag):
        return False
    already = conn.execute(
        "SELECT id, confidence, origin_conversation_id FROM tag_evidence "
        "WHERE conversation_id=? AND group_name=? AND tag=? AND method='neighbour'",
        (target, group, tag)).fetchone()
    if already:
        if (already[1] or 0) < sim or not _origin_ok(conn, already[2], group, tag):
            conn.execute("UPDATE tag_evidence SET confidence=?, detail=?, origin_conversation_id=?, ts=? WHERE id=?",
                         (round(sim, 4), f"similar to {origin}", origin, _now(), already[0]))
        return False
    conn.execute(
        "INSERT INTO tag_evidence(conversation_id, group_name, tag, method, confidence, detail, origin_conversation_id, "
        "definition_revision, ts) VALUES(?,?,?,?,?,?,?,?,?)",
        (target, group, tag, "neighbour", round(sim, 4), f"similar to {origin}", origin, definition_revision, _now()))
    return True


def _threshold(cfg, group):
    return float(cfg["suggest_threshold_by_group"].get(group, cfg["suggest_threshold"]))


def suggest(conn, origin_cid, group, tag, settings=None, definition_revision=None):
    """After the user accepted `tag` on `origin_cid`: propose it for similar conversations.
    Returns the number of suggestions written. Atomic: a failure leaves nothing behind."""
    cfg = learning_settings(settings)
    if not _tag_exists(conn, group, tag):
        return 0
    near = [(o, s) for o, s in neighbours(conn, origin_cid, k=cfg["suggest_k"]) if s >= _threshold(cfg, group)]
    written = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for other, sim in near:
            if _propose(conn, other, group, tag, origin_cid, sim, definition_revision):
                written += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return written


def reconcile(conn, cid, settings=None):
    """A conversation just got (a new) vector: run the suggestion logic it missed. Outward —
    its own accepted tags are proposed to its neighbours — and inward — accepted tags of its
    neighbours above the threshold are proposed to it. Bounded by suggest_k. Returns the count."""
    if not dbm._table_exists(conn, "tag_decisions"):
        return 0
    cfg = learning_settings(settings)
    if cfg["suggest_k"] <= 0:
        return 0
    rev = dbm.get_meta(conn, "tag_definitions_revision")
    written = 0
    for group, tag in conn.execute(
            "SELECT group_name, tag FROM tag_decisions WHERE conversation_id=? AND decision='accept' AND tag != ''", (cid,)).fetchall():
        written += suggest(conn, cid, group, tag, settings, definition_revision=rev)
    near = neighbours(conn, cid, k=cfg["suggest_k"])
    if not near:
        return written
    conn.execute("BEGIN IMMEDIATE")
    try:
        for other, sim in near:
            for group, tag in conn.execute(
                    "SELECT group_name, tag FROM tag_decisions WHERE conversation_id=? AND decision='accept' AND tag != ''",
                    (other,)).fetchall():
                if sim >= _threshold(cfg, group) and _tag_exists(conn, group, tag) and _propose(conn, cid, group, tag, other, sim, rev):
                    written += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return written


def suggestions_for(conn, cid):
    """Open suggestions on one conversation, most similar first."""
    rows = conn.execute(
        f"""SELECT e.group_name, e.tag, e.confidence, e.origin_conversation_id, oc.title
            FROM tag_evidence e
            LEFT JOIN conversations oc ON oc.id = e.origin_conversation_id
            LEFT JOIN conversation_state ost ON ost.conversation_id = e.origin_conversation_id
            WHERE e.conversation_id=? AND e.method='neighbour' AND {_ORIGIN_OK} AND {_OPEN}
            ORDER BY e.confidence DESC""", (cid,)).fetchall()
    return [{"group": g, "tag": t, "similarity": c, "origin": o, "origin_title": title} for g, t, c, o, title in rows]


def open_suggestions(conn, limit=200):
    """All open suggestions on visible conversations, newest first, for the review list."""
    rows = conn.execute(
        f"""SELECT e.conversation_id, c.title, e.group_name, e.tag, e.confidence, e.origin_conversation_id, oc.title, e.ts
            FROM tag_evidence e
            JOIN conversations c ON c.id = e.conversation_id
            LEFT JOIN conversations oc ON oc.id = e.origin_conversation_id
            LEFT JOIN conversation_state st ON st.conversation_id = e.conversation_id
            LEFT JOIN conversation_state ost ON ost.conversation_id = e.origin_conversation_id
            WHERE e.method='neighbour' AND COALESCE(st.deleted, 0) = 0 AND COALESCE(st.hidden, 0) = 0
              AND {_ORIGIN_OK} AND {_OPEN}
            ORDER BY e.ts DESC LIMIT ?""", (max(0, int(limit)),)).fetchall()
    return [{"id": r[0], "title": r[1], "group": r[2], "tag": r[3], "similarity": r[4], "origin": r[5],
             "origin_title": r[6], "ts": r[7]} for r in rows]


def _bucket(sim, step_units):
    """Bucket floor in integer hundredths, so 0.95 lands in [0.95, 1.00) and not in 0.90."""
    units = int(math.floor((sim or 0.0) * 100 + 1e-9))   # 0.95 -> 95 despite 94.999…; 0.9499 -> 94
    return f"{(units // step_units) * step_units / 100:.2f}"


def calibrate(conn, step=0.05):
    """Per group: suggestions that a later decision closed, bucketed by similarity
    ([b, b+step)), with the share accepted, plus the cumulative figures for everything at or
    above the bucket. Accepting another tag in an exclusive group counts as a rejection, as
    does "none of these"."""
    rows = conn.execute(
        """SELECT e.id, e.group_name, e.confidence, e.tag, d.decision, d.tag
           FROM tag_evidence e JOIN tag_decisions d
             ON d.conversation_id = e.conversation_id AND d.group_name = e.group_name
           JOIN tag_groups g ON g.name = e.group_name
           WHERE e.method='neighbour'
             AND (d.tag = e.tag OR d.tag = '' OR (g.exclusive = 1 AND d.decision = 'accept'))""").fetchall()
    verdict = {}   # evidence id -> (group, sim, accepted?)
    for eid, g, sim, etag, decision, dtag in rows:
        acc = decision == "accept" and dtag == etag
        prev = verdict.get(eid)
        verdict[eid] = (g, sim or 0.0, acc or (prev[2] if prev else False))
    step_units = max(1, int(round(step * 100)))
    out = {}
    for g, sim, acc in verdict.values():
        b = out.setdefault(g, {}).setdefault(_bucket(sim, step_units), {"accepted": 0, "rejected": 0})
        b["accepted" if acc else "rejected"] += 1
    for _g, buckets in out.items():
        cum_a = cum_r = 0
        for bkt in sorted(buckets, reverse=True):
            x = buckets[bkt]
            n = x["accepted"] + x["rejected"]
            x["precision"] = round(x["accepted"] / n, 2) if n else None
            cum_a += x["accepted"]; cum_r += x["rejected"]
            x["at_or_above"] = {"accepted": cum_a, "rejected": cum_r, "precision": round(cum_a / (cum_a + cum_r), 2)}
    return out
