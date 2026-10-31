from convo_index import db as dbm
from convo_index import analytics as A
from convo_index.model import Conversation, Message


def _seed(path):
    conn = dbm.connect(path); dbm.init_schema(conn)
    def add(native, cat, cwd, month_day, tickets, source="claude_code"):
        conv = Conversation(source=source, native_id=native, title="T", first_prompt="fp", source_ref="/a",
                            cwd=cwd, git_branch="m", start_ts=f"2026-{month_day}T00:00:00Z",
                            end_ts=f"2026-{month_day}T01:00:00Z",
                            messages=[Message(seq=0, role="user", text="t", ts="x")])
        dbm.upsert_conversation(conn, conv, cat, "path", tickets, "/a", "1", lambda t: [None] * len(t))
    add("s1", "globex", "/Users/a/Development/globex/widgets-hq", "06-16", ["SAAS-1"])
    add("s2", "globex", "/Users/a/Development/globex/widgets-hq", "06-10", ["SAAS-1"])
    add("s3", "personal", "/Users/a/gt/hobby", "05-02", [], source="opencode")
    conn.close()


def test_analytics(tmp_path):
    p = tmp_path / "t.db"; _seed(p)
    out = A.analytics(p)
    assert dict(out["by_category"])["globex"] == 2
    assert dict(out["by_category"])["personal"] == 1
    assert dict(out["by_source"]) == {"claude_code": 2, "opencode": 1}
    assert dict(out["by_month"]).get("2026-06") == 2
    assert out["top_tickets"][0] == ["SAAS-1", 2]
    assert any("widgets-hq" in proj for proj, _ in out["top_projects"])


def test_usage_analytics_prices_by_model_and_reports_coverage(tmp_path):
    from convo_index.model import UsageRecord
    from tests.conftest import make_settings
    from convo_index import references as REF
    st = make_settings(tmp_path)
    p = tmp_path / "t.db"
    conn = dbm.connect(p); dbm.init_schema(conn)
    def add(native, model, month, tokens, cwd="/Users/a/proj", tickets=(), reported=None):
        conv = Conversation(source="claude_code", native_id=native, title=f"conv {native}", first_prompt="fp " + " ".join(tickets),
                            source_ref="/a",
                            cwd=cwd, start_ts=f"2026-{month}-10T00:00:00Z",
                            messages=[Message(seq=0, role="user", text="t " + " ".join(tickets)), Message(seq=1, role="assistant", text="a")],
                            usage=[UsageRecord("r" + native, 1, "anthropic", model, input_uncached=tokens, output=tokens // 10,
                                               reported_cost_usd=reported)])
        dbm.upsert_conversation(conn, conv, "unknown", None, list(tickets), "/a", "1", lambda t: [None] * len(t))
        REF.store(conn, conv, REF.extract(conv, st))
    add("a", "claude-haiku-4-5", "06", 1_000_000, tickets=["SAAS-1", "SAAS-1"])   # mentioned twice: counted once
    add("b", "claude-haiku-4-5", "05", 2_000_000, tickets=["SAAS-1"])          # $2 + $1
    add("c", "unknown-model", "06", 500_000, cwd="/Users/a/other")              # unpriced
    conn.commit(); conn.close()
    out = A.analytics(p, st)
    u = out["usage"]
    t = u["totals"]
    assert t["tokens"] == 3_850_000 and t["requests"] == 3 and t["coverage"] == round(2 / 3, 3)
    assert abs(t["estimated_cost"] - 4.5) < 1e-6 and t["reported_cost"] is None and t["unpriced_models"] == ["unknown-model"]
    assert u["by_model"][0][0] == "anthropic / claude-haiku-4-5" and abs(u["by_model"][0][1]["estimated_cost"] - 4.5) < 1e-6
    assert u["by_model"][1][1]["coverage"] == 0.0
    months = dict((m, a["tokens"]) for m, a in u["by_month"])
    assert months == {"2026-05": 2_200_000, "2026-06": 1_650_000}
    assert u["by_project"][0][0] == "/Users/a/proj" and u["by_project"][0][1]["requests"] == 2
    assert u["by_reference"][0][0] == "ticket SAAS-1" and u["by_reference"][0][1]["tokens"] == 3_300_000
    assert [c["id"] for c in u["top_conversations"]] == ["claude_code:b", "claude_code:a", "claude_code:c"]
    assert u["top_conversations"][0]["title"] == "conv b" and u["revision"]


def test_usage_analytics_exclude_hidden_and_deleted_conversations(tmp_path):
    from convo_index.model import UsageRecord
    from tests.conftest import make_settings
    p = tmp_path / "t.db"
    conn = dbm.connect(p); dbm.init_schema(conn)
    for native, tokens in (("a", 1_000_000), ("b", 5_000_000), ("c", 3_000_000)):
        conv = Conversation(source="claude_code", native_id=native, title=f"conv {native}", first_prompt="fp", source_ref="/a",
                            cwd="/x", start_ts="2026-06-10T00:00:00Z", messages=[Message(seq=0, role="user", text="t")],
                            usage=[UsageRecord("r" + native, None, "anthropic", "claude-haiku-4-5", input_uncached=tokens)])
        dbm.upsert_conversation(conn, conv, "unknown", None, [], "/a", "1", lambda t: [None] * len(t))
    dbm.set_hidden(conn, "claude_code:b", True); dbm.delete_conversation(conn, "claude_code:c"); conn.commit(); conn.close()
    u = A.analytics(p, make_settings(tmp_path))["usage"]
    assert u["totals"]["tokens"] == 1_000_000 and [c["id"] for c in u["top_conversations"]] == ["claude_code:a"]
    assert u["pricing_errors"] == []


def test_analytics_tolerates_an_apostrophe_in_the_database_path(tmp_path):
    db = tmp_path / "O'Brien" / "t.db"; db.parent.mkdir()
    _seed(db)
    from tests.conftest import make_settings
    assert A.analytics(db, make_settings(tmp_path))["by_source"]


def test_activity_analytics_skip_hidden_and_deleted_conversations(tmp_path):
    db = tmp_path / "t.db"; _seed(db)
    conn = dbm.connect(db)
    dbm.set_hidden(conn, "opencode:s3", True); dbm.delete_conversation(conn, "claude_code:s2"); conn.commit(); conn.close()
    from tests.conftest import make_settings
    a = A.analytics(db, make_settings(tmp_path))
    assert a["by_source"] == [["claude_code", 1]] and a["top_tickets"] == [["SAAS-1", 1]]
    assert a["by_month"] == [["2026-06", 1]] and a["top_projects"][0][1] == 1


def test_usage_coverage_counts_token_bearing_requests_only(tmp_path):
    from convo_index.model import UsageRecord
    from tests.conftest import make_settings
    st = make_settings(tmp_path)
    p = tmp_path / "t.db"
    conn = dbm.connect(p); dbm.init_schema(conn)
    usage = [UsageRecord("r0", 0, "anthropic", "claude-haiku-4-5", input_uncached=1000, output=10)]
    usage += [UsageRecord(f"e{i}", 0, "anthropic", "claude-haiku-4-5") for i in range(9)]      # token-less turns
    usage += [UsageRecord("u", 0, "x", "mystery", input_uncached=1000, output=10)]               # unpriced
    conv = Conversation(source="claude_code", native_id="a", title="T", first_prompt="fp", source_ref="/a", cwd="/x",
                        start_ts="2026-06-10T00:00:00Z", messages=[Message(seq=0, role="user", text="t")], usage=usage)
    dbm.upsert_conversation(conn, conv, "unknown", None, [], "/a", "1", lambda t: [None] * len(t))
    conn.commit(); conn.close()
    u = A.analytics(p, st)["usage"]
    assert u["totals"]["requests"] == 11 and u["totals"]["coverage"] == 0.5
    assert u["by_model"][0][1]["coverage"] == 1.0 and u["by_project"][0][1]["coverage"] == 0.5


def test_usage_analytics_apply_prompt_size_tiers_per_request(tmp_path):
    """The DuckDB path groups by size band too: same numbers as the SQLite path."""
    from tests.conftest import make_settings
    from tests.test_cost import _tiered, TIERED_TOTAL
    st = make_settings(tmp_path)
    _tiered(tmp_path / "t.db").close()
    u = A.analytics(tmp_path / "t.db", st)["usage"]
    assert abs(u["totals"]["estimated_cost"] - round(TIERED_TOTAL, 4)) < 1e-9 and u["totals"]["requests"] == 4
    by_model = dict(u["by_model"])
    assert abs(by_model["openai / gpt-6.1-sol"]["estimated_cost"] - (0.205 + 0.59 + 0.50)) < 1e-9
    assert by_model["openai / gpt-6.1-sol"]["requests"] == 3                # one row per model, bands summed
