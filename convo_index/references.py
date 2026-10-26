"""References and related work (docs/design.md §9).

Every conversation is scanned for tickets, GitHub PR/issue links, branches, commits and
URLs. Each hit is stored with the message it came from, the matched span and the method,
so a link can always be explained and, if wrong, switched off. Only *linkable* references
connect conversations:

  ticket   key matches the configured regex and appears in user/assistant text (not only
           in tool output, where changelogs and docs mention unrelated keys)
  pr/issue a full URL on a configured GitHub host, or owner/repo#n; a bare "PR 123" only
           when the source recorded the conversation's own git_origin_url
  branch   the source's recorded branch (not a default branch), or an explicit
           `checkout -b X` / `--head X` in the text
  commit, url  never link; kept for display and search

Related work is computed directly (two conversations are related iff they share a linkable
key), never transitively, so one broad session cannot glue a whole repository together.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from convo_index import db as dbm
from convo_index import settings as S
from convo_index.model import Conversation, KIND_TEXT

LINKING_KINDS = ("ticket", "pr", "issue", "branch")
NEVER_LINK = ("commit", "url")   # kept for display/search only; no override can link them
URL_CAP = 50           # distinct urls stored per conversation (display/search only)
EXTRACTOR_VERSION = 2  # bump when extraction rules change: existing conversations are re-scanned
META_EXTRACTOR = "references_extractor"

URL_STRIP = ".,;:!?'\"`>)]}*_~"
COMMIT_CAP = 50
_URL_RE = re.compile(r"https?://[^\s<>()\"'\]`]+")
_GH_PATH_RE = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(pull|issues)/(\d+)(?=$|[/?#])")
# owner/repo#n: not preceded by a path character (src/pkg/file.py#12 is a file:line, not a repo)
_SHORT_REF_RE = re.compile(r"(?<![\w/.\-])([A-Za-z0-9_-][A-Za-z0-9_.-]*)/([A-Za-z0-9_-][A-Za-z0-9_.-]*)#(\d+)\b")
_CODE_EXT_RE = re.compile(r"\.(py|pyi|ts|tsx|js|jsx|mjs|go|rs|rb|java|kt|swift|c|h|cpp|hpp|cs|php|md|rst|txt|json|ya?ml|toml|ini|cfg|html|css|scss|sql|sh|bash|zsh|lock)$", re.IGNORECASE)
_BARE_PR_RE = re.compile(r"\b(?:PR|pull request|MR)\s*#?\s*(\d{1,7})\b", re.IGNORECASE)
_COMMIT_RE = re.compile(r"\b(?:commit|sha|rev(?:ision)?)\b[^0-9a-f]{0,12}([0-9a-f]{7,40})\b", re.IGNORECASE)
# `git checkout -b X` / `git switch -c X` (a git command, not any "--head" on a line) and
# `gh pr create --head [owner:]X`; quotes may open the argument and are not captured.
_CHECKOUT_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:checkout\s+-b|switch\s+-c)\s+['\"]?([A-Za-z0-9._/-]+)")
_GH_HEAD_RE = re.compile(r"\bgh\s+pr\b[^\n]*?--head[=\s]+['\"]?(?:[A-Za-z0-9_-]+:)?([A-Za-z0-9._/-]+)")


@dataclass
class Ref:
    kind: str
    key: str
    namespace: str
    message_seq: int | None
    span: str
    method: str            # regex | url | source | legacy
    confidence: float
    linkable: bool


def _host_of(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def repo_from_origin(origin_url, hosts):
    """'https://github.com/o/r.git' | 'git@github.com:o/r.git' | 'ssh://git@github.com:22/o/r.git' |
    'https://user:token@github.com/o/r' -> 'github.com/o/r' or None (credentials/ports dropped)."""
    if not origin_url:
        return None
    url = origin_url.strip()
    if "://" in url:
        from urllib.parse import urlsplit
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        host, path = (parts.hostname or "").lower(), parts.path
    else:
        m = re.match(r"^(?:[A-Za-z0-9_.-]+@)?([A-Za-z0-9.-]+):(.+)$", url)   # scp-like git@host:o/r.git
        if not m:
            return None
        host, path = m.group(1).lower(), "/" + m.group(2)
    if host not in hosts:
        return None
    segs = [x for x in path.split("/") if x]
    if len(segs) < 2:
        return None
    repo = segs[1][:-4] if segs[1].endswith(".git") else segs[1]
    return f"{host}/{segs[0].lower()}/{repo.lower()}"


def branch_key(namespace, branch):
    """Branches are scoped to their repository/project so `dev` in two projects never links.
    The `@` is always present, so a branch called `SAAS-1` can never share a key with the
    ticket `SAAS-1` (related work matches on key)."""
    return f"{namespace or ''}@{branch}"


def gh_key(host, owner, repo, n):
    """One spelling per GitHub PR/issue: host, owner and repository are case-insensitive on
    GitHub and the number has no leading zeros, so `Org/Repo#08` and `org/repo#8` connect."""
    return f"{host.lower()}/{owner.lower()}/{repo.lower()}#{int(n)}"


def branch_label(key):
    return key.split("@", 1)[1] if "@" in key else key


def extract(conv: Conversation, settings=None) -> list[Ref]:
    settings = settings or S.load()
    refs_cfg = settings["references"]
    host_list = [h.lower() for h in (refs_cfg.get("github_hosts") or ["github.com"])]
    hosts = set(host_list)
    defaults = set(refs_cfg.get("default_branches") or ["main", "master", "develop", "HEAD"])
    ticket_re = re.compile(refs_cfg["ticket_regex"])
    block = set(refs_cfg.get("ticket_prefix_blocklist") or [])
    tracker = _host_of(refs_cfg.get("ticket_url") or "")
    own_repo = repo_from_origin(conv.git_origin_url, hosts)
    # owner/repo#n names no host: the conversation's own origin host wins, else the first
    # configured host (config order, not alphabetical)
    short_host = own_repo.split("/")[0] if own_repo else host_list[0]

    found: dict[tuple, Ref] = {}

    def add(kind, key, namespace, seq, span, method, conf, linkable):
        # pr and issue share GitHub's number space and the same key format: one row per key,
        # kind "pr" wins once any mention shows it is a pull request.
        k = ("gh", key) if kind in ("pr", "issue") else (kind, key)
        prev = found.get(k)
        if prev is None:
            found[k] = Ref(kind, key, namespace, seq, span[:200], method, conf, linkable)
            return True
        if kind == "pr" and prev.kind == "issue":
            prev.kind = "pr"
        if linkable and not prev.linkable:
            prev.linkable, prev.method, prev.span, prev.message_seq = True, method, span[:200], seq
        return False

    urls_kept = commits_seen = 0
    for m in conv.messages:
        text = m.text or ""
        in_prose = m.kind == KIND_TEXT and m.role in ("user", "assistant")
        # tool calls are where agents run git/gh: a reference there is the agent's own work;
        # tool *results* (docs, changelogs, listings) mention unrelated things: stored, not linked
        agent_side = in_prose or m.kind == "tool_call"
        # tickets: linkable only from prose (docs/design.md §9)
        for mt in ticket_re.finditer(text):
            key = mt.group(0)
            if key.split("-")[0].upper() in block:
                continue
            add("ticket", key, tracker, m.seq, key, "regex", 1.0 if in_prose else 0.6, in_prose)
        # URLs: GitHub pull/issue links are linkable from any message; other URLs are display-only
        for mu in _URL_RE.finditer(text):
            url = mu.group(0).rstrip(URL_STRIP)
            host = _host_of(url)
            path = urlparse(url).path if host else ""
            gh = _GH_PATH_RE.match(path) if host in hosts else None
            if gh:
                owner, repo, kind, n = gh.groups()
                add("pr" if kind == "pull" else "issue", gh_key(host, owner, repo, n), f"{host}/{owner.lower()}/{repo.lower()}",
                    m.seq, url, "url", 1.0, True)
            elif agent_side and urls_kept < URL_CAP:
                if add("url", url, host, m.seq, url, "regex", 0.5, False):
                    urls_kept += 1          # repeats of one URL do not eat the cap
        # owner/repo#n and bare PR numbers: from prose and tool calls (linkable), from tool
        # results as display-only evidence
        if m.kind in (KIND_TEXT, "tool_call", "tool_result"):
            for ms in _SHORT_REF_RE.finditer(text):
                owner, repo, n = ms.groups()
                if _CODE_EXT_RE.search(repo) or repo.count(".") > 1:
                    continue   # src/file.py#12 is a file:line, not a repository
                add("issue", gh_key(short_host, owner, repo, n), f"{short_host}/{owner.lower()}/{repo.lower()}", m.seq,
                    ms.group(0), "regex", 0.8 if agent_side else 0.4, agent_side)
            for mb in _BARE_PR_RE.finditer(text):
                n = int(mb.group(1))
                if own_repo:
                    add("pr", f"{own_repo}#{n}", own_repo, m.seq, mb.group(0), "regex", 0.7 if agent_side else 0.4, agent_side)
                else:
                    add("pr", f"#{n}", "", m.seq, mb.group(0), "regex", 0.3, False)   # ambiguous: never links
        if in_prose:
            for mc in _COMMIT_RE.finditer(text):
                if commits_seen >= COMMIT_CAP:
                    break
                commits_seen += 1
                add("commit", mc.group(1)[:12], "", m.seq, mc.group(0), "regex", 0.5, False)
        # explicit branch creation: in prose or in a tool call (that is where agents run git)
        if agent_side:
            for rx in (_CHECKOUT_RE, _GH_HEAD_RE):
                for mc in rx.finditer(text):
                    br = mc.group(1).rstrip(".,;:'\"/")
                    if br and br not in defaults:
                        ns = conv.project_root or conv.cwd or ""
                        add("branch", branch_key(ns, br), ns, m.seq, mc.group(0), "regex", 0.9, bool(ns))

    br = (conv.git_branch or "").strip()
    if br and br not in defaults:
        ns = conv.project_root or conv.cwd or ""
        add("branch", branch_key(ns, br), ns, None, br, "source", 1.0, bool(ns))   # no project: a bare name links nothing
    return list(found.values())


def extraction_fingerprint(settings=None):
    """Extractor version + the [references] configuration: when either changes, every stored
    conversation is re-scanned (ensure_current) since the indexer skips unchanged sources."""
    settings = settings or S.load()
    cfg = settings["references"]
    return f"v{EXTRACTOR_VERSION}|" + json.dumps(cfg, sort_keys=True, default=str)


def _conversation_from_db(conn, cid):
    """A Conversation shaped enough for extract(): head fields and the stored messages."""
    from convo_index.model import Message
    r = conn.execute(
        """SELECT source, native_id, title, first_prompt, source_ref, cwd, project_root, git_branch, git_origin_url
           FROM conversations WHERE id=?""", (cid,)).fetchone()
    if not r:
        return None
    msgs = [Message(seq=seq, role=role, kind=kind, text=text) for seq, role, kind, text in conn.execute(
        "SELECT seq, role, kind, text FROM messages WHERE conversation_id=? ORDER BY seq", (cid,))]
    return Conversation(source=r[0], native_id=r[1], title=r[2], first_prompt=r[3], source_ref=r[4], cwd=r[5],
                        project_root=r[6], git_branch=r[7], git_origin_url=r[8], messages=msgs)


def backfill(conn, settings=None, batch=200):
    """Re-extract references for every live conversation from the stored transcript, in
    batches of one transaction each. Returns the number of conversations re-scanned."""
    settings = settings or S.load()
    ids = [r[0] for r in conn.execute(
        """SELECT c.id FROM conversations c LEFT JOIN conversation_state st ON st.conversation_id = c.id
           WHERE COALESCE(st.deleted, 0) = 0 ORDER BY c.start_ts""")]
    done = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for cid in chunk:
                if dbm.is_deleted(conn, cid):
                    continue
                conv = _conversation_from_db(conn, cid)   # read under the lock: what we store is what is there
                if conv is None:
                    continue
                _store_locked(conn, conv, extract(conv, settings))
                done += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return done


def ensure_current(conn, settings=None):
    """Backfill once per extractor version / [references] configuration. Returns the number
    of conversations re-scanned (0 when already current)."""
    settings = settings or S.load()
    fp = extraction_fingerprint(settings)
    if dbm.get_meta(conn, META_EXTRACTOR) == fp:
        return 0
    n = backfill(conn, settings)
    dbm.set_meta(conn, META_EXTRACTOR, fp)
    return n


def store(conn, conv: Conversation, refs: list[Ref]):
    """Replace the conversation's references, honouring the user's linkable overrides.
    Returns the sorted list of linkable ticket keys (kept in conversations.tickets)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        tickets = _store_locked(conn, conv, refs)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return tickets


def _store_locked(conn, conv: Conversation, refs: list[Ref]):
    """store() without the transaction: for callers that already hold one (the conversation
    upsert writes the row, its references and the ticket mirror atomically). Overrides are
    read under the same lock so a rejection committed a moment earlier is never overwritten."""
    overrides = {(k, key): bool(v) for k, key, v in conn.execute(
        "SELECT kind, key, linkable FROM reference_decisions WHERE conversation_id=?", (conv.id,))}
    conn.execute("DELETE FROM references_ WHERE conversation_id=?", (conv.id,))
    for r in refs:
        linkable = overrides.get((r.kind, r.key), r.linkable) and r.kind not in NEVER_LINK and not _ambiguous(r.kind, r.key)
        conn.execute(
            """INSERT INTO references_(conversation_id, kind, key, namespace, message_seq, span, method,
                                       confidence, linkable) VALUES(?,?,?,?,?,?,?,?,?)""",
            (conv.id, r.kind, r.key, r.namespace, r.message_seq, r.span, r.method, r.confidence, 1 if linkable else 0))
    tickets = sorted({r.key for r in refs if r.kind == "ticket" and overrides.get(("ticket", r.key), r.linkable)})
    conn.execute("UPDATE conversations SET tickets=? WHERE id=?", (json.dumps(tickets), conv.id))
    return tickets


def _ambiguous(kind, key):
    """A bare `PR 123` without a known repository has no identity to link on."""
    return kind in ("pr", "issue") and "/" not in key


def set_linkable(conn, cid, kind, key, linkable):
    """User override: mark a reference as (not) a real link; survives re-ingestion. For
    GitHub keys the override applies to the pr and issue kinds alike (same number space)."""
    if linkable and (kind in NEVER_LINK or _ambiguous(kind, key)):
        raise LookupError(f"{kind} references never link" if kind in NEVER_LINK
                          else "a bare PR number has no repository to link on")
    kinds = ("pr", "issue") if kind in ("pr", "issue") else (kind,)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not conn.execute(f"SELECT 1 FROM references_ WHERE conversation_id=? AND key=? AND kind IN ({','.join('?' * len(kinds))})",
                            (cid, key, *kinds)).fetchone():
            conn.execute("ROLLBACK")
            return False
        for k in kinds:
            conn.execute(
                "INSERT INTO reference_decisions(conversation_id, kind, key, linkable, ts) VALUES(?,?,?,?,?) "
                "ON CONFLICT(conversation_id, kind, key) DO UPDATE SET linkable=excluded.linkable, ts=excluded.ts",
                (cid, k, key, 1 if linkable else 0, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        q = ",".join("?" * len(kinds))
        conn.execute(f"UPDATE references_ SET linkable=? WHERE conversation_id=? AND key=? AND kind IN ({q})",
                     (1 if linkable else 0, cid, key, *kinds))
        if kind == "ticket":
            keys = [r[0] for r in conn.execute(
                "SELECT key FROM references_ WHERE conversation_id=? AND kind='ticket' AND linkable=1 ORDER BY key", (cid,))]
            conn.execute("UPDATE conversations SET tickets=? WHERE id=?", (json.dumps(keys), cid))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


def link_url(kind, key, settings=None):
    settings = settings or S.load()
    if kind == "branch":
        return None
    if kind == "ticket":
        return (settings["references"].get("ticket_url") or "").replace("{key}", key) or None
    if kind in ("pr", "issue") and "#" in key and "/" in key:
        repo, n = key.rsplit("#", 1)
        return f"https://{repo}/{'pull' if kind == 'pr' else 'issues'}/{n}"
    if kind == "url":
        return key
    return None


def references_for(conn, cid, settings=None):
    """All references of a conversation, linkable first, with display URLs."""
    rows = conn.execute(
        """SELECT kind, key, namespace, message_seq, span, method, confidence, linkable FROM references_
           WHERE conversation_id=? ORDER BY linkable DESC, kind, key""", (cid,)).fetchall()
    return [{"kind": k, "key": key, "namespace": ns, "message_seq": seq, "span": span, "method": meth,
             "confidence": conf, "linkable": bool(link), "url": link_url(k, key, settings)}
            for k, key, ns, seq, span, meth, conf, link in rows]


_LINKED = "('ticket','pr','issue','branch')"

_RELATED_SQL = f"""
    SELECT DISTINCT c.id, c.title, c.source, c.start_ts, c.cwd, s.is_resolved
    FROM references_ r
    JOIN conversations c ON c.id = r.conversation_id
    LEFT JOIN conversation_summary s ON s.conversation_id = c.id
    LEFT JOIN conversation_state st ON st.conversation_id = c.id
    WHERE r.key = ? AND r.linkable = 1 AND r.kind IN {_LINKED}
      AND COALESCE(st.hidden, 0) = 0 AND COALESCE(st.deleted, 0) = 0
"""


def _rows(cur):
    return [{"id": r[0], "title": r[1], "source": r[2], "start_ts": r[3], "cwd": r[4], "is_resolved": r[5]} for r in cur]


def related_for(conn, cid, settings=None, per_key=20):
    """Other conversations sharing a linkable key with this one (or with its parent, for
    sub-agent sessions), grouped by key, newest first. One SQL query per key, bounded; no
    transitive merging."""
    settings = settings or S.load()
    subject = cid
    seen = {cid}
    for _ in range(10):   # nested sub-agents: the subject is the top of the chain
        parent = conn.execute("SELECT parent_id FROM conversations WHERE id=?", (subject,)).fetchone()
        if not parent or not parent[0] or parent[0] in seen:
            break
        subject = parent[0]; seen.add(subject)
    keys = conn.execute(
        f"""SELECT key, MIN(CASE kind WHEN 'pr' THEN 0 WHEN 'issue' THEN 1 WHEN 'ticket' THEN 2 ELSE 3 END) AS ord,
                   MAX(CASE WHEN kind = 'pr' THEN 'pr' ELSE kind END) AS kind
            FROM references_ WHERE conversation_id IN (?, ?) AND linkable = 1 AND kind IN {_LINKED}
            GROUP BY key ORDER BY ord, key""", (cid, subject)).fetchall()
    groups = []
    for key, _, kind in keys:
        kind = "pr" if any(k == "pr" for (k,) in conn.execute(
            "SELECT DISTINCT kind FROM references_ WHERE key=? AND kind IN ('pr','issue')", (key,))) else kind
        count = conn.execute(
            f"""SELECT COUNT(DISTINCT r.conversation_id) FROM references_ r
                LEFT JOIN conversation_state st ON st.conversation_id = r.conversation_id
                WHERE r.key = ? AND r.linkable = 1 AND r.kind IN {_LINKED} AND r.conversation_id NOT IN (?, ?)
                  AND COALESCE(st.hidden, 0) = 0 AND COALESCE(st.deleted, 0) = 0""", (key, cid, subject)).fetchone()[0]
        if not count:
            continue
        items = _rows(conn.execute(_RELATED_SQL + " AND c.id NOT IN (?, ?) ORDER BY c.start_ts DESC LIMIT ?",
                                   (key, cid, subject, per_key)))
        groups.append({"kind": kind, "key": key, "label": branch_label(key) if kind == "branch" else key,
                       "url": link_url(kind, key, settings), "count": count, "conversations": items})
    groups.sort(key=lambda g: -g["count"])
    return groups


def resolve_key(conn, key):
    """A key as the UI/CLI show it: exact keys pass through; a bare branch name resolves to
    the one `<project>@<branch>` key that carries it. Returns (key, candidates): several
    candidates mean the name is ambiguous across projects and nothing was picked."""
    if conn.execute(f"SELECT 1 FROM references_ WHERE key=? AND linkable=1 AND kind IN {_LINKED} LIMIT 1", (key,)).fetchone():
        return key, [key]
    cands = [r[0] for r in conn.execute(
        "SELECT DISTINCT key FROM references_ WHERE kind='branch' AND linkable=1 AND key LIKE '%@' || ? ESCAPE '\\'",
        (key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"),))]
    return (cands[0] if len(cands) == 1 else key), cands


def timeline(conn, key, settings=None, limit=500):
    """Every conversation linked to a key, oldest first, with resolution status. A bare branch
    name is resolved to its project-scoped key when unambiguous; `candidates` lists the
    matching keys otherwise."""
    settings = settings or S.load()
    key, cands = resolve_key(conn, key)
    kinds = {k for (k,) in conn.execute(
        f"SELECT DISTINCT kind FROM references_ WHERE key=? AND linkable=1 AND kind IN {_LINKED}", (key,))}
    kind = "pr" if "pr" in kinds else (sorted(kinds)[0] if kinds else None)
    items = _rows(conn.execute(_RELATED_SQL + " ORDER BY c.start_ts LIMIT ?", (key, limit)))
    out = {"key": key, "kind": kind, "label": branch_label(key) if kind == "branch" else key,
           "url": link_url(kind, key, settings) if kind else None, "conversations": items}
    if len(cands) > 1:
        out["candidates"] = cands
    return out


def top_keys(conn, kinds=("ticket", "pr", "issue", "branch"), limit=15):
    q = ",".join("?" * len(kinds))
    return [[k, key, n] for k, key, n in conn.execute(
        f"""SELECT kind, key, COUNT(DISTINCT conversation_id) AS n FROM references_
            WHERE linkable=1 AND kind IN ({q}) GROUP BY kind, key ORDER BY n DESC, key LIMIT ?""",
        (*kinds, limit)).fetchall()]
