import json
from urllib.parse import quote

from convo_index import db as dbm
from convo_index import server
from convo_index.config import EMBED_DIM
from convo_index.model import Conversation, Message, KIND_TOOL_CALL


def _seed(path, monkeypatch):
    conn = dbm.connect(path); dbm.init_schema(conn)
    conv = Conversation(source="claude_code", native_id="s1", title="Kube fix",
                        first_prompt="kubernetes deployment", source_ref="/a", cwd="/x", git_branch="m",
                        start_ts="2026-06-16T00:00:00Z", end_ts="2026-06-16T01:00:00Z",
                        messages=[Message(seq=0, role="user", text="kubernetes deployment failing",
                                          ts="2026-06-16T00:00:00Z"),
                                  Message(seq=1, role="assistant", text="[bash] kubectl get pods",
                                          kind=KIND_TOOL_CALL)])
    dbm.upsert_conversation(conn, conv, "globex", "path", ["SAAS-1"], None, "1", lambda t: [[0.0] * EMBED_DIM for _ in t])
    conn.execute("INSERT INTO legacy_ids(legacy, id) VALUES('s1', 'claude_code:s1')"); conn.commit()
    conn.close()
    monkeypatch.setattr(server, "DB_PATH", path)
    monkeypatch.setattr(server, "embed", lambda texts: [None])


def test_handle_search(tmp_path, monkeypatch):
    _seed(tmp_path / "t.db", monkeypatch)
    data = json.loads(server.handle_search({"q": ["kubernetes"], "mode": ["keyword"]}))
    assert data[0]["id"] == "claude_code:s1" and data[0]["source"] == "claude_code"


def test_handle_search_source_filter(tmp_path, monkeypatch):
    _seed(tmp_path / "t.db", monkeypatch)
    assert json.loads(server.handle_search({"q": [""], "mode": ["hybrid"], "source": ["opencode"]})) == []
    assert len(json.loads(server.handle_search({"q": [""], "mode": ["hybrid"], "source": ["claude_code"]}))) == 1
    assert len(json.loads(server.handle_search({"q": [""], "mode": ["hybrid"], "source": ["opencode,claude_code"]}))) == 1
    assert json.loads(server.handle_search({"q": [""], "mode": ["hybrid"], "source": ["opencode,codex"]})) == []


def test_handle_sources(tmp_path, monkeypatch):
    _seed(tmp_path / "t.db", monkeypatch)
    assert json.loads(server.handle_sources()) == ["claude_code"]


def test_handle_conversation(tmp_path, monkeypatch):
    _seed(tmp_path / "t.db", monkeypatch)
    data = json.loads(server.handle_conversation("claude_code:s1"))
    assert data["title"] == "Kube fix" and data["id"] == "claude_code:s1"
    assert data["resume_cmd"] == "claude --resume s1"
    assert [(m["kind"], m["text"]) for m in data["messages"]] == [
        ("text", "kubernetes deployment failing"), ("tool_call", "[bash] kubectl get pods")]


def test_handle_conversation_resolves_legacy_id(tmp_path, monkeypatch):
    _seed(tmp_path / "t.db", monkeypatch)
    data = json.loads(server.handle_conversation("s1"))
    assert data["id"] == "claude_code:s1"


def test_handle_conversation_missing(tmp_path, monkeypatch):
    _seed(tmp_path / "t.db", monkeypatch)
    assert server.handle_conversation("claude_code:nope") is None


def test_segment_decodes_encoded_ids():
    assert server._segment("/api/conversation/" + quote("claude_code:s1", safe=""), "/api/conversation/") == "claude_code:s1"


def test_handle_hide_and_search_filter(tmp_path, monkeypatch):
    _seed(tmp_path / "t.db", monkeypatch)
    server.handle_hide("claude_code:s1", True)
    assert json.loads(server.handle_search({"q": [""], "mode": ["hybrid"]})) == []
    shown = json.loads(server.handle_search({"q": [""], "mode": ["hybrid"], "hidden": ["1"]}))
    assert [r["id"] for r in shown] == ["claude_code:s1"] and shown[0]["hidden"] is True


def test_handle_delete_removes_and_blocklists(tmp_path, monkeypatch):
    _seed(tmp_path / "t.db", monkeypatch)
    server.handle_delete("claude_code:s1")
    assert server.handle_conversation("claude_code:s1") is None
    conn = dbm.connect(tmp_path / "t.db")
    assert dbm.get_deleted_ids(conn) == {"claude_code:s1"}


def test_run_indexer_reports_busy_failure_and_totals(monkeypatch):
    class R:
        def __init__(self, rc, out="", err=""): self.returncode, self.stdout, self.stderr = rc, out, err
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: R(server.EXIT_LOCKED))
    assert server._run_indexer() == {"ok": False, "busy": True, "totals": None}
    monkeypatch.setattr(server.subprocess, "run",
                        lambda *a, **k: R(0, 'claude_code: ...\n{"indexed": 3, "failed": 0}\n'))
    r = server._run_indexer()
    assert r["ok"] is True and r["busy"] is False and r["totals"]["indexed"] == 3
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: R(1, '{"indexed": 0, "failed": 1}', "WARN: x"))
    r = server._run_indexer()
    assert r["ok"] is False and r["totals"]["failed"] == 1 and "WARN" in r["error"]


def test_first_run_serve_init_with_missing_data_dir(tmp_path, monkeypatch):
    p = tmp_path / "no" / "such" / "dir" / "convos.db"
    conn = dbm.connect(p); dbm.init_schema(conn, p); conn.close()
    assert p.exists()


def test_handle_config_exposes_labels_and_ticket_url(monkeypatch, tmp_path):
    from tests.conftest import make_settings
    st = make_settings(tmp_path)
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: st)
    data = json.loads(server.handle_config())
    assert data["category_labels"] == ["globex", "personal"]
    assert data["ticket_url"].endswith("{key}")



def test_start_command_follows_the_login_service(monkeypatch):
    from convo_index import schedule as SCH
    monkeypatch.setattr(SCH, "status", lambda **k: {"installed": True})
    assert server.start_command() == "convo-index service install"
    assert json.loads(server.handle_config())["start_command"] == "convo-index service install"
    for installed in (False, None):                  # None: cron, where nothing says if it is there
        monkeypatch.setattr(SCH, "status", lambda installed=installed, **k: {"installed": installed})
        assert server.start_command() == "convo-index serve"


def test_start_command_falls_back_when_the_service_check_fails(monkeypatch):
    from convo_index import schedule as SCH
    def boom(**k):
        raise ValueError("config file does not exist")
    monkeypatch.setattr(SCH, "status", boom)
    assert server.start_command() == "convo-index serve"

def test_semantic_search_disabled_on_fingerprint_mismatch(tmp_path, monkeypatch):
    real_embed = server.embed
    _seed(tmp_path / "t.db", monkeypatch)
    from tests.conftest import make_settings
    st = make_settings(tmp_path)
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: st)
    monkeypatch.setattr(server, "embed", real_embed)   # the real embed() with a fake embedder
    class Emb:
        fingerprint = "fp-new"
        def embed(self, texts, purpose="document"):
            from convo_index.providers.base import EmbedResult
            return EmbedResult(vectors=[[0.0] * 768 for _ in texts], fingerprint="fp-new")
    monkeypatch.setattr(server.R, "make_embedder", lambda *a, **k: Emb())
    conn = dbm.connect(tmp_path / "t.db"); dbm.set_meta(conn, "embedding_fingerprint", "fp-old"); conn.close()
    from convo_index import server as srv
    ok, reason = srv.semantic_status()
    assert ok is False and "reembed" in reason
    assert srv.embed(["q"]) == [None]
    assert json.loads(srv.handle_config())["semantic_search"] is False
    conn = dbm.connect(tmp_path / "t.db"); dbm.set_meta(conn, "embedding_fingerprint", "fp-new"); conn.close()
    assert srv.semantic_status() == (True, None) and srv.embed(["q"]) == [[0.0] * 768]


def test_tag_endpoint_and_config_groups(tmp_path, monkeypatch):
    from tests.conftest import make_settings
    from convo_index import tags as T
    st = make_settings(tmp_path)
    _seed(tmp_path / "t.db", monkeypatch)
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: st)
    conn = dbm.connect(tmp_path / "t.db"); T.sync_definitions(conn, st); conn.close()
    cfg = json.loads(server.handle_config())
    assert list(cfg["tag_groups"]) == ["workspace"] and [t["name"] for t in cfg["tag_groups"]["workspace"]["tags"]] == ["globex", "personal"]
    out = json.loads(server.handle_tag("claude_code:s1", {"group": "workspace", "tag": "personal"}))
    assert out["assignments"] == {"workspace": ["personal"]}
    assert out["decisions"] == [{"group": "workspace", "tag": "personal", "decision": "accept"}]
    conv = json.loads(server.handle_conversation("claude_code:s1"))
    assert conv["tags"]["assignments"] == {"workspace": ["personal"]} and conv["category"] == "personal"
    out = json.loads(server.handle_tag("claude_code:s1", {"group": "workspace", "tag": "personal", "decision": "clear"}))
    assert out["decisions"] == []
    assert server.handle_tag("claude_code:nope", {"group": "workspace", "tag": "personal"}) is None
    import pytest
    with pytest.raises(LookupError):
        server.handle_tag("claude_code:s1", {"group": "workspace", "tag": "bogus"})
    with pytest.raises(LookupError):
        server.handle_tag("claude_code:s1", {"group": "workspace", "tag": "personal", "decision": "approve"})
    with pytest.raises(LookupError):
        server.handle_tag("claude_code:s1", ["not", "an", "object"])
    res = json.loads(server.handle_search({"q": [""], "mode": ["hybrid"], "tag": ["workspace:personal"]}))
    assert res == [] or all("personal" in r["tags"].get("workspace", []) for r in res)


def test_reference_endpoint_validates(tmp_path, monkeypatch):
    _seed(tmp_path / "t.db", monkeypatch)
    import pytest
    with pytest.raises(LookupError):
        server.handle_reference("claude_code:s1", ["nope"])
    with pytest.raises(LookupError):
        server.handle_reference("claude_code:s1", {"kind": "bogus", "key": "x"})
    with pytest.raises(LookupError):
        server.handle_reference("claude_code:s1", {"kind": "ticket"})
    with pytest.raises(LookupError):
        server.handle_reference("claude_code:s1", {"kind": "ticket", "key": "SAAS-1", "linkable": "false"})   # not a boolean
    assert server.handle_reference("claude_code:s1", {"kind": "ticket", "key": "SAAS-1", "linkable": False}) is None  # no such reference
    from convo_index import references as REF
    conn = dbm.connect(tmp_path / "t.db")
    conv = Conversation(source="claude_code", native_id="s1", title="Kube fix", first_prompt="SAAS-1", source_ref="/a",
                        messages=[Message(seq=0, role="user", text="SAAS-1 and commit abc1234def")])
    REF.store(conn, conv, REF.extract(conv)); conn.close()
    out = json.loads(server.handle_reference("claude_code:s1", {"kind": "ticket", "key": "SAAS-1", "linkable": False}))
    assert out["ok"] is True and not [r for r in out["references"] if r["kind"] == "ticket"][0]["linkable"]
    with pytest.raises(LookupError):                                                     # commits never link
        server.handle_reference("claude_code:s1", {"kind": "commit", "key": "abc1234def", "linkable": True})


def test_handle_suggestions_lists_open_ones_and_hides_hidden_targets(tmp_path, monkeypatch):
    from tests.conftest import make_settings
    from tests.test_vectors import fake_embed, FP
    from convo_index import tags as T, vectors as V
    st = make_settings(tmp_path)
    _seed(tmp_path / "t.db", monkeypatch)
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: st)
    conn = dbm.connect(tmp_path / "t.db"); T.sync_definitions(conn, st)
    other = Conversation(source="claude_code", native_id="s2", title="Kube two", first_prompt="kubernetes deploy again",
                         source_ref="/b", cwd="/x", messages=[Message(seq=0, role="user", text="kubernetes deploy again")])
    dbm.upsert_conversation(conn, other, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    for cid in ("claude_code:s1", "claude_code:s2"):
        assert V.run_vector(conn, cid, fake_embed, FP) == "ok"
    conn.close()
    assert json.loads(server.handle_suggestions()) == []
    server.handle_tag("claude_code:s1", {"group": "workspace", "tag": "personal"})
    rows = json.loads(server.handle_suggestions())
    assert [(r["id"], r["group"], r["tag"], r["origin"], r["origin_title"]) for r in rows] == \
        [("claude_code:s2", "workspace", "personal", "claude_code:s1", "Kube fix")]
    assert rows[0]["similarity"] > 0.9
    conv = json.loads(server.handle_conversation("claude_code:s2"))
    assert conv["tags"]["suggestions"][0]["tag"] == "personal"
    server.handle_hide("claude_code:s2", True)
    assert json.loads(server.handle_suggestions()) == []


def test_prepare_passes_the_resolved_path_and_dim_to_init_schema(tmp_path, monkeypatch):
    from tests.conftest import make_settings
    st = make_settings(tmp_path, embeddings={"dim": 384})
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: st)
    monkeypatch.setattr(server, "DB_PATH", None)
    seen = {}
    monkeypatch.setattr(dbm, "init_schema", lambda conn, db_path=None, **kw: seen.update({"path": db_path, **kw}))
    path = server.prepare()
    assert path == st.db_path() and seen["path"] == st.db_path() and seen["dim"] == 384


def _serve(tmp_path, monkeypatch):
    import threading
    _seed(tmp_path / "t.db", monkeypatch)
    srv = server.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _request(srv, method, path, headers=None):
    import http.client
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})   # Host sent verbatim
    res = conn.getresponse()
    body = res.read()
    conn.close()
    return res.status, body


def test_rebinding_host_is_refused(tmp_path, monkeypatch):
    srv = _serve(tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, _ = _request(srv, "GET", "/api/search?q=kubernetes&mode=keyword", {"Host": f"attacker.example:{port}"})
        assert status == 403
        status, body = _request(srv, "GET", "/api/search?q=kubernetes&mode=keyword", {"Host": f"localhost:{port}"})
        assert status == 200 and json.loads(body)[0]["id"] == "claude_code:s1"
    finally:
        srv.shutdown()


def test_post_without_csrf_header_is_refused(tmp_path, monkeypatch):
    srv = _serve(tmp_path, monkeypatch)
    try:
        # a cross-site form/no-cors fetch can send neither the custom header nor a matching Origin
        status, _ = _request(srv, "POST", "/api/delete/claude_code:s1")
        assert status == 403
        status, _ = _request(srv, "POST", "/api/delete/claude_code:s1",
                             {"X-Convo-Index": "1", "Origin": "http://attacker.example"})
        assert status == 403
        assert server.handle_conversation("claude_code:s1") is not None   # nothing was deleted
        port = srv.server_address[1]
        status, _ = _request(srv, "POST", "/api/hide/claude_code:s1?hidden=1",
                             {"X-Convo-Index": "1", "Origin": f"http://127.0.0.1:{port}"})
        assert status == 200
    finally:
        srv.shutdown()


def test_allowed_hosts():
    assert server.allowed_hosts("127.0.0.1", 9753) == {("127.0.0.1", None), ("localhost", None), ("::1", None)}
    assert ("box.lan", None) in server.allowed_hosts("0.0.0.0", 9753, ["box.lan"])
    assert not any(h == "0.0.0.0" for h, _ in server.allowed_hosts("0.0.0.0", 9753))
    extra = server.allowed_hosts("0.0.0.0", 9753, ["Box.lan:9753", "fe80::1", "[fe80::2]:9753", "", "x:y:z:bad"])
    assert extra == server.allowed_hosts("0.0.0.0", 9753) | {("box.lan", 9753), ("fe80::1", None), ("fe80::2", 9753)}
    assert ("box.lan", None) in server.allowed_hosts("0.0.0.0", 9753, "box.lan")   # a string, not a list
    assert ("fe80::3", None) in server.allowed_hosts("fe80::3", 9753)


def test_host_header_forms():
    allowed = server.allowed_hosts("0.0.0.0", 9753, ["box.lan", "pinned.lan:9753"])
    for host in ("Box.lan:9753", "box.lan.:9753", "box.lan", "box.lan:80", "localhost:8080", "[::1]:8080",
                 "127.0.0.1:9753", "pinned.lan:9753"):
        assert server.request_error("GET", {"Host": host}, allowed) is None, host
    for host in ("pinned.lan", "pinned.lan:8080", "attacker.example:9753", "0.0.0.0:9753", "", "localhost:abc",
                 "localhost@attacker.example:9753", "attacker.example@localhost:9753",
                 "attacker.example:9753/localhost"):
        assert server.request_error("GET", {"Host": host}, allowed), host
    # behind a TLS-terminating proxy: Host carries port 80, Origin port 443; one bare entry covers both
    assert server.request_error("POST", {"Host": "box.lan", "Origin": "https://box.lan", "X-Convo-Index": "1"},
                                allowed) is None
    assert server.request_error("POST", {"Host": "box.lan:9753", "Origin": "null", "X-Convo-Index": "1"}, allowed)
    assert server.request_error("POST", {"Host": "box.lan:9753", "Origin": "http://localhost:8080",
                                         "X-Convo-Index": "1"}, allowed) is None   # a port forward's own origin


def test_default_http_port_host_has_no_port():
    allowed = server.allowed_hosts("0.0.0.0", 80, ["box.lan:80"])
    assert server.request_error("GET", {"Host": "box.lan"}, allowed) is None
    assert server.request_error("POST", {"Host": "box.lan", "Origin": "http://box.lan",
                                         "X-Convo-Index": "1"}, allowed) is None
    assert server.request_error("GET", {"Host": "box.lan"}, server.allowed_hosts("0.0.0.0", 9753, ["box.lan:9753"]))


def test_advertised_url_is_one_the_server_accepts():
    for bind in ("0.0.0.0", "::", "127.0.0.1", "box.lan", "fe80::1"):
        url = server.advertised_url(bind, 9753)
        allowed = server.allowed_hosts(bind, 9753)
        assert server.request_error("GET", {"Host": url.removeprefix("http://")}, allowed) is None, bind


def test_make_server_binds_an_ipv6_address():
    import pytest
    try:
        srv = server.make_server("::1", 0)
    except OSError as e:
        pytest.skip(f"no IPv6 loopback here: {e}")
    try:
        assert server.advertised_url("::1", srv.server_address[1]).startswith("http://[::1]:")
    finally:
        srv.server_close()


def test_delete_keeps_the_conversation_when_the_archive_cannot_be_removed(tmp_path, monkeypatch):
    import pytest
    from pathlib import Path
    _seed(tmp_path / "t.db", monkeypatch)
    arch = tmp_path / "copy.jsonl"; arch.write_text("{}")
    conn = dbm.connect(tmp_path / "t.db")
    conn.execute("UPDATE conversations SET archive_path=? WHERE id='claude_code:s1'", (str(arch),)); conn.commit(); conn.close()
    def refuse(self, missing_ok=False):
        raise PermissionError("read-only")
    monkeypatch.setattr(Path, "unlink", refuse)
    with pytest.raises(RuntimeError, match="archive copy not removed"):
        server.handle_delete("claude_code:s1")
    assert server.handle_conversation("claude_code:s1") is not None and arch.exists()


def test_delete_waits_for_no_refresh(tmp_path, monkeypatch):
    import pytest
    from convo_index.lock import index_lock
    _seed(tmp_path / "t.db", monkeypatch)
    with index_lock(tmp_path / "t.db", blocking=True):                     # a refresh is running
        with pytest.raises(RuntimeError, match="refresh is running"):
            server.handle_delete("claude_code:s1")
    assert server.handle_conversation("claude_code:s1") is not None


def test_serve_steps_aside_when_the_port_is_taken(tmp_path, monkeypatch, capsys):
    """A second `serve` (the login service next to a terminal one, or vice versa) exits 0, so a
    service manager that restarts on failure leaves it alone."""
    _seed(tmp_path / "t.db", monkeypatch)
    holder = server.make_server("127.0.0.1", 0)
    port = holder.server_address[1]
    cfg = {"server": {"host": "127.0.0.1", "port": port, "allowed_hosts": []}, "embeddings": {"dim": EMBED_DIM}}
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(server, "prepare", lambda c: None)
    token_file = tmp_path / "turn-token"
    token_file.write_text("live-servers-token")
    monkeypatch.setattr(server, "turn_token_path", lambda settings=None: token_file)
    try:
        assert server.main() == 0
    finally:
        holder.server_close()
    assert "already served" in capsys.readouterr().out
    assert token_file.read_text() == "live-servers-token"      # the running server's token is left alone


def test_refresh_after_turn_skips_derived_jobs(monkeypatch):
    seen = []

    class R:
        returncode, stdout, stderr = 0, '{"indexed": 1, "failed": 0}', ""
    monkeypatch.setattr(server.subprocess, "run", lambda argv, **k: seen.append(argv) or R())
    assert server._refresh_after_turn()["ok"] is True
    server._run_indexer()
    assert seen[0][-1] == "--no-jobs" and "--no-jobs" not in seen[1]
