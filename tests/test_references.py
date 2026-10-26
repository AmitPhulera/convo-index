"""References and related work (docs/design.md §9)."""
import json

from convo_index import db as dbm
from convo_index import references as REF
from convo_index.model import Conversation, Message, KIND_TEXT, KIND_TOOL_CALL
from tests.conftest import make_settings


def _conv(native="s1", texts=(), branch="main", origin=None, project_root="/repo", source="claude_code",
          start="2026-06-16T00:00:00Z"):
    msgs = []
    for i, t in enumerate(texts):
        kind, role, text = (t if isinstance(t, tuple) else (KIND_TEXT, "user", t))
        msgs.append(Message(seq=i, role=role, text=text, kind=kind))
    return Conversation(source=source, native_id=native, title="T " + native, first_prompt=msgs[0].text if msgs else "",
                        messages=msgs, source_ref="/a", cwd="/repo/x", project_root=project_root, git_branch=branch,
                        git_origin_url=origin, start_ts=start)


def _db(tmp_path):
    conn = dbm.connect(tmp_path / "t.db"); dbm.init_schema(conn, tmp_path / "t.db")
    return conn


def _by(refs):
    return {(r.kind, r.key): r for r in refs}


def test_tickets_linkable_only_from_prose(settings):
    c = _conv(texts=["Fix SAAS-42 please", (KIND_TOOL_CALL, "assistant", "[grep] changelog: QA-7 fixed UTF-8")])
    r = _by(REF.extract(c, settings))
    assert r[("ticket", "SAAS-42")].linkable is True and r[("ticket", "SAAS-42")].message_seq == 0
    assert r[("ticket", "QA-7")].linkable is False and r[("ticket", "QA-7")].confidence < 1
    assert ("ticket", "UTF-8") not in r
    assert r[("ticket", "SAAS-42")].namespace == "example.atlassian.net"


def test_github_urls_and_short_refs(settings):
    c = _conv(texts=["see https://github.com/globex/widgets-hq/pull/38068 and https://github.com/globex/widgets-hq/issues/12.",
                     "also globex/widgets-cloud#6905 and https://example.com/docs/page"])
    r = _by(REF.extract(c, settings))
    assert r[("pr", "github.com/globex/widgets-hq#38068")].linkable and r[("pr", "github.com/globex/widgets-hq#38068")].method == "url"
    assert r[("issue", "github.com/globex/widgets-hq#12")].linkable
    assert r[("issue", "github.com/globex/widgets-cloud#6905")].linkable and r[("issue", "github.com/globex/widgets-cloud#6905")].method == "regex"
    assert r[("url", "https://example.com/docs/page")].linkable is False
    assert REF.link_url("pr", "github.com/globex/widgets-hq#38068") == "https://github.com/globex/widgets-hq/pull/38068"
    assert REF.link_url("issue", "github.com/globex/widgets-hq#12") == "https://github.com/globex/widgets-hq/issues/12"


def test_bare_pr_number_links_only_with_known_origin(settings):
    anon = _conv(texts=["Review PR 123 and PR #38068"])
    r = _by(REF.extract(anon, settings))
    assert r[("pr", "#123")].linkable is False and r[("pr", "#38068")].linkable is False
    known = _conv(texts=["Review PR #38068"], origin="git@github.com:globex/widgets-hq.git")
    r = _by(REF.extract(known, settings))
    assert r[("pr", "github.com/globex/widgets-hq#38068")].linkable and r[("pr", "github.com/globex/widgets-hq#38068")].confidence == 0.7
    assert REF.repo_from_origin("https://github.com/o/r.git", {"github.com"}) == "github.com/o/r"
    assert REF.repo_from_origin("https://gitlab.com/o/r.git", {"github.com"}) is None


def test_branches_from_source_and_explicit_checkout_not_defaults(settings):
    c = _conv(texts=["git checkout -b ap/new-thing then gh pr create --head ap/other"], branch="ap/user-offboarding")
    r = _by(REF.extract(c, settings))
    assert r[("branch", "/repo@ap/user-offboarding")].method == "source" and r[("branch", "/repo@ap/user-offboarding")].namespace == "/repo"
    assert r[("branch", "/repo@ap/new-thing")].linkable and r[("branch", "/repo@ap/other")].linkable
    assert REF.branch_label("/repo@ap/new-thing") == "ap/new-thing"
    d = _conv(texts=["nothing"], branch="main")
    assert not any(x.kind == "branch" for x in REF.extract(d, settings))
    settings["references"]["default_branches"].append("ap/user-offboarding")
    assert not any(x.kind == "branch" for x in REF.extract(c, settings) if x.method == "source")


def test_commits_never_link_and_urls_are_capped(settings):
    c = _conv(texts=["commit abcdef1234 broke it; see sha 0123456789abcdef", " ".join(f"https://x.io/{i}" for i in range(80)),
                     (KIND_TOOL_CALL, "assistant", " ".join(f"commit {i:07x}abc" for i in range(200)))])
    r = REF.extract(c, settings)
    commits = [x for x in r if x.kind == "commit"]
    assert {x.key for x in commits} == {"abcdef1234", "0123456789ab"} and not any(x.linkable for x in commits)  # tool output ignored
    assert len([x for x in r if x.kind == "url"]) == REF.URL_CAP


def test_store_replaces_rows_mirrors_tickets_and_honours_overrides(tmp_path, settings):
    conn = _db(tmp_path)
    c = _conv(texts=["SAAS-1 and SAAS-2 and https://github.com/o/r/pull/9"])
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    assert REF.store(conn, c, REF.extract(c, settings)) == ["SAAS-1", "SAAS-2"]
    assert json.loads(conn.execute("SELECT tickets FROM conversations").fetchone()[0]) == ["SAAS-1", "SAAS-2"]
    REF.set_linkable(conn, c.id, "ticket", "SAAS-2", False)              # false match
    assert json.loads(conn.execute("SELECT tickets FROM conversations").fetchone()[0]) == ["SAAS-1"]
    REF.store(conn, c, REF.extract(c, settings))                          # re-ingest keeps the override
    refs = {r["key"]: r for r in REF.references_for(conn, c.id, settings)}
    assert refs["SAAS-2"]["linkable"] is False and refs["SAAS-1"]["linkable"] is True
    assert refs["github.com/o/r#9"]["url"] == "https://github.com/o/r/pull/9"
    assert conn.execute("SELECT COUNT(*) FROM references_ WHERE conversation_id=?", (c.id,)).fetchone()[0] == 3


def test_related_is_direct_not_transitive_and_inherits_parent(tmp_path, settings):
    conn = _db(tmp_path)
    a = _conv("a", texts=["SAAS-1 work"], branch="feat/x", start="2026-06-01T00:00:00Z")
    b = _conv("b", texts=["more SAAS-1"], branch="main", start="2026-06-02T00:00:00Z")
    c = _conv("c", texts=["unrelated but same branch"], branch="feat/x", start="2026-06-03T00:00:00Z")
    d = _conv("d", texts=["QA-9 only"], branch="main", start="2026-06-04T00:00:00Z")
    kid = _conv("kid", texts=["subagent grep"], branch="main", source="opencode", start="2026-06-05T00:00:00Z")
    kid.relation = ("subagent", "claude_code:b")
    for x in (a, b, c, d, kid):
        dbm.upsert_conversation(conn, x, "unknown", None, [], None, "1", lambda t: [None] * len(t))
        REF.store(conn, x, REF.extract(x, settings))
    rel = {g["key"]: g for g in REF.related_for(conn, "claude_code:a", settings)}
    assert set(rel) == {"SAAS-1", "/repo@feat/x"}
    assert [r["id"] for r in rel["SAAS-1"]["conversations"]] == ["claude_code:b"]
    assert [r["id"] for r in rel["/repo@feat/x"]["conversations"]] == ["claude_code:c"] and rel["/repo@feat/x"]["label"] == "feat/x"
    # b shares SAAS-1 with a, but NOT feat/x: c must not appear for b (no transitive merge)
    rel_b = {g["key"]: [r["id"] for r in g["conversations"]] for g in REF.related_for(conn, "claude_code:b", settings)}
    assert rel_b == {"SAAS-1": ["claude_code:a"]}
    # the sub-agent session inherits its parent's related work
    rel_kid = {g["key"]: [r["id"] for r in g["conversations"]] for g in REF.related_for(conn, "opencode:kid", settings)}
    assert rel_kid == {"SAAS-1": ["claude_code:a"]}
    assert REF.related_for(conn, "claude_code:d", settings) == []
    t = REF.timeline(conn, "SAAS-1", settings)
    assert [r["id"] for r in t["conversations"]] == ["claude_code:a", "claude_code:b"] and t["kind"] == "ticket"
    assert REF.top_keys(conn)[0] == ["ticket", "SAAS-1", 2] or REF.top_keys(conn)[0][2] == 2


def test_related_hides_hidden_and_deleted(tmp_path, settings):
    conn = _db(tmp_path)
    a = _conv("a", texts=["SAAS-1"]); b = _conv("b", texts=["SAAS-1"]); c = _conv("c", texts=["SAAS-1"])
    for x in (a, b, c):
        dbm.upsert_conversation(conn, x, "unknown", None, [], None, "1", lambda t: [None] * len(t))
        REF.store(conn, x, REF.extract(x, settings))
    dbm.set_hidden(conn, "claude_code:b", True)
    dbm.delete_conversation(conn, "claude_code:c")
    assert REF.related_for(conn, "claude_code:a", settings) == []


def test_v4_to_v5_seeds_references_from_tickets_json(tmp_path):
    conn = _db(tmp_path)
    c = _conv("a", texts=["x"])
    dbm.upsert_conversation(conn, c, "unknown", None, ["SAAS-7"], None, "1", lambda t: [None] * len(t))
    conn.execute("DELETE FROM schema_migrations WHERE version=5"); conn.execute("DROP TABLE references_"); conn.commit()
    dbm.init_schema(conn, tmp_path / "t.db")
    assert conn.execute("SELECT kind, key, method, linkable FROM references_").fetchall() == [("ticket", "SAAS-7", "legacy", 1)]


def test_file_line_refs_are_not_issues_and_pr_issue_share_one_row(settings):
    c = _conv(texts=["see convo_index/references.py#15 and src/index.ts#42 and tests/x.py#3",
                     "fix in globex/widgets-hq#38068 https://github.com/globex/widgets-hq/pull/38068"])
    r = REF.extract(c, settings)
    assert not any(x.kind == "issue" and ".py" in x.key or ".ts" in x.key for x in r)
    gh = [x for x in r if x.key == "github.com/globex/widgets-hq#38068"]
    assert len(gh) == 1 and gh[0].kind == "pr"                    # short ref + pull URL: one row, kind pr


def test_url_strip_and_checkout_in_tool_calls_and_blocklist_case(settings):
    c = _conv(texts=["docs at `https://example.com/page`. and utf-8, sha-256 handling for saas-1",
                     (KIND_TOOL_CALL, "assistant", "[bash] git checkout -b fix/thing.")])
    r = _by(REF.extract(c, settings))
    assert ("url", "https://example.com/page") in r
    assert not any(k[0] == "ticket" for k in r)                  # lowercase prefixes are blocked too; saas-1 not a ticket (regex is uppercase)
    assert r[("branch", "/repo@fix/thing")].linkable and r[("branch", "/repo@fix/thing")].method == "regex"


def test_repo_from_origin_handles_ports_and_credentials():
    hosts = {"github.com"}
    assert REF.repo_from_origin("ssh://git@github.com:22/o/r.git", hosts) == "github.com/o/r"
    assert REF.repo_from_origin("https://x-access-token:ghp_x@github.com/o/r.git", hosts) == "github.com/o/r"
    assert REF.repo_from_origin("git@github.com:o/r", hosts) == "github.com/o/r"
    assert REF.repo_from_origin("https://github.com/o", hosts) is None


def test_set_linkable_covers_pr_and_issue_kinds(tmp_path, settings):
    conn = _db(tmp_path)
    c = _conv(texts=["globex/widgets-hq#7"])
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    REF.store(conn, c, REF.extract(c, settings))
    REF.set_linkable(conn, c.id, "pr", "github.com/globex/widgets-hq#7", False)
    assert conn.execute("SELECT linkable FROM references_ WHERE key='github.com/globex/widgets-hq#7'").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM reference_decisions WHERE key='github.com/globex/widgets-hq#7'").fetchone()[0] == 2


def test_branches_with_same_name_in_different_projects_do_not_link(tmp_path, settings):
    conn = _db(tmp_path)
    a = _conv("a", texts=["x"], branch="dev", project_root="/p1")
    b = _conv("b", texts=["y"], branch="dev", project_root="/p2")
    for x in (a, b):
        dbm.upsert_conversation(conn, x, "unknown", None, [], None, "1", lambda t: [None] * len(t))
        REF.store(conn, x, REF.extract(x, settings))
    assert REF.related_for(conn, "claude_code:a", settings) == []


def test_github_keys_are_canonical_and_paths_need_a_boundary(settings):
    refs = _by(REF.extract(_conv(texts=("see Org/Repo#08 and https://GitHub.com/org/repo/pull/8 plus "
                                        "https://github.com/o/r/pull/12garbage",)), settings))
    assert ("pr", "github.com/org/repo#8") in refs and len([k for k in refs if k[0] in ("pr", "issue")]) == 1
    assert refs[("pr", "github.com/org/repo#8")].linkable
    assert REF.repo_from_origin("git@GitHub.com:Org/Repo.git", {"github.com"}) == "github.com/org/repo"
    assert REF.gh_key("GitHub.com", "Org", "Repo", "007") == "github.com/org/repo#7"


def test_short_refs_prefer_the_origin_host_then_config_order(tmp_path):
    st = make_settings(tmp_path, references={"github_hosts": ["ghe.acme.com", "github.com"]})
    refs = _by(REF.extract(_conv(texts=("fixes o/r#5",), origin="https://github.com/acme/app.git"), st))
    assert ("issue", "github.com/o/r#5") in refs                    # origin host wins over config
    refs = _by(REF.extract(_conv(texts=("fixes o/r#5",)), st))
    assert ("issue", "ghe.acme.com/o/r#5") in refs                  # else the first configured host, not alphabetical


def test_tool_calls_link_and_tool_results_only_record(settings):
    from convo_index.model import KIND_TOOL_CALL, KIND_TOOL_RESULT
    refs = _by(REF.extract(_conv(texts=((KIND_TOOL_CALL, "assistant", "[bash] gh pr view o/r#42 && git checkout -b feature/new"),
                                        (KIND_TOOL_RESULT, "tool", "o/r#43 fixed in PR 44; see https://docs.example/x"))), settings))
    assert refs[("issue", "github.com/o/r#42")].linkable and refs[("branch", "/repo@feature/new")].linkable
    assert ("issue", "github.com/o/r#43") in refs and not refs[("issue", "github.com/o/r#43")].linkable   # recorded, not linked
    assert ("url", "https://docs.example/x") not in refs                                                # tool output urls: noise


def test_branch_parsing_needs_git_context_and_handles_quotes_and_owner_prefix(settings):
    refs = _by(REF.extract(_conv(texts=("curl --head https://example.org", 'git checkout -b "feat/quoted"',
                                        "gh pr create --head alice:feature-x --title t", "git -C /repo switch -c hotfix.1"),
                                 branch="main"), settings))
    branches = {k[1] for k in refs if k[0] == "branch"}
    assert branches == {"/repo@feat/quoted", "/repo@feature-x", "/repo@hotfix.1"}
    assert not any("https" in b or "alice" in b for b in branches)


def test_url_cap_counts_distinct_urls(settings):
    texts = tuple("see https://same.example/page" for _ in range(REF.URL_CAP + 5)) + ("and https://other.example/y",)
    refs = _by(REF.extract(_conv(texts=texts), settings))
    assert ("url", "https://other.example/y") in refs and ("url", "https://same.example/page") in refs


def test_branch_key_never_collides_with_a_ticket_key(tmp_path, settings):
    conn = _db(tmp_path)
    a = _conv("a", texts=("SAAS-1 work",), project_root=None, branch="main"); a.cwd = None
    b = _conv("b", texts=("branch work",), project_root=None, branch="SAAS-1"); b.cwd = None
    for c in (a, b):
        dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
        REF.store(conn, c, REF.extract(c, settings))
    assert REF.branch_key(None, "SAAS-1") == "@SAAS-1" and REF.branch_label("@SAAS-1") == "SAAS-1"
    assert REF.related_for(conn, a.id, settings) == [] and REF.related_for(conn, b.id, settings) == []


def test_restore_refuses_never_link_kinds_and_missing_references(tmp_path, settings):
    conn = _db(tmp_path)
    c = _conv(texts=("commit abc1234def and PR 12 and https://x.example/p",))
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    REF.store(conn, c, REF.extract(c, settings))
    import pytest
    for kind, key in (("commit", "abc1234def"), ("url", "https://x.example/p"), ("pr", "#12")):
        with pytest.raises(LookupError):
            REF.set_linkable(conn, c.id, kind, key, True)
    assert REF.set_linkable(conn, c.id, "ticket", "NOPE-1", False) is False          # nothing to override
    assert conn.execute("SELECT COUNT(*) FROM references_ WHERE linkable=1").fetchone()[0] == 0


def test_overrides_are_read_under_the_store_lock_and_persist_across_reingest(tmp_path, settings):
    conn = _db(tmp_path)
    c = _conv(texts=("SAAS-9 fix",))
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    REF.store(conn, c, REF.extract(c, settings))
    assert REF.set_linkable(conn, c.id, "ticket", "SAAS-9", False)
    REF.store(conn, c, REF.extract(c, settings))                                       # re-ingest
    assert conn.execute("SELECT linkable FROM references_ WHERE key='SAAS-9'").fetchone()[0] == 0
    assert json.loads(conn.execute("SELECT tickets FROM conversations WHERE id=?", (c.id,)).fetchone()[0]) == []


def test_extractor_change_rescans_stored_conversations_once(tmp_path, settings):
    conn = _db(tmp_path)
    c = _conv(texts=("see https://github.com/o/r/pull/7 and SAAS-3",), branch="feat/x")
    dbm.upsert_conversation(conn, c, "unknown", None, ["SAAS-3"], None, "1", lambda t: [None] * len(t))
    assert conn.execute("SELECT COUNT(*) FROM references_").fetchone()[0] == 0          # like a v4 -> v5 upgrade
    assert REF.ensure_current(conn, settings) == 1
    keys = {(k, key) for k, key in conn.execute("SELECT kind, key FROM references_")}
    assert ("pr", "github.com/o/r#7") in keys and ("ticket", "SAAS-3") in keys and ("branch", "/repo@feat/x") in keys
    assert REF.ensure_current(conn, settings) == 0                                       # current: no work
    settings["references"]["default_branches"] = ["main", "feat/x"]                     # config change -> rescan
    assert REF.ensure_current(conn, settings) == 1
    assert ("branch", "/repo@feat/x") not in {(k, key) for k, key in conn.execute("SELECT kind, key FROM references_")}


def test_related_walks_the_parent_chain(tmp_path, settings):
    conn = _db(tmp_path)
    top = _conv("top", texts=("SAAS-5",))
    mid = _conv("mid", texts=()); mid.relation = ("subagent", top.id)
    leaf = _conv("leaf", texts=()); leaf.relation = ("subagent", mid.id)
    other = _conv("other", texts=("SAAS-5 too",))
    for c in (top, mid, leaf, other):
        dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
        REF.store(conn, c, REF.extract(c, settings))
    rel = REF.related_for(conn, leaf.id, settings)
    assert [g["key"] for g in rel] == ["SAAS-5"] and [r["id"] for r in rel[0]["conversations"]] == [other.id]


def test_timeline_resolves_a_bare_branch_name_when_unambiguous(tmp_path, settings):
    conn = _db(tmp_path)
    a = _conv("a", texts=("work",), branch="ap/my-branch", project_root="/repo1")
    b = _conv("b", texts=("work",), branch="ap/my-branch", project_root="/repo1")
    c = _conv("c", texts=("work",), branch="shared", project_root="/repo1")
    d = _conv("d", texts=("work",), branch="shared", project_root="/repo2")
    for x in (a, b, c, d):
        dbm.upsert_conversation(conn, x, "unknown", None, [], None, "1", lambda t: [None] * len(t))
        REF.store(conn, x, REF.extract(x, settings))
    tl = REF.timeline(conn, "ap/my-branch", settings)
    assert tl["key"] == "/repo1@ap/my-branch" and [r["id"] for r in tl["conversations"]] == [a.id, b.id] and "candidates" not in tl
    tl = REF.timeline(conn, "shared", settings)
    assert tl["conversations"] == [] and set(tl["candidates"]) == {"/repo1@shared", "/repo2@shared"}   # ambiguous: pick one
    assert REF.timeline(conn, "/repo2@shared", settings)["conversations"][0]["id"] == d.id


def test_branches_without_a_project_do_not_link(tmp_path, settings):
    conn = _db(tmp_path)
    a = _conv("a", texts=["x"], branch="feature", project_root=None, start="2026-06-01T00:00:00Z"); a.cwd = None
    b = _conv("b", texts=["y"], branch="feature", project_root=None, start="2026-06-02T00:00:00Z"); b.cwd = None
    for x in (a, b):
        dbm.upsert_conversation(conn, x, "unknown", None, [], None, "1", lambda t: [None] * len(t))
        refs = REF.extract(x, settings)
        assert [(r.key, r.linkable) for r in refs if r.kind == "branch"] == [("@feature", False)]
        REF.store(conn, x, refs)
    assert REF.related_for(conn, "claude_code:a", settings) == []


def test_backfill_reads_snapshots_under_the_lock_and_skips_deleted_conversations(tmp_path, settings, monkeypatch):
    """A conversation deleted after the id list was taken (but before its batch) is never
    re-scanned, and the transcript snapshot is read inside the write transaction, so nothing
    can change between reading it and storing its references."""
    conn = _db(tmp_path)
    a = _conv("a", texts=["SAAS-1 work"], start="2026-06-01T00:00:00Z")
    b = _conv("b", texts=["QA-2 work"], start="2026-06-02T00:00:00Z")
    for x in (a, b):
        dbm.upsert_conversation(conn, x, "unknown", None, [], None, "1", lambda t: [None] * len(t))
        REF.store(conn, x, REF.extract(x, settings))
    real_deleted, real_load, seen = dbm.is_deleted, REF._conversation_from_db, []
    def late_delete(conn_, cid):           # `a` is deleted after the id list was taken, under the lock
        if cid == "claude_code:a":        # what delete_conversation does: blocklist and drop the rows
            conn_.execute("INSERT INTO conversation_state(conversation_id, deleted) VALUES(?, 1) "
                          "ON CONFLICT(conversation_id) DO UPDATE SET deleted=1", (cid,))
            conn_.execute("DELETE FROM references_ WHERE conversation_id=?", (cid,))
        return real_deleted(conn_, cid)
    def spy(conn_, cid):
        seen.append((cid, conn_.in_transaction))
        return real_load(conn_, cid)
    monkeypatch.setattr(REF.dbm, "is_deleted", late_delete)
    monkeypatch.setattr(REF, "_conversation_from_db", spy)
    assert REF.backfill(conn, settings) == 1
    assert seen == [("claude_code:b", True)]
    assert conn.execute("SELECT COUNT(*) FROM references_ WHERE conversation_id='claude_code:a'").fetchone()[0] == 0
