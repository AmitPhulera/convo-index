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


def cmd_reembed(args):
    """Drop every vector and record the configured embedder; the next `index` backfills."""
    from convo_index import db as dbm
    from convo_index import settings as S
    from convo_index.lock import index_lock, IndexLocked
    from convo_index.providers import registry as R
    settings = S.load()
    emb = R.make_embedder(settings)
    if emb is None:
        print("no embedding backend configured", file=sys.stderr)
        return 1
    try:
        with index_lock(settings.db_path()):
            conn = dbm.connect(settings.db_path())
            dbm.init_schema(conn, settings.db_path(), have_lock=True)
            n = conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0]
            dim = int(settings["embeddings"].get("dim") or 0) or dbm.vector_dim(conn)
            dbm.recreate_vectors(conn, dim, fingerprint=emb.fingerprint)   # dimension may have changed with the model
            conn.close()
    except IndexLocked:
        print("another indexer is running; try again later", file=sys.stderr)
        return 75
    print(f"dropped {n} message vectors and all conversation vectors; tables recreated with {dim} dims; "
          f"embedder now {emb.fingerprint}. Run `convo-index index` to backfill (bounded per run).")
    return 0


def cmd_tag(args):
    """convo-index tag <id> <group>=<tag|none> [--reject]  |  convo-index tag --list  |
    convo-index tag --retag"""
    from convo_index import db as dbm, settings as S, tags as T, jobs
    settings = S.load()
    conn = dbm.connect(settings.db_path())
    dbm.init_schema(conn, settings.db_path())
    T.sync_definitions(conn, settings)
    if args.calibrate:
        from convo_index import vectors as V
        rep = V.calibrate(conn)
        if not rep:
            print("no suggestions have been decided on yet; accept or reject a few suggested tags first")
            return 0
        for g, buckets in rep.items():
            print(f"{g}:")
            for b in sorted(buckets, reverse=True):
                x = buckets[b]; cum = x["at_or_above"]
                print(f"  similarity [{b}, {float(b) + 0.05:.2f}): {x['accepted']} accepted, {x['rejected']} rejected"
                      + (f" (precision {x['precision']})" if x["precision"] is not None else "")
                      + f"; at or above {b}: {cum['accepted']}/{cum['accepted'] + cum['rejected']} accepted (precision {cum['precision']})")
        return 0
    if args.suggestions:
        from convo_index import vectors as V
        rows = V.open_suggestions(conn)
        for r in rows:
            print(f"{(r['similarity'] or 0):.2f}  {r['id']}  {r['group']}={r['tag']}  «{(r['title'] or '')[:50]}»  "
                  f"(like: {(r['origin_title'] or r['origin'] or '')[:40]})")
        print(f"{len(rows)} open suggestion(s)")
        return 0
    if args.list:
        groups = T.load_groups(conn)
        counts = {(g, t): n for g, t, n in conn.execute(
            "SELECT group_name, tag, COUNT(*) FROM tag_assignments GROUP BY 1, 2")}
        for g, spec in groups.items():
            print(f"{g} ({'exclusive' if spec['exclusive'] else 'multi'}): {spec['description']}")
            for t in spec["tags"]:
                print(f"  {t['name']:20s} {counts.get((g, t['name']), 0):5d}  {t['description']}")
        return 0
    if args.retag:
        from convo_index.lock import index_lock, IndexLocked
        try:
            with index_lock(settings.db_path()):
                # every conversation gets a fresh answer for every group (the LLM is asked about all open
                # groups at once), including those whose last answer was empty and so left no evidence
                affected = [r[0] for r in conn.execute("SELECT id FROM conversations")]
                conn.execute("DELETE FROM tag_evidence WHERE method='llm'")
                for i in range(0, len(affected), 500):
                    chunk = affected[i:i + 500]
                    conn.execute(f"DELETE FROM derived_jobs WHERE kind=? AND conversation_id IN ({','.join('?' * len(chunk))})",
                                 (jobs.TAGS, *chunk))
                conn.commit()
                for cid in affected:
                    T.resolve(conn, cid)
        except IndexLocked:
            print("an indexer is running; try again later", file=sys.stderr)
            return 75
        print(f"LLM tag evidence cleared for {len(affected)} conversation(s); the next `convo-index index` "
              "re-tags them (bounded per run). Rules and decisions are kept.")
        return 0
    if not args.id or not args.assign or "=" not in args.assign:
        print("usage: convo-index tag <id> <group>=<tag|none> [--reject] | --list | --retag", file=sys.stderr)
        return 2
    try:
        cid = dbm.resolve_id(conn, args.id)
    except dbm.AmbiguousId as e:
        print(f"ambiguous id {args.id}: " + ", ".join(e.args[0]), file=sys.stderr)
        return 1
    if not dbm.exists(conn, cid):
        print(f"no conversation {args.id}", file=sys.stderr)
        return 1
    group, _, tag = args.assign.partition("=")
    tag = None if tag in ("", "none", "null") else tag
    try:
        T.set_decision(conn, cid, group, tag, "reject" if args.reject else "accept")
    except LookupError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps(T.detail_for(conn, cid)["assignments"]))
    return 0


def cmd_related(args):
    from convo_index import db as dbm, settings as S, references as REF
    settings = S.load()
    conn = dbm.connect(settings.db_path()); dbm.init_schema(conn, settings.db_path())
    t = REF.timeline(conn, args.key, settings)
    if not t["conversations"]:
        print(f"no linked conversations for {args.key}")
        return 1
    print(f"{t['kind']} {t['key']}" + (f"  {t['url']}" if t["url"] else ""))
    for c in t["conversations"]:
        print(f"  {(c['start_ts'] or '')[:10]}  {c['source']:12s} {c['is_resolved'] or '-':8s} {c['id']}  {c['title']}")
    return 0


def _since_ts(spec):
    """'30d' | '12h' | '2026-06-01' -> ISO timestamp lower bound."""
    import re as _re
    from datetime import datetime, timedelta, timezone
    m = _re.fullmatch(r"(\d+)([dhw])", spec or "")
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(days=n) if unit == "d" else timedelta(hours=n) if unit == "h" else timedelta(weeks=n)
        return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", spec or ""):
        return spec + "T00:00:00Z"
    print(f"--since: expected 30d, 12h, 2w or YYYY-MM-DD, got {spec!r}", file=sys.stderr)
    raise SystemExit(2)   # usage error, like argparse


def cmd_search(args):
    from convo_index import settings as S
    from convo_index import db as dbm
    from convo_index import search as SE
    settings = S.load()
    q = (args.query or "").strip()
    since = _since_ts(args.since) if args.since else None   # usage errors before the embedder loads
    qvec = None
    if q and args.mode in ("hybrid", "semantic"):
        from convo_index import server
        qvec = server.embed([q])[0]
        if qvec is None and args.mode == "semantic":
            print(server.semantic_status()[1] or "semantic search unavailable", file=sys.stderr)
            return 1
    conn = dbm.connect(settings.db_path())
    try:
        hits = SE.search(conn, q, qvec, mode=args.mode, limit=max(1, args.limit),
                         source=args.source, project=args.project, tags=args.tag, since=since)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return 0
    for h in hits:
        tags = " ".join(f"#{t}" for ts in (h.get("tags") or {}).values() for t in ts)
        print(f"{(h.get('start_ts') or '')[:10]}  {h['id']:40s} {h.get('title') or ''}"[:140])
        extra = "  ".join(x for x in (h.get("cwd") or "", tags, (h.get("snippet") or "").replace("\n", " ")[:120]) if x)
        if extra:
            print(f"            {extra}")
    print(f"{len(hits)} result(s)", file=sys.stderr)
    return 0


def cmd_export(args):
    from convo_index import settings as S
    from convo_index import db as dbm
    from convo_index import export as X
    settings = S.load()
    conn = dbm.connect(settings.db_path())
    try:
        try:
            cid = dbm.resolve_id(conn, args.id)
        except dbm.AmbiguousId as e:
            print(f"ambiguous id; use one of {e.args[0]}", file=sys.stderr)
            return 1
        body = X.as_markdown(conn, cid, settings, include_tools=args.tools) if args.md else \
            X.as_dict(conn, cid, settings, include_tools=args.tools)
    finally:
        conn.close()
    if body is None:
        print(f"no conversation {args.id}", file=sys.stderr)
        return 1
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=1)
    if args.out:
        out_path = Path(args.out).expanduser()
        if out_path.exists() and not args.force:
            print(f"{out_path} exists; use --force to overwrite", file=sys.stderr)
            return 1
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_backup(args):
    import sqlite3
    from convo_index import settings as S
    src_path = S.load().db_path()
    if not src_path.exists():
        print(f"no database at {src_path}", file=sys.stderr)
        return 1
    raw = str(args.path)
    dest = Path(raw).expanduser()
    if dest.is_dir() or raw.endswith(("/", os.sep)):
        import time
        dest.mkdir(parents=True, exist_ok=True)               # a trailing slash means "a directory, create it"
        stamp = time.strftime('%Y%m%d-%H%M%S')
        dest, n = dest / f"{src_path.name}.{stamp}", 1
        while dest.exists():                                  # two backups within a second
            dest, n = dest.with_name(f"{src_path.name}.{stamp}-{n}"), n + 1
    if dest.exists():
        print(f"{dest} exists; refusing to overwrite", file=sys.stderr)
        return 1
    if not dest.parent.is_dir():
        print(f"{dest.parent} is not a directory; add a trailing slash to create a backup directory", file=sys.stderr)
        return 1
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        except BaseException:
            dst.close()
            dest.unlink(missing_ok=True)          # never leave a half-written copy behind
            raise
        finally:
            dst.close()
    finally:
        src.close()
    print(f"backed up {src_path} -> {dest} ({dest.stat().st_size} bytes)")
    return 0


def _job_cmd(args, job):
    """`schedule` (the daily refresh) and `service` (the UI server from login) share one shape."""
    from convo_index import schedule as SCH
    from convo_index import settings as S
    settings = S.load()
    hour, minute = (args.hour, args.minute) if job == "refresh" else (None, None)
    try:
        if args.action == "status":
            print(json.dumps(SCH.status(settings, job=job), indent=1))
            return 0
        if args.action == "uninstall":
            out = SCH.uninstall(settings, dry_run=args.dry_run, job=job)
            print(json.dumps(out, indent=1))
            return 1 if any(not r["ok"] for r in out.get("results", [])) else 0
        out = SCH.install(settings, hour, minute, dry_run=args.dry_run, job=job)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if args.dry_run or out["kind"] == "cron":
        for path, content in out["files"].items():
            print(f"--- {path}\n{content}")
        for cmd in out["commands"]:
            print("$ " + " ".join(cmd))
        if out.get("crontab"):
            print("add to `crontab -e`:\n" + out["crontab"])
        return 0
    bad = [r for r in out.get("results", []) if not r["ok"]]
    for path in out["files"]:
        print(f"wrote {path}")
    for r in out.get("results", []):
        print(("ok  " if r["ok"] else "FAIL") + " $ " + " ".join(r["cmd"]) + (f"\n     {r['stderr']}" if r["stderr"] and not r["ok"] else ""))
    if job == "refresh":
        print(f"daily refresh at {out['hour']:02d}:{out['minute']:02d}; log: {out['log']}")
    else:
        print(f"UI server runs from login; log: {out['log']}")
    return 1 if bad else 0


def cmd_schedule(args):
    return _job_cmd(args, "refresh")


def cmd_service(args):
    return _job_cmd(args, "serve")


def _green(text):
    """Highlight the one line the user needs, only when writing to a terminal."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    if os.name == "nt":
        os.system("")          # switches the console to VT processing (no-op on Windows Terminal)
    return f"\033[32m{text}\033[0m"


def _wait_for_port(host, port, seconds=15):
    """True once something accepts connections on host:port (the login service starting)."""
    import socket
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def cmd_up(args):
    """The first run in one go: write the config, keep the UI server running from login,
    schedule the daily refresh, start the index build in the background and open the UI.
    Nothing goes to a hosted model until the privacy choice is made (init asks on a
    terminal; otherwise it stays "ask")."""
    import webbrowser
    from convo_index import indexer, server
    from convo_index import schedule as SCH
    from convo_index import settings as S
    rc = cmd_init(args)
    if rc:
        return rc
    S.reset_cache()
    settings = S.load(use_cache=False)
    problems = []
    for job, skip in (("serve", args.no_serve), ("refresh", args.no_schedule)):
        if skip:
            continue
        try:
            out = SCH.install(settings, job=job)
        except ValueError as e:
            print(f"{job}: {e}", file=sys.stderr)
            problems.append(job)
            continue
        if out["kind"] == "cron":
            print(f"{job}: add to `crontab -e`:\n  {out['crontab']}")
            if job == "serve":
                SCH.start_detached(out)
        else:
            bad = [r for r in out.get("results", []) if not r["ok"]]
            if bad:
                problems.append(job)
            print(f"{job}: {'installed' if not bad else 'FAILED: ' + bad[0]['stderr']} ({', '.join(out['files'])})")
    host, port = settings["server"]["host"], int(settings["server"]["port"])
    url = server.advertised_url(host, port)
    if args.wait:
        print("building the index (the first run downloads the 130 MB embedding model) ...")
        rc = indexer.main()
    else:
        plan = SCH.plan(settings, job="refresh")
        SCH.start_detached(plan)
        print(f"building the index in the background (the first run downloads the 130 MB embedding model);"
              f" progress: tail -f {plan['log']}")
        rc = 0
    if settings["privacy"].get("hosted_processing") == "ask":
        print("summaries and tags wait for a privacy decision: run `convo-index init` on a terminal "
              "(or `convo-index init --yes` to allow hosted LLM processing)")
    if not args.no_serve:
        if _wait_for_port("127.0.0.1" if host in ("0.0.0.0", "::", "") else host, port):
            print(_green(f"convo-index is at {url}"))
            if not args.no_open:
                webbrowser.open(url)
        else:
            print(f"the UI server did not answer at {url} within 15 s; see {SCH.log_path(settings, 'serve')}", file=sys.stderr)
            problems.append("serve")
    if problems:
        print(f"up finished with problems: {', '.join(dict.fromkeys(problems))}", file=sys.stderr)
        return rc or 1
    return rc


def cmd_resummarize(args):
    from convo_index import settings as S
    from convo_index import db as dbm
    if not args.id and not args.all:
        print("give a conversation id or --all", file=sys.stderr)
        return 2
    conn = dbm.connect(S.load().db_path())
    try:
        if args.all:
            n = conn.execute("DELETE FROM derived_jobs WHERE kind='summary'").rowcount
            conn.execute("UPDATE conversation_summary SET summarized_revision=NULL")
        else:
            try:
                cid = dbm.resolve_id(conn, args.id)
            except dbm.AmbiguousId as e:
                print(f"ambiguous id; use one of {e.args[0]}", file=sys.stderr)
                return 1
            if not dbm.exists(conn, cid):
                print(f"no conversation {args.id}", file=sys.stderr)
                return 1
            n = conn.execute("DELETE FROM derived_jobs WHERE kind='summary' AND conversation_id=?", (cid,)).rowcount
            conn.execute("UPDATE conversation_summary SET summarized_revision=NULL WHERE conversation_id=?", (cid,))
        # a summary made from the current revision would otherwise be adopted back without an LLM call
        conn.commit()
    finally:
        conn.close()
    print(f"{n} summary job(s) forgotten; the next `convo-index index` re-summarizes them")
    return 0


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
    sub.add_parser("reembed", help="drop all vectors so the next index run re-embeds with the configured embedder")
    p_cfg = sub.add_parser("config", help="config file helpers")
    p_cfg.add_argument("what", choices=["path", "show"])
    p_rel = sub.add_parser("related", help="timeline of every conversation linked to a ticket, PR or branch key")
    p_rel.add_argument("key", help="e.g. SAAS-1234, github.com/o/r#38068, a branch name (resolved to its project when unambiguous)")
    p_tag = sub.add_parser("tag", help="tag a conversation by hand, list tags, or re-run LLM tagging")
    p_tag.add_argument("id", nargs="?", help="conversation id (namespaced or bare)")
    p_tag.add_argument("assign", nargs="?", help="<group>=<tag> or <group>=none")
    p_tag.add_argument("--reject", action="store_true", help="record a rejection instead of an acceptance")
    p_tag.add_argument("--list", action="store_true", help="show groups, tags and counts")
    p_tag.add_argument("--retag", action="store_true", help="clear LLM tag evidence so the next index re-tags")
    p_tag.add_argument("--calibrate", action="store_true", help="how past neighbour suggestions fared against your decisions")
    p_tag.add_argument("--suggestions", action="store_true", help="list open neighbour suggestions")
    p_search = sub.add_parser("search", help="search the index from the terminal")
    p_search.add_argument("query", nargs="?", default="", help="search text; empty lists recent conversations")
    p_search.add_argument("--source", choices=["claude_code", "opencode", "codex", "antigravity", "pi"])
    p_search.add_argument("--tag", action="append", default=[], help="group:tag filter (repeatable)")
    p_search.add_argument("--project", help="repo root / cwd")
    p_search.add_argument("--since", help="e.g. 30d, 12h, 2026-06-01")
    p_search.add_argument("--mode", choices=["hybrid", "keyword", "semantic"], default="hybrid")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--json", action="store_true")
    p_exp = sub.add_parser("export", help="export one conversation as JSON (default) or Markdown")
    p_exp.add_argument("id")
    p_exp.add_argument("--md", action="store_true", help="Markdown transcript")
    p_exp.add_argument("--tools", action="store_true", help="include tool calls and results")
    p_exp.add_argument("-o", "--out", help="write to this file instead of stdout")
    p_exp.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    p_bak = sub.add_parser("backup", help="copy the database (SQLite backup API, WAL-safe) to a path")
    p_bak.add_argument("path")
    p_sch = sub.add_parser("schedule", help="daily refresh via LaunchAgent (macOS), systemd user timer (Linux) or a crontab line")
    p_sch.add_argument("action", choices=["install", "uninstall", "status"])
    p_sch.add_argument("--hour", type=int); p_sch.add_argument("--minute", type=int)
    p_sch.add_argument("--print", dest="dry_run", action="store_true", help="show what would be written and run")
    p_svc = sub.add_parser("service", help="keep the UI server running from login (LaunchAgent / systemd user service / @reboot line)")
    p_svc.add_argument("action", choices=["install", "uninstall", "status"])
    p_svc.add_argument("--print", dest="dry_run", action="store_true", help="show what would be written and run")
    p_up = sub.add_parser("up", help="first run: init, UI server from login, daily refresh, then build the index")
    p_up.add_argument("--yes", "-y", action="store_true", help="no prompts; allow hosted processing if a CLI was found")
    p_up.add_argument("--no-serve", action="store_true", help="do not install the UI server service")
    p_up.add_argument("--no-schedule", action="store_true", help="do not install the daily refresh")
    p_up.add_argument("--no-open", action="store_true", help="do not open the UI in a browser")
    p_up.add_argument("--wait", action="store_true", help="build the index in the foreground instead of the background")
    sub.add_parser("mcp", help="MCP server over stdio (search_conversations, get_conversation, get_related)")
    p_jobs = sub.add_parser("jobs", help="derived-job maintenance")
    p_jobs.add_argument("action", choices=["unblock"], help="unblock: re-queue jobs blocked on auth/config now")
    p_res = sub.add_parser("resummarize", help="forget summary jobs so the next index run re-summarizes")
    p_res.add_argument("id", nargs="?", help="one conversation; omit with --all for every conversation")
    p_res.add_argument("--all", action="store_true")
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
    if args.cmd == "reembed":
        return cmd_reembed(args)
    if args.cmd == "tag":
        return cmd_tag(args)
    if args.cmd == "related":
        return cmd_related(args)
    if args.cmd == "search":
        return cmd_search(args)
    if args.cmd == "export":
        return cmd_export(args)
    if args.cmd == "backup":
        return cmd_backup(args)
    if args.cmd == "schedule":
        return cmd_schedule(args)
    if args.cmd == "service":
        return cmd_service(args)
    if args.cmd == "up":
        return cmd_up(args)
    if args.cmd == "mcp":
        from convo_index import mcp
        return mcp.serve()
    if args.cmd == "resummarize":
        return cmd_resummarize(args)
    if args.cmd == "jobs":
        from convo_index import settings as S, db as dbm, jobs
        conn = dbm.connect(S.load().db_path())
        try:
            out = jobs.unblock(conn, S.load(), force=True)
        finally:
            conn.close()
        print(f"re-queued: {out or 'nothing was blocked'}")
        return 0
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
