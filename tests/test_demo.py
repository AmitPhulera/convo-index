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
