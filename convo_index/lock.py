"""Cross-process writer lock so the scheduler, the CLI and the UI's refresh button
never index concurrently. An advisory lock on `<db>.lock` next to the database: flock on
POSIX, a byte-range lock through msvcrt on Windows."""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class IndexLocked(RuntimeError):
    pass


def _acquire(fh, blocking):
    if os.name != "nt":
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    fh.seek(0)
    while True:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)   # the first byte stands for the whole file
            return
        except OSError:
            if not blocking:
                raise BlockingIOError from None
            time.sleep(0.1)


def _release(fh):
    if os.name != "nt":
        fcntl.flock(fh, fcntl.LOCK_UN)
    else:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def index_lock(db_path, blocking=False):
    lock_path = Path(str(db_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    held = False
    try:
        try:
            _acquire(fh, blocking)
            held = True
        except BlockingIOError:
            raise IndexLocked(f"another indexer holds {lock_path}") from None
        yield
    finally:
        try:
            if held:
                _release(fh)
        finally:
            fh.close()
