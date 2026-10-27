"""Browser turns: run one more turn of a conversation in its own coding agent.

A turn belongs to the UI server process, not to the HTTP request that started it: the
harness runs to completion (or the timeout) whether or not a browser is still listening, its
events are kept in a bounded buffer that any number of streams can read from any position,
and the terminal event is appended only after the index refresh that follows the process,
so a client that reloads the conversation on `done` sees the new turn.

The runner knows nothing about harnesses. It is handed a `TurnCommand` (argv, cwd, extra
environment, optional stdin) and a line translator, keeps one process per conversation and
at most `max_concurrent` in total, drains stdout and stderr concurrently so a chatty child
can never block, bounds what it keeps by count and bytes, and ends the whole process group
on timeout, stop, server shutdown and after the harness exits: nothing of a turn outlives it.
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import threading
import time
import uuid

from convo_index.sources.base import TurnCommand

if os.name == "nt":
    from convo_index import winjob

EVENT_LIMIT = 2000              # events kept per turn; older ones are dropped and reported as a gap
EVENT_BYTES = 4 * 1024 * 1024   # bytes of events kept per turn (same eviction)
TEXT_LIMIT = 64 * 1024          # chars kept of one event's text
MAX_LINE = 1024 * 1024          # a stdout line longer than this is dropped, never accumulated
STDERR_LIMIT = 64 * 1024        # bytes of stderr kept (the tail)
KEEP_FINISHED = 600             # seconds a finished turn stays readable
KEEPALIVE = 15                  # seconds between keepalives on an idle stream
GRACE = 5                       # seconds between terminate and kill


class TurnRefused(Exception):
    """The runner will not start this turn; `status` is the HTTP code that says why."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Turn:
    def __init__(self, conversation_id, edits):
        self.id = uuid.uuid4().hex
        self.conversation_id = conversation_id
        self.edits = edits
        self.started = time.time()
        self.finished = None
        self.proc = None
        self.cond = threading.Condition()
        self._events = collections.deque()   # (size, event)
        self._bytes = 0
        self.dropped = 0                 # events evicted from the front
        self.stderr = bytearray()
        self.cost_usd = None
        self.harness_error = None        # the harness's own error event, if it sent one
        self.kill_reason = None          # "timeout" | "stopped"
        self.exit_code = None
        self.outcome = None              # (ok, message) latched as soon as the harness is gone
        self.activity = None             # callable sampling the source's activity marker
        self.activity_after = None       # that marker right after the harness is gone
        self.done = False
        self.readers = []                # stdout/stderr threads, joined before the terminal event
        self.waiter = None
        self.job = None                  # Windows: the job object that owns the harness's process tree

    @property
    def next_index(self):
        return self.dropped + len(self._events)

    def events_from(self, pos):
        """Events at absolute indexes >= pos (the caller holds `cond`)."""
        return [ev for _, ev in list(self._events)[max(0, pos - self.dropped):]]

    def _put(self, event):
        if self.done:                    # nothing lands after the terminal event
            return
        for key in ("text", "input", "message", "stderr"):
            if isinstance(event.get(key), str) and len(event[key]) > TEXT_LIMIT:
                event[key] = event[key][:TEXT_LIMIT] + "…"
        size = len(json.dumps(event))
        self._events.append((size, event))
        self._bytes += size
        while self._events and (len(self._events) > EVENT_LIMIT or self._bytes > EVENT_BYTES):
            gone, _ = self._events.popleft()
            self._bytes -= gone
            self.dropped += 1

    def append(self, event):
        with self.cond:
            self._put(event)
            self.cond.notify_all()

    def finish(self, event):
        """The terminal event and the done flag land together: no reader sees one without the other."""
        with self.cond:
            self._put(event)
            self.done = True
            self.finished = time.time()
            self.cond.notify_all()

    def snapshot(self):
        with self.cond:
            return {"turn_id": self.id, "conversation_id": self.conversation_id, "edits": self.edits,
                    "started": self.started, "done": self.done, "events": self.next_index}


def _child_env(command):
    env = {k: v for k, v in os.environ.items() if k not in command.unset}
    env.update(command.env)
    return env


def _adopt_into_job(proc, api=None):
    """Windows: put the still-suspended harness into a kill-on-close job, then let it run. If
    the job cannot be set up the harness never runs: it is killed while suspended and the
    turn is refused, so no process exists that the runner does not own."""
    api = api or winjob
    job = None
    try:
        job = api.adopt(proc)
        api.resume(proc)
        return job
    except OSError as e:
        if job is not None:
            api.close(job)             # kill-on-close
        try:
            proc.kill()
            proc.wait(GRACE)
        except OSError:
            pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        raise TurnRefused(500, f"could not confine the harness in a job object: {e}") from e


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_group(turn):
    """End the harness and everything it spawned. On POSIX the harness leads its own process
    group, which is signalled as a whole: SIGTERM, up to GRACE seconds for the members to go,
    then SIGKILL for whatever stayed, whether or not the harness itself is still there. On
    Windows the harness and its descendants live in a job object that is terminated as one."""
    proc = turn.proc
    try:
        if os.name == "nt":
            if turn.job is not None:
                winjob.terminate(turn.job)
            else:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
            return
        import signal
        if not _group_alive(proc.pid):
            return
        os.killpg(proc.pid, signal.SIGTERM)
        deadline = time.time() + GRACE
        while _group_alive(proc.pid) and time.time() < deadline:
            time.sleep(0.05)
        if _group_alive(proc.pid):
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class TurnRunner:
    def __init__(self, *, timeout=1200, max_concurrent=2, refresh=None):
        self.timeout = timeout
        self.max_concurrent = max_concurrent
        self.refresh = refresh            # callable -> {"ok", "busy", "error"?}; the UI's refresh path
        self._turns: dict[str, Turn] = {}
        self._lock = threading.Lock()
        self.closed = False               # set by shutdown(): no turn starts afterwards

    # -- bookkeeping --------------------------------------------------------------------------

    def _prune(self):
        now = time.time()
        for tid, t in list(self._turns.items()):
            if t.done and t.finished and now - t.finished > KEEP_FINISHED:
                del self._turns[tid]

    def get(self, turn_id):
        with self._lock:
            return self._turns.get(turn_id)

    def active(self, conversation_id):
        """The running turn of a conversation, or None."""
        with self._lock:
            for t in self._turns.values():
                if t.conversation_id == conversation_id and not t.done:
                    return t
        return None

    def running(self):
        with self._lock:
            return sum(1 for t in self._turns.values() if not t.done)

    def last_own_activity(self, conversation_id):
        """The activity marker the newest finished turn of a conversation left behind (the
        transcript as the harness wrote it), or None. A transcript still at that marker was
        last touched by the browser, not by a terminal session."""
        with self._lock:
            turns = [t for t in self._turns.values() if t.conversation_id == conversation_id and t.finished]
        if not turns:
            return None
        return max(turns, key=lambda t: t.finished).activity_after

    # -- lifecycle ----------------------------------------------------------------------------

    def start(self, conversation_id, command: TurnCommand, translate, *, edits=False, activity=None):
        """Spawn the harness. `translate(line) -> list[dict]` turns one stdout line into events;
        `activity() -> float | None` reads the source's activity marker (sampled right after
        the harness is gone, see last_own_activity)."""
        with self._lock:
            self._prune()
            if self.closed:
                raise TurnRefused(503, "the server is shutting down")
            if any(t.conversation_id == conversation_id and not t.done for t in self._turns.values()):
                raise TurnRefused(409, "a turn is already running for this conversation")
            if sum(1 for t in self._turns.values() if not t.done) >= self.max_concurrent:
                raise TurnRefused(429, f"{self.max_concurrent} turns are already running; try again when one finishes")
            turn = Turn(conversation_id, edits)
            turn.activity = activity
            popen_kw = {"cwd": command.cwd, "env": _child_env(command),
                        "stdin": subprocess.PIPE if command.stdin is not None else subprocess.DEVNULL,
                        "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
            if os.name == "nt":
                # started suspended, placed in a job that dies with its handle, then resumed: no
                # descendant can be created outside the job
                popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | winjob.CREATE_SUSPENDED
            else:
                popen_kw["start_new_session"] = True     # the harness leads its own group: killable as one
            try:
                turn.proc = subprocess.Popen(command.argv, **popen_kw)
            except ValueError as e:      # an argument the OS cannot carry (an embedded NUL)
                raise TurnRefused(400, f"the prompt cannot be passed to the harness: {e}") from e
            except OSError as e:
                raise TurnRefused(500, f"could not start the harness: {e}") from e
            if os.name == "nt":
                turn.job = _adopt_into_job(turn.proc)
            self._turns[turn.id] = turn
        threading.Thread(target=self._feed_stdin, args=(turn, command.stdin), daemon=True).start()
        turn.readers = [threading.Thread(target=self._read_stdout, args=(turn, translate), daemon=True),
                        threading.Thread(target=self._read_stderr, args=(turn,), daemon=True)]
        for t in turn.readers:
            t.start()
        turn.waiter = threading.Thread(target=self._wait, args=(turn,), daemon=True)
        turn.waiter.start()
        return turn

    def stop(self, turn_id):
        """Stop a running harness. A turn whose harness has already exited (it may still be
        winding down) keeps its outcome. Returns None for an unknown turn, else
        whether anything was stopped."""
        turn = self.get(turn_id)
        if turn is None:
            return None
        if turn.done or turn.proc.poll() is not None:
            return False
        turn.kill_reason = turn.kill_reason or "stopped"
        threading.Thread(target=_kill_group, args=(turn,), daemon=True).start()
        return True

    def shutdown(self, wait=GRACE * 2):
        """Stop every running turn and accept no more; the server calls this on exit so no
        harness outlives it."""
        with self._lock:
            self.closed = True           # under the lock: a start() racing this sees it
            running = [t for t in self._turns.values() if not t.done]
        for t in running:
            t.kill_reason = t.kill_reason or "stopped"
            _kill_group(t)
        deadline = time.time() + wait
        for t in running:
            if t.waiter is not None:
                t.waiter.join(max(0.0, deadline - time.time()))
        return len(running)

    # -- threads ------------------------------------------------------------------------------

    @staticmethod
    def _feed_stdin(turn, text):
        if text is None:
            return
        try:
            turn.proc.stdin.write(text.encode("utf-8"))
        except UnicodeEncodeError:
            turn.harness_error = "the prompt could not be sent to the harness (not valid Unicode text)"
        except (BrokenPipeError, OSError, ValueError):
            pass                          # the harness closed its input or exited: its own exit says why
        finally:
            try:
                turn.proc.stdin.close()   # always: a harness waiting for EOF must not wait for the timeout
            except (BrokenPipeError, OSError, ValueError):
                pass

    @staticmethod
    def _read_stdout(turn, translate):
        with turn.proc.stdout as out:
            while True:
                raw = out.readline(MAX_LINE + 1)     # one line, bounded: a runaway line is never accumulated
                if not raw:
                    break
                if len(raw) > MAX_LINE and not raw.endswith(b"\n"):
                    while True:                      # discard the rest of the overlong line
                        more = out.readline(MAX_LINE)
                        if not more or more.endswith(b"\n"):
                            break
                    turn.append({"kind": "text", "text": f"[a line longer than {MAX_LINE} bytes was dropped]"})
                    continue
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                try:
                    events = translate(line)
                except Exception as e:   # a translator bug must not end the stream
                    events = [{"kind": "error", "message": f"could not read the harness output: {e}"}]
                for ev in events:
                    kind = ev.get("kind")
                    if kind == "done":           # the harness's own summary feeds the terminal event
                        turn.cost_usd = ev.get("cost_usd")
                    elif kind == "error":
                        turn.harness_error = ev.get("message")
                    else:
                        turn.append(ev)

    @staticmethod
    def _read_stderr(turn):
        with turn.proc.stderr as err:
            while True:
                chunk = err.read(4096)
                if not chunk:
                    break
                turn.stderr += chunk
                if len(turn.stderr) > STDERR_LIMIT:
                    del turn.stderr[:len(turn.stderr) - STDERR_LIMIT]

    def _wait(self, turn):
        try:
            turn.proc.wait(self.timeout)
        except subprocess.TimeoutExpired:
            turn.kill_reason = "timeout"
            _kill_group(turn)
            turn.proc.wait()
        turn.exit_code = turn.proc.returncode
        # Every line the harness wrote must be in the buffer before the terminal event. The
        # readers end when the pipes close; a descendant that inherited them and outlived the
        # harness keeps them open, so after a grace period the group is ended, and it is ended
        # in any case: nothing of a finished turn may live on. A reader still alive after that
        # (a translator that never returns) makes the turn an error, and the terminal event
        # closes the buffer to whatever it produces later.
        for t in turn.readers:
            t.join(GRACE)
        _kill_group(turn)
        for t in turn.readers:
            t.join(GRACE)
        unread = any(t.is_alive() for t in turn.readers)
        turn.outcome = self._outcome(turn, unread)   # latched now: a late stop() changes nothing
        if turn.activity is not None:
            try:
                turn.activity_after = turn.activity()
            except Exception:
                turn.activity_after = None
        self._finish(turn)
        if turn.job is not None:
            winjob.close(turn.job)

    def _outcome(self, turn, unread):
        ok = turn.exit_code == 0 and turn.harness_error is None and turn.kill_reason is None and not unread
        if ok:
            return True, None
        return False, (turn.harness_error or
                       {"timeout": f"the harness was stopped after {self.timeout} seconds",
                        "stopped": "stopped"}.get(turn.kill_reason) or
                       ("the harness output could not be read to the end" if unread else
                        f"the harness exited with status {turn.exit_code}"))

    def _finish(self, turn):
        indexed, reason = self._refresh_index()
        ok, message = turn.outcome
        event = {"kind": "done" if ok else "error", "exit_code": turn.exit_code, "cost_usd": turn.cost_usd,
                 "indexed": indexed, "reason": reason}
        if not ok:
            event["message"] = message
            stderr = turn.stderr.decode("utf-8", "replace").strip()
            if stderr:
                event["stderr"] = stderr[-4000:]
        turn.finish(event)

    def _refresh_index(self):
        """(indexed, reason): the refresh ran cleanly, or why the new turn may not show yet. It
        runs after every exit, a killed turn included: whatever the harness wrote is real."""
        if self.refresh is None:
            return False, "no refresh configured"
        try:
            res = self.refresh() or {}
        except Exception as e:
            return False, f"refresh failed: {e}"
        if res.get("busy"):
            return False, "indexer busy; the turn appears on the next refresh"
        if not res.get("ok"):
            return False, (res.get("error") or "refresh reported failures")[-500:]
        return True, None

    # -- reading ------------------------------------------------------------------------------

    def events(self, turn_id, after=0):
        """Yield `(index, event)` from `after`, following the turn until its terminal event;
        `(index, None)` is a keepalive while nothing arrives. `after` is the number of events
        already seen, so a reconnecting client resumes without duplicates. Nothing is yielded
        while the turn's lock is held: a slow consumer never blocks the readers."""
        turn = self.get(turn_id)
        if turn is None:
            return
        pos, idle = max(0, after), False
        while True:
            with turn.cond:
                batch = []
                if pos < turn.dropped:   # the gap takes the last evicted index: resuming after it yields no second gap
                    batch.append((turn.dropped - 1, {"kind": "gap", "missed": turn.dropped - pos}))
                    pos = turn.dropped
                fresh = turn.events_from(pos)
                if not batch and not fresh and not turn.done and not idle:
                    turn.cond.wait(KEEPALIVE)
                    idle = True
                    continue             # re-check the gap: the buffer may have moved while waiting
                idle = False
                for ev in fresh:
                    batch.append((pos, ev))
                    pos += 1
                finished = turn.done and pos >= turn.next_index
            for item in batch:
                yield item
            if finished:
                return
            if not batch:
                yield pos, None
