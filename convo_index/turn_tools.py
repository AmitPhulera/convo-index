"""The MCP tools a browser turn in Edit mode may call without asking.

The list is kept in `turn-tools.json` next to the database, private to this user like
`turn-token`, and edited from the UI. It is not in config.toml: settings.write renders that file
afresh and would drop the user's comments. An entry is `mcp__<server>__<tool>`, or
`mcp__<server>__*` for every tool of a server, and must name a server the harness has
configured. A file others could write is ignored: it decides what runs without approval."""
import json
import os
import re

from convo_index import settings as CFG

TOOL_RE = re.compile(r"[A-Za-z0-9_-]+|\*")


def store_path(settings=None):
    return (settings or CFG.load()).db_path().with_name("turn-tools.json")


def load(settings=None):
    """The allowlisted entries, or [] when there is no usable store."""
    path = store_path(settings)
    try:
        if os.name != "nt" and path.stat().st_mode & 0o022:
            return []                     # writable by others: not the user's own list
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("mcp_tools") if isinstance(data, dict) else None
    return [e for e in entries if isinstance(e, str)] if isinstance(entries, list) else []


def split(entry, servers):
    """(server, tool) for a well-formed entry naming one of `servers`, else None. Server names
    may themselves contain "__", so the server is matched against the known names rather than
    parsed out of the entry."""
    if not isinstance(entry, str) or not entry.startswith("mcp__"):
        return None
    for server in sorted(servers, key=len, reverse=True):
        prefix = f"mcp__{server}__"
        if entry.startswith(prefix) and TOOL_RE.fullmatch(entry[len(prefix):]):
            return server, entry[len(prefix):]
    return None


def validate(entries, servers):
    """The entries, de-duplicated in order; raises ValueError naming the first bad one."""
    if not isinstance(entries, list):
        raise ValueError("mcp_tools must be a list")
    out = []
    for entry in entries:
        if split(entry, servers) is None:
            raise ValueError(f"not an MCP tool of a configured server: {entry!r}")
        if entry not in out:
            out.append(entry)
    return out


def save(entries, servers, settings=None):
    """Validate and write the list (owner-only). Returns what was stored."""
    entries = validate(entries, servers)
    path = store_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"mcp_tools": entries}, fh, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return entries
