"""scripts/demo_writers.py writes each source's native layout: proven by indexing it with the
real `convo-index index` in a subprocess (HOME and the source paths come from write_all)."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# The playground is built on Linux; Windows reads the home directory from USERPROFILE, not HOME.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the playground is built on Linux")

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("demo_writers", ROOT / "scripts" / "demo_writers.py")
dw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dw)

MODELS = {"claude_code": "claude-sonnet-4-5", "codex": "gpt-5-codex", "opencode": "claude-haiku-4-5",
          "pi": "claude-opus-4-1", "antigravity": "gemini-2.5-pro"}
USAGE_SOURCES = ("claude_code", "codex", "opencode", "pi")
TOOL_RESULT_SOURCES = ("claude_code", "codex", "pi", "antigravity")   # opencode keeps a tool's output on its call


def conversations():
    out = []
    for src in dw.SOURCES:
        for n in (1, 2):
            out.append({
                "id": f"c-{src}-{n}", "source": src, "title": f"{src} demo {n}: fix flaky checkout test",
                "cwd": f"/home/dev/acme-shop-{n}", "branch": f"fix/SHOP-14{n}-flaky-checkout",
                "repo_url": "https://github.com/acme-labs/acme-shop", "model": MODELS[src],
                "start": f"2026-09-2{n}T09:14:00Z",
                "turns": [
                    {"role": "user", "text": f"The checkout test is flaky on CI ({src} {n}). Can you look?", "at": 0},
                    {"role": "assistant", "at": 20, "text": "It races the payment mock; I added an await.",
                     "thinking": "Probably a timing issue in the payment mock.",
                     "tools": [{"name": "bash", "input": {"command": "pytest -x tests/test_checkout.py"},
                                "output": "1 failed, 12 passed"}],
                     "usage": {"input": 1200, "output": 340, "cache_read": 8000, "cache_write": 100}},
                    {"role": "user", "text": "Thanks, does it pass now?", "at": 90},
                    {"role": "assistant", "at": 100, "text": "Yes, 13 passed across 20 runs.",
                     "usage": {"input": 300, "output": 40, "cache_read": 9000, "cache_write": 0}},
                ],
            })
    return out


@pytest.fixture(scope="module")
def indexed(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("demo")
    convs = conversations()
    env_vars = dw.write_all(convs, tmp / "src")
    cfg = tmp / "config.toml"
    cfg.write_text('[embeddings]\nbackend = "openai_compat"\nbase_url = "http://127.0.0.1:9"\n')
    env = {**os.environ, **env_vars, "CONVO_INDEX_DB": str(tmp / "convos.db"),
           "CONVO_INDEX_ARCHIVE": str(tmp / "archive"), "CONVO_INDEX_CONFIG": str(cfg),
           "XDG_CONFIG_HOME": str(tmp / "xdg-config"), "XDG_DATA_HOME": str(tmp / "xdg-data"),
           "HF_HUB_OFFLINE": "1"}
    res = subprocess.run([sys.executable, "-m", "convo_index.cli", "index"], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stdout + res.stderr
    totals = json.loads(res.stdout.strip().splitlines()[-1])
    conn = sqlite3.connect(tmp / "convos.db")
    conn.row_factory = sqlite3.Row
    yield convs, totals, conn, env_vars
    conn.close()


def test_env_points_at_the_written_tree(indexed, tmp_path):
    _, _, _, env = indexed
    assert set(env) == {"HOME", "CODEX_HOME", "OPENCODE_DB", "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR",
                        "ANTIGRAVITY_HOME"}
    assert Path(env["OPENCODE_DB"]).is_file() and env["PI_CODING_AGENT_SESSION_DIR"] == ""


def test_output_is_deterministic(tmp_path):
    convs = conversations()
    for d in ("a", "b"):
        dw.write_all(convs, tmp_path / d)

    def texts(base):
        return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*.jsonl"))}
    a, b = texts(tmp_path / "a"), texts(tmp_path / "b")
    assert a == b and len(a) == 8          # claude, codex, pi, antigravity: 2 files each

    def rows(db, table):
        c = sqlite3.connect(db)
        try:
            return c.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        finally:
            c.close()
    oc = "home/.local/share/opencode/opencode.db"
    for t in ("session", "message", "part", "project", "workspace"):
        assert rows(tmp_path / "a" / oc, t) == rows(tmp_path / "b" / oc, t)


def test_every_source_indexes_cleanly(indexed):
    _, totals, _, _ = indexed
    assert totals["failed"] == 0
    for src in dw.SOURCES:
        assert totals["sources"][src]["indexed"] == 2, (src, totals)


@pytest.mark.parametrize("src", dw.SOURCES)
def test_conversation_fields(indexed, src):
    convs, _, conn, _ = indexed
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM conversations WHERE source=?", (src,))}
    assert len(rows) == 2
    for c in (c for c in convs if c["source"] == src):
        r = rows[c["title"]]
        assert r["cwd"] == c["cwd"]
        assert r["first_prompt"] == c["turns"][0]["text"]
        assert r["start_ts"].startswith(c["start"][:16])
        if src in ("claude_code", "codex", "opencode"):
            assert r["git_branch"] == c["branch"]
        if src == "codex":
            assert r["git_origin_url"] == c["repo_url"]
        if src != "antigravity":
            assert any(c["model"] in m for m in json.loads(r["models"]))


@pytest.mark.parametrize("src", dw.SOURCES)
def test_message_kinds(indexed, src):
    _, _, conn, _ = indexed
    kinds = {r[0] for r in conn.execute(
        "SELECT DISTINCT m.kind FROM messages m JOIN conversations c ON c.id = m.conversation_id WHERE c.source=?", (src,))}
    expected = {"text", "thinking", "tool_call"} | ({"tool_result"} if src in TOOL_RESULT_SOURCES else set())
    assert kinds == expected
    n = conn.execute("SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                     "WHERE c.source=? AND m.kind='text'", (src,)).fetchone()[0]
    assert n == 8      # 2 conversations x (2 user + 2 assistant replies)


@pytest.mark.parametrize("src", dw.SOURCES)
def test_usage_records(indexed, src):
    _, _, conn, _ = indexed
    rows = conn.execute("SELECT u.* FROM usage_records u JOIN conversations c ON c.id = u.conversation_id "
                        "WHERE c.source=? ORDER BY u.conversation_id, u.id", (src,)).fetchall()
    if src not in USAGE_SOURCES:
        assert rows == []
        return
    assert len(rows) == 4
    assert {r["model"] for r in rows} == {MODELS[src]}
    assert all(r["message_id"] is not None for r in rows)
    first = rows[0]
    assert (first["input_uncached"], first["output"], first["cache_read"]) == (1200, 340, 8000)
    assert first["cache_write"] == 100
