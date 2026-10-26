"""Indexer: walk every enabled source adapter, load changed conversations, classify,
extract tickets, embed, store, then run derived jobs (summaries) with retry state.

Change detection compares the adapter's `change_key` with the revision stored from the
previous successful load, so nothing is re-read when the source is untouched.

Exit codes: 0 all good, 1 some source or conversation failed, 75 (EX_TEMPFAIL) another
indexer holds the lock. The last stdout line is a JSON totals object for callers such as
the web server's refresh endpoint.
"""
from __future__ import annotations

import fnmatch
import json
import subprocess
import sys
from pathlib import Path

from convo_index.classify import extract_tickets
from convo_index import tags as T
from convo_index import references as REF
from convo_index.archive import write_archive_copy
from convo_index.lock import index_lock, IndexLocked
from convo_index.model import Conversation
from convo_index.sources.base import all_adapters, UnreadableSource
from convo_index.providers import registry as R
from convo_index import db as dbm
from convo_index import jobs
from convo_index import settings as S

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 75  # EX_TEMPFAIL: another indexer is running, try again later

META_EMBED_FINGERPRINT = "embedding_fingerprint"


def project_root(cwd):
    """Resolve a conversation's cwd to its git repository root, so worktrees and
    subfolders of the same repo group together. Falls back to cwd when it isn't a
    git repo or no longer exists. Uses --git-common-dir so linked worktrees map to
    the main repo rather than the worktree path."""
    if not cwd:
        return cwd
    try:
        res = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return cwd
    if res.returncode != 0:
        return cwd
    common = res.stdout.strip()  # e.g. /Users/me/repo/.git
    if common.endswith("/.git"):
        return common[:-len("/.git")]
    return cwd


def enabled_adapters(settings=None):
    settings = settings or S.load()
    out = []
    for a in all_adapters():
        spec = settings["sources"].get(a.name) or {}
        if spec.get("enabled", True) and a.available():
            out.append(a)
    return out


def cwd_excluded(cwd, patterns):
    """Does `cwd` fall under any `exclude_cwd` pattern? A pattern names a project, so it covers
    the directory itself as well as everything below it: `~/client/*` (or `~/client/**`)
    and a plain `~/client` all exclude a session started in `~/client`. Other patterns are
    fnmatch globs on the whole cwd (`*` crosses `/`). A session with no recorded cwd never
    matches; `enabled = false` is the way to switch a whole source off."""
    if not cwd:
        return False
    if isinstance(patterns, str):   # `exclude_cwd = "~/x"` must not be read as the characters ~, /, x
        patterns = [patterns]
    cwd = cwd.replace("\\", "/").rstrip("/") or "/"     # one separator, whatever the tool recorded
    for raw in patterns:
        # Path() normalises the pattern ("/secret/*/" -> "/secret/*", "/a/./b" -> "/a/b")
        try:
            pat = str(Path(raw).expanduser()).replace("\\", "/")
        except RuntimeError:          # "~nobody/x": no such user; keep the literal pattern
            pat = str(Path(raw)).replace("\\", "/")
        root = (pat[:-3] if pat.endswith("/**") else pat.removesuffix("/*")).rstrip("/")
        # The literal path rule comes first so a project directory whose name contains a glob
        # character ("~/[archived]/x") is still excluded by its path.
        if root and (cwd == root or cwd.startswith(root + "/")):
            return True
        if fnmatch.fnmatch(cwd, pat):
            return True
    return False


def excluded_by_config(conv: Conversation, settings):
    """`[sources.<name>].exclude_cwd` patterns (matched against cwd) keep whole projects out of
    the index and therefore away from every provider."""
    return cwd_excluded(conv.cwd, (settings["sources"].get(conv.source) or {}).get("exclude_cwd") or [])


def purge_excluded(conn, settings):
    """Remove already-indexed conversations that now match `exclude_cwd`. Runs before any
    derived job so excluded transcripts are never read back and sent to a provider."""
    removed = 0
    for source, spec in settings["sources"].items():
        patterns = (spec or {}).get("exclude_cwd") or []
        if not patterns:
            continue
        rows = conn.execute("SELECT id, cwd FROM conversations WHERE source=?", (source,)).fetchall()
        for cid, cwd in rows:
            if cwd_excluded(cwd, patterns) and dbm.remove_conversation(conn, cid):
                removed += 1
    return removed


def default_embed_fn(conn, settings, warnings):
    """Build the embed callable for this run, or a no-op when embeddings are unavailable,
    blocked by the privacy policy, or were produced by a different embedder (mixing vector
    spaces would corrupt search)."""
    fn, _ = embed_fn_and_fingerprint(conn, settings, warnings)
    return fn


def embed_fn_and_fingerprint(conn, settings, warnings):
    """(embed callable, embedder fingerprint or None when embeddings are disabled this run)."""
    noop = lambda texts: [None] * len(texts)
    blocked = R.embedder_blocked_reason(settings)
    if blocked:
        warnings.append(f"embeddings: {blocked}; vectors skipped")
        return noop, None
    emb = R.make_embedder(settings)
    if emb is None:
        warnings.append("embeddings: no backend configured; vectors skipped")
        return noop, None
    stored = dbm.get_meta(conn, META_EMBED_FINGERPRINT)
    if stored is None:
        dbm.set_meta(conn, META_EMBED_FINGERPRINT, emb.fingerprint)
    elif stored != emb.fingerprint:
        warnings.append(f"embeddings: configured embedder ({emb.fingerprint}) differs from the one the "
                        f"index was built with ({stored}); vectors skipped. Run `convo-index reembed`.")
        return noop, None
    state = {"reported": False}

    def embed_fn(texts):
        res = emb.embed(texts)
        if res.error and not state["reported"]:
            state["reported"] = True
            warnings.append(f"embeddings: {res.error}: {res.detail}")
        return res.vectors
    return embed_fn, emb.fingerprint


def index_conversation(conn, conv: Conversation, revision, embed_fn=None, summarize=True, settings=None):
    """Store one normalized conversation and run its derived jobs. Returns the upsert stats,
    or None when the conversation was deleted by the user while this ran (nothing written)."""
    settings = settings or S.load()
    embed_fn = embed_fn or (lambda texts: [None] * len(texts))
    if not conv.project_root:
        conv.project_root = project_root(conv.cwd)
    # category/category_source are a mirror of the workspace tag group, set by tags.resolve
    # below; the upsert writes placeholders that resolve() overwrites in the same run.
    category, source = "unknown", None
    full_text = "\n".join(m.text for m in conv.messages)
    tickets = extract_tickets(full_text, settings)
    archive_path = None
    src = Path(conv.source_ref) if conv.source_ref and not conv.source_ref.startswith("sqlite:") else None
    if src is not None and src.is_file():
        archive_path = str(write_archive_copy(
            src, {"session_id": conv.id, "title": conv.title, "start_ts": conv.start_ts},
            category, tickets, settings.archive_dir()))
    # References are extracted before the write and stored in the same transaction as the
    # conversation row, so a revision can never advance without its references.
    refs = REF.extract(conv, settings)
    stats = dbm.upsert_conversation(conn, conv, category, source, tickets, archive_path,
                                    revision, embed_fn, extra=lambda c: REF._store_locked(c, conv, refs))
    if stats is None:
        # Deleted by the user meanwhile: the archive copy we just wrote must not linger.
        if archive_path:
            try:
                Path(archive_path).unlink(missing_ok=True)
            except OSError:
                pass
        return None
    stats["references"] = len(refs)
    # Rules are cheap and deterministic: apply them at ingest so a new conversation is tagged
    # (and its category mirror set) without waiting for the LLM dispatcher.
    groups = T.load_groups(conn)
    if not groups:   # direct callers (tests, tools) may not have synced definitions yet
        T.sync_definitions(conn, settings)
        groups = T.load_groups(conn)
    if groups:
        T.apply_rules(conn, conv.id, {"cwd": conv.cwd, "git_branch": conv.git_branch, "title": conv.title,
                                      "first_prompt": conv.first_prompt, "source": conv.source}, groups)
        stats["tags"] = T.resolve(conn, conv.id, groups)
        stats["tags_llm_needed"] = not jobs.record_tags_covered(conn, conv.id, settings)
    if summarize:
        stats["summary"] = jobs.run_summary(conn, conv.id, conv.title, revision,
                                            jobs.transcript_from_db(conn, conv.id), settings)
    return stats


def run(adapters=None, db_path=None, settings=None):
    """Index every enabled source. Returns totals, or None when another indexer holds the lock."""
    settings = settings or S.load()
    db_path = db_path or settings.db_path()
    try:
        with index_lock(db_path):
            return _run(adapters, db_path, settings)
    except IndexLocked as e:
        print(f"skipped: {e}", file=sys.stderr)
        return None


def _run(adapters, db_path, settings):
    conn = dbm.connect(db_path)
    dbm.init_schema(conn, db_path, have_lock=True, dim=settings["embeddings"].get("dim"))  # run() holds the lock
    totals = {"indexed": 0, "skipped": 0, "removed": 0, "failed": 0, "sources": {}, "warnings": [],
              "jobs": {}}
    T.sync_definitions(conn, settings)   # config -> tag_groups/tags; one-time category conversion
    n = REF.ensure_current(conn, settings)   # extractor/config changed: re-scan stored transcripts once
    if n:
        totals["references_rescanned"] = n
    totals["removed"] += purge_excluded(conn, settings)
    embed_fn, embedder_fp = embed_fn_and_fingerprint(conn, settings, totals["warnings"])
    for adapter in (adapters if adapters is not None else enabled_adapters(settings)):
        counts = {"indexed": 0, "skipped": 0, "removed": 0, "failed": 0}
        totals["sources"][adapter.name] = counts
        try:
            refs = list(adapter.discover())
        except Exception as e:  # a broken source must not abort the others
            counts["failed"] += 1
            print(f"WARN: {adapter.name}: discovery failed: {e}", file=sys.stderr)
            refs = []
        for ref in refs:
            try:
                if dbm.is_deleted(conn, ref.id) or dbm.get_revision(conn, ref.id) == ref.change_key:
                    counts["skipped"] += 1
                    continue
                try:
                    loaded = adapter.load(ref)
                except UnreadableSource as e:
                    # Records exist but cannot be interpreted: keep what we have, report it.
                    counts["failed"] += 1
                    print(f"WARN: {adapter.name}: unreadable {ref.locator} ({ref.native_id}): {e}",
                          file=sys.stderr)
                    continue
                if loaded is not None and excluded_by_config(loaded[0], settings):
                    loaded = None
                if loaded is None:
                    # Nothing indexable any more (emptied, reverted, excluded by config, or
                    # recognised as our own LLM call). Drop stale content if we had indexed it.
                    if dbm.remove_conversation(conn, ref.id):
                        counts["removed"] += 1
                    else:
                        counts["skipped"] += 1
                    continue
                conv, revision = loaded
                # Derived work (summaries, reclassification) is dispatched once, bounded,
                # after ingestion, so a large import never runs an unbounded number of LLM calls.
                if index_conversation(conn, conv, revision, embed_fn, summarize=False, settings=settings) is None:
                    counts["skipped"] += 1
                    continue
                counts["indexed"] += 1
            except Exception as e:  # one bad conversation must not abort the run
                counts["failed"] += 1
                print(f"WARN: {adapter.name}: failed {ref.locator} ({ref.native_id}): {e}",
                      file=sys.stderr)
        print(f"{adapter.name}: indexed={counts['indexed']} skipped={counts['skipped']} "
              f"removed={counts['removed']} failed={counts['failed']}", file=sys.stderr)
        for k in ("indexed", "skipped", "removed", "failed"):
            totals[k] += counts[k]

    # One bounded dispatcher for derived work, each conversation at most once per run:
    # summaries first (they feed the conversation vector), then conversation vectors (they
    # feed the tag prompt's examples), then LLM tagging of the groups still open.
    try:
        totals["jobs"] = jobs.dispatch(conn, settings, embed_fn=embed_fn, embedder_fp=embedder_fp)
        totals["jobs"]["vectors_backfilled"] = jobs.backfill_vectors(conn, embed_fn, settings=settings)
    except Exception as e:
        totals["failed"] += 1
        print(f"WARN: derived jobs failed: {e}", file=sys.stderr)
    for w in totals["warnings"]:
        print(f"WARN: {w}", file=sys.stderr)
    conn.close()
    print(json.dumps(totals), flush=True)
    return totals


def main():
    totals = run()
    if totals is None:
        return EXIT_LOCKED
    return EXIT_FAILED if totals["failed"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
