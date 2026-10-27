"""Source adapter protocol and registry.

An adapter knows one tool's on-disk layout. It answers three questions:
is the tool present, which conversations exist (with a cheap change key), and what
does one conversation look like in the normalized model. `load()` returns the revision
it actually read so the indexer never records a change key it observed before reading.
"""
from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass, field
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
    from convo_index.sources.claude_code import ClaudeCodeAdapter
    from convo_index.sources.opencode import OpencodeAdapter
    from convo_index.sources.codex import CodexAdapter
    from convo_index.sources.antigravity import AntigravityAdapter
    from convo_index.sources.pi import PiAdapter
    return [ClaudeCodeAdapter(), OpencodeAdapter(), CodexAdapter(), AntigravityAdapter(), PiAdapter()]


def enabled_adapters() -> list[SourceAdapter]:
    return [a for a in all_adapters() if a.available()]


def resume_command(conversation_id: str, cwd: str | None) -> str | None:
    """Shell command that reopens a conversation in its own tool, or None if unknown."""
    source, _, _ = conversation_id.partition(":")
    if source == "claude_code":
        from convo_index.sources.claude_code import resume_command as rc
    elif source == "opencode":
        from convo_index.sources.opencode import resume_command as rc
    elif source == "codex":
        from convo_index.sources.codex import resume_command as rc
    elif source == "antigravity":
        from convo_index.sources.antigravity import resume_command as rc
    elif source == "pi":
        from convo_index.sources.pi import resume_command as rc
    else:
        return None
    return rc(conversation_id, cwd)


# --- Browser turns -------------------------------------------------------------------------
#
# A source that can run one more turn of a conversation non-interactively exposes three
# module-level functions; the registry below names the modules so adding a harness is one
# entry plus those functions:
#
#   turn_command(native_id, prompt, cwd, *, edits) -> TurnCommand | None
#       The process to run. None when this platform cannot launch it safely.
#   turn_events(line) -> list[dict]
#       Events for one stdout line: {"kind": "text", "text"}, {"kind": "tool", "name", "input"},
#       {"kind": "tool_result", "text", "error"}, {"kind": "denied", "name", "input"},
#       {"kind": "done", "cost_usd", ...} or {"kind": "error", "message"}. Unknown lines -> [].
#   last_activity(native_id, locator) -> float | None
#       When the session last changed (epoch seconds), None when unknown.
#   launcher() -> list[str] | None
#       The argv prefix that starts the tool here, None when it is not installed (or cannot
#       be started without a shell): what `doctor` and the UI's `turns_supported` consult.
#
# The native id is the only conversation-derived value that reaches argv, and only after
# NATIVE_ID_RE accepted it: plain characters, never a leading dash, which `claude --resume
# [value]` (an optional-value option) would read as the next flag. The prompt travels
# through stdin or as one argv element.

NATIVE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def is_windows():
    """Platform switch for the launchers (a function so tests can take the other branch
    without patching `os.name`, which would change what `Path` builds)."""
    return os.name == "nt"

TURN_MODULES = {
    "claude_code": "convo_index.sources.claude_code",
    "opencode": "convo_index.sources.opencode",
}


@dataclass(frozen=True)
class TurnCommand:
    argv: list[str]                          # fixed flags plus the validated session id
    cwd: str
    env: dict[str, str] = field(default_factory=dict)   # added to the inherited environment
    unset: tuple[str, ...] = ()              # removed from it (a user's own overrides must not reach a turn)
    stdin: str | None = None                 # the prompt when the harness reads it from stdin


def _turn_module(conversation_id):
    source, _, native = conversation_id.partition(":")
    name = TURN_MODULES.get(source)
    if name is None or not native or not NATIVE_ID_RE.fullmatch(native):
        return None, native
    return importlib.import_module(name), native


def supports_turns(conversation_id: str) -> bool:
    """Can this conversation's source run a browser turn on this machine's platform?"""
    mod, _ = _turn_module(conversation_id)
    return bool(mod) and bool(getattr(mod, "TURNS_ON_WINDOWS", True) or not is_windows())


def turn_command(conversation_id: str, prompt: str, cwd: str, *, edits: bool = False) -> TurnCommand | None:
    """The harness invocation for one more turn, or None when the source has no headless
    mode here. Raises ValueError for a native id that must never reach a command line."""
    source, _, native = conversation_id.partition(":")
    if source in TURN_MODULES and not NATIVE_ID_RE.fullmatch(native or ""):
        raise ValueError(f"refusing to run a turn for a session id that is not plain text: {native!r}")
    mod, native = _turn_module(conversation_id)
    if mod is None or not supports_turns(conversation_id):
        return None
    return mod.turn_command(native, prompt, cwd, edits=edits)


def turn_launcher(conversation_id: str) -> list[str] | None:
    """How the source's tool would be started on this machine, or None when it cannot be."""
    mod, _ = _turn_module(conversation_id)
    return mod.launcher() if mod and supports_turns(conversation_id) else None


def turn_events(conversation_id: str, line: str) -> list[dict]:
    mod, _ = _turn_module(conversation_id)
    return mod.turn_events(line) if mod else []


def last_activity(conversation_id: str, locator: str | None) -> float | None:
    mod, native = _turn_module(conversation_id)
    if mod is None or not locator:
        return None
    try:
        return mod.last_activity(native, locator)
    except (OSError, ValueError):
        return None


def read_snapshot(path):
    """Read a session file as a consistent snapshot.

    Tools append to their session files while a session is live. We fstat the open descriptor
    first and read exactly that many bytes, so the revision we report describes precisely
    the bytes we parsed: an append that lands during or after the read changes mtime/size
    and is picked up next run instead of being silently acknowledged. A trailing partial
    line (a write in progress) is dropped. Returns (text, revision)."""
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        data = fh.read(st.st_size)
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        data = data[:cut + 1] if cut >= 0 else b""
    return data.decode("utf-8", "replace"), f"{st.st_mtime}:{st.st_size}"
