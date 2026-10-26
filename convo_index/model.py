"""Normalized conversation model shared by every source adapter.

Adapters translate a tool's native storage into these dataclasses and nothing else:
no classification, no LLM calls. The pipeline (indexer, db, search, UI) only ever
sees this shape, so adding a tool means adding one adapter.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# Message kinds. Only `text` messages are embedded; every kind is keyword-indexed.
KIND_TEXT = "text"
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"
KIND_THINKING = "thinking"

FIDELITY_FULL = "full"
FIDELITY_PARTIAL = "partial"


def content_hash(role: str, kind: str, text: str) -> str:
    """Stable digest used to detect changed message text and to reuse embeddings.
    It is NOT an identity: identical prompts ("continue") share a hash."""
    h = hashlib.sha1(usedforsecurity=False)
    h.update(role.encode()); h.update(b"\0")
    h.update(kind.encode()); h.update(b"\0")
    h.update(text.encode("utf-8", "surrogatepass"))
    return h.hexdigest()


def make_id(source: str, native_id: str) -> str:
    """Conversation ids are namespaced by source so tools never collide."""
    return f"{source}:{native_id}"


def split_id(conversation_id: str) -> tuple[str, str]:
    source, _, native = conversation_id.partition(":")
    return source, native


TITLE_LIMIT = 120


def display_title(title, fallback=""):
    """Titles come from sources that may store a whole first prompt there (Codex threads without
    a real title): one line, at most TITLE_LIMIT characters, with an ellipsis when cut."""
    t = " ".join(str(title or "").split()) or " ".join(str(fallback or "").split())
    return t if len(t) <= TITLE_LIMIT else t[:TITLE_LIMIT - 1].rstrip() + "…"


@dataclass
class UsageRecord:
    """One model request. Buckets are disjoint; None means the tool did not report it."""
    native_request_id: str | None
    message_seq: int | None = None      # assistant message this request produced, if known
    provider: str | None = None
    model: str | None = None
    input_uncached: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None      # 5-minute cache writes (or the total when a vendor does not split)
    cache_write_1h: int | None = None   # 1-hour cache writes (Anthropic prices them at 2x input, not 1.25x)
    output: int | None = None
    reasoning: int | None = None        # informational; already inside `output` for most vendors
    reported_cost_usd: float | None = None
    ts: str | None = None


@dataclass
class ToolCall:
    native_id: str | None
    name: str
    args: dict | None = None
    result_text: str | None = None
    file_actions: list[tuple[str, str]] = field(default_factory=list)  # (action, path)


@dataclass
class Message:
    seq: int
    role: str                            # user | assistant | system | tool
    text: str
    kind: str = KIND_TEXT
    ts: str | None = None
    native_id: str | None = None         # tool's own id; identity when present
    model: str | None = None
    tool_call: ToolCall | None = None

    @property
    def content_hash(self) -> str:
        return content_hash(self.role, self.kind, self.text)


@dataclass
class Conversation:
    source: str
    native_id: str
    title: str
    first_prompt: str
    messages: list[Message]
    source_ref: str                      # file path or "sqlite:<db>#<row id>"
    cwd: str | None = None
    project_root: str | None = None
    git_branch: str | None = None
    git_origin_url: str | None = None
    relation: tuple[str, str] | None = None   # ("subagent" | "fork", parent conversation id)
    start_ts: str | None = None
    end_ts: str | None = None
    provider: str | None = None
    models: list[str] = field(default_factory=list)
    fidelity: str = FIDELITY_FULL
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    usage: list[UsageRecord] = field(default_factory=list)

    @property
    def id(self) -> str:
        return make_id(self.source, self.native_id)

    @property
    def parent_id(self) -> str | None:
        return self.relation[1] if self.relation else None
