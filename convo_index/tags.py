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
from convo_index.config import CLASSIFY_PROMPT_PREFIX, HAIKU_CONTEXT_CHARS
from convo_index.providers import registry as R

META_REVISION = "tag_definitions_revision"
META_CATEGORY_MIGRATED = "tags_migrated_from_category"
MIRROR_GROUP = "workspace"
NONE_TAG = ""            # stored value for the "none of these" decision (NULL would defeat the PK)
RULE_TEXT_LIMIT = 10_000 # chars of title+first prompt a rule regex may scan
RULE_KINDS = ("path_contains", "branch_prefix", "regex", "source")
DECISIONS = ("accept", "reject")
PROMPT_VERSION = 2       # bump when build_prompt changes what the model sees (2: few-shot examples)


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


def set_decision(conn, cid, group, tag, decision="accept", *, suggest=True, settings=None):
    """Record a user decision and re-resolve, atomically. `tag=None` with accept means
    "none of these" for the group (stored as the empty-string sentinel). Accepting a tag in
    an exclusive group replaces earlier accepts; accepting any tag removes a "none".
    An accept then proposes the tag to similar conversations (vectors.suggest) unless
    `suggest` is False; `settings` are loaded when not given."""
    if decision not in DECISIONS:
        raise LookupError(f"decision must be one of {DECISIONS}, not {decision!r}")
    stored = NONE_TAG if tag is None else tag
    if tag is None and decision != "accept":
        raise LookupError('"none of these" can only be accepted; clear it to undo')
    conn.execute("BEGIN IMMEDIATE")
    try:
        excl = conn.execute("SELECT exclusive FROM tag_groups WHERE name=?", (group,)).fetchone()
        if excl is None:
            raise LookupError(f"unknown tag group {group!r}")
        if tag is not None and not conn.execute("SELECT 1 FROM tags WHERE group_name=? AND name=?", (group, tag)).fetchone():
            raise LookupError(f"unknown tag {group}:{tag}")
        if decision == "accept":
            if tag is None or excl[0]:
                # "none of these" or an exclusive pick supersedes earlier accepts in this group
                conn.execute("DELETE FROM tag_decisions WHERE conversation_id=? AND group_name=? AND decision='accept'",
                             (cid, group))
            else:
                conn.execute("DELETE FROM tag_decisions WHERE conversation_id=? AND group_name=? AND tag=?",
                             (cid, group, NONE_TAG))   # a real tag cancels "none" in a multi group
        conn.execute(
            "INSERT INTO tag_decisions(conversation_id, group_name, tag, decision, ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(conversation_id, group_name, tag) DO UPDATE SET decision=excluded.decision, ts=excluded.ts",
            (cid, group, stored, decision, _now()))
        out = _resolve_locked(conn, cid)
        # the decision may have reopened a group (a rejection) or closed one: let the next run
        # look again; pending_tags marks the job done when nothing is left for the LLM
        conn.execute("DELETE FROM derived_jobs WHERE conversation_id=? AND kind='tags'", (cid,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if suggest and decision == "accept" and tag is not None:
        try:
            from convo_index import vectors as V
            V.suggest(conn, cid, group, tag, settings, definition_revision=dbm.get_meta(conn, META_REVISION))
        except Exception:
            pass   # suggestions are best-effort (no vectors yet, vec table missing): the decision stands
    return out


def clear_decision(conn, cid, group, tag=None):
    """Drop the user's decision(s) for a group (all of them when `tag` is None) and
    re-resolve, atomically, so the pipeline's own evidence shows again."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if tag is None:
            conn.execute("DELETE FROM tag_decisions WHERE conversation_id=? AND group_name=?", (cid, group))
        else:
            conn.execute("DELETE FROM tag_decisions WHERE conversation_id=? AND group_name=? AND tag=?", (cid, group, tag))
        out = _resolve_locked(conn, cid)
        # the group may be open again: forget the tags job so the dispatcher looks at it anew
        conn.execute("DELETE FROM derived_jobs WHERE conversation_id=? AND kind='tags'", (cid,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
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


def detail_for(conn, cid):
    """Assignments, evidence, decisions and open suggestions for the detail pane."""
    groups = load_groups(conn)
    ev = [{"group": g, "tag": t, "method": m, "confidence": c, "detail": d}
          for g, t, m, c, d in conn.execute(
              "SELECT group_name, tag, method, confidence, detail FROM tag_evidence WHERE conversation_id=? "
              "AND method != 'neighbour' ORDER BY id", (cid,))]
    dec = [{"group": g, "tag": (None if t == NONE_TAG else t), "decision": d} for g, t, d in conn.execute(
        "SELECT group_name, tag, decision FROM tag_decisions WHERE conversation_id=?", (cid,))]
    try:
        from convo_index import vectors as V
        suggestions = V.suggestions_for(conn, cid)
    except Exception:
        suggestions = []
    return {"groups": groups, "assignments": assignments_for(conn, [cid]).get(cid, {}),
            "evidence": ev, "decisions": dec, "suggestions": suggestions}


# ---------------------------------------------------------------- LLM -------------

def unresolved_groups(conn, cid, groups=None):
    """Groups the LLM should still be asked about: no *valid* user decision (an accepted tag
    that no longer exists does not close a group), and, for exclusive groups only, no rule hit
    on a valid, non-rejected tag. A multi group stays open after a rule hit because the rule
    settled one tag, not the question of which other tags also apply."""
    groups = groups or load_groups(conn)
    decisions = decisions_for(conn, cid)
    rule_hits = conn.execute(
        "SELECT group_name, tag FROM tag_evidence WHERE conversation_id=? AND method='rule'", (cid,)).fetchall()
    out = {}
    for g, spec in groups.items():
        if not spec["tags"]:
            continue
        valid = {t["name"] for t in spec["tags"]}
        d = decisions.get(g) or {"accept": set(), "reject": set(), "none": False}
        if d["none"] or (d["accept"] & valid):
            continue
        if spec["exclusive"] and any(rg == g and tag in valid and tag not in d["reject"] for rg, tag in rule_hits):
            continue
        out[g] = spec
    return out


def llm_schema(groups):
    props = {}
    for g, spec in groups.items():
        names = [t["name"] for t in spec["tags"]]
        if spec["exclusive"]:
            # anyOf keeps strict validators (OpenAI structured outputs) happy: no null in enum
            props[g] = {"anyOf": [{"type": "string", "enum": names}, {"type": "null"}]}
        else:
            props[g] = {"type": "array", "items": {"type": "string", "enum": names}}
    return {"type": "object", "properties": props, "required": list(props)}


def _one_line(text):
    return " ".join(str(text or "").split())


def build_prompt(groups, conv_row, summary=None, examples=None):
    """`examples`: {group: [{title, first_prompt, tag, decision}]} — the user's nearest past
    decisions, shown so the model follows their judgement (rejections as negatives)."""
    lines = [f"{CLASSIFY_PROMPT_PREFIX} into the tag groups below. For each group answer with the "
             "best-fitting tag name; for exclusive groups answer exactly one tag or null when none fits; "
             "for multi groups answer a JSON array (possibly empty). Reply with JSON only.", ""]
    for g, spec in groups.items():
        kind = "exclusive" if spec["exclusive"] else "multi"
        lines.append(f"Group \"{g}\" ({kind}){': ' + spec['description'] if spec['description'] else ''}")
        for t in spec["tags"]:
            lines.append(f"  - {t['name']}: {t['description']}" if t["description"] else f"  - {t['name']}")
        for ex in (examples or {}).get(g) or []:
            verdict = (f"NOT {ex['tag']}" if ex["decision"] == "reject" else
                       ("none of these" if ex["tag"] is None else ex["tag"]))
            lines.append(f"  example (user said {verdict}): \"{_one_line(ex['title'])[:80]}\" — {_one_line(ex['first_prompt'])[:160]}")
    lines += ["", f"Title: {conv_row.get('title') or ''}",
              f"Source tool: {conv_row.get('source') or ''}; cwd: {conv_row.get('cwd') or ''}; branch: {conv_row.get('git_branch') or ''}",
              f"First message: {(conv_row.get('first_prompt') or '')[:HAIKU_CONTEXT_CHARS]}"]
    if summary and (summary.get("problem") or summary.get("solution")):
        lines += [f"Summary: {summary.get('problem') or ''} {summary.get('solution') or ''}".strip()]
    return "\n".join(lines)


def llm_tag(conn, cid, conv_row, settings=None, groups=None):
    """One LLM call for every unresolved group. Returns (status, LLMResult|None) where status is
    'ok' | 'nothing' (no unresolved groups) | 'failed'."""
    settings = settings or S.load()
    groups = groups or load_groups(conn)
    todo = unresolved_groups(conn, cid, groups)
    if not todo:
        return "nothing", None
    examples = {}
    try:
        from convo_index import vectors as V
        k = int((settings.get("tags_learning") or {}).get("few_shot_k", 6))
        for g in todo:
            ex = V.few_shot_examples(conn, cid, g, k=k, settings=settings)
            if ex:
                examples[g] = ex
    except Exception:   # no vectors yet, or vec table missing: the prompt simply has no examples
        examples = {}
    before = _state_stamp(conn, cid)
    res = R.complete("classify", build_prompt(todo, conv_row, dbm.get_summary(conn, cid), examples),
                     schema=llm_schema(todo), settings=settings, max_output_tokens=200, timeout=120)
    if not res.ok or not isinstance(res.data, dict):
        return "failed", res
    conn.execute("BEGIN IMMEDIATE")
    try:
        # The call was slow: the conversation may be gone, re-ingested or re-defined meanwhile.
        # A result for an old state is discarded rather than written over the new one.
        if before is None or _state_stamp(conn, cid) != before:
            conn.execute("ROLLBACK")
            return "stale", res
        rev = dbm.get_meta(conn, META_REVISION)
        q = ",".join("?" * len(todo))
        conn.execute(f"DELETE FROM tag_evidence WHERE conversation_id=? AND method='llm' AND group_name IN ({q})",
                     (cid, *todo.keys()))   # only the groups asked about; other groups' evidence stays
        for g, spec in todo.items():
            picked = res.data.get(g)
            picked = [picked] if isinstance(picked, str) else (picked or [])
            valid = {t["name"] for t in spec["tags"]}
            for tag in picked:
                if tag in valid:
                    conn.execute(
                        "INSERT INTO tag_evidence(conversation_id, group_name, tag, method, confidence, detail, definition_revision, ts) "
                        "VALUES(?,?,?,?,?,?,?,?)", (cid, g, tag, "llm", 0.7, res.model_used or "", rev, _now()))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "ok", res


def _state_stamp(conn, cid):
    """(source revision, definitions revision) of a live conversation, or None when it is
    missing or deleted."""
    row = conn.execute("SELECT source_revision FROM conversations WHERE id=?", (cid,)).fetchone()
    if not row or dbm.is_deleted(conn, cid):
        return None
    return (row[0], dbm.get_meta(conn, META_REVISION))


def conv_row(conn, cid):
    r = conn.execute("SELECT title, first_prompt, cwd, git_branch, source FROM conversations WHERE id=?", (cid,)).fetchone()
    if not r:
        return None
    return dict(zip(("title", "first_prompt", "cwd", "git_branch", "source"), r, strict=False))
