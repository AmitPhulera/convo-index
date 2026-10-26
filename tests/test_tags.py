"""User-defined tag groups: definitions, rules, decisions, resolution, LLM tagging."""

import pytest

from convo_index import db as dbm
from convo_index import tags as T
from convo_index.model import Conversation, Message
from convo_index.providers.base import LLMResult
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


def test_user_decision_beats_rules_and_llm(tmp_path):
    conn = _db(tmp_path, multi_settings(tmp_path))
    c = _conv(cwd="/globex/x")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.apply_rules(conn, c.id, _row(c))
    assert T.resolve(conn, c.id)["workspace"] == ["globex"]
    T.set_decision(conn, c.id, "workspace", "personal")
    assert T.assignments_for(conn, [c.id])[c.id]["workspace"] == ["personal"]
    assert conn.execute("SELECT category, category_source FROM conversations WHERE id=?", (c.id,)).fetchone() == ("personal", "user")
    T.set_decision(conn, c.id, "workspace", None)            # "none of these"
    assert T.assignments_for(conn, [c.id])[c.id] == {}
    assert conn.execute("SELECT category FROM conversations WHERE id=?", (c.id,)).fetchone() == ("unknown",)
    T.clear_decision(conn, c.id, "workspace", None)
    assert T.assignments_for(conn, [c.id])[c.id]["workspace"] == ["globex"]   # rules show again


def test_rejection_removes_candidate_but_keeps_others(tmp_path):
    conn = _db(tmp_path, multi_settings(tmp_path))
    c = _conv(branch="infra/y", title="review the PR")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.apply_rules(conn, c.id, _row(c))
    T.set_decision(conn, c.id, "topic", "review", "reject")
    assert T.assignments_for(conn, [c.id])[c.id]["topic"] == ["infra"]
    # exclusive pick replaces an earlier pick
    T.set_decision(conn, c.id, "workspace", "globex"); T.set_decision(conn, c.id, "workspace", "personal")
    assert [r[0] for r in conn.execute("SELECT tag FROM tag_decisions WHERE group_name='workspace'")] == ["personal"]


def test_set_decision_validates_group_and_tag(tmp_path):
    conn = _db(tmp_path, multi_settings(tmp_path))
    c = _conv(); dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    with pytest.raises(LookupError):
        T.set_decision(conn, c.id, "nope", "x")
    with pytest.raises(LookupError):
        T.set_decision(conn, c.id, "topic", "nope")


def test_decisions_survive_reingest_but_evidence_is_rebuilt(tmp_path):
    conn = _db(tmp_path, multi_settings(tmp_path))
    c = _conv(cwd="/globex/x")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.apply_rules(conn, c.id, _row(c)); T.set_decision(conn, c.id, "topic", "review")
    dbm.remove_conversation(conn, c.id)                      # e.g. emptied at the source
    assert conn.execute("SELECT COUNT(*) FROM tag_evidence").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tag_decisions").fetchone()[0] == 1
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "2", lambda t: [None] * len(t))
    T.apply_rules(conn, c.id, _row(c))
    assert T.resolve(conn, c.id) == {"workspace": ["globex"], "topic": ["review"]}


# ---------------------------------------------------------------- LLM step

def test_llm_schema_and_prompt(tmp_path):
    conn = _db(tmp_path, multi_settings(tmp_path))
    groups = T.load_groups(conn)
    schema = T.llm_schema(groups)
    assert schema["properties"]["workspace"] == {"anyOf": [{"type": "string", "enum": ["globex", "personal"]}, {"type": "null"}]}
    assert schema["properties"]["topic"]["items"]["enum"] == ["infra", "review", "opencode-only"]
    p = T.build_prompt(groups, {"title": "Ti", "first_prompt": "Pr", "source": "opencode", "cwd": "/c", "git_branch": "b"},
                       {"problem": "P!", "solution": "S!"})
    assert 'Group "workspace" (exclusive)' in p and 'Group "topic" (multi)' in p and "Summary: P! S!" in p
    assert p.startswith("Classify this developer conversation")


def test_llm_tag_only_asks_about_unresolved_groups_and_records_evidence(tmp_path, fake_llm):
    s = multi_settings(tmp_path); s["llm"]["classify"] = {"backend": "claude_cli", "model": "m"}; s["privacy"]["hosted_processing"] = "allow"
    conn = _db(tmp_path, s)
    c = _conv(cwd="/globex/x")                                # workspace resolved by rule; topic open
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.apply_rules(conn, c.id, _row(c))
    f = fake_llm({"topic": ["infra", "bogus"]})
    status, res = T.llm_tag(conn, c.id, _row(c), s)
    assert status == "ok"
    assert list(f.calls[0]["schema"]["properties"]) == ["topic"]
    assert T.resolve(conn, c.id) == {"workspace": ["globex"], "topic": ["infra"]}   # bogus dropped
    srcs = {r[0]: r[1] for r in conn.execute("SELECT tag, source FROM tag_assignments WHERE conversation_id=?", (c.id,))}
    assert srcs == {"globex": "rule", "infra": "llm"}


def test_llm_tag_nothing_when_everything_resolved(tmp_path, fake_llm):
    conn = _db(tmp_path, make_settings(tmp_path))
    c = _conv(cwd="/globex/x")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.apply_rules(conn, c.id, _row(c))
    f = fake_llm({"workspace": "personal"})
    assert T.llm_tag(conn, c.id, _row(c), make_settings(tmp_path))[0] == "nothing" and f.calls == []


def test_llm_tag_failure_is_reported(tmp_path, fake_llm):
    s = make_settings(tmp_path); s["llm"]["classify"] = {"backend": "claude_cli", "model": "m"}; s["privacy"]["hosted_processing"] = "allow"
    conn = _db(tmp_path, s)
    c = _conv(cwd="/nowhere")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    fake_llm(LLMResult(ok=False, error="timeout"))
    status, res = T.llm_tag(conn, c.id, _row(c), s)
    assert status == "failed" and res.error == "timeout"


def test_none_decision_is_single_row_and_cancelled_by_a_real_tag(tmp_path):
    conn = _db(tmp_path, multi_settings(tmp_path))
    c = _conv(); dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.set_decision(conn, c.id, "topic", None); T.set_decision(conn, c.id, "topic", None)
    assert conn.execute("SELECT COUNT(*) FROM tag_decisions WHERE group_name='topic'").fetchone()[0] == 1
    assert T.decisions_for(conn, c.id)["topic"]["none"] is True
    T.set_decision(conn, c.id, "topic", "infra")                       # multi group: a real tag cancels none
    d = T.decisions_for(conn, c.id)["topic"]
    assert d["none"] is False and d["accept"] == {"infra"}
    assert conn.execute("SELECT category, category_source FROM conversations WHERE id=?", (c.id,)).fetchone() == ("unknown", None)
    T.set_decision(conn, c.id, "workspace", None)
    assert conn.execute("SELECT category, category_source FROM conversations WHERE id=?", (c.id,)).fetchone() == ("unknown", "user")


def test_invalid_decision_rejected_and_clear_whole_group(tmp_path):
    conn = _db(tmp_path, multi_settings(tmp_path))
    c = _conv(cwd="/globex/x"); dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.apply_rules(conn, c.id, _row(c))
    with pytest.raises(LookupError):
        T.set_decision(conn, c.id, "topic", "infra", "approve")
    T.set_decision(conn, c.id, "topic", "infra"); T.set_decision(conn, c.id, "topic", "review", "reject")
    assert conn.execute("SELECT COUNT(*) FROM tag_decisions WHERE group_name='topic'").fetchone()[0] == 2
    assert T.clear_decision(conn, c.id, "topic")["topic"] == []          # whole group, one call
    assert conn.execute("SELECT COUNT(*) FROM tag_decisions").fetchone()[0] == 0


def test_multi_group_stays_open_for_llm_after_a_rule_hit(tmp_path, fake_llm):
    s = multi_settings(tmp_path); s["llm"]["classify"] = {"backend": "claude_cli", "model": "m"}; s["privacy"]["hosted_processing"] = "allow"
    conn = _db(tmp_path, s)
    c = _conv(cwd="/globex/x", branch="infra/y")                      # workspace + topic:infra by rule
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.apply_rules(conn, c.id, _row(c))
    assert list(T.unresolved_groups(conn, c.id)) == ["topic"]         # exclusive closed, multi still open
    fake_llm({"topic": ["review"]})
    assert T.llm_tag(conn, c.id, _row(c), s)[0] == "ok"
    assert T.resolve(conn, c.id) == {"workspace": ["globex"], "topic": ["infra", "review"]}


def test_llm_tag_only_replaces_evidence_for_asked_groups(tmp_path, fake_llm):
    s = multi_settings(tmp_path); s["llm"]["classify"] = {"backend": "claude_cli", "model": "m"}; s["privacy"]["hosted_processing"] = "allow"
    conn = _db(tmp_path, s)
    c = _conv(cwd="/nowhere"); dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    fake_llm({"workspace": "personal", "topic": ["infra"]})
    T.llm_tag(conn, c.id, _row(c), s)
    T.set_decision(conn, c.id, "workspace", "globex")                 # workspace now decided
    fake_llm({"topic": ["review"]})
    T.llm_tag(conn, c.id, _row(c), s)                                 # asks about topic only
    kept = {(g, t) for g, t in conn.execute("SELECT group_name, tag FROM tag_evidence WHERE method='llm'")}
    assert kept == {("workspace", "personal"), ("topic", "review")}


def test_scalar_rule_values_are_one_value_not_characters(tmp_path):
    s = make_settings(tmp_path, tag_groups=[{"name": "workspace", "exclusive": True}],
                      tags=[{"name": "acme", "group": "workspace", "rules": {"path_contains": "acme"}}])
    assert T.definitions(s)["tags"][0]["rules"] == {"path_contains": ["acme"]}
    assert T.rule_matches({"path_contains": ["acme"]}, {"cwd": "/x/a"}) is None


def test_legacy_migration_seeds_tags_jobs_as_done(tmp_path, settings):
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn, tmp_path / "t.db")
    dbm.upsert_conversation(conn, _conv("a"), "personal", "haiku", [], None, "1", lambda t: [None] * len(t))
    T.sync_definitions(conn, settings)
    assert conn.execute("SELECT status, input_fingerprint FROM derived_jobs WHERE kind='tags'").fetchone() == ("ok", None)


def test_definition_change_rebuilds_rules_on_unchanged_conversations(tmp_path):
    s = multi_settings(tmp_path)
    conn = _db(tmp_path, s)
    c = _conv(cwd="/Users/me/globex/x", branch="infra/k8s")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.apply_rules(conn, c.id, _row(c)); T.resolve(conn, c.id)
    assert T.assignments_for(conn, [c.id])[c.id] == {"workspace": ["globex"], "topic": ["infra"]}
    # the path rule moves to another tag and `infra` is removed: nothing re-ingests, yet the
    # next sync must reflect it
    s["tags"][0]["rules"]["path_contains"] = ["elsewhere"]
    s["tags"][1]["rules"]["path_contains"] = ["globex"]           # personal now matches
    s["tags"] = [t for t in s["tags"] if t["name"] != "infra"]
    T.sync_definitions(conn, s)
    assert T.assignments_for(conn, [c.id])[c.id] == {"workspace": ["personal"]}
    assert conn.execute("SELECT COUNT(*) FROM tag_assignments WHERE tag='infra'").fetchone()[0] == 0
    # an accepted tag that no longer exists does not keep its group closed
    s2 = multi_settings(tmp_path)
    conn2 = _db(tmp_path / "b", s2)
    d = _conv("s2", cwd="/nowhere")
    dbm.upsert_conversation(conn2, d, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.set_decision(conn2, d.id, "topic", "review", suggest=False)
    assert "topic" not in T.unresolved_groups(conn2, d.id)
    s2["tags"] = [t for t in s2["tags"] if t["name"] != "review"]
    T.sync_definitions(conn2, s2)
    assert "topic" in T.unresolved_groups(conn2, d.id) and T.assignments_for(conn2, [d.id])[d.id] == {}


def test_rejecting_the_only_rule_candidate_reopens_an_exclusive_group(tmp_path):
    s = multi_settings(tmp_path)
    conn = _db(tmp_path, s)
    c = _conv(cwd="/Users/me/globex/x")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.apply_rules(conn, c.id, _row(c)); T.resolve(conn, c.id)
    assert "workspace" not in T.unresolved_groups(conn, c.id)          # rule settled it
    T.set_decision(conn, c.id, "workspace", "globex", "reject", suggest=False)
    assert T.assignments_for(conn, [c.id])[c.id] == {}
    assert "workspace" in T.unresolved_groups(conn, c.id)               # empty group: ask the LLM


def test_clearing_a_decision_reopens_the_tags_job(tmp_path, fake_llm):
    from convo_index import jobs
    s = multi_settings(tmp_path)
    s["llm"]["classify"] = {"backend": "claude_cli", "model": "m"}; s["privacy"]["hosted_processing"] = "allow"
    conn = _db(tmp_path, s)
    c = _conv(cwd="/nowhere")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.set_decision(conn, c.id, "workspace", "globex", suggest=False)
    T.set_decision(conn, c.id, "topic", None, suggest=False)             # none: everything decided
    assert jobs.pending_tags(conn, s, 10) == [] and jobs.get_job(conn, c.id, jobs.TAGS)["status"] == "ok"
    T.clear_decision(conn, c.id, "workspace")
    assert jobs.get_job(conn, c.id, jobs.TAGS) is None and jobs.pending_tags(conn, s, 10) == [c.id]
    assert jobs.pending_tags(conn, s, 0) == []                            # spent budget: no candidate at all


def test_slow_llm_result_is_discarded_when_the_conversation_moved(tmp_path, fake_llm):
    from convo_index import jobs
    s = multi_settings(tmp_path)
    s["llm"]["classify"] = {"backend": "claude_cli", "model": "m"}; s["privacy"]["hosted_processing"] = "allow"
    conn = _db(tmp_path, s)
    c = _conv(cwd="/nowhere")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    from convo_index.providers import registry as R
    def slow(job, prompt, schema=None, settings=None, **kw):
        dbm.delete_conversation(conn, c.id)                              # the user deletes it mid-call
        return LLMResult(ok=True, data={"workspace": "globex", "topic": ["infra"]})
    orig = R.complete; R.complete = slow
    try:
        assert T.llm_tag(conn, c.id, T.conv_row(conn, c.id), s)[0] == "stale"
    finally:
        R.complete = orig
    assert conn.execute("SELECT COUNT(*) FROM tag_evidence WHERE conversation_id=?", (c.id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tag_assignments WHERE conversation_id=?", (c.id,)).fetchone()[0] == 0
    # same for a re-ingest that bumps the source revision during the call
    d = _conv("s2", cwd="/nowhere")
    dbm.upsert_conversation(conn, d, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    def reingest(job, prompt, schema=None, settings=None, **kw):
        dbm.upsert_conversation(conn, d, "unknown", None, [], None, "2", lambda t: [None] * len(t))
        return LLMResult(ok=True, data={"workspace": "globex", "topic": []})
    R.complete = reingest
    try:
        assert jobs.run_tags(conn, d.id, s) == "skipped" and jobs.get_job(conn, d.id, jobs.TAGS) is None
    finally:
        R.complete = orig


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


def test_removed_accepted_tag_lets_rule_evidence_through(tmp_path):
    s = multi_settings(tmp_path)
    conn = _db(tmp_path, s)
    c = _conv(cwd="/x", branch="infra/k8s")                           # the branch rule matches `infra`
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    T.set_decision(conn, c.id, "topic", "review", suggest=False)       # the user's pick wins over the rule
    T.apply_rules(conn, c.id, _row(c)); T.resolve(conn, c.id)
    assert T.assignments_for(conn, [c.id])[c.id]["topic"] == ["review"]
    s["tags"] = [t for t in s["tags"] if t["name"] != "review"]      # `review` is removed from the config
    T.sync_definitions(conn, s)
    assert T.assignments_for(conn, [c.id])[c.id]["topic"] == ["infra"]  # the rule's candidate is no longer suppressed
