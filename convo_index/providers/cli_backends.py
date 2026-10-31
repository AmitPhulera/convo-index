"""LLM backends that shell out to a locally installed coding CLI and reuse its login.

Envelopes verified on 2026-09-16:

  claude -p --output-format json [--json-schema S]
      {"type":"result","subtype":"success","is_error":false,"result":"<text or ```json fenced>",
       "usage":{"input_tokens","cache_creation_input_tokens","cache_read_input_tokens","output_tokens",...},
       "modelUsage":{"<model>":{...,"costUSD":...}},"total_cost_usd":...,"session_id":...}
  agy --print=P --output-format json [--json-schema S]           (Antigravity CLI, Gemini)
      {"status":"SUCCESS","response":"<text>","structured_output":{...},
       "usage":{"input_tokens","output_tokens","thinking_tokens","cache_read_tokens","total_tokens"}}
  codex exec --ephemeral --json [--output-schema S] -o LAST P
      JSONL events: item.completed {"item":{"type":"agent_message","text":...}},
      turn.completed {"usage":{"input_tokens","cached_input_tokens","cache_write_input_tokens",
      "output_tokens","reasoning_output_tokens"}}; LAST holds the final message text.
      Strict schemas need additionalProperties=false on every object.

The prompt goes to Claude Code and Codex on stdin: as an argument it would hit the operating
system's limit (128 KiB per argument on Linux, 32,767 characters for the whole command line on
Windows) long before the model's. agy takes it only as an argument (`--print=` reads no stdin),
so its prompt is bounded by `GeminiCLI.max_prompt_bytes()`.

The prompt is untrusted text (a transcript), so every call runs with the agent's tools,
MCP servers and slash commands disabled and from the temp dir, where no project settings or
instructions files are picked up. Claude Code is told not to persist the session and Codex
runs ephemeral; the adapters additionally exclude any session that still carries the prompt
marker. Backends never raise: a missing binary or an unexpected envelope is an LLMResult.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from convo_index.model import UsageRecord
from convo_index.providers.base import LLMRequest, LLMResult, finish, strict_schema


class _Missing(Exception):
    """The CLI could not be started (not installed, not executable)."""


def _run(cmd, timeout, stdin=None):
    """Completed process, or None on timeout. Runs from the temp dir so the CLI loads no
    project settings or instructions from this checkout."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=tempfile.gettempdir(), input=stdin)
    except subprocess.TimeoutExpired:
        return None
    except (FileNotFoundError, OSError) as e:
        raise _Missing(str(e)) from e


def _envelope(stdout, backend):
    """Parsed JSON object from the CLI's stdout, or the LLMResult that explains why not."""
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        return None, LLMResult(ok=False, error="provider", detail=stdout[-300:], backend=backend)
    if not isinstance(env, dict):
        return None, LLMResult(ok=False, error="provider", detail=f"unexpected envelope: {stdout[:200]}", backend=backend)
    return env, None


def _classify_failure(res, backend):
    if res is None:
        return LLMResult(ok=False, error="timeout", backend=backend)
    text = (res.stderr or "") + (res.stdout or "")
    low = text.lower()
    if any(k in low for k in ("not logged in", "unauthorized", "authentication", "login required",
                              "invalid api key", "401")):
        return LLMResult(ok=False, error="auth", detail=text[-500:], backend=backend)
    if any(k in low for k in ("rate limit", "429", "overloaded", "quota")):
        return LLMResult(ok=False, error="rate_limit", detail=text[-500:], backend=backend)
    return LLMResult(ok=False, error="provider", detail=text[-500:] or f"exit {res.returncode}",
                     backend=backend)


class ClaudeCLI:
    name = "claude_cli"
    binary = "claude"

    @classmethod
    def available(cls):
        return shutil.which(cls.binary) is not None

    def complete(self, req: LLMRequest) -> LLMResult:
        # No tools, no MCP servers, no slash commands, no session on disk: the transcript in
        # the prompt must not be able to make the agent act.
        cmd = ["claude", "-p", "--model", req.model, "--output-format", "json", "--tools", "",
               "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]
        if req.schema is not None:
            cmd += ["--json-schema", json.dumps(req.schema)]
        try:
            res = _run(cmd, req.timeout, stdin=req.marker + req.prompt)   # -p with no argument reads stdin
        except _Missing as e:
            return LLMResult(ok=False, error="config", detail=str(e), backend=self.name)
        if res is None or res.returncode != 0:
            return _classify_failure(res, self.name)
        env, err = _envelope(res.stdout, self.name)
        if err:
            return err
        if env.get("is_error"):
            return _classify_failure(res, self.name)
        u = env.get("usage") or {}
        usage = UsageRecord(native_request_id=env.get("session_id"), provider="anthropic",
                            model=next(iter(env.get("modelUsage") or {}), req.model),
                            input_uncached=u.get("input_tokens"),
                            cache_read=u.get("cache_read_input_tokens"),
                            cache_write=u.get("cache_creation_input_tokens"),
                            output=u.get("output_tokens"),
                            reported_cost_usd=env.get("total_cost_usd")) if u else None
        # With --json-schema the answer may arrive parsed under "structured_output" or as text.
        return finish(req, env.get("result"), usage=usage, model_used=usage.model if usage else req.model,
                      backend=self.name, structured=env.get("structured_output"))


class GeminiCLI:
    """Antigravity CLI (`agy`, Google-hosted Gemini). Runs sandboxed; headless mode denies
    every tool, so a transcript that asks the model to act gets an empty answer, not an action."""
    name = "gemini_cli"
    binary = "agy"

    @staticmethod
    def max_prompt_bytes():
        """What `--print=<prompt>` can carry, in UTF-8 bytes (never fewer than the units the OS
        counts): Linux caps one argument at 128 KiB, Windows the whole command line at 32,767
        characters, which the model, schema and flags share."""
        return 28_000 if os.name == "nt" else 120_000

    @classmethod
    def available(cls):
        return shutil.which(cls.binary) is not None

    def complete(self, req: LLMRequest) -> LLMResult:
        if not self.available():
            return LLMResult(ok=False, error="config", detail="agy (Antigravity CLI) is not on PATH", backend=self.name)
        cmd = ["agy", "--model", req.model, "--sandbox", "--disable-slash-commands",
               "--output-format", "json", "--print-timeout", f"{int(req.timeout)}s"]
        if req.schema is not None:
            cmd += ["--json-schema", json.dumps(req.schema)]
        size = len((req.marker + req.prompt).encode("utf-8", "surrogatepass"))
        if size > self.max_prompt_bytes():
            return LLMResult(ok=False, error="config", backend=self.name,
                             detail=f"a {size:,}-byte prompt does not fit agy's command line "
                                    f"(at most {self.max_prompt_bytes():,}; agy reads no prompt from stdin)")
        cmd.append("--print=" + req.marker + req.prompt)
        try:
            res = _run(cmd, req.timeout + 10)
        except _Missing as e:
            return LLMResult(ok=False, error="config", detail=str(e), backend=self.name)
        if res is None or res.returncode != 0:
            return _classify_failure(res, self.name)
        env, err = _envelope(res.stdout, self.name)
        if err:
            return err
        if env.get("status") not in (None, "SUCCESS"):
            return LLMResult(ok=False, error="provider", detail=str(env.get("status")), backend=self.name)
        denied = [d.get("display_name") or d.get("action") or "?" for d in env.get("denied_actions") or []
                  if isinstance(d, dict)]
        if denied and not (env.get("response") or env.get("structured_output")):
            # The model tried to act instead of answering; the sandbox refused the tool and the
            # CLI still reports SUCCESS with an empty response. Name the tool so doctor can say so.
            return LLMResult(ok=False, error="malformed", backend=self.name, model_used=req.model,
                             detail=f"no answer: the model tried to use {', '.join(denied)} and headless mode denied it")
        u = env.get("usage") or {}
        usage = UsageRecord(native_request_id=env.get("conversation_id"), provider="google", model=req.model,
                            input_uncached=u.get("input_tokens"), cache_read=u.get("cache_read_tokens"),
                            output=u.get("output_tokens"), reasoning=u.get("thinking_tokens")) if u else None
        return finish(req, env.get("response"), usage=usage, model_used=req.model, backend=self.name,
                      structured=env.get("structured_output"))


class CodexCLI:
    name = "codex_cli"
    binary = "codex"

    @classmethod
    def available(cls):
        return shutil.which(cls.binary) is not None

    def complete(self, req: LLMRequest) -> LLMResult:
        with tempfile.TemporaryDirectory(prefix="convo-index-codex-") as td:
            last = os.path.join(td, "last.txt")
            cmd = ["codex", "exec", "--model", req.model, "--ephemeral", "-s", "read-only",
                   "--skip-git-repo-check", "--json", "-o", last]
            if req.schema is not None:
                sp = os.path.join(td, "schema.json")
                with open(sp, "w", encoding="utf-8") as fh:
                    json.dump(strict_schema(req.schema), fh)
                cmd += ["--output-schema", sp]
            try:
                res = _run(cmd, req.timeout, stdin=req.marker + req.prompt)   # no PROMPT argument: read from stdin
            except _Missing as e:
                return LLMResult(ok=False, error="config", detail=str(e), backend=self.name)
            if res is None or res.returncode != 0:
                return _classify_failure(res, self.name)
            usage = None
            text = None
            for line in res.stdout.splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "turn.completed":
                    u = ev.get("usage") or {}
                    inp = u.get("input_tokens")
                    cached = u.get("cached_input_tokens") or 0
                    usage = UsageRecord(native_request_id=None, provider="openai", model=req.model,
                                        input_uncached=(inp - cached) if inp is not None else None,
                                        cache_read=cached, cache_write=u.get("cache_write_input_tokens"),
                                        output=u.get("output_tokens"), reasoning=u.get("reasoning_output_tokens"))
                elif ev.get("type") == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                    text = ev["item"].get("text")
                elif ev.get("type") in ("error", "turn.failed"):
                    msg = json.dumps(ev.get("error") or ev.get("message") or ev)
                    kind = "auth" if "401" in msg or "unauthorized" in msg.lower() else \
                        "rate_limit" if "429" in msg or "rate" in msg.lower() else "provider"
                    return LLMResult(ok=False, error=kind, detail=msg[:500], backend=self.name)
            if text is None and os.path.exists(last):
                with open(last) as fh:
                    text = fh.read()
        return finish(req, text, usage=usage, model_used=req.model, backend=self.name)


CLI_BACKENDS = {ClaudeCLI.name: ClaudeCLI, GeminiCLI.name: GeminiCLI, CodexCLI.name: CodexCLI}
