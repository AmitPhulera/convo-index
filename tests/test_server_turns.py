"""POST /api/conversation/<id>/turn, GET /api/turn/<id> (server-sent events) and DELETE, against
a live server and a fake `claude` installed on PATH: streaming while the harness runs, replay,
the loopback rule, every precondition, and the CSRF header on POST and DELETE."""
import http.client
import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from convo_index import db as dbm
from convo_index import server
from convo_index import turns
from convo_index.config import EMBED_DIM
from convo_index.model import Conversation, Message
from tests.conftest import make_settings
from tests.test_turns import FAKE

FAKE_JS = str(Path(__file__).with_name("fake_harness.js"))
SID = "0f1e2d3c-1234-4abc-8def-0123456789ab"
CID = f"claude_code:{SID}"


def install_fake_claude(bindir, monkeypatch):
    """`claude` on PATH that runs the fake harness: a Python script on POSIX; on Windows the npm
    layout (`claude.cmd` shim + `node_modules/.../cli.js`) so the node launcher is exercised."""
    bindir.mkdir(exist_ok=True)
    if os.name == "nt":
        if not shutil.which("node"):
            pytest.skip("node is needed for the Windows fake harness")
        script = bindir / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        script.parent.mkdir(parents=True)
        shutil.copy(FAKE_JS, script)
        (bindir / "claude.cmd").write_text('@ECHO off\r\n"%_prog%"  "%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n')
    else:
        shim = bindir / "claude"
        shim.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({FAKE!r}, run_name='__main__')\n")
        shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


def claude_lines(*texts, cost=0.05):
    """Fake stream-json lines: one assistant text message per text, then the result."""
    out = [{"type": "system", "subtype": "init", "tools": ["Read"], "mcp_servers": []}]
    out += [{"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}} for t in texts]
    out.append({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost, "num_turns": 1,
                "permission_denials": []})
    return [{"line": json.dumps(o)} for o in out]


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A seeded database, settings, a fake claude, a runner with a recorded refresh, and a live server."""
    proj = tmp_path / "proj"; proj.mkdir()
    transcript = tmp_path / "transcript.jsonl"; transcript.write_text("{}\n")
    old = time.time() - 3600
    os.utime(transcript, (old, old))
    path = tmp_path / "t.db"
    conn = dbm.connect(path); dbm.init_schema(conn)
    conv = Conversation(source="claude_code", native_id=SID, title="Kube fix", first_prompt="k8s",
                        source_ref=str(transcript), cwd=str(proj), start_ts="2026-06-16T00:00:00Z",
                        messages=[Message(seq=0, role="user", text="k8s", ts="2026-06-16T00:00:00Z")])
    dbm.upsert_conversation(conn, conv, "globex", "path", [], None, "1", lambda t: [[0.0] * EMBED_DIM for _ in t])
    other = Conversation(source="antigravity", native_id="t1", title="Other", first_prompt="x", source_ref="/x", cwd=str(proj),
                         start_ts="2026-06-16T00:00:00Z", messages=[Message(seq=0, role="user", text="x", ts="2026-06-16T00:00:00Z")])
    dbm.upsert_conversation(conn, other, "globex", "path", [], None, "1", lambda t: [[0.0] * EMBED_DIM for _ in t])
    conn.close()
    monkeypatch.setattr(server, "DB_PATH", path)
    st = make_settings(tmp_path)
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: st)
    install_fake_claude(tmp_path / "bin", monkeypatch)
    refreshes = []
    runner = turns.TurnRunner(timeout=20, max_concurrent=2, refresh=lambda: refreshes.append(1) or {"ok": True, "busy": False})
    monkeypatch.setattr(server, "RUNNER", runner)
    srv = server.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield type("Site", (), {"srv": srv, "port": srv.server_address[1], "settings": st, "proj": proj,
                            "transcript": transcript, "runner": runner, "refreshes": refreshes, "tmp": tmp_path})
    srv.shutdown()


def request(site, method, path, body=None, headers=None, csrf=True, token=True):
    conn = http.client.HTTPConnection("127.0.0.1", site.port, timeout=10)
    h = {"Host": f"127.0.0.1:{site.port}", **(headers or {})}
    if csrf and method in ("POST", "DELETE"):
        h[server.CSRF_HEADER] = "1"
    if token and "/turn" in path:
        h[server.TURN_TOKEN_HEADER] = server.turn_token()
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, (json.loads(data) if data[:1] in (b"{", b"[") else data)


def start(site, steps, prompt="what next?", edits=False, monkeypatch=None):
    os.environ["FAKE_HARNESS"] = json.dumps(steps)
    return request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": prompt, "edits": edits})


def sse_events(site, turn_id, after=0, stop_at_done=True):
    """Read the stream as a browser would: parse `data:` lines as they arrive."""
    conn = http.client.HTTPConnection("127.0.0.1", site.port, timeout=30)
    conn.request("GET", f"/api/turn/{turn_id}?after={after}&token={server.turn_token()}", headers={"Host": f"127.0.0.1:{site.port}"})
    res = conn.getresponse()
    assert res.status == 200 and res.getheader("Content-Type") == "text/event-stream"
    events, arrival = [], []
    while True:
        line = res.fp.readline()
        if not line:
            break
        if line.startswith(b"data: "):
            events.append(json.loads(line[6:]))
            arrival.append(time.time())
            if stop_at_done and events[-1]["kind"] in ("done", "error"):
                break
    conn.close()
    return events, arrival


def test_turn_streams_while_running_and_the_server_keeps_answering(site):
    status, body = start(site, claude_lines("first")[:2] + [{"sleep": 4}] + claude_lines("second")[1:])
    assert status == 200 and body["conversation_id"] == CID and body["done"] is False
    conn = http.client.HTTPConnection("127.0.0.1", site.port, timeout=30)
    conn.request("GET", f"/api/turn/{body['turn_id']}", headers={"Host": f"127.0.0.1:{site.port}",
                                                                 server.TURN_TOKEN_HEADER: server.turn_token()})
    res = conn.getresponse()
    first = None
    while first is None:
        line = res.fp.readline()
        if line.startswith(b"data: "):
            first = json.loads(line[6:])
    assert first == {"kind": "text", "text": "first"}
    assert site.runner.get(body["turn_id"]).proc.poll() is None        # received while the harness still runs
    status, conv = request(site, "GET", f"/api/conversation/{CID}")                # a concurrent request completes
    assert status == 200 and conv["active_turn"]["turn_id"] == body["turn_id"] and conv["turns_supported"] is True
    rest = []
    while True:
        line = res.fp.readline()
        if line.startswith(b"data: "):
            rest.append(json.loads(line[6:]))
            if rest[-1]["kind"] in ("done", "error"):
                break
    conn.close()
    assert [e["kind"] for e in rest] == ["text", "done"]
    assert rest[-1]["indexed"] is True and rest[-1]["cost_usd"] == 0.05 and site.refreshes == [1]
    status, conv = request(site, "GET", f"/api/conversation/{CID}")
    assert conv["active_turn"] is None


def test_prompt_travels_on_stdin_with_metacharacters_intact(site):
    prompt = 'say "hi"; $(rm -rf /) `x` ünï\nsecond line'
    captured = site.tmp / "stdin.txt"
    status, body = start(site, [{"stdin_to": str(captured)}] + claude_lines()[1:], prompt=prompt)
    assert status == 200
    events, _ = sse_events(site, body["turn_id"])
    assert events[-1]["kind"] == "done"
    assert captured.read_text(encoding="utf-8") == prompt                 # what the harness read on stdin
    cmd = server.turn_preconditions(CID, prompt, False, site.settings)[1]
    assert prompt not in " ".join(cmd.argv)


def test_a_follow_up_turn_is_not_mistaken_for_terminal_activity(site):
    status, body = start(site, [{"touch": str(site.transcript)}] + claude_lines("one"))   # the harness appends its turn
    assert status == 200
    sse_events(site, body["turn_id"])
    status, out = start(site, [{"touch": str(site.transcript)}] + claude_lines("two"))
    assert status == 200, out
    sse_events(site, out["turn_id"])
    time.sleep(0.05)
    os.utime(site.transcript, None)                                        # a change after the turn: someone else
    status, out = start(site, [])
    assert status == 423


def test_a_terminal_write_during_the_refresh_is_not_browser_owned(site):
    gate = threading.Event()
    site.runner.refresh = lambda: gate.wait(10) and {"ok": True, "busy": False}
    status, body = start(site, [{"touch": str(site.transcript)}] + claude_lines("one"))
    turn = site.runner.get(body["turn_id"])
    deadline = time.time() + 10
    while turn.activity_after is None and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.05)
    os.utime(site.transcript, None)                                        # someone writes while the index refreshes
    gate.set()
    sse_events(site, body["turn_id"])
    status, out = start(site, [])
    assert status == 423


@pytest.mark.parametrize("prompt", ["a\x00b", "bell\x07", "\x1b[31mred"])
def test_control_characters_in_the_prompt_are_refused(site, prompt):
    os.environ["FAKE_HARNESS"] = "[]"
    status, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": prompt})
    assert status == 400 and "control characters" in out["error"]


def test_a_lone_surrogate_in_the_prompt_is_refused(site):
    os.environ["FAKE_HARNESS"] = "[]"
    conn = http.client.HTTPConnection("127.0.0.1", site.port, timeout=10)
    conn.request("POST", f"/api/conversation/{CID}/turn", body=b'{"prompt": "a\\ud800b"}',
                 headers={"Host": f"127.0.0.1:{site.port}", server.CSRF_HEADER: "1",
                          server.TURN_TOKEN_HEADER: server.turn_token()})
    res = conn.getresponse(); body = json.loads(res.read()); conn.close()
    assert res.status == 400 and "Unicode" in body["error"] and site.runner.running() == 0


def test_oversized_or_negative_bodies_are_refused(site):
    for length in ("-5", str(server.MAX_TURN_BODY + 1)):
        conn = http.client.HTTPConnection("127.0.0.1", site.port, timeout=10)
        conn.putrequest("POST", f"/api/conversation/{CID}/turn", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{site.port}"); conn.putheader(server.CSRF_HEADER, "1")
        conn.putheader(server.TURN_TOKEN_HEADER, server.turn_token())
        conn.putheader("Content-Length", length); conn.endheaders()
        res = conn.getresponse(); body = res.read(); conn.close()
        assert res.status == 413 and "bytes" in json.loads(body)["error"]


def test_a_malformed_content_length_is_a_bad_request(site):
    conn = http.client.HTTPConnection("127.0.0.1", site.port, timeout=10)
    conn.putrequest("POST", f"/api/conversation/{CID}/turn", skip_host=True)
    conn.putheader("Host", f"127.0.0.1:{site.port}"); conn.putheader(server.CSRF_HEADER, "1")
    conn.putheader(server.TURN_TOKEN_HEADER, server.turn_token())
    conn.putheader("Content-Length", "abc"); conn.endheaders()
    res = conn.getresponse(); res.read(); conn.close()
    assert res.status == 400 and site.runner.running() == 0


def test_last_event_id_resumes_like_after(site):
    status, body = start(site, claude_lines("a", "b", "c"))
    sse_events(site, body["turn_id"])
    conn = http.client.HTTPConnection("127.0.0.1", site.port, timeout=10)
    conn.request("GET", f"/api/turn/{body['turn_id']}", headers={"Host": f"127.0.0.1:{site.port}", "Last-Event-ID": "2",
                                                                 server.TURN_TOKEN_HEADER: server.turn_token()})
    res = conn.getresponse(); raw = res.read().decode(); conn.close()
    assert [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: ")][0]["text"] == "c"
    assert "id: 3\n" in raw


def test_replay_from_an_offset(site):
    status, body = start(site, claude_lines("a", "b", "c"))
    events, _ = sse_events(site, body["turn_id"])
    assert [e.get("text", e["kind"]) for e in events] == ["a", "b", "c", "done"]
    later, _ = sse_events(site, body["turn_id"], after=2)
    assert [e.get("text", e["kind"]) for e in later] == ["c", "done"]
    assert request(site, "GET", "/api/turn/nope")[0] == 404
    assert request(site, "GET", f"/api/turn/{body['turn_id']}?after=x")[0] == 400


def test_one_turn_per_conversation_and_stop(site):
    status, body = start(site, [{"sleep": 30}])
    assert status == 200
    status, again = start(site, [])
    assert status == 409 and "already running" in again["error"]
    status, stopped = request(site, "DELETE", f"/api/turn/{body['turn_id']}")
    assert status == 200 and stopped["ok"] is True and stopped["stopped"] is True
    events, _ = sse_events(site, body["turn_id"])
    assert events[-1]["kind"] == "error" and events[-1]["message"] == "stopped" and events[-1]["indexed"] is True
    assert request(site, "DELETE", "/api/turn/nope")[0] == 404


def test_global_cap_is_429(site, monkeypatch):
    site.runner.max_concurrent = 0
    status, body = start(site, [])
    assert status == 429 and "0 turns" in body["error"]


def test_csrf_header_is_required_on_post_and_delete(site):
    os.environ["FAKE_HARNESS"] = "[]"
    assert request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"}, csrf=False)[0] == 403
    assert request(site, "DELETE", "/api/turn/x", csrf=False)[0] == 403
    assert site.runner.running() == 0


def test_the_turn_token_is_required_and_never_served(site):
    os.environ["FAKE_HARNESS"] = "[]"
    for method, path in (("POST", f"/api/conversation/{CID}/turn"), ("GET", "/api/turn/x"), ("DELETE", "/api/turn/x")):
        status, out = request(site, method, path, {"prompt": "x"} if method == "POST" else None, token=False)
        assert status == 403 and "turn token" in out["error"], (method, out)
        status, out = request(site, method, path, {"prompt": "x"} if method == "POST" else None, token=False,
                              headers={server.TURN_TOKEN_HEADER: "wrong"})
        assert status == 403
    assert request(site, "GET", f"/api/turn/x?token={server.turn_token()}", token=False)[0] == 404   # right token, no such turn
    status, cfg = request(site, "GET", "/api/config")
    assert status == 200 and server.turn_token() not in json.dumps(cfg)
    assert site.runner.running() == 0


def test_main_writes_the_token_after_binding_and_prints_it_only_to_a_terminal(tmp_path, monkeypatch, capsys):
    cfg = {"server": {"host": "127.0.0.1", "port": 0, "allowed_hosts": []}, "embeddings": {"dim": 8}}
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(server, "prepare", lambda c: None)
    token_file = tmp_path / "turn-token"
    monkeypatch.setattr(server, "turn_token_path", lambda settings=None: token_file)

    class Fake:
        server_address = ("127.0.0.1", 4242)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            closed.append(1)
    closed = []
    monkeypatch.setattr(server, "make_server", lambda host, port, extra_hosts=(): Fake())
    monkeypatch.setattr(server.signal, "signal", lambda *a: None)
    for tty, expect in ((True, f"#turns={server.turn_token()}"), (False, f"(turn token in {token_file})")):
        monkeypatch.setattr(sys.stdout, "isatty", lambda t=tty: t)
        token_file.unlink(missing_ok=True)
        assert server.main() == 0
        out = capsys.readouterr().out
        assert expect in out and token_file.read_text() == server.turn_token()
        assert (server.turn_token() in out) is tty and "server stopped" in out
    assert closed == [1, 1]                                            # the port is released on the way out


def test_the_turn_token_is_written_for_the_cli_only(site):
    path = server.write_turn_token(site.settings)
    assert path == site.settings.db_path().with_name("turn-token")
    assert server.read_turn_token(site.settings) == server.turn_token()
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert server.advertised_url("127.0.0.1", 9753, "abc") == "http://127.0.0.1:9753#turns=abc"
    assert server.advertised_url("0.0.0.0", 9753) == "http://127.0.0.1:9753"
    assert server.read_turn_token(make_settings(site.tmp / "elsewhere")) is None


def test_a_restarted_server_keeps_a_private_well_formed_token(site, monkeypatch):
    path = site.settings.db_path().with_name("turn-token")
    saved = "A" * 32
    path.write_text(saved)
    os.chmod(path, 0o600)
    monkeypatch.setattr(server, "_turn_token", None)
    assert server.adopt_turn_token(site.settings) == saved and server.turn_token() == saved
    for content, mode in (("short", 0o600), ("B" * 32, 0o644)):
        if os.name == "nt" and mode == 0o644:
            continue
        path.write_text(content)
        os.chmod(path, mode)
        monkeypatch.setattr(server, "_turn_token", None)
        assert server.adopt_turn_token(site.settings) is None and server.turn_token() not in (content, saved)
    path.unlink()
    assert server.adopt_turn_token(site.settings) is None


def test_up_waits_briefly_for_the_token_file(monkeypatch):
    from convo_index import cli
    reads = iter([None, None, "tok"])
    fake = type("S", (), {"read_turn_token": staticmethod(lambda settings: next(reads))})
    assert cli._turn_token_of_running_server(fake, None, seconds=5) == "tok"
    fake.read_turn_token = staticmethod(lambda settings: None)
    assert cli._turn_token_of_running_server(fake, None, seconds=0.2) is None


@pytest.mark.parametrize("body, status, needle", [
    ({"prompt": ""}, 400, "prompt is required"),
    ({"prompt": "x", "edits": "yes"}, 400, "edits must be a JSON boolean"),
    ("not an object", 400, "body must be a JSON object"),
])
def test_bad_bodies(site, body, status, needle):
    got, out = request(site, "POST", f"/api/conversation/{CID}/turn", body)
    assert got == status and needle in out["error"]


def test_preconditions(site):
    os.environ["FAKE_HARNESS"] = "[]"
    st = site.settings
    assert request(site, "POST", "/api/conversation/claude_code:nope/turn", {"prompt": "x"})[0] == 404
    got, out = request(site, "POST", "/api/conversation/antigravity:t1/turn", {"prompt": "x"})
    assert got == 400 and "no browser turns for antigravity" in out["error"]
    st["sources"]["claude_code"]["enabled"] = False
    got, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"})
    assert got == 403 and "disabled" in out["error"]
    st["sources"]["claude_code"]["enabled"] = True
    st["sources"]["claude_code"]["exclude_cwd"] = [str(site.proj)]
    got, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"})
    assert got == 403 and "exclude_cwd" in out["error"]
    st["sources"]["claude_code"]["exclude_cwd"] = []
    st["ui"]["turns"]["enabled"] = False
    got, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"})
    assert got == 403 and "disabled in settings" in out["error"]
    assert request(site, "GET", f"/api/conversation/{CID}")[1]["turns_supported"] is False
    st["ui"]["turns"]["enabled"] = True
    os.utime(site.transcript, None)                                    # just changed: open in a terminal?
    got, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"})
    assert got == 423 and "changed 0 s ago" in out["error"]
    old = time.time() - 3600
    os.utime(site.transcript, (old, old))
    site.proj.rename(site.tmp / "gone")
    got, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"})
    assert got == 412 and "no longer exists" in out["error"]
    (site.tmp / "gone").rename(site.proj)
    assert site.runner.running() == 0


def test_inferred_cwd_is_refused(site):
    conn = dbm.connect(server.DB_PATH)
    conn.execute("UPDATE conversations SET metadata=? WHERE id=?", (json.dumps({"cwd_source": "tool_args"}), CID))
    conn.commit(); conn.close()
    got, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"})
    assert got == 412 and "inferred" in out["error"]


def test_missing_harness_binary_is_424(site, monkeypatch):
    monkeypatch.setenv("PATH", str(site.tmp / "empty"))
    got, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"})
    assert got == 424 and "not installed" in out["error"]


def test_a_session_the_harness_cannot_continue_is_412(site, monkeypatch):
    seen = {}

    def refuse(cid, prompt, cwd, **kw):
        seen.update(kw)
        raise ValueError("the session file no longer holds this session")
    monkeypatch.setattr(server, "turn_command", refuse)
    got, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"})
    assert got == 412 and "no longer holds" in out["error"]
    assert seen["locator"] == str(site.transcript)          # the indexed file reaches the adapter


def test_edits_position_is_passed_through(site):
    captured = site.tmp / "argv.txt"
    status, body = start(site, [{"argv_to": str(captured)}] + claude_lines()[1:], edits=True)
    assert status == 200 and body["edits"] is True
    sse_events(site, body["turn_id"])
    argv = captured.read_text(encoding="utf-8").splitlines()
    assert "--permission-mode" in argv and "acceptEdits" in argv and "Read,Glob,Grep,Edit,Write,MultiEdit,Bash" in argv


# ---- the loopback rule -------------------------------------------------------------------------

def test_turns_refusal_for_names():
    ok = ("127.0.0.1", 1), ("127.0.0.1", 2)
    assert server.turns_refusal(*ok, {"Host": "localhost:9753"}) is None
    assert server.turns_refusal(*ok, {"Host": "127.0.0.1:9753", "Origin": "http://[::1]:9753"}) is None
    assert "Host: box.lan" in server.turns_refusal(*ok, {"Host": "box.lan"})            # a same-host reverse proxy
    assert "Origin: https://box.lan" in server.turns_refusal(*ok, {"Host": "localhost:9753", "Origin": "https://box.lan"})
    assert "Host:" in server.turns_refusal(*ok, {"Host": "not a host"})
    for name in server.PROXY_HEADERS:     # a proxy that rewrote Host to the upstream still leaves these
        assert name in server.turns_refusal(*ok, {"Host": "127.0.0.1:9753", name: "x"})


def test_proxied_name_refuses_turns_but_serves_the_rest(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = dbm.connect(path); dbm.init_schema(conn); conn.close()
    monkeypatch.setattr(server, "DB_PATH", path)
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: make_settings(tmp_path))
    monkeypatch.setattr(server, "RUNNER", turns.TurnRunner())
    srv = server.make_server("127.0.0.1", 0, extra_hosts=("box.lan",))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        site = type("S", (), {"port": srv.server_address[1]})
        h = {"Host": "box.lan", "Origin": "https://box.lan"}
        assert request(site, "GET", "/api/sources", headers=h)[0] == 200
        status, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"}, headers=h)
        assert status == 403 and "localhost" in out["error"]
        assert request(site, "GET", "/api/turn/x", headers=h)[0] == 403
        h = {"Host": f"127.0.0.1:{site.port}", "X-Forwarded-For": "203.0.113.9"}   # nginx with the upstream as Host
        assert request(site, "GET", "/api/sources", headers=h)[0] == 200
        status, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"}, headers=h)
        assert status == 403 and "proxy" in out["error"]
    finally:
        srv.shutdown()


def test_turns_refusal_for_binds_and_peers():
    assert server.turns_refusal(("127.0.0.1", 1), ("127.0.0.1", 2)) is None
    assert server.turns_refusal(("::1", 1, 0, 0), ("::1", 2, 0, 0)) is None
    assert server.turns_refusal(("::1", 1, 0, 0), ("::ffff:127.0.0.1", 2, 0, 0)) is None
    assert "bound to 0.0.0.0" in server.turns_refusal(("0.0.0.0", 1), ("127.0.0.1", 2))
    assert "bound to ::" in server.turns_refusal(("::", 1, 0, 0), ("::1", 2, 0, 0))
    assert "bound to 192.168.1.5" in server.turns_refusal(("192.168.1.5", 1), ("127.0.0.1", 2))
    assert "request from 10.0.0.9" in server.turns_refusal(("127.0.0.1", 1), ("10.0.0.9", 2))
    assert "request from" in server.turns_refusal(("127.0.0.1", 1), ("not an ip", 2))


def test_wildcard_bind_refuses_turns_but_serves_the_rest(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = dbm.connect(path); dbm.init_schema(conn); conn.close()
    monkeypatch.setattr(server, "DB_PATH", path)
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: make_settings(tmp_path))
    monkeypatch.setattr(server, "RUNNER", turns.TurnRunner())
    srv = server.make_server("0.0.0.0", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        site = type("S", (), {"port": port})
        status, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"})
        assert status == 403 and "loopback" in out["error"]
        assert request(site, "GET", "/api/turn/x")[0] == 403
        assert request(site, "DELETE", "/api/turn/x")[0] == 403
        assert request(site, "GET", "/api/sources")[0] == 200
    finally:
        srv.shutdown()


def test_non_loopback_peer_is_refused(site, monkeypatch):
    """The socket is loopback; the handler sees a LAN peer (as behind a port forward)."""
    seen = {}
    orig = server.turns_refusal

    def spy(server_address, client_address, headers=None):
        seen["peer"] = client_address
        return orig(server_address, ("10.1.2.3", client_address[1]), headers)
    monkeypatch.setattr(server, "turns_refusal", spy)
    status, out = request(site, "POST", f"/api/conversation/{CID}/turn", {"prompt": "x"})
    assert status == 403 and "request from 10.1.2.3" in out["error"] and seen["peer"][0] == "127.0.0.1"


# ---- the server's runner --------------------------------------------------------------------------

def test_concurrent_first_requests_share_one_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: make_settings(tmp_path))
    monkeypatch.setattr(server, "RUNNER", None)
    seen, barrier = [], threading.Barrier(16)

    def grab():
        barrier.wait()
        seen.append(server._runner())
    ts = [threading.Thread(target=grab) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(seen) == 16 and all(r is seen[0] for r in seen)
    assert seen[0].max_concurrent == 2 and seen[0].timeout == 1200 and seen[0].refresh is server._refresh_after_turn


def test_stop_turns_ends_running_harnesses_and_closes_admission(site, monkeypatch):
    monkeypatch.setattr(server, "_stopping", False)
    status, body = start(site, [{"sleep": 60}])
    assert status == 200
    assert server.stop_turns() == 1
    turn = site.runner.get(body["turn_id"])
    assert turn.done and turn.proc.poll() is not None
    status, out = start(site, [])
    assert status == 503 and "shutting down" in out["error"]
    assert server.stop_turns() == 0


def test_a_runner_created_after_stop_is_born_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: make_settings(tmp_path))
    monkeypatch.setattr(server, "RUNNER", None)
    monkeypatch.setattr(server, "_stopping", False)
    assert server.stop_turns() == 0
    assert server._runner().closed is True


def shutdown(site, port=None, headers=None, csrf=True, token=True):
    conn = http.client.HTTPConnection("127.0.0.1", port or site.port, timeout=10)
    h = {"Host": f"127.0.0.1:{port or site.port}", **(headers or {})}
    if csrf:
        h[server.CSRF_HEADER] = "1"
    if token:
        h[server.TURN_TOKEN_HEADER] = server.turn_token()
    conn.request("POST", "/api/shutdown", body="{}", headers=h)
    res = conn.getresponse(); body = json.loads(res.read()); conn.close()
    return res.status, body


@pytest.mark.parametrize("kw, needle", [
    ({"token": False}, "turn token"),
    ({"csrf": False}, ""),
    ({"headers": {"X-Forwarded-For": "10.0.0.1"}}, "proxy"),
])
def test_shutdown_is_refused_without_the_token_csrf_or_through_a_proxy(site, kw, needle):
    status, body = shutdown(site, **kw)
    assert status == 403 and needle in body["error"]
    assert request(site, "GET", "/api/config")[0] == 200            # still serving


def test_shutdown_from_a_remote_peer_is_refused():
    assert "only from this machine" in server.shutdown_refusal(("192.168.1.5", 4000), {})
    assert server.shutdown_refusal(("127.0.0.1", 4000), {server.TURN_TOKEN_HEADER: server.turn_token()}) is None


def test_shutdown_ends_running_turns_then_the_serve_loop(site, monkeypatch):
    monkeypatch.setattr(server, "_stopping", False)                 # restored for the tests after this one
    status, body = start(site, claude_lines("one")[:2] + [{"sleep": 30}])
    assert status == 200
    turn = site.runner.get(body["turn_id"])
    srv = server.make_server("127.0.0.1", 0)
    loop = threading.Thread(target=srv.serve_forever, daemon=True)
    loop.start()
    assert shutdown(site, port=srv.server_address[1]) == (202, {"ok": True, "stopping": True})
    loop.join(20)
    assert not loop.is_alive()
    deadline = time.time() + 20
    while not turn.done and time.time() < deadline:
        time.sleep(0.05)
    assert turn.done and site.runner.closed
    srv.server_close()


def stop_cli(site, port, monkeypatch):
    from convo_index import cli
    from convo_index import settings as S
    st = make_settings(site.tmp, server={"host": "127.0.0.1", "port": port})
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    return cli.main(["stop", "--timeout", "10"]), st


def test_stop_when_nothing_is_listening(site, monkeypatch, capsys):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    assert stop_cli(site, port, monkeypatch)[0] == 0
    assert "no server is running" in capsys.readouterr().out


def test_stop_with_a_stale_token_is_refused(site, monkeypatch, capsys):
    site.settings.db_path().with_name("turn-token").write_text("stale" * 8)
    assert stop_cli(site, site.port, monkeypatch)[0] == 1
    assert "refused to stop (403)" in capsys.readouterr().err
    assert request(site, "GET", "/api/config")[0] == 200            # still serving


def test_stop_ends_a_live_server(site, monkeypatch, capsys):
    monkeypatch.setattr(server, "_stopping", False)
    server.write_turn_token(site.settings)
    srv = server.make_server("127.0.0.1", 0)

    def serve():                     # what main() does: serve, then release the port
        srv.serve_forever()
        srv.server_close()
    loop = threading.Thread(target=serve, daemon=True)
    loop.start()
    assert stop_cli(site, srv.server_address[1], monkeypatch)[0] == 0
    loop.join(10)
    assert not loop.is_alive() and "server stopped" in capsys.readouterr().out


# ---- the MCP tool allowlist for Edit-mode turns --------------------------------------------------

@pytest.fixture
def mcp_home(site, monkeypatch):
    home = site.tmp / "claude-home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "atlassian": {"type": "http", "url": "https://mcp.example/secret-path", "headers": {"Authorization": "Bearer t"}},
        "local": {"command": "local-mcp"}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    conn = dbm.connect(server.DB_PATH)
    conv = Conversation(source="claude_code", native_id="11111111-2222-4333-8444-555555555555", title="Jira", first_prompt="j",
                        source_ref="/x", cwd=str(site.proj), start_ts="2026-06-16T00:00:00Z",
                        messages=[Message(seq=0, role="user", text="j", ts="2026-06-16T00:00:00Z"),
                                  Message(seq=1, role="assistant", kind="tool_call", text='[mcp__atlassian__getJiraIssue] {"key": "X-1"}', ts="2026-06-16T00:00:01Z"),
                                  Message(seq=2, role="assistant", kind="tool_call", text="[mcp__claude_ai_Slack__post] {}", ts="2026-06-16T00:00:02Z")])
    dbm.upsert_conversation(conn, conv, "globex", "path", [], None, "1", lambda t: [[0.0] * EMBED_DIM for _ in t])
    conn.close()
    return home


def test_tools_endpoint_lists_servers_without_their_secrets(site, mcp_home):
    status, body = request(site, "GET", "/api/turns/tools")
    assert status == 200
    assert body["servers"] == [{"name": "atlassian", "type": "http"}, {"name": "local", "type": "stdio"}]
    assert body["allowed"] == []
    assert body["seen"] == {"atlassian": ["getJiraIssue"]}          # an account connector's call is not a configured server
    status, raw = request(site, "GET", "/api/turns/tools", token=False)
    assert status == 403
    _, text = request(site, "GET", "/api/turns/tools")
    assert "secret-path" not in json.dumps(text) and "Bearer" not in json.dumps(text)


def test_tools_endpoint_validates_and_saves(site, mcp_home):
    status, body = request(site, "POST", "/api/turns/tools", {"mcp_tools": ["mcp__atlassian__*", "mcp__slack__post"]})
    assert status == 400 and "mcp__slack__post" in body["error"]
    status, body = request(site, "POST", "/api/turns/tools", {"mcp_tools": ["mcp__atlassian__getJiraIssue"]}, csrf=False)
    assert status == 403
    status, body = request(site, "POST", "/api/turns/tools", {"mcp_tools": ["mcp__atlassian__getJiraIssue"]}, token=False)
    assert status == 403
    status, body = request(site, "POST", "/api/turns/tools", {"mcp_tools": ["mcp__atlassian__getJiraIssue"]})
    assert status == 200 and body["allowed"] == ["mcp__atlassian__getJiraIssue"]
    assert request(site, "GET", f"/api/conversation/{CID}")[1]["edit_mcp_tools"] == 1   # the Tools button's count
    assert request(site, "GET", "/api/turns/tools")[1]["allowed"] == ["mcp__atlassian__getJiraIssue"]


def test_an_edit_turn_gets_the_allowlist_and_a_read_only_turn_does_not(site, mcp_home):
    request(site, "POST", "/api/turns/tools", {"mcp_tools": ["mcp__atlassian__*"]})
    captured = site.tmp / "argv.txt"
    status, body = start(site, [{"argv_to": str(captured)}] + claude_lines()[1:], edits=True)
    assert status == 200
    sse_events(site, body["turn_id"])
    argv = captured.read_text(encoding="utf-8").splitlines()
    assert argv[argv.index("--allowedTools") + 1] == "Bash,mcp__atlassian" and "--mcp-config" in argv
    os.utime(site.transcript, (time.time() - 3600, time.time() - 3600))   # not "recent activity" for the next turn
    status, body = start(site, [{"argv_to": str(captured)}] + claude_lines()[1:], edits=False)
    assert status == 200
    sse_events(site, body["turn_id"])
    argv = captured.read_text(encoding="utf-8").splitlines()
    assert "--mcp-config" not in argv and "--allowedTools" not in argv and "--safe-mode" in argv


# ---- handoffs ---------------------------------------------------------------------------------

def handoff(site, path, body, token=True):
    headers = {server.TURN_TOKEN_HEADER: server.turn_token()} if token else {}
    return request(site, "POST", path, body, headers=headers, token=False)


def test_a_handoff_is_prepared_then_started_and_links_the_new_conversation(site):
    status, prep = handoff(site, f"/api/conversation/{CID}/handoff", {"kind": "review"})
    assert status == 200 and prep["prompt"].startswith("You are reviewing") and prep["digest_status"] is None
    assert "claude_code" in prep["targets"] and "hosted_processing = ask" in prep["send_refusal"]
    body = {"preview_id": prep["preview_id"], "target": "claude_code", "prompt": prep["prompt"], "edits": True}
    status, refused = handoff(site, "/api/handoff/start", body)
    assert status == 403 and "hosted_processing = ask" in refused["error"] and "--start" not in refused["error"]
    site.settings["privacy"]["hosted_processing"] = "allow"
    new = "9a8b7c6d-1234-4abc-8def-0123456789ab"
    os.environ["FAKE_HARNESS"] = json.dumps([{"argv_to": str(site.tmp / "argv.txt")},
                                             {"line": json.dumps({"type": "system", "subtype": "init", "session_id": new})}]
                                            + claude_lines("second opinion: looks fine")[1:])
    status, snap = handoff(site, "/api/handoff/start", body)
    assert status == 200 and snap["turn_id"] and snap["edits"] is False             # a review never edits
    events, _ = sse_events(site, snap["turn_id"])
    assert {"kind": "session", "id": f"claude_code:{new}", "linked": True} in events
    assert {"kind": "text", "text": "second opinion: looks fine"} in events and events[-1]["kind"] == "done"
    argv = (site.tmp / "argv.txt").read_text().splitlines()
    assert len(argv[argv.index("--session-id") + 1]) == 36 and "--add-dir" in argv     # our id; the copy's directory, from the server
    copy_dir = argv[argv.index("--add-dir") + 1]
    assert (Path(copy_dir) / "transcript.md").exists() and Path(copy_dir).parent.name == "handoffs"
    status, conv = request(site, "GET", f"/api/conversation/{CID}")
    assert conv["links"] == [{"id": f"claude_code:{new}", "relation": "review_of", "direction": "to", "title": None}]
    assert "claude_code" in conv["handoff_targets"]


def test_handoff_requests_are_guarded_and_checked(site):
    assert handoff(site, f"/api/conversation/{CID}/handoff", {"kind": "review"}, token=False)[0] == 403
    assert handoff(site, f"/api/conversation/{CID}/handoff", {"kind": "fork"})[0] == 400
    assert handoff(site, "/api/conversation/claude_code:nope/handoff", {"kind": "review"})[0] == 404
    status, body = handoff(site, "/api/handoff/start", {"preview_id": "made-up", "target": "claude_code", "prompt": "x"})
    assert status == 404 and "prepare it again" in body["error"]
    site.settings["privacy"]["hosted_processing"] = "allow"
    pid = handoff(site, f"/api/conversation/{CID}/handoff", {"kind": "continue"})[1]["preview_id"]
    start = lambda **kw: handoff(site, "/api/handoff/start", {"preview_id": pid, "target": "claude_code", "prompt": "go", **kw})
    assert start(target="codex")[0] == 400
    assert start(prompt="use AKIAABCDEFGHIJKLMNOP")[0] == 409                        # a credential needs confirming
    assert start(prompt="bad \x00 prompt")[0] == 400
    os.environ["FAKE_HARNESS"] = json.dumps(claude_lines("ok"))
    status, snap = start(prompt="use AKIAABCDEFGHIJKLMNOP", ignore_secrets=True)
    assert status == 200
    sse_events(site, snap["turn_id"])
    site.settings["sources"]["claude_code"] = {"enabled": False, "exclude_cwd": []}
    status, body = handoff(site, f"/api/conversation/{CID}/handoff", {"kind": "review"})
    assert status == 403 and "disabled" in body["error"]


def test_a_continue_preview_carries_the_note_or_says_why_not(site, monkeypatch):
    from convo_index import handoff as HO
    monkeypatch.setattr(HO, "digest", lambda msgs, title, settings: (None, "the summary backend did not answer within 90 s"))
    status, prep = handoff(site, f"/api/conversation/{CID}/handoff", {"kind": "continue"})
    assert status == 200 and prep["digest_status"].startswith("the summary backend did not answer")
    assert "None could be written (the summary backend did not answer" in prep["prompt"]


def test_a_bad_preview_id_is_a_400(site):
    for pid in ([], {}, 3, None):
        assert handoff(site, "/api/handoff/start", {"preview_id": pid, "target": "claude_code", "prompt": "x"})[0] == 400


def test_a_conversation_deleted_during_prepare_leaves_no_copy(site, monkeypatch):
    from convo_index import handoff as HO
    real = HO.write_transcript

    def then_deleted(conn, cid, directory, settings=None, stats=None):
        path = real(conn, cid, directory, settings, stats=stats)
        dbm.delete_conversation(dbm.connect(site.tmp / "t.db"), cid)      # a delete that lands in between
        return path
    monkeypatch.setattr(HO, "write_transcript", then_deleted)
    status, _ = handoff(site, f"/api/conversation/{CID}/handoff", {"kind": "review"})
    assert status == 404 and not list((site.tmp / "handoffs").glob("*"))
