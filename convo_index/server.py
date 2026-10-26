import errno
import os
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, urlsplit, parse_qs, unquote

from convo_index.config import STATIC_DIR
from convo_index.sources.base import resume_command
from convo_index.providers import registry as R
from convo_index import db as dbm
from convo_index import search as S
from convo_index import analytics as A
from convo_index import settings as CFG
from convo_index import tags as T
from convo_index import references as REF
from convo_index import cost as K

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
        "hidden": hidden, "summary": summary, "tags": tag_detail, "usage": usage,
        "references": refs, "related": related,
        "messages": [{"role": m[0], "kind": m[1] or "text", "ts": m[2], "text": m[3]} for m in msgs],
    })


def handle_analytics():
    return json.dumps(A.analytics(_db(), CFG.load()))


# Browser-facing guard (any web page can reach a localhost port): every request must name this
# server in Host, which defeats DNS rebinding, and every POST must carry CSRF_HEADER, which a
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
    if method == "POST":
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
        if path == "/api/search":
            return self._send(200, handle_search(qs))
        if path == "/api/sources":
            return self._send(200, handle_sources())
        if path == "/api/config":
            return self._send(200, handle_config())
        if path == "/api/analytics":
            return self._send(200, handle_analytics())
        if path.startswith("/api/conversation/"):
            body = handle_conversation(_segment(path, "/api/conversation/"))
            return self._send(404, "not found", "text/plain") if body is None else self._send(200, body)
        return self._send(404, "not found", "text/plain")

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


def advertised_url(bind_host, port):
    """The URL to open: a wildcard bind is reached through loopback (the allow-list never
    carries "0.0.0.0"), and an IPv6 literal needs brackets."""
    if bind_host in ("0.0.0.0", "::", ""):
        bind_host = "127.0.0.1"
    elif ":" in bind_host:
        bind_host = f"[{bind_host}]"
    return f"http://{bind_host}:{port}"


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
    print(f"serving on {advertised_url(host, srv.server_address[1])}")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    main()
