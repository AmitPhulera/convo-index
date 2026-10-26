"""HTTP backends: Anthropic Messages API and any OpenAI-compatible server
(Ollama, OpenAI, OpenRouter, LM Studio, vLLM). stdlib urllib only."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from convo_index.model import UsageRecord
from convo_index.providers.base import LLMRequest, LLMResult, EmbedResult, finish, strict_schema, is_local_url


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _http_error(e, backend):
    if isinstance(e, urllib.error.HTTPError):
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        kind = "auth" if e.code in (401, 403) else "rate_limit" if e.code == 429 else "provider"
        return LLMResult(ok=False, error=kind, detail=f"HTTP {e.code} {body}", backend=backend)
    if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
        return LLMResult(ok=False, error="timeout", detail=str(e), backend=backend)
    return LLMResult(ok=False, error="provider", detail=str(e)[:500], backend=backend)


def _key(spec, default_env):
    """API key from the environment. An explicit `api_key_env = ""` means "send no key":
    a user pointing at an unauthenticated internal server must not leak OPENAI_API_KEY to it.
    Only when the key is absent from the spec do we fall back to the conventional variable."""
    env = spec["api_key_env"] if "api_key_env" in spec and spec["api_key_env"] is not None else default_env
    return os.environ.get(env, "") if env else ""


class AnthropicAPI:
    name = "anthropic_api"

    def __init__(self, spec):
        self.spec = spec
        self.base_url = (spec.get("base_url") or "https://api.anthropic.com").rstrip("/")

    def available(self):
        return bool(_key(self.spec, "ANTHROPIC_API_KEY"))

    def complete(self, req: LLMRequest) -> LLMResult:
        key = _key(self.spec, "ANTHROPIC_API_KEY")
        if not key:
            return LLMResult(ok=False, error="auth", detail="ANTHROPIC_API_KEY not set", backend=self.name)
        body = {"model": req.model, "max_tokens": req.max_output_tokens,
                "messages": [{"role": "user", "content": req.prompt}]}
        if req.schema is not None:
            # Structured output via a forced tool call: the arguments are the answer.
            body["tools"] = [{"name": "answer", "description": "Return the requested JSON.",
                              "input_schema": req.schema}]
            body["tool_choice"] = {"type": "tool", "name": "answer"}
        try:
            env = _post(f"{self.base_url}/v1/messages", body,
                        {"x-api-key": key, "anthropic-version": "2023-06-01"}, req.timeout)
        except Exception as e:
            return _http_error(e, self.name)
        u = env.get("usage") or {}
        usage = UsageRecord(native_request_id=env.get("id"), provider="anthropic", model=env.get("model"),
                            input_uncached=u.get("input_tokens"), cache_read=u.get("cache_read_input_tokens"),
                            cache_write=u.get("cache_creation_input_tokens"), output=u.get("output_tokens"))
        structured, text = None, ""
        for block in env.get("content") or []:
            if block.get("type") == "tool_use":
                structured = block.get("input")
            elif block.get("type") == "text":
                text += block.get("text", "")
        return finish(req, text, usage=usage, model_used=env.get("model"), backend=self.name,
                      structured=structured)


class OpenAICompat:
    """Chat + embeddings against `<base_url>/chat/completions` and `<base_url>/embeddings`."""
    name = "openai_compat"

    def __init__(self, spec):
        self.spec = spec
        self.base_url = (spec.get("base_url") or "http://localhost:11434/v1").rstrip("/")

    def _headers(self):
        key = _key(self.spec, "" if is_local_url(self.base_url) else "OPENAI_API_KEY")
        return {"Authorization": f"Bearer {key}"} if key else {}

    def available(self):
        try:
            req = urllib.request.Request(f"{self.base_url}/models", headers=self._headers())
            with urllib.request.urlopen(req, timeout=3):
                return True
        except Exception:
            return False

    def complete(self, req: LLMRequest) -> LLMResult:
        body = {"model": req.model, "max_tokens": req.max_output_tokens,
                "messages": [{"role": "user", "content": req.prompt}]}
        if req.schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "answer", "strict": True,
                                                       "schema": strict_schema(req.schema)}}
        try:
            env = _post(f"{self.base_url}/chat/completions", body, self._headers(), req.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 400 and req.schema is not None:
                # Older servers know json_object but not json_schema: degrade and validate ourselves.
                body["response_format"] = {"type": "json_object"}
                try:
                    env = _post(f"{self.base_url}/chat/completions", body, self._headers(), req.timeout)
                except Exception as e2:
                    return _http_error(e2, self.name)
            else:
                return _http_error(e, self.name)
        except Exception as e:
            return _http_error(e, self.name)
        choice = (env.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content")) or ""
        u = env.get("usage") or {}
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        pt = u.get("prompt_tokens")
        usage = UsageRecord(native_request_id=env.get("id"), provider=self.spec.get("provider") or "openai_compat",
                            model=env.get("model") or req.model,
                            input_uncached=(pt - cached) if pt is not None else None, cache_read=cached or None,
                            output=u.get("completion_tokens"),
                            reasoning=((u.get("completion_tokens_details") or {}).get("reasoning_tokens")))
        return finish(req, text, usage=usage, model_used=env.get("model") or req.model, backend=self.name)

    # ---- embeddings -----------------------------------------------------------------
    @property
    def fingerprint(self):
        return f"{self.name}|{self.base_url}|{self.spec.get('model')}|dim={self.spec.get('dim')}|trunc={self.spec.get('max_chars')}"

    def embed(self, texts, batch_size=32, purpose="document") -> EmbedResult:
        # `purpose` is accepted for interface parity; the server decides any task prefixing.
        model = self.spec.get("model")
        max_chars = int(self.spec.get("max_chars") or 8000)
        dim = int(self.spec.get("dim") or 0)
        vectors: list[list[float] | None] = []
        for i in range(0, len(texts), batch_size):
            batch = [(t or " ")[:max_chars] for t in texts[i:i + batch_size]]
            try:
                env = _post(f"{self.base_url}/embeddings", {"model": model, "input": batch},
                            self._headers(), 120)
            except Exception as e:
                err = _http_error(e, self.name)
                return EmbedResult(vectors=[None] * len(texts), fingerprint=self.fingerprint,
                                   error=err.error, detail=err.detail)
            data = sorted(env.get("data") or [], key=lambda d: d.get("index", 0))
            if len(data) != len(batch):
                return EmbedResult(vectors=[None] * len(texts), fingerprint=self.fingerprint,
                                   error="provider", detail=f"got {len(data)} vectors for {len(batch)} inputs")
            for d in data:
                v = d.get("embedding")
                if dim and v is not None and len(v) != dim:
                    return EmbedResult(vectors=[None] * len(texts), fingerprint=self.fingerprint,
                                       error="config", detail=f"model returned {len(v)} dims, config says {dim}")
                vectors.append(v)
        return EmbedResult(vectors=vectors, fingerprint=self.fingerprint)


HTTP_BACKENDS = {AnthropicAPI.name: AnthropicAPI, OpenAICompat.name: OpenAICompat}
