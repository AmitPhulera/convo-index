"""User-defined tag groups: definitions, rules, decisions, resolution, LLM tagging."""


from convo_index import db as dbm
from convo_index import tags as T
from convo_index.model import Conversation, Message
from tests.conftest import make_settings


def _conv(native="s1", cwd="/x", branch="main", title="T", prompt="fp", source="claude_code"):
    return Conversation(source=source, native_id=native, title=title, first_prompt=prompt, source_ref="/a",
                        cwd=cwd, git_branch=branch,
                        messages=[Message(seq=0, role="user", text=prompt)])


def _db(tmp_path, settings):
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn, tmp_path / "t.db")
    T.sync_definitions(conn, settings)
    return conn


def _row(conv):
    return {"cwd": conv.cwd, "git_branch": conv.git_branch, "title": conv.title,
            "first_prompt": conv.first_prompt, "source": conv.source}


def multi_settings(tmp_path):
    return make_settings(tmp_path, tag_groups=[
        {"name": "workspace", "exclusive": True, "description": "whose work"},
        {"name": "topic", "exclusive": False, "description": "what it is about"},
    ], tags=[
        {"name": "globex", "group": "workspace", "description": "work", "rules": {"path_contains": ["globex"]}},
        {"name": "personal", "group": "workspace", "description": "side", "rules": {"path_contains": ["hobby"]}},
        {"name": "infra", "group": "topic", "description": "servers", "rules": {"branch_prefix": ["infra/"], "regex": ["ansible|deploy"]}},
        {"name": "review", "group": "topic", "description": "PR review", "rules": {"regex": [r"\bPR\b"]}},
        {"name": "opencode-only", "group": "topic", "description": "", "rules": {"source": ["opencode"]}},
    ])


# ---------------------------------------------------------------- definitions

def test_category_block_synthesizes_workspace_group(settings):
    d = T.definitions(settings)
    assert [g["name"] for g in d["groups"]] == ["workspace"] and d["groups"][0]["exclusive"]
    tags = {t["name"]: t for t in d["tags"]}
    assert tags["globex"]["rules"] == {"path_contains": ["globex", "widgets", "saas-scripts"]}
    assert tags["personal"]["description"] == "side projects"


def test_explicit_tag_groups_win_over_category(tmp_path):
    s = multi_settings(tmp_path)
    d = T.definitions(s)
    assert [g["name"] for g in d["groups"]] == ["workspace", "topic"]
    assert {t["name"] for t in d["tags"] if t["group"] == "topic"} == {"infra", "review", "opencode-only"}


def test_sync_is_idempotent_and_revision_tracks_definitions(tmp_path, settings):
    conn = _db(tmp_path, settings)
    rev1 = dbm.get_meta(conn, T.META_REVISION)
    assert T.sync_definitions(conn, settings) == rev1
    settings["category"]["hints"]["globex"].append("new-repo")
    rev2 = T.sync_definitions(conn, settings)
    assert rev2 != rev1
    assert T.load_groups(conn)["workspace"]["tags"][0]["rules"]["path_contains"][-1] == "new-repo"


def test_legacy_category_column_becomes_workspace_assignment_once(tmp_path, settings):
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn, tmp_path / "t.db")
    for native, cat, src in (("a", "globex", "path"), ("b", "personal", "haiku"), ("c", "unknown", "llm")):
        dbm.upsert_conversation(conn, _conv(native), cat, src, [], None, "1", lambda t: [None] * len(t))
    T.sync_definitions(conn, settings)
    assert T.assignments_for(conn, ["claude_code:a", "claude_code:b", "claude_code:c"]) == {
        "claude_code:a": {"workspace": ["globex"]}, "claude_code:b": {"workspace": ["personal"]}, "claude_code:c": {}}
    srcs = dict(conn.execute("SELECT conversation_id, source FROM tag_assignments").fetchall())
    assert srcs == {"claude_code:a": "rule", "claude_code:b": "llm"}
    conn.execute("DELETE FROM tag_assignments"); conn.commit()
    T.sync_definitions(conn, settings)                       # second sync must not re-convert
    assert conn.execute("SELECT COUNT(*) FROM tag_assignments").fetchone()[0] == 0


# ---------------------------------------------------------------- rules

def test_rule_kinds(tmp_path):
    conn = _db(tmp_path, multi_settings(tmp_path))
    groups = T.load_groups(conn)
    c = _conv(cwd="/Users/me/globex/hq", branch="infra/ec2", title="Fix deploy", prompt="please", source="opencode")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    hits = T.apply_rules(conn, c.id, _row(c), groups)
    assert hits == {"workspace": ["globex"], "topic": ["infra", "opencode-only"]}
    details = {r[0]: r[1] for r in conn.execute("SELECT tag, detail FROM tag_evidence WHERE conversation_id=?", (c.id,))}
    assert details["globex"] == "path_contains:globex" and details["infra"] == "branch_prefix:infra/"
    assert details["opencode-only"] == "source:opencode"


def test_rule_regex_on_title_and_prompt_and_invalid_regex_ignored(tmp_path):
    s = multi_settings(tmp_path)
    s["tags"][3]["rules"]["regex"].append("(unclosed")
    conn = _db(tmp_path, s)
    c = _conv(title="Look at this PR please")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    assert T.apply_rules(conn, c.id, _row(c)) == {"topic": ["review"]}


# ---------------------------------------------------------------- resolution

def test_resolve_exclusive_keeps_one_multi_keeps_all_and_mirrors_category(tmp_path):
    conn = _db(tmp_path, multi_settings(tmp_path))
    c = _conv(cwd="/globex/x", branch="infra/y", title="review the PR")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.apply_rules(conn, c.id, _row(c))
    eff = T.resolve(conn, c.id)
    assert eff == {"workspace": ["globex"], "topic": ["infra", "review"]}
    assert conn.execute("SELECT category, category_source FROM conversations WHERE id=?", (c.id,)).fetchone() == ("globex", "rule")


# ---------------------------------------------------------------- LLM step


def test_scalar_rule_values_are_one_value_not_characters(tmp_path):
    s = make_settings(tmp_path, tag_groups=[{"name": "workspace", "exclusive": True}],
                      tags=[{"name": "acme", "group": "workspace", "rules": {"path_contains": "acme"}}])
    assert T.definitions(s)["tags"][0]["rules"] == {"path_contains": ["acme"]}
    assert T.rule_matches({"path_contains": ["acme"]}, {"cwd": "/x/a"}) is None


def test_invalid_rules_are_reported_and_dropped(tmp_path, capsys):
    s = multi_settings(tmp_path)
    s["tags"][2]["rules"]["regex"] = ["(unclosed", "ansible"]
    s["tags"][2]["rules"]["bogus_kind"] = ["x"]
    s["tags"][3]["rules"]["regex"] = [42, r"\bPR\b"]
    defs = T.definitions(s)
    assert len(defs["warnings"]) == 3 and any("invalid regex '(unclosed'" in w for w in defs["warnings"])
    infra = next(t for t in defs["tags"] if t["name"] == "infra")
    assert infra["rules"]["regex"] == ["ansible"] and "bogus_kind" not in infra["rules"]
    _db(tmp_path, s)
    assert "tag definitions: tag topic:infra: invalid regex" in capsys.readouterr().err
    assert T.definitions_revision(defs) == T.definitions_revision({**defs, "warnings": []})


def test_category_conversion_goes_through_resolution_and_mirrors_source(tmp_path, settings):
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn, tmp_path / "t.db")
    for native, cat, src in (("a", "globex", "path"), ("b", "personal", "haiku"), ("c", "weird", "path")):
        dbm.upsert_conversation(conn, _conv(native), cat, src, [], None, "1", lambda t: [None] * len(t))
    T.sync_definitions(conn, settings)
    rows = dict((r[0], (r[1], r[2])) for r in conn.execute("SELECT id, category, category_source FROM conversations"))
    assert rows["claude_code:a"] == ("globex", "rule") and rows["claude_code:b"] == ("personal", "llm")
    assert rows["claude_code:c"] == ("unknown", None) and T.assignments_for(conn, ["claude_code:c"])["claude_code:c"] == {}
    assert conn.execute("SELECT COUNT(*) FROM tag_assignments WHERE group_name='workspace'").fetchone()[0] == 2
    T.sync_definitions(conn, settings)                                  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM tag_evidence WHERE definition_revision='legacy'").fetchone()[0] == 2
