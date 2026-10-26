"""Export one conversation as JSON or Markdown (`convo-index export`, MCP get_conversation)."""
from __future__ import annotations

import json
import re

from convo_index import cost as K
from convo_index import db as dbm
from convo_index import references as REF
from convo_index import tags as T
from convo_index.model import KIND_TEXT, KIND_THINKING, KIND_TOOL_CALL, KIND_TOOL_RESULT
from convo_index.sources.base import resume_command

_HEAD = """SELECT id, title, source, native_id, cwd, project_root, git_branch, git_origin_url, start_ts, end_ts,
                  message_count, fidelity, models, warnings, metadata, parent_id, relation, archive_path, source_ref
           FROM conversations WHERE id=?"""


def head(conn, cid):
    r = conn.execute(_HEAD, (cid,)).fetchone()
    if not r:
        return None
    return {"id": r[0], "title": r[1], "source": r[2], "native_id": r[3], "cwd": r[4], "project_root": r[5] or r[4],
            "git_branch": r[6], "git_origin_url": r[7], "start_ts": r[8], "end_ts": r[9], "message_count": r[10],
            "fidelity": r[11] or "full", "models": json.loads(r[12] or "[]"), "warnings": json.loads(r[13] or "[]"),
            "metadata": json.loads(r[14] or "{}"), "parent_id": r[15], "relation": r[16], "archive_path": r[17],
            "source_ref": r[18], "resume_cmd": resume_command(r[0], r[4])}


def messages(conn, cid, kinds=None, limit=None, max_chars=None):
    q = "SELECT seq, role, kind, ts, text FROM messages WHERE conversation_id=?"
    params = [cid]
    if kinds:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += list(kinds)
    q += " ORDER BY seq"
    if limit:
        q += " LIMIT ?"
        params.append(int(limit))
    out = []
    for seq, role, kind, ts, text in conn.execute(q, params):
        t = text or ""
        if max_chars and len(t) > max_chars:
            t = t[:max_chars] + f"… [{len(text) - max_chars} more chars]"
        out.append({"seq": seq, "role": role, "kind": kind, "ts": ts, "text": t})
    return out


def as_dict(conn, cid, settings=None, include_tools=True, max_messages=None, max_chars=None):
    """Everything the index knows about one conversation, or None."""
    h = head(conn, cid)
    if h is None:
        return None
    kinds = None if include_tools else (KIND_TEXT, KIND_THINKING)
    h["summary"] = dbm.get_summary(conn, cid)
    h["tags"] = T.assignments_for(conn, [cid]).get(cid, {}) if dbm._table_exists(conn, "tag_groups") else {}
    h["references"] = REF.references_for(conn, cid, settings) if dbm._table_exists(conn, "references_") else []
    h["usage"] = K.usage_for_conversation(conn, cid, K.load(settings)) if dbm._table_exists(conn, "usage_records") else {}
    h["messages"] = messages(conn, cid, kinds, max_messages, max_chars)
    h["messages_total"] = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0]
    return h


def _one_line(text):
    return " ".join(str(text or "").split())


def _escape_headings(text):
    """A message that starts a line with `#` must not become a section of the export. Lines
    inside a fenced or indented code block are code and stay as they are."""
    out, fence = [], None
    for ln in (text or "").split("\n"):
        stripped = ln.lstrip()
        run = re.match(r"(`{3,}|~{3,})", stripped)
        if fence is None and run:
            fence = run.group(1)                           # the opening fence, in full
        elif fence is not None and run and run.group(1)[0] == fence[0] and len(run.group(1)) >= len(fence) \
                and not stripped[len(run.group(1)):].strip():
            fence = None                                   # a closing fence: same character, at least as long
        elif fence is None and stripped.startswith("#") and not ln.startswith(("    ", "\t")):
            ln = "\\" + ln
        out.append(ln)
    return "\n".join(out)


def _fence(text):
    """A backtick fence longer than any run of backticks inside `text`, so tool output that
    itself contains ``` cannot close the block early."""
    longest = max((len(m) for m in re.findall(r"`+", text or "")), default=0)
    return "`" * max(3, longest + 1)


def as_markdown(conn, cid, settings=None, include_tools=False):
    d = as_dict(conn, cid, settings, include_tools=include_tools)
    if d is None:
        return None
    lines = [f"# {_one_line(d['title']) or d['id']}", "",
             f"- id: `{d['id']}`", f"- source: {d['source']}", f"- when: {d['start_ts'] or '?'} → {d['end_ts'] or '?'}",
             f"- cwd: {d['cwd'] or '?'}" + (f" (branch {d['git_branch']})" if d['git_branch'] else "")]
    if d["models"]:
        lines.append(f"- models: {', '.join(d['models'])}")
    u = d.get("usage") or {}
    if u.get("requests"):
        cost = ""
        if u.get("coverage") is not None:
            prefix = "≥ " if u["coverage"] < 1 else "≈ "
            cost = f", {prefix}{K.fmt_usd(u['estimated_cost'])} API-equivalent estimate"
            if u["coverage"] < 1:
                cost += f" ({round(u['coverage'] * 100)}% priced)"
        lines.append(f"- usage: {K.fmt_tokens(u['tokens'])} tokens over {u['requests']} requests{cost}")
    tags = [f"{g}:{t}" for g, ts in (d["tags"] or {}).items() for t in ts]
    if tags:
        lines.append(f"- tags: {', '.join(tags)}")
    refs = [f"{r['kind']} {r.get('label') or r['key']}" for r in d["references"] if r.get("linkable", True)]
    if refs:
        lines.append(f"- references: {', '.join(refs)}")
    if d["resume_cmd"]:
        lines.append(f"- resume: `{d['resume_cmd']}`")
    s = d.get("summary") or {}
    if s.get("problem") or s.get("solution"):
        lines += ["", "## Summary", "", f"**Problem.** {s.get('problem') or ''}", "", f"**Solution.** {s.get('solution') or ''}"]
        if s.get("learning"):
            lines += ["", f"**Learning.** {s['learning']}"]
    lines += ["", "## Transcript", ""]
    for m in d["messages"]:
        who = {"user": "User", "assistant": "Assistant", "tool": "Tool"}.get(m["role"], m["role"])
        label = {KIND_THINKING: f"{who} (thinking)", KIND_TOOL_CALL: f"{who} → tool", KIND_TOOL_RESULT: "Tool result"}.get(m["kind"], who)
        lines += [f"### {label}" + (f" · {m['ts'][:19]}" if m.get("ts") else ""), ""]
        if m["kind"] in (KIND_TOOL_CALL, KIND_TOOL_RESULT):
            fence = _fence(m["text"])
            lines += [fence, m["text"], fence, ""]
        else:
            lines += [_escape_headings(m["text"]), ""]
    return "\n".join(lines)
