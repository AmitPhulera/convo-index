"""Shared fixtures. Tests never reach a real LLM, embedder or network: settings default to
no LLM backend, and the embedder is replaced per test where vectors matter."""
import copy

import pytest

from convo_index import settings as S


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
