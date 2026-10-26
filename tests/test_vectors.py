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


def test_few_shot_examples_come_from_nearest_user_decisions(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes deploy failing", "kubernetes deploy upgrade", "kubernetes ingress", "astrology chart"])
    T.set_decision(conn, "claude_code:s1", "workspace", "globex", suggest=False)
    T.set_decision(conn, "claude_code:s2", "workspace", "personal", "reject", suggest=False)
    T.set_decision(conn, "claude_code:s3", "workspace", "personal", suggest=False)   # far away: astrology
    ex = V.few_shot_examples(conn, "claude_code:s0", "workspace", k=6)
    assert [(e["tag"], e["decision"]) for e in ex][:2] == [("globex", "accept"), ("personal", "reject")]
    assert all(e["similarity"] > 0.9 for e in ex[:2])
    prompt = T.build_prompt(T.load_groups(conn), {"title": "T", "first_prompt": "p"}, None, {"workspace": ex})
    assert "user said globex" in prompt and "user said NOT personal" in prompt


def test_suggest_writes_neighbour_evidence_without_assigning(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes deploy failing", "kubernetes cluster upgrade", "kubernetes ingress", "astrology chart"])
    T.set_decision(conn, "claude_code:s0", "workspace", "globex", suggest=False)
    n = V.suggest(conn, "claude_code:s0", "workspace", "globex", s)
    assert n == 2                                                        # the two kubernetes neighbours, not astrology
    for cid in ("claude_code:s1", "claude_code:s2"):
        assert T.assignments_for(conn, [cid])[cid] == {}                  # suggestion only
        sg = V.suggestions_for(conn, cid)
        assert sg and sg[0]["tag"] == "globex" and sg[0]["origin"] == "claude_code:s0" and sg[0]["similarity"] > 0.9
    assert T.resolve(conn, "claude_code:s1") == {"workspace": []}         # neighbour evidence never resolves
    assert V.suggest(conn, "claude_code:s0", "workspace", "globex", s) == 0   # idempotent
    # a decision on s1 closes its suggestion and, being a suggestion, does not chain further
    T.set_decision(conn, "claude_code:s1", "workspace", "globex", suggest=False)
    assert V.suggestions_for(conn, "claude_code:s1") == []
    assert len(V.open_suggestions(conn)) == 1 and V.open_suggestions(conn)[0]["id"] == "claude_code:s2"


def test_set_decision_triggers_suggestions_and_threshold_is_configurable(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes deploy failing", "kubernetes cluster upgrade", "astrology chart"])
    T.set_decision(conn, "claude_code:s0", "workspace", "globex")
    assert [x["tag"] for x in V.suggestions_for(conn, "claude_code:s1")] == ["globex"]
    assert V.suggestions_for(conn, "claude_code:s2") == []
    s["tags_learning"]["suggest_threshold_by_group"] = {"workspace": 0.999999}
    conn.execute("DELETE FROM tag_evidence WHERE method='neighbour'"); conn.commit()
    assert V.suggest(conn, "claude_code:s0", "workspace", "globex", s) == 0


def test_calibrate_reports_precision_by_similarity_bucket(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes deploy failing", "kubernetes cluster upgrade", "kubernetes ingress"])
    T.set_decision(conn, "claude_code:s0", "workspace", "globex", suggest=False)
    V.suggest(conn, "claude_code:s0", "workspace", "globex", s)
    T.set_decision(conn, "claude_code:s1", "workspace", "globex", suggest=False)            # accepted
    T.set_decision(conn, "claude_code:s2", "workspace", "globex", "reject", suggest=False)  # rejected
    rep = V.calibrate(conn)
    buckets = rep["workspace"]
    total = sum(b["accepted"] + b["rejected"] for b in buckets.values())
    assert total == 2 and any(b["precision"] is not None for b in buckets.values())


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


def test_suggestion_closure_follows_group_exclusivity(tmp_path):
    from tests.test_tags import multi_settings
    s = multi_settings(tmp_path)
    conn, _ = _db(tmp_path, s)
    _seed(conn, ["kubernetes deploy failing", "kubernetes cluster upgrade"])
    T.set_decision(conn, "claude_code:s0", "workspace", "globex", suggest=False)
    T.set_decision(conn, "claude_code:s0", "topic", "infra", suggest=False)
    V.suggest(conn, "claude_code:s0", "workspace", "globex", s)
    V.suggest(conn, "claude_code:s0", "topic", "infra", s)
    assert {(x["group"], x["tag"]) for x in V.suggestions_for(conn, "claude_code:s1")} == {("workspace", "globex"), ("topic", "infra")}
    # exclusive: accepting another tag closes the globex suggestion (implicit rejection)
    T.set_decision(conn, "claude_code:s1", "workspace", "personal", suggest=False)
    # multi: accepting another tag leaves the infra suggestion open, a reject of infra closes it
    T.set_decision(conn, "claude_code:s1", "topic", "review", suggest=False)
    assert [(x["group"], x["tag"]) for x in V.suggestions_for(conn, "claude_code:s1")] == [("topic", "infra")]
    T.set_decision(conn, "claude_code:s1", "topic", "infra", "reject", suggest=False)
    assert V.suggestions_for(conn, "claude_code:s1") == []
    # and suggest() itself respects the same rule: a new multi tag can still be proposed
    T.set_decision(conn, "claude_code:s0", "topic", "opencode-only", suggest=False)
    assert V.suggest(conn, "claude_code:s0", "topic", "opencode-only", s) == 1
    assert V.suggest(conn, "claude_code:s0", "workspace", "globex", s) == 0
    rep = V.calibrate(conn)
    assert sum(b["rejected"] for b in rep["workspace"].values()) == 1 and sum(b["accepted"] for b in rep["workspace"].values()) == 0
    b = next(iter(rep["topic"].values()))
    assert b["at_or_above"]["accepted"] + b["at_or_above"]["rejected"] == 1


def test_suggest_repoints_open_suggestion_at_closer_origin_and_is_atomic(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes deploy failing", "kubernetes deploy again", "kubernetes cluster upgrade"])
    s["tags_learning"]["suggest_threshold"] = 0.5
    for o in ("claude_code:s1", "claude_code:s2"):
        T.set_decision(conn, o, "workspace", "globex", suggest=False)
    V.suggest(conn, "claude_code:s2", "workspace", "globex", s)                   # cluster: further from s0/s1
    first = V.suggestions_for(conn, "claude_code:s0")[0]
    V.suggest(conn, "claude_code:s1", "workspace", "globex", s)                   # deploy: nearer to s0
    second = V.suggestions_for(conn, "claude_code:s0")[0]
    assert second["origin"] == "claude_code:s1" and second["similarity"] >= first["similarity"]
    assert conn.execute("SELECT COUNT(*) FROM tag_evidence WHERE conversation_id='claude_code:s0' AND method='neighbour'").fetchone()[0] == 1
    conn.execute("BEGIN IMMEDIATE")                                              # a failure must not leak a transaction
    import pytest
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        V.suggest(conn, "claude_code:s1", "workspace", "globex", s)
    conn.execute("ROLLBACK")
    assert not conn.in_transaction


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


def test_few_shot_ignores_removed_tags_and_caps_examples_per_neighbour(tmp_path):
    from tests.test_tags import multi_settings
    s = multi_settings(tmp_path)
    conn, _ = _db(tmp_path, s)
    _seed(conn, ["kubernetes deploy failing", "kubernetes deploy upgrade"])
    for tag in ("infra", "review", "opencode-only"):
        T.set_decision(conn, "claude_code:s1", "topic", tag, suggest=False)
    assert len(V.few_shot_examples(conn, "claude_code:s0", "topic", k=6)) == V.PER_NEIGHBOUR_EXAMPLES
    conn.execute("DELETE FROM tags WHERE group_name='topic' AND name IN ('infra', 'review')"); conn.commit()
    assert [e["tag"] for e in V.few_shot_examples(conn, "claude_code:s0", "topic", k=6)] == ["opencode-only"]
    prompt = T.build_prompt(T.load_groups(conn), {"title": "T"}, None,
                            {"topic": [{"title": "multi\nline", "first_prompt": "a\n\nb", "tag": "infra", "decision": "accept"}]})
    assert 'user said infra): "multi line" — a b' in prompt


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


def test_neighbours_are_exact_k_and_few_shot_finds_sparse_decisions(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, [f"kubernetes note {i}" for i in range(110)])
    for i in range(1, 104):
        dbm.set_hidden(conn, f"claude_code:s{i}", True)                          # the 103 nearest are hidden
    nb = V.neighbours(conn, "claude_code:s0", k=1)
    assert len(nb) == 1 and nb[0][0] not in {f"claude_code:s{i}" for i in range(1, 104)}
    assert len(V.neighbours(conn, "claude_code:s0", k=6)) == 6 and V.neighbours(conn, "claude_code:s0", k=0) == []
    for i in range(1, 104):
        dbm.set_hidden(conn, f"claude_code:s{i}", False)
    T.set_decision(conn, "claude_code:s109", "workspace", "globex", suggest=False)   # the only labelled one, far down the list
    ex = V.few_shot_examples(conn, "claude_code:s0", "workspace", k=6)
    assert [(e["tag"], e["decision"]) for e in ex] == [("globex", "accept")]


def test_suggestions_follow_the_origin_decision_and_tag_definitions(tmp_path):
    from tests.test_tags import multi_settings
    s = multi_settings(tmp_path)
    conn, _ = _db(tmp_path, s)
    _seed(conn, ["kubernetes deploy failing", "kubernetes cluster upgrade"])
    T.set_decision(conn, "claude_code:s0", "topic", "infra", settings=s)          # suggests infra to s1
    assert [x["tag"] for x in V.suggestions_for(conn, "claude_code:s1")] == ["infra"]
    T.set_decision(conn, "claude_code:s0", "topic", "infra", "reject", suggest=False)   # origin changed its mind
    assert V.suggestions_for(conn, "claude_code:s1") == []
    T.set_decision(conn, "claude_code:s0", "topic", "infra", suggest=False)
    assert [x["tag"] for x in V.suggestions_for(conn, "claude_code:s1")] == ["infra"]
    conn.execute("DELETE FROM tags WHERE group_name='topic' AND name='infra'"); conn.commit()   # tag removed from config
    assert V.suggestions_for(conn, "claude_code:s1") == []
    # an accepted tag that no longer exists does not close an exclusive group's suggestions
    T.set_decision(conn, "claude_code:s0", "workspace", "globex", settings=s)
    T.set_decision(conn, "claude_code:s1", "workspace", "personal", suggest=False)
    assert V.suggestions_for(conn, "claude_code:s1") == []
    conn.execute("DELETE FROM tags WHERE group_name='workspace' AND name='personal'"); conn.commit()
    assert [x["tag"] for x in V.suggestions_for(conn, "claude_code:s1")] == ["globex"]


def test_vector_replacement_and_reembed_drop_stale_suggestions(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes deploy failing", "kubernetes cluster upgrade"])
    T.set_decision(conn, "claude_code:s0", "workspace", "globex", settings=s)
    old = conn.execute("SELECT id FROM tag_evidence WHERE method='neighbour' AND conversation_id='claude_code:s1'").fetchall()
    assert len(old) == 1
    dbm.set_summary(conn, "claude_code:s1", {"problem": "pods crash", "solution": "restart"}, "1")   # new vector for s1
    assert V.run_vector(conn, "claude_code:s1", fake_embed, FP, s) == "ok"
    new = conn.execute("SELECT id FROM tag_evidence WHERE method='neighbour' AND conversation_id='claude_code:s1'").fetchall()
    assert len(new) == 1 and new != old            # the stale row went; reconcile() proposed afresh against the new vector
    dbm.recreate_vectors(conn, 768)
    assert conn.execute("SELECT COUNT(*) FROM tag_evidence WHERE method='neighbour'").fetchone()[0] == 0


def test_accept_before_vectors_propagates_once_vectors_arrive(tmp_path):
    conn, s = _db(tmp_path)
    for i, txt in enumerate(["kubernetes deploy failing", "kubernetes cluster upgrade", "kubernetes ingress"]):
        dbm.upsert_conversation(conn, _conv(f"s{i}", txt), "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.set_decision(conn, "claude_code:s0", "workspace", "globex", settings=s)           # no vectors yet: nothing proposed
    assert V.open_suggestions(conn) == []
    assert V.run_vector(conn, "claude_code:s0", fake_embed, FP, s) == "ok"                # origin vectorized: still alone
    assert V.run_vector(conn, "claude_code:s1", fake_embed, FP, s) == "ok"                # a target arrives: inward proposal
    assert [x["tag"] for x in V.suggestions_for(conn, "claude_code:s1")] == ["globex"]
    T.set_decision(conn, "claude_code:s2", "workspace", "globex", suggest=False)
    assert V.run_vector(conn, "claude_code:s2", fake_embed, FP, s) == "ok"                # outward: s2's accept reaches s1 too
    assert V.suggestions_for(conn, "claude_code:s1")[0]["origin"] in ("claude_code:s0", "claude_code:s2")


def test_tags_fingerprint_changes_when_the_vector_arrives(tmp_path):
    s = make_settings(tmp_path, llm={"classify": {"backend": "claude_cli", "model": "m"}}, privacy={"hosted_processing": "allow"})
    conn, _ = _db(tmp_path, s)
    dbm.upsert_conversation(conn, _conv("s0", "kubernetes a"), "unknown", None, [], None, "1", lambda t: [None] * len(t))
    before = jobs.tags_fingerprint(conn, "claude_code:s0", s)
    jobs._record(conn, "claude_code:s0", jobs.TAGS, before, "ok", None, reset=True)
    assert jobs.pending_tags(conn, s, 10) == []
    assert V.run_vector(conn, "claude_code:s0", fake_embed, FP) == "ok"
    assert jobs.tags_fingerprint(conn, "claude_code:s0", s) != before and "|v1|" in jobs.tags_fingerprint(conn, "claude_code:s0", s)
    assert jobs.pending_tags(conn, s, 10) == ["claude_code:s0"]                       # re-tag once, now with few-shot support


def test_learning_settings_are_validated_and_calibration_buckets_are_exact(tmp_path):
    s = make_settings(tmp_path)
    s["tags_learning"].update({"vectors_per_run": -5, "suggest_k": "many", "suggest_threshold": 7, "suggest_threshold_by_group": {"workspace": "nan"}})
    cfg = V.learning_settings(s)
    assert cfg["vectors_per_run"] == 0 and cfg["suggest_k"] == 25 and cfg["suggest_threshold"] == 1.0
    assert cfg["suggest_threshold_by_group"]["workspace"] == 1.0
    conn, _ = _db(tmp_path, make_settings(tmp_path))
    assert V.pending_vectors(conn, FP, -1) == [] and V.pending_vectors(conn, FP, "x") == []
    assert V._bucket(0.95, 5) == "0.95" and V._bucket(0.9499, 5) == "0.90" and V._bucket(1.0, 5) == "1.00" and V._bucket(0.80, 5) == "0.80"


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


def test_changed_origin_vector_drops_its_outgoing_suggestions(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes a", "kubernetes b"])
    T.set_decision(conn, "claude_code:s0", "workspace", "globex", suggest=False)
    assert V.suggest(conn, "claude_code:s0", "workspace", "globex", s) == 1
    assert V.suggestions_for(conn, "claude_code:s1")[0]["similarity"] > 0.99
    moved = _conv("s0", "astrology chart")                                 # s0 is re-ingested about something else
    dbm.upsert_conversation(conn, moved, "unknown", None, [], None, "2", lambda t: [None] * len(t))
    assert V.run_vector(conn, "claude_code:s0", fake_embed, FP) == "ok"
    assert V.suggestions_for(conn, "claude_code:s1") == []                  # scored against the old vector: gone


def test_suggestion_from_an_origin_that_no_longer_accepts_is_replaced(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes a", "kubernetes deploy b", "kubernetes c"])    # s1 is a little farther from s2 than s0
    for o in ("s0", "s1"):
        T.set_decision(conn, f"claude_code:{o}", "workspace", "globex", suggest=False)
        V.suggest(conn, f"claude_code:{o}", "workspace", "globex", s)
    sg = V.suggestions_for(conn, "claude_code:s2")
    assert sg and sg[0]["origin"] == "claude_code:s0"                       # the closer origin wins
    T.clear_decision(conn, "claude_code:s0", "workspace")                   # s0 withdraws
    assert V.suggestions_for(conn, "claude_code:s2") == []
    V.suggest(conn, "claude_code:s1", "workspace", "globex", s)              # the farther, still-valid origin
    sg = V.suggestions_for(conn, "claude_code:s2")
    assert sg and sg[0]["origin"] == "claude_code:s1" and sg[0]["similarity"] < 0.999


def test_few_shot_examples_never_come_from_a_disabled_source(tmp_path):
    conn, s = _db(tmp_path)
    _seed(conn, ["kubernetes SECRET a"])                                      # claude_code, carries a decision
    T.set_decision(conn, "claude_code:s0", "workspace", "globex", suggest=False)
    other = Conversation(source="codex", native_id="x", title="kubernetes b", first_prompt="kubernetes b", source_ref="/b",
                         cwd="/nowhere", messages=[Message(seq=0, role="user", text="kubernetes b")])
    dbm.upsert_conversation(conn, other, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    assert V.run_vector(conn, "codex:x", fake_embed, FP) == "ok"
    assert V.few_shot_examples(conn, "codex:x", "workspace", k=6, settings=s)[0]["title"].startswith("kubernetes SECRET")
    s["sources"] = {"claude_code": {"enabled": False}}
    assert V.few_shot_examples(conn, "codex:x", "workspace", k=6, settings=s) == []
