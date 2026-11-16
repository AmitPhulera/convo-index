"""The playground database builds from demo/ through the real parsers and indexer."""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import pytest

# The playground is built on Linux; Windows reads the home directory from USERPROFILE, not HOME.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the playground is built on Linux")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import make_demo_data  # noqa: E402
from demo import CONVERSATIONS  # noqa: E402


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("playground")
    make_demo_data.main(["--out", str(out), "--no-vectors"])
    return out


def test_every_conversation_is_indexed_with_its_summary(built):
    conn = sqlite3.connect(built / "convos.db")
    try:
        by_source = Counter(s for (s,) in conn.execute("SELECT source FROM conversations"))
        assert by_source == Counter(c["source"] for c in CONVERSATIONS)
        assert conn.execute("SELECT COUNT(*) FROM conversation_summary").fetchone()[0] == len(CONVERSATIONS)
        assert conn.execute("SELECT COUNT(DISTINCT model) FROM usage_records").fetchone()[0] >= 5
        tags = {t for (t,) in conn.execute("SELECT tag FROM tag_assignments")}
        assert {"work", "personal", "bug", "infra"} <= tags
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
    assert sorted(p.name for p in built.iterdir() if p.name.startswith("convos.db")) == ["convos.db"]


def test_runtime_config_is_the_playground(built):
    import tomllib
    cfg = tomllib.loads((built / "config.toml").read_text())
    assert cfg["server"]["demo"] is True and cfg["ui"]["turns"]["enabled"] is False
    assert cfg["paths"]["db"] == str(built / "convos.db")


def test_keyword_search_finds_a_known_conversation(built, monkeypatch):
    from convo_index import db as dbm
    from convo_index import server
    monkeypatch.setattr(server, "DB_PATH", built / "convos.db")
    monkeypatch.setattr(dbm, "_read_only", True)        # as the playground opens it
    hits = json.loads(server.handle_search({"q": ["lru_cache"], "mode": ["keyword"]}))
    assert hits and hits[0]["title"] == "Flaky checkout test on CI"
