"""The MCP tool allowlist for Edit-mode turns: validation, the private store, and refusing a
store that others could write."""
import json
import os

import pytest

from convo_index import turn_tools as TT
from tests.conftest import make_settings

SERVERS = ["atlassian", "claude_ai_Datadog"]


def test_entries_name_a_configured_server_and_a_tool_or_star():
    assert TT.split("mcp__atlassian__getJiraIssue", SERVERS) == ("atlassian", "getJiraIssue")
    assert TT.split("mcp__atlassian__*", SERVERS) == ("atlassian", "*")
    # a server name with "__" in it is matched as a whole, not cut at the first separator
    assert TT.split("mcp__claude_ai_Datadog__search_logs", SERVERS) == ("claude_ai_Datadog", "search_logs")
    for bad in ["mcp__slack__post", "mcp__atlassian__", "mcp__atlassian__a b", "Bash", "mcp__atlassian__x;y", None, 3]:
        assert TT.split(bad, SERVERS) is None


def test_validate_names_the_first_bad_entry_and_drops_duplicates():
    assert TT.validate(["mcp__atlassian__*", "mcp__atlassian__*"], SERVERS) == ["mcp__atlassian__*"]
    with pytest.raises(ValueError, match="mcp__slack__post"):
        TT.validate(["mcp__atlassian__*", "mcp__slack__post"], SERVERS)
    with pytest.raises(ValueError, match="list"):
        TT.validate("mcp__atlassian__*", SERVERS)


def test_save_writes_a_private_store_that_load_reads_back(tmp_path):
    st = make_settings(tmp_path)
    assert TT.load(st) == []                                   # no store yet
    assert TT.save(["mcp__atlassian__getJiraIssue"], SERVERS, st) == ["mcp__atlassian__getJiraIssue"]
    path = TT.store_path(st)
    assert path.parent == st.db_path().parent
    assert json.loads(path.read_text()) == {"mcp_tools": ["mcp__atlassian__getJiraIssue"]}
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert TT.load(st) == ["mcp__atlassian__getJiraIssue"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_store_others_can_write_is_ignored(tmp_path):
    st = make_settings(tmp_path)
    TT.save(["mcp__atlassian__*"], SERVERS, st)
    os.chmod(TT.store_path(st), 0o666)
    assert TT.load(st) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership")
def test_a_store_another_account_owns_is_ignored(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    TT.save(["mcp__atlassian__*"], SERVERS, st)
    monkeypatch.setattr(TT.os, "getuid", lambda: TT.store_path(st).stat().st_uid + 1)
    assert TT.load(st) == []


def test_a_malformed_store_is_empty(tmp_path):
    st = make_settings(tmp_path)
    path = TT.store_path(st)
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ["not json", "[]", '{"mcp_tools": "x"}']:
        path.write_text(text)
        os.chmod(path, 0o600)
        assert TT.load(st) == []
    path.write_text('{"mcp_tools": ["mcp__atlassian__*", 3]}')
    assert TT.load(st) == ["mcp__atlassian__*"]
