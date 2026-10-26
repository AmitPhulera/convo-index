import json


from convo_index import indexer
from convo_index.sources.claude_code import ClaudeCodeAdapter


def _make_file(projects, sid, cwd, text, entrypoint=None, uuid=None):
    d = projects / "proj"; d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    rec = {
        "type": "user", "isMeta": False, "timestamp": "2026-06-16T00:00:00Z",
        "cwd": cwd, "gitBranch": "main", "sessionId": sid, "uuid": uuid or f"{sid}-u0",
        "message": {"role": "user", "content": text},
    }
    if entrypoint:
        rec["entrypoint"] = entrypoint
    f.write_text(json.dumps(rec) + "\n")
    return f


def _load(adapter, sid):
    ref = next(r for r in adapter.discover() if r.native_id == sid)
    return adapter.load(ref)


def test_adapter_skips_headless_sdk_session(tmp_path):
    projects = tmp_path / "projects"
    _make_file(projects, "h1", "/x", "hello there", entrypoint="sdk-cli")
    assert _load(ClaudeCodeAdapter(projects), "h1") is None


def test_adapter_skips_own_classification_prompt(tmp_path):
    # Backstop: a classification session written without a flushed entrypoint line
    # must still be skipped based on its prompt text.
    projects = tmp_path / "projects"
    _make_file(projects, "h2", "/x", "Classify this developer conversation as either 'globex' or 'personal'")
    assert _load(ClaudeCodeAdapter(projects), "h2") is None


def test_project_root_strips_git_common_dir(monkeypatch):
    class R: returncode = 0; stdout = "/Users/me/repo/.git\n"
    monkeypatch.setattr(indexer.subprocess, "run", lambda *a, **k: R())
    assert indexer.project_root("/Users/me/repo/.claude/worktrees/wt") == "/Users/me/repo"


def test_project_root_falls_back_when_not_git(monkeypatch):
    class R: returncode = 128; stdout = ""
    monkeypatch.setattr(indexer.subprocess, "run", lambda *a, **k: R())
    assert indexer.project_root("/Users/me/notrepo") == "/Users/me/notrepo"


def test_project_root_falls_back_when_git_missing(monkeypatch):
    def boom(*a, **k): raise FileNotFoundError()
    monkeypatch.setattr(indexer.subprocess, "run", boom)
    assert indexer.project_root("/x") == "/x"
    assert indexer.project_root(None) is None


def test_cwd_excluded_covers_the_project_root_itself(monkeypatch):
    """`~/client/*` must exclude a session started in ~/client, not only its subfolders."""
    monkeypatch.setenv("HOME", "/home/u"); monkeypatch.setenv("USERPROFILE", "/home/u")   # ~ on POSIX and Windows
    for pat in ("~/client/*", "~/client/**", "~/client", "~/client/", "/home/u/client/*"):
        assert indexer.cwd_excluded("/home/u/client", [pat]), pat
        assert indexer.cwd_excluded("/home/u/client/sub/dir", [pat]), pat
        assert not indexer.cwd_excluded("/home/u/client-other", [pat]), pat
        assert not indexer.cwd_excluded("/home/u", [pat]), pat
    assert indexer.cwd_excluded("/work/acme-api", ["*/acme-*"])      # plain globs still work
    for pat in ("/home/u/client/*/", "~/client/*/", "/home/u/./client/*"):   # normalised like before
        assert indexer.cwd_excluded("/home/u/client/sub", [pat]), pat
    assert not indexer.cwd_excluded("", ["*"]) and not indexer.cwd_excluded(None, ["~/client"])
    assert indexer.cwd_excluded("/home/u/[archived]/x", ["~/[archived]"])   # a path wins over glob syntax
    assert indexer.cwd_excluded("/home/u/client/x", "~/client")              # a bare string is one pattern,
    assert not indexer.cwd_excluded("/home/u/other", "~/client")            # not the characters ~, /, c...


def test_cwd_excluded_tolerates_a_pattern_for_an_unknown_user():
    assert indexer.cwd_excluded("/home/me/proj", ["~nosuchuser/secret"]) is False   # no RuntimeError
    assert indexer.cwd_excluded("/x/~nosuchuser/secret/sub", ["/x/~nosuchuser/secret"]) is True
