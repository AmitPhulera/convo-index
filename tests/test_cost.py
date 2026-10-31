"""Cost estimates (docs/design.md §10): exact ids and aliases only, two separate numbers, coverage."""
from convo_index import cost as K
from convo_index import db as dbm
from convo_index.model import Conversation, Message, UsageRecord
from tests.conftest import make_settings


def test_seed_pricing_loads_and_prices_known_models(tmp_path):
    s = make_settings(tmp_path)
    p = K.load(s)
    assert p.revision and p.user_path is None
    r = p.rates("claude-haiku-4-5-20251001")           # alias -> exact id
    assert r == {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_write": 1.25, "cache_write_1h": 2.0}
    assert p.rates("claude-haiku-4-5") == r
    assert p.rates("claude-haiku-4-5-2099") is None      # no prefix guessing
    assert p.rates("gemini-3.8-flash")["cache_write"] == 0.75      # missing cache_write -> input rate
    assert p.rates("qwen3.8:27b-mlx", provider="ollama") == {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "cache_write_1h": 0.0}
    assert p.rates(None) is None
    est = p.estimate("gpt-6-astra", "openai", {"input_uncached": 1_000_000, "cache_read": 1_000_000, "output": 100_000})
    assert abs(est - (10 + 1 + 5)) < 1e-9
    # Opus 5.5: cache reads at 0.05x input (not 0.1x), 1-hour writes at 2x
    assert p.rates("claude-opus-5-5") == {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0, "cache_write_1h": 8.0}


def test_user_pricing_file_overrides_and_extends(tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    up = tmp_path / "pricing.toml"
    up.write_text('revision = "mine"\nzero_cost_providers = ["vllm"]\n[models]\n"claude-haiku-4-5" = { input = 2.0, output = 8.0 }\n'
                  '"my-model" = { input = 1.0, output = 1.0 }\n[aliases]\n"m1" = "my-model"\n')
    s["pricing"] = {"file": str(up)}
    p = K.load(s)
    assert p.revision == "mine" and p.user_path == up
    assert p.rates("claude-haiku-4-5") == {"input": 2.0, "output": 8.0, "cache_read": 2.0, "cache_write": 2.0, "cache_write_1h": 2.0}
    assert p.rates("m1")["output"] == 1.0 and p.rates("x", "vllm")["input"] == 0.0
    assert p.rates("gpt-6-astra")["input"] == 10.0                   # seed entries still there


def _seed(path):
    conn = dbm.connect(path); dbm.init_schema(conn)
    conv = Conversation(source="claude_code", native_id="s1", title="T", first_prompt="fp", source_ref="/a", cwd="/x",
                        start_ts="2026-06-16T00:00:00Z",
                        messages=[Message(seq=0, role="user", text="hi"), Message(seq=1, role="assistant", text="yo"),
                                  Message(seq=2, role="assistant", text="more")],
                        usage=[UsageRecord("r1", 1, "anthropic", "claude-haiku-4-5", input_uncached=1000, cache_read=10_000,
                                           cache_write=2000, output=500),
                               UsageRecord("r2", 2, "anthropic", "mystery-model", input_uncached=1000, output=100),
                               UsageRecord("r3", None, "anthropic", "<synthetic>", input_uncached=0, output=0),
                               UsageRecord("r4", None, "opencode", "x-free", input_uncached=5, output=5, reported_cost_usd=0.0)])
    dbm.upsert_conversation(conn, conv, "unknown", None, [], None, "1", lambda t: [None] * len(t))
    return conn


def test_conversation_usage_separates_reported_and_estimated_with_coverage(tmp_path):
    conn = _seed(tmp_path / "t.db")
    u = K.usage_for_conversation(conn, "claude_code:s1", K.load(make_settings(tmp_path)))
    assert u["requests"] == 4 and u["tokens"] == 1000 + 10_000 + 2000 + 500 + 1100 + 10
    # haiku: 1000*1 + 10000*0.1 + 2000*1.25 + 500*5 = 1000+1000+2500+2500 = 7000 per MTok -> $0.007
    assert abs(u["estimated_cost"] - 0.007) < 1e-6
    assert u["reported_cost"] == 0.0 and u["reported_requests"] == 1          # opencode said 0, which is a value
    # 3 token-bearing requests (the synthetic one has none): haiku priced, mystery-model and x-free not
    assert u["token_requests"] == 3 and u["priced_requests"] == 1 and u["coverage"] == round(1 / 3, 3)
    assert u["unpriced_models"] == ["mystery-model", "x-free"]
    assert u["models"] == ["<synthetic>", "claude-haiku-4-5", "mystery-model", "x-free"]
    assert [d["model"] for d in K.unpriced_models(conn, K.load(make_settings(tmp_path)))] == ["mystery-model", "x-free"]


def test_formatters():
    assert K.fmt_usd(None) == "—" and K.fmt_usd(0.00123) == "$0.0012" and K.fmt_usd(1234.5) == "$1,234.50"
    assert K.fmt_tokens(999) == "999" and K.fmt_tokens(12_300) == "12.3k" and K.fmt_tokens(2_500_000) == "2.5M"


def test_one_hour_cache_writes_are_priced_at_their_own_rate(tmp_path):
    from convo_index.sources import claude_code as cc
    d = {"type": "assistant", "requestId": "r", "timestamp": "t", "message": {"model": "claude-fable-5-1", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 1_000_000, "output_tokens": 1,
        "cache_creation": {"ephemeral_5m_input_tokens": 100_000, "ephemeral_1h_input_tokens": 900_000}}}}
    rec = cc._usage_record(d, 0)
    assert rec.cache_write == 100_000 and rec.cache_write_1h == 900_000
    p = K.load(make_settings(tmp_path))
    tokens = {"input_uncached": 0, "cache_read": 0, "cache_write": rec.cache_write, "cache_write_1h": rec.cache_write_1h, "output": 0}
    assert abs(p.estimate("claude-fable-5-1", "anthropic", tokens) - (0.1 * 12.5 + 0.9 * 20.0)) < 1e-9   # not 1.0 * 12.5
    # no breakdown -> the total counts as 5-minute writes; a breakdown that does not add up -> trust the total
    d["message"]["usage"].pop("cache_creation")
    assert (cc._usage_record(d, 0).cache_write, cc._usage_record(d, 0).cache_write_1h) == (1_000_000, None)
    d["message"]["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 1}
    assert (cc._usage_record(d, 0).cache_write, cc._usage_record(d, 0).cache_write_1h) == (1_000_000, None)


def test_broken_or_invalid_user_pricing_is_reported_not_swallowed(tmp_path):
    s = make_settings(tmp_path)
    up = tmp_path / "pricing.toml"; s["pricing"] = {"file": str(up)}
    up.write_text('[models]\n"claude-haiku-4-5" = 3\n"good" = { input = 1.0, output = 2.0 }\n"nooutput" = { input = 1.0 }\n[aliases]\n"x" = "nowhere"\n')
    p = K.load(s)
    assert p.rates("claude-haiku-4-5")["input"] == 1.0                    # bad override dropped, seed kept
    assert p.rates("good")["output"] == 2.0 and p.rates("nooutput") is None and p.rates("x") is None
    assert len(p.errors) == 3 and any("must be a table" in e for e in p.errors) and any("unknown model" in e for e in p.errors)
    up.write_text("this is = not toml [[[")
    p = K.load(s)
    assert p.errors and "pricing.toml" in p.errors[0] and p.rates("gpt-6-astra")["input"] == 10.0


def test_coverage_counts_only_token_bearing_requests_within_a_group(tmp_path):
    p = K.load(make_settings(tmp_path))
    rows = [("anthropic", "claude-haiku-4-5", 10, 100, 0, 0, 50, 0, None, 0, 0, 1),   # 1 real request, 9 token-less
            ("x", "mystery", 1, 100, 0, 0, 50, 0, None, 0, 0, 1)]
    out = K.aggregate(rows, p)
    assert out["token_requests"] == 2 and out["priced_requests"] == 1 and out["coverage"] == 0.5
    assert out["requests"] == 11


def test_alias_with_a_non_string_target_is_reported_not_fatal(tmp_path):
    s = make_settings(tmp_path)
    up = tmp_path / "pricing.toml"; s["pricing"] = {"file": str(up)}
    up.write_text('[aliases]\nalias = ["m"]\n')
    p = K.load(s)
    assert len(p.errors) == 1 and "alias" in p.errors[0] and p.rates("claude-haiku-4-5")["input"] == 1.0
