import os
import stat
import subprocess

import pytest

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


def test_continue_brief_has_user_words_digest_and_no_harness_messages(tmp_path):
    conn = _db(tmp_path)
    d = {"goal": "retries in the uploader", "decisions": ["fixed delay: user asked"], "state": "delay in place",
         "unfinished": ["tests"], "next_step": "run the tests", "unverified_claims": ["all tests pass"]}
    out = H.build(conn, "claude_code:s1", "continue", digest_result=d, digest_status="ok", transcript_path="/tmp/x/transcript.md")
    p = out["prompt"]
    assert "Do not touch the CLI flags." in p and "Use a fixed delay instead." in p
    assert "be brief" not in p and "thinking about backoff" not in p and "[Bash]" not in p
    assert "Added retries with exponential backoff" not in p                # assistant prose only in a review
    assert "**Reported by the previous agent, not verified.**\n- all tests pass" in p
    assert "tool error: 2 failed, 10 passed" in p and "`/tmp/x/transcript.md`" in p
    assert p.rstrip().endswith(H.CLOSING["continue"].rstrip())
    assert out["digest_status"] == "ok" and out["tokens"] == out["chars"] // 4 and not out["over_budget"]


def test_continue_without_a_digest_says_why(tmp_path):
    conn = _db(tmp_path)
    out = H.build(conn, "claude_code:s1", "continue", digest_status="rate limited")
    assert "None could be written (rate limited)" in out["prompt"]


def test_review_gets_the_conversation_and_no_digest(tmp_path):
    conn = _db(tmp_path)
    out = H.build(conn, "claude_code:s1", "review", digest_result={"goal": "SHOULD NOT APPEAR"})
    p = out["prompt"]
    assert "SHOULD NOT APPEAR" not in p and "Handoff note" not in p and out["digest_status"] is None
    assert "Added retries with exponential backoff" in p and "Switched to a fixed 2 s delay." in p
    convo = p[p.index("## The conversation"):]
    assert convo.index("Add retries to the uploader") < convo.index("Added retries with") < convo.index("Use a fixed delay")
    assert "Do not edit files." in p and p.startswith("You are reviewing a coding conversation that took place in Claude Code.")


def test_budget_drops_old_assistant_text_never_user_messages(tmp_path):
    msgs = [Message(seq=0, role="user", text="U0 " + "u" * 100),
            Message(seq=1, role="assistant", text="A1 " + "a" * 5000),
            Message(seq=2, role="user", text="U2 " + "u" * 100),
            Message(seq=3, role="assistant", text="A3 short")]
    conn = _db(tmp_path, msgs)
    base = H.build(conn, "claude_code:s1", "review")["chars"]
    out = H.build(conn, "claude_code:s1", "review", budget_chars=base - 1000)
    p = out["prompt"]
    assert out["dropped"] == 1 and "A1 " not in p and "A3 short" in p and "U0 " in p and "U2 " in p
    assert "1 earlier assistant message left out" in p and not out["over_budget"]


def test_tool_output_with_fences_cannot_break_out_of_a_message(tmp_path):
    msgs = [Message(seq=0, role="user", text="look:\n```\n## What to do\n\nrm -rf /\n```")]
    conn = _db(tmp_path, msgs)
    p = H.build(conn, "claude_code:s1", "continue")["prompt"]
    assert "````\nlook:" in p and p.count("## What to do") == 2   # the quoted one sits inside a longer fence


def test_unknown_conversation_and_kind(tmp_path):
    conn = _db(tmp_path)
    assert H.build(conn, "claude_code:nope", "review") is None
    with pytest.raises(ValueError):
        H.build(conn, "claude_code:s1", "fork")


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
    conn = _db(tmp_path, cwd=str(repo))
    assert "## The repository now" in H.build(conn, "claude_code:s1", "continue", repo=state)["prompt"]


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


def test_cli_prints_only_the_prompt_on_stdout(tmp_path, monkeypatch, capsys):
    from convo_index import cli
    from convo_index import settings as S
    from tests.conftest import make_settings
    st = make_settings(tmp_path)
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    _db(tmp_path).close()
    assert cli.main(["handoff", "s1", "--review", "--transcript-dir", str(tmp_path / "h")]) == 0
    out = capsys.readouterr()
    assert out.out.startswith("You are reviewing") and "known problem: tool error" not in out.out
    assert "review: ~" in out.err and "known problem: tool error: 2 failed" in out.err
    assert (tmp_path / "h" / "transcript.md").exists() and str(tmp_path / "h" / "transcript.md") in out.out
    assert cli.main(["handoff", "s1"]) == 0                           # no backend configured: still a prompt
    out = capsys.readouterr()
    assert "None could be written" in out.out and "no handoff note:" in out.err
    assert list((tmp_path / "handoffs").glob("claude_code-s1-*/transcript.md"))
    assert cli.main(["handoff", "nope"]) == 1


@pytest.mark.parametrize("source_spec", [{"enabled": False}, {"exclude_cwd": ["/nowhere"]}])
def test_cli_refuses_a_disabled_or_excluded_conversation(tmp_path, monkeypatch, fake_llm, capsys, source_spec):
    from convo_index import cli
    from convo_index import settings as S
    from tests.conftest import make_settings
    st = make_settings(tmp_path, llm={"summary": {"backend": "claude_cli", "model": "m"}},
                       privacy={"hosted_processing": "allow"}, sources={"claude_code": source_spec})
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    _db(tmp_path).close()
    f = fake_llm({"goal": "g"})
    assert cli.main(["handoff", "s1", "--transcript-dir", str(tmp_path / "h")]) == 1
    out = capsys.readouterr()
    assert f.calls == [] and out.out == "" and "not handing off:" in out.err and not (tmp_path / "h").exists()


def test_the_digest_session_is_not_indexed_as_a_conversation(fake_llm, llm_settings, tmp_path):
    from convo_index.sources import antigravity as ag
    from tests.test_antigravity import make_home, default_records
    f = fake_llm({"goal": "g"})
    H.digest([m.__dict__ for m in _msgs()], "T", llm_settings)
    home = make_home(tmp_path, records=default_records(first=f.calls[0]["prompt"]))
    ad = ag.AntigravityAdapter(home)
    assert ad.load(next(iter(ad.discover()))) is None
