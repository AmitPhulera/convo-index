"""User-defined tag groups (docs/design.md §7).

Definitions come from config (`[[tag_groups]]` / `[[tags]]`, or the simpler `[category]`
block which is synthesized into an exclusive `workspace` group) and are synced into the
`tag_groups` / `tags` tables with a definitions revision. Per conversation and group the
pipeline is: explicit user decision > rules (cwd substrings, branch prefixes, regexes on
title + first prompt) > one LLM call covering every still-unresolved group. Everything the
pipeline proposed is kept in `tag_evidence`; what the UI shows and filters on is
`tag_assignments`; what the user said is `tag_decisions` and is never overridden.

`conversations.category` is kept as a mirror of the `workspace` group's assignment so the
older analytics and links keep working.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time

from convo_index import db as dbm
from convo_index import settings as S

META_REVISION = "tag_definitions_revision"
META_CATEGORY_MIGRATED = "tags_migrated_from_category"
MIRROR_GROUP = "workspace"
NONE_TAG = ""            # stored value for the "none of these" decision (NULL would defeat the PK)
RULE_TEXT_LIMIT = 10_000 # chars of title+first prompt a rule regex may scan
RULE_KINDS = ("path_contains", "branch_prefix", "regex", "source")
DECISIONS = ("accept", "reject")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- definitions -----

def definitions(settings=None):
    """Normalized definitions: {"groups": [{name, exclusive, description}],
    "tags": [{name, group, description, examples, rules}]}. Explicit `tag_groups` in the
    config win; otherwise `[category]` becomes the exclusive `workspace` group."""
    settings = settings or S.load()
    groups = list(settings.get("tag_groups") or [])
    tags = list(settings.get("tags") or [])
    if not any(g.get("name") == MIRROR_GROUP for g in groups):
        cat = settings["category"]
        groups.insert(0, {"name": MIRROR_GROUP, "exclusive": True,
                          "description": "Whose work this conversation is (formerly `category`)"})
        for label in cat["labels"]:
            if any(t.get("name") == label and t.get("group") == MIRROR_GROUP for t in tags):
                continue
            tags.append({"name": label, "group": MIRROR_GROUP,
                         "description": (cat.get("descriptions") or {}).get(label, label),
                         "rules": {"path_contains": list((cat.get("hints") or {}).get(label) or [])}})
    out_groups = [{"name": g["name"], "exclusive": bool(g.get("exclusive", True)),
                   "description": g.get("description", "")} for g in groups if g.get("name")]
    warnings = []

    def _rules(t):
        # `path_contains = "acme"` (a bare string) must mean one value, not six characters;
        # unknown rule kinds and non-string values are reported and dropped; regexes must compile.
        out = {}
        for k, v in (t.get("rules") or {}).items():
            vals = [v] if isinstance(v, str) else list(v or [])
            if k not in RULE_KINDS:
                warnings.append(f"tag {t.get('group', MIRROR_GROUP)}:{t['name']}: unknown rule kind {k!r} ignored")
                continue
            keep = []
            for x in vals:
                if not isinstance(x, str) or not x:
                    warnings.append(f"tag {t.get('group', MIRROR_GROUP)}:{t['name']}: {k} value {x!r} is not a string; ignored")
                    continue
                if k == "regex":
                    try:
                        re.compile(x)
                    except re.error as e:
                        warnings.append(f"tag {t.get('group', MIRROR_GROUP)}:{t['name']}: invalid regex {x!r} ({e}); ignored")
                        continue
                keep.append(x)
            out[k] = keep
        return out
    out_tags = [{"name": t["name"], "group": t.get("group", MIRROR_GROUP), "description": t.get("description", ""),
                 "examples": list(t.get("examples") or []), "rules": _rules(t)}
                for t in tags if t.get("name")]
    return {"groups": out_groups, "tags": out_tags, "warnings": warnings}


def definitions_revision(defs):
    defs = {"groups": defs["groups"], "tags": defs["tags"]}
    return hashlib.sha1(json.dumps(defs, sort_keys=True).encode(), usedforsecurity=False).hexdigest()[:12]


def sync_definitions(conn, settings=None):
    """Write the configured definitions into the DB (idempotent) and, once, convert the
    legacy `category` column into `workspace` assignments. Returns the revision."""
    settings = settings or S.load()
    defs = definitions(settings)
    rev = definitions_revision(defs)
    for w in defs.get("warnings") or []:
        print(f"tag definitions: {w}", file=sys.stderr)
    prev = dbm.get_meta(conn, META_REVISION)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM tags")
        conn.execute("DELETE FROM tag_groups")
        for g in defs["groups"]:
            conn.execute("INSERT INTO tag_groups(name, exclusive, description, revision) VALUES(?,?,?,?)",
                         (g["name"], 1 if g["exclusive"] else 0, g["description"], rev))
        for t in defs["tags"]:
            conn.execute(
                "INSERT OR REPLACE INTO tags(group_name, name, description, examples, rules, revision, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (t["group"], t["name"], t["description"], json.dumps(t["examples"]), json.dumps(t["rules"]), rev, _now()))
        conn.execute("INSERT INTO index_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (META_REVISION, rev))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    _migrate_category(conn, defs)
    if prev is not None and prev != rev:
        rebuild_rules(conn)   # changed rules/tags must reach conversations that will not be re-ingested
    return rev


def rebuild_rules(conn, groups=None):
    """Re-apply the rules to every live conversation from its stored metadata and re-resolve,
    in one transaction. Used when the definitions changed: a renamed path rule or a removed
    tag otherwise lingers on conversations the indexer sees as unchanged. Returns the count."""
    groups = groups or load_groups(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            """SELECT c.id, c.title, c.first_prompt, c.cwd, c.git_branch, c.source FROM conversations c
               LEFT JOIN conversation_state st ON st.conversation_id = c.id WHERE COALESCE(st.deleted, 0) = 0""").fetchall()
        for cid, title, fp, cwd, branch, source in rows:
            _apply_rules_locked(conn, cid, {"title": title, "first_prompt": fp, "cwd": cwd, "git_branch": branch, "source": source}, groups)
            _resolve_locked(conn, cid, groups)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def _migrate_category(conn, defs):
    """One-time: existing `conversations.category` values that name a workspace tag become
    evidence, and the conversation is resolved through the normal precedence path so the
    assignment and the mirror columns agree. Marker check, snapshot and writes happen in one
    transaction, so a user decision taken meanwhile cannot be overwritten."""
    ws_tags = {t["name"] for t in defs["tags"] if t["group"] == MIRROR_GROUP}
    conn.execute("BEGIN IMMEDIATE")
    try:
        if dbm.get_meta(conn, META_CATEGORY_MIGRATED):
            conn.execute("ROLLBACK")
            return
        groups = load_groups(conn)
        rows = conn.execute("SELECT id, category, category_source FROM conversations").fetchall()
        for cid, cat, src in rows:
            if cat not in ws_tags:
                if src in ("path", "haiku"):          # legacy value naming no tag: mirror becomes honest
                    _resolve_locked(conn, cid, groups)
                continue
            method = "rule" if src == "path" else "llm"
            conn.execute(
                "INSERT INTO tag_evidence(conversation_id, group_name, tag, method, confidence, definition_revision, ts) "
                "VALUES(?,?,?,?,?,?,?)", (cid, MIRROR_GROUP, cat, method, 1.0 if method == "rule" else 0.7, "legacy", _now()))
            _resolve_locked(conn, cid, groups)
            # The legacy classification counts as done tagging work: seed the job as ok with no
            # fingerprint (adopted lazily), so the first run does not re-tag every old conversation.
            conn.execute(
                "INSERT OR IGNORE INTO derived_jobs(conversation_id, kind, input_fingerprint, status, attempts, last_run) "
                "VALUES(?, 'tags', NULL, 'ok', 1, ?)", (cid, _now()))
        conn.execute("INSERT INTO index_meta(key, value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET value='1'",
                     (META_CATEGORY_MIGRATED,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def load_groups(conn):
    """{group: {"exclusive": bool, "description": str, "tags": [{name, description, rules}]}}"""
    groups = {}
    for name, excl, desc in conn.execute("SELECT name, exclusive, description FROM tag_groups ORDER BY rowid"):
        groups[name] = {"exclusive": bool(excl), "description": desc or "", "tags": []}
    for g, name, desc, rules in conn.execute("SELECT group_name, name, description, rules FROM tags ORDER BY rowid"):
        if g in groups:
            groups[g]["tags"].append({"name": name, "description": desc or "", "rules": json.loads(rules or "{}")})
    return groups


# ---------------------------------------------------------------- rules -----------

def rule_matches(rules, conv_row):
    """conv_row: dict with cwd, git_branch, title, first_prompt, source."""
    cwd = (conv_row.get("cwd") or "").lower()
    for h in rules.get("path_contains") or []:
        if h and h.lower() in cwd:
            return f"path_contains:{h}"
    branch = conv_row.get("git_branch") or ""
    for pfx in rules.get("branch_prefix") or []:
        if pfx and branch.startswith(pfx):
            return f"branch_prefix:{pfx}"
    text = f"{conv_row.get('title') or ''}\n{conv_row.get('first_prompt') or ''}"[:RULE_TEXT_LIMIT]
    for rx in rules.get("regex") or []:
        try:
            if rx and re.search(rx, text, re.IGNORECASE):
                return f"regex:{rx}"
        except re.error:
            continue
    srcs = rules.get("source") or []
    if srcs and conv_row.get("source") in srcs:
        return f"source:{conv_row.get('source')}"
    return None


def apply_rules(conn, cid, conv_row, groups=None):
    """Record rule evidence for every group; returns {group: [tag,...]} of rule hits."""
    hits = _apply_rules_locked(conn, cid, conv_row, groups)
    conn.commit()
    return hits


def _apply_rules_locked(conn, cid, conv_row, groups=None):
    groups = groups or load_groups(conn)
    hits = {}
    rev = dbm.get_meta(conn, META_REVISION)
    conn.execute("DELETE FROM tag_evidence WHERE conversation_id=? AND method='rule'", (cid,))
    for g, spec in groups.items():
        for t in spec["tags"]:
            why = rule_matches(t["rules"], conv_row)
            if why:
                conn.execute(
                    "INSERT INTO tag_evidence(conversation_id, group_name, tag, method, confidence, detail, definition_revision, ts) "
                    "VALUES(?,?,?,?,?,?,?,?)", (cid, g, t["name"], "rule", 1.0, why, rev, _now()))
                hits.setdefault(g, []).append(t["name"])
    return hits


# ---------------------------------------------------------------- decisions -------

def decisions_for(conn, cid):
    """{group: {"accept": set(tags), "reject": set(tags), "none": bool}}"""
    out = {}
    for g, tag, decision in conn.execute(
            "SELECT group_name, tag, decision FROM tag_decisions WHERE conversation_id=?", (cid,)):
        d = out.setdefault(g, {"accept": set(), "reject": set(), "none": False})
        if tag == NONE_TAG:
            d["none"] = decision == "accept"
        elif decision in DECISIONS:
            d[decision].add(tag)
    return out


# ---------------------------------------------------------------- resolution ------

def resolve(conn, cid, groups=None):
    """Compute effective assignments for one conversation from decisions > rules > llm
    evidence and write them; mirror the workspace group into `conversations.category`.
    Runs in its own transaction; callers already inside one use `_resolve_locked`."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        out = _resolve_locked(conn, cid, groups)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def _resolve_locked(conn, cid, groups=None):
    groups = groups or load_groups(conn)
    decisions = decisions_for(conn, cid)
    # neighbour rows are suggestions: shown to the user, never turned into assignments
    evidence = conn.execute(
        "SELECT id, group_name, tag, method, confidence FROM tag_evidence WHERE conversation_id=? AND method != 'neighbour' "
        "ORDER BY CASE method WHEN 'rule' THEN 0 WHEN 'llm' THEN 1 ELSE 2 END, confidence DESC, id DESC", (cid,)).fetchall()
    rows = []   # (group, tag, source, confidence, evidence_id)
    for g, spec in groups.items():
        valid = {t["name"] for t in spec["tags"]}
        d = decisions.get(g)
        accepted = sorted(t for t in d["accept"] if t in valid) if d else []   # a removed tag closes nothing
        if accepted or (d and d["none"]):
            for t in accepted:
                rows.append((g, t, "user", 1.0, None))
                if spec["exclusive"]:
                    break
            continue
        rejected = d["reject"] if d else set()
        chosen = []
        for eid, eg, tag, method, conf in evidence:
            if eg != g or tag not in valid or tag in rejected or tag in [c[1] for c in chosen]:
                continue
            chosen.append((g, tag, method, conf, eid))
            if spec["exclusive"]:
                break
        rows.extend(chosen)
    conn.execute("DELETE FROM tag_assignments WHERE conversation_id=?", (cid,))
    for g, tag, source, conf, eid in rows:
        conn.execute(
            "INSERT INTO tag_assignments(conversation_id, group_name, tag, source, confidence, evidence_id, ts) "
            "VALUES(?,?,?,?,?,?,?)", (cid, g, tag, source, conf, eid, _now()))
    ws = next(((tag, source) for g, tag, source, _, _ in rows if g == MIRROR_GROUP), None)
    ws_dec = decisions.get(MIRROR_GROUP)
    if ws:
        cat, cat_src = ws
    elif ws_dec and ws_dec["none"]:
        cat, cat_src = "unknown", "user"       # the user said none of these
    else:
        cat, cat_src = "unknown", None         # nothing decided or matched (yet)
    conn.execute("UPDATE conversations SET category=?, category_source=? WHERE id=?", (cat, cat_src, cid))
    return {g: sorted(tag for gg, tag, *_ in rows if gg == g) for g in groups}


def assignments_for(conn, cids):
    """{cid: {group: [tags]}} for many conversations at once."""
    if not cids:
        return {}
    out = {c: {} for c in cids}
    q = ",".join("?" * len(cids))
    for cid, g, tag in conn.execute(
            f"SELECT conversation_id, group_name, tag FROM tag_assignments WHERE conversation_id IN ({q}) ORDER BY tag",
            list(cids)):
        out.setdefault(cid, {}).setdefault(g, []).append(tag)
    return out


# ---------------------------------------------------------------- LLM -------------


def conv_row(conn, cid):
    r = conn.execute("SELECT title, first_prompt, cwd, git_branch, source FROM conversations WHERE id=?", (cid,)).fetchone()
    if not r:
        return None
    return dict(zip(("title", "first_prompt", "cwd", "git_branch", "source"), r, strict=False))
