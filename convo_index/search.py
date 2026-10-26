import json
import re

import sqlite_vec

from convo_index import tags as T


def fts_query(q):
    """Turn an arbitrary user string into a safe FTS5 MATCH expression.

    FTS5 treats bare input as query syntax, so hyphens (e.g. SAAS-1234),
    operators (AND/OR/NEAR), and punctuation otherwise raise syntax errors.
    Tokenize on word chars and quote each token; returns "" if nothing usable.
    """
    tokens = re.findall(r"\w+", q or "")
    return " ".join(f'"{t}"' for t in tokens)


def rrf_merge(ranked_lists, k=60):
    """Reciprocal-rank fusion. Each list is [(id, rank), ...] rank starting at 1."""
    scores = {}
    for lst in ranked_lists:
        for sid, rank in lst:
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank)
    return [sid for sid, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


def _keyword_ranked(conn, query, limit, where="", params=()):
    match = fts_query(query)
    if not match:
        return []
    # Group the hits per conversation first: the filter then runs once per conversation
    # instead of once per matching message.
    rows = conn.execute(
        f"""WITH scored AS (SELECT rowid, rank FROM messages_fts WHERE messages_fts MATCH ?),
                grouped AS (SELECT m.conversation_id AS cid, MIN(s.rank) AS best_score
                            FROM scored s JOIN messages m ON m.id=s.rowid GROUP BY m.conversation_id)
           SELECT g.cid, g.best_score FROM grouped g JOIN conversations c ON c.id = g.cid
           {where}
           ORDER BY g.best_score LIMIT ?""",
        (match, *params, limit),
    ).fetchall()
    return [(r[0], i + 1) for i, r in enumerate(rows)]


# sqlite-vec caps k for a KNN query
VEC_K_MAX = 4096


def _semantic_ranked(conn, query_vec, limit, where="", params=()):
    """Nearest conversations among those that pass `where`. A bare k-NN followed by the filter
    is the fast path. When it yields fewer than `limit` conversations (a long conversation can
    fill the window with its own messages, or the filter can reject most neighbours), further
    passes look past the conversations already ranked: restricting the k-NN by `rowid IN` is
    exact but materialises every eligible message id, so it runs only when needed."""
    if query_vec is None:
        return []
    # k is cheap (sqlite-vec scans every vector either way) and transcripts contribute many
    # messages per conversation, so take plenty of neighbours to fill the page on the first pass.
    vec, k = sqlite_vec.serialize_float32(query_vec), min(VEC_K_MAX, limit * 16)

    def nearest(knn_filter, knn_params):
        return conn.execute(
            f"""SELECT m.conversation_id, MIN(v.distance) AS d
               FROM (SELECT rowid, distance FROM vec_messages
                     WHERE embedding MATCH ? {knn_filter} ORDER BY distance LIMIT ?) v
               JOIN messages m ON m.id=v.rowid JOIN conversations c ON c.id = m.conversation_id
               {where}
               GROUP BY m.conversation_id ORDER BY d""",
            (vec, *knn_params, k, *params),
        ).fetchall()

    ranked = nearest("", ())                       # fast path: bare k-NN, then the filter
    seen = {cid for cid, _ in ranked}
    while len(ranked) < limit:
        # exact pass: the filter and the exclusion of what is already ranked go inside the k-NN
        marks = ",".join("?" * len(seen))
        clause = f"{where} AND" if where else "WHERE"
        knn_filter = (f"AND rowid IN (SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                      f"{clause} m.conversation_id NOT IN ({marks}))")
        new = [r for r in nearest(knn_filter, (*params, *seen)) if r[0] not in seen]
        if not new:
            break
        ranked += new
        seen.update(cid for cid, _ in new)
    ranked.sort(key=lambda r: r[1])
    return [(cid, i + 1) for i, (cid, _) in enumerate(ranked[:limit])]


def _snippet(conn, conversation_id, query):
    match = fts_query(query)
    if match:
        row = conn.execute(
            """SELECT snippet(messages_fts, 0, '[', ']', ' … ', 12)
               FROM messages_fts
               WHERE rowid IN (SELECT id FROM messages WHERE conversation_id=?)
                 AND messages_fts MATCH ?
               ORDER BY rank LIMIT 1""",
            (conversation_id, match),
        ).fetchone()
        if row:
            return row[0]
    r = conn.execute(
        "SELECT first_prompt FROM conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    return (r[0] or "")[:200]


def _tag_conditions(wanted, alias="c"):
    """EXISTS clauses for {group: {tags}} (any within a group, all across groups)."""
    clauses, params = [], []
    for g, tags in wanted.items():
        q = ",".join("?" * len(tags))
        clauses.append(f"EXISTS (SELECT 1 FROM tag_assignments ta WHERE ta.conversation_id = {alias}.id "
                       f"AND ta.group_name = ? AND ta.tag IN ({q}))")
        params += [g, *sorted(tags)]
    return clauses, params


def _filter_clause(*, tags=None, category=None, source=None, project=None, has_ticket=False,
                   include_hidden=False, include_children=False, since=None):
    """`WHERE ...` over `conversations c` for every result filter, so ranking windows and LIMITs
    apply to rows that can actually be shown (filtering afterwards starves the page)."""
    clauses, params = [], []
    if not include_children:
        clauses.append("c.parent_id IS NULL")
    if not include_hidden:
        clauses.append("NOT EXISTS (SELECT 1 FROM conversation_state st WHERE st.conversation_id = c.id AND st.hidden = 1)")
    if source:                                             # one name or several
        srcs = [source] if isinstance(source, str) else list(source)
        clauses.append(f"c.source IN ({','.join('?' * len(srcs))})"); params.extend(srcs)
    if project:
        clauses.append("COALESCE(c.project_root, c.cwd) = ?"); params.append(project)
    if has_ticket:
        clauses.append("c.tickets IS NOT NULL AND c.tickets != '[]'")
    if since:
        clauses.append("c.start_ts >= ?"); params.append(since)
    if category:   # legacy alias: the mirrored column or the workspace tag
        (tag_clause,), tag_params = _tag_conditions({T.MIRROR_GROUP: {category}})
        clauses.append(f"(c.category = ? OR {tag_clause})"); params += [category, *tag_params]
    tc, tp = _tag_conditions(parse_tag_filters(tags))
    clauses += tc; params += tp
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def _recent(conn, limit, where="", params=()):
    """Conversation ids ordered most-recent first — used to browse when there's no query."""
    rows = conn.execute(
        f"SELECT c.id FROM conversations c {where} ORDER BY c.start_ts DESC LIMIT ?", (*params, limit),
    ).fetchall()
    return [r[0] for r in rows]


def _hydrate(conn, conversation_id, query):
    r = conn.execute(
        """SELECT c.id, c.title, c.category, c.tickets, c.cwd, c.git_branch, c.start_ts, c.message_count,
                  c.archive_path, c.project_root, c.source, c.parent_id, c.fidelity, c.models,
                  COALESCE(st.hidden, 0)
           FROM conversations c LEFT JOIN conversation_state st ON st.conversation_id = c.id
           WHERE c.id=?""", (conversation_id,)
    ).fetchone()
    if not r:
        return None
    return {
        "id": r[0], "title": r[1], "category": r[2],
        "tickets": json.loads(r[3] or "[]"), "cwd": r[4], "git_branch": r[5],
        "start_ts": r[6], "message_count": r[7], "archive_path": r[8],
        "project_root": r[9] or r[4], "source": r[10], "parent_id": r[11],
        "fidelity": r[12] or "full", "models": json.loads(r[13] or "[]"), "hidden": bool(r[14]),
        "snippet": _snippet(conn, conversation_id, query) if query else "",
    }


def parse_tag_filters(values):
    """['workspace:globex', 'topic:infra', 'topic:review'] -> {'workspace': {'globex'}, 'topic': {...}}.
    Within a group: any of; across groups: all."""
    out = {}
    for v in values or []:
        if ":" in v:
            g, t = v.split(":", 1)
            if g and t:
                out.setdefault(g, set()).add(t)
    return out


def search(conn, query, query_vec, category=None, has_ticket=None, mode="hybrid",
           limit=30, include_hidden=False, project=None, source=None,
           include_children=False, tags=None, since=None):
    """`tags`: list of "group:tag" filters. `category` is kept as an alias for
    "workspace:<category>" so older links keep working. `since`: ISO timestamp lower bound."""
    where, params = _filter_clause(
        tags=tags, category=category, source=source, project=project, has_ticket=has_ticket,
        include_hidden=include_hidden, include_children=include_children, since=since)
    lists = []
    window = limit * 2
    if mode in ("keyword", "hybrid") and query:
        lists.append(_keyword_ranked(conn, query, window, where, params))
    if mode in ("semantic", "hybrid"):
        lists.append(_semantic_ranked(conn, query_vec, window, where, params))
    if query or query_vec is not None:
        ordered = rrf_merge(lists)
    else:
        ordered = _recent(conn, limit, where, params)   # no search input: browse most recent
    ordered = ordered[:limit]
    all_tags = T.assignments_for(conn, ordered) if ordered else {}
    results = []
    for cid in ordered:
        item = _hydrate(conn, cid, query)
        if not item:
            continue
        item["tags"] = all_tags.get(cid, {})
        results.append(item)
    return results
