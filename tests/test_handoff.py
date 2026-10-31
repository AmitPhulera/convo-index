import os
import stat
import subprocess

from convo_index import db as dbm
from convo_index import handoff as H
from convo_index.model import Conversation, Message, KIND_TOOL_CALL, KIND_TOOL_RESULT, KIND_THINKING
from convo_index.providers.base import LLMResult


def _msgs():
    rows = [
        ("user", "text", "Add retries to the uploader. Do not touch the CLI flags."),
        ("assistant", KIND_THINKING, "thinking about backoff"),
        ("assistant", KIND_TOOL_CALL, "[Bash] {\"command\": \"pytest\"}"),
        ("tool", KIND_TOOL_RESULT, "ERROR: 2 failed, 10 passed"),
        ("assistant", "text", "Added retries with exponential backoff; all tests pass."),
        ("user", "text", "<system-reminder>be brief</system-reminder>"),
        ("user", "text", "No, the tests failed. Use a fixed delay instead."),
        ("user", "text", "[Request interrupted by user for tool use]"),
        ("user", "text", "This session is being continued from a previous conversation that ran out of context."),
        ("assistant", "text", "Switched to a fixed 2 s delay."),
        ("user", "text", "nothing wrong here, no need to rush"),   # "no" inside the message, not opening it
    ]
    return [Message(seq=i, role=r, kind=k, text=t, ts=f"2026-10-31T10:{i:02d}:00Z") for i, (r, k, t) in enumerate(rows)]


def _db(tmp_path, msgs=None, cwd="/nowhere"):
    db = tmp_path / "t.db"
    conn = dbm.connect(db)
    dbm.init_schema(conn, db)
    c = Conversation(source="claude_code", native_id="s1", title="Uploader retries", first_prompt="Add retries",
                     messages=msgs or _msgs(), source_ref="/x/s1.jsonl", cwd=cwd, git_branch="feat/retries",
                     start_ts="2026-10-31T10:00:00Z", end_ts="2026-10-31T11:00:00Z")
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    return conn


def test_known_problems_are_facts_in_order():
    probs = H.known_problems([m.__dict__ for m in _msgs()])
    assert [(p["kind"], p["seq"]) for p in probs] == [
        ("tool error", 3), ("possible correction", 6), ("interrupted", 7), ("compacted", 8)]
    assert probs[0]["text"] == "2 failed, 10 passed"


def test_a_correction_needs_a_reply_before_it():
    msgs = [{"seq": 0, "role": "user", "kind": "text", "text": "Don't use the cache for this."}]
    assert H.known_problems(msgs) == []


def test_opencode_errors_after_the_input_are_found():
    msgs = [{"seq": 0, "role": "tool", "kind": KIND_TOOL_RESULT, "text": "[bash] ls /nope\nERROR: No such file"}]
    assert H.known_problems(msgs)[0]["text"] == "No such file"


def test_secrets_are_reported_without_repeating_them():
    hits = H.find_secrets("key AKIAABCDEFGHIJKLMNOP and ghp_" + "x" * 36 + " and password=hunter2hunter2")
    assert [h["kind"] for h in hits] == ["AWS access key", "GitHub token", "credential assignment"]
    assert all("ABCDEFGHIJ" not in h["sample"] for h in hits)
    assert H.find_secrets("nothing to see") == []


def test_transcript_file_is_private_and_complete(tmp_path):
    conn = _db(tmp_path)
    path = H.write_transcript(conn, "claude_code:s1", tmp_path / "scratch")
    text = path.read_text()
    assert "[Bash]" in text and "ERROR: 2 failed" in text
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert H.write_transcript(conn, "claude_code:nope", tmp_path / "scratch") is None


def test_repo_state_reads_git_and_is_none_elsewhere(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(git + ["add", "a.txt"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "first commit"], check=True)
    (repo / "a.txt").write_text("two\n")
    state = H.repo_state(str(repo))
    assert state["branch"] == "main" and "a.txt" in state["status"] and "first commit" in state["log"]
    assert "a.txt" in state["diff_stat"]
    assert H.repo_state(str(tmp_path / "plain")) is None and H.repo_state(None) is None


def test_digest_uses_the_summary_job_and_reports_failure(fake_llm, llm_settings):
    msgs = [m.__dict__ for m in _msgs()]
    f = fake_llm({"goal": " g ", "decisions": ["d", ""], "state": "s", "unfinished": [], "next_step": "n",
                  "unverified_claims": ["c"]})
    d, status = H.digest(msgs, "T", llm_settings)
    assert status == "ok" and d["goal"] == "g" and d["decisions"] == ["d"] and d["unverified_claims"] == ["c"]
    call = f.calls[0]
    assert call["job"] == "summary" and call["timeout"] == 90 and "be brief" not in call["prompt"]
    assert "[Bash]" in call["prompt"] and "2 failed" not in call["prompt"] and "Do not touch the CLI flags." in call["prompt"]
    fake_llm(LLMResult(ok=False, error="provider", detail="429 rate limited\nretry later"))
    assert H.digest(msgs, "T", llm_settings) == (None, "429 rate limited")
    f = fake_llm(LLMResult(ok=False, error="malformed", detail='{"goal": "half an ans'), {"goal": "g"})
    assert H.digest(msgs, "T", llm_settings)[1] == "ok" and len(f.calls) == 2       # retried once
    f = fake_llm(*[LLMResult(ok=False, error="malformed", detail='{"goal": "half')] * 3)
    assert H.digest(msgs, "T", llm_settings) == (None, "the summary backend's answer was not the expected JSON")
    assert len(f.calls) == 2
    f = fake_llm(LLMResult(ok=False, error="timeout"), {"goal": "g"})
    assert H.digest(msgs, "T", llm_settings) == (None, "the summary backend did not answer within 90 s")
    assert len(f.calls) == 1                                                        # a timeout is not retried

