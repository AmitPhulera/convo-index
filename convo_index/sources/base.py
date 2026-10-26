"""Source adapter protocol and registry.

An adapter knows one tool's on-disk layout. It answers three questions:
is the tool present, which conversations exist (with a cheap change key), and what
does one conversation look like in the normalized model. `load()` returns the revision
it actually read so the indexer never records a change key it observed before reading.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from convo_index.model import Conversation


class UnreadableSource(Exception):
    """The source has records for this conversation but none could be interpreted
    (malformed JSON, unknown layout). Distinct from a legitimately empty or excluded
    session: the indexer must keep any previously indexed content and report a failure."""


@dataclass(frozen=True)
class SourceRef:
    source: str
    native_id: str
    change_key: str          # opaque; equal keys mean "nothing to do"
    locator: str             # path / db pointer the adapter needs to load this ref

    @property
    def id(self) -> str:
        return f"{self.source}:{self.native_id}"


class SourceAdapter(Protocol):
    name: str
    supported_versions: str

    def available(self) -> bool: ...
    def discover(self) -> Iterable[SourceRef]: ...
    def load(self, ref: SourceRef) -> tuple[Conversation, str] | None:
        """Return (conversation, revision_read); None when the session is confirmed to have
        nothing indexable (empty, reverted to nothing, or the indexer's own headless LLM
        call); raise UnreadableSource when records exist but cannot be interpreted."""
        ...


def shell_quote(s: str) -> str:
    """One argument, quoted for the shell the user will paste into: POSIX sh quoting, or
    PowerShell on Windows, where a single-quoted string is literal (cmd.exe and PowerShell
    quote differently; the UI says which one the command is for)."""
    import os
    if os.name == "nt":
        if s and all(c.isalnum() or c in "-_./:@=+," for c in s):
            return s
        return "'" + s.replace("'", "''") + "'"
    import shlex
    return shlex.quote(s)


def all_adapters() -> list[SourceAdapter]:
    """Adapters in a fixed order; import lazily so an optional source cannot break startup."""
    return []


def enabled_adapters() -> list[SourceAdapter]:
    return [a for a in all_adapters() if a.available()]


def resume_command(conversation_id: str, cwd: str | None) -> str | None:
    """Shell command that reopens a conversation in its own tool, or None if unknown."""
    return None
