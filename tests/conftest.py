"""Shared fixtures. Tests never reach a real LLM, embedder or network: settings default to
no LLM backend, and the embedder is replaced per test where vectors matter."""
import copy
import hashlib

import pytest

from convo_index import settings as S
from convo_index.providers.base import LLMResult


def make_settings(tmp_path, **overrides):
    """Settings with tmp paths, example category hints (so the classification tests read
    naturally), and NO llm backend configured."""
    cfg = copy.deepcopy(S.DEFAULTS)
    cfg["paths"] = {"db": str(tmp_path / "t.db"), "archive": str(tmp_path / "archive")}
    cfg["category"] = {
        "labels": ["globex", "personal"],
        "descriptions": {"globex": "work for Globex", "personal": "side projects"},
        "hints": {"globex": ["globex", "widgets", "saas-scripts"], "personal": ["hobby", "/gt/"]},
    }
    cfg["references"]["ticket_url"] = "https://example.atlassian.net/browse/{key}"
    for k, v in overrides.items():
        cfg[k] = S._merge(cfg.get(k, {}), v) if isinstance(v, dict) else v
    s = S.Settings(cfg)
    s.path = None
    return s


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def llm_settings(tmp_path):
    """Settings with a (fake) backend configured for both jobs."""
    return make_settings(tmp_path, llm={"classify": {"backend": "claude_cli", "model": "m"},
                                        "summary": {"backend": "claude_cli", "model": "m"}},
                         privacy={"hosted_processing": "allow"})


class FakeLLM:
    """Stand-in for providers.registry.complete: returns queued results, records prompts."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, job, prompt, schema=None, settings=None, **kw):
        self.calls.append({"job": job, "prompt": prompt, "schema": schema, **kw})
        if not self.results:
            return LLMResult(ok=False, error="provider", detail="no more fake results")
        r = self.results.pop(0)
        return r if isinstance(r, LLMResult) else LLMResult(ok=True, data=r)


@pytest.fixture
def fake_llm(monkeypatch):
    def install(*results):
        f = FakeLLM(*results)
        from convo_index.providers import registry
        monkeypatch.setattr(registry, "complete", f)
        return f
    return install


def _real_config_state():
    from convo_index import config as C
    path = C.user_dir("config") / "convo-index" / "config.toml"
    try:
        return path, hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return path, None


@pytest.fixture(autouse=True, scope="session")
def _the_users_config_is_never_touched():
    """A test that writes to the real default config location empties the developer's own
    configuration. Fail the run if the file is not exactly as it was."""
    path, before = _real_config_state()
    yield
    after = _real_config_state()[1]
    assert after == before, f"a test changed {path}: give it its own XDG_CONFIG_HOME (tmp_path)"
