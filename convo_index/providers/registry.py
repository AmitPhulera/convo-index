"""Pick backends from settings; probe installed CLIs for defaults.

Default resolution order (owner decision, 2026-09-16): gemini_cli -> codex_cli -> claude_cli.
An explicit `[llm].<job>` entry always wins. `init` writes the probe result into the config
file so the choice is visible; nothing is re-derived silently at run time.
"""
from __future__ import annotations

from convo_index.providers.base import LLMRequest, LLMResult, is_local_url
from convo_index.providers.cli_backends import CLI_BACKENDS, GeminiCLI, CodexCLI, ClaudeCLI
from convo_index.providers.http_backends import HTTP_BACKENDS, OpenAICompat
from convo_index.providers.local_backends import FastEmbed
from convo_index import settings as S

PROBE_ORDER = (GeminiCLI, CodexCLI, ClaudeCLI)
HOSTED_BACKENDS = {"gemini_cli", "codex_cli", "claude_cli", "anthropic_api"}


def probe_cli_backends():
    """Installed CLI backends in preference order."""
    return [cls.name for cls in PROBE_ORDER if cls.available()]


def default_backend():
    found = probe_cli_backends()
    return found[0] if found else ""


def usable(spec, settings=None):
    """Could this job's backend be called right now: configured, installed/keyed, and allowed
    by the privacy policy? Cheap (no network); the signature lets blocked jobs notice change."""
    backend = (spec or {}).get("backend") or ""
    if not backend:
        return False
    llm = make_llm(spec)
    return bool(llm and llm.available() and policy_allows(spec, settings))


# credential files the CLIs write on login: their mtime changes when the user logs in again,
# which is exactly when blocked jobs deserve a retry
CREDENTIAL_FILES = {
    "claude_cli": ("~/.claude/.credentials.json", "~/.claude.json"),
    "codex_cli": ("~/.codex/auth.json",),
    "gemini_cli": ("~/.gemini/oauth_creds.json", "~/.gemini/google_accounts.json", "~/.gemini/antigravity-cli/oauth_creds.json"),
}


def credential_stamp(backend, spec=None):
    """What a login or a new key changes: the CLI credential files' mtimes, or a digest of
    the configured API key (so replacing a bad key with a good one is a change)."""
    import hashlib
    import os
    from convo_index.providers.http_backends import _key
    stamps = []
    for f in CREDENTIAL_FILES.get(backend, ()):
        try:
            stamps.append(str(int(os.stat(os.path.expanduser(f)).st_mtime)))
        except OSError:
            stamps.append("-")
    env = {"anthropic_api": "ANTHROPIC_API_KEY", "openai_compat": "OPENAI_API_KEY"}.get(backend)
    if env:
        key = _key(spec or {}, env)
        stamps.append(hashlib.sha256(key.encode()).hexdigest()[:12] if key else "-")
    return ",".join(stamps)


def usability_signature(spec, settings=None):
    backend = (spec or {}).get("backend") or ""
    return (f"{backend}|{(spec or {}).get('model') or ''}|{policy_state(settings)}|{int(usable(spec, settings))}"
            f"|{credential_stamp(backend, spec)}")


def make_llm(spec):
    """Instantiate the backend named in a job spec, or None when unconfigured/unknown."""
    backend = (spec or {}).get("backend") or ""
    if backend in CLI_BACKENDS:
        return CLI_BACKENDS[backend]()
    if backend in HTTP_BACKENDS:
        return HTTP_BACKENDS[backend](spec)
    return None


def is_hosted(spec):
    backend = (spec or {}).get("backend") or ""
    if backend in HOSTED_BACKENDS:
        return True
    if backend == "openai_compat":
        return not is_local_url(spec.get("base_url") or "http://localhost:11434/v1")
    return False


def policy_state(settings=None):
    return (settings or S.load())["privacy"].get("hosted_processing", "ask")


def policy_allows(spec, settings=None):
    """Hosted backends run only under `hosted_processing = allow`. `deny` refuses them and so
    does the unresolved default `ask`: consent is given by an interactive `init`, by
    `init --yes`, or by editing the file, never implied by a non-interactive run."""
    settings = settings or S.load()
    return not is_hosted(spec) or policy_state(settings) == "allow"


def policy_reason(spec, settings=None):
    state = policy_state(settings)
    return (f"[privacy].hosted_processing = {state} forbids hosted backend {spec.get('backend')}"
            + ("; run `convo-index init` to decide" if state == "ask" else ""))


def complete(job, prompt, schema=None, settings=None, **kw) -> LLMResult:
    """Run one LLM call for a configured job. Never raises."""
    settings = settings or S.load()
    spec = settings.llm_job(job)
    llm = make_llm(spec)
    if llm is None:
        return LLMResult(ok=False, error="config",
                         detail=f"no backend configured for [llm].{job}; run `convo-index init`")
    if not spec.get("model"):
        return LLMResult(ok=False, error="config", detail=f"no model for [llm].{job}", backend=spec["backend"])
    if not policy_allows(spec, settings):
        return LLMResult(ok=False, error="config", detail=policy_reason(spec, settings), backend=spec["backend"])
    req = LLMRequest(prompt=prompt, model=spec["model"], schema=schema, **kw)
    if len(req.prompt) > req.max_input_chars:
        req.prompt = req.prompt[:req.max_input_chars] + "\n…[input truncated]"
    try:
        return llm.complete(req)
    except FileNotFoundError as e:
        return LLMResult(ok=False, error="config", detail=str(e), backend=spec["backend"])
    except Exception as e:  # a backend bug must never abort indexing
        return LLMResult(ok=False, error="provider", detail=f"{type(e).__name__}: {e}"[:500],
                         backend=spec["backend"])


def embedder_blocked_reason(settings=None):
    """Why no embedder may be used, or None. fastembed runs in-process, so only its install
    matters; the privacy policy applies to HTTP embedders because every indexed message and
    every search query goes to that server."""
    settings = settings or S.load()
    spec = dict(settings["embeddings"])
    backend = spec.get("backend") or "fastembed"
    if backend == "fastembed":
        if not FastEmbed.available():
            return ("fastembed is not installed: re-run install.sh (`uv tool install --reinstall`) or "
                    "`uv sync` in a checkout, or set [embeddings].backend = \"openai_compat\" for an "
                    "Ollama / OpenAI-compatible server")
        return None
    if backend != "openai_compat":
        return f"unsupported embeddings backend {backend!r}"
    if not policy_allows(spec, settings):
        return f"[privacy].hosted_processing = {policy_state(settings)} forbids embeddings at {spec.get('base_url')}"
    return None


def make_embedder(settings=None):
    settings = settings or S.load()
    if embedder_blocked_reason(settings):
        return None
    spec = dict(settings["embeddings"])
    return FastEmbed(spec) if (spec.get("backend") or "fastembed") == "fastembed" else OpenAICompat(spec)
