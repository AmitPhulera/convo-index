"""Antigravity CLI adapter (`agy`): `~/.gemini/antigravity-cli` (override: ANTIGRAVITY_HOME).

Verified layout (2026-09): one conversation per `brain/<conversation-id>/`, transcript at
`.system_generated/logs/transcript_full.jsonl` (records `{step_index, source, type, status,
created_at, content?, tool_calls?, thinking?}` with `type ∈ USER_INPUT | PLANNER_RESPONSE |
GENERIC`; `USER_INPUT.content` is wrapped in `<USER_REQUEST>…</USER_REQUEST>`; `GENERIC`
rows are tool output). `conversation_summaries.db` (title, workspace_uris,
parent_conversation_id, last_modified_time) covers only some conversations. No model or
token data exists in the transcript; `conversations/<id>.db` holds protobuf blobs and is
not used. `transcript.jsonl` had identical line counts to `transcript_full.jsonl` for every
local conversation; `_full` is read.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from convo_index.config import CLASSIFY_PROMPT_PREFIX, SUMMARY_PROMPT_PREFIX
from convo_index.model import (Conversation, Message, ToolCall, KIND_TEXT, KIND_THINKING, KIND_TOOL_CALL,
                               KIND_TOOL_RESULT, FIDELITY_FULL, FIDELITY_PARTIAL, make_id, split_id)
from convo_index.sources.base import SourceRef, UnreadableSource, shell_quote

SOURCE = "antigravity"
PARSER_VERSION = 2       # part of the change key: bump when parsing changes so indexed transcripts re-ingest once
KNOWN_STEP_TYPES = {"USER_INPUT", "PLANNER_RESPONSE", "GENERIC", "CORTEX_STEP", "SYSTEM"}
TRANSCRIPT = Path(".system_generated") / "logs" / "transcript_full.jsonl"
TOOL_TEXT_LIMIT = 4000
_REQ_OPEN = re.compile(r"^\s*<USER_REQUEST>\s*", re.DOTALL)
_REQ_CLOSE = re.compile(r"\s*</USER_REQUEST>\s*$", re.DOTALL)


def antigravity_home():
    return Path(os.environ.get("ANTIGRAVITY_HOME") or (Path.home() / ".gemini" / "antigravity-cli")).expanduser()


def read_snapshot(path):
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        data = fh.read(st.st_size)
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        data = data[:cut + 1] if cut >= 0 else b""
    return data.decode("utf-8", "replace"), f"{st.st_mtime}:{st.st_size}"


def _unwrap(content):
    """Strip the <USER_REQUEST> wrapper Antigravity adds; tolerate a missing closing tag and
    trailing material after it (keep everything but the tags)."""
    if not isinstance(content, str):
        return ""
    text = _REQ_OPEN.sub("", content, count=1)
    text = _REQ_CLOSE.sub("", text, count=1)
    # a closing tag that is not at the end still belongs to the wrapper (trailing material
    # follows it); only the first one is removed so a literal tag the user typed survives
    return text.replace("</USER_REQUEST>", "", 1).strip() if "</USER_REQUEST>" in text and _REQ_CLOSE.search(content or "") is None else text.strip()


def _tool_text(name, args):
    text = f"[{name}]"
    if args:
        text += " " + (args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))
    return text[:TOOL_TEXT_LIMIT]


_INTERNAL_PATH_MARKERS = ("/.gemini/", "/antigravity-cli/", "/.claude/", "/.codex/", "/node_modules/")


def _cwd_from_args(args):
    """(priority, path): an explicit Cwd beats a directory argument beats a file's parent.
    Paths inside tool-internal directories (skills, caches) never count."""
    if not isinstance(args, dict):
        return None
    for prio, keys, is_file in ((0, ("Cwd", "cwd"), False), (1, ("SearchDirectory", "TargetDirectory"), False),
                                (2, ("AbsolutePath",), True)):
        for k in keys:
            v = args.get(k)
            if isinstance(v, str) and v.startswith("/") and not any(m in v for m in _INTERNAL_PATH_MARKERS):
                return prio, (str(Path(v).parent) if is_file else v)
    return None


def parse_transcript(text):
    """Returns (messages, meta) where meta has first_ts, last_ts, cwd_guess, lines, malformed."""
    messages = []
    meta = {"first_ts": None, "last_ts": None, "cwd_guess": None, "lines": 0, "malformed": 0, "recognized": 0}
    best_cwd = None            # (priority, path); an explicit Cwd seen later beats an early file path
    pending_calls = []         # tool-call ids awaiting their GENERIC result, in order
    seq = 0

    def emit(role, kind, body, ts, native_id, tool_call=None):
        nonlocal seq
        if not body:
            return
        messages.append(Message(seq=seq, role=role, kind=kind, text=body, ts=ts, native_id=native_id, tool_call=tool_call))
        seq += 1

    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        meta["lines"] += 1
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            meta["malformed"] += 1
            continue
        if not isinstance(d, dict):
            meta["malformed"] += 1
            continue
        ts = d.get("created_at")
        if ts:
            meta["first_ts"] = meta["first_ts"] or ts
            meta["last_ts"] = ts
        idx = d.get("step_index")
        t = d.get("type")
        if t is not None and not isinstance(t, str):
            meta["malformed"] += 1
            continue
        if t in KNOWN_STEP_TYPES:
            meta["recognized"] += 1
        if t == "USER_INPUT":
            emit("user", KIND_TEXT, _unwrap(d.get("content")), ts, f"{idx}")
        elif t == "PLANNER_RESPONSE":
            if isinstance(d.get("thinking"), str) and d["thinking"]:
                emit("assistant", KIND_THINKING, d["thinking"], ts, f"{idx}:think")
            calls = d.get("tool_calls")
            calls = calls if isinstance(calls, list) else ([] if calls is None else [None])
            for i, tc in enumerate(calls):
                if not isinstance(tc, dict):
                    meta["malformed"] += 1     # a tool call that is not an object: skip it, keep the step
                    continue
                name = tc.get("name") if isinstance(tc.get("name"), str) else "tool"
                args = tc.get("args")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass
                guess = _cwd_from_args(args)
                if guess and (best_cwd is None or guess[0] < best_cwd[0]):
                    best_cwd = guess
                call_id = f"{idx}:{i}"
                pending_calls.append(call_id)
                emit("assistant", KIND_TOOL_CALL, _tool_text(name, args), ts, f"{idx}:tool{i}",
                     ToolCall(native_id=call_id, name=name, args=args if isinstance(args, dict) else None))
            if isinstance(d.get("content"), str) and d["content"]:
                emit("assistant", KIND_TEXT, d["content"], ts, f"{idx}:text")
        elif t == "GENERIC":
            body = (d.get("content") if isinstance(d.get("content"), str) else "")[:TOOL_TEXT_LIMIT]
            call_id = pending_calls.pop(0) if pending_calls else f"{idx}"   # results follow calls in order
            emit("tool", KIND_TOOL_RESULT, body, ts, f"{idx}",
                 ToolCall(native_id=call_id, name="result", result_text=body or None))
        # other step types: ignored (no transcript text)
    meta["cwd_guess"] = best_cwd[1] if best_cwd else None
    return messages, meta


_summaries_cache = {}


def _summaries(home):
    """{conversation_id: {"title", "workspace", "parent"}} from conversation_summaries.db, if
    readable; cached per file mtime/size so load() does not re-read it per conversation."""
    db = home / "conversation_summaries.db"
    if not db.exists():
        return {}
    stamp = []
    for f in (db, db.with_name(db.name + "-wal")):   # committed rows may still sit in the WAL
        try:
            st = f.stat(); stamp += [st.st_mtime, st.st_size]
        except OSError:
            stamp += [None, None]
    stamp = tuple(stamp)
    cached = _summaries_cache.get(str(db))
    if cached and cached[0] == stamp:
        return cached[1]
    out = {}
    try:
        conn = sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            for cid, title, uris, parent in conn.execute(
                    "SELECT conversation_id, title, workspace_uris, parent_conversation_id FROM conversation_summaries"):
                ws = None
                try:
                    lst = json.loads(uris or "[]")
                except (json.JSONDecodeError, TypeError):
                    lst = []
                if isinstance(lst, list) and lst and isinstance(lst[0], str):   # optional metadata: ignore odd shapes
                    ws = _workspace_path(lst[0])
                out[cid] = {"title": (title or "").strip() or None, "workspace": ws, "parent": (parent or "").strip() or None}
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    _summaries_cache[str(db)] = (stamp, out)
    return out


def _workspace_path(uri):
    """`file:///Users/me/My%20Project` -> `/Users/me/My Project`; a bare path is returned as is;
    other schemes (or a remote authority) are not local paths -> None."""
    from urllib.parse import unquote, urlsplit
    if uri.startswith("/"):
        return uri
    parts = urlsplit(uri)
    if parts.scheme != "file" or (parts.netloc not in ("", "localhost")):
        return None
    return unquote(parts.path) or None


def _summary_stamp(info):
    """Part of the change key: title/workspace/parent arrive in the summaries db after the
    transcript, so their appearance must trigger a re-ingest."""
    if not info:
        return "-"
    return f"{info.get('title') or ''}|{info.get('workspace') or ''}|{info.get('parent') or ''}"


class AntigravityAdapter:
    name = SOURCE
    supported_versions = "Antigravity CLI (brain/<id>/.system_generated/logs/transcript_full.jsonl)"

    def __init__(self, home=None):
        self.home = Path(home) if home else None

    def _home(self):
        return self.home or antigravity_home()

    def available(self):
        return (self._home() / "brain").is_dir()

    def discover(self):
        brain = self._home() / "brain"
        if not brain.is_dir():
            return
        summaries = _summaries(self._home())
        for d in sorted(brain.iterdir()):
            f = d / TRANSCRIPT
            if not f.is_file():
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            yield SourceRef(SOURCE, d.name, f"p{PARSER_VERSION}:{st.st_mtime}:{st.st_size}|{_summary_stamp(summaries.get(d.name))}", str(f))

    def load(self, ref):
        if not Path(ref.locator).is_file():
            return None   # removed between discover and load: nothing to index
        text, revision = read_snapshot(ref.locator)
        messages, meta = parse_transcript(text)
        if not messages:
            if meta["lines"] and meta["malformed"] == meta["lines"]:
                raise UnreadableSource(f"{meta['lines']} lines, none parseable")
            if meta["malformed"]:
                raise UnreadableSource(f"{meta['malformed']} of {meta['lines']} lines unparseable, no messages recovered")
            if meta["lines"] and not meta["recognized"]:
                raise UnreadableSource(f"{meta['lines']} records in an unknown layout (no known step types); "
                                       f"supported: {self.supported_versions}")
            return None
        first = next((m.text for m in messages if m.role == "user" and m.kind == KIND_TEXT), "")
        if first.startswith((CLASSIFY_PROMPT_PREFIX, SUMMARY_PROMPT_PREFIX)):
            return None   # the indexer's own agy call (agy has no --ephemeral)
        info = _summaries(self._home()).get(ref.native_id, {})
        revision = f"p{PARSER_VERSION}:{revision}|{_summary_stamp(info or None)}"
        warnings = ["source records no model or token usage"]
        if meta["malformed"]:
            warnings.append(f"{meta['malformed']} unparseable lines skipped")
        relation = ("subagent", make_id(SOURCE, info["parent"])) if info.get("parent") else None
        conv = Conversation(
            source=SOURCE, native_id=ref.native_id, title=info.get("title") or first[:80] or ref.native_id,
            first_prompt=first, messages=messages, source_ref=ref.locator,
            cwd=info.get("workspace") or meta["cwd_guess"], git_branch=None, relation=relation,
            start_ts=meta["first_ts"], end_ts=meta["last_ts"], provider="google", models=[],
            fidelity=FIDELITY_PARTIAL if meta["malformed"] else FIDELITY_FULL, warnings=warnings,
            metadata={"cwd_source": "summaries" if info.get("workspace") else ("tool_args" if meta["cwd_guess"] else "unknown")})
        return conv, revision


def resume_command(conversation_id, cwd):
    _, native = split_id(conversation_id)
    return f"agy --conversation {shell_quote(native)}"
