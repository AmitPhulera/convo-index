"""Write fictional conversations to disk in each source's native format.

The playground's demo database is built by running the real `convo-index index` over these
files, so every conversation goes through the same adapter a user's own would. Input is one
neutral dict per conversation (see `write_all`); output is the on-disk layout of Claude Code,
Codex CLI, Opencode 1.x, pi and Antigravity under one root, plus the environment that points
an index run at it. Deterministic: ids are uuid5 of the conversation id, times come from the
input, nothing random. Stdlib only.

    convs = [{"id": "c-acme-001", "source": "claude_code", "title": "...", "cwd": "/home/dev/acme",
              "branch": "fix/x", "repo_url": "https://...", "model": "claude-sonnet-4-5",
              "start": "2026-09-22T09:14:00Z",
              "turns": [{"role": "user", "text": "...", "at": 0},
                        {"role": "assistant", "at": 20, "text": "...", "thinking": "...",
                         "tools": [{"name": "bash", "input": {...}, "output": "..."}],
                         "usage": {"input": 1200, "output": 340, "cache_read": 8000, "cache_write": 0}}]}]
    env = write_all(convs, root)     # then: env={**os.environ, **env} convo-index index
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/AmitPhulera/convo-index#demo")
SOURCES = ("claude_code", "codex", "opencode", "pi", "antigravity")


# --- shared helpers --------------------------------------------------------------------------

def _uid(*parts) -> str:
    return str(uuid.uuid5(NS, "/".join(str(p) for p in parts)))


def _start(conv) -> datetime:
    return datetime.fromisoformat(conv["start"].replace("Z", "+00:00")).astimezone(timezone.utc)


def _at(conv, seconds) -> datetime:
    return _start(conv) + timedelta(seconds=seconds)


def _iso(dt: datetime) -> str:
    """2026-09-22T09:14:00.000Z, the form Claude Code, Codex and pi write."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _provider(conv) -> str:
    if conv.get("provider"):
        return conv["provider"]
    m = (conv.get("model") or "").lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gemini"):
        return "google"
    return "openai"


def _usage(turn) -> dict:
    u = turn.get("usage") or {}
    return {k: int(u.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write")}


def _first_user_text(conv) -> str:
    return next((t["text"] for t in conv["turns"] if t["role"] == "user" and t.get("text")), "")


def _jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8", newline="\n")


def _check(conv) -> None:
    if conv.get("source") not in SOURCES:
        raise ValueError(f"{conv.get('id')}: unknown source {conv.get('source')!r}")
    for k in ("id", "cwd", "start", "turns"):
        if not conv.get(k):
            raise ValueError(f"{conv.get('id')}: missing {k!r}")


# --- Claude Code: <HOME>/.claude/projects/<encoded cwd>/<session uuid>.jsonl ------------------
# One event per content block; every event of one assistant turn shares requestId and
# message.usage (the adapter de-duplicates on requestId). Tool results are `user` events.

CLAUDE_VERSION = "2.1.30"


def claude_session_id(conv) -> str:
    return _uid("claude_code", conv["id"])


def write_claude_code(conv, home: Path) -> Path:
    sid = claude_session_id(conv)
    path = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", conv["cwd"]) / f"{sid}.jsonl"
    common = {"sessionId": sid, "cwd": conv["cwd"], "version": CLAUDE_VERSION, "entrypoint": "cli",
              "userType": "external", "isSidechain": False}
    if conv.get("branch"):
        common["gitBranch"] = conv["branch"]
    out, parent, n = [], None, 0

    def event(kind, ts, message, **extra):
        nonlocal parent, n
        uid = _uid("claude_code", conv["id"], "event", n)
        n += 1
        out.append({"parentUuid": parent, **common, "type": kind, "uuid": uid, "timestamp": _iso(ts),
                    "message": message, **extra})
        parent = uid

    for ti, turn in enumerate(conv["turns"]):
        ts = _at(conv, turn.get("at", 0))
        if turn["role"] == "user":
            event("user", ts, {"role": "user", "content": turn["text"]})
            continue
        u = _usage(turn)
        req, mid = f"req_{_uid('claude_code', conv['id'], ti).replace('-', '')[:24]}", \
            f"msg_{_uid('claude_code', conv['id'], ti, 'm').replace('-', '')[:24]}"
        usage = {"input_tokens": u["input"], "cache_creation_input_tokens": u["cache_write"],
                 "cache_read_input_tokens": u["cache_read"], "output_tokens": u["output"], "service_tier": "standard"}

        def assistant(block, ts=ts, mid=mid, req=req, usage=usage):
            event("assistant", ts, {"model": conv["model"], "id": mid, "type": "message", "role": "assistant",
                                    "content": [block], "stop_reason": None, "stop_sequence": None, "usage": usage},
                  requestId=req)

        if turn.get("thinking"):
            assistant({"type": "thinking", "thinking": turn["thinking"], "signature": ""})
        tools = turn.get("tools") or []
        ids = [f"toolu_{_uid('claude_code', conv['id'], ti, i).replace('-', '')[:24]}" for i in range(len(tools))]
        for tool, tid in zip(tools, ids, strict=True):
            assistant({"type": "tool_use", "id": tid, "name": tool["name"], "input": tool.get("input") or {}})
        for tool, tid in zip(tools, ids, strict=True):
            event("user", ts + timedelta(seconds=1), {"role": "user", "content": [
                {"tool_use_id": tid, "type": "tool_result", "content": tool.get("output") or "", "is_error": False}]},
                toolUseResult={"stdout": tool.get("output") or "", "stderr": "", "interrupted": False})
        if turn.get("text"):
            assistant({"type": "text", "text": turn["text"]}, ts=ts + timedelta(seconds=2 if tools else 0))
    if conv.get("title"):
        out.append({"type": "ai-title", "aiTitle": conv["title"], "sessionId": sid})
    _jsonl(path, out)
    return path


# --- Codex CLI: $CODEX_HOME/state_5.sqlite threads + sessions/YYYY/MM/DD/rollout-*.jsonl -------

CODEX_VERSION = "0.154.0"
_CODEX_THREADS = """CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
    created_at INTEGER, updated_at INTEGER, source TEXT, model_provider TEXT, cwd TEXT, title TEXT,
    tokens_used INTEGER, archived INTEGER DEFAULT 0, git_sha TEXT, git_branch TEXT, git_origin_url TEXT,
    cli_version TEXT, first_user_message TEXT, model TEXT, history_mode TEXT, created_at_ms INTEGER,
    updated_at_ms INTEGER, originator TEXT)"""


def codex_thread_id(conv) -> str:
    return _uid("codex", conv["id"])


def write_codex(conv, codex_home: Path, db: sqlite3.Connection) -> Path:
    tid = codex_thread_id(conv)
    start = _start(conv)
    path = (codex_home / "sessions" / start.strftime("%Y") / start.strftime("%m") / start.strftime("%d")
            / f"rollout-{start.strftime('%Y-%m-%dT%H-%M-%S')}-{tid}.jsonl")
    provider = _provider(conv)
    out, ordinal = [], 0

    def ev(t, payload, ts):
        nonlocal ordinal
        out.append({"timestamp": _iso(ts), "ordinal": ordinal, "type": t, "payload": payload})
        ordinal += 1

    git = {k: v for k, v in (("branch", conv.get("branch")), ("repository_url", conv.get("repo_url"))) if v}
    ev("session_meta", {"id": tid, "session_id": tid, "timestamp": _iso(start), "cwd": conv["cwd"],
                        "originator": "codex_cli_rs", "cli_version": CODEX_VERSION, "model_provider": provider,
                        **({"git": git} if git else {})}, start)
    last = start
    tokens_used = 0
    for ti, turn in enumerate(conv["turns"]):
        ts = _at(conv, turn.get("at", 0))
        last = max(last, ts)
        key = f"{conv['id']}/{ti}"
        if turn["role"] == "user":
            turn_id = _uid("codex", key, "turn")
            ev("event_msg", {"type": "task_started", "turn_id": turn_id}, ts)
            ev("turn_context", {"turn_id": turn_id, "cwd": conv["cwd"], "model": conv["model"],
                                "approval_policy": "on-request", "sandbox_policy": {"type": "workspace-write"}}, ts)
            ev("response_item", {"type": "message", "id": f"msg_{_uid('codex', key).replace('-', '')}", "role": "user",
                                 "content": [{"type": "input_text", "text": turn["text"]}]}, ts)
            continue
        if turn.get("thinking"):
            ev("response_item", {"type": "reasoning", "id": f"rs_{_uid('codex', key, 'r').replace('-', '')}",
                                 "summary": [{"type": "summary_text", "text": turn["thinking"]}],
                                 "encrypted_content": None}, ts)
        for i, tool in enumerate(turn.get("tools") or []):
            call_id = f"call_{_uid('codex', key, i).replace('-', '')[:24]}"
            ev("response_item", {"type": "function_call", "id": f"fc_{_uid('codex', key, i, 'f').replace('-', '')}",
                                 "name": tool["name"], "arguments": json.dumps(tool.get("input") or {}),
                                 "call_id": call_id}, ts)
            ev("response_item", {"type": "function_call_output", "call_id": call_id,
                                 "output": tool.get("output") or ""}, ts + timedelta(seconds=1))
        if turn.get("text"):
            ev("response_item", {"type": "message", "id": f"msg_{_uid('codex', key).replace('-', '')}",
                                 "role": "assistant", "phase": "final_answer",
                                 "content": [{"type": "output_text", "text": turn["text"]}]}, ts + timedelta(seconds=2))
        if turn.get("usage"):
            u = _usage(turn)
            # Codex's input_tokens includes the cached part; cached_input_tokens is that subset
            ev("token_usage_record", {"response_id": f"resp_{_uid('codex', key, 'u').replace('-', '')}",
                                      "usage": {"input_tokens": u["input"] + u["cache_read"],
                                                "cached_input_tokens": u["cache_read"],
                                                "cache_write_input_tokens": u["cache_write"],
                                                "output_tokens": u["output"], "reasoning_output_tokens": 0}},
               ts + timedelta(seconds=2))
            tokens_used += u["input"] + u["cache_read"] + u["output"]
            if turn.get("text"):
                ev("event_msg", {"type": "task_complete", "last_agent_message": turn["text"]}, ts + timedelta(seconds=2))
    _jsonl(path, out)
    db.execute("INSERT OR REPLACE INTO threads(id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, "
               "tokens_used, archived, git_sha, git_branch, git_origin_url, cli_version, first_user_message, model, "
               "history_mode, created_at_ms, updated_at_ms, originator) VALUES (?,?,?,?,?,?,?,?,?,0,NULL,?,?,?,?,?,?,?,?,?)",
               (tid, str(path), int(start.timestamp()), int(last.timestamp()), "cli", provider, conv["cwd"],
                conv.get("title") or _first_user_text(conv)[:80], tokens_used, conv.get("branch"), conv.get("repo_url"),
                CODEX_VERSION, _first_user_text(conv), conv["model"], "paginated", _ms(start), _ms(last), "codex_cli_rs"))
    return path


# --- Opencode 1.x: one SQLite db (project, workspace, session, message, part) ------------------
# Parts are read in id order, messages in time_created order: ids carry a zero-padded counter.

OPENCODE_VERSION = "1.18.25"
_OPENCODE_SCHEMA = """
CREATE TABLE IF NOT EXISTS project(id text PRIMARY KEY, worktree text NOT NULL, vcs text, name text,
    time_created integer NOT NULL, time_updated integer NOT NULL, sandboxes text NOT NULL);
CREATE TABLE IF NOT EXISTS workspace(id text PRIMARY KEY, type text NOT NULL, name text, branch text,
    directory text, project_id text NOT NULL, time_used integer NOT NULL);
CREATE TABLE IF NOT EXISTS session(id text PRIMARY KEY, project_id text NOT NULL, workspace_id text,
    parent_id text, slug text NOT NULL, directory text NOT NULL, path text, title text NOT NULL,
    version text NOT NULL, share_url text, cost real DEFAULT 0 NOT NULL,
    tokens_input integer DEFAULT 0, tokens_output integer DEFAULT 0, revert text,
    agent text, model text, time_created integer NOT NULL, time_updated integer NOT NULL,
    time_archived integer);
CREATE TABLE IF NOT EXISTS message(id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL,
    time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE IF NOT EXISTS part(id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
    time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
"""


def opencode_session_id(conv) -> str:
    return "ses_" + _uid("opencode", conv["id"]).replace("-", "")[:26]


def write_opencode(conv, db: sqlite3.Connection) -> None:
    sid = opencode_session_id(conv)
    stem = sid[4:16]
    provider = _provider(conv)
    start = _start(conv)
    pid = "prj_" + _uid("opencode", "project", conv["cwd"]).replace("-", "")[:24]
    db.execute("INSERT OR IGNORE INTO project VALUES(?,?,?,?,?,?,?)",
               (pid, conv["cwd"], "git", Path(conv["cwd"]).name, _ms(start), _ms(start), "[]"))
    wid = None
    if conv.get("branch"):
        wid = "wrk_" + _uid("opencode", "workspace", conv["cwd"], conv["branch"]).replace("-", "")[:24]
        db.execute("INSERT OR IGNORE INTO workspace VALUES(?,?,?,?,?,?,?)",
                   (wid, "worktree", conv["branch"].rsplit("/", 1)[-1], conv["branch"], conv["cwd"], pid, _ms(start)))
    counters = {"msg": 0, "prt": 0}

    def nid(kind):
        counters[kind] += 1
        return f"{kind}_{stem}{counters[kind]:06d}"

    last = start
    for turn in conv["turns"]:
        ts = _at(conv, turn.get("at", 0))
        t0 = _ms(ts)
        mid = nid("msg")
        parts = []
        if turn["role"] == "user":
            data = {"role": "user", "time": {"created": t0}, "agent": "build",
                    "model": {"providerID": provider, "modelID": conv["model"]}}
            parts.append({"type": "text", "text": turn["text"], "time": {"start": t0, "end": t0}})
            done = t0
        else:
            u = _usage(turn)
            done = t0 + 3000
            data = {"role": "assistant", "mode": "build", "agent": "build", "parentID": None,
                    "modelID": conv["model"], "providerID": provider,
                    "path": {"cwd": conv["cwd"], "root": conv["cwd"]},
                    "tokens": {"total": u["input"] + u["output"] + u["cache_read"] + u["cache_write"],
                               "input": u["input"], "output": u["output"], "reasoning": 0,
                               "cache": {"read": u["cache_read"], "write": u["cache_write"]}},
                    "time": {"created": t0, "completed": done},
                    "finish": "tool-calls" if turn.get("tools") and not turn.get("text") else "stop"}
            parts.append({"type": "step-start"})
            if turn.get("thinking"):
                parts.append({"type": "reasoning", "text": turn["thinking"], "time": {"start": t0, "end": t0 + 500}})
            for i, tool in enumerate(turn.get("tools") or []):
                parts.append({"type": "tool", "tool": tool["name"],
                              "callID": f"call_{_uid('opencode', conv['id'], mid, i).replace('-', '')[:24]}",
                              "state": {"status": "completed", "input": tool.get("input") or {},
                                        "output": tool.get("output") or "", "title": tool["name"], "metadata": {},
                                        "time": {"start": t0 + 1000, "end": t0 + 2000}}})
            if turn.get("text"):
                parts.append({"type": "text", "text": turn["text"], "time": {"start": t0 + 2500, "end": done}})
            parts.append({"type": "step-finish", "reason": data["finish"], "tokens": data["tokens"]})
        last = max(last, datetime.fromtimestamp(done / 1000, tz=timezone.utc))
        db.execute("INSERT INTO message VALUES(?,?,?,?,?)", (mid, sid, t0, done, json.dumps(data)))
        for p in parts:
            db.execute("INSERT INTO part VALUES(?,?,?,?,?,?)", (nid("prt"), mid, sid, t0, done, json.dumps(p)))
    slug = "-".join(re.findall(r"[a-z0-9]+", (conv.get("title") or sid).lower())[:3]) or "session"
    db.execute("INSERT INTO session(id, project_id, workspace_id, parent_id, slug, directory, title, version, "
               "agent, model, time_created, time_updated) VALUES(?,?,?,NULL,?,?,?,?,?,?,?,?)",
               (sid, pid, wid, slug, conv["cwd"], conv.get("title") or _first_user_text(conv)[:80], OPENCODE_VERSION,
                "build", json.dumps({"id": conv["model"], "providerID": provider}), _ms(start), _ms(last)))


# --- pi: <agent dir>/sessions/--<cwd>--/<ts>_<uuid>.jsonl (format version 3) -------------------
# Header first, then a parentId chain. A tool-using turn is two assistant messages, as pi writes
# it: thinking + toolCall (carrying the usage), the toolResult messages, then the text reply.

def pi_session_id(conv) -> str:
    return _uid("pi", conv["id"])


def write_pi(conv, sessions: Path) -> Path:
    sid = pi_session_id(conv)
    start = _start(conv)
    enc = "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", conv["cwd"])) + "--"
    path = sessions / enc / f"{_iso(start).replace(':', '-').replace('.', '-')}_{sid}.jsonl"
    provider = _provider(conv)
    out = [{"type": "session", "version": 3, "id": sid, "timestamp": _iso(start), "cwd": conv["cwd"]}]
    parent, n = None, 0

    def entry(t, ts, **kw):
        nonlocal parent, n
        eid = _uid("pi", conv["id"], n).replace("-", "")[:8]
        n += 1
        out.append({"type": t, "id": eid, "parentId": parent, "timestamp": _iso(ts), **kw})
        parent = eid

    entry("model_change", start, provider=provider, modelId=conv["model"])
    if conv.get("title"):
        entry("session_info", start, name=conv["title"])

    def assistant(ts, content, usage=None, stop="stop", key=""):
        msg = {"role": "assistant", "content": content, "api": "anthropic-messages" if provider == "anthropic"
               else "openai-responses", "provider": provider, "model": conv["model"], "stopReason": stop,
               "timestamp": _ms(ts)}
        if usage is not None:
            msg["usage"] = {"input": usage["input"], "output": usage["output"], "cacheRead": usage["cache_read"],
                            "cacheWrite": usage["cache_write"],
                            "totalTokens": sum(usage.values())}   # no "cost": a zero would show as "reported $0"
            msg["responseId"] = f"resp_{_uid('pi', conv['id'], key).replace('-', '')[:24]}"
        entry("message", ts, message=msg)

    for ti, turn in enumerate(conv["turns"]):
        ts = _at(conv, turn.get("at", 0))
        if turn["role"] == "user":
            entry("message", ts, message={"role": "user", "content": [{"type": "text", "text": turn["text"]}],
                                          "timestamp": _ms(ts)})
            continue
        usage = _usage(turn) if turn.get("usage") else None
        tools = turn.get("tools") or []
        first = []
        if turn.get("thinking"):
            first.append({"type": "thinking", "thinking": turn["thinking"], "thinkingSignature": ""})
        ids = [f"call_{_uid('pi', conv['id'], ti, i).replace('-', '')[:24]}" for i in range(len(tools))]
        first += [{"type": "toolCall", "id": cid, "name": t["name"], "arguments": t.get("input") or {}}
                  for t, cid in zip(tools, ids, strict=True)]
        if tools:
            assistant(ts, first, usage, stop="toolUse", key=ti)
            for t, cid in zip(tools, ids, strict=True):
                entry("message", ts + timedelta(seconds=1), message={
                    "role": "toolResult", "toolCallId": cid, "toolName": t["name"], "isError": False,
                    "content": [{"type": "text", "text": t.get("output") or ""}], "timestamp": _ms(ts + timedelta(seconds=1))})
            if turn.get("text"):
                assistant(ts + timedelta(seconds=2), [{"type": "text", "text": turn["text"]}])
        else:
            assistant(ts, first + ([{"type": "text", "text": turn["text"]}] if turn.get("text") else []), usage, key=ti)
    _jsonl(path, out)
    return path


# --- Antigravity: <home>/brain/<id>/.system_generated/logs/transcript_full.jsonl --------------
# Records no model or token usage; title and workspace come from conversation_summaries.db.

def antigravity_id(conv) -> str:
    return _uid("antigravity", conv["id"])


def write_antigravity(conv, home: Path, db: sqlite3.Connection) -> Path:
    cid = antigravity_id(conv)
    path = home / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
    out = []

    def step(t, source, ts, **kw):
        out.append({"step_index": len(out), "source": source, "type": t, "status": "DONE",
                    "created_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), **kw})

    for turn in conv["turns"]:
        ts = _at(conv, turn.get("at", 0))
        if turn["role"] == "user":
            step("USER_INPUT", "USER_EXPLICIT", ts, content=f"<USER_REQUEST>\n{turn['text']}\n</USER_REQUEST>")
            continue
        tools = turn.get("tools") or []
        if tools or turn.get("thinking"):
            kw = {"thinking": turn["thinking"]} if turn.get("thinking") else {}
            if tools:
                kw["tool_calls"] = [{"name": t["name"], "args": t.get("input") or {}} for t in tools]
            if not tools and turn.get("text"):
                kw["content"] = turn["text"]
            step("PLANNER_RESPONSE", "MODEL", ts, **kw)
            for t in tools:
                step("GENERIC", "MODEL", ts + timedelta(seconds=1), content=t.get("output") or "")
            if tools and turn.get("text"):
                step("PLANNER_RESPONSE", "MODEL", ts + timedelta(seconds=2), content=turn["text"])
        elif turn.get("text"):
            step("PLANNER_RESPONSE", "MODEL", ts, content=turn["text"])
    _jsonl(path, out)
    if conv.get("title") or conv.get("cwd"):
        db.execute("INSERT OR REPLACE INTO conversation_summaries VALUES(?,?,?,?,?)",
                   (cid, conv.get("title") or "", json.dumps(["file://" + quote(conv["cwd"])]), "",
                    _ms(_at(conv, max((t.get("at", 0) for t in conv["turns"]), default=0)))))
    return path


# --- everything --------------------------------------------------------------------------------

def layout(root: Path) -> dict[str, Path]:
    """Where each source lives under `root` (a fake HOME, in each tool's default place)."""
    home = Path(root) / "home"
    return {"home": home,
            "claude_projects": home / ".claude" / "projects",
            "codex_home": home / ".codex",
            "opencode_db": home / ".local" / "share" / "opencode" / "opencode.db",
            "pi_agent_dir": home / ".pi" / "agent",
            "antigravity_home": home / ".gemini" / "antigravity-cli"}


def write_all(convs: list[dict], root: Path) -> dict[str, str]:
    """Write every conversation under `root` in its source's native layout. Returns the env vars
    a `convo-index index` subprocess needs to read exactly these: merge them over os.environ.
    PI_CODING_AGENT_SESSION_DIR is returned empty, which pi's adapter treats as unset, so a
    value in the caller's own environment cannot redirect the run."""
    for c in convs:
        _check(c)
    ids = [c["id"] for c in convs]
    if len(set(ids)) != len(ids):
        raise ValueError("conversation ids must be unique")
    p = layout(root)
    p["home"].mkdir(parents=True, exist_ok=True)
    by = {s: [c for c in convs if c["source"] == s] for s in SOURCES}

    p["claude_projects"].mkdir(parents=True, exist_ok=True)
    for c in by["claude_code"]:
        write_claude_code(c, p["home"])

    p["codex_home"].mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p["codex_home"] / "state_5.sqlite")
    try:
        db.execute(_CODEX_THREADS)
        for c in by["codex"]:
            write_codex(c, p["codex_home"], db)
        db.commit()
    finally:
        db.close()

    p["opencode_db"].parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p["opencode_db"])
    try:
        db.executescript(_OPENCODE_SCHEMA)
        for c in by["opencode"]:
            write_opencode(c, db)
        db.commit()
    finally:
        db.close()

    (p["pi_agent_dir"] / "sessions").mkdir(parents=True, exist_ok=True)
    for c in by["pi"]:
        write_pi(c, p["pi_agent_dir"] / "sessions")

    (p["antigravity_home"] / "brain").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p["antigravity_home"] / "conversation_summaries.db")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS conversation_summaries(conversation_id text, title text NOT NULL DEFAULT '', "
                   "workspace_uris text NOT NULL, parent_conversation_id text NOT NULL DEFAULT '', "
                   "last_modified_time integer, PRIMARY KEY(conversation_id))")
        for c in by["antigravity"]:
            write_antigravity(c, p["antigravity_home"], db)
        db.commit()
    finally:
        db.close()

    return {"HOME": str(p["home"]),
            "CODEX_HOME": str(p["codex_home"]),
            "OPENCODE_DB": str(p["opencode_db"]),
            "PI_CODING_AGENT_DIR": str(p["pi_agent_dir"]),
            "PI_CODING_AGENT_SESSION_DIR": "",
            "ANTIGRAVITY_HOME": str(p["antigravity_home"])}
