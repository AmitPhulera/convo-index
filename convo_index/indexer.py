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
import subprocess
from pathlib import Path

from convo_index.model import Conversation
from convo_index.sources.base import all_adapters
from convo_index import db as dbm
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
