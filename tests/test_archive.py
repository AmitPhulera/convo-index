from convo_index.archive import archive_filename, session_key, slugify, write_archive_copy


def test_slugify():
    assert slugify("Review Remaining Tasks!") == "review-remaining-tasks"
    assert slugify("a  b__c") == "a-b-c"


def test_archive_filename_with_ticket():
    conv = {"session_id": "63451d88-cc73-4bb2", "title": "Review Remaining Tasks",
            "start_ts": "2026-06-16T15:24:03.001Z"}
    name = archive_filename(conv, "globex", ["SAAS-1234", "QA-7"])
    assert name == f"2026-06-16__globex__review-remaining-tasks__SAAS-1234__{session_key(conv)}.jsonl"


def test_archive_filename_without_ticket():
    conv = {"session_id": "b9e3c11a-xxxx", "title": "Hobby chart parity fix",
            "start_ts": "2026-06-12T09:00:00Z"}
    assert archive_filename(conv, "personal", []) == f"2026-06-12__personal__hobby-chart-parity-fix__{session_key(conv)}.jsonl"


def test_archive_filename_missing_date():
    conv = {"session_id": "abcd1234", "title": "x", "start_ts": None}
    assert archive_filename(conv, "unknown", []).startswith("0000-00-00__unknown__x__")


def test_archive_filename_is_safe_and_budgeted():
    conv = {"session_id": "../*", "title": "t" * 500, "start_ts": "/tmp/pwned"}
    name = archive_filename(conv, "a/b" * 50, ["X*" * 50])
    assert "/" not in name and "*" not in name and name.startswith("0000-00-00__a-ba-b")
    assert len(name.encode()) <= 200 and name.endswith(f"__{session_key(conv)}.jsonl")


def test_write_archive_copy_replaces_only_this_sessions_copy(tmp_path):
    src = tmp_path / "s.jsonl"; src.write_text("{}")
    arc = tmp_path / "arc"
    copies = {}
    for sid in ("deadbeef-1111", "deadbeef-2222", "abc", "?abc", "victim", "prefix__victim", "*"):
        copies[sid] = write_archive_copy(src, {"session_id": sid, "title": "one", "start_ts": "2026-01-01T"}, "c", [], arc)
    assert all(p.exists() for p in copies.values()) and all(p.parent == arc for p in copies.values())
    renamed = write_archive_copy(src, {"session_id": "abc", "title": "renamed", "start_ts": "2026-01-01T"}, "c", [], arc)
    assert renamed.exists() and not copies["abc"].exists()          # its own predecessor is replaced
    assert all(p.exists() for sid, p in copies.items() if sid != "abc")


def test_failed_copy_keeps_the_previous_archive(tmp_path):
    import pytest
    src = tmp_path / "s.jsonl"; src.write_text("{}")
    conv = {"session_id": "s1", "title": "t", "start_ts": "2026-01-01T"}
    first = write_archive_copy(src, conv, "c", [], tmp_path / "arc")
    with pytest.raises(FileNotFoundError):
        write_archive_copy(tmp_path / "missing.jsonl", conv, "c", [], tmp_path / "arc")
    assert first.exists() and first.read_text() == "{}"
