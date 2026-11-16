"""Build the public playground's database from the fictional conversations in demo/.

    uv run python scripts/make_demo_data.py --out build/playground [--models DIR]

The conversations are written in each tool's native on-disk format and indexed by the real
`convo-index index`, so the playground exercises the real parsers. No LLM is called: summaries come
from demo/ and tags from rules in demo/config.toml. Embeddings are real (fastembed, in-process) and
are computed with the model in --models, which must be the directory the server reads at run time.

Writes <out>/convos.db (checkpointed, not WAL: the server opens it immutable) and <out>/config.toml,
the playground's runtime config. Deterministic apart from the vectors' float noise.
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from demo import CONVERSATIONS  # noqa: E402
import demo_writers as W  # noqa: E402

from convo_index import db as dbm  # noqa: E402
from convo_index import settings as S  # noqa: E402


def runtime_config(out, models):
    """demo/config.toml plus the paths of this build."""
    cfg = tomllib.loads((ROOT / "demo" / "config.toml").read_text(encoding="utf-8"))
    cfg["paths"] = {"db": str(out / "convos.db"), "archive": str(out / "archive")}
    cfg.setdefault("embeddings", {})["cache_dir"] = str(models)
    return cfg


def index(env):
    subprocess.run([sys.executable, "-m", "convo_index.cli", "index"], env=env, check=True)


NATIVE_ID = {"claude_code": W.claude_session_id, "codex": W.codex_thread_id, "opencode": W.opencode_session_id,
             "pi": W.pi_session_id, "antigravity": W.antigravity_id}


def add_summaries(db_path):
    """Attach demo/'s summaries to the indexed conversations (ids come from the writers)."""
    conn = dbm.connect(db_path)
    try:
        rows = dict((cid, rev) for cid, rev in conn.execute("SELECT id, source_revision FROM conversations"))
        missing = []
        for c in CONVERSATIONS:
            cid = f"{c['source']}:{NATIVE_ID[c['source']](c)}"
            hit = (cid, rows[cid]) if cid in rows else None
            if not hit:
                missing.append(c["id"])
            elif c.get("summary"):
                dbm.set_summary(conn, hit[0], c["summary"], hit[1])
        if missing:
            raise SystemExit(f"not indexed: {', '.join(missing)}")
    finally:
        conn.close()


def bake(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("VACUUM")
    conn.close()
    for suffix in ("-wal", "-shm", ".lock"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--models", type=Path, help="fastembed cache dir (default: <out>/models)")
    ap.add_argument("--no-vectors", action="store_true",
                    help="skip embeddings (tests: no model download); semantic search stays off")
    a = ap.parse_args(argv)
    out = a.out.resolve()
    models = (a.models or out / "models").resolve()
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / "convos.db"
    for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        p.unlink(missing_ok=True)

    cfg = runtime_config(out, models)
    with tempfile.TemporaryDirectory(prefix="convo-index-demo-") as tmp:
        tmp = Path(tmp)
        build_cfg = dict(cfg, server=dict(cfg["server"], demo=False), paths={"db": str(db_path), "archive": str(tmp / "archive")})
        if a.no_vectors:    # an unreachable local embedder: every vector is skipped, nothing is downloaded
            build_cfg["embeddings"] = {"backend": "openai_compat", "base_url": "http://127.0.0.1:9/v1"}
        S.write(build_cfg, tmp / "config.toml")
        env = {k: v for k, v in os.environ.items() if not k.startswith("CONVO_INDEX_")}
        env.update(W.write_all(CONVERSATIONS, tmp / "sources"))
        env.update({"CONVO_INDEX_CONFIG": str(tmp / "config.toml"), "CONVO_INDEX_DB": str(db_path),
                    "CONVO_INDEX_ARCHIVE": str(tmp / "archive"), "PYTHONPATH": str(ROOT)})
        index(env)
        add_summaries(db_path)
        index(env)                        # the summaries changed the conversation vectors' text
    bake(db_path)
    cfg["paths"]["archive"] = "/tmp/convo-index-archive"    # never read by the server; must not point into the image
    S.write(cfg, out / "config.toml")
    print(f"playground database: {db_path} ({db_path.stat().st_size // 1024} KB), config: {out / 'config.toml'}")


if __name__ == "__main__":
    main()
