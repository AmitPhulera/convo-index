"""The turn runner against a fake harness: concurrency limits, streaming, stdin, process-group
kills (grandchild included), bounded buffers and the terminal event."""
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from convo_index import turns
from convo_index.sources.base import TurnCommand

FAKE = str(Path(__file__).with_name("fake_harness.py"))


def command(steps, cwd=None, stdin=None, env=None):
    return TurnCommand(argv=[sys.executable, FAKE], cwd=cwd or os.getcwd(),
                       env={"FAKE_HARNESS": json.dumps(steps), **(env or {})}, stdin=stdin)


def translate(line):
    """Runner tests speak JSON events directly; the harness adapters have their own tests."""
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return []
    return [d] if isinstance(d, dict) and "kind" in d else []


def drain(runner, turn, after=0, timeout=30):
    out, deadline = [], time.time() + timeout
    for _, ev in runner.events(turn.id, after):
        if ev is not None:
            out.append(ev)
        if time.time() > deadline:
            raise AssertionError(f"turn did not finish; events so far {out}")
    return out


def wait_done(turn, timeout=30):
    deadline = time.time() + timeout
    while not turn.done:
        assert time.time() < deadline, "turn did not finish"
        time.sleep(0.02)


def pid_alive(pid):
    if os.name == "nt":
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def ok_refresh():
    return {"ok": True, "busy": False}


def test_events_stream_while_the_harness_is_still_running():
    r = turns.TurnRunner(refresh=ok_refresh)
    t = r.start("claude_code:s1", command([{"line": '{"kind": "text", "text": "one"}'}, {"sleep": 1.5},
                                           {"line": '{"kind": "text", "text": "two"}'}]), translate)
    gen = r.events(t.id)
    idx, first = next(gen)
    assert first == {"kind": "text", "text": "one"} and idx == 0
    assert not t.done                       # received before the harness exited
    rest = [ev for _, ev in gen if ev is not None]
    assert rest == [{"kind": "text", "text": "two"},
                    {"kind": "done", "exit_code": 0, "cost_usd": None, "indexed": True, "reason": None}]


def test_prompt_reaches_stdin_and_env_and_cwd_apply(tmp_path):
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([{"echo_stdin": True}, {"echo_env": "EXTRA_FLAG"}, {"echo_cwd": True}],
                                          cwd=str(tmp_path), stdin="a; rm -rf / $(x) `y` ünï", env={"EXTRA_FLAG": "on"}),
                lambda line: [{"kind": "text", "text": json.loads(line)}])
    texts = [e["text"] for e in drain(r, t) if e["kind"] == "text"]
    assert texts[0] == "a; rm -rf / $(x) `y` ünï" and texts[1] == "on"
    assert Path(texts[2]).resolve() == tmp_path.resolve()


def test_an_unencodable_prompt_still_closes_stdin_and_is_reported():
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([{"echo_stdin": True}], stdin="a\ud800b"), translate)
    last = drain(r, t, timeout=20)[-1]                 # the harness saw EOF at once rather than waiting for the timeout
    assert last["kind"] == "error" and "not valid Unicode" in last["message"]


def test_stdin_is_closed_when_the_command_has_no_prompt():
    r = turns.TurnRunner()
    t = r.start("opencode:s1", command([{"echo_stdin": True}], stdin=None), lambda line: [{"kind": "text", "text": json.loads(line)}])
    assert [e for e in drain(r, t) if e["kind"] == "text"] == [{"kind": "text", "text": ""}]


def test_one_turn_per_conversation_and_a_global_cap():
    r = turns.TurnRunner(max_concurrent=2)
    a = r.start("claude_code:a", command([{"sleep": 3}]), translate)
    with pytest.raises(turns.TurnRefused) as e:
        r.start("claude_code:a", command([]), translate)
    assert e.value.status == 409
    b = r.start("claude_code:b", command([{"sleep": 3}]), translate)
    with pytest.raises(turns.TurnRefused) as e:
        r.start("claude_code:c", command([]), translate)
    assert e.value.status == 429
    assert r.active("claude_code:a") is a and r.active("claude_code:c") is None and r.running() == 2
    r.stop(a.id); r.stop(b.id)
    wait_done(a); wait_done(b)
    assert r.running() == 0 and r.active("claude_code:a") is None
    c = r.start("claude_code:c", command([]), translate)    # the slots were released
    wait_done(c)


def test_stderr_flood_does_not_block_and_is_capped():
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([{"stderr": 1_000_000}, {"line": '{"kind": "text", "text": "after"}'},
                                           {"exit": 3}]), translate)
    events = drain(r, t)
    assert events[0] == {"kind": "text", "text": "after"}
    assert events[-1]["kind"] == "error" and events[-1]["exit_code"] == 3
    assert len(events[-1]["stderr"]) <= 4000 and len(t.stderr) <= turns.STDERR_LIMIT
    assert events[-1]["message"] == "the harness exited with status 3"


def test_partial_lines_and_garbage_are_tolerated():
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([{"partial": '{"kind": "text", '}, {"sleep": 0.2}, {"line": '"text": "joined"}'},
                                           {"line": "garbage"}, {"partial": '{"kind": "text", "text": "tail"}'}]), translate)
    assert [e["text"] for e in drain(r, t) if e["kind"] == "text"] == ["joined", "tail"]


def test_timeout_kills_the_whole_group_including_a_grandchild(tmp_path):
    pidfile = tmp_path / "child.pid"
    r = turns.TurnRunner(timeout=4)
    t = r.start("claude_code:s1", command([{"child": str(pidfile)}, {"sleep": 60}]), translate)
    deadline = time.time() + 3
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    events = drain(r, t, timeout=40)
    assert events[-1]["kind"] == "error" and "stopped after 4 seconds" in events[-1]["message"]
    child = int(pidfile.read_text())
    deadline = time.time() + 5
    while pid_alive(child) and time.time() < deadline:
        time.sleep(0.1)
    assert not pid_alive(child), "grandchild survived the group kill"


def test_terminal_event_comes_after_every_line_even_with_a_slow_translator():
    def slow(line):
        time.sleep(0.05)
        return translate(line)
    r = turns.TurnRunner()
    lines = [{"line": json.dumps({"kind": "text", "text": str(i)})} for i in range(20)]
    t = r.start("claude_code:s1", command(lines), slow)     # the harness exits long before the reader is done
    events = drain(r, t)
    assert [e["text"] for e in events[:-1]] == [str(i) for i in range(20)] and events[-1]["kind"] == "done"


def test_done_and_the_terminal_event_are_seen_together():
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([]), translate)
    for _ in range(200):
        with t.cond:
            if t.done:
                assert t.events_from(0)[-1]["kind"] == "done"
                break
        time.sleep(0.01)
    else:
        raise AssertionError("turn did not finish")


def test_an_orphan_holding_the_pipes_is_killed_after_the_grace_period(tmp_path):
    pidfile = tmp_path / "orphan.pid"
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([{"line": '{"kind": "text", "text": "x"}'}, {"orphan": str(pidfile)}]), translate)
    started = time.time()
    events = drain(r, t, timeout=60)
    assert events[0] == {"kind": "text", "text": "x"} and events[-1]["kind"] == "done"
    assert time.time() - started < turns.GRACE * 3
    child = int(pidfile.read_text())
    deadline = time.time() + 5
    while pid_alive(child) and time.time() < deadline:
        time.sleep(0.1)
    assert not pid_alive(child)


def test_stop_kills_a_grandchild_that_ignores_sigterm(tmp_path):
    pidfile = tmp_path / "child.pid"
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([{"child": str(pidfile), "ignore_term": True}, {"sleep": 60}]), translate)
    deadline = time.time() + 10
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    child = int(pidfile.read_text())
    r.stop(t.id)
    events = drain(r, t, timeout=60)
    assert events[-1]["message"] == "stopped"
    deadline = time.time() + 5
    while pid_alive(child) and time.time() < deadline:
        time.sleep(0.1)
    assert not pid_alive(child), "grandchild outlived the stopped turn"


def test_overlong_lines_are_dropped_not_accumulated(monkeypatch):
    monkeypatch.setattr(turns, "MAX_LINE", 1000)
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([{"bigline": 5000}, {"line": '{"kind": "text", "text": "after"}'}]), translate)
    events = drain(r, t)
    assert events[0]["text"].startswith("[a line longer than 1000 bytes was dropped]")
    assert events[1] == {"kind": "text", "text": "after"} and events[-1]["kind"] == "done"


def test_event_text_is_clipped_and_the_buffer_is_bounded_by_bytes(monkeypatch):
    monkeypatch.setattr(turns, "TEXT_LIMIT", 100)
    monkeypatch.setattr(turns, "EVENT_BYTES", 400)
    r = turns.TurnRunner()
    lines = [{"line": json.dumps({"kind": "text", "text": str(i) * 150})} for i in range(6)]
    t = r.start("claude_code:s1", command(lines), translate)
    wait_done(t)
    events = [ev for _, ev in r.events(t.id) if ev is not None]
    assert events[0]["kind"] == "gap" and all(len(e.get("text", "")) <= 101 for e in events)
    assert t._bytes <= 400 and events[-1]["kind"] == "done"


def test_overflow_while_a_reader_waits_is_reported_as_a_gap(monkeypatch):
    monkeypatch.setattr(turns, "EVENT_LIMIT", 3)
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([{"sleep": 30}]), translate)      # the harness itself stays quiet
    gen = r.events(t.id)
    reader = threading.Thread(target=lambda: results.append(next(gen)), daemon=True)
    results = []
    reader.start()
    time.sleep(0.3)                                                          # the reader is now waiting on the condition
    with t.cond:                                                             # seven events land before it can wake
        for i in range(7):
            t._put({"kind": "text", "text": str(i)})
        t.cond.notify_all()
    reader.join(5)
    assert results == [(3, {"kind": "gap", "missed": 4})]          # the gap carries the last evicted index
    assert [(i, ev["text"]) for i, ev in [next(gen) for _ in range(3)]] == [(4, "4"), (5, "5"), (6, "6")]
    gen.close()
    r.stop(t.id)
    wait_done(t)


def test_a_reader_that_never_returns_makes_the_turn_an_error_and_closes_the_buffer(monkeypatch):
    monkeypatch.setattr(turns, "GRACE", 0.5)
    release = threading.Event()

    def blocking(line):
        release.wait()
        return translate(line)
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([{"line": '{"kind": "text", "text": "late"}'}]), blocking)
    events = drain(r, t, timeout=30)
    assert events[-1]["kind"] == "error" and events[-1]["message"] == "the harness output could not be read to the end"
    release.set()                                                            # the reader now returns: its event is dropped
    time.sleep(0.3)
    with t.cond:
        assert t.events_from(0)[-1]["kind"] == "error" and len(t.events_from(0)) == 1


def test_no_turn_starts_after_shutdown():
    r = turns.TurnRunner()
    r.shutdown()
    with pytest.raises(turns.TurnRefused) as e:
        r.start("claude_code:s1", command([]), translate)
    assert e.value.status == 503


def _suspended_like_process():
    import subprocess
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_job_adoption_failure_kills_the_harness_and_refuses_the_turn():
    """The Windows job path, exercised with a stand-in API on any platform."""
    calls = []

    class Api:
        def adopt(self, proc):
            raise OSError("no job for you")

        def resume(self, proc):
            calls.append("resume")

        def close(self, job):
            calls.append(("close", job))
    proc = _suspended_like_process()
    with pytest.raises(turns.TurnRefused) as e:
        turns._adopt_into_job(proc, api=Api())
    assert e.value.status == 500 and "no job for you" in str(e.value)
    assert proc.poll() is not None and calls == []          # never resumed, and gone


def test_resume_failure_after_adoption_closes_the_job_and_refuses():
    calls = []

    class Api:
        def adopt(self, proc):
            return "JOB"

        def resume(self, proc):
            raise OSError("cannot resume")

        def close(self, job):
            calls.append(("close", job))
    proc = _suspended_like_process()
    with pytest.raises(turns.TurnRefused):
        turns._adopt_into_job(proc, api=Api())
    assert calls == [("close", "JOB")] and proc.poll() is not None


def test_job_adoption_success_resumes_and_returns_the_job():
    calls = []

    class Api:
        def adopt(self, proc):
            calls.append("adopt"); return "JOB"

        def resume(self, proc):
            calls.append("resume")
    proc = _suspended_like_process()
    try:
        assert turns._adopt_into_job(proc, api=Api()) == "JOB" and calls == ["adopt", "resume"]
    finally:
        proc.kill(); proc.wait()


def test_unset_variables_do_not_reach_the_harness(monkeypatch):
    monkeypatch.setenv("CONVO_TEST_OVERRIDE", "leaks")
    r = turns.TurnRunner()
    cmd = command([{"echo_env": "CONVO_TEST_OVERRIDE"}])
    cmd = TurnCommand(argv=cmd.argv, cwd=cmd.cwd, env=cmd.env, unset=("CONVO_TEST_OVERRIDE",))
    t = r.start("claude_code:s1", cmd, lambda line: [{"kind": "text", "text": json.loads(line)}])
    assert [e["text"] for e in drain(r, t) if e["kind"] == "text"] == [None]


def test_shutdown_stops_every_running_turn():
    r = turns.TurnRunner()
    a = r.start("claude_code:a", command([{"sleep": 60}]), translate)
    b = r.start("claude_code:b", command([{"sleep": 60}]), translate)
    started = time.time()
    assert r.shutdown() == 2
    assert a.done and b.done and a.proc.poll() is not None and b.proc.poll() is not None
    assert time.time() - started < turns.GRACE * 2
    assert r.shutdown() == 0


def test_stop_ends_a_running_turn_and_detaching_does_not():
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([{"line": '{"kind": "text", "text": "x"}'}, {"sleep": 30}]), translate)
    gen = r.events(t.id)
    next(gen); gen.close()                  # a client went away
    time.sleep(0.3)
    assert not t.done and t.proc.poll() is None
    assert r.stop(t.id) is True
    events = drain(r, t)
    assert events[-1]["kind"] == "error" and events[-1]["message"] == "stopped"
    assert r.stop("nope") is None
    assert r.stop(t.id) is False                         # nothing left to stop


def test_stop_during_the_refresh_keeps_a_completed_turns_outcome():
    gate = threading.Event()

    def slow_refresh():
        gate.wait(10)
        return {"ok": True, "busy": False}
    r = turns.TurnRunner(refresh=slow_refresh)
    t = r.start("claude_code:s1", command([{"line": '{"kind": "done", "cost_usd": 0.5}'}]), translate)
    deadline = time.time() + 10
    while t.outcome is None and time.time() < deadline:
        time.sleep(0.02)
    assert t.outcome == (True, None) and not t.done        # exited, refreshing
    assert r.stop(t.id) is False
    gate.set()
    last = drain(r, t)[-1]
    assert last["kind"] == "done" and last["cost_usd"] == 0.5 and last["indexed"] is True
    assert r.last_own_activity("claude_code:none") is None


def test_the_activity_marker_is_sampled_after_the_harness_and_before_the_refresh():
    marks = iter([10.0, 20.0])
    gate = threading.Event()

    def refresh():
        gate.wait(10)
        return {"ok": True}
    r = turns.TurnRunner(refresh=refresh)
    t = r.start("claude_code:s1", command([]), translate, activity=lambda: next(marks))
    deadline = time.time() + 10
    while t.activity_after is None and time.time() < deadline:
        time.sleep(0.02)
    assert t.activity_after == 10.0 and r.last_own_activity("claude_code:s1") is None   # not finished yet
    gate.set()
    wait_done(t)
    assert r.last_own_activity("claude_code:s1") == 10.0                   # the marker from before the refresh


def test_harness_done_and_error_events_feed_the_terminal_event():
    r = turns.TurnRunner(refresh=ok_refresh)
    t = r.start("claude_code:s1", command([{"line": '{"kind": "done", "cost_usd": 0.42}'}]), translate)
    assert drain(r, t)[-1] == {"kind": "done", "exit_code": 0, "cost_usd": 0.42, "indexed": True, "reason": None}
    t = r.start("claude_code:s2", command([{"line": '{"kind": "error", "message": "no credits"}'}]), translate)
    last = drain(r, t)[-1]
    assert last["kind"] == "error" and last["message"] == "no credits" and last["exit_code"] == 0


def test_replay_from_an_offset_and_the_bounded_buffer(monkeypatch):
    monkeypatch.setattr(turns, "EVENT_LIMIT", 5)
    r = turns.TurnRunner()
    lines = [{"line": json.dumps({"kind": "text", "text": str(i)})} for i in range(8)]
    t = r.start("claude_code:s1", command(lines), translate)
    wait_done(t)
    all_events = [ev for _, ev in r.events(t.id, 0) if ev is not None]
    assert all_events[0] == {"kind": "gap", "missed": 4}            # 9 events in total, the last 5 kept
    assert [e["text"] for e in all_events[1:-1]] == ["4", "5", "6", "7"]
    later = [ev for _, ev in r.events(t.id, 7) if ev is not None]
    assert [e["text"] for e in later[:-1]] == ["7"] and later[-1]["kind"] == "done"
    assert [ev for _, ev in r.events("nope")] == []


def test_translator_failure_is_reported_not_fatal():
    r = turns.TurnRunner()

    def bad(line):
        raise RuntimeError("parser bug")
    t = r.start("claude_code:s1", command([{"line": "x"}]), bad)
    assert drain(r, t)[-1]["message"] == "could not read the harness output: parser bug"


def test_unstartable_command_is_refused_with_500():
    r = turns.TurnRunner()
    with pytest.raises(turns.TurnRefused) as e:
        r.start("claude_code:s1", TurnCommand(argv=["/nonexistent/harness"], cwd=os.getcwd()), translate)
    assert e.value.status == 500 and r.running() == 0


def test_an_argument_with_a_nul_byte_is_refused_with_400():
    r = turns.TurnRunner()
    with pytest.raises(turns.TurnRefused) as e:
        r.start("claude_code:s1", TurnCommand(argv=[sys.executable, FAKE, "a\x00b"], cwd=os.getcwd()), translate)
    assert e.value.status == 400 and r.running() == 0


def test_finished_turns_are_forgotten_after_the_keep_window(monkeypatch):
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([]), translate)
    wait_done(t)
    t.finished -= turns.KEEP_FINISHED + 1
    r.start("claude_code:s2", command([]), translate)       # start prunes
    assert r.get(t.id) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal semantics")
def test_harness_runs_in_its_own_session():
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([{"sleep": 5}]), translate)
    assert os.getpgid(t.proc.pid) == t.proc.pid != os.getpgid(0)
    os.killpg(t.proc.pid, signal.SIGKILL)
    wait_done(t)


# ---- the refresh before the terminal event -------------------------------------------------------

def test_terminal_event_waits_for_the_refresh_and_reports_it():
    seen = {}

    def slow_refresh():
        seen["started"] = time.time()
        time.sleep(1.0)
        return {"ok": True, "busy": False}
    r = turns.TurnRunner(refresh=slow_refresh)
    t = r.start("claude_code:s1", command([{"line": '{"kind": "text", "text": "x"}'}]), translate)
    events = drain(r, t)
    assert events[-1]["indexed"] is True and events[-1]["reason"] is None
    assert t.finished - seen["started"] >= 0.9           # done was appended after the refresh returned


@pytest.mark.parametrize("result, indexed, reason", [
    ({"ok": False, "busy": True}, False, "indexer busy; the turn appears on the next refresh"),
    ({"ok": False, "busy": False, "error": "WARN: codex: failed"}, False, "WARN: codex: failed"),
    ({"ok": False, "busy": False}, False, "refresh reported failures"),
    (None, False, "refresh reported failures"),
])
def test_refresh_outcomes_are_reported(result, indexed, reason):
    r = turns.TurnRunner(refresh=lambda: result)
    t = r.start("claude_code:s1", command([]), translate)
    last = drain(r, t)[-1]
    assert last["kind"] == "done" and last["indexed"] is indexed and last["reason"] == reason


def test_refresh_exception_and_missing_refresh():
    def boom():
        raise RuntimeError("db locked")
    r = turns.TurnRunner(refresh=boom)
    t = r.start("claude_code:s1", command([]), translate)
    assert drain(r, t)[-1]["reason"] == "refresh failed: db locked"
    r = turns.TurnRunner()
    t = r.start("claude_code:s1", command([]), translate)
    assert drain(r, t)[-1]["reason"] == "no refresh configured"


def test_refresh_runs_even_when_the_turn_was_stopped():
    calls = []
    r = turns.TurnRunner(refresh=lambda: calls.append(1) or {"ok": True})
    t = r.start("claude_code:s1", command([{"sleep": 30}]), translate)
    r.stop(t.id)
    last = drain(r, t)[-1]
    assert calls == [1] and last["kind"] == "error" and last["indexed"] is True


def test_activity_is_sampled_before_the_output_drains():
    """A terminal write while the readers are still draining is not the browser's own."""
    marker, calls, release = [1.0], [], threading.Event()

    def slow_translate(line):
        release.wait(10)
        return translate(line)

    def activity():
        calls.append(marker[0])
        return marker[0]
    r = turns.TurnRunner(refresh=ok_refresh)
    t = r.start("claude_code:s1", command([{"line": '{"kind": "text", "text": "hi"}'}]), slow_translate,
                activity=activity)
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    marker[0] = 2.0                          # the terminal writes while the translator is still busy
    release.set()
    wait_done(t)
    assert t.activity_after == 1.0 and r.last_own_activity("claude_code:s1") == 1.0
