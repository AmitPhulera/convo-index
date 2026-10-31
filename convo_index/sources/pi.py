"""pi coding agent adapter: `~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl`.

Verified layout (pi 1.4.x, session format version 3): one JSON entry per line. The first
line is the header `{"type": "session", "id", "cwd", "timestamp", "version"}`; every other
entry has `id`, `parentId` and `timestamp` and forms a tree, because pi can branch a session
from an earlier point. The live conversation is the path from the root to the leaf, and the
leaf is the last entry in the file (that is how pi itself picks it when it reopens a file).
Entries on abandoned branches are skipped and counted.

`message` entries carry `{role, content, ...}`: `user` content is a string or a list of
`text`/`image` blocks; `assistant` content is a list of `text`, `thinking` and `toolCall`
blocks, plus `provider`, `model` and `usage`; a `toolResult` message carries `toolCallId`,
`toolName`, `content` (text blocks) and `isError`; `bashExecution` records a command the
user ran from the prompt. `model_change`, `thinking_level_change` and `session_info` (the
name the user gave the session) are metadata; `compaction`, `branch_summary`, `label`,
`context_edit`, `custom` and `custom_message` are skipped.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from convo_index.model import (Conversation, Message, ToolCall, UsageRecord, KIND_TEXT, KIND_THINKING,
                               KIND_TOOL_CALL, KIND_TOOL_RESULT, FIDELITY_FULL, FIDELITY_PARTIAL, split_id)
from convo_index.sources import base as sources
from convo_index.sources.base import SourceRef, TurnCommand, UnreadableSource, read_snapshot, shell_quote

TOOL_TEXT_LIMIT = 4000
SOURCE = "pi"
PARSER_VERSION = 1       # part of the change key: bump when parsing changes so indexed sessions re-ingest once

# Every entry type pi 1.4 writes (core/session-manager.d.ts). A file with none of these is
# an unknown layout, not an empty session.
KNOWN_TYPES = {"session", "message", "model_change", "thinking_level_change", "session_info", "usage",
               "compaction", "branch_summary", "label", "context_edit", "custom", "custom_message"}
# Entry types whose content never reaches the index; counted so the warning can say so.
SKIPPED_TYPES = {"compaction", "branch_summary", "label", "context_edit", "custom", "custom_message", "usage"}
# pi message roles -> normalized roles; `system` (prompt updates) carries no transcript
_ROLES = {"user": "user", "assistant": "assistant", "toolResult": "tool", "bashExecution": "user"}
# built-in tool names -> file action for the files-touched facet
_FILE_TOOLS = {"read": "read", "edit": "edit", "write": "create"}


def _text_of(content):
    """Text of a message content that is a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"])
    return ""


def _add_text(out, text):
    if out and out[-1][0] == KIND_TEXT:
        out[-1] = (KIND_TEXT, out[-1][1] + "\n" + text, None)
    else:
        out.append((KIND_TEXT, text, None))


def blocks_in_order(msg):
    """(kind, text, tool_call) for every block of a message, in the order written. Consecutive
    text blocks merge into one text entry; a string content is one text block."""
    role = msg.get("role")
    out = []
    if role == "toolResult":
        body = _text_of(msg.get("content"))
        if msg.get("isError"):
            body = "ERROR: " + body
        body = body[:TOOL_TEXT_LIMIT]
        out.append((KIND_TOOL_RESULT, body,
                    ToolCall(native_id=msg.get("toolCallId"), name="result", result_text=body or None)))
        return out
    if role == "bashExecution":
        cmd = msg.get("command")
        if not isinstance(cmd, str) or not cmd:
            return out
        text = f"[bash] $ {cmd}"
        output = msg.get("output")
        if isinstance(output, str) and output:
            text += "\n" + output
        out.append((KIND_TOOL_CALL, text[:TOOL_TEXT_LIMIT].rstrip(),
                    ToolCall(native_id=None, name="bash", args={"command": cmd},
                             result_text=output[:TOOL_TEXT_LIMIT] if isinstance(output, str) and output else None)))
        return out
    content = msg.get("content")
    if isinstance(content, str):
        return [(KIND_TEXT, content, None)] if content else out
    if not isinstance(content, list):
        return out
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and isinstance(b.get("text"), str) and b["text"]:
            _add_text(out, b["text"])
        elif t == "thinking" and isinstance(b.get("thinking"), str) and b["thinking"]:
            out.append((KIND_THINKING, b["thinking"], None))
        elif t == "toolCall":
            name = b.get("name") or "tool"
            args = b.get("arguments") if isinstance(b.get("arguments"), dict) else None
            text = f"[{name}] " + (json.dumps(args, ensure_ascii=False) if args else "")
            path = (args or {}).get("path")
            actions = [(_FILE_TOOLS[name], path)] if name in _FILE_TOOLS and isinstance(path, str) else []
            out.append((KIND_TOOL_CALL, text[:TOOL_TEXT_LIMIT].rstrip(),
                        ToolCall(native_id=b.get("id"), name=name, args=args, file_actions=actions)))
    return out


def _well_formed(msg):
    """A message object pi could have written: a string role, and the content shape that
    role carries (text for user/assistant/toolResult, a command for bashExecution)."""
    if not isinstance(msg, dict) or not isinstance(msg.get("role"), str):
        return False
    role = msg["role"]
    if role in ("user", "assistant", "toolResult"):
        return isinstance(msg.get("content"), (str, list))
    if role == "bashExecution":
        return isinstance(msg.get("command"), str)
    return True


def _usage_record(entry, msg, seq):
    u = msg.get("usage")
    if not isinstance(u, dict) or not any(isinstance(u.get(k), int) for k in ("input", "output")):
        return None
    cost = u.get("cost") if isinstance(u.get("cost"), dict) else {}
    total = cost.get("total")
    # cacheWrite is the total of 5-minute and 1-hour cache writes; cacheWrite1h (when present)
    # is the 1-hour share, priced differently, so the two are stored as disjoint buckets.
    cache_write, cache_write_1h = u.get("cacheWrite"), u.get("cacheWrite1h")
    if isinstance(cache_write, int) and isinstance(cache_write_1h, int) and 0 <= cache_write_1h <= cache_write:
        cache_write -= cache_write_1h
    else:
        cache_write_1h = None
    return UsageRecord(
        native_request_id=msg.get("responseId") or entry.get("id"),
        message_seq=seq,
        provider=msg.get("provider"),
        model=msg.get("model"),
        input_uncached=u.get("input"),
        cache_read=u.get("cacheRead"),
        cache_write=cache_write,
        cache_write_1h=cache_write_1h,
        output=u.get("output"),
        reasoning=u.get("reasoning"),
        reported_cost_usd=float(total) if isinstance(total, (int, float)) and not isinstance(total, bool) else None,
        ts=entry.get("timestamp"),
    )


HEADER_SCAN_BYTES = 1024 * 1024     # pi's own bound when it looks for a header


def header_id(path):
    """The id in the session header, found the way pi finds it: within the first MiB, the
    first non-blank line that parses must be a `session` entry with a string id."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEADER_SCAN_BYTES)
    except OSError:
        return None
    for raw in head.split(b"\n"):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "session" and isinstance(d.get("id"), str) and d["id"]:
            return d["id"]
        return None
    return None


def native_id_of(path):
    """The session id: the header's `id` (pi resumes by it, and `--session <path>` lets a
    file carry any name), else what follows the first underscore of pi's default file name
    `<ISO timestamp>_<uuid>.jsonl`, else the file stem."""
    found = header_id(path)
    if found:
        return found
    stem = Path(path).stem
    return stem.partition("_")[2] or stem


def live_path(entries):
    """The entries on the live branch, root to leaf. `entries` are the parsed tree entries in
    file order (header excluded); the leaf is the last of them. An entry whose parent is
    missing starts the path there, so a truncated file still yields its tail."""
    if not entries:
        return []
    by_id = {e["id"]: e for e in entries if isinstance(e.get("id"), str)}
    path, seen = [], set()
    cur = entries[-1]
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        path.append(cur)
        parent = cur.get("parentId")
        cur = by_id.get(parent) if isinstance(parent, str) else None
    path.reverse()
    return path


def parse_session(path, text=None):
    """Parse one session file into a plain dict; `to_conversation` lifts it into the
    normalized model. `text` may be a pre-read snapshot from `read_snapshot`."""
    path = Path(path)
    session_id = Path(path).stem.partition("_")[2] or path.stem    # until the header says otherwise
    cwd = title = start_ts = None
    version = None
    recognized = 0
    lines = malformed = 0
    entries = []
    skipped = {}       # entry type -> count, for types whose content is never indexed
    if text is None:
        text, _ = read_snapshot(path)
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        lines += 1
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(d, dict) or not isinstance(d.get("type"), str):
            malformed += 1
            continue
        t = d["type"]
        if t in KNOWN_TYPES:
            recognized += 1
        if t != "session" and not isinstance(d.get("id"), str):
            malformed += 1          # every tree entry carries an id; without one it cannot be placed
            continue
        if t == "session":
            if isinstance(d.get("id"), str) and d["id"]:
                session_id = d["id"]
            if isinstance(d.get("cwd"), str):
                cwd = d["cwd"]
            if isinstance(d.get("timestamp"), str):
                start_ts = d["timestamp"]
            version = d.get("version")
            continue
        if t == "message" and not _well_formed(d.get("message")):
            malformed += 1          # not an object, or a role/content shape pi never writes
            d = {"type": "malformed", "id": d["id"], "parentId": d.get("parentId")}   # stays a tree link
        if t in SKIPPED_TYPES:
            skipped[t] = skipped.get(t, 0) + 1
        if t == "session_info" and isinstance(d.get("name"), str) and d["name"].strip():
            title = d["name"].strip()
        entries.append(d)

    path_entries = live_path(entries)
    branched = sum(1 for e in entries if e["type"] == "message") - sum(1 for e in path_entries if e["type"] == "message")
    messages, usage, models = [], [], []
    first_prompt = end_ts = None
    seq = 0
    for d in path_entries:
        if d["type"] == "model_change":
            name = _model_name(d.get("provider"), d.get("modelId"))
            if name and name not in models:
                models.append(name)
            continue
        if d["type"] != "message":
            continue
        msg = d["message"]
        role = msg.get("role")
        mrole = _ROLES.get(role)
        if mrole is None:
            continue                # `system` (prompt updates) and unknown roles carry no transcript
        ts = d.get("timestamp") if isinstance(d.get("timestamp"), str) else None
        blocks = blocks_in_order(msg)
        if role == "assistant":
            # Model and usage are kept even for a response without content (an aborted or
            # failed request still cost tokens); message_seq then stays unset.
            name = _model_name(msg.get("provider"), msg.get("model"))
            if name and name not in models:
                models.append(name)
            rec = _usage_record(d, msg, None)
            if rec:
                if blocks:
                    rec.message_seq = seq + next((i for i, b in enumerate(blocks) if b[0] == KIND_TEXT), 0)
                usage.append(rec)
        if not blocks:
            continue
        if role == "user" and first_prompt is None:
            text = _text_of(msg.get("content")).strip()
            if text:
                first_prompt = text
        if ts:
            start_ts = start_ts or ts
            end_ts = ts
        model = msg.get("model") if role == "assistant" else None
        n_text = sum(1 for b in blocks if b[0] == KIND_TEXT)
        for i, (kind, body, tc) in enumerate(blocks):
            nid = d["id"] if (kind == KIND_TEXT and n_text == 1) else f"{d['id']}#{i}"
            messages.append({"seq": seq, "role": mrole, "kind": kind, "ts": ts, "text": body,
                             "native_id": nid, "model": model, "tool_call": tc})
            seq += 1
    return {
        "session_id": session_id,
        "cwd": cwd,
        "title": title or first_prompt or session_id,
        "first_prompt": first_prompt or "",
        "start_ts": start_ts,
        "end_ts": end_ts,
        "version": version,
        "lines": lines,
        "malformed_lines": malformed,
        "recognized": recognized,
        "branched_messages": branched,
        "skipped": skipped,
        "models": models,
        "usage": usage,
        "messages": messages,
    }


def _model_name(provider, model):
    if not isinstance(model, str) or not model:
        return None
    return f"{provider}/{model}" if isinstance(provider, str) and provider else model


def exclude_reason(parsed):
    """Why a parsed session must not be indexed, or None. Raises UnreadableSource when the
    file has records but none could be interpreted as a message."""
    if parsed["messages"]:
        return None
    if parsed.get("malformed_lines"):
        raise UnreadableSource(
            f"{parsed['malformed_lines']} of {parsed['lines']} lines unparseable, no messages recovered")
    if parsed.get("lines") and not parsed.get("recognized"):
        raise UnreadableSource(f"{parsed['lines']} records in an unknown layout (no pi entry types)")
    return "empty"


def to_conversation(parsed, path):
    msgs = [Message(seq=m["seq"], role=m["role"], text=m["text"], kind=m["kind"], ts=m["ts"],
                    native_id=m["native_id"], model=m["model"], tool_call=m["tool_call"])
            for m in parsed["messages"]]
    metadata = {"session_version": parsed["version"]} if parsed.get("version") is not None else {}
    warnings = []
    if parsed.get("malformed_lines"):
        warnings.append(f"{parsed['malformed_lines']} unparseable lines skipped")
    if parsed.get("branched_messages"):
        warnings.append(f"{parsed['branched_messages']} messages on abandoned branches skipped")
    skipped = parsed.get("skipped") or {}
    if skipped:
        warnings.append("skipped entries: " + ", ".join(f"{k} x{v}" for k, v in sorted(skipped.items())))
    models = list(parsed.get("models") or [])
    provider = models[0].partition("/")[0] if models and "/" in models[0] else None
    return Conversation(
        source=SOURCE, native_id=parsed["session_id"],
        title=parsed["title"], first_prompt=parsed["first_prompt"], messages=msgs,
        source_ref=str(path), cwd=parsed.get("cwd"),
        start_ts=parsed.get("start_ts"), end_ts=parsed.get("end_ts"),
        provider=provider, models=models,
        fidelity=FIDELITY_PARTIAL if parsed.get("malformed_lines") else FIDELITY_FULL,
        warnings=warnings, metadata=metadata, usage=list(parsed.get("usage") or []),
    )


def sessions_dir():
    """Where pi keeps sessions: `$PI_CODING_AGENT_SESSION_DIR` (a flat directory of session
    files), else `sessions/` under `$PI_CODING_AGENT_DIR` or `~/.pi/agent` (one subdirectory
    per encoded cwd). Returns (path, flat)."""
    override = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if override:
        return Path(override).expanduser(), True
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    base = Path(agent_dir).expanduser() if agent_dir else Path.home() / ".pi" / "agent"
    return base / "sessions", False


def revision_of(path):
    """Change key "p<parser version>:<mtime>:<size>", like the Claude Code adapter's."""
    st = Path(path).stat()
    return f"p{PARSER_VERSION}:{st.st_mtime}:{st.st_size}"


class PiAdapter:
    name = SOURCE
    supported_versions = "pi coding agent 1.x session files (format version 3)"

    def __init__(self, sessions=None, flat=False):
        """`sessions` is the sessions directory (tests); by default it comes from the environment."""
        if sessions is None:
            self.sessions, self.flat = sessions_dir()
        else:
            self.sessions, self.flat = Path(sessions), flat

    def available(self):
        return self.sessions.is_dir()

    def iter_files(self):
        if not self.sessions.is_dir():
            return
        yield from sorted(self.sessions.glob("*.jsonl" if self.flat else "*/*.jsonl"))

    def discover(self):
        for path in self.iter_files():
            try:
                key = revision_of(path)
            except OSError:
                continue
            yield SourceRef(SOURCE, native_id_of(path), key, str(path))

    def load(self, ref):
        text, revision = read_snapshot(ref.locator)
        revision = f"p{PARSER_VERSION}:{revision}"
        parsed = parse_session(ref.locator, text)
        if exclude_reason(parsed):
            return None
        conv = to_conversation(parsed, ref.locator)
        # The file name carries the session id; the header should agree, but the namespaced
        # id must follow the ref so discovery and storage stay aligned.
        conv.native_id = ref.native_id
        return conv, revision


def resume_command(conversation_id, cwd):
    """Shell command to continue this conversation in pi."""
    _, native = split_id(conversation_id)
    return f"pi --session {shell_quote(native)}"


# --- Browser turns (sources.base turn protocol) ----------------------------------------------
#
# `pi -p --session <file>` runs one more turn of a session and appends it to the same file, so
# `pi --session <id>` in a terminal afterwards sees it. The session is named by its file, not
# its id: resuming by id from a directory other than the session's own makes pi ask on stdin
# whether to fork, and the prompt waiting there answers "no", so the run ends with status 0 and
# no output. The prompt travels on stdin, which pi reads to its end.
#
# pi has no permission prompts and no sandbox: the tools named by --tools are the whole policy,
# and its read tools are not confined to the project directory. Verified on 1.4.0: with the
# flags below the system prompt lists only the named tools, no MCP server, extension, skill,
# prompt template or AGENTS.md/CLAUDE.md is loaded, and bash is absent in Read-only.

TURNS_ON_WINDOWS = True       # the prompt goes through stdin; npm's pi.cmd is bypassed for node
TURN_FLAGS = ["-p", "--mode", "json",
              "--no-extensions",          # no extension discovery, installed packages or built-in extensions
              "--no-mcp",                 # no MCP servers and no MCP tools
              "--no-skills", "--no-prompt-templates", "--no-themes",
              "--no-context-files",       # no AGENTS.md or CLAUDE.md, global or in the project
              "--no-approve",             # project-local .pi files are not trusted for this run
              "--offline"]                # no update checks or other startup network calls
READ_TOOLS = "read,grep,find,ls"
EDIT_TOOLS = "read,grep,find,ls,edit,write,bash"
TURN_TEXT_LIMIT = 400         # chars of a tool input/result shown in a turn event


def launcher():
    """argv prefix that starts pi without a shell in between, or None when it is not installed."""
    return sources.npm_launcher("pi")


def turn_command(native_id, prompt, cwd, *, edits=False, mcp_tools=(), locator=None):
    """`mcp_tools` is ignored: --no-mcp loads no server. Raises ValueError when the indexed file
    is gone or its header names another session."""
    prefix = launcher()
    if prefix is None:
        return None
    if not locator or not os.path.isabs(locator) or not os.path.isfile(locator) or header_id(locator) != native_id:
        raise ValueError("the pi session file is gone or no longer holds this session; refresh the index")
    argv = prefix + TURN_FLAGS + ["--session", locator, "--tools", EDIT_TOOLS if edits else READ_TOOLS]
    return TurnCommand(argv=argv, cwd=cwd, stdin=prompt)


def _turn_clip(value, limit=TURN_TEXT_LIMIT):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _cost(messages):
    """Total reported cost of the assistant messages of one run, None when none reports one."""
    costs = [m["usage"]["cost"].get("total") for m in messages
             if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("usage"), dict)
             and isinstance(m["usage"].get("cost"), dict)]
    costs = [c for c in costs if isinstance(c, (int, float)) and not isinstance(c, bool)]
    return sum(costs) if costs else None


def _verdict(messages):
    """done or error for a run that pi will not retry, judged by its final assistant message: in
    JSON mode pi exits 0 even when that request failed."""
    last = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "assistant"), {})
    if last.get("stopReason") == "error":
        return {"kind": "error", "message": _turn_clip(last.get("errorMessage") or "request failed", 2000)}
    return {"kind": "done", "cost_usd": _cost(messages),
            "turns": sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant")}


def turn_events(line):
    """Events for one `--mode json` line of `pi -p`. Whole messages are read from `message_end`
    (assistant text and tool calls, tool results; thinking is dropped); each run ends with an
    `agent_end`, which becomes the turn's verdict unless pi is about to retry a failed request
    (`willRetry`; the retry ends with an `agent_end` of its own, and the runner keeps the last
    verdict). A retry is announced as a text line. The streaming deltas, tool execution updates
    and session header are dropped."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(d, dict):
        return []
    if d.get("type") == "agent_end":
        if d.get("willRetry") is True:
            return []
        return [_verdict(d.get("messages") if isinstance(d.get("messages"), list) else [])]
    if d.get("type") == "auto_retry_start":
        return [{"kind": "text", "text": f"[pi is retrying (attempt {d.get('attempt')} of {d.get('maxAttempts')}) "
                                         f"after: {_turn_clip(str(d.get('errorMessage') or 'an error'), 300)}]"}]
    msg = d.get("message")
    if d.get("type") != "message_end" or not isinstance(msg, dict):
        return []
    out = []
    if msg.get("role") == "assistant":
        for b in msg.get("content") if isinstance(msg.get("content"), list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"]:
                out.append({"kind": "text", "text": b["text"]})
            elif b.get("type") == "toolCall":
                out.append({"kind": "tool", "name": str(b.get("name", "")), "input": _turn_clip(b.get("arguments") or {})})
    elif msg.get("role") == "toolResult":
        out.append({"kind": "tool_result", "text": _turn_clip(_text_of(msg.get("content"))), "error": bool(msg.get("isError"))})
    return out


def last_activity(native_id, locator):
    """mtime of the session file, which pi appends to while a session is live."""
    return os.stat(locator).st_mtime
