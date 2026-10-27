"""A stand-in coding agent for the turn tests. FAKE_HARNESS holds a JSON list of steps:
  {"line": "<text>"}        print one line to stdout
  {"partial": "<text>"}     print without a newline (a write in progress)
  {"sleep": <seconds>}
  {"stderr": <bytes>}       flood stderr with that many bytes
  {"echo_stdin": true}      print stdin as one line, JSON-encoded
  {"stdin_to": "<path>"}    write stdin to that file
  {"argv_to": "<path>"}     write the arguments it was given to that file, one per line
  {"touch": "<path>"}       set that file's mtime to now (a harness appending to its transcript)
  {"echo_env": "<NAME>"}    print that environment variable's value, JSON-encoded
  {"echo_cwd": true}        print the working directory, JSON-encoded
  {"child": "<pidfile>", "ignore_term": bool}
                            spawn a sleeping grandchild (optionally one that ignores SIGTERM) and
                            write its pid to that file
  {"orphan": "<pidfile>"}   the same, but exit at once without waiting for it (it inherits the pipes)
  {"bigline": <bytes>}      print one line of that many bytes
  {"exit": <code>}          exit with that status
Run as `python fake_harness.py`; the launchers in the tests install it under a harness's name."""
import json
import os
import subprocess
import sys
import time


def read_stdin():
    """stdin as UTF-8, whatever the console code page (the real harnesses read bytes too)."""
    return sys.stdin.buffer.read().decode("utf-8")


def main():
    steps = json.loads(os.environ.get("FAKE_HARNESS") or "[]")
    children = []
    for step in steps:
        if "line" in step:
            sys.stdout.write(step["line"] + "\n"); sys.stdout.flush()
        elif "partial" in step:
            sys.stdout.write(step["partial"]); sys.stdout.flush()
        elif "sleep" in step:
            time.sleep(step["sleep"])
        elif "stderr" in step:
            sys.stderr.write("e" * step["stderr"]); sys.stderr.flush()
        elif step.get("echo_stdin"):
            sys.stdout.write(json.dumps(read_stdin()) + "\n"); sys.stdout.flush()
        elif "touch" in step:
            os.utime(step["touch"], None)
        elif "argv_to" in step:
            with open(step["argv_to"], "w", encoding="utf-8") as fh:
                fh.write("\n".join(sys.argv[1:]))
        elif "stdin_to" in step:
            with open(step["stdin_to"], "w", encoding="utf-8") as fh:
                fh.write(read_stdin())
        elif "echo_env" in step:
            sys.stdout.write(json.dumps(os.environ.get(step["echo_env"])) + "\n"); sys.stdout.flush()
        elif step.get("echo_cwd"):
            sys.stdout.write(json.dumps(os.getcwd()) + "\n"); sys.stdout.flush()
        elif "child" in step or "orphan" in step:
            body = "import time; time.sleep(300)"
            if step.get("ignore_term"):
                body = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
            child = subprocess.Popen([sys.executable, "-c", body])
            with open(step.get("child") or step["orphan"], "w") as fh:
                fh.write(str(child.pid))
            if "orphan" in step:
                os._exit(0)
            children.append(child)
        elif "bigline" in step:
            sys.stdout.write("x" * step["bigline"] + "\n"); sys.stdout.flush()
        elif "exit" in step:
            sys.exit(step["exit"])
    for c in children:
        c.wait()


if __name__ == "__main__":
    main()
