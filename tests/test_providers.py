"""Provider backends against recorded CLI/HTTP envelopes (no processes, no network)."""
import io
import json
import math
import os
import sys
import urllib.error

import pytest

from convo_index.providers import base, cli_backends as cli, http_backends as http, local_backends as local
from convo_index.providers.base import LLMRequest

SCHEMA = {"type": "object", "properties": {"category": {"type": "string", "enum": ["a", "b"]}},
          "required": ["category"]}


class _Res:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _fake_run(monkeypatch, fn):
    """fn(cmd) -> _Res | None (None = timeout)."""
    recorded = []
    def run(cmd, timeout, stdin=None):
        recorded.append(cmd)
        return fn(cmd)
    monkeypatch.setattr(cli, "_run", run)
    return recorded


# ---------------------------------------------------------------- base helpers

def test_extract_json_handles_fences_and_prose():
    assert base.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert base.extract_json('Sure! {"a": 1} hope that helps') == {"a": 1}
    assert base.extract_json("no json") is None


def test_validate_types_required_enum():
    assert base.validate({"category": "a"}, SCHEMA) is None
    assert "not in" in base.validate({"category": "z"}, SCHEMA)
    assert "missing" in base.validate({}, SCHEMA)
    assert "expected object" in base.validate([], SCHEMA)
    assert base.validate({"n": True}, {"type": "object", "properties": {"n": {"type": "number"}}})


def test_strict_schema_adds_additional_properties_false_recursively():
    s = base.strict_schema({"type": "object", "properties": {"x": {"type": "object", "properties": {"y": {"type": "string"}}}}})
    assert s["additionalProperties"] is False and s["properties"]["x"]["additionalProperties"] is False
    assert s["required"] == ["x"]


def test_finish_marks_malformed_and_invalid():
    req = LLMRequest(prompt="p", model="m", schema=SCHEMA)
    assert base.finish(req, "garbage").error == "malformed"
    bad = base.finish(req, '{"category": "z"}')
    assert bad.error == "malformed" and "not in" in bad.detail
    assert base.finish(req, None, structured={"category": "b"}).data == {"category": "b"}
    assert base.finish(LLMRequest(prompt="p", model="m"), "free text").data == "free text"


# ---------------------------------------------------------------- claude_cli

def test_claude_cli_parses_envelope_and_usage(monkeypatch):
    env = {"type": "result", "is_error": False, "result": "```json\n{\"category\":\"a\"}\n```",
           "session_id": "sess", "total_cost_usd": 0.0194,
           "usage": {"input_tokens": 10, "cache_creation_input_tokens": 8292, "cache_read_input_tokens": 13971,
                     "output_tokens": 86},
           "modelUsage": {"claude-haiku-4-5-20251001": {"costUSD": 0.0009}}}
    cmds = _fake_run(monkeypatch, lambda cmd: _Res(0, json.dumps(env)))
    r = cli.ClaudeCLI().complete(LLMRequest(prompt="P", model="claude-haiku-4-5", schema=SCHEMA, marker="MARK "))
    assert r.ok and r.data == {"category": "a"} and r.backend == "claude_cli"
    assert r.model_used == "claude-haiku-4-5-20251001"
    assert (r.usage.input_uncached, r.usage.cache_write, r.usage.cache_read, r.usage.output) == (10, 8292, 13971, 86)
    assert r.usage.reported_cost_usd == 0.0194
    cmd = cmds[0]
    assert cmd[:3] == ["claude", "-p", "--model"] and "--json-schema" in cmd and cmd[-1].startswith("MARK P")


def test_claude_cli_failures_are_classified(monkeypatch):
    _fake_run(monkeypatch, lambda cmd: _Res(1, "", "Not logged in. Please run /login"))
    assert cli.ClaudeCLI().complete(LLMRequest(prompt="P", model="m")).error == "auth"
    _fake_run(monkeypatch, lambda cmd: _Res(1, "", "429 rate limit exceeded"))
    assert cli.ClaudeCLI().complete(LLMRequest(prompt="P", model="m")).error == "rate_limit"
    _fake_run(monkeypatch, lambda cmd: None)
    assert cli.ClaudeCLI().complete(LLMRequest(prompt="P", model="m")).error == "timeout"
    _fake_run(monkeypatch, lambda cmd: _Res(0, "not json"))
    assert cli.ClaudeCLI().complete(LLMRequest(prompt="P", model="m")).error == "provider"


# ---------------------------------------------------------------- gemini_cli (agy)

def test_agy_uses_structured_output_and_usage(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/agy")
    env = {"conversation_id": "c1", "status": "SUCCESS", "response": "{\"category\": \"b\"}\n",
           "structured_output": {"category": "b"},
           "usage": {"input_tokens": 28459, "output_tokens": 65, "thinking_tokens": 3, "cache_read_tokens": 0}}
    cmds = _fake_run(monkeypatch, lambda cmd: _Res(0, json.dumps(env)))
    r = cli.GeminiCLI().complete(LLMRequest(prompt="P", model="gemini-3.8-flash-low", schema=SCHEMA))
    assert r.ok and r.data == {"category": "b"} and r.usage.provider == "google"
    assert (r.usage.input_uncached, r.usage.output, r.usage.reasoning) == (28459, 65, 3)
    cmd = cmds[0]
    assert cmd[0] == "agy" and "--json-schema" in cmd and cmd[-1] == "--print=P"   # prompt attached to the flag


def test_agy_non_success_status_is_provider_error(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/agy")
    _fake_run(monkeypatch, lambda cmd: _Res(0, json.dumps({"status": "FAILED", "response": ""})))
    assert cli.GeminiCLI().complete(LLMRequest(prompt="P", model="m")).error == "provider"


def test_agy_denied_tool_with_empty_response_names_the_tool(monkeypatch):
    """Headless agy refuses a tool the model reached for, then reports SUCCESS with no text."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/agy")
    env = {"status": "SUCCESS", "response": "", "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
           "usage": {"input_tokens": 67433, "output_tokens": 1481}}
    _fake_run(monkeypatch, lambda cmd: _Res(0, json.dumps(env)))
    r = cli.GeminiCLI().complete(LLMRequest(prompt="P", model="m", schema=SCHEMA))
    assert r.error == "malformed" and "RunCommand" in r.detail and "denied" in r.detail
    env["response"] = "{\"category\": \"a\"}"          # a denied tool with an answer anyway is fine
    _fake_run(monkeypatch, lambda cmd: _Res(0, json.dumps(env)))
    assert cli.GeminiCLI().complete(LLMRequest(prompt="P", model="m", schema=SCHEMA)).ok


def test_gemini_cli_without_binary_is_config_error(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.GeminiCLI().complete(LLMRequest(prompt="P", model="m")).error == "config"


# ---------------------------------------------------------------- codex_cli

def test_codex_parses_event_stream_and_writes_strict_schema(monkeypatch, tmp_path):
    seen = {}
    def run(cmd):
        i = cmd.index("--output-schema")
        with open(cmd[i + 1]) as fh:
            seen["schema"] = json.load(fh)
        seen["cmd"] = cmd
        events = [{"type": "thread.started", "thread_id": "t"},
                  {"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": '{"category":"a"}'}},
                  {"type": "turn.completed", "usage": {"input_tokens": 12256, "cached_input_tokens": 256,
                                                       "cache_write_input_tokens": 0, "output_tokens": 21,
                                                       "reasoning_output_tokens": 5}}]
        return _Res(0, "\n".join(json.dumps(e) for e in events))
    _fake_run(monkeypatch, run)
    r = cli.CodexCLI().complete(LLMRequest(prompt="P", model="gpt-5.6-luna", schema=SCHEMA))
    assert r.ok and r.data == {"category": "a"} and r.usage.provider == "openai"
    assert (r.usage.input_uncached, r.usage.cache_read, r.usage.output, r.usage.reasoning) == (12000, 256, 21, 5)
    assert seen["schema"]["additionalProperties"] is False
    assert "--ephemeral" in seen["cmd"] and "read-only" in seen["cmd"]


def test_codex_turn_failed_is_classified(monkeypatch):
    err = {"type": "turn.failed", "error": {"message": "invalid_json_schema: additionalProperties required"}}
    _fake_run(monkeypatch, lambda cmd: _Res(0, json.dumps(err)))
    r = cli.CodexCLI().complete(LLMRequest(prompt="P", model="m", schema=SCHEMA))
    assert not r.ok and r.error == "provider" and "additionalProperties" in r.detail


# ---------------------------------------------------------------- http backends

class _HTTPResp:
    def __init__(self, body): self.body = json.dumps(body).encode()
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_anthropic_api_uses_forced_tool_for_schema(monkeypatch):
    sent = {}
    def urlopen(req, timeout):
        sent["body"] = json.loads(req.data); sent["headers"] = dict(req.headers)
        return _HTTPResp({"id": "msg_1", "model": "claude-haiku-4-5", "usage": {"input_tokens": 5, "output_tokens": 7},
                          "content": [{"type": "tool_use", "name": "answer", "input": {"category": "a"}}]})
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    r = http.AnthropicAPI({"backend": "anthropic_api"}).complete(LLMRequest(prompt="P", model="claude-haiku-4-5", schema=SCHEMA))
    assert r.ok and r.data == {"category": "a"} and r.usage.output == 7
    assert sent["body"]["tool_choice"] == {"type": "tool", "name": "answer"}
    assert sent["headers"]["X-api-key"] == "k"


def test_anthropic_api_without_key_is_auth_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert http.AnthropicAPI({}).complete(LLMRequest(prompt="P", model="m")).error == "auth"


def test_openai_compat_chat_and_usage_split(monkeypatch):
    def urlopen(req, timeout):
        return _HTTPResp({"id": "c", "model": "qwen", "choices": [{"message": {"content": '{"category":"b"}'}}],
                          "usage": {"prompt_tokens": 20, "prompt_tokens_details": {"cached_tokens": 5},
                                    "completion_tokens": 36}})
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    r = http.OpenAICompat({"base_url": "http://localhost:11434/v1"}).complete(
        LLMRequest(prompt="P", model="qwen", schema=SCHEMA))
    assert r.ok and r.data == {"category": "b"}
    assert (r.usage.input_uncached, r.usage.cache_read, r.usage.output) == (15, 5, 36)


def test_openai_compat_http_errors(monkeypatch):
    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "nope", {}, io.BytesIO(b"bad key"))
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    r = http.OpenAICompat({"base_url": "https://api.example/v1"}).complete(LLMRequest(prompt="P", model="m"))
    assert r.error == "auth" and "bad key" in r.detail


def test_openai_compat_embeddings_batches_truncates_and_checks_dim(monkeypatch):
    calls = []
    def urlopen(req, timeout):
        body = json.loads(req.data); calls.append(body)
        return _HTTPResp({"data": [{"index": i, "embedding": [0.1, 0.2]} for i in range(len(body["input"]))]})
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    emb = http.OpenAICompat({"base_url": "http://localhost:11434/v1", "model": "nomic", "dim": 2, "max_chars": 5})
    res = emb.embed(["a" * 100, "b", "c"], batch_size=2)
    assert res.error is None and len(res.vectors) == 3 and res.vectors[0] == [0.1, 0.2]
    assert len(calls) == 2 and calls[0]["input"][0] == "aaaaa"    # truncated to max_chars
    assert "nomic" in emb.fingerprint and "dim=2" in emb.fingerprint
    bad = http.OpenAICompat({"base_url": "http://localhost:11434/v1", "model": "nomic", "dim": 768})
    res = bad.embed(["x"])
    assert res.error == "config" and res.vectors == [None]


def test_openai_compat_embeddings_error_returns_none_vectors(monkeypatch):
    def urlopen(req, timeout):
        raise urllib.error.HTTPError("u", 404, "model not found", {}, io.BytesIO(b'{"error":"not found"}'))
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    res = http.OpenAICompat({"base_url": "http://localhost:11434/v1", "model": "nomic"}).embed(["x", "y"])
    assert res.vectors == [None, None] and res.error == "provider" and "404" in res.detail


# ---------------------------------------------------------------- registry


def test_validate_rejects_null_unless_schema_allows():
    assert "null" in base.validate({"category": None}, SCHEMA)
    assert base.validate({"x": None}, {"type": "object", "properties": {"x": {"type": ["string", "null"]}}}) is None
    assert base.validate({"x": 3}, {"type": "object", "properties": {"x": {"type": ["string", "null"]}}})


def test_validate_anyof_nullable_enum():
    sch = {"type": "object", "properties": {"w": {"anyOf": [{"type": "string", "enum": ["a", "b"]}, {"type": "null"}]}},
           "required": ["w"]}
    assert base.validate({"w": "a"}, sch) is None and base.validate({"w": None}, sch) is None
    assert "anyOf" in base.validate({"w": "z"}, sch)
    strict = base.strict_schema(sch)
    assert strict["properties"]["w"]["anyOf"][0] == {"type": "string", "enum": ["a", "b"]}


def test_is_local_url_rejects_dns_lookalikes_of_loopback():
    assert not base.is_local_url("https://127.evil.example/v1") and not base.is_local_url("http://localhost.evil.example/")
    assert not base.is_local_url("http://127/") and not base.is_local_url("not a url")
    assert base.is_local_url("http://127.0.0.2:11434/v1") and base.is_local_url("http://0.0.0.0:8000/") and base.is_local_url("http://[::1]/")


# ---------------------------------------------------------------- fastembed (in-process)

class _FakeTextEmbedding:
    """Stand-in for fastembed.TextEmbedding: records construction, returns 3-dim vectors."""
    made = []

    def __init__(self, model_name, cache_dir=None, local_files_only=False, lazy_load=False, **kw):
        self.made.append((model_name, cache_dir, local_files_only))
        if local_files_only and not (os.path.isdir(cache_dir) and os.listdir(cache_dir)):
            raise ValueError("model not in cache")

    seen = []

    def embed(self, documents, batch_size=256):
        import numpy
        for d in documents:
            self.seen.append(d)
            yield numpy.array([len(d), 0.5, -1.0])


@pytest.fixture
def fake_fastembed(monkeypatch):
    import types
    mod = types.ModuleType("fastembed")
    _FakeTextEmbedding.made = []
    _FakeTextEmbedding.seen = []
    mod.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    monkeypatch.setattr(local, "installed", lambda: True)
    monkeypatch.setattr(local, "_MODELS", {})
    return _FakeTextEmbedding


def test_fastembed_embeds_truncates_normalises_and_reuses_one_loaded_model(fake_fastembed, tmp_path):
    emb = local.FastEmbed({"model": "m", "cache_dir": str(tmp_path), "max_chars": 4, "dim": 3})
    res = emb.embed(["abcdefgh", "xy"])
    assert res.error is None
    n = math.sqrt(16 + 0.25 + 1)
    assert res.vectors[0] == pytest.approx([4 / n, 0.5 / n, -1 / n])         # truncated to 4 chars, unit length
    assert res.vectors[1] == pytest.approx([2 / math.sqrt(5.25), 0.5 / math.sqrt(5.25), -1 / math.sqrt(5.25)])
    emb.embed(["again"])
    assert fake_fastembed.made == [("m", str(tmp_path), False)]      # loaded once per process
    assert emb.fingerprint == "fastembed|m|dim=3|trunc=4|unit+task"
    assert local.FastEmbed({"model": "m", "cache_dir": str(tmp_path), "dim": 2}).embed(["x"]).error == "config"


def test_fastembed_prefixes_nomic_by_purpose_and_truncates_before_prefixing(fake_fastembed, tmp_path):
    emb = local.FastEmbed({"model": "nomic-ai/nomic-embed-text-v1.5-Q", "cache_dir": str(tmp_path), "max_chars": 3})
    emb.embed(["abcdef"]); emb.embed(["abcdef"], purpose="query")
    assert fake_fastembed.seen == ["search_document: abc", "search_query: abc"]
    other = local.FastEmbed({"model": "BAAI/bge-small-en-v1.5", "cache_dir": str(tmp_path)})
    other.embed(["abc"], purpose="query")
    assert fake_fastembed.seen[-1] == "abc"                             # only nomic models take a prefix


def test_unit_rejects_zero_and_non_finite_vectors():
    assert local._unit([3.0, 4.0]) == [0.6, 0.8]
    assert local._unit([0.0, 0.0]) is None and local._unit([float("nan"), 1.0]) is None


def test_fastembed_load_failure_is_a_provider_error_not_an_exception(fake_fastembed, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("no network")
    monkeypatch.setattr(fake_fastembed, "__init__", boom)
    res = local.FastEmbed({"model": "m", "cache_dir": str(tmp_path)}).embed(["x", "y"])
    assert res.error == "provider" and "no network" in res.detail and res.vectors == [None, None]


def test_fastembed_model_cached_never_downloads(fake_fastembed, tmp_path):
    emb = local.FastEmbed({"model": "m", "cache_dir": str(tmp_path / "models")})
    assert emb.model_cached() is False
    (tmp_path / "models" / "x").mkdir(parents=True)
    assert emb.model_cached() is True
    assert all(made[2] for made in fake_fastembed.made)                   # local_files_only every time


def test_validate_applies_sibling_constraints_and_rejects_bool_as_number():
    assert base.validate(None, {"type": ["string", "null"], "enum": ["a"]})
    assert base.validate(True, {"type": ["integer", "null"]})
    assert base.validate({}, {"anyOf": [{"type": "object"}, {"type": "null"}], "required": ["x"]})
    assert base.validate({"x": 1}, {"anyOf": [{"type": "object"}, {"type": "null"}], "required": ["x"]}) is None
    assert base.validate(None, {"type": ["string", "null"], "enum": ["a", None]}) is None


def test_claude_cli_runs_without_tools_or_sessions(monkeypatch):
    env = {"type": "result", "is_error": False, "result": "x"}
    cmds = _fake_run(monkeypatch, lambda cmd: _Res(0, json.dumps(env)))
    cli.ClaudeCLI().complete(LLMRequest(prompt="P", model="m"))
    cmd = cmds[0]
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd
    assert "--disable-slash-commands" in cmd and "--no-session-persistence" in cmd


def test_cli_backends_never_raise(monkeypatch):
    def missing(cmd):
        raise cli._Missing("No such file: claude")
    _fake_run(monkeypatch, missing)
    for backend in (cli.ClaudeCLI(), cli.CodexCLI()):
        r = backend.complete(LLMRequest(prompt="P", model="m"))
        assert not r.ok and r.error == "config" and "No such file" in r.detail
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/agy")
    r = cli.GeminiCLI().complete(LLMRequest(prompt="P", model="m"))
    assert not r.ok and r.error == "config"
    _fake_run(monkeypatch, lambda cmd: _Res(0, "[]"))                       # valid JSON, wrong shape
    for backend in (cli.ClaudeCLI(), cli.GeminiCLI()):
        r = backend.complete(LLMRequest(prompt="P", model="m"))
        assert not r.ok and r.error == "provider" and "envelope" in r.detail
    r = cli.CodexCLI().complete(LLMRequest(prompt="P", model="m"))
    assert not r.ok and r.error == "malformed"                               # no agent message at all
