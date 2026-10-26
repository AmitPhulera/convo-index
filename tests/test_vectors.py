"""Conversation vectors, few-shot examples, neighbour suggestions and calibration (docs/design.md §8)."""
import math

from convo_index import db as dbm
from convo_index import jobs
from convo_index import tags as T
from convo_index import vectors as V
from convo_index.config import EMBED_DIM
from convo_index.model import Conversation, Message
from tests.conftest import make_settings

FP = "test-embedder|dim=768"


def _unit(*coords):
    """A deterministic unit vector: first coordinates given, rest zero."""
    v = [0.0] * EMBED_DIM
    for i, c in enumerate(coords):
        v[i] = c
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


# Text -> vector: conversations mentioning "kubernetes" cluster together, "astrology" elsewhere.
def fake_embed(texts):
    out = []
    for t in texts:
        t = (t or "").lower()
        if "kubernetes" in t:
            out.append(_unit(1.0, 0.1 if "deploy" in t else 0.0))
        elif "astrology" in t:
            out.append(_unit(0.0, 1.0))
        else:
            out.append(_unit(0.0, 0.0, 1.0))
    return out


def _conv(native, text, cwd="/nowhere"):
    return Conversation(source="claude_code", native_id=native, title=text[:30], first_prompt=text, source_ref="/a",
                        cwd=cwd, start_ts=f"2026-06-{int(native[1:]) + 1:02d}T00:00:00Z",
                        messages=[Message(seq=0, role="user", text=text)])


def _db(tmp_path, settings=None):
    settings = settings or make_settings(tmp_path)
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn, tmp_path / "t.db")
    T.sync_definitions(conn, settings)
    return conn, settings


def _seed(conn, texts):
    for i, txt in enumerate(texts):
        c = _conv(f"s{i}", txt)
        dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    for cid in [f"claude_code:s{i}" for i in range(len(texts))]:
        assert V.run_vector(conn, cid, fake_embed, FP) == "ok"


def test_conversation_text_prefers_summary_then_last_assistant(tmp_path):
    conn, s = _db(tmp_path)
    c = _conv("s0", "kubernetes deploy failing"); c.messages.append(Message(seq=1, role="assistant", text="try rollout restart"))
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    assert "try rollout restart" in V.conversation_text(conn, c.id)
    dbm.set_summary(conn, c.id, {"problem": "pods crash", "solution": "restart"}, "1")
    txt = V.conversation_text(conn, c.id)
    assert "pods crash" in txt and "try rollout restart" not in txt
    assert V.conversation_text(conn, "claude_code:nope") is None


def test_run_vector_is_stable_and_refreshes_when_inputs_change(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes deploy failing"])
    cid = "claude_code:s0"
    calls = []
    def spy(texts):
        calls.append(texts); return fake_embed(texts)
    assert V.run_vector(conn, cid, spy, FP) == "skipped" and calls == []
    dbm.set_summary(conn, cid, {"problem": "pods crash", "solution": "restart"}, "1")
    assert V.run_vector(conn, cid, spy, FP) == "ok" and len(calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM vec_conversations").fetchone()[0] == 1   # replaced, not duplicated
    assert V.run_vector(conn, cid, lambda t: [None], FP + "x") == "failed"
    assert jobs.get_job(conn, cid, V.VECTOR)["status"] == "failed"


def test_pending_vectors_selects_missing_and_stale(tmp_path):
    conn, s = _db(tmp_path)
    for i, txt in enumerate(["kubernetes a", "astrology b"]):
        dbm.upsert_conversation(conn, _conv(f"s{i}", txt), "unknown", None, [], None, "1", lambda t: [None] * len(t))
    assert sorted(V.pending_vectors(conn, FP, 10)) == ["claude_code:s0", "claude_code:s1"]
    V.run_vector(conn, "claude_code:s0", fake_embed, FP)
    assert V.pending_vectors(conn, FP, 10) == ["claude_code:s1"]
    V.run_vector(conn, "claude_code:s1", fake_embed, FP)
    assert V.pending_vectors(conn, FP, 10) == []
    assert V.pending_vectors(conn, "other-embedder", 10) != []             # a new embedder needs everything again


def test_neighbours_by_cosine_and_hidden_excluded(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes deploy failing", "kubernetes cluster upgrade", "astrology chart", "shopping list"])
    nb = V.neighbours(conn, "claude_code:s0", k=3)
    assert nb[0][0] == "claude_code:s1" and nb[0][1] > 0.99
    assert all(sim < 0.1 for cid, sim in nb if cid != "claude_code:s1")
    dbm.set_hidden(conn, "claude_code:s1", True)
    assert all(cid != "claude_code:s1" for cid, _ in V.neighbours(conn, "claude_code:s0", k=3))
    assert V.neighbours(conn, "claude_code:nope") == []


def test_dispatch_embeds_conversations_first_and_within_budget(tmp_path):
    s = make_settings(tmp_path, llm={"classify": {"backend": "claude_cli", "model": "m"}, "summary": {"backend": "claude_cli", "model": "m"}},
                      privacy={"hosted_processing": "allow"})
    s["tags_learning"]["vectors_per_run"] = 2
    conn, _ = _db(tmp_path, s)
    for i, txt in enumerate(["kubernetes a", "kubernetes b", "kubernetes c"]):
        dbm.upsert_conversation(conn, _conv(f"s{i}", txt), "unknown", None, [], None, "1", lambda t: [None] * len(t))
    from convo_index.providers import registry
    from convo_index.providers.base import LLMResult
    conn.close(); conn = dbm.connect(tmp_path / "t.db")
    # summaries/tags would call the LLM: make them fail fast so only vectors matter here
    orig = registry.complete
    registry.complete = lambda *a, **k: LLMResult(ok=False, error="auth")
    try:
        out = jobs.dispatch(conn, s, embed_fn=fake_embed, embedder_fp=FP)
    finally:
        registry.complete = orig
    assert out["vectors"] == 2 and out["vectors_ok"] == 2
    assert conn.execute("SELECT COUNT(*) FROM vec_conversations").fetchone()[0] == 2
    out = jobs.dispatch(conn, s, embed_fn=fake_embed, embedder_fp=FP, attempted=set())
    assert out["vectors"] == 1                                             # the third one next run


def test_remove_and_reembed_clean_conversation_vectors(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes a", "astrology b"])
    dbm.remove_conversation(conn, "claude_code:s0")
    assert conn.execute("SELECT COUNT(*) FROM vec_conversations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM conv_vec_ids").fetchone()[0] == 1
    dbm.recreate_vectors(conn, 768)
    assert conn.execute("SELECT COUNT(*) FROM vec_conversations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM conv_vec_ids").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM derived_jobs WHERE kind='vector'").fetchone()[0] == 0


def test_vectors_are_normalized_so_cosine_holds_for_unnormalized_embedders(tmp_path):
    conn, s = _db(tmp_path)
    big = lambda texts: [[x * 7.0 for x in v] for v in fake_embed(texts)]      # same directions, norm 7
    for i, txt in enumerate(["kubernetes deploy failing", "kubernetes cluster upgrade", "astrology chart"]):
        dbm.upsert_conversation(conn, _conv(f"s{i}", txt), "unknown", None, [], None, "1", lambda t: [None] * len(t))
        assert V.run_vector(conn, f"claude_code:s{i}", big, FP) == "ok"
    nb = dict(V.neighbours(conn, "claude_code:s0", k=2))
    assert nb["claude_code:s1"] > 0.99 and 0.0 < nb["claude_code:s2"] < 0.2   # true cosine ≈0.10, not the clamped -1.0
    assert abs(sum(x * x for x in V.normalize([3.0, 4.0])) - 1.0) < 1e-9
    assert V.normalize([0.0, 0.0]) is None and V.normalize([1.0, float("nan")]) is None and V.normalize([]) is None
    assert abs(sum(x * x for x in V.normalize([1e200, 1e200])) - 1.0) < 1e-9          # no overflow


def test_neighbours_oversample_past_hidden_and_deleted_rows(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, [f"kubernetes note {i}" for i in range(12)] + ["astrology chart"])
    for i in range(1, 9):
        dbm.set_hidden(conn, f"claude_code:s{i}", True)                      # the 8 nearest are hidden
    dbm.delete_conversation(conn, "claude_code:s9")
    nb = V.neighbours(conn, "claude_code:s0", k=3)
    assert {c for c, _ in nb} == {"claude_code:s10", "claude_code:s11", "claude_code:s12"}   # identical vectors: any order
    assert len(V.neighbours(conn, "claude_code:s0", k=3, exclude_hidden=False)) == 3


def test_summary_and_reingest_mark_vector_stale_and_pending_is_sql_side(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes deploy failing"])
    cid = "claude_code:s0"
    assert V.pending_vectors(conn, FP, 10) == []
    dbm.upsert_conversation(conn, _conv("s0", "kubernetes deploy failing"), "unknown", None, [], None, "2", lambda t: [None] * len(t))
    assert jobs.get_job(conn, cid, V.VECTOR)["status"] == "stale" and V.pending_vectors(conn, FP, 10) == [cid]
    calls = []
    assert V.run_vector(conn, cid, lambda t: calls.append(t) or fake_embed(t), FP) == "skipped" and calls == []
    assert jobs.get_job(conn, cid, V.VECTOR)["status"] == "ok" and V.pending_vectors(conn, FP, 10) == []
    assert dbm.set_summary_if_current(conn, cid, {"problem": "pods crash", "solution": "restart"}, "2")
    assert V.pending_vectors(conn, FP, 10) == [cid]
    assert V.run_vector(conn, cid, fake_embed, FP) == "ok" and V.pending_vectors(conn, FP, 10) == []
    # failures are retried until max_attempts for the same inputs, then dropped
    dbm.set_summary(conn, cid, {"problem": "new", "solution": "text"}, "2")
    for _ in range(3):
        assert cid in V.pending_vectors(conn, FP, 10, max_attempts=3)
        assert V.run_vector(conn, cid, lambda t: [None], FP) == "failed"
    assert V.pending_vectors(conn, FP, 10, max_attempts=3) == []
    assert V.pending_vectors(conn, FP, 10, max_attempts=4) == [cid]


def test_dispatch_runs_summaries_before_vectors_so_new_conversations_embed_once(tmp_path):
    s = make_settings(tmp_path, llm={"summary": {"backend": "claude_cli", "model": "m"}}, privacy={"hosted_processing": "allow"})
    conn, _ = _db(tmp_path, s)
    dbm.upsert_conversation(conn, _conv("s0", "kubernetes deploy failing"), "unknown", None, [], None, "1", lambda t: [None] * len(t))
    from convo_index.providers import registry
    from convo_index.providers.base import LLMResult
    orig = registry.complete
    registry.complete = lambda *a, **k: LLMResult(ok=True, data={"problem": "pods crash", "solution": "restart", "is_resolved": True, "learning": ""})
    seen = []
    try:
        jobs.dispatch(conn, s, embed_fn=lambda t: seen.extend(t) or fake_embed(t), embedder_fp=FP)
    finally:
        registry.complete = orig
    assert len(seen) == 1 and "pods crash" in seen[0]
    assert jobs.dispatch(conn, s, embed_fn=fake_embed, embedder_fp=FP, attempted=set())["vectors"] == 0


def test_changed_inputs_revive_an_exhausted_vector_job(tmp_path):
    conn, s = _db(tmp_path)
    dbm.upsert_conversation(conn, _conv("s0", "kubernetes a"), "unknown", None, [], None, "1", lambda t: [None] * len(t))
    cid = "claude_code:s0"
    for _ in range(3):
        assert V.run_vector(conn, cid, lambda t: [None], FP) == "failed"
    assert V.pending_vectors(conn, FP, 10, max_attempts=3) == []                 # exhausted
    dbm.set_summary(conn, cid, {"problem": "pods crash", "solution": "restart"}, "1")
    job = jobs.get_job(conn, cid, V.VECTOR)                                     # still failed (no vector was ever stored),
    assert job["status"] == "failed" and job["attempts"] == 0                   # but the changed inputs earn fresh attempts
    assert V.pending_vectors(conn, FP, 10, max_attempts=3) == [cid]
    assert V.run_vector(conn, cid, fake_embed, FP) == "ok" and jobs.get_job(conn, cid, V.VECTOR)["attempts"] == 1


def test_embedding_of_old_text_is_discarded_under_the_lock(tmp_path):
    conn, s = _db(tmp_path)
    dbm.upsert_conversation(conn, _conv("s0", "kubernetes a"), "unknown", None, [], None, "1", lambda t: [None] * len(t))
    cid = "claude_code:s0"
    def slow_embed(texts):
        dbm.set_summary(conn, cid, {"problem": "new text arrived", "solution": "meanwhile"}, "1")   # summary lands mid-call
        return fake_embed(texts)
    assert V.run_vector(conn, cid, slow_embed, FP) == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM vec_conversations").fetchone()[0] == 0
    job = jobs.get_job(conn, cid, V.VECTOR)
    assert (job is None or job["status"] == "stale") and V.pending_vectors(conn, FP, 10) == [cid]   # still pending
    assert V.run_vector(conn, cid, fake_embed, FP) == "ok"


def test_provider_exceptions_and_bad_vectors_are_recorded_and_do_not_stop_the_batch(tmp_path):
    conn, s = _db(tmp_path)
    for i, txt in enumerate(["kubernetes a", "kubernetes b", "kubernetes c"]):
        dbm.upsert_conversation(conn, _conv(f"s{i}", txt), "unknown", None, [], None, "1", lambda t: [None] * len(t))
    def flaky(texts):
        out = []
        for t in texts:
            if "kubernetes b" in t:
                out.append([float("nan")] * EMBED_DIM)      # unusable vector
            else:
                out.append(fake_embed([t])[0])
        return out
    res = V.run_vectors(conn, [f"claude_code:s{i}" for i in range(3)], flaky, FP, s)
    assert res == {"attempted": 3, "ok": 2, "failed": 1, "skipped": 0}
    assert "unusable" in jobs.get_job(conn, "claude_code:s1", V.VECTOR)["last_error"]
    def boom(texts):
        raise RuntimeError("provider down")
    dbm.upsert_conversation(conn, _conv("s3", "kubernetes d"), "unknown", None, [], None, "1", lambda t: [None] * len(t))
    assert V.run_vector(conn, "claude_code:s3", boom, FP) == "failed"
    assert jobs.get_job(conn, "claude_code:s3", V.VECTOR)["last_error"].startswith("RuntimeError")
    res = jobs.dispatch(conn, s, embed_fn=boom, embedder_fp=FP)                 # nothing raises out of the dispatcher
    assert res["vectors_ok"] == 0


def test_failed_update_is_not_promoted_by_a_later_reingest(tmp_path):
    """A failed re-embed keeps the old vector; a later stale mark must lead to a real embed,
    never to the old vector being certified as current."""
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes a"])
    cid = "claude_code:s0"
    dbm.set_summary(conn, cid, {"problem": "pods crash", "solution": "restart"}, "1")      # new text
    assert V.run_vector(conn, cid, lambda t: [None], FP) == "failed"                        # embedder outage
    dbm.set_summary(conn, cid, {"problem": "pods crash", "solution": "restart"}, "1")      # reingest, same text
    calls = []
    def embed(texts):
        calls.append(texts); return fake_embed(texts)
    assert V.run_vector(conn, cid, embed, FP) == "ok" and len(calls) == 1
    assert jobs.get_job(conn, cid, V.VECTOR)["status"] == "ok"
