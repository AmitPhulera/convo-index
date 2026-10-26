"""A readable copy of every indexed transcript next to the database, named after the
conversation. The descriptive parts of the name come from source metadata, so they are
reduced to a safe character set and budgeted; ownership is a digest of the full session id,
which cannot collide or carry glob characters."""
import hashlib
import os
import re
import shutil
from pathlib import Path

from convo_index.config import ARCHIVE_DIR

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
NAME_BUDGET = 200            # bytes for the whole file name: well under every filesystem's 255


def slugify(text, maxlen=60):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:maxlen].strip("-") or "untitled"


def _component(text, fallback, maxlen=40):
    """One descriptive part: letters, digits, dot, dash and underscore only, never empty."""
    return _UNSAFE.sub("-", str(text or "")).strip("-.")[:maxlen] or fallback


def session_key(conv):
    """What an archive file belongs to: a digest of the full session id (any string)."""
    return hashlib.sha256(str(conv.get("session_id") or "").encode()).hexdigest()[:16]


def archive_filename(conv, category, tickets):
    date = str(conv.get("start_ts") or "")[:10]
    if not _DATE.fullmatch(date):
        date = "0000-00-00"
    parts = [date, _component(category, "none"), slugify(conv.get("title"))]
    if tickets:
        parts.append(_component(tickets[0], "ticket"))
    tail = "__" + session_key(conv) + ".jsonl"
    head = "__".join(parts)
    return head[:NAME_BUDGET - len(tail)].rstrip("-_") + tail


def write_archive_copy(src_path, conv, category, tickets, archive_dir=None):
    """Copy `src_path` under its archive name. The previous copy for this session (its title
    or category may have changed) is removed only after the new one is in place."""
    archive_dir = Path(archive_dir) if archive_dir else ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / archive_filename(conv, category, tickets)
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copy2(src_path, tmp)
    os.replace(tmp, dest)
    for old in archive_dir.glob("*__" + session_key(conv) + ".jsonl"):   # hex digest: no glob characters
        if old != dest:
            old.unlink()
    return dest
