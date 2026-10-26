"""References and related work (docs/design.md §9)."""

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
