"""MCP server over stdio (docs/design.md §11): `convo-index mcp`.

JSON-RPC 2.0, one message per line. Tools: search_conversations, get_conversation,
get_related. Read-only; respects the same hidden/deleted state as the UI; every hit carries
`source` and `fidelity`. Results returned here go to whichever agent asked, which may itself
be hosted (docs/design.md §4 table) — that is the user's agent configuration, not this tool's.
"""
from __future__ import annotations

import json
import sys

from convo_index import db as dbm
from convo_index import export as X
from convo_index import references as REF
from convo_index import search as SE
from convo_index import settings as S

PROTOCOL = "2025-06-18"
KNOWN_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_LIMIT = 50
MAX_MESSAGES = 2000
MAX_CHARS = 100_000


def _version():
    try:
        from importlib.metadata import version
        return version("convo-index")
    except Exception:
        return "unknown"


SERVER_INFO = {"name": "convo-index", "version": _version()}

TOOLS = [
    {"name": "search_conversations",
     "description": "Search indexed AI coding-assistant conversations (Claude Code, Opencode, Codex, Antigravity, pi). "
                    "Hybrid keyword + semantic search; returns ids to pass to get_conversation.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "search text; empty lists the most recent conversations"},
         "source": {"type": "string", "enum": ["claude_code", "opencode", "codex", "antigravity", "pi"]},
         "tags": {"type": "array", "items": {"type": "string"}, "description": '"group:tag" filters, e.g. "workspace:work"'},
         "project": {"type": "string", "description": "repo root / cwd to restrict to"},
         "mode": {"type": "string", "enum": ["hybrid", "keyword", "semantic"], "default": "hybrid"},
         "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 10}},
      "required": ["query"]}},
    {"name": "get_conversation",
     "description": "Full record of one conversation: metadata, summary, tags, references, token usage/cost estimate and transcript.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "conversation id from search_conversations (source:native_id)"},
         "max_messages": {"type": "integer", "minimum": 1, "default": 200},
         "max_chars": {"type": "integer", "minimum": 200, "default": 4000, "description": "per-message text cap"},
         "include_tools": {"type": "boolean", "default": False, "description": "include tool calls and results"}},
      "required": ["id"]}},
    {"name": "get_related",
     "description": "Timeline of every conversation linked to a ticket key (e.g. SAAS-1234), a GitHub PR/issue "
                    "(github.com/owner/repo#123) or a branch (bare name, resolved to its project when unambiguous; "
                    "the response lists candidates otherwise).",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
]


class ToolError(Exception):
    pass


def _conn(settings):
    """Read the index; never create one. An MCP call against a missing database must say so
    instead of leaving an empty convos.db behind and answering with SQL errors."""
    path = settings.db_path()
    if not path.exists():
        raise ToolError(f"no index at {path}; run `convo-index index` first")
    conn = dbm.connect(path)
    if not dbm._table_exists(conn, "conversations"):
        conn.close()
        raise ToolError(f"{path} has no conversations table; run `convo-index index` first")
    return conn


def tool_search(args, settings):
    q = str(args.get("query") or "").strip()
    limit = max(1, min(int(args.get("limit") or 10), MAX_LIMIT))
    raw_tags = args.get("tags") or []
    tags = [raw_tags] if isinstance(raw_tags, str) else [str(t) for t in raw_tags]
    mode = args.get("mode") or "hybrid"
    if mode not in ("hybrid", "keyword", "semantic"):
        raise ToolError("mode must be hybrid, keyword or semantic")
    conn = _conn(settings)        # before embedding: that path opens the database and would create it
    try:
        qvec = None
        note = None
        if q and mode in ("hybrid", "semantic"):
            from convo_index import server
            qvec = server.embed([q])[0]
            if qvec is None:
                reason = server.semantic_status()[1] or "semantic search unavailable"
                if mode == "semantic":
                    raise ToolError(f"semantic search unavailable: {reason}")   # never an empty success
                note = f"keyword results only: {reason}"
        hits = SE.search(conn, q, qvec, mode=mode, limit=limit, source=args.get("source") or None,
                         project=args.get("project") or None, tags=tags)
    finally:
        conn.close()
    keep = ("id", "title", "source", "start_ts", "cwd", "git_branch", "message_count", "fidelity", "snippet", "tags", "tickets")
    out = {"count": len(hits), "results": [{k: h.get(k) for k in keep if k in h} for h in hits[:limit]]}
    if note:
        out["note"] = note
    return out


def tool_get(args, settings):
    cid = str(args.get("id") or "")
    if not cid:
        raise ToolError("id is required")
    conn = _conn(settings)
    try:
        try:
            cid = dbm.resolve_id(conn, cid)
        except dbm.AmbiguousId as e:
            raise ToolError(f"ambiguous id; use one of {e.args[0]}") from None
        if dbm.is_deleted(conn, cid) or cid in dbm.get_hidden_ids(conn):
            return None                                   # hidden/deleted never leave the machine this way
        try:
            max_messages = max(1, min(int(args.get("max_messages") or 200), MAX_MESSAGES))
            max_chars = max(200, min(int(args.get("max_chars") or 4000), MAX_CHARS))
        except (TypeError, ValueError):
            raise ToolError("max_messages and max_chars must be integers") from None
        return X.as_dict(conn, cid, settings, include_tools=bool(args.get("include_tools", False)),
                         max_messages=max_messages, max_chars=max_chars)
    finally:
        conn.close()


def tool_related(args, settings):
    key = str(args.get("key") or "").strip()
    if not key:
        raise ToolError("key is required")
    conn = _conn(settings)
    try:
        return REF.timeline(conn, key, settings)
    finally:
        conn.close()


HANDLERS = {"search_conversations": tool_search, "get_conversation": tool_get, "get_related": tool_related}


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(msg, settings=None):
    """One JSON-RPC message in, one response dict out (None for notifications)."""
    settings = settings or S.load()
    if not isinstance(msg, dict):
        return _error(None, -32600, "invalid request")
    method, id_, params = msg.get("method"), msg.get("id"), msg.get("params")
    if method is None:
        return None                                   # a response to us; nothing to do
    if id_ is None:
        return None                                   # JSON-RPC 2.0 §4.1: never reply to a notification
    if not isinstance(method, str):
        return _error(id_, -32600, "invalid request: method must be a string")
    if method.startswith("notifications/"):
        return None
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(id_, -32600, "invalid request: params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(id_, {"protocolVersion": asked if asked in KNOWN_PROTOCOLS else PROTOCOL,
                             "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO,
                             "instructions": "Search the user's local archive of coding-assistant conversations. "
                                             "Ids look like source:native_id."})
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return _error(id_, -32602, f"unknown tool {name!r}")
        try:
            out = fn(args, settings)
        except ToolError as e:
            return _result(id_, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:  # never kill the server on a bad query
            return _result(id_, {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True})
        if out is None:
            return _result(id_, {"content": [{"type": "text", "text": "not found"}], "isError": True})
        return _result(id_, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                             "structuredContent": out if isinstance(out, dict) else None})
    return _error(id_, -32601, f"method not found: {method}")


def _utf8(stream):
    """MCP messages are UTF-8; Windows pipes default to the ANSI code page, which would garble a
    non-ASCII query and crash on a non-ASCII title. Reconfigured in place, before any I/O."""
    if hasattr(stream, "reconfigure") and (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
        stream.reconfigure(encoding="utf-8")
    return stream


def serve(stdin=None, stdout=None, settings=None):
    stdin = _utf8(stdin or sys.stdin)
    stdout = _utf8(stdout or sys.stdout)
    settings = settings or S.load()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = _error(None, -32700, "parse error")
        else:
            try:
                resp = handle(msg, settings)
            except Exception as e:   # no input may end the loop
                rid = msg.get("id") if isinstance(msg, dict) else None
                resp = _error(rid, -32603, f"internal error: {type(e).__name__}: {e}") if rid is not None else None
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
