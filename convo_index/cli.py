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
            if not getattr(args, "from_up", False):        # `up` lists it with everything else to do
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


def cmd_handoff(args):
    """Print the prompt that seeds a new session in another harness: paste it there. What the
    preview would show beside it (known problems, credential-looking strings, size) goes to
    stderr, so stdout is only the prompt. With --start the new session is run here instead and
    its replies are printed."""
    from convo_index import settings as S
    from convo_index import db as dbm
    from convo_index import export as X
    from convo_index import handoff as H
    from convo_index.sources import base as SB
    settings = S.load()
    kind = "review" if args.review else "continue"
    if args.start and args.start not in SB.new_session_targets():
        print(f"cannot start a session in {args.start} here; choose one of: {', '.join(SB.new_session_targets()) or 'none'}",
              file=sys.stderr)
        return 1
    conn = dbm.connect(settings.db_path())
    try:
        try:
            cid = dbm.resolve_id(conn, args.id)
        except dbm.AmbiguousId as e:
            print(f"ambiguous id; use one of {e.args[0]}", file=sys.stderr)
            return 1
        h = X.head(conn, cid)
        if h is None:
            print(f"no conversation {args.id}", file=sys.stderr)
            return 1
        why = H.refusal(settings, h)
        if why:
            print(f"not handing off: {why}", file=sys.stderr)
            return 1
        if args.start and not (h["cwd"] and os.path.isdir(h["cwd"])):
            print(f"not starting a session: the conversation's directory is gone: {h['cwd'] or '(none recorded)'}",
                  file=sys.stderr)
            return 1
        H.prune_copies(settings.db_path())
        where = Path(args.transcript_dir).expanduser() if args.transcript_dir else H.copy_dir(settings.db_path(), cid)
        try:                       # before the digest: a bad directory should not cost an LLM call
            transcript = H.write_transcript(conn, cid, where, settings)
        except OSError as e:
            print(f"cannot write the transcript to {where}: {e.strerror or e}", file=sys.stderr)
            return 1
        d, status = None, None
        if kind == "continue":
            if args.no_digest:
                status = "skipped with --no-digest"
            else:
                print("writing the handoff note with the summary backend…", file=sys.stderr)
                d, status = H.digest(X.messages(conn, cid), h["title"] or "", settings)
        out = H.build(conn, cid, kind, digest_result=d, digest_status=status, transcript_path=transcript,
                      repo=H.repo_state(h["cwd"]))
    finally:
        conn.close()
    if not args.start:
        print(out["prompt"], end="")
    note = [f"{kind}: ~{out['tokens']:,} tokens" + (" (over the budget)" if out["over_budget"] else "")
            + (f", {out['dropped']} assistant message(s) left out" if out["dropped"] else ""),
            f"transcript: {transcript}"]
    if out["digest_status"] and out["digest_status"] != "ok":
        note.append(f"no handoff note: {out['digest_status']}")
    note += [f"known problem: {p['kind']}: {p['text']}" for p in out["problems"][-H.MAX_PROBLEMS:]]
    if len(out["problems"]) > H.MAX_PROBLEMS:
        note.append(f"known problems: {len(out['problems']) - H.MAX_PROBLEMS} earlier ones not listed")
    note += [f"looks like a secret, remove before sending: {s['kind']} {s['sample']}" for s in out["secrets"]]
    note += [f"looks like a secret in the transcript file, which the prompt points at: {s['kind']} {s['sample']}"
             for s in out["transcript_secrets"]]
    print("\n".join(note), file=sys.stderr)
    if args.start:
        return _start_handoff(args, settings, cid, h, kind, out, transcript)
    return 0


def _ingest_only():
    """The refresh a browser turn runs after the harness exits: ingest, no LLM jobs."""
    import subprocess
    from convo_index.indexer import EXIT_LOCKED
    res = subprocess.run([sys.executable, "-m", "convo_index.indexer", "--no-jobs"], capture_output=True, text=True, check=False)
    return {"ok": res.returncode == 0, "busy": res.returncode == EXIT_LOCKED, "error": (res.stderr or "")[-500:]}


def _start_handoff(args, settings, cid, h, kind, out, transcript):
    """Run the new session here: refuse credential-looking strings unless told otherwise,
    stream the replies (text to stdout, the rest to stderr), and say how to resume it."""
    from convo_index import handoff as H
    from convo_index import turns as TURNS
    from convo_index.sources.base import resume_command
    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            print(f"cannot read {args.prompt_file}: {e.strerror or e}", file=sys.stderr)
            return 1
        # all of the transcript file: transcript_secrets leaves out what the generated prompt showed
        secrets = H.find_secrets(prompt) + H.find_secrets(transcript.read_text(encoding="utf-8"))
    else:
        prompt, secrets = out["prompt"], out["secrets"] + out["transcript_secrets"]
    if secrets and not args.ignore_secrets:
        print("not starting: the prompt or the transcript file holds what looks like a credential (listed above). "
              "Edit the prompt (print it without --start, then pass --prompt-file), or pass --ignore-secrets.", file=sys.stderr)
        return 1
    t = settings["ui"]["turns"]
    runner = TURNS.TurnRunner(timeout=int(t.get("timeout_seconds") or 1200), max_concurrent=1, refresh=_ingest_only)
    try:
        turn, state = H.start_session(runner, settings.db_path(), cid, args.start, prompt, h["cwd"], kind=kind,
                                      edits=args.edit, read_dirs=(transcript.parent,),
                                      model=H.session_model(settings, args.start, args.model))
    except (ValueError, LookupError, TURNS.TurnRefused) as e:
        print(f"not starting: {e}", file=sys.stderr)
        return 1
    mode = "edit" if args.edit and kind == "continue" else "read-only"
    print(f"starting a new {args.start} session ({mode}) in {h['cwd']}; Ctrl-C stops it", file=sys.stderr)
    last = None
    try:
        for _, ev in runner.events(turn.id):
            if ev is None:
                continue
            last = ev
            if ev["kind"] == "text":
                print(ev["text"], flush=True)
            elif ev["kind"] == "tool":
                print(f"→ {ev.get('name') or 'tool'}", file=sys.stderr)
            elif ev["kind"] == "denied":
                print(f"✗ {ev.get('name') or 'tool'}: not allowed in this mode", file=sys.stderr)
    except KeyboardInterrupt:
        runner.stop(turn.id)
        for _, ev in runner.events(turn.id):
            last = ev if ev is not None else last
    ok = bool(last) and last["kind"] == "done"
    if not ok and last:
        print(f"the session failed: {last.get('message') or 'unknown error'}", file=sys.stderr)
        if last.get("stderr"):
            print(last["stderr"], file=sys.stderr)
    if state["id"]:
        relation = "reviews" if kind == "review" else "continues"
        print(f"new conversation {state['id']} {relation} {cid}", file=sys.stderr)
        resume = resume_command(state["id"], h["cwd"])
        if resume:
            print(f"resume it: {resume}", file=sys.stderr)
        if last and not last.get("indexed"):
            print(f"not indexed yet: {last.get('reason') or 'run convo-index index'}", file=sys.stderr)
    return 0 if ok else 1


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
    if out["kind"] == "none":
        what = "keep the UI server running" if job == "serve" else "run the daily refresh"
        print(f"nothing installed: {out['reason']}, so nothing here can {what}.\n"
              f"run it yourself instead:\n  {out['manual']}", file=sys.stderr)
        return 1
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


def _turn_token_of_running_server(server, settings, seconds=3):
    """The token file the server writes just after it binds: on a first start the port can
    answer a moment before the file exists (a restart keeps the token, so an older file is
    already right)."""
    import time
    deadline = time.monotonic() + seconds
    while True:
        token = server.read_turn_token(settings)
        if token or time.monotonic() >= deadline:
            return token
        time.sleep(0.1)


def _log_tail(path, lines=5, window=8192):
    """The last few lines of a log, reading only its end (a long-lived log can be large)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - window))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    found = text.splitlines()
    if size > window and found:
        found = found[1:]                      # the first line was cut by the seek
    return found[-lines:]


def _attention(items):
    """The numbered list of what the user still has to do, printed last so it is what they see."""
    if not items:
        return
    head = "Needs your attention:"
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        head = f"\033[1;33m{head}\033[0m"
    print(f"\n{head}")
    for n, (text, cmds, *then) in enumerate(items, 1):
        print(f"  {n}. {text}")
        for c in cmds:
            print(f"       {c}")
        for more, more_cmds in then:           # a second step of the same item
            print(f"     {more}")
            for c in more_cmds:
                print(f"       {c}")


def cmd_up(args):
    """The first run in one go: write the config, keep the UI server running from login,
    schedule the daily refresh, start the index build in the background and open the UI.
    Nothing goes to a hosted model until the privacy choice is made (init asks on a
    terminal; otherwise it stays "ask"). Where no scheduler works (a container) the server is
    started for this session and the output says what to run by hand. Whatever needs the
    user ends up in one numbered list at the end."""
    import webbrowser
    from convo_index import indexer, server
    from convo_index import schedule as SCH
    from convo_index import settings as S
    args.from_up = True
    try:
        sys.stdout.reconfigure(line_buffering=True)   # piped (an installer), stderr notes must not jump ahead
    except (AttributeError, ValueError):
        pass
    rc = cmd_init(args)
    if rc:
        return rc
    S.reset_cache()
    settings = S.load(use_cache=False)
    print()
    problems, todo, told = [], [], set()
    restart, start = None, "convo-index serve"   # the "start it again" item, and the command that starts it
    for job, skip in (("serve", args.no_serve), ("refresh", args.no_schedule)):
        if skip:
            continue
        name = "UI server" if job == "serve" else "daily refresh"
        try:
            out = SCH.install(settings, job=job)
        except ValueError as e:
            print(f"{name}: not set up: {e}", file=sys.stderr)
            problems.append(job)
            todo.append((f"The {name} could not be set up: {e}", []))
            continue
        bad = [r for r in out.get("results", []) if not r["ok"]]
        if out["kind"] in ("launchd", "systemd", "schtasks") and not bad:
            print(f"{name}: installed ({', '.join(out['files'])})")
            continue
        if bad:
            problems.append(job)
            print(f"{name}: `{' '.join(bad[0]['cmd'])}` failed: {bad[0]['stderr'] or 'no error output'}", file=sys.stderr)
        elif out.get("reason") and out["reason"] not in told:
            told.add(out["reason"])
            print(f"no {'usable ' if out['kind'] == 'none' else 'built-in '}scheduler: {out['reason']}")
        if job == "serve":
            SCH.start_detached(out)
            print(f"{name}: running for this session only")
            if out["kind"] == "cron":
                restart = ("The UI server will not start again after a reboot. If cron runs on this machine, "
                           "add this line with `crontab -e`:", [out["crontab"]])
            else:
                restart = ("The UI server will not start again after this machine or container restarts. "
                           "Start it again with:", [out["manual"]])
            start = out["manual"]
            todo.append(restart)
        else:
            print(f"{name}: not scheduled")
            if out["kind"] == "cron":
                todo.append(("New conversations are not indexed automatically. If cron runs on this machine, "
                             "add this line with `crontab -e` to refresh daily:", [out["crontab"]]))
            else:
                todo.append(("New conversations are not indexed automatically. Run this when you want "
                             "them searchable:", [out["manual"]]))
    host, port = settings["server"]["host"], int(settings["server"]["port"])
    url = server.advertised_url(host, port)
    if args.wait:
        print("index: building (the first run downloads the 130 MB embedding model) ...")
        rc = indexer.main()
    else:
        plan = SCH.plan(settings, job="refresh")
        SCH.start_detached(plan)
        print(f"index: building in the background (the first run downloads the 130 MB embedding model); "
              f"progress: tail -f {plan['log']}")
        rc = 0
    if settings["privacy"].get("hosted_processing") == "ask":
        backends = sorted({settings["llm"][j].get("backend") for j in ("classify", "summary")} - {None, ""})
        todo.insert(0, (f"Summaries and tags are off until you decide whether conversation text may be sent "
                        f"to {', '.join(backends) or 'a hosted model'}. Run one of:",
                        ["convo-index init           # asks you", "convo-index init --yes     # allows it"]))
    if not args.no_serve:
        if _wait_for_port("127.0.0.1" if host in ("0.0.0.0", "::", "") else host, port):
            url = server.advertised_url(host, port, _turn_token_of_running_server(server, settings))
            print()
            print(_green(f"convo-index is at {url}"))
            if not args.no_open:
                webbrowser.open(url)
        else:
            log = SCH.log_path(settings, "serve")
            print(f"UI server: did not answer at {url} within 15 s", file=sys.stderr)
            problems.append("serve")
            tail = _log_tail(log)
            if restart:                        # "start it again after a restart" would contradict this item
                todo.remove(restart)
            if tail:
                todo.append((f"The UI server is not running. Its log ({log}) ends with:", tail,
                             ("After fixing that, start it with:", [start])))
            else:
                todo.append((f"The UI server is not running and wrote nothing to {log}. Start it with:", [start]))
    _attention(todo)
    if problems:
        return rc or 1
    return rc


def cmd_stop(args):
    """Stop the running UI server through its shutdown endpoint (running turns end first). The
    login service stays installed: it starts the server again at the next login, as do
    `convo-index serve` and `up`. Exit 0 when it stopped or was not running."""
    import http.client
    import socket
    import time
    from urllib.parse import urlsplit
    from convo_index import server
    from convo_index import settings as S
    settings = S.load()
    host, port = settings["server"]["host"], int(settings["server"]["port"])
    netloc = urlsplit(server.advertised_url(host, port)).netloc     # loopback, bracketed for IPv6
    target = urlsplit(f"http://{netloc}").hostname
    token = server.read_turn_token(settings)
    conn = http.client.HTTPConnection(target, port, timeout=10)
    try:
        conn.request("POST", "/api/shutdown", body="{}",
                     headers={"Host": netloc, "Content-Type": "application/json", server.CSRF_HEADER: "1",
                              server.TURN_TOKEN_HEADER: token or ""})
        res = conn.getresponse()
        body = res.read().decode("utf-8", "replace")
    except ConnectionRefusedError:
        print(f"no server is running at http://{netloc}")
        return 0
    except OSError as e:
        print(f"could not reach the server at http://{netloc}: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if res.status != 202:
        try:
            reason = json.loads(body).get("error") or body
        except ValueError:
            reason = body
        print(f"the server at http://{netloc} refused to stop ({res.status}): {reason}", file=sys.stderr)
        if not token:
            print(f"no turn token at {server.turn_token_path(settings)}; is the server running as this user "
                  "with this configuration?", file=sys.stderr)
        return 1
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((target, port), timeout=1):
                pass
        except OSError:
            print("server stopped; start it again with `convo-index serve` (or at the next login, if the service is installed)")
            return 0
        time.sleep(0.2)
    print(f"the server accepted the request but is still listening after {args.timeout:g} s", file=sys.stderr)
    return 1


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
    p_hand = sub.add_parser("handoff", help="print a prompt that continues (or reviews) a conversation in another harness")
    p_hand.add_argument("id")
    p_hand.add_argument("--review", action="store_true", help="a second opinion instead of a continuation")
    p_hand.add_argument("--no-digest", action="store_true", help="skip the LLM handoff note (continue only)")
    p_hand.add_argument("--transcript-dir", help="where to write the full transcript the prompt points at")
    p_hand.add_argument("--start", metavar="TOOL", help="run the new session here, in claude_code or opencode")
    p_hand.add_argument("--edit", action="store_true", help="with --start: let it edit files and run the shell (continue only)")
    p_hand.add_argument("--model", help="with --start: the model the new session runs (else [handoff.models])")
    p_hand.add_argument("--prompt-file", help="with --start: send this (edited) prompt instead of the generated one")
    p_hand.add_argument("--ignore-secrets", action="store_true", help="with --start: send even if a credential is found")
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
    p_stop = sub.add_parser("stop", help="stop the running UI server (running turns end first; the login service stays installed)")
    p_stop.add_argument("--timeout", type=float, default=15, help="seconds to wait for the port to close")
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
    if args.cmd == "stop":
        return cmd_stop(args)
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
    if args.cmd == "handoff":
        return cmd_handoff(args)
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
