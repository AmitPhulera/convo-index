"""Provider interfaces: one small contract for chat completions, one for embeddings.

Every backend returns a typed result rather than raising, so callers (the job runner)
can record the error category and decide whether to retry:
  auth        -> never retry, tell the user
  rate_limit  -> retry later
  timeout     -> retry later
  malformed   -> the model answered but not in the requested shape; retried within the job budget
  provider    -> anything else (process failed, HTTP 5xx)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from convo_index.model import UsageRecord

RETRYABLE = {"rate_limit", "timeout", "provider", "malformed"}


@dataclass
class LLMRequest:
    prompt: str
    model: str
    schema: dict | None = None            # JSON schema for the answer; None = free text
    max_output_tokens: int = 1024    # HTTP backends only: the CLIs expose no output cap
    max_input_chars: int = 60_000    # enforced once, in registry.complete, before any backend
    timeout: float = 180.0
    marker: str = ""                      # prompt prefix the adapters use to exclude our own sessions


@dataclass
class LLMResult:
    ok: bool
    data: dict | str | None = None        # parsed object when a schema was given, else text
    error: str | None = None              # auth | rate_limit | timeout | malformed | provider | config
    detail: str | None = None
    model_used: str | None = None
    usage: UsageRecord | None = None
    backend: str | None = None

    @property
    def retryable(self):
        return not self.ok and self.error in RETRYABLE


@dataclass
class EmbedResult:
    vectors: list[list[float] | None]
    fingerprint: str
    error: str | None = None
    detail: str | None = None



_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text):
    """Best-effort: strip code fences, then take the outermost {...}."""
    if not isinstance(text, str):
        return None
    t = _FENCE.sub("", text.strip())
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end < start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None


_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool,
          "array": list, "object": dict}


def _is_type(data, name):
    """JSON-schema type check; a bool is never a number/integer."""
    if name not in _TYPES:
        return True
    if isinstance(data, bool):
        return name == "boolean"
    return isinstance(data, _TYPES[name])


def validate(data, schema):
    """Minimal JSON-schema check (type, anyOf, required, enum, properties, items) — enough to
    catch truncated or off-shape answers without a dependency. Returns an error string or None."""
    if schema is None:
        return None
    if "anyOf" in schema:
        errs = [validate(data, sub) for sub in schema["anyOf"]]
        if not any(e is None for e in errs):
            return "matches none of anyOf: " + "; ".join(e for e in errs if e)
        schema = {k: v for k, v in schema.items() if k != "anyOf"}   # the siblings still apply
    t = schema.get("type")
    allowed = set(t) if isinstance(t, list) else ({t} if t else set())
    if data is None:
        if allowed and "null" not in allowed:
            return f"expected {t}, got null"
    elif allowed == {"null"}:
        return f"expected null, got {type(data).__name__}"
    elif allowed and not any(_is_type(data, x) for x in allowed if x != "null"):
        return f"expected {t}, got {type(data).__name__}"
    if "enum" in schema and data not in schema["enum"]:
        return f"{data!r} not in {schema['enum']}"
    if isinstance(data, dict):
        for k in schema.get("required", []):
            if k not in data:
                return f"missing {k!r}"
        for k, sub in (schema.get("properties") or {}).items():
            if k in data:
                err = validate(data[k], sub)
                if err:
                    return f"{k}: {err}"
    if isinstance(data, list) and "items" in schema:
        for i, item in enumerate(data):
            err = validate(item, schema["items"])
            if err:
                return f"[{i}]: {err}"
    return None


def strict_schema(schema):
    """OpenAI-style strict mode needs additionalProperties=false on every object."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if out.get("type") == "object":
        out.setdefault("additionalProperties", False)
        if "properties" in out:
            out["properties"] = {k: strict_schema(v) for k, v in out["properties"].items()}
            out.setdefault("required", list(out["properties"].keys()))
    if "items" in out:
        out["items"] = strict_schema(out["items"])
    if "anyOf" in out:
        out["anyOf"] = [strict_schema(x) for x in out["anyOf"]]
    return out


def finish(req: LLMRequest, text, usage=None, model_used=None, backend=None, structured=None):
    """Turn a backend's raw answer into an LLMResult, validating against the schema."""
    if req.schema is None:
        if text is None:
            return LLMResult(ok=False, error="malformed", detail="no answer", usage=usage,
                             model_used=model_used, backend=backend)
        return LLMResult(ok=True, data=text, usage=usage, model_used=model_used, backend=backend)
    data = structured if isinstance(structured, dict) else extract_json(text)
    if data is None:
        return LLMResult(ok=False, error="malformed", detail=(text or "")[:300], usage=usage,
                         model_used=model_used, backend=backend)
    err = validate(data, req.schema)
    if err:
        return LLMResult(ok=False, error="malformed", detail=err, data=data, usage=usage,
                         model_used=model_used, backend=backend)
    return LLMResult(ok=True, data=data, usage=usage, model_used=model_used, backend=backend)


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_local_url(url):
    """True only when the URL's *hostname* is `localhost` or a loopback/unspecified IP
    address parsed as such. Substring or prefix checks would accept
    https://localhost.evil.example, https://evil.example/localhost or https://127.evil.example."""
    import ipaddress
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified
