import json
import tomllib

from convo_index import cli
from convo_index import db as dbm
from convo_index import settings as S
from convo_index.model import Conversation, Message
from convo_index.providers import registry as R


def _isolate(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg" / "config.toml"
    monkeypatch.setattr(S, "CONFIG_PATH", cfg)
    monkeypatch.setenv("CONVO_INDEX_DB", str(tmp_path / "convos.db"))
    S.reset_cache()
    return cfg


def test_init_writes_config_with_probe_default_and_allows_hosted(tmp_path, monkeypatch, capsys):
    cfg = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(R, "probe_cli_backends", lambda: ["codex_cli", "claude_cli"])
    assert cli.main(["init", "--yes"]) == 0
    data = tomllib.loads(cfg.read_text())
    assert data["llm"]["summary"] == {"backend": "codex_cli", "model": S.DEFAULT_MODELS["codex_cli"]["summary"]}
    assert data["llm"]["classify"]["backend"] == "codex_cli"
    assert data["privacy"]["hosted_processing"] == "allow"
    assert "probe order: codex_cli, claude_cli" in capsys.readouterr().out


def test_init_keeps_existing_explicit_choices(tmp_path, monkeypatch):
    cfg = _isolate(tmp_path, monkeypatch)
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[llm]\nsummary = { backend = "claude_cli", model = "claude-haiku-4-5" }\n'
                   '[privacy]\nhosted_processing = "deny"\n')
    monkeypatch.setattr(R, "probe_cli_backends", lambda: ["gemini_cli"])
    cli.main(["init", "--yes"])
    data = tomllib.loads(cfg.read_text())
    assert data["llm"]["summary"]["backend"] == "claude_cli"        # explicit entry wins over the probe
    assert data["llm"]["classify"]["backend"] == "gemini_cli"       # unset job gets the probe default
    assert data["privacy"]["hosted_processing"] == "deny"


def test_init_without_any_cli_leaves_jobs_unconfigured(tmp_path, monkeypatch):
    cfg = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(R, "probe_cli_backends", lambda: [])
    cli.main(["init", "--yes"])
    data = tomllib.loads(cfg.read_text())
    assert data["llm"]["summary"]["backend"] == "" and data["privacy"]["hosted_processing"] == "ask"


def test_init_seeds_category_labels_from_existing_index(tmp_path, monkeypatch):
    cfg = _isolate(tmp_path, monkeypatch)
    db = tmp_path / "convos.db"
    conn = dbm.connect(db); dbm.init_schema(conn, db)
    for i, cat in enumerate(["globex", "globex", "personal", "unknown"]):
        c = Conversation(source="claude_code", native_id=f"s{i}", title="t", first_prompt="p", source_ref="/a",
                         messages=[Message(seq=0, role="user", text="x")])
        dbm.upsert_conversation(conn, c, cat, "path", [], None, "1", lambda t: [None] * len(t))
    conn.close()
    monkeypatch.setattr(R, "probe_cli_backends", lambda: [])
    cli.main(["init", "--yes"])
    data = tomllib.loads(cfg.read_text())
    assert data["category"]["labels"] == ["globex", "personal"]


def test_config_path_and_show(tmp_path, monkeypatch, capsys):
    cfg = _isolate(tmp_path, monkeypatch)
    cli.main(["config", "path"])
    assert capsys.readouterr().out.strip() == str(cfg)
    cli.main(["config", "show"])
    assert "[privacy]" in capsys.readouterr().out


def test_reembed_drops_vectors_and_records_fingerprint(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    db = tmp_path / "convos.db"
    conn = dbm.connect(db); dbm.init_schema(conn, db)
    c = Conversation(source="claude_code", native_id="s", title="t", first_prompt="p", source_ref="/a",
                     messages=[Message(seq=0, role="user", text="x")])
    from convo_index.config import EMBED_DIM
    dbm.upsert_conversation(conn, c, "globex", "path", [], None, "1", lambda t: [[0.0] * EMBED_DIM for _ in t])
    assert conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0] == 1
    conn.close()
    assert cli.main(["reembed"]) == 0
    conn = dbm.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0] == 0
    assert "nomic-embed-text" in dbm.get_meta(conn, "embedding_fingerprint")
    assert "dropped 1 message vectors" in capsys.readouterr().out


def test_doctor_json_reports_warnings(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    from convo_index import doctor
    monkeypatch.setattr(doctor, "_embedding_model_present", lambda spec: (False, False, ["connection refused"]))
    monkeypatch.setattr(R, "probe_cli_backends", lambda: [])
    monkeypatch.setattr(S, "DEFAULTS", {**S.DEFAULTS, "embeddings": {**S.DEFAULTS["embeddings"], "backend": "openai_compat"}})
    rc = cli.main(["doctor", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert any("no config file" in w for w in rep["warnings"])
    assert any("no backend configured" in w for w in rep["warnings"])
    assert any("not reachable" in w for w in rep["warnings"])
    assert any("no index yet" in w for w in rep["warnings"])


def test_doctor_reports_turn_harnesses(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    from convo_index import doctor
    monkeypatch.setattr(R, "probe_cli_backends", lambda: [])
    from convo_index.sources import base as sources
    monkeypatch.setattr(sources, "is_windows", lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: "/opt/bin/claude" if name == "claude" else None)   # one module object for all
    rc = cli.main(["doctor", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert rep["turns"]["enabled"] is True
    assert rep["turns"]["harnesses"]["claude_code"]["path"] == "/opt/bin/claude"
    assert rep["turns"]["harnesses"]["opencode"]["path"] is None
    assert rep["turns"]["harnesses"]["claude_code"]["launcher"] == ["/opt/bin/claude"]
    assert rep["turns"]["harnesses"]["opencode"]["launcher"] is None
    assert set(rep["turns"]["harnesses"]) == {"claude_code", "opencode"}
    text = doctor.format_report(rep)
    assert "browser turns: on; usable harness: claude_code (/opt/bin/claude)" in text
    assert rc == 1   # the usual warnings (no config, no index) still apply


def test_init_preserves_explicit_disabled_source(tmp_path, monkeypatch):
    cfg = _isolate(tmp_path, monkeypatch)
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[sources.opencode]\nenabled = false\n')
    monkeypatch.setattr(R, "probe_cli_backends", lambda: [])
    cli.main(["init", "--yes"])
    assert tomllib.loads(cfg.read_text())["sources"]["opencode"]["enabled"] is False


def test_reembed_recreates_vector_table_with_configured_dim(tmp_path, monkeypatch, capsys):
    cfg = _isolate(tmp_path, monkeypatch)
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[embeddings]\ndim = 1536\nmodel = "text-embedding-3-small"\n')
    db = tmp_path / "convos.db"
    conn = dbm.connect(db); dbm.init_schema(conn, db); conn.close()
    assert cli.main(["reembed"]) == 0
    conn = dbm.connect(db)
    assert dbm.vector_dim(conn) == 1536
    assert "1536 dims" in capsys.readouterr().out


def test_init_without_tty_and_without_yes_keeps_ask(tmp_path, monkeypatch, capsys):
    cfg = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(R, "probe_cli_backends", lambda: ["gemini_cli"])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    cli.main(["init"])
    assert tomllib.loads(cfg.read_text())["privacy"]["hosted_processing"] == "ask"
    assert "pass --yes" in capsys.readouterr().err


def test_tag_cli_assign_list_and_retag(tmp_path, monkeypatch, capsys):
    from tests.conftest import make_settings
    _isolate(tmp_path, monkeypatch)
    st = make_settings(tmp_path)
    st["paths"]["db"] = str(tmp_path / "convos.db")
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    db = tmp_path / "convos.db"
    conn = dbm.connect(db); dbm.init_schema(conn, db)
    c = Conversation(source="claude_code", native_id="s", title="t", first_prompt="p", source_ref="/a", cwd="/nowhere",
                     messages=[Message(seq=0, role="user", text="x")])
    dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t)); conn.close()
    assert cli.main(["tag", "s", "workspace=personal"]) == 0
    assert json.loads(capsys.readouterr().out) == {"workspace": ["personal"]}
    assert cli.main(["tag", "s", "workspace=none"]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert cli.main(["tag", "s", "workspace=bogus"]) == 1
    assert cli.main(["tag", "s", "workspace"]) == 2                 # missing '=' is an error, not "none"
    assert cli.main(["tag", "--list"]) == 0
    out = capsys.readouterr().out
    assert "workspace (exclusive)" in out and "globex" in out
    conn = dbm.connect(db)        # a finished job whose answer was empty: no evidence row, still re-tagged
    conn.execute("INSERT INTO derived_jobs(conversation_id, kind, input_fingerprint, status, attempts, last_run) "
                 "VALUES('claude_code:s','tags','f','ok',1,'t')")
    conn.commit(); conn.close()
    assert cli.main(["tag", "--retag"]) == 0
    assert "1 conversation" in capsys.readouterr().out
    conn = dbm.connect(db)
    assert conn.execute("SELECT count(*) FROM derived_jobs WHERE kind='tags'").fetchone()[0] == 0
    conn.close()


def test_tag_suggestions_and_calibrate_cli(tmp_path, monkeypatch, capsys):
    from tests.conftest import make_settings
    from tests.test_vectors import fake_embed, FP
    from convo_index import tags as T, vectors as V
    _isolate(tmp_path, monkeypatch)
    st = make_settings(tmp_path); st["paths"]["db"] = str(tmp_path / "convos.db")
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    db = tmp_path / "convos.db"
    conn = dbm.connect(db); dbm.init_schema(conn, db); T.sync_definitions(conn, st)
    for i, (title, txt) in enumerate([("Kube", "kubernetes deploy failing"), (None, "kubernetes cluster upgrade"), (None, "kubernetes ingress")]):
        c = Conversation(source="claude_code", native_id=f"s{i}", title=title, first_prompt=txt, source_ref="/a", cwd="/x",
                         start_ts=f"2026-06-0{i + 1}T00:00:00Z", messages=[Message(seq=0, role="user", text=txt)])
        dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
        V.run_vector(conn, c.id, fake_embed, FP)
    assert cli.main(["tag", "--calibrate"]) == 0
    assert "no suggestions" in capsys.readouterr().out
    T.set_decision(conn, "claude_code:s0", "workspace", "globex", settings=st)     # suggests to s1 and s2
    conn.close()
    assert cli.main(["tag", "--suggestions"]) == 0
    out = capsys.readouterr().out
    assert "2 open suggestion(s)" in out and "workspace=globex" in out and "«kubernetes cluster upgrade»" in out   # None title -> first prompt
    conn = dbm.connect(db)
    T.set_decision(conn, "claude_code:s1", "workspace", "globex", suggest=False)
    T.set_decision(conn, "claude_code:s2", "workspace", "personal", suggest=False)  # exclusive: implicit reject
    conn.close()
    assert cli.main(["tag", "--calibrate"]) == 0
    out = capsys.readouterr().out
    assert "workspace:" in out and "1 accepted, 1 rejected (precision 0.5)" in out and "at or above" in out
    assert cli.main(["tag", "--suggestions"]) == 0
    assert "0 open suggestion(s)" in capsys.readouterr().out


ADVANCED_TAGS = '''
[[tag_groups]]
name = "topic"
exclusive = false
description = "what the conversation is about"

[[tags]]
name = "infra"
group = "topic"
rules = { branch_prefix = ["infra/"], regex = ["ansible|deploy"] }

[[tags]]
name = "review"
group = "topic"
'''


def test_init_keeps_tag_groups_and_tags(tmp_path, monkeypatch):
    """Re-running init must not drop [[tag_groups]] / [[tags]] (arrays of tables)."""
    cfg = _isolate(tmp_path, monkeypatch)
    cfg.parent.mkdir(parents=True)
    original = '# my notes\n[privacy]\nhosted_processing = "deny"\n' + ADVANCED_TAGS
    cfg.write_text(original)
    monkeypatch.setattr(R, "probe_cli_backends", lambda: ["gemini_cli"])
    assert cli.main(["init", "--yes"]) == 0
    data = tomllib.loads(cfg.read_text())
    assert data["tag_groups"] == [{"name": "topic", "exclusive": False, "description": "what the conversation is about"}]
    assert [t["name"] for t in data["tags"]] == ["infra", "review"]
    assert data["tags"][0]["rules"] == {"branch_prefix": ["infra/"], "regex": ["ansible|deploy"]}
    assert [b.read_text() for b in cfg.parent.glob("config.toml.bak-*")] == [original]   # comments recoverable


def test_render_roundtrips_tables_arrays_and_top_level_keys():
    s = {"version": 2, "llm": {"summary": {"backend": "x", "model": "m"}},
         "tags": [{"name": "a", "group": "g", "rules": {"regex": ["\\bPR\\b"]}}, {"name": "b", "group": "g"}]}
    assert tomllib.loads(S.render(s)) == s


def test_doctor_reports_fastembed_state_without_warning_for_a_missing_download(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    from convo_index import doctor
    from convo_index.providers import local_backends as local
    monkeypatch.setattr(R, "probe_cli_backends", lambda: [])
    monkeypatch.setattr(local, "installed", lambda: True)
    monkeypatch.setattr(local.FastEmbed, "model_cached", lambda self: False)
    cli.main(["doctor", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert rep["embeddings"]["backend"] == "fastembed" and rep["embeddings"]["model_present"] is False
    assert "first `index`" in rep["embeddings"]["note"]
    assert not any(w.startswith("embeddings") for w in rep["warnings"])
    assert "not downloaded yet" in doctor.format_report(rep)

    monkeypatch.setattr(local, "installed", lambda: False)
    cli.main(["doctor", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert any("uv sync" in w for w in rep["warnings"])
    assert "blocked: see warnings" in doctor.format_report(rep)


def test_doctor_names_a_policy_blocked_server_embedder_as_blocked(tmp_path, monkeypatch, capsys):
    cfg = _isolate(tmp_path, monkeypatch)
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[embeddings]\nbackend = "openai_compat"\nbase_url = "https://api.openai.com/v1"\n'
                   '[privacy]\nhosted_processing = "deny"\n')
    from convo_index import doctor
    monkeypatch.setattr(R, "probe_cli_backends", lambda: [])
    cli.main(["doctor", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert rep["embeddings"]["base_url"] == "https://api.openai.com/v1" and "deny" in rep["embeddings"]["blocked"]
    line = next(l for l in doctor.format_report(rep).splitlines() if l.startswith("embeddings:"))
    assert "openai_compat https://api.openai.com/v1" in line and "blocked" in line and "not installed" not in line


def test_init_picks_the_embedder_that_is_present(tmp_path, monkeypatch):
    from convo_index.providers import local_backends as local
    cfg = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(R, "probe_cli_backends", lambda: [])
    monkeypatch.setattr(local, "installed", lambda: True)
    from convo_index import doctor
    monkeypatch.setattr(doctor, "_embedding_model_present", lambda spec: (True, True, []))
    cli.main(["init", "--yes"])
    assert tomllib.loads(cfg.read_text())["embeddings"]["backend"] == "fastembed"   # installed wins

    cfg.unlink()
    monkeypatch.setattr(local, "installed", lambda: False)
    cli.main(["init", "--yes"])
    data = tomllib.loads(cfg.read_text())["embeddings"]
    assert data["backend"] == "openai_compat" and data["model"] == "nomic-embed-text"  # Ollama is running

    cfg.unlink()
    monkeypatch.setattr(doctor, "_embedding_model_present", lambda spec: (False, False, ["refused"]))
    cli.main(["init", "--yes"])
    assert tomllib.loads(cfg.read_text())["embeddings"]["backend"] == "fastembed"   # neither: doctor explains

    cfg.write_text('[embeddings]\nbackend = "openai_compat"\nmodel = "mxbai-embed-large"\n')
    monkeypatch.setattr(local, "installed", lambda: True)
    cli.main(["init", "--yes"])
    data = tomllib.loads(cfg.read_text())["embeddings"]
    assert data["backend"] == "openai_compat" and data["model"] == "mxbai-embed-large"  # explicit choice stays


def test_up_runs_init_services_and_the_first_index(tmp_path, monkeypatch, capsys):
    import webbrowser
    from convo_index import indexer
    from convo_index import schedule as SCH
    _isolate(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(cli, "cmd_init", lambda args: calls.append("init") or 0)
    monkeypatch.setattr(SCH, "install", lambda settings, hour=None, minute=None, dry_run=False, system=None, home=None, job="refresh":
                        calls.append(job) or {"kind": "launchd", "files": {"/x.plist": ""}, "results": [], "log": "/l"})
    monkeypatch.setattr(SCH, "plan", lambda settings, **k: {"kind": "launchd", "argv": ["convo-index", "index"], "log": str(tmp_path / "refresh.log")})
    monkeypatch.setattr(SCH, "start_detached", lambda plan: calls.append("background " + plan["argv"][-1]))
    monkeypatch.setattr(indexer, "main", lambda: calls.append("index") or 0)
    monkeypatch.setattr(cli, "_wait_for_port", lambda host, port, seconds=15: calls.append(f"port {host}:{port}") or True)
    monkeypatch.setattr(webbrowser, "open", lambda url: calls.append("open " + url))
    assert cli.main(["up"]) == 0
    assert calls == ["init", "serve", "refresh", "background index", "port 127.0.0.1:9753", "open http://127.0.0.1:9753"]
    out = capsys.readouterr().out
    assert "convo-index is at http://127.0.0.1:9753" in out and "privacy decision" in out   # nothing hosted until the user decides
    assert "tail -f" in out
    calls.clear()
    from convo_index import server
    from convo_index import settings as S
    S.reset_cache()
    server.turn_token_path(S.load(use_cache=False)).write_text("tok123")   # a running server left its turn token
    assert cli.main(["up"]) == 0 and calls[-1] == "open http://127.0.0.1:9753#turns=tok123"
    assert "convo-index is at http://127.0.0.1:9753#turns=tok123" in capsys.readouterr().out
    calls.clear()
    assert cli.main(["up", "--no-serve", "--no-schedule", "--wait"]) == 0 and calls == ["init", "index"]
    calls.clear()
    assert cli.main(["up", "--no-open"]) == 0 and "open" not in " ".join(calls)
    capsys.readouterr()
    monkeypatch.setattr(cli, "_wait_for_port", lambda host, port, seconds=15: False)
    assert cli.main(["up", "--no-schedule"]) == 1                        # no server answered: no URL, a failure
    out, err = capsys.readouterr()
    assert "convo-index is at" not in out and "did not answer" in err and "problems: serve" in err
    monkeypatch.setattr(cli, "cmd_init", lambda args: 1)
    assert cli.main(["up"]) == 1                                         # a failed init stops everything
    monkeypatch.setattr(SCH, "uninstall", lambda settings, dry_run=False, job="refresh", **k:
                        {"kind": "schtasks", "results": [{"cmd": ["schtasks"], "ok": False, "stderr": "Access is denied"}]})
    assert cli.main(["service", "uninstall"]) == 1                       # a failed deregistration is a failure
