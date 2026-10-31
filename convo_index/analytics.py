import json

import duckdb

from convo_index import cost as K

_USAGE_COLS = """COUNT(*), SUM(u.input_uncached), SUM(u.cache_read), SUM(u.cache_write), SUM(u.output),
                 SUM(u.reasoning), SUM(u.reported_cost_usd), SUM(CASE WHEN u.reported_cost_usd IS NOT NULL THEN 1 ELSE 0 END),
                 SUM(u.cache_write_1h),
                 SUM(CASE WHEN COALESCE(u.input_uncached, 0) + COALESCE(u.cache_read, 0) + COALESCE(u.cache_write, 0)
                               + COALESCE(u.cache_write_1h, 0) + COALESCE(u.output, 0) > 0 THEN 1 ELSE 0 END)"""
# hidden and deleted conversations stay out of the usage figures, as they do in search and MCP
_VISIBLE = """LEFT JOIN c.conversation_state st ON st.conversation_id = cv.id
              WHERE COALESCE(st.hidden, 0) = 0 AND COALESCE(st.deleted, 0) = 0"""


def _grouped(con, pricing, dim_sql, extra_join="", where="", limit=None, order="tokens"):
    """[(label, summary)] for one grouping dimension: rows grouped by (dim, provider, model) are
    priced per model and rolled up per dim."""
    rows = con.execute(
        f"""SELECT {dim_sql} AS dim, u.provider, u.model, {_USAGE_COLS}, {pricing.band_sql()} AS band
            FROM c.usage_records u JOIN c.conversations cv ON cv.id = u.conversation_id {extra_join}
            {_VISIBLE} {('AND ' + where[len('WHERE '):]) if where.upper().startswith('WHERE ') else where}
            GROUP BY dim, u.provider, u.model, band""").fetchall()
    by_dim = {}
    for dim, *rest in rows:
        by_dim.setdefault(dim, []).append(tuple(rest))
    out = [(dim, K.aggregate(g, pricing)) for dim, g in by_dim.items() if dim is not None]
    key = (lambda x: (x[1]["estimated_cost"], x[1]["tokens"])) if order == "cost" else (lambda x: x[1]["tokens"])
    out.sort(key=key, reverse=(order != "label"))
    if order == "label":
        out.sort(key=lambda x: str(x[0]))
    return [[dim, _slim(a)] for dim, a in (out[:limit] if limit else out)]


def _slim(a):
    return {"tokens": a["tokens"], "input_uncached": a["input_uncached"], "cache_read": a["cache_read"],
            "cache_write": a["cache_write"] + a.get("cache_write_1h", 0), "cache_write_1h": a.get("cache_write_1h", 0),
            "output": a["output"], "requests": a["requests"],
            "estimated_cost": a["estimated_cost"], "reported_cost": a["reported_cost"], "coverage": a["coverage"]}


def usage(con, pricing):
    """Tokens and API-equivalent cost: totals, by month / source / model / project / reference
    key, top conversations, coverage per source (docs/design.md §10)."""
    total_rows = con.execute(
        f"""SELECT u.provider, u.model, {_USAGE_COLS}, {pricing.band_sql()} AS band FROM c.usage_records u
            JOIN c.conversations cv ON cv.id = u.conversation_id {_VISIBLE} GROUP BY u.provider, u.model, band""").fetchall()
    totals = K.aggregate(total_rows, pricing)
    by_model = _grouped(con, pricing, "COALESCE(u.provider, '?') || ' / ' || COALESCE(u.model, '?')", order="cost")
    by_source = _grouped(con, pricing, "cv.source", order="cost")
    by_month = _grouped(con, pricing, "substr(cv.start_ts, 1, 7)",          # ISO text; no strict CAST to fail on
                        where="WHERE cv.start_ts IS NOT NULL AND length(cv.start_ts) >= 7", order="label")
    by_project = _grouped(con, pricing, "COALESCE(cv.project_root, cv.cwd)", order="cost", limit=10)
    by_reference = []
    try:
        # one row per (conversation, kind, key): several mentions of one key must not multiply usage
        by_reference = _grouped(con, pricing, "r.kind || ' ' || r.key",
                                extra_join="JOIN (SELECT DISTINCT conversation_id, kind, key FROM c.references_ "
                                           "WHERE linkable = 1 AND kind IN ('ticket', 'pr', 'issue', 'branch')) r "
                                           "ON r.conversation_id = cv.id",
                                order="cost", limit=15)
        from convo_index.references import branch_label
        by_reference = [[("branch " + branch_label(lbl[7:])) if lbl.startswith("branch ") else lbl, a]
                        for lbl, a in by_reference]
    except duckdb.Error:   # pre-v5 database
        pass
    top = _grouped(con, pricing, "cv.id", order="tokens", limit=20)
    titles = {}
    if top:
        ids = [cid for cid, _ in top]
        titles = dict(con.execute(
            f"SELECT id, title FROM c.conversations WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall())
    top_conversations = [{"id": cid, "title": titles.get(cid), **a} for cid, a in top]
    return {"revision": pricing.revision, "pricing_errors": pricing.errors,
            "totals": _slim(totals) | {"unpriced_models": totals["unpriced_models"]},
            "by_month": by_month, "by_source": by_source, "by_model": by_model, "by_project": by_project,
            "by_reference": by_reference, "top_conversations": top_conversations,
            "note": "API-equivalent estimate from list prices; a conversation linked to two keys counts in both."}


def analytics(db_path, settings=None):
    con = duckdb.connect()
    con.execute("INSTALL sqlite; LOAD sqlite;")
    con.execute(f"ATTACH '{str(db_path).replace(chr(39), chr(39) * 2)}' AS c (TYPE sqlite, READ_ONLY);")

    # Hidden and deleted conversations are out of every number here, as they are in search.
    con.execute("""CREATE TEMP VIEW visible AS SELECT cv.* FROM c.conversations cv
                   LEFT JOIN c.conversation_state st ON st.conversation_id = cv.id
                   WHERE COALESCE(st.hidden, 0) = 0 AND COALESCE(st.deleted, 0) = 0""")
    by_category = con.execute(
        "SELECT category, COUNT(*) FROM visible GROUP BY category ORDER BY 2 DESC"
    ).fetchall()

    by_tag_group = {}
    try:
        for g, t, n in con.execute(
                "SELECT group_name, tag, COUNT(*) FROM c.tag_assignments "
                "WHERE conversation_id IN (SELECT id FROM visible) GROUP BY group_name, tag ORDER BY 1, 3 DESC"
        ).fetchall():
            by_tag_group.setdefault(g, []).append([t, n])
    except Exception:  # pre-v4 database
        pass

    by_source = con.execute(
        "SELECT source, COUNT(*) FROM visible GROUP BY source ORDER BY 2 DESC"
    ).fetchall()

    by_month = con.execute(
        """SELECT strftime(CAST(start_ts AS TIMESTAMP), '%Y-%m') AS ym, COUNT(*)
           FROM visible WHERE start_ts IS NOT NULL
           GROUP BY ym ORDER BY ym"""
    ).fetchall()

    # Group by repo root (worktrees/subfolders collapse together); fall back to cwd.
    top_projects = con.execute(
        """SELECT COALESCE(project_root, cwd) AS proj, COUNT(*) AS n
           FROM visible
           WHERE COALESCE(project_root, cwd) IS NOT NULL
           GROUP BY proj ORDER BY n DESC LIMIT 10"""
    ).fetchall()

    # tickets is a JSON array string; unnest in Python (small data).
    rows = con.execute("SELECT tickets FROM visible").fetchall()
    counts = {}
    for (tj,) in rows:
        for t in json.loads(tj or "[]"):
            counts[t] = counts.get(t, 0) + 1
    top_tickets = sorted(([t, n] for t, n in counts.items()),
                         key=lambda x: x[1], reverse=True)[:15]

    top_references = []
    try:
        top_references = [list(r) for r in con.execute(
            """SELECT kind, key, COUNT(DISTINCT conversation_id) AS n FROM c.references_
               WHERE linkable = 1 AND kind IN ('pr', 'issue', 'branch')
                 AND conversation_id IN (SELECT id FROM visible) GROUP BY kind, key
               ORDER BY n DESC, key LIMIT 15""").fetchall()]
    except duckdb.Error:  # pre-v5 database without references_
        pass

    usage_report = {}
    try:
        usage_report = usage(con, K.load(settings))
    except duckdb.Error:   # pre-v2 database without usage_records
        pass

    con.close()
    return {
        "usage": usage_report,
        "by_category": [list(r) for r in by_category],
        "by_tag_group": by_tag_group,
        "by_source": [list(r) for r in by_source],
        "by_month": [list(r) for r in by_month],
        "top_tickets": top_tickets,
        "top_references": top_references,
        "top_projects": [list(r) for r in top_projects],
    }
