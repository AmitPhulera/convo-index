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
import tempfile
import uuid
from pathlib import Path

from convo_index.config import PROJECTS_DIR, CLASSIFY_PROMPT_PREFIX, SUMMARY_PROMPT_PREFIX, HANDOFF_PROMPT_PREFIXES
from convo_index.model import (Conversation, Message, ToolCall, UsageRecord, KIND_TEXT, KIND_THINKING,
                               KIND_TOOL_CALL, KIND_TOOL_RESULT, FIDELITY_FULL, FIDELITY_PARTIAL, split_id)
from convo_index.sources import base as sources
from convo_index import turn_tools
from convo_index.sources.base import SourceRef, TurnCommand, UnreadableSource, read_snapshot, shell_quote

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


_HANDOFF_TITLE_RE = re.compile(r"^- title: (.+)$", re.M)


def handoff_title(prompt):
    """"Continued: <title>" / "Second opinion: <title>" for a handoff's session, whose prompt
    opens with HANDOFF_PROMPT_PREFIXES and names the original's title; else None."""
    for prefix, label in zip(HANDOFF_PROMPT_PREFIXES, ("Continued", "Second opinion"), strict=True):
        if (prompt or "").startswith(prefix):
            m = _HANDOFF_TITLE_RE.search(prompt)
            return f"{label}: {m.group(1).strip()}" if m else label
    return None


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
        title = handoff_title(first_prompt) or first_prompt or session_id
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


def exclude_reason(parsed, handoff=False):
    """Why a parsed session must not be indexed, or None. Raises UnreadableSource when the
    file has records but every one of them was unparseable. `handoff`: a handoff started this
    session (it is linked), so its being headless does not keep it out."""
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
    # A handoff's new session is headless as well, but a real conversation: known by its link,
    # or (before the link is written, or for a link the user deleted) by its prompt's opening.
    if (parsed.get("entrypoint") in SKIP_ENTRYPOINTS and not handoff
            and not parsed["first_prompt"].startswith(HANDOFF_PROMPT_PREFIXES)):
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
        self.handoff_ids = set()      # set by the indexer from conversation_links

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
        if exclude_reason(parsed, handoff=ref.native_id in self.handoff_ids):
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


# --- Browser turns (sources.base turn protocol) ----------------------------------------------
#
# `claude -p --resume <id>` runs one more turn of a session and appends it to the same
# transcript, so an interactive `claude --resume` afterwards sees it. Verified on 2.1.311: with
# the read-only flags below the init event lists no MCP servers, a PreToolUse hook in the
# project's settings does not fire, Bash is absent, and a write outside the working directory
# is refused by --restricted.
#
# Edit mode adds the edit tools, Bash and the MCP tools on the user's allowlist (turn_tools).
# --safe-mode drops MCP servers even when they are passed with --mcp-config, so Edit mode runs
# without it and closes the rest another way: --setting-sources "" loads no user, project or
# local settings (no hooks, plugins or permission rules; only Claude Code's built-in plugins
# load), and the turn's own mcp.json holds just the allowlisted servers. Verified on 2.1.311:
# Bash runs once --tools names it and --allowedTools allows it, an allowlisted MCP tool runs,
# `mcp__<server>` allows all of a server's tools, and an MCP tool not on the list is denied.

TURN_FLAGS = ["-p", "--output-format", "stream-json", "--verbose",   # stream-json needs --verbose in print mode
              "--safe-mode",              # no plugins, hooks, MCP servers, skills, commands; auth and built-in tools work
              "--restricted",             # no Bash/code tools, no user/project/local settings, file tools confined to cwd
              "--strict-mcp-config",      # with no --mcp-config: no MCP servers at all
              "--disable-slash-commands", # the prompt is a message, never a skill or command
              "--permission-prompts", "none"]   # anything that would ask is denied
EDIT_FLAGS = ["-p", "--output-format", "stream-json", "--verbose",
              "--setting-sources", "",    # no user/project/local settings: no hooks, plugins or permission rules
              "--restricted",             # file tools confined to cwd; Bash only because --tools names it
              "--strict-mcp-config",      # MCP servers only from the turn's own --mcp-config
              "--disable-slash-commands",
              "--permission-prompts", "none"]
READ_TOOLS = "Read,Glob,Grep"
EDIT_TOOLS = "Read,Glob,Grep,Edit,Write,MultiEdit,Bash"
TURNS_ON_WINDOWS = True       # the prompt goes through stdin; see launcher()
TURN_TEXT_LIMIT = 400         # chars of a tool input/result shown in a turn event (the transcript parser keeps TOOL_TEXT_LIMIT)


def launcher():
    """argv prefix that starts Claude Code without a shell in between, or None when it is not
    installed: an npm `claude.cmd` shim on Windows is bypassed for node (sources.npm_launcher);
    the native installer's `claude.exe` and the POSIX binary are used as they are."""
    return sources.npm_launcher("claude")


def claude_config_path():
    """Claude Code's user configuration file, where `claude mcp add --scope user` puts servers."""
    base_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base_dir) / ".claude.json" if base_dir else Path.home() / ".claude.json"


def mcp_servers():
    """{name: spec} of the user-scope MCP servers; {} when there are none or the file is unreadable.
    claude.ai account connectors are not in any file and cannot be loaded into a turn."""
    try:
        servers = json.loads(claude_config_path().read_text(encoding="utf-8")).get("mcpServers")
    except (OSError, ValueError, AttributeError):
        return {}
    return {k: v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}


def turn_command(native_id, prompt, cwd, *, edits=False, mcp_tools=(), locator=None):
    return _command(["--resume", native_id], prompt, cwd, edits=edits, mcp_tools=mcp_tools)


# A handoff starts a new session with the same closing flags. The id is ours (`--session-id`),
# so the link to the original can be written before the harness answers. `--restricted`
# confines Read to the cwd; each of `read_dirs` (the transcript copy's directory) is opened
# with `--add-dir` and nothing else outside is. Verified on 2.1.316: the init event echoes the
# id, and Read opens a file in an added directory under the read-only flags.

def new_session_command(prompt, cwd, *, edits=False, mcp_tools=(), read_dirs=(), model=None):
    """(TurnCommand, native id) for a new session seeded with `prompt`, or (None, None)."""
    native_id = str(uuid.uuid4())
    extra = [a for d in read_dirs for a in ("--add-dir", str(d))] + (["--model", model] if model else [])
    cmd = _command(["--session-id", native_id], prompt, cwd, edits=edits, mcp_tools=mcp_tools, extra=extra)
    return (cmd, native_id) if cmd else (None, None)


def session_id(line):
    """The session id an init event reports, or None."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(d, dict) and d.get("type") == "system" and isinstance(d.get("session_id"), str):
        return d["session_id"]
    return None


def _command(session, prompt, cwd, *, edits, mcp_tools, extra=()):
    prefix = launcher()
    if prefix is None:
        return None
    if not edits:
        return TurnCommand(argv=prefix + session + TURN_FLAGS + ["--tools", READ_TOOLS] + list(extra), cwd=cwd, stdin=prompt)
    servers = mcp_servers()
    chosen = [p for p in (turn_tools.split(e, servers) for e in mcp_tools) if p]   # a server removed since is skipped
    allowed = ["Bash"] + [f"mcp__{server}" if tool == "*" else f"mcp__{server}__{tool}" for server, tool in chosen]
    argv = prefix + session + EDIT_FLAGS + ["--tools", EDIT_TOOLS, "--permission-mode", "acceptEdits"]
    scratch = ()
    if chosen:
        # the specs can carry headers with tokens: an owner-only directory the runner removes
        scratch = (tempfile.mkdtemp(prefix="convo-index-claude-"),)
        config = os.path.join(scratch[0], "mcp.json")
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {server: servers[server] for server, _ in chosen}}, fh)
        argv += ["--mcp-config", config]
    argv += ["--allowedTools", ",".join(allowed)] + list(extra)
    return TurnCommand(argv=argv, cwd=cwd, stdin=prompt, scratch=scratch)


def _turn_clip(value, limit=TURN_TEXT_LIMIT):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _turn_result_text(block):
    content = block.get("content")
    if isinstance(content, list):
        content = "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return _turn_clip(content if isinstance(content, str) else json.dumps(content))


def turn_events(line):
    """Events for one stream-json line of `claude -p`. Assistant text and tool_use blocks,
    tool results, the permission denials and the final result are kept; everything else
    (init, rate limits, token counters) is dropped."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(d, dict):
        return []
    t = d.get("type")
    message = d.get("message") if isinstance(d.get("message"), dict) else {}
    blocks = message.get("content") if isinstance(message.get("content"), list) else []
    out = []
    if t == "assistant":
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text"):
                out.append({"kind": "text", "text": b["text"]})
            elif b.get("type") == "tool_use":
                out.append({"kind": "tool", "name": str(b.get("name", "")), "input": _turn_clip(b.get("input", {}))})
    elif t == "user":
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out.append({"kind": "tool_result", "text": _turn_result_text(b), "error": bool(b.get("is_error"))})
    elif t == "result":
        for den in d.get("permission_denials") or []:
            if isinstance(den, dict):
                out.append({"kind": "denied", "name": str(den.get("tool_name", "")), "input": _turn_clip(den.get("tool_input", {}))})
        if d.get("is_error"):
            out.append({"kind": "error", "message": _turn_clip(d.get("result") or d.get("subtype") or "turn failed", 2000)})
        else:
            out.append({"kind": "done", "cost_usd": d.get("total_cost_usd"), "turns": d.get("num_turns"),
                        "stop": d.get("subtype")})
    return out


def last_activity(native_id, locator):
    """mtime of the transcript, which Claude Code appends to while a session is live."""
    return os.stat(locator).st_mtime
