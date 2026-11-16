"""The public playground: CONVO_INDEX_DEMO=1 serves a baked database read-only and refuses
every request that would write, run a turn or stop the server."""
import json
import sqlite3
import threading

import pytest

from convo_index import db as dbm
from convo_index import server
from tests.conftest import make_settings
from tests.test_server import _request, _seed


def _bake(path):
    """What the image build does to the demo database: no WAL, no sidecar files."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()


@pytest.fixture
def demo(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _seed(db, monkeypatch)
    _bake(db)
    st = make_settings(tmp_path)
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: st)
    monkeypatch.setenv("CONVO_INDEX_DEMO", "1")
    monkeypatch.setattr(dbm, "_read_only", False)    # restored after the test: set_read_only is global
    server.prepare(st)
    srv = server.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, db
    srv.shutdown()


def _post(srv, path, method="POST"):
    port = srv.server_address[1]
    return _request(srv, method, path, {"X-Convo-Index": "1", "Origin": f"http://127.0.0.1:{port}"})


def test_demo_flag_env_overrides_config(tmp_path, monkeypatch):
    st = make_settings(tmp_path, server={"demo": True})
    monkeypatch.delenv("CONVO_INDEX_DEMO", raising=False)
    assert st.demo()
    monkeypatch.setenv("CONVO_INDEX_DEMO", "0")
    assert not st.demo()
    monkeypatch.setenv("CONVO_INDEX_DEMO", "off")
    assert not st.demo()
    monkeypatch.setenv("CONVO_INDEX_DEMO", "1")
    assert make_settings(tmp_path).demo()


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/refresh"),
    ("POST", "/api/shutdown"),
    ("POST", "/api/hide/claude_code:s1?hidden=1"),
    ("POST", "/api/delete/claude_code:s1"),
    ("POST", "/api/tag/claude_code:s1"),
    ("POST", "/api/reference/claude_code:s1"),
    ("POST", "/api/conversation/claude_code:s1/turn"),
    ("POST", "/api/conversation/claude_code:s1/handoff"),
    ("POST", "/api/handoff/start"),
    ("POST", "/api/turns/tools"),
    ("POST", "/api/some-endpoint-added-later"),
    ("DELETE", "/api/turn/abc"),
])
def test_every_write_is_refused(demo, method, path):
    srv, db = demo
    before = db.stat().st_mtime_ns
    status, body = _post(srv, path, method)
    assert status == 403 and json.loads(body)["error"] == server.DEMO_REFUSAL
    assert db.stat().st_mtime_ns == before
    assert server.handle_conversation("claude_code:s1") is not None


@pytest.mark.parametrize("path", ["/api/turn/abc", "/api/turns/tools"])
def test_turn_reads_are_refused(demo, path):
    srv, _ = demo
    status, _ = _request(srv, "GET", path)
    assert status == 403


def test_reads_work_and_report_demo(demo):
    srv, db = demo
    before = db.stat().st_mtime_ns
    status, body = _request(srv, "GET", "/api/search?q=kubernetes&mode=keyword")
    assert status == 200 and json.loads(body)[0]["id"] == "claude_code:s1"
    status, body = _request(srv, "GET", "/api/config")
    assert status == 200 and json.loads(body)["demo"] is True
    status, body = _request(srv, "GET", "/api/conversation/claude_code:s1")
    conv = json.loads(body)
    assert status == 200 and conv["turns_supported"] is False and conv["handoff_targets"] == []
    for path in ("/", "/analytics", "/api/analytics", "/api/sources", "/api/suggestions", "/api/related/SAAS-1"):
        assert _request(srv, "GET", path)[0] == 200, path
    assert db.stat().st_mtime_ns == before
    assert not db.with_name(db.name + "-wal").exists()


def test_connections_are_read_only(demo):
    _, db = demo
    conn = dbm.connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE conversations SET title='x'")
    finally:
        conn.close()


def test_demo_prepare_refuses_an_old_schema(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    sqlite3.connect(db).close()                      # an empty file: no schema at all
    st = make_settings(tmp_path)
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: st)
    monkeypatch.setattr(server, "DB_PATH", db)
    monkeypatch.setenv("CONVO_INDEX_DEMO", "1")
    monkeypatch.setattr(dbm, "_read_only", False)
    with pytest.raises(SystemExit, match="schema version"):
        server.prepare(st)
    assert db.stat().st_size == 0                    # nothing was migrated into it


def test_demo_main_writes_no_turn_token(tmp_path, monkeypatch, capsys):
    st = make_settings(tmp_path, server={"port": 0})
    monkeypatch.setattr(server.CFG, "load", lambda *a, **k: st)
    monkeypatch.setenv("CONVO_INDEX_DEMO", "1")
    monkeypatch.setattr(server, "prepare", lambda c: None)
    monkeypatch.setattr(server, "write_turn_token", lambda *a: pytest.fail("demo wrote a turn token"))
    monkeypatch.setattr(server, "adopt_turn_token", lambda *a: pytest.fail("demo read a turn token"))

    class Srv:
        server_address = ("127.0.0.1", 1)
        def serve_forever(self): raise KeyboardInterrupt
        def server_close(self): pass
    monkeypatch.setattr(server, "make_server", lambda *a, **k: Srv())
    assert server.main() == 0
    assert "playground" in capsys.readouterr().out


def test_demo_prepare_refuses_a_database_with_wal_content(tmp_path, monkeypatch):
    """immutable=1 reads only the main file, so un-checkpointed WAL pages would silently vanish."""
    db = tmp_path / "t.db"
    _seed(db, monkeypatch)
    _bake(db)
    holder = sqlite3.connect(db)                     # held open: the WAL is not checkpointed on close
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("UPDATE conversations SET title='newer'"); holder.commit()
    try:
        st = make_settings(tmp_path)
        monkeypatch.setattr(server.CFG, "load", lambda *a, **k: st)
        monkeypatch.setenv("CONVO_INDEX_DEMO", "1")
        monkeypatch.setattr(dbm, "_read_only", False)
        with pytest.raises(SystemExit, match="checkpoint"):
            server.prepare(st)
    finally:
        holder.close()


def test_analytics_loads_a_baked_duckdb_extension_without_writing_home(demo, tmp_path, monkeypatch):
    """The playground image bakes the sqlite extension; at run time HOME is read-only."""
    from convo_index import analytics as A
    _, db = demo
    ext = tmp_path / "duckdb-ext"
    A.install_duckdb_extensions(ext)                 # the image build step (same network need as test_analytics)
    home = tmp_path / "ro-home"
    home.mkdir(); home.chmod(0o500)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(A.DUCKDB_EXTENSIONS_ENV, str(ext))
    try:
        assert A.analytics(db)["by_source"]
        assert list(home.iterdir()) == []
    finally:
        home.chmod(0o700)
