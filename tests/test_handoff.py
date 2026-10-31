import json
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


def test_opencode_errors_after_the_status_line_are_found():
    msgs = [{"seq": 0, "role": "tool", "kind": KIND_TOOL_RESULT, "text": "[bash] ls /nope\nSTATUS: error\nERROR: No such file"}]
    assert H.known_problems(msgs)[0]["text"] == "No such file"


def test_an_error_line_in_successful_output_is_not_a_tool_error():
    msgs = [{"seq": 0, "role": "tool", "kind": KIND_TOOL_RESULT, "text": "tail of app.log:\nERROR: connection reset (old entry)"}]
    assert H.known_problems(msgs) == []


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


def _git_repo(path, subject="first commit"):
    path.mkdir()
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
    (path / "a.txt").write_text("one\n")
    subprocess.run(git + ["add", "a.txt"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", subject], check=True)
    return path


def test_repo_state_reads_git_and_is_none_elsewhere(tmp_path):
    repo = _git_repo(tmp_path / "repo")
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


def test_the_digest_reads_the_middle_of_a_long_conversation(fake_llm, llm_settings, tmp_path):
    msgs = [{"seq": i, "role": "user" if i % 2 == 0 else "assistant", "kind": "text", "text": f"m{i} " + "x" * 2000}
            for i in range(60)]
    msgs[30]["text"] = "MIDDLE-CONSTRAINT never touch the CLI flags " + "x" * 2000
    f = fake_llm({"goal": "g"})
    d, _ = H.digest(msgs, "T", llm_settings)
    assert "MIDDLE-CONSTRAINT" in f.calls[0]["prompt"] and not d["cut"]
    assert f.calls[0]["max_input_chars"] >= len(f.calls[0]["prompt"])   # not cut again by the registry
    huge = [{"seq": i, "role": "user", "kind": "text", "text": "y" * 10_000} for i in range(30)]
    fake_llm({"goal": "g"})
    d, _ = H.digest(huge, "T", llm_settings)
    assert d["cut"]
    conn = _db(tmp_path)
    p = H.build(conn, "claude_code:s1", "continue", digest_result=d, digest_status="ok")["prompt"]
    assert "the middle was too long to include" in p


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")
def test_transcript_file_is_private_even_over_an_existing_file_or_symlink(tmp_path):
    conn = _db(tmp_path)
    d = tmp_path / "out"
    d.mkdir()
    (d / "transcript.md").write_text("old")
    os.chmod(d / "transcript.md", 0o644)
    path = H.write_transcript(conn, "claude_code:s1", d)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and "[Bash]" in path.read_text()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    path.unlink()
    path.symlink_to(victim)
    H.write_transcript(conn, "claude_code:s1", d)
    assert victim.read_text() == "keep me" and not path.is_symlink()
    assert sorted(p.name for p in d.iterdir()) == ["transcript.md"]       # no temp file left behind


def test_a_relative_transcript_dir_gives_an_absolute_pointer(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    monkeypatch.chdir(tmp_path)
    path = H.write_transcript(conn, "claude_code:s1", "rel")
    assert path.is_absolute() and path == (tmp_path / "rel" / "transcript.md").resolve()


def test_copies_go_with_their_conversation_and_expire(tmp_path, monkeypatch):
    from convo_index import server
    db = tmp_path / "t.db"
    monkeypatch.setattr(server, "_db", lambda: db)
    _db(tmp_path).close()
    root = tmp_path / "handoffs"
    mine, neighbour = root / "claude_code-s1-20261031-120000", root / "claude_code-s1-b-20261031-120000"
    for d in (mine, neighbour):
        d.mkdir(parents=True)
        (d / "transcript.md").write_text("plaintext")
    server.handle_delete("claude_code:s1")
    assert not mine.exists() and neighbour.exists()             # `s1` does not take `s1-b`'s copies
    t = 1_700_000_000
    os.utime(neighbour / "transcript.md", (t, t))
    fresh = H.copy_dir(db, "claude_code:s2")
    fresh.mkdir()
    (fresh / "transcript.md").write_text("x")
    (root / "not-a-copy").mkdir()
    H.prune_copies(db, now=t + 31 * 86400)
    assert not neighbour.exists() and fresh.exists() and (root / "not-a-copy").exists()


def test_cli_prunes_old_copies(tmp_path, monkeypatch, capsys):
    from convo_index import cli
    from convo_index import settings as S
    from tests.conftest import make_settings
    st = make_settings(tmp_path)
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    _db(tmp_path).close()
    old = tmp_path / "handoffs" / "claude_code-other-20260101-000000"
    old.mkdir(parents=True)
    (old / "transcript.md").write_text("x")
    os.utime(old / "transcript.md", (1_700_000_000, 1_700_000_000))
    assert cli.main(["handoff", "s1", "--review"]) == 0
    assert not old.exists() and len(list((tmp_path / "handoffs").iterdir())) == 1


def test_secrets_in_tool_output_are_reported_from_the_transcript_file(tmp_path, monkeypatch, capsys):
    from convo_index import cli
    from convo_index import settings as S
    from tests.conftest import make_settings
    msgs = [Message(seq=0, role="user", text="deploy it"),
            Message(seq=1, role="tool", kind=KIND_TOOL_RESULT, text="export AWS_KEY=AKIAABCDEFGHIJKLMNOP")]
    st = make_settings(tmp_path)
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    _db(tmp_path, msgs).close()
    assert cli.main(["handoff", "s1", "--review", "--transcript-dir", str(tmp_path / "h")]) == 0
    out = capsys.readouterr()
    assert "AKIA" not in out.out and "masked 1 credential-looking string(s) in the transcript file" in out.err
    text = (tmp_path / "h" / "transcript.md").read_text()
    assert "AKIAABCDEFGHIJKLMNOP" not in text and "export AWS_KEY=[credential masked: AWS access key]" in text


def test_the_budget_counts_each_message_as_rendered(tmp_path):
    msgs = [Message(seq=0, role="user", text="go")] + [
        Message(seq=i, role="assistant", text="a" * 60) for i in range(1, 2001)]
    conn = _db(tmp_path, msgs)
    out = H.build(conn, "claude_code:s1", "review", budget_chars=100_000)
    assert not out["over_budget"] and out["dropped"] > 0 and "go" in out["prompt"]


def test_known_problems_keep_the_most_recent(tmp_path):
    msgs = [Message(seq=0, role="user", text="fix the build")] + [
        Message(seq=i, role="tool", kind=KIND_TOOL_RESULT, text=f"ERROR: build failed attempt {i}") for i in range(1, 1001)]
    conn = _db(tmp_path, msgs)
    out = H.build(conn, "claude_code:s1", "continue")
    section = out["prompt"][out["prompt"].index("## Known problems"):out["prompt"].index("## The user's messages")]
    assert section.count("- tool error:") == H.MAX_PROBLEMS and "attempt 1000" in section and "attempt 950\n" not in section
    assert "(950 earlier ones left out" in section and len(out["problems"]) == 1000


def test_a_failing_git_command_is_not_shown_as_a_clean_tree(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / ".git" / "index").write_bytes(b"not an index")
    state = H.repo_state(str(repo))
    p = H._repo(state)
    assert state["status"] is None and "git status --short:\n(unavailable" in p
    assert "first commit" in p                                            # the log still reads


def test_a_fence_in_a_commit_subject_stays_inside_the_block(tmp_path):
    repo = _git_repo(tmp_path / "repo", subject="fix ``` in docs")
    p = H._repo(H.repo_state(str(repo)))
    assert p[p.index("git log --oneline -10:"):].startswith("git log --oneline -10:\n````\n")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX modes; root ignores them")
def test_cli_reports_an_unwritable_transcript_dir_before_any_llm_call(tmp_path, monkeypatch, fake_llm, capsys):
    from convo_index import cli
    from convo_index import settings as S
    from tests.conftest import make_settings
    st = make_settings(tmp_path, llm={"summary": {"backend": "claude_cli", "model": "m"}}, privacy={"hosted_processing": "allow"})
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    _db(tmp_path).close()
    f = fake_llm({"goal": "g"})
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        assert cli.main(["handoff", "s1", "--transcript-dir", str(ro / "sub")]) == 1
    finally:
        os.chmod(ro, 0o700)
    out = capsys.readouterr()
    assert "cannot write the transcript to" in out.err and out.out == "" and f.calls == []


def test_the_digest_fits_a_backend_that_carries_less(fake_llm, llm_settings, monkeypatch):
    from convo_index.providers import registry
    msgs = [{"seq": i, "role": "user" if i % 2 == 0 else "assistant", "kind": "text", "text": f"m{i} " + "é" * 2000}
            for i in range(60)]
    msgs[-1]["text"] = "LAST-STATE the tests pass now"
    monkeypatch.setattr(registry, "max_prompt_bytes", lambda job, settings=None: 30_000)
    f = fake_llm({"goal": "g"})
    d, status = H.digest(msgs, "T", llm_settings)
    sent = f.calls[0]["prompt"]
    assert status == "ok" and d["cut"] and len(sent.encode()) <= 30_000
    assert "LAST-STATE" in sent and "m0 " in sent                       # both ends kept
    monkeypatch.setattr(registry, "max_prompt_bytes", lambda job, settings=None: None)
    f = fake_llm({"goal": "g"})
    H.digest(msgs, "T", llm_settings)
    assert len(f.calls[0]["prompt"]) > 100_000                            # no limit: the full window


def test_repo_state_reads_utf8_whatever_the_locale(tmp_path):
    repo = _git_repo(tmp_path / "repo", subject="Ánimo: fix the café menu")   # Á is C3 81; cp1252 has no 0x81
    assert "Ánimo: fix the café menu" in H.repo_state(str(repo))["log"]


def test_start_session_links_once_the_harness_reports_its_session(tmp_path, monkeypatch):
    from convo_index import turns
    from convo_index.sources import base as SB
    from tests.test_turns import command, drain
    conn = _db(tmp_path)
    conn.close()
    seen = {}

    def fake_new_session(target, prompt, cwd, **kw):
        seen.update(kw, target=target, prompt=prompt)
        return command([{"line": "not json"},
                        {"line": json.dumps({"type": "step_start", "sessionID": "ses_new1", "part": {"type": "step-start"}})},
                        {"line": json.dumps({"type": "text", "sessionID": "ses_new1", "part": {"type": "text", "text": "on it"}})}]), None
    monkeypatch.setattr(SB, "new_session_command", fake_new_session)
    runner = turns.TurnRunner()
    turn, state = H.start_session(runner, tmp_path / "t.db", "claude_code:s1", "opencode", "the prompt", str(tmp_path),
                                  kind="review", edits=True, read_dirs=["/d"], model="p/m")
    events = drain(runner, turn)
    assert state["id"] == "opencode:ses_new1" and any(e.get("text") == "on it" for e in events) and events[-1]["kind"] == "done"
    assert [e for e in events if e["kind"] == "session"] == [{"kind": "session", "id": "opencode:ses_new1", "linked": True}]
    assert seen["edits"] is False and seen["read_dirs"] == ("/d",) and seen["model"] == "p/m"   # a review never edits
    conn = dbm.connect(tmp_path / "t.db")
    assert dbm.links_for(conn, "claude_code:s1") == [{"id": "opencode:ses_new1", "relation": "review_of", "direction": "to"}]


def test_start_session_without_a_session_leaves_no_link(tmp_path, monkeypatch):
    from convo_index import turns
    from convo_index.sources import base as SB
    from tests.test_turns import command, drain
    _db(tmp_path).close()
    monkeypatch.setattr(SB, "new_session_command", lambda *a, **k: (command([{"line": "boom"}, {"exit": 3}]), None))
    runner = turns.TurnRunner()
    turn, state = H.start_session(runner, tmp_path / "t.db", "claude_code:s1", "opencode", "p", str(tmp_path), kind="continue")
    assert drain(runner, turn)[-1]["kind"] == "error" and state["id"] is None
    assert dbm.links_for(dbm.connect(tmp_path / "t.db"), "claude_code:s1") == []
    monkeypatch.setattr(SB, "new_session_command", lambda *a, **k: (None, None))
    with pytest.raises(LookupError):
        H.start_session(runner, tmp_path / "t.db", "claude_code:s1", "codex", "p", str(tmp_path), kind="continue")


def test_session_model_prefers_the_flag_then_the_settings(tmp_path):
    from tests.conftest import make_settings
    st = make_settings(tmp_path, handoff={"models": {"opencode": "opencode-go/deepseek-v4.1-flash"}})
    assert H.session_model(st, "opencode") == "opencode-go/deepseek-v4.1-flash"
    assert H.session_model(st, "opencode", "x/y") == "x/y" and H.session_model(st, "claude_code") is None


def _start_env(tmp_path, monkeypatch, msgs=None, lines=None):
    from convo_index import cli
    from convo_index import settings as S
    from convo_index.sources import base as SB
    from tests.conftest import make_settings
    from tests.test_turns import command
    st = make_settings(tmp_path, handoff={"models": {"opencode": "opencode-go/deepseek-v4.1-flash"}},
                       privacy={"hosted_processing": "allow"})
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    _db(tmp_path, msgs, cwd=str(tmp_path)).close()
    sent = {}

    def fake_new_session(target, prompt, cwd, **kw):
        sent.update(kw, prompt=prompt, cwd=cwd)
        return command([{"line": x} for x in (lines or [
            json.dumps({"type": "step_start", "sessionID": "ses_new1", "part": {"type": "step-start"}}),
            json.dumps({"type": "text", "sessionID": "ses_new1", "part": {"type": "text", "text": "Taking over."}})])]), None
    monkeypatch.setattr(SB, "new_session_targets", lambda: ["claude_code", "opencode"])
    monkeypatch.setattr(SB, "new_session_command", fake_new_session)
    monkeypatch.setattr(cli, "_ingest_only", lambda: {"ok": True, "busy": False})
    return cli, sent


def test_cli_start_runs_the_session_links_it_and_says_how_to_resume(tmp_path, monkeypatch, capsys):
    cli, sent = _start_env(tmp_path, monkeypatch)
    assert cli.main(["handoff", "s1", "--start", "opencode", "--edit", "--no-digest"]) == 0
    out = capsys.readouterr()
    assert out.out == "Taking over.\n"                                   # the reply, not the prompt
    assert "new conversation opencode:ses_new1 continues claude_code:s1" in out.err and "resume it:" in out.err
    assert sent["edits"] is True and sent["model"] == "opencode-go/deepseek-v4.1-flash" and sent["cwd"] == str(tmp_path)
    assert sent["prompt"].startswith("You are taking over") and sent["read_dirs"][0].name.startswith("claude_code-s1-")
    links = dbm.links_for(dbm.connect(tmp_path / "t.db"), "claude_code:s1")
    assert links == [{"id": "opencode:ses_new1", "relation": "continued_from", "direction": "to"}]


def test_cli_start_refuses_secrets_unless_told_and_sends_an_edited_prompt(tmp_path, monkeypatch, capsys):
    msgs = [Message(seq=0, role="user", text="use key AKIAABCDEFGHIJKLMNOP to deploy")]
    cli, sent = _start_env(tmp_path, monkeypatch, msgs)
    assert cli.main(["handoff", "s1", "--start", "opencode", "--review"]) == 1
    assert "not starting: the prompt holds" in capsys.readouterr().err and not sent
    edited = tmp_path / "edited.md"
    edited.write_text("Review the deploy script. The key was removed.")
    assert cli.main(["handoff", "s1", "--start", "opencode", "--review", "--prompt-file", str(edited)]) == 0   # the copy is masked
    assert sent["prompt"] == "Review the deploy script. The key was removed." and sent["edits"] is False
    copy = sent["read_dirs"][0] / "transcript.md"
    assert "AKIAABCDEFGHIJKLMNOP" not in copy.read_text() and "[credential masked: AWS access key]" in copy.read_text()
    sent.clear()
    assert cli.main(["handoff", "s1", "--start", "opencode", "--review", "--ignore-secrets"]) == 0
    assert "AKIAABCDEFGHIJKLMNOP" in sent["prompt"]                      # told to: the prompt goes as it is


def test_cli_start_reports_a_failed_session_and_an_unknown_target(tmp_path, monkeypatch, capsys):
    cli, _ = _start_env(tmp_path, monkeypatch, lines=["no session here"])
    assert cli.main(["handoff", "s1", "--start", "codex"]) == 1
    assert "cannot start a session in codex here" in capsys.readouterr().err
    assert cli.main(["handoff", "s1", "--start", "opencode", "--review"]) == 0
    err = capsys.readouterr().err
    assert "new conversation" not in err and dbm.links_for(dbm.connect(tmp_path / "t.db"), "claude_code:s1") == []


def test_start_session_brings_an_older_database_up_to_the_link_table(tmp_path, monkeypatch):
    from convo_index import turns
    from convo_index.sources import base as SB
    from tests.test_turns import command, drain
    _db(tmp_path).close()
    conn = dbm.connect(tmp_path / "t.db")
    conn.execute("DROP TABLE conversation_links")                       # as a database made before v8
    conn.execute("DELETE FROM schema_migrations WHERE version = 8")
    conn.commit()
    conn.close()
    monkeypatch.setattr(SB, "new_session_command", lambda *a, **k: (command([{"line": json.dumps({"sessionID": "ses_9"})}]), None))
    runner = turns.TurnRunner()
    turn, state = H.start_session(runner, tmp_path / "t.db", "claude_code:s1", "opencode", "p", str(tmp_path), kind="review")
    assert drain(runner, turn)[-1]["kind"] == "done"
    assert dbm.links_for(dbm.connect(tmp_path / "t.db"), "claude_code:s1")[0]["id"] == "opencode:ses_9"


def test_a_failed_link_write_is_reported_and_the_session_still_finishes(tmp_path, monkeypatch, capsys):
    import sqlite3
    cli, _ = _start_env(tmp_path, monkeypatch)

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(dbm, "add_link", broken)
    assert cli.main(["handoff", "s1", "--start", "opencode", "--review"]) == 0
    out = capsys.readouterr()
    assert out.out == "Taking over.\n" and "not linked to claude_code:s1: database is locked" in out.err
    assert "reviews claude_code:s1" not in out.err


def test_mask_secrets_replaces_every_match_and_counts_them():
    text, n = H.mask_secrets("a AKIAABCDEFGHIJKLMNOP b ghp_" + "x" * 36 + " c password=hunter2hunter2 d")
    assert n == 3 and "AKIA" not in text and "ghp_" not in text and "hunter2" not in text
    assert text.startswith("a [credential masked: AWS access key] b [credential masked: GitHub token] c ")
    assert H.mask_secrets("nothing here") == ("nothing here", 0) and H.find_secrets(text) == []


@pytest.mark.parametrize("state", ["deny", "ask"])
def test_cli_start_needs_hosted_processing_allowed(tmp_path, monkeypatch, capsys, state):
    cli, sent = _start_env(tmp_path, monkeypatch)
    from convo_index import settings as S
    S.load()["privacy"]["hosted_processing"] = state
    assert cli.main(["handoff", "s1", "--start", "opencode", "--review"]) == 1
    err = capsys.readouterr().err
    assert f"not starting: [privacy].hosted_processing = {state}" in err and not sent
    assert ("convo-index init" in err) == (state == "ask")
    assert not (tmp_path / "handoffs").exists()                          # refused before anything is written
    assert cli.main(["handoff", "s1", "--review"]) == 0                  # printing the prompt is still fine
    assert capsys.readouterr().out.startswith("You are reviewing")


def test_a_private_key_is_masked_whole_even_when_cut_short():
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
    whole = f"before\n-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n{body}\n-----END RSA PRIVATE KEY-----\nafter"
    text, n = H.mask_secrets(whole)
    assert n == 1 and "MIIEvQ" not in text and "QUJDREVG" not in text and "END RSA" not in text
    assert text.startswith("before\n[credential masked: private key]") and text.endswith("\nafter")
    cut = f"-----BEGIN PRIVATE KEY-----\n{body}\n…[tool text truncated]"            # a tool's output cut mid-key
    text, n = H.mask_secrets(cut)
    assert n == 1 and "MIIEvQ" not in text and "QUJDREVG" not in text and "truncated" in text


def test_an_edited_prompt_points_at_the_copy_this_run_opens(tmp_path, monkeypatch, capsys):
    cli, sent = _start_env(tmp_path, monkeypatch)
    assert cli.main(["handoff", "s1", "--review", "--transcript-dir", str(tmp_path / "first")]) == 0
    printed = capsys.readouterr().out
    assert str(tmp_path / "first" / "transcript.md") in printed
    edited = tmp_path / "edited.md"
    edited.write_text(printed.replace("Give a second opinion", "Give a short second opinion"))
    assert cli.main(["handoff", "s1", "--start", "opencode", "--review", "--prompt-file", str(edited)]) == 0
    opened = sent["read_dirs"][0] / "transcript.md"
    assert f"is in `{opened}`" in sent["prompt"] and str(tmp_path / "first") not in sent["prompt"]
    assert "Give a short second opinion" in sent["prompt"]
    assert H.point_at("no pointer here", "/x/transcript.md") == "no pointer here"


def test_an_id_we_chose_links_even_when_the_stream_does_not_name_it(tmp_path, monkeypatch):
    from convo_index import turns
    from convo_index.sources import base as SB
    from tests.test_turns import command, drain
    _db(tmp_path).close()
    monkeypatch.setattr(SB, "new_session_command", lambda *a, **k: (command([
        {"line": "warming up"},                                            # not an event: not proof the session exists
        {"line": json.dumps({"type": "system", "subtype": "init", "sessionId_v2": "renamed"})}]), "ours-1234"))
    runner = turns.TurnRunner()
    turn, state = H.start_session(runner, tmp_path / "t.db", "claude_code:s1", "claude_code", "p", str(tmp_path), kind="review")
    drain(runner, turn)
    assert state["id"] == "claude_code:ours-1234" and state["linked"]


def test_cli_says_when_nothing_was_linked(tmp_path, monkeypatch, capsys):
    cli, _ = _start_env(tmp_path, monkeypatch, lines=["no session here"])
    assert cli.main(["handoff", "s1", "--start", "opencode", "--review"]) == 0
    assert "the tool never reported its session, so nothing was linked" in capsys.readouterr().err


def test_the_refresh_after_a_session_has_a_time_limit(monkeypatch):
    import subprocess
    from convo_index import cli

    def slow(*a, **k):
        assert k["timeout"] == cli.INGEST_TIMEOUT
        raise subprocess.TimeoutExpired(a[0], k["timeout"])
    monkeypatch.setattr(subprocess, "run", slow)
    out = cli._ingest_only()
    assert out["ok"] is False and out["busy"] is False and "run convo-index index" in out["error"]
