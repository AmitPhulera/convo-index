"""Codex CLI adapter: `$CODEX_HOME` (default `~/.codex`).

Verified layout (Codex 0.154): `state_<n>.sqlite` has `threads(id, rollout_path, cwd, title,
first_user_message, model, model_provider, git_branch, git_origin_url, git_sha, archived,
history_mode, cli_version, originator, created_at_ms, updated_at_ms, ...)`. Each thread points
at a rollout JSONL (`sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl`) of events
`{timestamp, ordinal, type, payload}`:

  session_meta        cwd, cli_version, originator, model_provider, git{branch, repository_url, commit_hash}
  turn_context        model, cwd, sandbox
  response_item       payload.type: message (role user|assistant|developer; content items
                      input_text/output_text; `internal_chat_message_metadata_passthrough.
                      content_item_kinds` marks injected boilerplate), custom_tool_call
                      (name, input, call_id), custom_tool_call_output (call_id, output[]),
                      function_call / function_call_output, reasoning (summary[], encrypted_content)
  token_usage_record  response_id + usage{input_tokens, cached_input_tokens (subset of input),
                      cache_write_input_tokens, output_tokens, reasoning_output_tokens}
  event_msg           task_started, item_completed (duplicates of response_item), token_count
                      (cumulative + last), task_complete
  world_state         ignored

`response_item` is the canonical transcript; `item_completed` is skipped to avoid doubles.
Usage comes from `token_usage_record` (one per model response); `token_count.total_*` is
cumulative and never summed.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from convo_index.config import CLASSIFY_PROMPT_PREFIX, SUMMARY_PROMPT_PREFIX
from convo_index.model import (Conversation, Message, ToolCall, UsageRecord, KIND_TEXT, KIND_THINKING,
                               KIND_TOOL_CALL, KIND_TOOL_RESULT, FIDELITY_FULL, FIDELITY_PARTIAL, split_id)
from convo_index.sources import base as sources
from convo_index.sources.base import SourceRef, TurnCommand, UnreadableSource, shell_quote

SOURCE = "codex"
PARSER_VERSION = 2       # part of the change key: bump when parsing changes so indexed threads re-ingest once
TOOL_TEXT_LIMIT = 4000
KNOWN_EVENT_TYPES = {"session_meta", "turn_context", "response_item", "token_usage_record", "event_msg", "world_state",
                     "compacted", "compact"}
# content_item_kinds of user/developer messages Codex injects itself (not typed by the user)
BOILERPLATE_KINDS_PREFIXES = ("environments.", "plugins.", "host_skills.", "permissions.", "apps.", "agents_md")


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def state_db_path(home=None):
    home = Path(home) if home else codex_home()
    cands = sorted(glob.glob(str(home / "state_*.sqlite")),
                   key=lambda p: (int(''.join(ch for ch in Path(p).stem if ch.isdigit()) or 0), p))
    return Path(cands[-1]) if cands else None


def _iso_ms(ms):
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def read_snapshot(path):
    """fstat-bounded read (see claude_code.read_snapshot). Returns (text, revision_suffix)."""
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        data = fh.read(st.st_size)
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        data = data[:cut + 1] if cut >= 0 else b""
    return data.decode("utf-8", "replace"), f"{st.st_mtime}:{st.st_size}"


def _texts(items):
    """Text of a content list; tolerant of strings, dicts and lists of strings so a tool
    output in an unexpected shape is still shown rather than dropped."""
    if items is None:
        return ""
    if isinstance(items, str):
        return items
    if isinstance(items, dict):
        return items.get("text") if isinstance(items.get("text"), str) else json.dumps(items, ensure_ascii=False)
    out = []
    for it in items:
        if isinstance(it, dict):
            if it.get("type") in ("input_text", "output_text", "text", "summary_text", "input_image") and it.get("text"):
                out.append(it["text"])
            elif isinstance(it.get("text"), str):
                out.append(it["text"])
            elif it:
                out.append(json.dumps(it, ensure_ascii=False))
        elif isinstance(it, str):
            out.append(it)
    return "\n".join(x for x in out if x)


def _kinds(payload):
    meta = payload.get("internal_chat_message_metadata_passthrough")
    kinds = (meta or {}).get("content_item_kinds") if isinstance(meta, dict) else None
    # one entry per content item; a non-string kind is "unknown", never boilerplate
    return [k if isinstance(k, str) else "" for k in kinds] if isinstance(kinds, list) else []


def _boilerplate_kind(k):
    return bool(k) and any(k.startswith(pfx) for pfx in BOILERPLATE_KINDS_PREFIXES)


def _user_content(payload, first_user_message=None):
    """The content items of a user message that the user actually typed. Codex injects
    environment/plugin/skill/permission text into user messages and marks each item's kind in
    `content_item_kinds`. When kinds and items align, injected items are removed one by one;
    when they do not align, nothing is guessed: all-boilerplate kinds drop the message, mixed
    kinds keep it whole. A message equal to the thread's recorded first message is always kept."""
    content = payload.get("content")
    kinds = _kinds(payload)
    if not kinds:
        return content
    if first_user_message and _texts(content).strip() == first_user_message.strip():
        return content
    if isinstance(content, list) and len(kinds) == len(content):
        return [it for it, k in zip(content, kinds, strict=False) if not _boilerplate_kind(k)]
    return [] if all(_boilerplate_kind(k) for k in kinds) else content



def _tool_text(name, inp, out=None):
    text = f"[{name}]"
    if inp:
        text += " " + (inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False))
    if out:
        text += "\n" + out
    if len(text) > TOOL_TEXT_LIMIT:
        text = text[:TOOL_TEXT_LIMIT] + "\n…[tool text truncated]"
    return text


def parse_rollout(text, thread=None):
    """Normalize one rollout into (messages, usage, meta). `thread` is the state-db row dict."""
    messages, usage = [], []
    meta = {"cwd": None, "cli_version": None, "originator": None, "git_branch": None, "git_origin_url": None,
            "git_sha": None, "models": [], "provider": None, "start_ts": None, "end_ts": None}
    lines = malformed = recognized = 0
    seq = 0
    warnings = []
    group_start = 0        # seq of the first item of the response currently being produced
    usage_seen = 0

    def emit(role, kind, text, ts, native_id, model=None, tool_call=None):
        nonlocal seq
        if not text:
            return
        messages.append(Message(seq=seq, role=role, kind=kind, text=text, ts=ts, native_id=native_id,
                                model=model, tool_call=tool_call))
        seq += 1

    current_model = (thread or {}).get("model")
    if current_model:
        meta["models"].append(current_model)
    first_user_message = (thread or {}).get("first_user_message")
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        lines += 1
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(ev, dict):
            malformed += 1
            continue
        t, p, ts = ev.get("type"), ev.get("payload"), ev.get("timestamp")
        if t in KNOWN_EVENT_TYPES:
            recognized += 1
        if p is None:
            p = {}
        if not isinstance(p, dict):
            malformed += 1     # a payload that is not an object: skip this record, keep the rest
            continue
        if ts:
            meta["start_ts"] = meta["start_ts"] or ts
            meta["end_ts"] = ts
        ordinal = ev.get("ordinal")
        if t == "session_meta":
            meta["cwd"] = p.get("cwd") or meta["cwd"]
            meta["cli_version"] = p.get("cli_version")
            meta["originator"] = p.get("originator")
            meta["provider"] = p.get("model_provider")
            git = p.get("git") or {}
            meta["git_branch"] = git.get("branch") or meta["git_branch"]
            meta["git_origin_url"] = git.get("repository_url") or meta["git_origin_url"]
            meta["git_sha"] = git.get("commit_hash") or meta["git_sha"]
        elif t == "turn_context":
            current_model = p.get("model") or current_model
            if current_model and current_model not in meta["models"]:
                meta["models"].append(current_model)
            meta["cwd"] = meta["cwd"] or p.get("cwd")
        elif t == "response_item":
            pt = p.get("type")
            call_id = p.get("call_id") if isinstance(p.get("call_id"), str) else None
            # stable identity: the item's id, else the tool call id (call/out namespaces), else
            # the event ordinal — so a later recovered record does not shift anonymous rows
            pid = (p.get("id") if isinstance(p.get("id"), str) else None) or \
                  (f"{call_id}:{'out' if pt and pt.endswith('_output') else 'call'}" if call_id else None) or \
                  (f"o{ordinal}" if ordinal is not None else None)
            if pt == "message":
                role = p.get("role")
                if role == "developer":
                    continue   # Codex's own instructions, not the conversation
                body = _texts(_user_content(p, first_user_message) if role == "user" else p.get("content"))
                if role == "user":
                    emit("user", KIND_TEXT, body, ts, pid)
                elif role == "assistant":
                    emit("assistant", KIND_TEXT, body, ts, pid, current_model)
            elif pt in ("custom_tool_call", "function_call"):
                name = p.get("name") or pt
                inp = p.get("input") if pt == "custom_tool_call" else p.get("arguments")
                args = inp if isinstance(inp, dict) else None
                if isinstance(inp, str):
                    try:
                        parsed = json.loads(inp)
                        args = parsed if isinstance(parsed, dict) else None
                    except json.JSONDecodeError:
                        args = None
                emit("assistant", KIND_TOOL_CALL, _tool_text(name, inp), ts, pid, current_model,
                     ToolCall(native_id=p.get("call_id"), name=name, args=args))
            elif pt in ("custom_tool_call_output", "function_call_output"):
                out = _texts(p.get("output"))
                emit("tool", KIND_TOOL_RESULT, _tool_text("result", None, out) if out else "", ts, pid, None,
                     ToolCall(native_id=p.get("call_id"), name="result", result_text=out[:TOOL_TEXT_LIMIT] if out else None))
            elif pt == "reasoning":
                summary = _texts(p.get("summary"))
                if summary:
                    emit("assistant", KIND_THINKING, summary, ts, pid, current_model)
                elif p.get("encrypted_content") and "reasoning encrypted" not in warnings:
                    warnings.append("reasoning encrypted")
            # other item types (web_search_call, local_shell_call...) are ignored
        elif t == "token_usage_record":
            u = p.get("usage") if isinstance(p.get("usage"), dict) else {}
            inp, cached = u.get("input_tokens"), u.get("cached_input_tokens") or 0
            # A token_usage_record closes the model response written since the previous one
            # (event order, not timestamps: items and their record can share a timestamp).
            # Link it to that response's first text item, else its first item; a record with
            # no assistant item in its window stays unlinked rather than guessed.
            window = [m for m in messages[group_start:] if m.role == "assistant" and m.kind in (KIND_TEXT, KIND_TOOL_CALL)]
            first_text = next((m for m in window if m.kind == KIND_TEXT), None)
            anchor = first_text or (window[0] if window else None)
            usage.append(UsageRecord(native_request_id=p.get("response_id") or p.get("turn_id") or f"usage{usage_seen}",
                                     message_seq=anchor.seq if anchor else None,
                                     provider=meta["provider"] or (thread or {}).get("model_provider") or "openai",
                                     model=current_model,
                                     input_uncached=(inp - cached) if inp is not None else None,
                                     cache_read=cached or None, cache_write=u.get("cache_write_input_tokens"),
                                     output=u.get("output_tokens"), reasoning=u.get("reasoning_output_tokens"), ts=ts))
            usage_seen += 1
            group_start = len(messages)
        elif t == "event_msg" and isinstance(p.get("type"), str) and p["type"] in ("task_started", "user_message"):
            group_start = len(messages)   # a new turn: nothing before it belongs to the next response
    meta.update({"lines": lines, "malformed": malformed, "recognized": recognized, "warnings": warnings})
    return messages, usage, meta


class CodexAdapter:
    name = SOURCE
    supported_versions = "Codex CLI 0.15x (state_*.sqlite threads + sessions/*.jsonl rollouts)"

    def __init__(self, home=None):
        self.home = Path(home) if home else None

    def _home(self):
        return self.home or codex_home()

    def available(self):
        return state_db_path(self._home()) is not None

    def _threads(self):
        """All thread rows, cached per state-db mtime so load() does not rescan the table for
        every conversation."""
        db = state_db_path(self._home())
        if db is None:
            return []
        # Committed rows may sit in the WAL file before a checkpoint touches the main file.
        stamp = [str(db)]
        for f in (db, db.with_name(db.name + "-wal")):
            try:
                st = f.stat()
                stamp += [st.st_mtime, st.st_size]
            except OSError:
                stamp += [None, None]
        stamp = tuple(stamp)
        cache = getattr(self, "_threads_cache", None)
        if cache and cache[0] == stamp:
            return cache[1]
        conn = sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
            need = {"id", "rollout_path", "cwd", "title", "updated_at_ms"}
            if not need <= cols:
                raise RuntimeError(f"codex state db: threads is missing {sorted(need - cols)}; "
                                   f"supported: {self.supported_versions}")
            rows = [dict(r) for r in conn.execute("SELECT * FROM threads ORDER BY created_at_ms")]
            self._threads_cache = (stamp, rows)
            self._threads_by_id = {r["id"]: r for r in rows}
            return rows
        finally:
            conn.close()

    def discover(self):
        for t in self._threads():
            path = t.get("rollout_path") or ""
            try:
                st = Path(path).stat()
                key = f"p{PARSER_VERSION}:{t.get('updated_at_ms')}:{st.st_mtime}:{st.st_size}"
            except OSError:
                key = f"p{PARSER_VERSION}:{t.get('updated_at_ms')}:missing"
            yield SourceRef(SOURCE, t["id"], key, path)

    def load(self, ref):
        self._threads()
        thread = getattr(self, "_threads_by_id", {}).get(ref.native_id)
        if thread is None:
            return None
        path = Path(ref.locator)
        if not path.is_file():
            # Thread known but no rollout on disk: keep what the state db offers, marked partial.
            first = thread.get("first_user_message") or ""
            if not first or first.startswith((CLASSIFY_PROMPT_PREFIX, SUMMARY_PROMPT_PREFIX)):
                return None   # nothing to show, or the indexer's own (ephemeral) call
            msgs = [Message(seq=0, role="user", text=first, kind=KIND_TEXT, ts=_iso_ms(thread.get("created_at_ms")))]
            return Conversation(source=SOURCE, native_id=thread["id"], title=thread.get("title") or first[:80],
                                first_prompt=first, messages=msgs, source_ref=str(path), cwd=thread.get("cwd"),
                                git_branch=thread.get("git_branch"), git_origin_url=thread.get("git_origin_url"),
                                start_ts=_iso_ms(thread.get("created_at_ms")), end_ts=_iso_ms(thread.get("updated_at_ms")),
                                provider=thread.get("model_provider"), models=[thread["model"]] if thread.get("model") else [],
                                fidelity=FIDELITY_PARTIAL, warnings=["rollout file missing; only the first message is known"],
                                metadata={"cli_version": thread.get("cli_version"), "archived": bool(thread.get("archived"))}), ref.change_key
        text, rev_suffix = read_snapshot(path)
        messages, usage, meta = parse_rollout(text, thread)
        if not messages:
            if meta["lines"] and meta["malformed"] == meta["lines"]:
                raise UnreadableSource(f"{meta['lines']} lines, none parseable")
            if meta["malformed"]:
                raise UnreadableSource(f"{meta['malformed']} of {meta['lines']} lines unparseable, no messages recovered")
            if meta["lines"] and not meta["recognized"]:
                raise UnreadableSource(f"{meta['lines']} records in an unknown layout (no known event types); "
                                       f"supported: {self.supported_versions}")
            return None
        first = next((m.text for m in messages if m.role == "user" and m.kind == KIND_TEXT), "")
        if first.startswith((CLASSIFY_PROMPT_PREFIX, SUMMARY_PROMPT_PREFIX)):
            return None   # the indexer's own LLM call (normally --ephemeral, never persisted)
        warnings = list(meta["warnings"])
        if meta["malformed"]:
            warnings.append(f"{meta['malformed']} unparseable lines skipped")
        models = meta["models"] or ([thread["model"]] if thread.get("model") else [])
        conv = Conversation(
            source=SOURCE, native_id=thread["id"], title=thread.get("title") or first[:80] or thread["id"],
            first_prompt=first, messages=messages, source_ref=str(path),
            cwd=meta["cwd"] or thread.get("cwd"), git_branch=meta["git_branch"] or thread.get("git_branch"),
            git_origin_url=meta["git_origin_url"] or thread.get("git_origin_url"),
            start_ts=meta["start_ts"] or _iso_ms(thread.get("created_at_ms")),
            end_ts=meta["end_ts"] or _iso_ms(thread.get("updated_at_ms")),
            provider=meta["provider"] or thread.get("model_provider"), models=models,
            fidelity=FIDELITY_PARTIAL if meta["malformed"] else FIDELITY_FULL, warnings=warnings,
            metadata={k: v for k, v in {"cli_version": meta["cli_version"] or thread.get("cli_version"),
                                        "originator": meta["originator"] or thread.get("originator"),
                                        "history_mode": thread.get("history_mode"),
                                        "git_sha": meta["git_sha"] or thread.get("git_sha"),
                                        "archived": bool(thread.get("archived"))}.items() if v not in (None, "", False)},
            usage=usage)
        return conv, f"p{PARSER_VERSION}:{thread.get('updated_at_ms')}:{rev_suffix}"


def resume_command(conversation_id, cwd):
    _, native = split_id(conversation_id)
    return f"codex resume {shell_quote(native)}"


# --- Browser turns (sources.base turn protocol) ----------------------------------------------
#
# `codex exec resume <id> -` runs one more turn of a thread and appends it to the same rollout,
# so `codex resume <id>` in a terminal afterwards sees it. Codex runs model-generated shell
# commands in its own sandbox: Read-only (`-s read-only`) can read but not write, Edit
# (`-s workspace-write`) can write in the project and the temporary directories; neither has
# network access. `approval_policy = "never"` means anything that would ask (an MCP tool, a
# command outside the sandbox) fails and the model is told so.
#
# Verified on 0.166.0. `--ignore-user-config` drops config.toml (MCP servers, profiles, the
# `notify` program, trusted projects), but several surfaces load without it: ChatGPT apps (an
# MCP server whose read tools run without asking), plugins, skills (the project's
# `.agents/skills` too), the project's AGENTS.md, multi-agent tools and web search. Each is
# switched off below; with these flags a fresh thread's context holds only the permissions
# note, the environment and the user's own `$CODEX_HOME/AGENTS.md`. Unknown feature names and
# unknown `-c` keys are refused (`--strict-config`), so an upgrade that renames one fails the
# turn rather than reopening the surface.
#
# A resumed thread keeps neither its model nor its provider (with the user's config ignored,
# Codex falls back to its defaults and refuses a thread recorded with another provider), so
# both are read from the rollout and passed back.

TURNS_ON_WINDOWS = False      # Codex's Windows sandbox is not verified; turns are not offered there
CLOSED_FEATURES = ["apps", "plugins", "remote_plugin", "hooks", "computer_use", "browser_use", "browser_use_external",
                   "in_app_browser", "image_generation", "multi_agent", "tool_suggest", "skill_mcp_dependency_install",
                   "memories", "goals"]
TURN_FLAGS = (["exec", "--json",
               "--strict-config",          # an unknown -c key or feature name is an error, not ignored
               "--ignore-user-config",     # no config.toml: MCP servers, profiles, notify, project trust
               "--ignore-rules",           # no user or project execpolicy .rules
               "--skip-git-repo-check"]    # the conversation's directory need not be a repository
              + [a for f in CLOSED_FEATURES for a in ("--disable", f)]
              + ["-c", "project_doc_max_bytes=0",          # no project AGENTS.md
                 "-c", "skills.include_instructions=false",  # no skill catalog (user or project)
                 "-c", 'web_search="disabled"',
                 "-c", 'approval_policy="never"'])         # anything that would ask is refused
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*")    # a model or provider name, never an option
TURN_TEXT_LIMIT = 400


def launcher():
    """argv prefix that starts Codex without a shell in between, or None when it is not installed."""
    return None if sources.is_windows() else sources.npm_launcher("codex")


def thread_settings(native_id, locator):
    """(model_provider, model) recorded in the rollout: the provider from `session_meta`, the
    model from the last `turn_context`. Raises ValueError when the file is gone or its
    `session_meta` names another thread."""
    provider = model = None
    meta_id = None
    try:
        with open(locator, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if '"session_meta"' not in raw and '"turn_context"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue                   # a malformed record, which the indexer skips too
                p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
                if d.get("type") == "session_meta" and meta_id is None:
                    meta_id, provider = p.get("id"), p.get("model_provider")
                elif d.get("type") == "turn_context" and isinstance(p.get("model"), str):
                    model = p["model"]
    except OSError:
        meta_id = None
    if meta_id != native_id:
        raise ValueError("the Codex rollout is gone or no longer holds this thread; refresh the index")
    ok = lambda v: v if isinstance(v, str) and MODEL_RE.fullmatch(v) else None   # noqa: E731
    return ok(provider), ok(model)


def turn_command(native_id, prompt, cwd, *, edits=False, mcp_tools=(), locator=None):
    """`mcp_tools` is ignored: MCP servers live in the config file a turn does not load. Raises
    ValueError for an id that is not a UUID (`resume` would look a name up) or a rollout that
    does not hold this thread."""
    prefix = launcher()
    if prefix is None:
        return None
    if not UUID_RE.fullmatch(native_id) or not locator or not os.path.isabs(locator):
        raise ValueError("the Codex rollout is gone or no longer holds this thread; refresh the index")
    provider, model = thread_settings(native_id, locator)
    argv = prefix + TURN_FLAGS
    if provider:
        argv += ["-c", f'model_provider="{provider}"']
    if model:
        argv += ["-m", model]
    argv += ["-s", "workspace-write" if edits else "read-only", "-C", cwd, "resume", native_id, "-"]
    return TurnCommand(argv=argv, cwd=cwd, stdin=prompt)


def _turn_clip(value, limit=TURN_TEXT_LIMIT):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _item_events(item, started):
    """Events for one `item.started` / `item.completed` of `codex exec --json`."""
    t = item.get("type")
    if t == "agent_message" and not started and isinstance(item.get("text"), str) and item["text"]:
        return [{"kind": "text", "text": item["text"]}]
    if t == "command_execution":
        if started:
            return [{"kind": "tool", "name": "shell", "input": _turn_clip(item.get("command") or "")}]
        failed = item.get("status") != "completed" or item.get("exit_code") not in (0, None)
        out = item.get("aggregated_output") or ""
        if failed and item.get("exit_code") is not None:
            out = f"exit {item['exit_code']}\n{out}"
        return [{"kind": "tool_result", "text": _turn_clip(out), "error": failed}]
    if t == "mcp_tool_call":
        name = f"{item.get('server')}.{item.get('tool')}"
        if started:
            return [{"kind": "tool", "name": name, "input": _turn_clip(item.get("arguments") or {})}]
        err = item.get("error") if isinstance(item.get("error"), dict) else None
        if err or item.get("status") == "failed":
            return [{"kind": "tool_result", "text": _turn_clip((err or {}).get("message") or "failed"), "error": True}]
        return [{"kind": "tool_result", "text": _turn_clip(item.get("result") or ""), "error": False}]
    if t == "file_change" and not started:
        changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
        listing = "\n".join(f"{c.get('kind')} {c.get('path')}" for c in changes)
        return [{"kind": "tool", "name": "edit", "input": _turn_clip(listing)},
                {"kind": "tool_result", "text": item.get("status") or "", "error": item.get("status") != "completed"}]
    if t == "web_search" and not started:
        return [{"kind": "tool", "name": "web_search", "input": _turn_clip(item.get("query") or "")}]
    return []                    # reasoning, todo lists, and non-fatal `error` items (warnings)


def turn_events(line):
    """Events for one line of `codex exec --json`. Agent messages, shell commands, MCP calls and
    file changes are kept; reasoning and warnings (items of type "error", such as a model
    switch notice) are dropped. `turn.completed` is the verdict, `turn.failed` an error; a
    top-level `error` (a stream retry notice, or the failure that turn.failed repeats) is shown
    as a text line so a reconnect does not fail the turn."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(d, dict):
        return []
    t = d.get("type")
    if t in ("item.started", "item.completed") and isinstance(d.get("item"), dict):
        return _item_events(d["item"], t == "item.started")
    if t == "turn.completed":
        return [{"kind": "done", "cost_usd": None}]
    if t == "turn.failed":
        err = d.get("error") if isinstance(d.get("error"), dict) else {}
        return [{"kind": "error", "message": _turn_clip(err.get("message") or "turn failed", 2000)}]
    if t == "error" and isinstance(d.get("message"), str):
        return [{"kind": "text", "text": f"[codex: {_turn_clip(d['message'], 300)}]"}]
    return []


def last_activity(native_id, locator):
    """mtime of the rollout, which Codex appends to while a thread is live."""
    return os.stat(locator).st_mtime
