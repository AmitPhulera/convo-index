# tests/test_search.py
from convo_index import db as dbm
from convo_index import search as S
from convo_index.config import EMBED_DIM
from convo_index.model import Conversation, Message


def _zero(texts):
    return [[0.0] * EMBED_DIM for _ in texts]


def _conv(native, txt, source="claude_code", cwd="/x", parent=None):
    return Conversation(source=source, native_id=native, title="T" + native, first_prompt=txt,
                        messages=[Message(seq=0, role="user", text=txt, ts="2026-06-16T00:00:00Z")],
                        source_ref="/a", cwd=cwd, git_branch="m",
                        relation=("subagent", parent) if parent else None,
                        start_ts="2026-06-16T00:00:00Z", end_ts="2026-06-16T01:00:00Z")


def _seed(conn):
    dbm.init_schema(conn)
    dbm.upsert_conversation(conn, _conv("s1", "kubernetes deployment failing on widgets"),
                            "globex", "path", ["SAAS-1"], "/a", "1", _zero)
    dbm.upsert_conversation(conn, _conv("s2", "astrology chart calculation in hobby"),
                            "personal", "path", [], "/a", "1", _zero)


def test_keyword_search_finds_match(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _seed(conn)
    res = S.search(conn, "kubernetes", None, mode="keyword")
    assert [r["id"] for r in res] == ["claude_code:s1"]
    assert "kubernetes" in res[0]["snippet"].lower()
    assert res[0]["source"] == "claude_code"


def test_empty_query_browses_recent(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _seed(conn)
    res = S.search(conn, "", None, mode="hybrid")
    assert {r["id"] for r in res} == {"claude_code:s1", "claude_code:s2"}


def test_empty_query_respects_category_filter(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _seed(conn)
    res = S.search(conn, "", None, category="personal")
    assert [r["id"] for r in res] == ["claude_code:s2"]


def test_fts_query_quotes_tokens():
    assert S.fts_query("SAAS-1234") == '"SAAS" "1234"'
    assert S.fts_query("kube deploy") == '"kube" "deploy"'
    assert S.fts_query("") == ""
    assert S.fts_query("!!! +++") == ""


def test_search_with_hyphen_does_not_crash(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _seed(conn)
    res = S.search(conn, "deployment-failing", None, mode="keyword")
    assert [r["id"] for r in res] == ["claude_code:s1"]


def test_search_with_fts_operators_does_not_crash(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _seed(conn)
    assert isinstance(S.search(conn, "widgets AND", None, mode="keyword"), list)
    assert isinstance(S.search(conn, "NEAR( hobby", None, mode="keyword"), list)
    assert S.search(conn, "+++ !!!", None, mode="keyword") == []


def test_filter_by_category(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _seed(conn)
    res = S.search(conn, "a", None, category="personal", mode="keyword")
    assert all(r["category"] == "personal" for r in res)


def test_filter_has_ticket(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _seed(conn)
    res = S.search(conn, "widgets", None, has_ticket=True, mode="keyword")
    assert [r["id"] for r in res] == ["claude_code:s1"]


def test_rrf_merge_orders_by_combined_rank():
    kw = [("s1", 1), ("s2", 2)]
    vec = [("s2", 1), ("s3", 2)]
    merged = S.rrf_merge([kw, vec], k=60)
    assert merged[0] == "s2"


def test_hidden_excluded_by_default_and_included_on_flag(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _seed(conn)
    dbm.set_hidden(conn, "claude_code:s1", True)
    default = S.search(conn, "", None, mode="hybrid")
    assert {r["id"] for r in default} == {"claude_code:s2"}
    shown = S.search(conn, "", None, mode="hybrid", include_hidden=True)
    assert {r["id"] for r in shown} == {"claude_code:s1", "claude_code:s2"}
    assert next(r for r in shown if r["id"] == "claude_code:s1")["hidden"] is True


def test_project_filter_matches_exact_cwd(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn)
    dbm.upsert_conversation(conn, _conv("s1", "hi", cwd="/Users/me/projA"), "globex", "path", [], "/a", "1", _zero)
    dbm.upsert_conversation(conn, _conv("s2", "hi", cwd="/Users/me/projB"), "globex", "path", [], "/a", "1", _zero)
    res = S.search(conn, "", None, mode="hybrid", project="/Users/me/projA")
    assert [r["id"] for r in res] == ["claude_code:s1"]


def test_source_filter(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _seed(conn)
    dbm.upsert_conversation(conn, _conv("o1", "kubernetes via opencode", source="opencode"),
                            "globex", "path", [], None, "1", _zero)
    res = S.search(conn, "kubernetes", None, mode="keyword")
    assert {r["id"] for r in res} == {"claude_code:s1", "opencode:o1"}
    res = S.search(conn, "kubernetes", None, mode="keyword", source="opencode")
    assert [r["id"] for r in res] == ["opencode:o1"]


def test_children_hidden_unless_requested(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _seed(conn)
    dbm.upsert_conversation(conn, _conv("kid", "subagent grep", source="opencode", parent="opencode:s1"),
                            "globex", "path", [], None, "1", _zero)
    assert "opencode:kid" not in {r["id"] for r in S.search(conn, "", None)}
    assert "opencode:kid" in {r["id"] for r in S.search(conn, "", None, include_children=True)}


def test_recent_listing_not_starved_by_children(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn)
    dbm.upsert_conversation(conn, _conv("top", "parent", source="opencode"), "globex", "path", [], None, "1", _zero)
    for i in range(200):
        dbm.upsert_conversation(conn, _conv(f"kid{i}", "sub", source="opencode", parent="opencode:top"),
                                "globex", "path", [], None, "1", _zero)
    assert [r["id"] for r in S.search(conn, "", None, limit=30)] == ["opencode:top"]


def test_tag_filters_any_within_group_all_across_groups(tmp_path):
    from convo_index import tags as T
    from tests.conftest import make_settings
    st = make_settings(tmp_path, tag_groups=[{"name": "workspace", "exclusive": True}, {"name": "topic", "exclusive": False}],
                       tags=[{"name": "globex", "group": "workspace", "rules": {"path_contains": ["globex"]}},
                             {"name": "personal", "group": "workspace", "rules": {"path_contains": ["gt"]}},
                             {"name": "infra", "group": "topic", "rules": {"regex": ["deploy"]}},
                             {"name": "review", "group": "topic", "rules": {"regex": ["review"]}}])
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn); T.sync_definitions(conn, st)
    for native, cwd, title in (("a", "/globex/x", "deploy it"), ("b", "/globex/y", "review this"), ("c", "/gt/z", "deploy me")):
        c = _conv(native, title, cwd=cwd); c.title = title
        dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", _zero)
        T.apply_rules(conn, c.id, {"cwd": cwd, "git_branch": None, "title": title, "first_prompt": title, "source": "claude_code"})
        T.resolve(conn, c.id)
    ids = lambda res: sorted(r["id"].split(":")[1] for r in res)
    assert ids(S.search(conn, "", None, tags=["workspace:globex"])) == ["a", "b"]
    assert ids(S.search(conn, "", None, tags=["topic:infra", "topic:review"])) == ["a", "b", "c"]     # any within group
    assert ids(S.search(conn, "", None, tags=["workspace:globex", "topic:infra"])) == ["a"]           # all across groups
    assert ids(S.search(conn, "", None, category="personal")) == ["c"]                                  # legacy alias
    by_id = {r["id"].split(":")[1]: r["tags"] for r in S.search(conn, "", None, tags=["workspace:globex"])}
    assert by_id == {"a": {"workspace": ["globex"], "topic": ["infra"]}, "b": {"workspace": ["globex"], "topic": ["review"]}}


def test_tag_filter_reaches_matches_beyond_the_first_ranked_page(tmp_path):
    from tests.conftest import make_settings
    from convo_index import tags as T
    st = make_settings(tmp_path)
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn); T.sync_definitions(conn, st)
    # 80 unrelated conversations all mention "deploy"; only the last (lowest-ranked) one is globex-tagged
    for i in range(80):
        c = _conv(f"n{i:02d}", "deploy " * (80 - i), cwd="/gt/z"); c.title = "deploy"
        dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", _zero)
    tagged = _conv("t", "deploy", cwd="/globex/x"); tagged.title = "deploy"
    dbm.upsert_conversation(conn, tagged, "unknown", None, [], None, "1", _zero)
    T.apply_rules(conn, tagged.id, {"cwd": "/globex/x", "git_branch": None, "title": "deploy", "first_prompt": "deploy", "source": "claude_code"})
    T.resolve(conn, tagged.id)
    res = S.search(conn, "deploy", None, mode="keyword", limit=10, tags=["workspace:globex"])
    assert [r["id"] for r in res] == ["claude_code:t"]


def _crowd(conn, n=200):
    """One old codex conversation behind `n` newer claude_code ones that match the same words."""
    dbm.init_schema(conn)
    old = _conv("old", "kubernetes crash", source="codex", cwd="/proj/b")
    old.start_ts = "2020-01-01T00:00:00Z"
    dbm.upsert_conversation(conn, old, "unknown", None, ["ABC-1"], None, "r", _zero)
    for i in range(n):
        c = _conv(f"n{i}", "kubernetes crash kubernetes crash", cwd="/proj/a")
        c.start_ts = f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00Z"
        dbm.upsert_conversation(conn, c, "unknown", None, [], None, "r", _zero)


def test_filters_are_not_starved_by_ranking_window(tmp_path):
    """Filters apply before the ranking window: a minority source/project/ticket match beyond
    the first page of raw hits is still found (browse, keyword and semantic)."""
    conn = dbm.connect(tmp_path / "t.db"); _crowd(conn)
    for mode, q, vec in (("hybrid", "", None), ("keyword", "kubernetes", None),
                         ("semantic", "kubernetes", [0.0] * EMBED_DIM)):
        assert [r["id"] for r in S.search(conn, q, vec, mode=mode, source="codex")] == ["codex:old"], mode
        assert [r["id"] for r in S.search(conn, q, vec, mode=mode, project="/proj/b")] == ["codex:old"], mode
        assert [r["id"] for r in S.search(conn, q, vec, mode=mode, has_ticket=True)] == ["codex:old"], mode


def test_since_filters_in_sql(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _crowd(conn, n=50)
    res = S.search(conn, "kubernetes", None, mode="keyword", since="2025-01-01T00:00:00Z", limit=100)
    assert len(res) == 50 and "codex:old" not in {r["id"] for r in res}
    assert [r["id"] for r in S.search(conn, "kubernetes", None, mode="keyword", source="codex",
                                     since="2025-01-01T00:00:00Z")] == []


def test_hidden_excluded_unless_asked(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); _seed(conn)
    dbm.set_hidden(conn, "claude_code:s1", True)
    assert [r["id"] for r in S.search(conn, "kubernetes", None, mode="keyword")] == []
    res = S.search(conn, "kubernetes", None, mode="keyword", include_hidden=True)
    assert [(r["id"], r["hidden"]) for r in res] == [("claude_code:s1", True)]


def _axis(i):
    return lambda texts: [[1.0 if d == i else 0.0 for d in range(EMBED_DIM)] for _ in texts]


def test_semantic_filters_apply_inside_the_knn(tmp_path):
    """Nearer messages that the filters exclude must not use up the k-NN neighbourhood."""
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn)
    dbm.upsert_conversation(conn, _conv("far", "x", source="codex"), "u", None, [], None, "r", _axis(1))
    for i in range(200):   # identical to the query vector, so nearer than "far"
        dbm.upsert_conversation(conn, _conv(f"near{i}", "y"), "u", None, [], None, "r", _axis(0))
    q = _axis(0)([""])[0]
    assert [r["id"] for r in S.search(conn, "", q, mode="semantic", limit=1, source="codex")] == ["codex:far"]
    for i in range(200):   # the default filters (hidden) are applied the same way
        dbm.set_hidden(conn, f"claude_code:near{i}", True)
    assert [r["id"] for r in S.search(conn, "", q, mode="semantic", limit=1)] == ["codex:far"]


def test_a_long_conversation_does_not_starve_the_semantic_page(tmp_path):
    """One conversation whose 100 messages are all nearest must not hide the next conversation."""
    from convo_index.model import Conversation, Message
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn)
    def axis(i):
        v = [0.0] * EMBED_DIM; v[i] = 1.0
        return v
    long = Conversation(source="claude_code", native_id="long", title="long", first_prompt="x", source_ref="/a", cwd="/x",
                        start_ts="2026-06-01T00:00:00Z", messages=[Message(seq=i, role="user", text=f"m{i}") for i in range(100)])
    dbm.upsert_conversation(conn, long, "u", None, [], None, "r", lambda texts: [axis(0) for _ in texts])
    dbm.upsert_conversation(conn, _conv("far", "y"), "u", None, [], None, "r", lambda texts: [axis(1) for _ in texts])
    ids = [r["id"] for r in S.search(conn, "", axis(0), mode="semantic", limit=2)]
    assert ids == ["claude_code:long", "claude_code:far"]
