"""Opencode adapter: one SQLite database for every session.

Verified layout (Opencode 1.18.x): `session(id, project_id, workspace_id, parent_id,
title, directory, model JSON, cost, tokens_*, time_created, time_updated, time_archived,
revert)`, `message(id, session_id, time_created, data JSON)` and
`part(id, message_id, session_id, data JSON)`. `message.data.role` is `user` or
`assistant`; assistant rows carry `providerID`, `modelID`, `tokens{input, output,
reasoning, cache{read, write}}` and `cost`. Part `data.type` is one of text, reasoning,
tool (with `callID`, `tool`, `state{status, input, output}`), step-start, step-finish,
patch, file. `step-finish` repeats the message tokens and is ignored so nothing is
double counted. Timestamps are epoch milliseconds.

The database is located via `OPENCODE_DB`, then `opencode db path`, then the default
XDG data path. It is opened read-only and every session is read inside one
transaction so messages and parts come from the same snapshot.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from convo_index.config import CLASSIFY_PROMPT_PREFIX, SUMMARY_PROMPT_PREFIX
from convo_index.model import (Conversation, Message, ToolCall, UsageRecord,
                               KIND_TEXT, KIND_THINKING, KIND_TOOL_CALL, FIDELITY_FULL,
                               FIDELITY_PARTIAL, make_id, split_id)
from convo_index.sources.base import SourceRef, UnreadableSource, shell_quote

SOURCE = "opencode"
TOOL_TEXT_LIMIT = 4000       # chars of tool input+output kept per tool call
KNOWN_PART_TYPES = {"text", "reasoning", "tool", "step-start", "step-finish", "patch",
                    "file", "snapshot", "agent", "subtask", "retry", "compaction"}


def _iso(ms):
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    except (OverflowError, OSError, ValueError, TypeError):
        return None


class _Malformed(dict):
    """Marker for JSON we could not parse: treated as empty but recorded as a warning."""


def _loads(s):
    """Parse an object-valued JSON column; anything that is not an object is malformed."""
    try:
        data = json.loads(s) if s else {}
    except (json.JSONDecodeError, TypeError):
        return _Malformed()
    return data if isinstance(data, dict) else _Malformed()


# Every table/column load_session() reads. Anything else is optional metadata.
REQUIRED_TABLES = {"session": {"id", "project_id", "workspace_id", "parent_id", "slug", "directory",
                               "title", "version", "agent", "model", "revert",
                               "time_created", "time_updated", "time_archived"},
                   "message": {"id", "session_id", "time_created", "data"},
                   "part": {"id", "message_id", "session_id", "data"},
                   "project": {"id", "worktree"},
                   "workspace": {"id", "branch"}}


def check_schema(conn):
    """Raise if the database lacks the tables/columns this adapter was written against."""
    for table, cols in REQUIRED_TABLES.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        missing = cols - have
        if missing:
            raise RuntimeError(f"opencode db: table {table!r} is missing {sorted(missing)}; "
                               f"supported: {OpencodeAdapter.supported_versions}")


def locate_db():
    """Resolve the Opencode database path without opening it."""
    env = os.environ.get("OPENCODE_DB")
    if env:
        return Path(env).expanduser()
    if shutil.which("opencode"):
        try:
            res = subprocess.run(["opencode", "db", "path"], capture_output=True, text=True, timeout=10)
            if res.returncode == 0 and res.stdout.strip():
                return Path(res.stdout.strip().splitlines()[-1])
        except (subprocess.TimeoutExpired, OSError):
            pass
    return default_db()


def default_db():
    """Opencode's database under the XDG data dir (`~/.local/share` when unset)."""
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "opencode" / "opencode.db"


def _connect_ro(path):
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _as_text(value):
    """Tool inputs/outputs may be strings or structured JSON; render both searchably."""
    if value in (None, "", {}, []):
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


ERROR_TEXT_RESERVE = 500   # error/status text keeps its own budget so a long input cannot hide it


def _tool_text(name, state):
    text = f"[{name}]"
    inp = _as_text(state.get("input"))
    if inp:
        text += " " + inp
    out = _as_text(state.get("output"))
    if out:
        text += "\n" + out
    err = _as_text(state.get("error"))
    status = state.get("status")
    tail = ""
    if err or (status and status not in ("completed", "done", "success")):
        tail = f"\nSTATUS: {status or 'error'}"
        if err:
            tail += "\nERROR: " + err[:ERROR_TEXT_RESERVE]
    budget = TOOL_TEXT_LIMIT - len(tail)
    if len(text) > budget:
        text = text[:budget] + "\n…[tool text truncated]"
    return text + tail


def _file_actions(name, state):
    inp = state.get("input") or {}
    if not isinstance(inp, dict):
        return []
    path = inp.get("filePath") or inp.get("path") or inp.get("file")
    if not isinstance(path, str):
        return []
    action = {"read": "read", "edit": "edit", "write": "create", "patch": "edit"}.get(name)
    return [(action, path)] if action else []


def apply_revert(message_rows, part_rows_by_msg, revert):
    """Opencode's `session.revert` = {"messageID": ..., "partID": ...} marks everything from
    that message (and, within it, from that part) onward as reverted: hidden in Opencode's
    UI and dropped when the user continues. Mirror that so reverted turns are not indexed."""
    if not isinstance(revert, dict) or not revert.get("messageID"):
        return message_rows, part_rows_by_msg
    boundary, part_boundary = revert["messageID"], revert.get("partID")
    kept = []
    for row in message_rows:  # ids are time-ordered, but rely on the ordering we already have
        if row["id"] == boundary:
            if part_boundary:
                parts = [p for p in part_rows_by_msg.get(row["id"], []) if p["id"] < part_boundary]
                if parts:
                    part_rows_by_msg[row["id"]] = parts
                    kept.append(row)
            break
        kept.append(row)
    else:  # boundary message not present: nothing to cut
        return message_rows, part_rows_by_msg
    return kept, part_rows_by_msg


def build_messages(message_rows, part_rows_by_msg, warnings):
    """Flatten Opencode messages+parts into normalized messages, preserving order.
    Consecutive text (or reasoning) parts of one message merge into one entry."""
    messages, usage = [], []
    seq = 0

    def emit(role, kind, text, ts, native_id, model=None, tool_call=None):
        nonlocal seq
        if not text:
            return None
        m = Message(seq=seq, role=role, kind=kind, text=text, ts=ts, native_id=native_id,
                    model=model, tool_call=tool_call)
        messages.append(m)
        seq += 1
        return m

    for row in message_rows:
        data = _loads(row["data"])
        if isinstance(data, _Malformed):
            warnings.append(f"malformed message json {row['id']}")
        role = data.get("role") or "assistant"
        mid = row["id"]
        created = _iso((data.get("time") or {}).get("created") or row["time_created"])
        model = data.get("modelID") if role == "assistant" else None
        pending_kind = pending_text = pending_id = pending_ts = None

        def flush(role=role, created=created, model=model):
            nonlocal pending_kind, pending_text, pending_id, pending_ts
            if pending_text:
                emit(role, pending_kind, "\n\n".join(pending_text), pending_ts or created,
                     pending_id, model)
            pending_kind = pending_text = pending_id = pending_ts = None

        first_seq = seq  # first normalized message emitted for this Opencode message, any kind
        for prow in part_rows_by_msg.get(mid, []):
            p = _loads(prow["data"])
            if isinstance(p, _Malformed):
                warnings.append(f"malformed part json {prow['id']}")
                continue
            ptype = p.get("type")
            pts = _iso(((p.get("time") or {}).get("start"))) or created
            if ptype in ("text", "reasoning"):
                kind = KIND_TEXT if ptype == "text" else KIND_THINKING
                if pending_kind != kind:
                    flush()
                    pending_kind, pending_text, pending_id, pending_ts = kind, [], prow["id"], pts
                if p.get("text"):
                    pending_text.append(p["text"])
            elif ptype == "tool":
                flush()
                state = p.get("state") or {}
                name = p.get("tool") or "tool"
                tc = ToolCall(native_id=p.get("callID"), name=name,
                              args=state.get("input") if isinstance(state.get("input"), dict) else None,
                              result_text=_as_text(state.get("output")) or None,
                              file_actions=_file_actions(name, state))
                emit(role, KIND_TOOL_CALL, _tool_text(name, state), pts, prow["id"], model, tc)
            elif ptype in KNOWN_PART_TYPES or ptype is None:
                continue  # step markers, patches, files: no transcript text
            else:
                warnings.append(f"unknown part type {ptype!r}")
        flush()

        if role == "assistant":
            tok = data.get("tokens") or {}
            cache = tok.get("cache") or {}
            if tok or data.get("cost") is not None:
                usage.append(UsageRecord(
                    native_request_id=mid, message_seq=first_seq if seq > first_seq else None,
                    provider=data.get("providerID"), model=data.get("modelID"),
                    input_uncached=tok.get("input"), cache_read=cache.get("read"),
                    cache_write=cache.get("write"), output=tok.get("output"),
                    reasoning=tok.get("reasoning"), reported_cost_usd=data.get("cost"),
                    ts=created))
    return messages, usage


def load_session(conn, session_id, db_path):
    """Read one session inside a single read transaction. Returns (Conversation, revision)
    or None when the session is empty or is the indexer's own LLM call."""
    conn.execute("BEGIN")
    try:
        s = conn.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone()
        if s is None:
            return None
        proj = conn.execute("SELECT worktree FROM project WHERE id=?", (s["project_id"],)).fetchone()
        branch = None
        if s["workspace_id"]:
            w = conn.execute("SELECT branch FROM workspace WHERE id=?", (s["workspace_id"],)).fetchone()
            branch = w["branch"] if w else None
        message_rows = conn.execute(
            "SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created, id",
            (session_id,)).fetchall()
        parts = conn.execute(
            # Opencode itself orders a message's parts by id (message-v2.ts hydrate()/parts());
            # ids are time-sortable, and mirroring the source is what keeps revert boundaries right.
            "SELECT id, message_id, data FROM part WHERE session_id=? ORDER BY id",
            (session_id,)).fetchall()
    finally:
        conn.execute("COMMIT")

    parts_by_msg = {}
    for p in parts:
        parts_by_msg.setdefault(p["message_id"], []).append(p)
    warnings = []
    revert = _loads(s["revert"]) if s["revert"] else None
    message_rows, parts_by_msg = apply_revert(message_rows, parts_by_msg, revert)
    messages, usage = build_messages(message_rows, parts_by_msg, warnings)
    if not messages:
        if message_rows and warnings:
            # Records exist but we could not interpret any of them: not a confirmed empty session.
            raise UnreadableSource(f"session {session_id}: {len(message_rows)} messages, none readable: "
                                   + "; ".join(sorted(set(warnings))[:3]))
        return None
    first_prompt = next((m.text for m in messages if m.role == "user" and m.kind == KIND_TEXT), "")
    if first_prompt.startswith((CLASSIFY_PROMPT_PREFIX, SUMMARY_PROMPT_PREFIX)):
        return None

    model_json = _loads(s["model"]) if s["model"] else {}
    models = []
    for m in messages:
        if m.model and m.model not in models:
            models.append(m.model)
    if model_json.get("id") and model_json["id"] not in models:
        models.append(model_json["id"])
    relation = ("subagent", make_id(SOURCE, s["parent_id"])) if s["parent_id"] else None
    worktree = proj["worktree"] if proj else None
    metadata = {k: v for k, v in {
        "agent": s["agent"], "slug": s["slug"], "opencode_version": s["version"],
        "project_id": s["project_id"], "archived": bool(s["time_archived"]),
        "reverted": bool(s["revert"]),
    }.items() if v not in (None, False, "")}
    conv = Conversation(
        source=SOURCE, native_id=s["id"], title=s["title"] or first_prompt or s["id"],
        first_prompt=first_prompt, messages=messages,
        source_ref=f"sqlite:{db_path}#{s['id']}",
        cwd=s["directory"], project_root=worktree if worktree and worktree != "/" else None,
        git_branch=branch, relation=relation,
        start_ts=_iso(s["time_created"]), end_ts=_iso(s["time_updated"]),
        provider=model_json.get("providerID"), models=models,
        fidelity=FIDELITY_PARTIAL if warnings else FIDELITY_FULL,
        warnings=sorted(set(warnings)), metadata=metadata, usage=usage,
    )
    return conv, str(s["time_updated"])


class OpencodeAdapter:
    name = SOURCE
    supported_versions = "Opencode 1.18.x SQLite (tables session/message/part)"

    def __init__(self, db_path=None):
        self._db_path = Path(db_path) if db_path else None

    @property
    def db_path(self):
        if self._db_path is None:
            self._db_path = locate_db()
        return self._db_path

    def available(self):
        try:
            return self.db_path.exists()
        except OSError:
            return False

    def discover(self):
        conn = _connect_ro(self.db_path)
        try:
            check_schema(conn)
            rows = conn.execute("SELECT id, time_updated FROM session ORDER BY time_created").fetchall()
        finally:
            conn.close()
        for r in rows:
            yield SourceRef(SOURCE, r["id"], str(r["time_updated"]), str(self.db_path))

    def load(self, ref):
        conn = _connect_ro(ref.locator)
        try:
            return load_session(conn, ref.native_id, ref.locator)
        finally:
            conn.close()


def resume_command(conversation_id, cwd):
    _, native = split_id(conversation_id)
    return f"opencode --session {shell_quote(native)}"
