"""`convo-index doctor`: what this install can see, what it will call, what is failing."""
from __future__ import annotations

import json
import urllib.request

from convo_index import db as dbm
from convo_index import jobs
from convo_index import settings as S
from convo_index.lock import index_lock, IndexLocked
from convo_index.providers import registry as R
from convo_index.sources.base import all_adapters


def _embedding_model_present(spec):
    """For OpenAI-compatible servers (Ollama), check the model is actually pulled."""
    from convo_index.providers.http_backends import OpenAICompat
    base = (spec.get("base_url") or "").rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/models", headers=OpenAICompat(spec)._headers())
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        ids = {m.get("id", "") for m in data.get("data", [])}
        model = spec.get("model") or ""
        return True, any(i == model or i.split(":")[0] == model for i in ids), sorted(ids)
    except Exception as e:
        return False, False, [str(e)]


def report(settings=None):
    settings = settings or S.load()
    rep = {"config_path": str(settings.path) if settings.path else None,
           "config_default_path": str(S.CONFIG_PATH), "db_path": str(settings.db_path()),
           "sources": [], "llm": {}, "embeddings": {}, "index": {}, "warnings": []}
    if settings.path is None:
        rep["warnings"].append("no config file; running on defaults. Run `convo-index init`.")

    for a in all_adapters():
        spec = settings["sources"].get(a.name) or {}
        rep["sources"].append({"name": a.name, "enabled": spec.get("enabled", True),
                               "available": a.available(), "versions": a.supported_versions})

    probe = R.probe_cli_backends()
    rep["llm"]["installed_clis"] = probe
    for job in ("classify", "summary"):
        spec = settings.llm_job(job)
        entry = {"backend": spec["backend"] or None, "model": spec.get("model") or None,
                 "hosted": R.is_hosted(spec) if spec["backend"] else None,
                 "allowed": R.policy_allows(spec, settings) if spec["backend"] else None}
        llm = R.make_llm(spec) if spec["backend"] else None
        entry["available"] = bool(llm and llm.available()) if llm else False
        rep["llm"][job] = entry
        if not spec["backend"]:
            rep["warnings"].append(f"[llm].{job}: no backend configured"
                                   + (f"; `init` would pick {probe[0]}" if probe else "; no CLI found"))
        elif not entry["available"]:
            rep["warnings"].append(f"[llm].{job}: backend {spec['backend']} not usable (not installed / no key)")
        elif not entry["allowed"]:
            rep["warnings"].append(f"[llm].{job}: {R.policy_reason(spec, settings)}")

    espec = settings["embeddings"]
    emb = R.make_embedder(settings)
    blocked = R.embedder_blocked_reason(settings)
    rep["embeddings"] = {"backend": espec.get("backend"), "model": espec.get("model"),
                         "fingerprint": emb.fingerprint if emb else None, "blocked": blocked}
    if espec.get("backend") != "fastembed":
        rep["embeddings"]["base_url"] = espec.get("base_url")
    if blocked:
        rep["warnings"].append(f"embeddings: {blocked}; keyword search works, semantic search does not")
    elif emb.name == "fastembed":
        rep["embeddings"]["model_present"] = present = emb.model_cached()
        rep["embeddings"]["cache_dir"] = emb.cache_dir
        if not present:   # not a warning: the first `index` fetches it
            rep["embeddings"]["note"] = "model not downloaded yet; the first `index` fetches it from Hugging Face"
    else:
        reachable, present, models = _embedding_model_present(espec)
        rep["embeddings"].update({"base_url": espec.get("base_url"), "server_reachable": reachable,
                                  "model_present": present})
        if not reachable:
            rep["warnings"].append(f"embeddings: {espec.get('base_url')} not reachable ({models[0] if models else ''}); "
                                   "keyword search works, semantic search and new vectors do not")
        elif not present:
            rep["warnings"].append(f"embeddings: model {espec.get('model')!r} is not available on the server "
                                   f"(have: {', '.join(models[:6])}). For Ollama: `ollama pull {espec.get('model')}`")

    db_path = settings.db_path()
    rep["index"]["exists"] = db_path.exists()
    if db_path.exists():
        conn = dbm.connect(db_path)
        try:
            ver = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] \
                if dbm._table_exists(conn, "schema_migrations") else ("v1" if dbm._is_legacy_v1(conn) else None)
            rep["index"]["schema_version"] = ver
            if dbm._table_exists(conn, "conversations") and not dbm._is_legacy_v1(conn):
                rep["index"]["conversations_by_source"] = dict(conn.execute(
                    "SELECT source, COUNT(*) FROM conversations GROUP BY source").fetchall())
                total_text = conn.execute("SELECT COUNT(*) FROM messages WHERE kind='text'").fetchone()[0]
                with_vec = conn.execute(
                    "SELECT COUNT(*) FROM messages m JOIN vec_messages v ON v.rowid=m.id WHERE m.kind='text'").fetchone()[0]
                rep["index"]["text_messages"] = total_text
                rep["index"]["text_messages_with_vectors"] = with_vec
                # doctor never migrates: tolerate a database that predates v3 tables
                stored_fp = dbm.get_meta(conn, "embedding_fingerprint") if dbm._table_exists(conn, "index_meta") else None
                rep["index"]["embedding_fingerprint"] = stored_fp
                if ver != dbm.SCHEMA_VERSION:
                    rep["warnings"].append(f"index schema is {ver}, current is {dbm.SCHEMA_VERSION}; "
                                           "the next `index` or `serve` migrates it (after a backup)")
                if emb and stored_fp and stored_fp != emb.fingerprint:
                    what = "vectors predate embedder fingerprints (legacy Ollama path)" if stored_fp.startswith("legacy|") \
                        else "index was built with a different embedder"
                    rep["warnings"].append(f"embeddings: {what}; run `convo-index reembed` "
                                           "(new vectors are skipped until then, keyword search unaffected)")
                if dbm._table_exists(conn, "derived_jobs"):
                    failed = jobs.failed_jobs(conn, limit=10)
                    rep["index"]["failed_jobs"] = [{"id": r[0], "kind": r[1], "attempts": r[2],
                                                    "error": (r[3] or "")[:160], "at": r[4]} for r in failed]
                    n_failed = len(jobs.failed_jobs(conn, limit=10_000))
                    if n_failed:
                        rep["warnings"].append(f"{n_failed} derived job(s) failing; see index.failed_jobs")
                    blocked = jobs.blocked_counts(conn)
                    rep["index"]["blocked_jobs"] = blocked
                    if blocked:
                        ex = jobs.failed_jobs(conn, limit=1, status=jobs.BLOCKED)
                        rep["warnings"].append(f"{sum(blocked.values())} derived job(s) blocked on auth/config "
                                               f"({', '.join(f'{k}: {n}' for k, n in blocked.items())}); they retry when the backend's "
                                               f"login/key/policy changes, or now with `convo-index jobs unblock`"
                                               + (f" — last error: {(ex[0][3] or '')[:120]}" if ex else ""))
                    missing = conn.execute(
                        """SELECT COUNT(*) FROM conversations c LEFT JOIN conversation_summary s
                           ON s.conversation_id=c.id LEFT JOIN conversation_state st ON st.conversation_id=c.id
                           WHERE s.conversation_id IS NULL AND COALESCE(st.deleted, 0) = 0""").fetchone()[0]
                    rep["index"]["conversations_without_summary"] = missing
                if dbm._table_exists(conn, "usage_records"):
                    from convo_index import cost as K
                    pricing = K.load(settings)
                    rep["pricing"] = {"revision": pricing.revision, "user_file": str(pricing.user_path) if pricing.user_path else None,
                                      "unpriced_models": K.unpriced_models(conn, pricing), "errors": pricing.errors}
                    for err in pricing.errors:
                        rep["warnings"].append(f"pricing: {err}")
                    if rep["pricing"]["unpriced_models"]:
                        ids = ", ".join(f"{u['provider']}/{u['model']}" for u in rep["pricing"]["unpriced_models"][:8])
                        rep["warnings"].append(f"{len(rep['pricing']['unpriced_models'])} model id(s) without a price "
                                               f"(cost estimates exclude them): {ids}; add them to {K.user_path(settings)}")
        finally:
            conn.close()
        try:
            with index_lock(db_path):
                rep["index"]["locked"] = False
        except IndexLocked:
            rep["index"]["locked"] = True
    else:
        rep["warnings"].append("no index yet; run `convo-index index`")
    return rep


def format_report(rep):
    lines = [f"config: {rep['config_path'] or 'defaults (no file at ' + rep['config_default_path'] + ')'}",
             f"database: {rep['db_path']} ({'schema v' + str(rep['index'].get('schema_version')) if rep['index'].get('exists') else 'missing'})"]
    lines.append("sources:")
    for s in rep["sources"]:
        state = "disabled" if not s["enabled"] else ("found" if s["available"] else "not found")
        lines.append(f"  {s['name']:12s} {state:9s} {s['versions']}")
    lines.append(f"llm: installed CLIs in preference order: {', '.join(rep['llm']['installed_clis']) or 'none'}")
    for job in ("classify", "summary"):
        e = rep["llm"][job]
        lines.append(f"  {job:9s} backend={e['backend'] or '-'} model={e['model'] or '-'} "
                     f"usable={'yes' if e['available'] else 'no'} hosted={e['hosted']} allowed={e['allowed']}")
    e = rep["embeddings"]
    if e.get("blocked"):
        lines.append(f"embeddings: {e['backend']} {e.get('base_url') or ''} model={e['model']} (blocked: see warnings)")
    elif e["backend"] == "fastembed":
        state = "downloaded" if e.get("model_present") else "not downloaded yet"
        lines.append(f"embeddings: fastembed model={e['model']} ({state})")
    else:
        lines.append(f"embeddings: {e['backend']} {e['base_url']} model={e['model']} "
                     f"server={'up' if e['server_reachable'] else 'down'} model_present={e['model_present']}")
    idx = rep["index"]
    if idx.get("exists"):
        lines.append(f"index: {idx.get('conversations_by_source')} vectors {idx.get('text_messages_with_vectors')}/"
                     f"{idx.get('text_messages')} text messages; {idx.get('conversations_without_summary')} without summary; "
                     f"{'LOCKED (indexer running)' if idx.get('locked') else 'idle'}")
        for j in idx.get("failed_jobs") or []:
            lines.append(f"  failed {j['kind']} {j['id']} x{j['attempts']}: {j['error']}")
    if rep.get("pricing"):
        pr = rep["pricing"]
        lines.append(f"pricing: revision {pr['revision']}" + (f" + {pr['user_file']}" if pr["user_file"] else "")
                     + f"; {len(pr['unpriced_models'])} unpriced model id(s)")
    lines.append("warnings:" if rep["warnings"] else "warnings: none")
    for w in rep["warnings"]:
        lines.append(f"  - {w}")
    return "\n".join(lines)
