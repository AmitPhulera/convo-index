"""Provider backends against recorded CLI/HTTP envelopes (no processes, no network)."""


from convo_index.providers import base
from convo_index.providers.base import LLMRequest

SCHEMA = {"type": "object", "properties": {"category": {"type": "string", "enum": ["a", "b"]}},
          "required": ["category"]}


class _Res:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


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


# ---------------------------------------------------------------- gemini_cli (agy)


# ---------------------------------------------------------------- codex_cli


# ---------------------------------------------------------------- http backends


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


def test_validate_applies_sibling_constraints_and_rejects_bool_as_number():
    assert base.validate(None, {"type": ["string", "null"], "enum": ["a"]})
    assert base.validate(True, {"type": ["integer", "null"]})
    assert base.validate({}, {"anyOf": [{"type": "object"}, {"type": "null"}], "required": ["x"]})
    assert base.validate({"x": 1}, {"anyOf": [{"type": "object"}, {"type": "null"}], "required": ["x"]}) is None
    assert base.validate(None, {"type": ["string", "null"], "enum": ["a", None]}) is None
