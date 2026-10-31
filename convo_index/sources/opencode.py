"""Opencode adapter: one SQLite database for every session, in one of two layouts.

Opencode 1.x (verified on 1.18): `session(id, project_id, workspace_id, parent_id, title,
directory, model JSON, cost, tokens_*, time_created, time_updated, time_archived, revert)`,
`message(id, session_id, time_created, data JSON)` and `part(id, message_id, session_id,
data JSON)`. `message.data.role` is `user` or `assistant`; assistant rows carry
`providerID`, `modelID`, `tokens{input, output, reasoning, cache{read, write}}` and
`cost`. Part `data.type` is one of text, reasoning, tool (with `callID`, `tool`,
`state{status, input, output}`), step-start, step-finish, patch, file. `step-finish`
repeats the message tokens and is ignored so nothing is double counted.

Opencode 2.x (verified on 2.1): `session_v2` keeps the same session columns (the
`workspace` row no longer records a branch) and `session_message(id, session_id, type,
seq, data JSON)` replaces both `message` and `part`. `type` is `user` (data: `text`,
`files`, `agents`), `assistant` (data: `model{id, providerID}`, `tokens`, `cost`,
`content[]` of text / reasoning / tool items, where a tool item has `id`, `name`,
`state{status, input, content[{text}], error{message}}`), or bookkeeping without
transcript text: `idle`, `synthetic` (text Opencode injects for the model, e.g. system
reminders) and `compaction`. Both layouts are normalized into the same message/part shape
before anything else looks at them. Timestamps are epoch milliseconds.

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
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from convo_index.config import CLASSIFY_PROMPT_PREFIX, SUMMARY_PROMPT_PREFIX
from convo_index.model import (Conversation, Message, ToolCall, UsageRecord,
                               KIND_TEXT, KIND_THINKING, KIND_TOOL_CALL, FIDELITY_FULL,
                               FIDELITY_PARTIAL, make_id, split_id)
from convo_index.sources import base as sources
from convo_index.sources.base import SourceRef, TurnCommand, UnreadableSource, shell_quote

SOURCE = "opencode"
TOOL_TEXT_LIMIT = 4000       # chars of tool input+output kept per tool call
KNOWN_PART_TYPES = {"text", "reasoning", "tool", "step-start", "step-finish", "patch",
                    "file", "snapshot", "agent", "subtask", "retry", "compaction"}
KNOWN_ROW_TYPES = {"user", "assistant", "idle", "synthetic", "compaction"}   # 2.x session_message.type


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


# Every table/column load_session() reads per layout. Anything else is optional metadata.
SESSION_COLUMNS = {"id", "project_id", "workspace_id", "parent_id", "slug", "directory", "title", "version",
                   "agent", "model", "revert", "time_created", "time_updated", "time_archived"}
SCHEMAS = {   # newest first: a migrated database may still carry the old tables, stale
    "2.x": {"session_v2": SESSION_COLUMNS,
            "session_message": {"id", "session_id", "type", "seq", "time_created", "data"},
            "project": {"id", "worktree"}},
    "1.x": {"session": SESSION_COLUMNS,
            "message": {"id", "session_id", "time_created", "data"},
            "part": {"id", "message_id", "session_id", "data"},
            "project": {"id", "worktree"},
            "workspace": {"id", "branch"}},
}
SESSION_TABLE = {"1.x": "session", "2.x": "session_v2"}


def _schema_gap(conn, tables):
    """First table that is absent or lacks a column this adapter reads, or None."""
    for table, cols in tables.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not have:
            return f"table {table!r} is absent"
        missing = cols - have
        if missing:
            return f"table {table!r} is missing {sorted(missing)}"
    return None


def detect_schema(conn):
    """Name the layout this database uses ("1.x" or "2.x"); raise when it matches neither."""
    gaps = []
    for name, tables in SCHEMAS.items():
        gap = _schema_gap(conn, tables)
        if gap is None:
            return name
        gaps.append(f"{name}: {gap}")
    raise RuntimeError(f"opencode db: {'; '.join(gaps)}; supported: {OpencodeAdapter.supported_versions}")


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


def _dict(value):
    """A JSON object field, or {} when the row holds something else there."""
    return value if isinstance(value, dict) else {}


@dataclass
class Turn:
    """One Opencode message in the 1.x shape, whichever layout it was read from: `data` is
    the parsed message JSON (role, time, modelID, providerID, tokens, cost) and `parts` the
    parsed part JSONs in Opencode's own order, each with the id Opencode gave it. 2.x text
    items have no stored id and get a positional one, `<message id>/<index>`."""
    id: str
    created_ms: int | None
    data: dict
    parts: list[tuple[str, dict]]
    warning: str | None = None     # about the row itself (e.g. an unknown type); reported if the turn survives revert


def _positional(pid, mid):
    return pid.startswith(f"{mid}/")


def apply_revert(turns, revert):
    """Opencode's `session.revert` = {"messageID": ..., "partID": ...} marks everything from
    that message (and, within it, from that part) onward as reverted: hidden in Opencode's
    UI and dropped when the user continues. Mirror that so reverted turns are not indexed."""
    if not isinstance(revert, dict) or not revert.get("messageID"):
        return turns
    boundary, part_boundary = revert["messageID"], revert.get("partID")
    kept = []
    for turn in turns:
        if turn.id == boundary:
            if part_boundary:
                ids = [pid for pid, _ in turn.parts]
                if part_boundary in ids:
                    cut = ids.index(part_boundary)   # parts are in Opencode's order: the position is the cut
                else:
                    # the boundary part itself is gone: Opencode's ids are time-sortable, so the
                    # first id past it is the cut; positional ids say nothing and ride along
                    cut = next((i for i, pid in enumerate(ids)
                                if not _positional(pid, turn.id) and pid > part_boundary), None)
                parts = turn.parts if cut is None else turn.parts[:cut]
                if parts:
                    kept.append(Turn(turn.id, turn.created_ms, turn.data, parts, turn.warning))
            break
        kept.append(turn)
    else:  # boundary message not present: nothing to cut
        return turns
    return kept


def build_messages(turns, warnings):
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

    for turn in turns:
        data = turn.data
        if turn.warning:
            warnings.append(turn.warning)
        if isinstance(data, _Malformed):
            warnings.append(f"malformed message json {turn.id}")
        role = data.get("role") or "assistant"
        mid = turn.id
        created = _iso(_dict(data.get("time")).get("created") or turn.created_ms)
        model = data.get("modelID") if role == "assistant" else None
        pending_kind = pending_text = pending_id = pending_ts = None

        def flush(role=role, created=created, model=model):
            nonlocal pending_kind, pending_text, pending_id, pending_ts
            if pending_text:
                emit(role, pending_kind, "\n\n".join(pending_text), pending_ts or created,
                     pending_id, model)
            pending_kind = pending_text = pending_id = pending_ts = None

        first_seq = seq  # first normalized message emitted for this Opencode message, any kind
        for pid, p in turn.parts:
            if isinstance(p, _Malformed):
                warnings.append(f"malformed part json {pid}")
                continue
            ptype = p.get("type")
            pts = _iso(_dict(p.get("time")).get("start")) or created
            if ptype in ("text", "reasoning"):
                kind = KIND_TEXT if ptype == "text" else KIND_THINKING
                if pending_kind != kind:
                    flush()
                    pending_kind, pending_text, pending_id, pending_ts = kind, [], pid, pts
                text = p.get("text")
                if isinstance(text, str) and text:
                    pending_text.append(text)
                elif text not in (None, ""):
                    warnings.append(f"malformed part json {pid}")
            elif ptype == "tool":
                flush()
                state = _dict(p.get("state"))
                name = p.get("tool") or "tool"
                tc = ToolCall(native_id=p.get("callID"), name=name,
                              args=state.get("input") if isinstance(state.get("input"), dict) else None,
                              result_text=_as_text(state.get("output")) or None,
                              file_actions=_file_actions(name, state))
                emit(role, KIND_TOOL_CALL, _tool_text(name, state), pts, pid, model, tc)
            elif ptype in KNOWN_PART_TYPES or ptype is None:
                continue  # step markers, patches, files: no transcript text
            else:
                warnings.append(f"unknown part type {ptype!r}")
        flush()

        if role == "assistant":
            tok = _dict(data.get("tokens"))
            cache = _dict(tok.get("cache"))
            if tok or data.get("cost") is not None:
                usage.append(UsageRecord(
                    native_request_id=mid, message_seq=first_seq if seq > first_seq else None,
                    provider=data.get("providerID"), model=data.get("modelID"),
                    input_uncached=tok.get("input"), cache_read=cache.get("read"),
                    cache_write=cache.get("write"), output=tok.get("output"),
                    reasoning=tok.get("reasoning"), reported_cost_usd=data.get("cost"),
                    ts=created))
    return messages, usage


def _read_v1(conn, session_id):
    """1.x: message rows plus their part rows, parts in id order like Opencode's own hydrate()."""
    rows = conn.execute(
        "SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created, id",
        (session_id,)).fetchall()
    parts = conn.execute(
        "SELECT id, message_id, data FROM part WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    by_msg = {}
    for p in parts:
        by_msg.setdefault(p["message_id"], []).append((p["id"], _loads(p["data"])))
    return [Turn(r["id"], r["time_created"], _loads(r["data"]), by_msg.get(r["id"], [])) for r in rows]


def _v2_part(item, index, mid):
    """One 2.x assistant content item as a 1.x part: (id, part JSON). Text and reasoning
    items carry no id in the stored row, so they get a positional one. An item without a
    type, or whose required fields hold the wrong kind of value, is malformed."""
    kind = item.get("type")
    pid = item.get("id") if isinstance(item.get("id"), str) else f"{mid}/{index}"
    created = _dict(item.get("time")).get("created")
    if kind in ("text", "reasoning"):
        if not isinstance(item.get("text"), str):
            return f"{mid}/{index}", _Malformed()
        return f"{mid}/{index}", {"type": kind, "text": item["text"], "time": {"start": created}}
    if kind == "tool":
        state = _dict(item.get("state"))
        content = state.get("content") if isinstance(state.get("content"), list) else []
        output = "\n".join(c["text"] for c in content
                           if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"])
        error = state.get("error")
        if isinstance(error, dict):
            error = error.get("message") or _as_text(error)
        return pid, {
            "type": "tool", "tool": item.get("name"), "callID": item.get("id"), "time": {"start": created},
            "state": {"status": state.get("status"), "input": state.get("input"),
                      "output": output or None, "error": error}}
    if not isinstance(kind, str):
        return pid, _Malformed()
    return pid, item   # known markers pass, unknown types warn


def _v2_turn(row):
    """One 2.x session_message row as a Turn, or None for bookkeeping rows without
    transcript text. Rows of a type this adapter does not know are kept as a Turn with a
    warning, so a session made only of such rows is unreadable rather than confirmed empty."""
    kind, mid, created = row["type"], row["id"], row["time_created"]
    if kind in KNOWN_ROW_TYPES - {"user", "assistant"}:
        return None
    if kind not in KNOWN_ROW_TYPES:
        return Turn(mid, created, {"role": kind}, [], warning=f"unknown message type {kind!r}")
    d = _loads(row["data"])
    if isinstance(d, _Malformed):
        return Turn(mid, created, d, [])
    if kind == "user":
        if not isinstance(d.get("text"), str):
            return Turn(mid, created, _Malformed(), [])
        return Turn(mid, created, {"role": "user", "time": d.get("time")}, [(mid, {"type": "text", "text": d["text"]})])
    content = d.get("content")
    if not isinstance(content, list):
        return Turn(mid, created, _Malformed(), [])
    model = _dict(d.get("model"))
    head = {"role": "assistant", "time": d.get("time"), "modelID": model.get("id"),
            "providerID": model.get("providerID"), "tokens": d.get("tokens"),
            "cost": d.get("cost") if isinstance(d.get("cost"), (int, float)) else None}
    parts = [_v2_part(item, i, mid) if isinstance(item, dict) else (f"{mid}/{i}", _Malformed())
             for i, item in enumerate(content)]
    return Turn(mid, created, head, parts)


def _read_v2(conn, session_id):
    """2.x: one session_message row per turn, in Opencode's `seq` order."""
    rows = conn.execute(
        "SELECT id, type, time_created, data FROM session_message WHERE session_id=? ORDER BY seq, id",
        (session_id,)).fetchall()
    turns = (_v2_turn(r) for r in rows)
    return [t for t in turns if t is not None]


def load_session(conn, session_id, db_path):
    """Read one session inside a single read transaction. Returns (Conversation, revision)
    or None when the session is empty or is the indexer's own LLM call."""
    conn.execute("BEGIN")
    try:
        schema = detect_schema(conn)
        s = conn.execute(f"SELECT * FROM {SESSION_TABLE[schema]} WHERE id=?", (session_id,)).fetchone()
        if s is None:
            return None
        proj = conn.execute("SELECT worktree FROM project WHERE id=?", (s["project_id"],)).fetchone()
        branch = None
        if schema == "1.x" and s["workspace_id"]:
            w = conn.execute("SELECT branch FROM workspace WHERE id=?", (s["workspace_id"],)).fetchone()
            branch = w["branch"] if w else None
        turns = _read_v1(conn, session_id) if schema == "1.x" else _read_v2(conn, session_id)
    finally:
        conn.execute("COMMIT")

    revert = _loads(s["revert"]) if s["revert"] else None
    turns = apply_revert(turns, revert)   # reverted rows are gone for good: they say nothing about readability
    warnings = []
    messages, usage = build_messages(turns, warnings)
    if not messages:
        if turns and warnings:
            # Records exist but we could not interpret any of them: not a confirmed empty session.
            raise UnreadableSource(f"session {session_id}: {len(turns)} messages, none readable: "
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
    supported_versions = "Opencode 1.x SQLite (session/message/part) and 2.x (session_v2/session_message)"

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
            table = SESSION_TABLE[detect_schema(conn)]
            rows = conn.execute(f"SELECT id, time_updated FROM {table} ORDER BY time_created").fetchall()
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


# --- Browser turns (sources.base turn protocol) ----------------------------------------------
#
# `opencode run --session <id>` appends one more turn to the stored session, so an interactive
# `opencode --session <id>` afterwards sees it. Verified on 2.1.4: with the configuration below
# the model reports no bash and no MCP tools in the read-only position, writes work in the
# edits position, and a project `opencode.json` declaring an MCP server is not loaded.
#
# `--standalone` runs a private server for this process; the background service would keep
# whatever configuration it started with and ignore the environment set here. Opencode has no
# switch for its global configuration (`~/.config/opencode`: plugins, MCP servers, agents),
# so the turn gets an empty XDG_CONFIG_HOME of its own, made fresh for the turn and removed
# after it: a reused one could be given a plugin by an edits turn (it may sit inside the
# project, as convo-index's data directory does in a checkout) that the next turn would run.
# Credentials and the session database live under the data directory, which stays where it
# is (verified: the turn still answers).
# The variables through which a shell could point opencode at other configuration are
# dropped from the turn's environment. The system directory (/etc/opencode, /Library/
# Application Support/opencode, %ProgramData%\opencode) is administrator-managed and stays in
# force, as Claude Code's managed settings do.
#
# The prompt is an argv element (run mode takes no stdin prompt), which is fine without a
# shell on POSIX; on Windows an npm `.cmd` shim re-parses arguments through cmd.exe, so turns
# are not offered there until a stdin path exists. A prompt starting with "-" would be read
# as an option (`--` is no help: opencode 2.1.4 then repeats the message), so it gets a
# leading space, which only the model sees.

TURNS_ON_WINDOWS = False
RUN_FLAGS = ["run", "--standalone", "--format", "json"]
CONFIG_OVERRIDES = ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CLI_CONFIG_CONTENT")   # 2.1.4's extra config sources
_ALLOW_READ = ["read", "glob", "grep", "list"]


def _permission_rules(edits, read_dirs=()):
    """Ordered rules, last match wins: deny everything (MCP and plugin tools included, and
    `external_directory`, which confines every tool to the project: a write to /tmp or a read
    of /etc/hosts is refused with "Permission denied: external_directory"), then allow the
    read tools and, in Edit mode, the edit tool (which covers write and patch; its paths are
    project-relative, so `*` is the whole project and nothing more) and bash, which runs as the
    user wherever its commands reach."""
    rules = [{"action": "*", "resource": "*", "effect": "deny"}]
    for action in _ALLOW_READ + (["edit", "bash"] if edits else []):
        rules.append({"action": action, "resource": "*", "effect": "allow"})
    for d in read_dirs:     # a handoff's transcript copy: outside the project, and nothing else is
        rules.append({"action": "external_directory", "resource": f"{d}/*", "effect": "allow"})
    return rules


def launcher():
    """`opencode` on PATH, or None; never through an npm shim on Windows (see TURNS_ON_WINDOWS)."""
    if sources.is_windows():
        return None
    binary = shutil.which("opencode")
    return [binary] if binary else None


def turn_config_home():
    """A new, empty, owner-only configuration home for one turn (the runner removes it)."""
    return tempfile.mkdtemp(prefix="convo-index-opencode-")


def session_model(native_id, locator):
    """"provider/model" of the session's last request, or None. A turn names it: with the turn's
    empty configuration home opencode would otherwise fall back to a default model, which a
    workspace's privacy settings may refuse ("This Go model trains on request data", 2.2.4)."""
    if not locator or not locator.startswith("sqlite:") or "#" not in locator:
        return None
    db_path, _, session_id = locator[len("sqlite:"):].rpartition("#")
    if session_id != native_id:
        return None
    try:
        conn = _connect_ro(db_path)
    except sqlite3.Error:
        return None
    try:
        loaded = load_session(conn, session_id, db_path)
    except (sqlite3.Error, RuntimeError, UnreadableSource):
        return None
    finally:
        conn.close()
    usage = [u for u in (loaded[0].usage if loaded else []) if u.provider and u.model]
    return f"{usage[-1].provider}/{usage[-1].model}" if usage else None


def turn_command(native_id, prompt, cwd, *, edits=False, mcp_tools=(), locator=None):
    """`mcp_tools` is ignored: a turn's configuration home is empty, so no MCP server loads.
    The session is resumed by id (`locator` points into the shared database), with its own
    model when the database names one."""
    model = session_model(native_id, locator)
    return _command(["--session", native_id] + (["-m", model] if model else []), prompt, cwd, edits=edits)


# A handoff starts a new session: `run` without `--session`, and every event carries the
# `sessionID` it was given. Verified on 2.2.4: the transcript copy outside the project is
# refused ("Permission denied: external_directory") until a rule allows that directory. With
# the turn's empty configuration home opencode falls back to a default model that a workspace's
# privacy settings may refuse, so a new session always names one, as a resumed turn names its
# session's (session_model): the caller's, else the first of opencode's own recent models.

def recent_model():
    """"provider/model" the user last picked in opencode, or None."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "opencode" / "model.json"
    try:
        first = (json.loads(state.read_text(encoding="utf-8")).get("recent") or [None])[0]
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(first, dict) and first.get("providerID") and first.get("modelID"):
        return f"{first['providerID']}/{first['modelID']}"
    return None


# A handoff prompt is a whole conversation: as `run`'s message argument it would exceed Linux's
# 128 KiB per-argument limit and sit in the process list. It goes in an owner-only file attached
# with `-f` (opencode reads it itself; verified on 2.2.4 from a directory no rule opens), and
# the argument is one line.
ATTACHED_PROMPT = "Follow the instructions in the attached file."


def new_session_command(prompt, cwd, *, edits=False, mcp_tools=(), read_dirs=(), model=None):
    """(TurnCommand, None) for a new session seeded with `prompt` (the id comes from the stream,
    see session_id), or (None, None). Raises ValueError when no model can be named."""
    if launcher() is None:
        return None, None
    model = model or recent_model()
    if not model:
        raise ValueError("no model for a new opencode session: pass --model provider/model "
                         "(opencode's recent-models list is empty)")
    scratch = tempfile.mkdtemp(prefix="convo-index-handoff-")      # the runner removes it when the turn ends
    path = os.path.join(scratch, "prompt.md")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    cmd = _command(["-m", model, "-f", path], ATTACHED_PROMPT, cwd, edits=edits, read_dirs=read_dirs)
    return replace(cmd, scratch=cmd.scratch + (scratch,)), None


def session_id(line):
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    sid = d.get("sessionID") if isinstance(d, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _command(session, prompt, cwd, *, edits, read_dirs=()):
    prefix = launcher()
    if prefix is None:
        return None
    if prompt.startswith("-"):
        prompt = " " + prompt
    argv = prefix + RUN_FLAGS + session + ([] if edits else ["--agent", "plan"]) + [prompt]
    home = turn_config_home()
    env = {"XDG_CONFIG_HOME": home,                   # no global opencode.json, plugins, agents, commands
           "OPENCODE_DISABLE_CLAUDE_CODE": "1",       # nor ~/.claude and project .claude instructions
           "OPENCODE_CONFIG_PROJECT_DISABLE": "1",    # nor the project's opencode.json and .opencode directory
           "OPENCODE_CONFIG_CONTENT": json.dumps({"permissions": _permission_rules(edits, read_dirs)})}
    return TurnCommand(argv=argv, cwd=cwd, env=env, unset=CONFIG_OVERRIDES, stdin=None, scratch=(home,))


def _clip(value, limit=TOOL_TEXT_LIMIT // 10):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _error_message(err):
    """The readable part of an opencode error object (or the string it already is)."""
    if isinstance(err, str) and err:
        return err
    if isinstance(err, dict):
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        for value in (data.get("message"), err.get("message"), err.get("name")):
            if isinstance(value, str) and value:
                return value
    return "turn failed"


def turn_events(line):
    """Events for one `--format json` line of `opencode run`: text parts, tool parts (one event
    per status update) and an error event for a failed step. step markers and anything
    unknown are dropped; the process exit is the turn's end."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(d, dict):
        return []
    if d.get("type") == "error" and not isinstance(d.get("part"), dict):
        # a failed step: {"type": "error", "error": {"name", "data": {"message"}}}, no part
        return [{"kind": "error", "message": _clip(_error_message(d.get("error")), 2000)}]
    if not isinstance(d.get("part"), dict):
        return []
    part, kind = d["part"], d.get("type")
    if kind == "text" and isinstance(part.get("text"), str) and part["text"]:
        return [{"kind": "text", "text": part["text"]}]
    if part.get("type") == "tool":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status")
        if status == "error":
            return [{"kind": "tool_result", "text": _clip(state.get("error") or "tool failed"), "error": True}]
        if status == "completed":
            return [{"kind": "tool_result", "text": _clip(state.get("output") or ""), "error": False}]
        if status in ("pending", "running"):
            return [{"kind": "tool", "name": str(part.get("tool", "")), "input": _clip(state.get("input") or {})}]
        return []
    if kind == "error" or part.get("type") == "error":
        msg = part.get("error") or part.get("message") or d.get("error") or "turn failed"
        return [{"kind": "error", "message": _clip(msg, 2000)}]
    return []


def last_activity(native_id, locator):
    """`time_updated` of the session row: the database file's mtime says nothing about one
    session, and WAL writes do not touch it anyway."""
    if not locator.startswith("sqlite:") or "#" not in locator:
        return None
    db_path, _, session_id = locator[len("sqlite:"):].rpartition("#")
    if session_id != native_id:
        return None
    try:
        conn = _connect_ro(db_path)
    except sqlite3.Error:
        return None
    try:
        table = SESSION_TABLE[detect_schema(conn)]
        row = conn.execute(f"SELECT time_updated FROM {table} WHERE id=?", (session_id,)).fetchone()
    except (sqlite3.Error, RuntimeError):   # unreadable or unknown layout: no activity information
        return None
    finally:
        conn.close()
    return row["time_updated"] / 1000 if row and row["time_updated"] is not None else None
