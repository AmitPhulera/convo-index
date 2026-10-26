import tomllib

from convo_index import settings as S


def test_defaults_when_no_file(tmp_path):
    s = S.load(tmp_path / "missing.toml", use_cache=False)
    assert s.path is None
    assert s["privacy"]["hosted_processing"] == "ask"
    assert s.llm_job("summary") == {"backend": "", "model": ""}
    assert s["category"]["labels"] == ["work", "personal"]      # generic, nothing employer-specific


def test_file_overrides_merge_deeply(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[llm]\nsummary = { backend = "codex_cli" }\n[category]\nlabels = ["a", "b"]\n'
                 '[sources.opencode]\nenabled = false\n')
    s = S.load(p, use_cache=False)
    assert s.path == p
    assert s.llm_job("summary") == {"backend": "codex_cli", "model": S.DEFAULT_MODELS["codex_cli"]["summary"]}
    assert s.llm_job("classify")["backend"] == ""
    assert s["category"]["labels"] == ["a", "b"]
    assert s["sources"]["opencode"]["enabled"] is False
    assert s["sources"]["claude_code"]["enabled"] is True     # untouched sibling keeps default


def test_env_overrides_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVO_INDEX_DB", str(tmp_path / "x.db"))
    s = S.load(tmp_path / "none.toml", use_cache=False)
    assert s.db_path() == tmp_path / "x.db"


def test_render_roundtrips_through_tomllib(tmp_path):
    cfg = {"llm": {"summary": {"backend": "gemini_cli", "model": "g"}},
           "sources": {"opencode": {"enabled": False, "exclude_cwd": ["~/secret/*"]}},
           "category": {"labels": ["x"], "hints": {"x": ["a", "b"]}},
           "server": {"host": "127.0.0.1", "port": 9753},
           "privacy": {"hosted_processing": "deny"}}
    text = S.render(cfg, {"privacy": "who sees what"})
    back = tomllib.loads(text)
    assert back == cfg
    assert "# who sees what" in text


def test_write_then_load(tmp_path):
    p = tmp_path / "c.toml"
    S.write({"privacy": {"hosted_processing": "deny"}}, p)
    assert S.load(p, use_cache=False)["privacy"]["hosted_processing"] == "deny"


def test_render_escapes_newlines_and_quotes():
    cfg = {"category": {"descriptions": {"a": 'line1\nline2 "quoted"'}}}
    assert tomllib.loads(S.render(cfg)) == cfg


def test_render_quotes_non_bare_keys():
    cfg = {"category": {"hints": {"client work": ["a"], "acme.io": ["b"]}}}
    assert tomllib.loads(S.render(cfg)) == cfg


def test_persisted_defaults_do_not_include_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CONVO_INDEX_DB", str(tmp_path / "env.db"))
    assert "env.db" not in S.DEFAULTS["paths"]["db"]
    s = S.load(tmp_path / "none.toml", use_cache=False)
    assert s.db_path() == tmp_path / "env.db"          # env applies at run time only


def test_render_keeps_supplementary_unicode_literal():
    cfg = {"category": {"descriptions": {"fun 😀": "emoji 😀 and accents é"}, "labels": ["日本語"]}}
    text = S.render(cfg)
    assert "\\ud83d" not in text and "😀" in text
    assert tomllib.loads(text) == cfg


def test_model_only_embeddings_keep_the_default_backend(tmp_path):
    """A `[embeddings]` table that names a model but no backend keeps fastembed; openai_compat
    without a model gets Ollama's default name rather than fastembed's."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[embeddings]\nmodel = "nomic-ai/nomic-embed-text-v1.5-Q"\n')
    loaded = S.load(cfg, use_cache=False)["embeddings"]
    assert loaded["backend"] == "fastembed" and loaded["model"] == "nomic-ai/nomic-embed-text-v1.5-Q"
    cfg.write_text('[embeddings]\nbackend = "openai_compat"\n')
    assert S.load(cfg, use_cache=False)["embeddings"]["model"] == "nomic-embed-text"


def test_user_dirs_follow_the_platform(monkeypatch, tmp_path):
    from convo_index import config as C
    monkeypatch.delenv("XDG_DATA_HOME", raising=False); monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local")); monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert C.user_dir("data", windows=True) == tmp_path / "local" and C.user_dir("config", windows=True) == tmp_path / "roaming"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert C.user_dir("config", windows=True) == tmp_path / "xdg"         # an XDG variable wins on every platform
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert C.user_dir("config", windows=False) == C.Path.home() / ".config"
