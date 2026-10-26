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


def test_init_preserves_explicit_disabled_source(tmp_path, monkeypatch):
    cfg = _isolate(tmp_path, monkeypatch)
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[sources.opencode]\nenabled = false\n')
    monkeypatch.setattr(R, "probe_cli_backends", lambda: [])
    cli.main(["init", "--yes"])
    assert tomllib.loads(cfg.read_text())["sources"]["opencode"]["enabled"] is False


def test_init_without_tty_and_without_yes_keeps_ask(tmp_path, monkeypatch, capsys):
    cfg = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(R, "probe_cli_backends", lambda: ["gemini_cli"])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    cli.main(["init"])
    assert tomllib.loads(cfg.read_text())["privacy"]["hosted_processing"] == "ask"
    assert "pass --yes" in capsys.readouterr().err


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
