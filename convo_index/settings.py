"""User configuration: `~/.config/convo-index/config.toml` (XDG), `CONVO_INDEX_CONFIG` overrides.

Every value has a default so the tool runs with no config file at all. `convo-index init`
writes a starter file after probing the machine. Sections:

  [paths]        db, archive
  [server]       host, port, allowed_hosts (extra Host names, e.g. a LAN name when bound to 0.0.0.0;
                 a bare name matches any port, "name:port" only that one; loopback always matches)
  [sources.*]    enabled, exclude_cwd (project paths or globs)
  [llm]          classify / summary = { backend, model, base_url?, api_key_env? }
  [embeddings]   backend (fastembed | openai_compat), model, cache_dir?, base_url?, api_key_env?, dim, max_chars
  [privacy]      hosted_processing = ask | allow | deny
  [category]     labels, descriptions, hints (path substrings per label) -> exclusive "workspace" tag group
  [[tag_groups]] name, exclusive, description         (advanced: several groups)
  [[tags]]       name, group, description, examples, rules = { path_contains, branch_prefix, regex, source }
  [tags_learning] few_shot_k, suggest_threshold, suggest_threshold_by_group, suggest_k, vectors_per_run
  [references]   ticket_regex, ticket_prefix_blocklist, ticket_url, github_hosts, default_branches
  [summary]      head_chars, tail_chars, max_attempts
  [ui.turns]     enabled, timeout_seconds, max_concurrent (continuing a conversation from the browser)
"""
from __future__ import annotations

import copy
import json
import os
import re
import tomllib
from datetime import datetime
from pathlib import Path

from convo_index import config as C

CONFIG_DIR = C.user_dir("config") / "convo-index"
CONFIG_PATH = Path(os.environ.get("CONVO_INDEX_CONFIG", CONFIG_DIR / "config.toml"))

DEFAULTS = {
    # Persisted defaults never include env overrides (CONVO_INDEX_DB is applied at run time).
    "paths": {"db": str(C.DATA_DIR / "convos.db"), "archive": str(C.DATA_DIR / "archive")},
    "server": {"host": C.HOST, "port": C.PORT, "allowed_hosts": []},
    "sources": {
        "claude_code": {"enabled": True, "exclude_cwd": []},
        "opencode": {"enabled": True, "exclude_cwd": []},
        "codex": {"enabled": True, "exclude_cwd": []},        # CODEX_HOME
        "antigravity": {"enabled": True, "exclude_cwd": []},  # ANTIGRAVITY_HOME
        "pi": {"enabled": True, "exclude_cwd": []},           # PI_CODING_AGENT_DIR, PI_CODING_AGENT_SESSION_DIR
    },
    # Per-job LLM backend. Empty backend = "not configured": `init` fills it from the probe
    # order gemini_cli -> codex_cli -> claude_cli; `doctor` warns when a job has none.
    "llm": {
        "classify": {"backend": "", "model": ""},
        "summary": {"backend": "", "model": ""},
    },
    # fastembed runs in-process (the default); openai_compat talks to Ollama or any
    # /v1/embeddings server. `init` picks whichever is present. Changing either needs `reembed`.
    "embeddings": {"backend": "fastembed", "model": C.FASTEMBED_MODEL, "cache_dir": "",
                   "base_url": C.OLLAMA_URL, "dim": C.EMBED_DIM, "api_key_env": "", "max_chars": 8000},
    # ask = unresolved: hosted backends are refused until `init` (interactive or --yes) or a
    # hand edit sets allow/deny. Localhost backends always work.
    "privacy": {"hosted_processing": "ask"},
    # Generic defaults; `init` seeds labels from an existing database's categories.
    "category": {
        "labels": ["work", "personal"],
        "descriptions": {"work": "work for my employer or clients",
                         "personal": "side projects, learning, personal life"},
        "hints": {"work": [], "personal": []},
    },
    "references": {"ticket_regex": C.TICKET_RE,
                   "ticket_prefix_blocklist": sorted(C.TICKET_PREFIX_BLOCKLIST),
                   "ticket_url": "https://example.atlassian.net/browse/{key}",
                   "github_hosts": ["github.com"],
                   "default_branches": ["main", "master", "develop", "HEAD"]},
    "summary": {"head_chars": C.SUMMARY_HEAD_CHARS, "tail_chars": C.SUMMARY_TAIL_CHARS, "max_attempts": 3,
                "per_run": 50},   # derived jobs (summaries + tagging) attempted per index run
    # Learning from manual tags (docs/design.md §8): nearest user decisions as few-shot examples, and
    # suggestions for similar conversations above the threshold. Nothing is auto-applied.
    "tags_learning": {"few_shot_k": 6, "suggest_threshold": 0.80, "suggest_threshold_by_group": {}, "suggest_k": 25,
                      "vectors_per_run": 200},
    # `convo-index schedule install`: daily refresh time (local).
    "schedule": {"hour": 21, "minute": 0},
    # Cost estimates: user pricing file (default ~/.config/convo-index/pricing.toml) overriding the seed.
    "pricing": {"file": ""},
    # Browser turns: the UI server runs one more turn of a conversation in its own tool. Off
    # switches the endpoint off; a turn is killed after timeout_seconds; at most max_concurrent
    # harness processes run at once.
    "ui": {"turns": {"enabled": True, "timeout_seconds": 1200, "max_concurrent": 2}},
}

# Cheap / capable model per CLI backend, used when a job names a backend but no model.
DEFAULT_MODELS = {
    "gemini_cli": {"classify": "gemini-3.8-flash-low", "summary": "gemini-3.8-flash-high"},
    "codex_cli": {"classify": "gpt-5.6-luna", "summary": "gpt-5.6-terra"},
    "claude_cli": {"classify": "claude-haiku-4-5", "summary": "claude-haiku-4-5"},
    "anthropic_api": {"classify": "claude-haiku-4-5", "summary": "claude-haiku-4-5"},
    "openai_compat": {"classify": "", "summary": ""},
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings(dict):
    """Nested dict with the defaults merged in. `s.section("llm", "summary")`."""

    path: Path | None = None

    def disabled_sources(self):
        """Sources with `[sources.<name>] enabled = false`: not indexed, and what was indexed
        before is never sent to a provider again."""
        return sorted(n for n, s in (self.get("sources") or {}).items()
                      if isinstance(s, dict) and s.get("enabled") is False)

    def section(self, *keys, default=None):
        cur = self
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def db_path(self):
        return Path(os.environ.get("CONVO_INDEX_DB", self["paths"]["db"])).expanduser()

    def archive_dir(self):
        return Path(os.environ.get("CONVO_INDEX_ARCHIVE", self["paths"]["archive"])).expanduser()

    def llm_job(self, job):
        """Backend/model for a job, falling back to the backend's default model."""
        spec = dict(self["llm"].get(job) or {})
        backend = spec.get("backend") or ""
        if backend and not spec.get("model"):
            spec["model"] = DEFAULT_MODELS.get(backend, {}).get(job, "")
        spec["backend"] = backend
        return spec


_cache: dict[str, Settings] = {}


def load(path=None, use_cache=True) -> Settings:
    path = Path(path) if path else CONFIG_PATH
    key = str(path)
    if use_cache and key in _cache:
        return _cache[key]
    data = {}
    if path.exists():
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    s = Settings(_merge(DEFAULTS, data))
    if s["embeddings"]["backend"] == "openai_compat" and "model" not in (data.get("embeddings") or {}):
        s["embeddings"]["model"] = C.EMBED_MODEL   # the default model name is fastembed's; Ollama's differs
    s.path = path if path.exists() else None
    if use_cache:
        _cache[key] = s
    return s


def reset_cache():
    _cache.clear()


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        # TOML basic strings share JSON's escapes for control chars/quotes; non-ASCII must stay
        # literal (json's default \\ud83d\\ude00 surrogate pairs are not valid TOML).
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(type(v))


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(k):
    """Bare keys only for [A-Za-z0-9_-]; anything else (spaces, dots) is a quoted key so a
    label such as "client work" or "acme.io" round-trips instead of corrupting the file."""
    k = str(k)
    return k if _BARE_KEY.match(k) else json.dumps(k, ensure_ascii=False)


def render(settings: dict, comments: dict | None = None) -> str:
    """Render a settings dict as TOML (stdlib has no writer). Nested dicts become tables;
    dicts inside tables become inline tables. `comments` maps "section" -> comment text."""
    comments = comments or {}
    out = ["# convo-index configuration. Every key is optional; missing keys use defaults.", ""]

    def comment(name):
        if name in comments:
            out.append("# " + comments[name].replace("\n", "\n# "))

    def emit(table, name):
        comment(name)
        out.append(f"[{name}]")
        subtables = []
        for k, v in table.items():
            if isinstance(v, dict) and all(isinstance(x, dict) for x in v.values()) and v:
                subtables.append((k, v))
            else:
                out.append(f"{_toml_key(k)} = {_toml_value(v)}")
        out.append("")
        for k, v in subtables:
            emit(v, f"{name}.{_toml_key(k)}") if not all(isinstance(x, dict) for x in v.values()) else \
                [emit(sub, f"{name}.{_toml_key(k)}.{_toml_key(sk)}") for sk, sub in v.items()]

    # TOML: bare keys must precede the first table header.
    bare = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in settings.items()
            if not isinstance(v, dict) and not _is_table_array(v)]
    if bare:
        out += bare + [""]
    for name, table in settings.items():
        if isinstance(table, dict):
            emit(table, name)
        elif _is_table_array(table):   # [[tag_groups]], [[tags]]
            comment(name)
            for item in table:
                out.append(f"[[{_toml_key(name)}]]")
                out.extend(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in item.items())
                out.append("")
    return "\n".join(out).rstrip() + "\n"


def _is_table_array(v):
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def write(settings: dict, path=None, comments=None):
    """Write the config. An existing file that would change is first copied to
    `<name>.bak-<stamp>` — a new copy each time, so hand-written comments (which do not
    survive rendering) are not lost to a later rewrite's own output."""
    path = Path(path) if path else CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render(settings, comments)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != text:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")   # unique even for rewrites in the same second
            path.with_name(f"{path.name}.bak-{stamp}").write_text(existing, encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    reset_cache()
    return path
