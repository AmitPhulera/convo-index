"""`convo-index` command line entry point."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path


def cmd_init(args):
    """Probe the machine and write a config file. Idempotent: existing keys are kept."""
    from convo_index import config as C
    from convo_index import settings as S
    from convo_index import db as dbm
    from convo_index.providers import registry as R
    from convo_index.sources.base import all_adapters

    path = Path(args.config) if args.config else S.CONFIG_PATH
    current = S.load(path, use_cache=False)
    cfg = copy.deepcopy(dict(current))

    found_sources = [a.name for a in all_adapters() if a.available()]
    explicit_all = {}
    if current.path is not None:
        import tomllib
        with open(current.path, "rb") as fh:
            explicit_all = tomllib.load(fh)
    explicit = explicit_all.get("sources") or {}
    for a in all_adapters():
        if "enabled" in (explicit.get(a.name) or {}):
            continue   # the user's own choice stays
        cfg["sources"][a.name]["enabled"] = a.name in found_sources

    probe = R.probe_cli_backends()
    default = probe[0] if probe else ""
    for job in ("classify", "summary"):
        if not cfg["llm"][job].get("backend") and default:
            cfg["llm"][job] = {"backend": default, "model": S.DEFAULT_MODELS[default][job]}

    # Embeddings: an explicit backend stays; otherwise in-process fastembed when installed,
    # else a running Ollama, else fastembed so `doctor` says what to install.
    if "backend" not in (explicit_all.get("embeddings") or {}):
        from convo_index.doctor import _embedding_model_present
        cfg["embeddings"]["backend"] = "fastembed" if R.FastEmbed.available() else (
            "openai_compat" if _embedding_model_present(cfg["embeddings"])[0] else "fastembed")
        if cfg["embeddings"]["backend"] == "openai_compat":
            cfg["embeddings"]["model"] = C.EMBED_MODEL

    # Seed category labels from an existing index so filters keep working after upgrade.
    # (Read through the env override if any, but never bake an env value into the file.)
    db_path = current.db_path()
    cfg["paths"] = dict(current["paths"])
    if db_path.exists() and current.path is None:
        try:
            conn = dbm.connect(db_path)
            if dbm._table_exists(conn, "conversations"):
                labels = [r[0] for r in conn.execute(
                    "SELECT category FROM conversations WHERE category NOT IN ('unknown','ambiguous') "
                    "GROUP BY category ORDER BY COUNT(*) DESC").fetchall() if r[0]]
                if labels:
                    cfg["category"]["labels"] = labels
                    cfg["category"]["descriptions"] = {l: cfg["category"]["descriptions"].get(l, l) for l in labels}
                    cfg["category"]["hints"] = {l: cfg["category"]["hints"].get(l, []) for l in labels}
            conn.close()
        except Exception:
            pass

    hosted = any(R.is_hosted(cfg["llm"][j]) for j in ("classify", "summary") if cfg["llm"][j].get("backend"))
    if cfg["privacy"].get("hosted_processing") == "ask":
        if args.yes:
            cfg["privacy"]["hosted_processing"] = "allow" if hosted else "ask"
        elif not sys.stdin.isatty():
            # Consent is never implied by a pipe: leave it unresolved (hosted calls refused).
            print("hosted_processing left as 'ask' (no terminal to ask; pass --yes to allow)", file=sys.stderr)
        else:
            backends = sorted({cfg["llm"][j]["backend"] for j in ("classify", "summary") if cfg["llm"][j].get("backend")})
            ans = input(f"Summaries and classification will send conversation text to hosted models via "
                        f"{', '.join(backends) or 'nothing yet'}. Allow? [Y/n] ").strip().lower()
            cfg["privacy"]["hosted_processing"] = "deny" if ans.startswith("n") else "allow"

    comments = {
        "llm": f"Backend per job. Detected CLIs in preference order: {', '.join(probe) or 'none'}.\n"
               "Backends: gemini_cli, codex_cli, claude_cli, anthropic_api, openai_compat (Ollama etc.).",
        "privacy": "hosted_processing: allow | deny | ask. deny refuses every non-localhost backend.",
        "category": "Labels are the conversation categories; hints are cwd substrings that decide without an LLM.",
        "embeddings": "fastembed = in-process (default; the model is fetched on first index);\n"
                      "openai_compat = Ollama or any /v1/embeddings server at base_url.\n"
                      "Changing backend or model requires `convo-index reembed`.",
        "sources": "exclude_cwd: project paths or globs (a path covers itself and everything below it);\n"
                   "matching sessions are never indexed or sent anywhere.",
        "references": "ticket_url: {key} is replaced by the ticket id.",
    }
    written = S.write(cfg, path, comments)
    print(f"wrote {written}")
    print(f"sources found: {', '.join(found_sources) or 'none'}")
    print(f"llm: classify={cfg['llm']['classify'].get('backend') or '-'} summary={cfg['llm']['summary'].get('backend') or '-'} "
          f"(probe order: {', '.join(probe) or 'no CLI found'})")
    print(f"privacy.hosted_processing = {cfg['privacy']['hosted_processing']}")
    e = cfg["embeddings"]
    how = {"fastembed": "in-process" if R.FastEmbed.available() else "NOT INSTALLED: re-run install.sh, or `uv sync` in a checkout",
           "openai_compat": e.get("base_url")}.get(e["backend"], "")
    print(f"embeddings: {e['backend']} model={e['model']} ({how})")
    return 0


def cmd_doctor(args):
    from convo_index import doctor
    rep = doctor.report()
    print(json.dumps(rep, indent=2) if args.json else doctor.format_report(rep))
    return 1 if rep["warnings"] else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="convo-index",
                                 description="Index and browse AI coding-assistant conversations.")
    ap.add_argument("--config", help="config file (default: $CONVO_INDEX_CONFIG or ~/.config/convo-index/config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="detect installed tools and LLM CLIs, write the config file")
    p_init.add_argument("--yes", "-y", action="store_true", help="no prompts; allow hosted processing if a CLI was found")
    sub.add_parser("index", help="build or refresh the index (incremental). Exit 0 ok, 1 failures, 75 busy")
    sub.add_parser("serve", help="run the local web UI")
    p_doc = sub.add_parser("doctor", help="show sources, backends, embedder and failing jobs. Exit 1 if warnings")
    p_doc.add_argument("--json", action="store_true")
    p_cfg = sub.add_parser("config", help="config file helpers")
    p_cfg.add_argument("what", choices=["path", "show"])
    args = ap.parse_args(argv)

    if args.config:
        os.environ["CONVO_INDEX_CONFIG"] = args.config
        from convo_index import settings as S
        S.CONFIG_PATH = Path(args.config)
        S.reset_cache()

    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "index":
        from convo_index import indexer
        return indexer.main()
    if args.cmd == "serve":
        from convo_index import server
        server.main()
        return 0
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "config":
        from convo_index import settings as S
        if args.what == "path":
            print(S.CONFIG_PATH)
        else:
            print(S.render(dict(S.load(use_cache=False))))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
