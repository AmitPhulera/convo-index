"""Ticket keys (JIRA-style `ABC-123`) in a text blob, for callers that need nothing more."""
from __future__ import annotations

import re

from convo_index import settings as S


def extract_tickets(text, settings=None):
    if not text:
        return []
    settings = settings or S.load()
    refs = settings["references"]
    matches = {m.group(0) for m in re.finditer(refs["ticket_regex"], text)}  # groups in a user regex are fine
    block = set(refs.get("ticket_prefix_blocklist") or [])
    # Drop matches whose prefix is a known non-ticket token (e.g. UTF-8, SHA-256).
    return sorted(m for m in matches if m.split("-")[0] not in block)
