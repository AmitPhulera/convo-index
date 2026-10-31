import errno
import ipaddress
import os
import json
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, urlsplit, parse_qs, unquote

from convo_index.config import STATIC_DIR
from convo_index.sources.base import (resume_command, supports_turns, turn_command, turn_events, last_activity,
                                      turn_launcher)
from convo_index.indexer import EXIT_LOCKED, cwd_excluded
from convo_index import turns as TURNS
from convo_index.lock import index_lock, IndexLocked
from convo_index.providers import registry as R
from convo_index import db as dbm
from convo_index import search as S
from convo_index import analytics as A
from convo_index import settings as CFG
from convo_index import turn_tools as TT
from convo_index.sources import claude_code as CC
from convo_index import tags as T
from convo_index import references as REF
from convo_index import cost as K
from convo_index import handoff as HO
from convo_index import vectors as V

DB_PATH = None   # tests override; otherwise resolved from settings at request time


def _db():
    return DB_PATH or CFG.load().db_path()


def semantic_status():
    """Whether query embeddings would land in the same space as the stored vectors.
    Returns (ok, reason)."""
    emb = R.make_embedder()
    if emb is None:
        return False, R.embedder_blocked_reason() or "no embedder configured"
    conn = dbm.connect(_db())
    try:
        stored = dbm.get_meta(conn, "embedding_fingerprint") if dbm._table_exists(conn, "index_meta") else None
        dim = dbm.vector_dim(conn)
    finally:
        conn.close()
    if stored is not None and stored != emb.fingerprint:
        return False, "index built with a different embedder; run `convo-index reembed`"
    if dim and int(CFG.load()["embeddings"].get("dim") or dim) != dim:
        return False, f"index vectors are {dim}-dimensional, config says {CFG.load()['embeddings'].get('dim')}"
    return True, None


def embed(texts):
    """Query-time embedding through the configured embedder; None per text when embeddings
    are unavailable or would not match the indexed vector space."""
    ok, _ = semantic_status()
    if not ok:
        return [None] * len(texts)
    emb = R.make_embedder()
    return emb.embed(texts, purpose="query").vectors if emb else [None] * len(texts)


def handle_search(qs):
    q = (qs.get("q", [""])[0]).strip()
    mode = qs.get("mode", ["hybrid"])[0]
    category = qs.get("category", [None])[0] or None
    source = [s for s in qs.get("source", [""])[0].split(",") if s] or None   # comma-separated: any of them
    has_ticket = qs.get("ticket", ["0"])[0] == "1"
    include_hidden = qs.get("hidden", ["0"])[0] == "1"
    include_children = qs.get("children", ["0"])[0] == "1"
    project = qs.get("project", [None])[0] or None
    tag_filters = qs.get("tag", [])
    query_vec = None
    if mode in ("semantic", "hybrid") and q:
        query_vec = embed([q])[0]
    conn = dbm.connect(_db())
    try:
        results = S.search(conn, q, query_vec, category=category, has_ticket=has_ticket,
                           mode=mode, include_hidden=include_hidden, project=project,
                           source=source, include_children=include_children, tags=tag_filters)
    finally:
        conn.close()
    return json.dumps(results)


def handle_sources():
    conn = dbm.connect(_db())
    try:
        return json.dumps(dbm.list_sources(conn))
    finally:
        conn.close()


def handle_config():
    """What the UI needs from settings: category labels, ticket link template."""
    cfg = CFG.load()
    ok, reason = semantic_status()
    conn = dbm.connect(_db())
    try:
        groups = T.load_groups(conn) if dbm._table_exists(conn, "tag_groups") else {}
    finally:
        conn.close()
    return json.dumps({"category_labels": cfg["category"]["labels"],
                       "tag_groups": groups,
                       "ticket_url": cfg["references"]["ticket_url"],
                       "config_path": str(cfg.path) if cfg.path else None,
                       "windows": os.name == "nt",
                       "semantic_search": ok, "semantic_reason": reason})


def handle_suggestions():
    conn = dbm.connect(_db())
    try:
        return json.dumps(V.open_suggestions(conn) if dbm._table_exists(conn, "tag_evidence") else [])
    finally:
        conn.close()


def handle_related(key):
    conn = dbm.connect(_db())
    try:
        return json.dumps(REF.timeline(conn, key))
    finally:
        conn.close()


def handle_reference(conversation_id, body):
    """POST /api/reference/<id> {kind, key, linkable}: mark a false (or real) match."""
    if not isinstance(body, dict):
        raise LookupError("body must be a JSON object")
    kind, key = body.get("kind"), body.get("key")
    if not isinstance(kind, str) or kind not in ("ticket", "pr", "issue", "branch", "commit", "url"):
        raise LookupError("kind must be one of ticket, pr, issue, branch, commit, url")
    if not isinstance(key, str) or not key:
        raise LookupError("key is required")
    linkable = body.get("linkable", False)
    if not isinstance(linkable, bool):
        raise LookupError("linkable must be a JSON boolean")
    conn = dbm.connect(_db())
    try:
        conversation_id = dbm.resolve_id(conn, conversation_id)
        if not dbm.exists(conn, conversation_id):
            return None
        if not REF.set_linkable(conn, conversation_id, kind, key, linkable):
            return None   # no such reference on this conversation
        return json.dumps({"ok": True, "id": conversation_id, "references": REF.references_for(conn, conversation_id),
                           "related": REF.related_for(conn, conversation_id)})
    finally:
        conn.close()


def handle_tag(conversation_id, body):
    """POST /api/tag/<id> {group, tag|null, decision: accept|reject|clear}."""
    if not isinstance(body, dict):
        raise LookupError("body must be a JSON object")
    group, tag, decision = body.get("group"), body.get("tag"), body.get("decision", "accept")
    if not isinstance(group, str) or not group:
        raise LookupError("group is required")
    if tag is not None and not isinstance(tag, str):
        raise LookupError("tag must be a string or null")
    if decision not in ("accept", "reject", "clear"):
        raise LookupError("decision must be accept, reject or clear")
    conn = dbm.connect(_db())
    try:
        conversation_id = dbm.resolve_id(conn, conversation_id)
        if not dbm.exists(conn, conversation_id):
            return None
        if decision == "clear":
            T.clear_decision(conn, conversation_id, group, tag)   # tag None = whole group
        else:
            T.set_decision(conn, conversation_id, group, tag, decision)
        return json.dumps({"ok": True, "id": conversation_id, **T.detail_for(conn, conversation_id)})
    finally:
        conn.close()


def handle_hide(conversation_id, hidden):
    conn = dbm.connect(_db())
    try:
        conversation_id = dbm.resolve_id(conn, conversation_id)
        dbm.set_hidden(conn, conversation_id, hidden)
    finally:
        conn.close()
    return json.dumps({"ok": True, "id": conversation_id, "hidden": hidden})


def handle_delete(conversation_id):
    """Remove the archive copy, then the rows, under the index lock so a refresh cannot write a
    new copy in between. If the copy cannot be removed the conversation is kept and the
    request fails: a plaintext copy is never left behind by a deletion that reported success."""
    db = _db()
    try:
        with index_lock(db):
            conn = dbm.connect(db)
            try:
                conversation_id = dbm.resolve_id(conn, conversation_id)
                row = conn.execute("SELECT archive_path FROM conversations WHERE id=?", (conversation_id,)).fetchone()
                if row and row[0]:
                    try:
                        Path(row[0]).unlink(missing_ok=True)
                    except OSError as e:
                        raise RuntimeError(f"archive copy not removed, conversation kept: {e}") from e
                try:
                    HO.remove_copies(db, conversation_id)
                except OSError as e:
                    raise RuntimeError(f"handoff transcript not removed, conversation kept: {e}") from e
                dbm.delete_conversation(conn, conversation_id)
            finally:
                conn.close()
    except IndexLocked:
        raise RuntimeError("a refresh is running; retry when it has finished") from None
    return json.dumps({"ok": True, "id": conversation_id})


def handle_conversation(conversation_id):
    conn = dbm.connect(_db())
    try:
        conversation_id = dbm.resolve_id(conn, conversation_id)
        head = conn.execute(
            """SELECT title, category, tickets, cwd, git_branch, start_ts, archive_path,
                      source, native_id, parent_id, relation, fidelity, models, warnings, metadata
               FROM conversations WHERE id=?""", (conversation_id,)).fetchone()
        if not head:
            return None
        msgs = conn.execute(
            "SELECT role, kind, ts, text FROM messages WHERE conversation_id=? ORDER BY seq",
            (conversation_id,)).fetchall()
        children = [r[0] for r in conn.execute(
            "SELECT id FROM conversations WHERE parent_id=? ORDER BY start_ts", (conversation_id,)).fetchall()]
        hidden = conversation_id in dbm.get_hidden_ids(conn)
        summary = dbm.get_summary(conn, conversation_id)
        tag_detail = T.detail_for(conn, conversation_id) if dbm._table_exists(conn, "tag_groups") else {}
        refs = REF.references_for(conn, conversation_id) if dbm._table_exists(conn, "references_") else []
        related = REF.related_for(conn, conversation_id) if dbm._table_exists(conn, "references_") else []
        usage = K.usage_for_conversation(conn, conversation_id, K.load(CFG.load())) if dbm._table_exists(conn, "usage_records") else {}
    finally:
        conn.close()
    return json.dumps({
        "id": conversation_id, "title": head[0], "category": head[1],
        "tickets": json.loads(head[2] or "[]"), "cwd": head[3], "git_branch": head[4],
        "start_ts": head[5], "archive_path": head[6], "source": head[7], "native_id": head[8],
        "parent_id": head[9], "relation": head[10], "fidelity": head[11] or "full",
        "models": json.loads(head[12] or "[]"), "warnings": json.loads(head[13] or "[]"),
        "metadata": json.loads(head[14] or "{}"), "children": children,
        "resume_cmd": resume_command(conversation_id, head[3]),
        "turns_supported": (bool(CFG.load()["ui"]["turns"].get("enabled", True)) and supports_turns(conversation_id)
                            and turn_launcher(conversation_id) is not None),
        "active_turn": (lambda t: t.snapshot() if t else None)(_runner().active(conversation_id)),
        "edit_mcp_tools": len(TT.load()) if head[7] == "claude_code" else 0,   # how many the Tools button shows
        "hidden": hidden, "summary": summary, "tags": tag_detail, "usage": usage,
        "references": refs, "related": related,
        "messages": [{"role": m[0], "kind": m[1] or "text", "ts": m[2], "text": m[3]} for m in msgs],
    })


def handle_analytics():
    return json.dumps(A.analytics(_db(), CFG.load()))



_refresh_lock = threading.Lock()


def _run_indexer(derived=True):
    """Run a refresh in a subprocess. Returns {"ok", "busy", "totals"}: busy when another
    indexer (this server, the scheduler or the CLI) already holds the lock, ok only when
    every source and conversation indexed cleanly. `derived=False` ingests only, leaving
    summaries and vectors to the next full run."""
    if not _refresh_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "totals": None}
    try:
        res = subprocess.run([sys.executable, "-m", "convo_index.indexer"] + ([] if derived else ["--no-jobs"]),
                             capture_output=True, text=True, check=False)
        if res.returncode == EXIT_LOCKED:
            return {"ok": False, "busy": True, "totals": None}
        totals = None
        for line in reversed(res.stdout.strip().splitlines()):
            try:
                totals = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        return {"ok": res.returncode == 0, "busy": False, "totals": totals,
                "error": None if res.returncode == 0 else (res.stderr or "")[-2000:]}
    finally:
        _refresh_lock.release()


# --- Browser turns -------------------------------------------------------------------------------

def _refresh_after_turn():
    """The refresh a turn waits for before its terminal event: ingest only, so `done` never
    waits for a queue of LLM summaries (the next scheduled or manual refresh runs those)."""
    return _run_indexer(derived=False)


TURN_TOKEN_HEADER = "X-Convo-Index-Turn"
_turn_token = None          # per server process; see turn_token()
RUNNER = None               # one TurnRunner per server process, built from settings on first use
_runner_lock = threading.Lock()
_stopping = False           # set by stop_turns(): a runner created afterwards is born closed
RECENT_ACTIVITY = 30        # seconds: a session that just changed is probably open in a terminal


def _runner():
    global RUNNER
    with _runner_lock:      # concurrent first requests must share one runner, or its limits mean nothing
        if RUNNER is None:
            t = CFG.load()["ui"]["turns"]
            RUNNER = TURNS.TurnRunner(timeout=int(t.get("timeout_seconds") or 1200),
                                      max_concurrent=int(t.get("max_concurrent") or 2), refresh=_refresh_after_turn)
            RUNNER.closed = _stopping
        return RUNNER


def stop_turns():
    """End every running harness and accept no more; the server calls this on its way out so
    none outlives it. A request that is past its preconditions when this runs is refused by
    the runner, which closes under its own lock before it picks what to stop."""
    global _stopping
    with _runner_lock:
        _stopping = True
        runner = RUNNER
    return runner.shutdown() if runner is not None else 0


def turn_token():
    """The secret a turn request must carry. It lives in a file only this user can read, next
    to the database, and is kept across restarts (see adopt_turn_token): a tab that already
    holds it, and `up` reading the file while a restarted server binds, stay valid. `serve` on
    a terminal and `up` put it in the fragment of the URL they print and open, so a page
    reached through that URL has it and nothing served over HTTP ever does. That is what keeps
    a reverse proxy on this machine out: it can forward requests, but not read the file or the
    terminal."""
    global _turn_token
    with _runner_lock:
        if _turn_token is None:
            _turn_token = secrets.token_urlsafe(24)
        return _turn_token


def turn_token_path(settings=None):
    return (settings or CFG.load()).db_path().with_name("turn-token")


TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")


def adopt_turn_token(settings=None):
    """Take over the token the previous server wrote, when the file is well-formed and (on
    POSIX) private to this user; otherwise turn_token() makes a new one. Returns the token
    adopted, or None."""
    global _turn_token
    path = turn_token_path(settings)
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            return None                   # readable by others: the old secret is not one any more
    except OSError:
        return None
    saved = read_turn_token(settings)
    if not saved or not TOKEN_RE.fullmatch(saved):
        return None
    with _runner_lock:
        _turn_token = saved
    return saved


def write_turn_token(settings=None):
    path = turn_token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(turn_token())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def read_turn_token(settings=None):
    """The running server's token, for a CLI that prints or opens the UI; None when no server
    has written one."""
    try:
        return turn_token_path(settings).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def token_refusal(headers, qs=None):
    """Why the request's turn token is not acceptable, or None. POST and DELETE send it in a
    header; the event stream may send it as ?token= because EventSource cannot set headers."""
    given = (headers or {}).get(TURN_TOKEN_HEADER) or ((qs or {}).get("token") or [None])[0]
    if not given or not secrets.compare_digest(given, turn_token()):
        return "missing or wrong turn token; open the UI through the URL that `convo-index serve` or `up` prints"
    return None


def _loopback(address):
    try:
        ip = ipaddress.ip_address((address or "").split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)        # ::ffff:127.0.0.1 on a dual-stack socket
    return ip.is_loopback or (mapped is not None and mapped.is_loopback)


def turns_refusal(server_address, client_address, headers=None):
    """Why this request may not run a turn, or None. A turn runs a coding agent with the
    user's credentials, so beyond the Host/Origin/CSRF checks (which say a page this server
    served sent it) the request must have come over loopback at all: the socket is bound to a
    loopback address, the peer is one, and the browser addressed the server by a loopback name.
    A same-host reverse proxy connects from 127.0.0.1, so it is kept out by what it leaves in
    the request: the name it serves in Host or Origin, or the forwarding headers proxies add
    (the CSRF header is no secret). A proxy that rewrites Host to the upstream and adds no
    forwarding header is indistinguishable from a local browser; the README says such a
    deployment must keep the port unreachable or switch turns off."""
    if not _loopback(server_address[0]):
        return f"browser turns run only when the server listens on a loopback address (it is bound to {server_address[0]})"
    if not _loopback(client_address[0]):
        return f"browser turns are accepted only from this machine (request from {client_address[0]})"
    for name in PROXY_HEADERS:
        if (headers or {}).get(name) is not None:
            return f"browser turns are not accepted through a proxy ({name} header present)"
    for name in ("Host", "Origin"):
        value = (headers or {}).get(name)
        if value is None:
            continue
        key = host_key(urlsplit(value).netloc if name == "Origin" else value, 80)
        if not key or key[0] not in LOOPBACK_HOSTS:
            return f"browser turns are accepted only when the server is addressed as localhost ({name}: {value})"
    return None


class BodyTooLarge(ValueError):
    """A request body outside the accepted size."""


def shutdown_refusal(client_address, headers=None):
    """Why this request may not stop the server, or None. Beyond the Host/Origin/CSRF checks it
    needs a loopback peer, no proxy header and the turn token (from the terminal or the 0600
    file). Unlike a turn it does not need a loopback listener: a server bound to 0.0.0.0 can
    still be stopped from this machine, and the peer check keeps everyone else out."""
    if not _loopback(client_address[0]):
        return f"the server can be stopped only from this machine (request from {client_address[0]})"
    for name in PROXY_HEADERS:
        if (headers or {}).get(name) is not None:
            return f"the server cannot be stopped through a proxy ({name} header present)"
    return token_refusal(headers)


def request_shutdown(srv):
    """End running turns, then leave serve_forever (main() returns 0 after it, so a login
    service that restarts only failed exits leaves the server stopped). Runs on its own
    thread: shutdown() from a request thread would wait for itself."""
    def _stop():
        stop_turns()
        srv.shutdown()
    threading.Thread(target=_stop, daemon=True).start()


PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Via")
MAX_TURN_BODY = 1024 * 1024
PROMPT_FORBIDDEN = {chr(c) for c in range(32)} - {"\n", "\r", "\t"}


def turn_preconditions(conversation_id, prompt, edits, cfg=None):
    """Everything that must hold before a harness is spawned. Returns (conversation_id, command,
    locator);
    raises TurnRefused with the status and the message the UI shows verbatim."""
    cfg = cfg or CFG.load()
    if not cfg["ui"]["turns"].get("enabled", True):
        raise TURNS.TurnRefused(403, "browser turns are disabled in settings ([ui.turns] enabled = false)")
    if not isinstance(prompt, str) or not prompt.strip():
        raise TURNS.TurnRefused(400, "prompt is required")
    if any(c in PROMPT_FORBIDDEN for c in prompt):
        raise TURNS.TurnRefused(400, "prompt contains control characters")
    try:
        prompt.encode("utf-8")
    except UnicodeEncodeError:        # a lone surrogate: JSON carries it, no pipe or argv can
        raise TURNS.TurnRefused(400, "prompt is not valid Unicode text") from None
    if not isinstance(edits, bool):
        raise TURNS.TurnRefused(400, "edits must be a JSON boolean")
    conn = dbm.connect(_db())
    try:
        conversation_id = dbm.resolve_id(conn, conversation_id)
        row = conn.execute("SELECT source, native_id, cwd, source_ref, metadata FROM conversations WHERE id=?",
                           (conversation_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise TURNS.TurnRefused(404, "conversation not found")
    source, cwd, locator, metadata = row[0], row[2], row[3], json.loads(row[4] or "{}")
    spec = cfg["sources"].get(source) or {}
    if spec.get("enabled", True) is False:
        raise TURNS.TurnRefused(403, f"source {source} is disabled in settings")
    if cwd_excluded(cwd, spec.get("exclude_cwd") or []):
        raise TURNS.TurnRefused(403, f"this project is excluded by [sources.{source}] exclude_cwd")
    if not supports_turns(conversation_id):
        raise TURNS.TurnRefused(400, f"no browser turns for {source} conversations on this platform")
    if not cwd or not os.path.isdir(cwd):
        raise TURNS.TurnRefused(412, f"the conversation's directory no longer exists: {cwd or '(none recorded)'}")
    if metadata.get("cwd_source") not in (None, "summaries"):
        raise TURNS.TurnRefused(412, "the conversation's directory was inferred, not recorded; continue it in the terminal")
    changed = last_activity(conversation_id, locator)
    own = _runner().last_own_activity(conversation_id)  # where the previous browser turn left the transcript
    if changed is not None and 0 <= time.time() - changed < RECENT_ACTIVITY and not (own is not None and changed <= own):
        raise TURNS.TurnRefused(423, f"the session changed {int(time.time() - changed)} s ago; "
                                     "if it is open in a terminal, continue it there")
    try:
        command = turn_command(conversation_id, prompt, cwd, edits=edits,
                               mcp_tools=tuple(TT.load(cfg)) if edits else (), locator=locator)
    except ValueError as e:
        raise TURNS.TurnRefused(412, str(e)) from None
    if command is None:
        raise TURNS.TurnRefused(424, f"the {source} command is not installed on this machine (not on PATH)")
    return conversation_id, command, locator


def handle_turn_start(conversation_id, body):
    """POST /api/conversation/<id>/turn {prompt, edits}: (status, json)."""
    if not isinstance(body, dict):
        return 400, json.dumps({"error": "body must be a JSON object"})
    try:
        conversation_id, command, locator = turn_preconditions(conversation_id, body.get("prompt"), body.get("edits", False))
        turn = _runner().start(conversation_id, command, lambda line: turn_events(conversation_id, line),
                               edits=bool(body.get("edits", False)),
                               activity=lambda: last_activity(conversation_id, locator))
    except TURNS.TurnRefused as e:
        return e.status, json.dumps({"error": str(e)})
    return 200, json.dumps(turn.snapshot())


MCP_CALL_RE = re.compile(r"\[(mcp__[A-Za-z0-9_-]+)\]")


def seen_mcp_tools(servers):
    """{server: [tool, ...]} of the configured servers' tools that indexed conversations called,
    as suggestions for the allowlist (the harness lists no tools without starting the servers)."""
    if not servers:
        return {}
    seen = {}
    conn = dbm.connect(_db())
    try:
        rows = conn.execute(r"""SELECT DISTINCT substr(text, 1, 200) FROM messages
                                WHERE kind = 'tool_call' AND text LIKE '[mcp\_\_%' ESCAPE '\'""").fetchall()
    finally:
        conn.close()
    for (text,) in rows:
        m = MCP_CALL_RE.match(text or "")
        part = TT.split(m.group(1), servers) if m else None
        if part and part[1] != "*" and part[1] not in seen.setdefault(part[0], []):
            seen[part[0]].append(part[1])
    return {k: sorted(v) for k, v in seen.items()}


def handle_turn_tools():
    """GET /api/turns/tools: the user's MCP servers (names and transport only; their URLs and
    headers never leave this machine's config), the allowlist, and tools seen in transcripts."""
    servers = CC.mcp_servers()
    return 200, json.dumps({"servers": [{"name": k, "type": str(v.get("type") or "stdio")} for k, v in sorted(servers.items())],
                            "allowed": TT.load(), "seen": seen_mcp_tools(servers)})


def handle_turn_tools_save(body):
    """POST /api/turns/tools {mcp_tools: [...]}: validate against the configured servers and store."""
    if not isinstance(body, dict):
        return 400, json.dumps({"error": "body must be a JSON object"})
    try:
        saved = TT.save(body.get("mcp_tools"), CC.mcp_servers())
    except ValueError as e:
        return 400, json.dumps({"error": str(e)})
    return 200, json.dumps({"ok": True, "allowed": saved})


def handle_turn_stop(turn_id):
    turn = _runner().get(turn_id)
    if turn is None:
        return 404, json.dumps({"error": "no such turn"})
    stopped = bool(_runner().stop(turn_id))
    return 200, json.dumps({"ok": True, "stopped": stopped, **turn.snapshot()})


# Browser-facing guard (any web page can reach a localhost port): every request must name this
# server in Host, which defeats DNS rebinding, and every POST/DELETE must carry CSRF_HEADER, which a
# cross-origin page cannot send without a CORS preflight that this server never approves.
CSRF_HEADER = "X-Convo-Index"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def host_key(value, default_port):
    """`(hostname, port)` for a Host header or an Origin's netloc, or None when it is not one.
    "box.lan", "Box.lan:9753", "localhost.", "fe80::1" and "[fe80::1]:80" all normalise, so
    the allow-list compares names and ports instead of enumerating header spellings."""
    value = (value or "").strip().lower()
    if value.count(":") > 1 and not value.startswith("["):   # bare IPv6 literal
        value = f"[{value}]"
    try:
        parts = urlsplit("//" + value)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or parts.netloc != value or "@" in value:   # a path, query or userinfo is not a host
        return None
    return host.rstrip("."), port or default_port


def allowed_hosts(bind_host, port, extra=()):
    """`(hostname, port)` pairs this server answers to: the loopback names, the bound host (when
    it is not a wildcard) and `[server].allowed_hosts`. A name without a port is `(name, None)`
    and matches any port: behind a TLS proxy the browser sends `Host: box.lan` (port 80) with
    `Origin: https://box.lan` (port 443), and a port forward presents yet another. This loosens
    nothing against DNS rebinding, which can only ever present the attacker's own hostname."""
    names = list(LOOPBACK_HOSTS)
    if bind_host and bind_host not in ("0.0.0.0", "::", ""):
        names.append(bind_host)
    names += [extra] if isinstance(extra, str) else list(extra or ())
    return {key for key in (host_key(n, None) for n in names) if key}


def _permitted(key, allowed):
    return key is not None and (key in allowed or (key[0], None) in allowed or key[0] in LOOPBACK_HOSTS)


def request_error(method, headers, allowed):
    """Why this request must be refused, or None. `headers` is the request's header mapping."""
    if not _permitted(host_key(headers.get("Host"), 80), allowed):
        return "unexpected Host header"
    if method in ("POST", "DELETE"):
        origin = headers.get("Origin")
        if origin is not None:
            o = urlsplit(origin)
            if not _permitted(host_key(o.netloc, 443 if o.scheme == "https" else 80), allowed):
                return "cross-origin request"
        if headers.get(CSRF_HEADER) != "1":
            return f"missing {CSRF_HEADER} header"
    return None


def _segment(path, prefix):
    """Path segment after `prefix`, URL-decoded (ids contain ':' which clients encode)."""
    return unquote(path[len(prefix):])


class Handler(BaseHTTPRequestHandler):
    def _refused(self):
        err = request_error(self.command, self.headers, self.server.allowed)
        if err:
            self._send(403, json.dumps({"error": err}))
        return err is not None

    def _send(self, code, body, ctype="application/json", headers=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, relpath, ctype):
        fp = STATIC_DIR / relpath
        if not fp.exists() or STATIC_DIR not in fp.resolve().parents:
            return self._send(404, "not found", "text/plain")
        # The UI files ship with the package and change when it is upgraded; revalidating
        # keeps a stale cached page from talking to a newer API (and makes edits show up).
        self._send(200, fp.read_bytes(), ctype, headers=(("Cache-Control", "no-cache"),))

    def do_GET(self):
        if self._refused():
            return
        try:
            self._route_get()
        except dbm.AmbiguousId as e:
            self._send(409, json.dumps({"error": "ambiguous id; use a namespaced id", "candidates": e.args[0]}))
        except Exception as e:  # never let a handler error kill the response
            self._send(500, json.dumps({"error": str(e)}))

    def _route_get(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if path == "/":
            return self._send_file("index.html", "text/html")
        if path == "/analytics":
            return self._send_file("analytics.html", "text/html")
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            ctype = ("text/css" if name.endswith(".css")
                     else "application/javascript" if name.endswith(".js")
                     else "text/plain")
            return self._send_file(name, ctype)
        if path == "/api/search":
            return self._send(200, handle_search(qs))
        if path == "/api/sources":
            return self._send(200, handle_sources())
        if path == "/api/config":
            return self._send(200, handle_config())
        if path == "/api/analytics":
            return self._send(200, handle_analytics())
        if path == "/api/suggestions":
            return self._send(200, handle_suggestions())
        if path.startswith("/api/related/"):
            return self._send(200, handle_related(_segment(path, "/api/related/")))
        if path.startswith("/api/turn/"):
            return self._stream_turn(_segment(path, "/api/turn/"), qs)
        if path == "/api/turns/tools":       # what Edit-mode turns may run: guarded like a turn
            if self._turns_refused():
                return
            return self._send(*handle_turn_tools())
        if path.startswith("/api/conversation/"):
            body = handle_conversation(_segment(path, "/api/conversation/"))
            return self._send(404, "not found", "text/plain") if body is None else self._send(200, body)
        return self._send(404, "not found", "text/plain")

    def _turns_refused(self, qs=None):
        err = (turns_refusal(self.server.server_address, self.client_address, self.headers)
               or token_refusal(self.headers, qs))
        if err:
            self._send(403, json.dumps({"error": err}))
        return err is not None

    def _stream_turn(self, turn_id, qs):
        """GET /api/turn/<id>?after=<n>: the turn's events as server-sent events from `after`,
        following it live until its terminal event. Closing the connection detaches the
        stream; the turn keeps running."""
        if self._turns_refused(qs):
            return
        if _runner().get(turn_id) is None:
            return self._send(404, json.dumps({"error": "no such turn"}))
        try:   # a reconnecting EventSource sends the last id it saw, which is the count of events seen
            after = max(0, int(self.headers.get("Last-Event-ID") or qs.get("after", ["0"])[0]))
        except ValueError:
            return self._send(400, json.dumps({"error": "after must be an integer"}))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for idx, ev in _runner().events(turn_id, after):
                if ev is None:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    self.wfile.write(f"id: {idx + 1}\ndata: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return   # the client went away; the turn does not

    def _json_body(self, limit=MAX_TURN_BODY):
        """The request body as JSON. Raises BodyTooLarge for a negative or oversized length and
        json.JSONDecodeError for a body (or a Content-Length) that is not what it says."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise json.JSONDecodeError("Content-Length is not a number", "", 0) from None
        if not 0 <= length <= limit:
            raise BodyTooLarge(f"body must be between 0 and {limit} bytes")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_DELETE(self):
        if self._refused():
            return
        try:
            path = urlparse(self.path).path
            if path.startswith("/api/turn/"):
                if self._turns_refused():
                    return
                return self._send(*handle_turn_stop(_segment(path, "/api/turn/")))
            return self._send(404, "not found", "text/plain")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))

    def do_POST(self):
        if self._refused():
            return
        try:
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            if path == "/api/refresh":
                return self._send(200, json.dumps(_run_indexer()))
            if path == "/api/shutdown":
                err = shutdown_refusal(self.client_address, self.headers)
                if err:
                    return self._send(403, json.dumps({"error": err}))
                self._send(202, json.dumps({"ok": True, "stopping": True}))
                return request_shutdown(self.server)
            if path == "/api/turns/tools":
                if self._turns_refused():
                    return
                try:
                    body = self._json_body()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return self._send(400, json.dumps({"error": "invalid json"}))
                except BodyTooLarge as e:
                    return self._send(413, json.dumps({"error": str(e)}))
                return self._send(*handle_turn_tools_save(body))
            if path.startswith("/api/conversation/") and path.endswith("/turn"):
                if self._turns_refused():
                    return
                try:
                    body = self._json_body()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return self._send(400, json.dumps({"error": "invalid json"}))
                except BodyTooLarge as e:
                    return self._send(413, json.dumps({"error": str(e)}))
                return self._send(*handle_turn_start(_segment(path[:-len("/turn")], "/api/conversation/"), body))
            if path.startswith("/api/hide/"):
                hidden = qs.get("hidden", ["1"])[0] == "1"
                return self._send(200, handle_hide(_segment(path, "/api/hide/"), hidden))
            if path.startswith("/api/delete/"):
                return self._send(200, handle_delete(_segment(path, "/api/delete/")))
            if path.startswith("/api/reference/"):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, json.dumps({"error": "invalid json"}))
                try:
                    out = handle_reference(_segment(path, "/api/reference/"), body)
                except LookupError as e:
                    return self._send(400, json.dumps({"error": str(e)}))
                return self._send(404, "not found", "text/plain") if out is None else self._send(200, out)
            if path.startswith("/api/tag/"):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, json.dumps({"error": "invalid json"}))
                try:
                    out = handle_tag(_segment(path, "/api/tag/"), body)
                except LookupError as e:
                    return self._send(400, json.dumps({"error": str(e)}))
                return self._send(404, "not found", "text/plain") if out is None else self._send(200, out)
            return self._send(404, "not found", "text/plain")
        except dbm.AmbiguousId as e:
            self._send(409, json.dumps({"error": "ambiguous id; use a namespaced id", "candidates": e.args[0]}))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))

    def log_message(self, *a):
        pass


def prepare(cfg=None):
    """Apply the schema (incl. migrations) before serving, so an older convos.db is migrated
    on startup without waiting for a reindex. The resolved path goes to init_schema so the
    migration runs under the cross-process lock and after a backup, with the configured
    vector dimension. Returns the database path."""
    cfg = cfg or CFG.load()
    path = Path(_db())
    conn = dbm.connect(path)
    try:
        dbm.init_schema(conn, path, dim=cfg["embeddings"].get("dim"))
        if dbm._table_exists(conn, "tag_groups"):
            T.sync_definitions(conn, cfg)   # groups, tags and the one-time category conversion exist before the first request
    finally:
        conn.close()
    return path


class _Server4(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is in use; a busy port
    # must fail to bind so the second serve steps aside instead of silently sharing it.
    allow_reuse_address = os.name != "nt"


class _Server6(_Server4):
    address_family = socket.AF_INET6


def make_server(host, port, extra_hosts=()):
    """Bind the UI server. Port 0 picks a free port; the Host allow-list uses the real one."""
    srv = (_Server6 if ":" in host else _Server4)((host, port), Handler)
    srv.allowed = frozenset(allowed_hosts(host, srv.server_address[1], extra_hosts))
    return srv


def advertised_url(bind_host, port, token=None):
    """The URL to open: a wildcard bind is reached through loopback (the allow-list never
    carries "0.0.0.0"), an IPv6 literal needs brackets, and the turn token travels in the
    fragment, which the browser keeps to itself (it is never sent to the server or a proxy)."""
    if bind_host in ("0.0.0.0", "::", ""):
        bind_host = "127.0.0.1"
    elif ":" in bind_host:
        bind_host = f"[{bind_host}]"
    return f"http://{bind_host}:{port}" + (f"#turns={token}" if token else "")


def main():
    cfg = CFG.load()
    prepare(cfg)
    host, port = cfg["server"]["host"], int(cfg["server"]["port"])
    try:
        srv = make_server(host, port, cfg["server"].get("allowed_hosts") or ())
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        # another serve (a terminal one, or the login service) has the port: not a failure,
        # so a service manager does not restart this one in a loop
        print(f"{advertised_url(host, port)} is already served by another process; nothing to do")
        return 0
    adopt_turn_token(cfg)                     # the same secret across restarts
    token_file = write_turn_token(cfg)        # only once the port is ours: a stepped-aside serve must not replace it
    if sys.stdout.isatty():                   # a person at a terminal gets the one-click URL; a service log never holds the secret
        print(f"serving on {advertised_url(host, srv.server_address[1], turn_token())}")
    else:
        print(f"serving on {advertised_url(host, srv.server_address[1])} (turn token in {token_file})")

    def _terminate(signum, frame):        # SIGTERM from a service manager ends the loop like Ctrl-C does
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _terminate)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        n = stop_turns()
        if n:
            print(f"stopped {n} running turn(s)", file=sys.stderr)
        srv.server_close()                    # the port is free as soon as the loop ends (`convo-index stop` waits for it)
    print("server stopped")
    return 0


if __name__ == "__main__":
    main()
