"""Claude Code adapter: `~/.claude/projects/<encoded-cwd>/<session>.jsonl`.

Verified layout (Claude Code 2.1.x): one JSON event per line. `user` / `assistant`
events carry `uuid`, `timestamp`, `cwd`, `gitBranch`, `entrypoint`, `isSidechain` and a
`message` whose `content` is a string or a list of typed parts (text, thinking, tool_use,
tool_result). Assistant events also carry `message.model`, `message.usage` and a
`requestId`; one API request is written as several events (one per content block), so
usage is de-duplicated on `requestId`. `ai-title` events carry the generated title.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from convo_index.config import PROJECTS_DIR, CLASSIFY_PROMPT_PREFIX, SUMMARY_PROMPT_PREFIX
from convo_index.model import (Conversation, Message, ToolCall, UsageRecord, KIND_TEXT, KIND_THINKING,
                               KIND_TOOL_CALL, KIND_TOOL_RESULT, FIDELITY_FULL, FIDELITY_PARTIAL, split_id)
from convo_index.sources.base import SourceRef, UnreadableSource, shell_quote

TOOL_TEXT_LIMIT = 4000
# tool names -> file action for the files-touched facet
_FILE_TOOLS = {"Read": "read", "View": "read", "Edit": "edit", "Write": "create", "MultiEdit": "edit", "NotebookEdit": "edit"}

SOURCE = "claude_code"
PARSER_VERSION = 3       # part of the change key: bump when parsing changes so indexed sessions re-ingest once

# Entrypoints that indicate a non-interactive, programmatic session rather than a
# real conversation worth indexing (e.g. `claude -p` headless SDK invocations).
SKIP_ENTRYPOINTS = {"sdk-cli", "sdk-py", "sdk-ts"}
# Record types Claude Code writes; a file with none of these (and no uuid/sessionId on any
# record) is an unknown layout, not an empty session.
KNOWN_TYPES = {"user", "assistant", "system", "summary", "ai-title", "file-history-snapshot",
               "queue-operation", "progress", "attachment"}


_CMD_TAG_RE = re.compile(r"</?command-[a-z]+>")


def flatten_content(content):
    """message.content is either a string or a list of typed parts; keep text parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                out.append(p["text"])
        return "\n".join(out)
    return ""


def _tool_result_text(part):
    c = part.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def blocks_in_order(content):
    """(kind, text, tool_call) for every block of an event, in the order they were written.
    Consecutive text blocks merge into one text entry; a string content is one text block."""
    out = []
    if isinstance(content, str):
        return [(KIND_TEXT, content, None)] if content else []
    if not isinstance(content, list):
        return out
    for p in content:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "text" and p.get("text"):
            if out and out[-1][0] == KIND_TEXT:
                out[-1] = (KIND_TEXT, out[-1][1] + "\n" + p["text"], None)
            else:
                out.append((KIND_TEXT, p["text"], None))
        elif t == "thinking" and p.get("thinking"):
            out.append((KIND_THINKING, p["thinking"], None))
        elif t == "tool_use":
            name = p.get("name") or "tool"
            args = p.get("input") if isinstance(p.get("input"), dict) else None
            text = f"[{name}] " + (json.dumps(args, ensure_ascii=False) if args else "")
            path = (args or {}).get("file_path") or (args or {}).get("notebook_path") or (args or {}).get("path")
            actions = [(_FILE_TOOLS[name], path)] if name in _FILE_TOOLS and isinstance(path, str) else []
            out.append((KIND_TOOL_CALL, text[:TOOL_TEXT_LIMIT].rstrip(),
                        ToolCall(native_id=p.get("id"), name=name, args=args, file_actions=actions)))
        elif t == "tool_result":
            body = _tool_result_text(p)
            if p.get("is_error"):
                body = "ERROR: " + body
            out.append((KIND_TOOL_RESULT, body[:TOOL_TEXT_LIMIT],
                        ToolCall(native_id=p.get("tool_use_id"), name="result", result_text=body[:TOOL_TEXT_LIMIT] or None)))
    return out


def clean_prompt(text):
    """Strip <command-message>/<command-name> wrapper tags, keep the slash command/body."""
    lines = []
    for line in text.splitlines():
        stripped = _CMD_TAG_RE.sub("", line).strip()
        # drop the descriptive <command-message> content, keep the actual /command-name
        if line.strip().startswith("<command-message>"):
            continue
        if stripped:
            lines.append(stripped)
    return "\n".join(lines).strip() or text.strip()


def _usage_record(d, seq):
    msg = d.get("message") or {}
    u = msg.get("usage")
    if not isinstance(u, dict):
        return None
    # cache_creation_input_tokens is the 5m + 1h total; the breakdown (when present) prices the
    # two tiers differently. Without it the total counts as 5-minute writes.
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    total = u.get("cache_creation_input_tokens")
    w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        cache_write, cache_write_1h = total, None
    else:
        cache_write, cache_write_1h = (w5 or 0), (w1 or 0)
        if total is not None and (w5 or 0) + (w1 or 0) != total:   # breakdown does not add up: trust the total as 5m
            cache_write, cache_write_1h = total, None
    return UsageRecord(
        native_request_id=d.get("requestId") or d.get("uuid"),
        message_seq=seq,
        provider="anthropic",
        model=msg.get("model"),
        input_uncached=u.get("input_tokens"),
        cache_read=u.get("cache_read_input_tokens"),
        cache_write=cache_write,
        cache_write_1h=cache_write_1h,
        output=u.get("output_tokens"),
        reasoning=(u.get("output_tokens_details") or {}).get("thinking_tokens"),
        ts=d.get("timestamp"),
    ) if any(k in u for k in ("input_tokens", "output_tokens")) else None


def read_snapshot(path):
    """Read a session file as a consistent snapshot.

    Claude Code appends to the file while a session is live. We fstat the open descriptor
    first and read exactly that many bytes, so the revision we report describes precisely
    the bytes we parsed: an append that lands during or after the read changes mtime/size
    and is picked up next run instead of being silently acknowledged. A trailing partial
    line (a write in progress) is dropped. Returns (text, revision)."""
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        data = fh.read(st.st_size)
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        data = data[:cut + 1] if cut >= 0 else b""
    return data.decode("utf-8", "replace"), f"{st.st_mtime}:{st.st_size}"


def parse_conversation(path, text=None):
    """Parse one session file into a plain dict (kept for tests and for the archive
    naming code); `to_conversation` lifts it into the normalized model. `text` may be a
    pre-read snapshot from `read_snapshot`."""
    path = Path(path)
    session_id = path.stem
    cwd = git_branch = title = first_prompt = start_ts = end_ts = entrypoint = None
    version = None
    recognized = 0       # Claude Code records: an unknown layout has none
    messages = []
    usage = []
    pending_usage = {}   # requestId -> UsageRecord awaiting its first text-bearing block
    provisional_link = {}   # requestId -> True while message_seq points at a non-text block
    models = []
    sidechain = 0
    lines = malformed = 0
    seq = 0
    if text is None:
        text, _ = read_snapshot(path)
    if True:
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
            if not isinstance(d, dict):
                malformed += 1
                continue
            t = d.get("type")
            if t is not None and not isinstance(t, str):
                malformed += 1          # a record whose type is not a string
                continue
            if t in KNOWN_TYPES or d.get("uuid") or d.get("sessionId"):
                recognized += 1
            if d.get("sessionId"):
                session_id = d["sessionId"]
            if t == "ai-title" and d.get("aiTitle"):
                title = d["aiTitle"]
            if t in ("user", "assistant"):
                if d.get("isMeta"):
                    continue
                if version is None and d.get("version"):
                    version = d["version"]
                if d.get("isSidechain"):
                    sidechain += 1
                msg = d.get("message")
                if msg is None:
                    msg = {}
                elif not isinstance(msg, dict):
                    malformed += 1          # a typed event whose message is not an object
                    continue
                ts = d.get("timestamp")
                model = msg.get("model") if t == "assistant" else None
                if model and model not in models:
                    models.append(model)
                text = flatten_content(msg.get("content"))
                # Usage rides on every content-block event of a request (thinking, tool_use,
                # text...). Keep one record per requestId and link it to the first
                # text-bearing block of that request, which may arrive several events later.
                if t == "assistant":
                    rid = d.get("requestId") or d.get("uuid")
                    if rid and rid not in pending_usage:
                        rec = _usage_record(d, None)
                        if rec:
                            usage.append(rec)
                            pending_usage[rid] = rec
                # Every block becomes its own message, in the order it was written: text stays
                # text; thinking / tool_use / tool_result are searchable but never embedded or
                # summarized (kind != text).
                blocks = blocks_in_order(msg.get("content"))
                if t == "assistant" and blocks:
                    rec = pending_usage.get(rid)
                    if rec is not None:
                        # Link to the request's first text block, even if it arrives in a later
                        # event; until then the first block of any kind (a tool-only turn keeps it).
                        first_text = next((i for i, b in enumerate(blocks) if b[0] == KIND_TEXT), None)
                        if first_text is not None and provisional_link.get(rid, True):
                            rec.message_seq = seq + first_text
                            provisional_link[rid] = False
                        elif rec.message_seq is None:
                            rec.message_seq = seq
                            provisional_link[rid] = True
                if not blocks:
                    continue
                if t == "user":
                    if d.get("cwd"):
                        cwd = d["cwd"]
                    if d.get("gitBranch"):
                        git_branch = d["gitBranch"]
                    if d.get("entrypoint") and entrypoint is None:
                        entrypoint = d["entrypoint"]
                    if first_prompt is None and text:
                        first_prompt = clean_prompt(text)
                if start_ts is None and ts:
                    start_ts = ts
                if ts:
                    end_ts = ts
                uuid = d.get("uuid")
                n_text = sum(1 for b in blocks if b[0] == KIND_TEXT)
                for i, (kind, body, tc) in enumerate(blocks):
                    role = "tool" if kind == KIND_TOOL_RESULT else t
                    # the (single) text block keeps the bare event uuid, as before; others get #i
                    nid = uuid if (kind == KIND_TEXT and n_text == 1) else (f"{uuid}#{i}" if uuid else None)
                    messages.append({"seq": seq, "role": role, "kind": kind, "ts": ts, "text": body,
                                     "uuid": nid, "model": model, "tool_call": tc})
                    seq += 1
    if not title:
        title = first_prompt or session_id
    return {
        "session_id": session_id,
        "cwd": cwd,
        "git_branch": git_branch,
        "title": title,
        "first_prompt": first_prompt or "",
        "start_ts": start_ts,
        "end_ts": end_ts,
        "entrypoint": entrypoint,
        "version": version,
        "sidechain_messages": sidechain,
        "lines": lines,
        "malformed_lines": malformed,
        "recognized": recognized,
        "models": models,
        "usage": usage,
        "messages": messages,
    }


def exclude_reason(parsed):
    """Why a parsed session must not be indexed, or None. Raises UnreadableSource when the
    file has records but every one of them was unparseable."""
    if not parsed["messages"]:
        # No messages recovered. If any record was unparseable it could be the reason, so
        # this is not a confirmed-empty session even when metadata lines (ai-title, mode...)
        # parsed fine: keep whatever was indexed before and report it.
        if parsed.get("malformed_lines"):
            raise UnreadableSource(
                f"{parsed['malformed_lines']} of {parsed['lines']} lines unparseable, no messages recovered")
        if parsed.get("lines") and not parsed.get("recognized"):
            raise UnreadableSource(f"{parsed['lines']} records in an unknown layout (no typed records)")
        return "empty"
    # Skip non-interactive headless sessions (e.g. our own `claude -p` Haiku
    # classification calls), which would otherwise pollute the index every run.
    # The first_prompt check is a flush-race-proof backstop: a concurrently-written
    # classification session may be indexed before its `entrypoint` line lands.
    if parsed.get("entrypoint") in SKIP_ENTRYPOINTS:
        return "headless-sdk"
    if parsed["first_prompt"].startswith((CLASSIFY_PROMPT_PREFIX, SUMMARY_PROMPT_PREFIX)):
        return "self-llm-call"
    return None


def to_conversation(parsed, path):
    msgs = [Message(seq=m["seq"], role=m["role"], text=m["text"], kind=m.get("kind", KIND_TEXT),
                    ts=m["ts"], native_id=m.get("uuid"), model=m.get("model"), tool_call=m.get("tool_call"))
            for m in parsed["messages"]]
    metadata = {k: v for k, v in (("entrypoint", parsed.get("entrypoint")),
                                  ("cli_version", parsed.get("version"))) if v}
    warnings = []
    if parsed.get("malformed_lines"):
        warnings.append(f"{parsed['malformed_lines']} unparseable lines skipped")
    if parsed.get("sidechain_messages"):
        # Sub-agent turns are interleaved in the same file; we keep them as ordinary text
        # until a real fixture lets us model parent/child properly.
        warnings.append(f"{parsed['sidechain_messages']} sidechain (sub-agent) messages kept inline")
    return Conversation(
        source=SOURCE, native_id=parsed["session_id"],
        title=parsed["title"], first_prompt=parsed["first_prompt"], messages=msgs,
        source_ref=str(path), cwd=parsed.get("cwd"), git_branch=parsed.get("git_branch"),
        start_ts=parsed.get("start_ts"), end_ts=parsed.get("end_ts"),
        provider="anthropic" if parsed.get("models") else None, models=list(parsed.get("models") or []),
        fidelity=FIDELITY_PARTIAL if parsed.get("malformed_lines") else FIDELITY_FULL,
        warnings=warnings, metadata=metadata, usage=list(parsed.get("usage") or []),
    )


def revision_of(path):
    """Change key "p<parser version>:<mtime>:<size>". mtime alone misses appends inside the
    filesystem's timestamp granularity; the parser version re-ingests every session once
    when parsing changes."""
    st = Path(path).stat()
    return f"p{PARSER_VERSION}:{st.st_mtime}:{st.st_size}"


class ClaudeCodeAdapter:
    name = SOURCE
    supported_versions = "Claude Code 2.x JSONL sessions"

    def __init__(self, projects_dir=None):
        self.projects_dir = Path(projects_dir) if projects_dir else PROJECTS_DIR

    def available(self):
        return self.projects_dir.exists()

    def iter_files(self):
        if not self.projects_dir.exists():
            return
        yield from sorted(self.projects_dir.glob("*/*.jsonl"))

    def discover(self):
        for path in self.iter_files():
            try:
                key = revision_of(path)
            except OSError:
                continue
            yield SourceRef(SOURCE, path.stem, key, str(path))

    def load(self, ref):
        text, revision = read_snapshot(ref.locator)
        revision = f"p{PARSER_VERSION}:{revision}"
        parsed = parse_conversation(ref.locator, text)
        if exclude_reason(parsed):
            return None
        conv = to_conversation(parsed, ref.locator)
        # The file name is the session id; `sessionId` inside should agree, but the
        # namespaced id must follow the ref so discovery and storage stay aligned.
        conv.native_id = ref.native_id
        return conv, revision


def resume_command(conversation_id, cwd):
    """Shell command to continue this conversation in Claude Code."""
    _, native = split_id(conversation_id)
    return f"claude --resume {shell_quote(native)}"
