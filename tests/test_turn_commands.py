"""Per-harness browser-turn commands: the closing flags, where the prompt travels, how the
harness's stream is translated, and how recent activity is measured."""
import json
import os
import time

import pytest

from convo_index.sources import base
from convo_index.sources import claude_code as cc


# ---- Claude Code ------------------------------------------------------------------------------

@pytest.fixture
def claude_on_path(monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/opt/bin/claude" if name == "claude" else None)
    monkeypatch.setattr(base, "is_windows", lambda: False)


def test_claude_read_only_turn_closes_every_surface_and_sends_the_prompt_on_stdin(claude_on_path):
    cmd = base.turn_command("claude_code:0f1e2d3c-1234-4abc-8def-0123456789ab", "what changed?", "/proj")
    assert cmd.argv == ["/opt/bin/claude", "--resume", "0f1e2d3c-1234-4abc-8def-0123456789ab",
                        "-p", "--output-format", "stream-json", "--verbose", "--safe-mode", "--restricted",
                        "--strict-mcp-config", "--disable-slash-commands", "--permission-prompts", "none",
                        "--tools", "Read,Glob,Grep"]
    assert cmd.stdin == "what changed?" and cmd.cwd == "/proj" and cmd.env == {}
    assert "what changed?" not in cmd.argv
    assert base.turn_launcher("claude_code:abc") == ["/opt/bin/claude"]


def test_claude_edits_turn_adds_file_tools_but_never_bash(claude_on_path):
    cmd = base.turn_command("claude_code:abc", "fix it", "/proj", edits=True)
    assert cmd.argv[-4:] == ["--tools", "Read,Glob,Grep,Edit,Write,MultiEdit", "--permission-mode", "acceptEdits"]
    assert "--restricted" in cmd.argv and "Bash" not in " ".join(cmd.argv)


def test_claude_turn_helpers_leave_the_transcript_parser_alone():
    # the turn translator has its own, shorter clip; the indexer's limit and helper are untouched
    assert cc.TOOL_TEXT_LIMIT == 4000 and cc.TURN_TEXT_LIMIT == 400
    long_result = {"type": "tool_result", "content": "r" * 1000}
    assert len(cc._tool_result_text(long_result)) == 1000
    assert len(cc._turn_result_text(long_result)) == cc.TURN_TEXT_LIMIT + 1


def test_claude_missing_binary_means_no_command(monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    assert base.turn_command("claude_code:abc", "x", "/proj") is None
    assert base.turn_launcher("claude_code:abc") is None


def test_claude_windows_npm_shim_is_bypassed_for_node(monkeypatch, tmp_path):
    shim = tmp_path / "claude.cmd"
    script = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("// entry")
    shim.write_text('@ECHO off\r\nendLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
                    '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n')
    monkeypatch.setattr(base, "is_windows", lambda: True)
    monkeypatch.setattr(cc.shutil, "which", lambda name: {"claude": str(shim), "node": "C:\\node\\node.exe"}.get(name))
    cmd = cc.turn_command("abc", "x", "C:\\proj")
    assert cmd.argv[:2] == ["C:\\node\\node.exe", str(script)]
    assert not any(a.lower().endswith(".cmd") for a in cmd.argv)


def test_claude_windows_shim_without_a_script_is_refused(monkeypatch, tmp_path):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@ECHO off\r\nsomething else\r\n")
    monkeypatch.setattr(base, "is_windows", lambda: True)
    monkeypatch.setattr(cc.shutil, "which", lambda name: {"claude": str(shim), "node": "node"}.get(name))
    assert cc.turn_command("abc", "x", "C:\\proj") is None
    assert base.turn_launcher("claude_code:abc") is None            # what doctor and the UI see


def test_claude_windows_native_exe_is_used_directly(monkeypatch):
    monkeypatch.setattr(base, "is_windows", lambda: True)
    monkeypatch.setattr(cc.shutil, "which", lambda name: "C:\\Users\\me\\.local\\bin\\claude.exe")
    assert cc.turn_command("abc", "x", "C:\\proj").argv[0].endswith("claude.exe")


# Shapes captured from `claude -p --resume ... --output-format stream-json` on 2.1.311, content replaced.
CLAUDE_STREAM = [
    {"type": "system", "subtype": "init", "tools": ["Glob", "Grep", "Read"], "mcp_servers": [], "session_id": "s"},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/proj/hello.txt"}}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "1\thello from the file\n"}]}},
    {"type": "rate_limit_event", "rate_limit_info": {}},
    {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 12},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "The file says hello."}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_2", "is_error": True,
         "content": "/tmp/x is outside /proj; --restricted confines the file tools to the working directory."}]}},
    {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.18, "num_turns": 3,
     "permission_denials": [{"tool_name": "Write", "tool_use_id": "toolu_2", "tool_input": {"file_path": "/tmp/x"}}]},
]


def test_claude_stream_is_translated_and_noise_dropped():
    events = [e for line in CLAUDE_STREAM for e in base.turn_events("claude_code:s", json.dumps(line))]
    assert [e["kind"] for e in events] == ["tool", "tool_result", "text", "tool_result", "denied", "done"]
    assert events[0] == {"kind": "tool", "name": "Read", "input": '{"file_path": "/proj/hello.txt"}'}
    assert events[1]["text"].startswith("1\thello") and events[1]["error"] is False
    assert events[2]["text"] == "The file says hello."
    assert events[3]["error"] is True and "--restricted" in events[3]["text"]
    assert events[4] == {"kind": "denied", "name": "Write", "input": '{"file_path": "/tmp/x"}'}
    assert events[5] == {"kind": "done", "cost_usd": 0.18, "turns": 3, "stop": "success"}


def test_claude_error_result_and_garbage_lines():
    err = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"})
    assert cc.turn_events(err) == [{"kind": "error", "message": "boom"}]
    assert cc.turn_events("not json") == [] and cc.turn_events("[1, 2]") == [] and cc.turn_events("") == []
    assert cc.turn_events(json.dumps({"type": "assistant", "message": {"content": "a string"}})) == []


def test_claude_long_tool_input_is_clipped():
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {"content": "x" * 5000}}]}})
    (ev,) = cc.turn_events(line)
    assert len(ev["input"]) <= cc.TOOL_TEXT_LIMIT + 1 and ev["input"].endswith("…")


def test_claude_last_activity_is_the_transcript_mtime(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text("{}\n")
    then = time.time() - 600
    os.utime(f, (then, then))
    assert abs(base.last_activity("claude_code:s", str(f)) - then) < 2
    assert base.last_activity("claude_code:s", str(tmp_path / "gone.jsonl")) is None
    assert base.last_activity("claude_code:s", None) is None


# ---- opencode ---------------------------------------------------------------------------------

from convo_index.sources import opencode as oc   # noqa: E402
from tests.test_opencode import make_opencode_db   # noqa: E402
from tests.test_opencode_v2 import make_opencode_v2_db, T0   # noqa: E402


@pytest.fixture
def opencode_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(oc.shutil, "which", lambda name: "/opt/bin/opencode" if name == "opencode" else None)
    monkeypatch.setattr(base, "is_windows", lambda: False)
    monkeypatch.setattr(oc.tempfile, "tempdir", str(tmp_path))
    return tmp_path


def test_opencode_read_only_turn(opencode_on_path):
    cmd = base.turn_command("opencode:ses_abc", "what changed?", "/proj")
    assert cmd.argv == ["/opt/bin/opencode", "run", "--standalone", "--format", "json", "--session", "ses_abc",
                        "--agent", "plan", "what changed?"]
    assert cmd.stdin is None and cmd.cwd == "/proj"
    assert cmd.env["OPENCODE_CONFIG_PROJECT_DISABLE"] == "1" and cmd.env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    home = cmd.env["XDG_CONFIG_HOME"]
    assert os.path.dirname(home) == str(opencode_on_path) and os.path.isdir(home)
    assert os.listdir(home) == []                                   # nothing of the user's configuration is in it
    assert cmd.scratch == (home,)                                   # the runner removes it when the turn ends
    assert base.turn_command("opencode:ses_abc", "again", "/proj").env["XDG_CONFIG_HOME"] != home   # never reused
    assert cmd.unset == ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CLI_CONFIG_CONTENT")
    rules = json.loads(cmd.env["OPENCODE_CONFIG_CONTENT"])["permissions"]
    assert rules[0] == {"action": "*", "resource": "*", "effect": "deny"}
    assert [r["action"] for r in rules[1:]] == ["read", "glob", "grep", "list"]
    assert all(r["effect"] == "allow" for r in rules[1:])


def test_opencode_edits_turn_allows_edit_never_bash(opencode_on_path):
    cmd = base.turn_command("opencode:ses_abc", "fix it", "/proj", edits=True)
    assert "--agent" not in cmd.argv and "--auto" not in cmd.argv
    rules = json.loads(cmd.env["OPENCODE_CONFIG_CONTENT"])["permissions"]
    assert [r["action"] for r in rules] == ["*", "read", "glob", "grep", "list", "edit"]
    assert rules[-1] == {"action": "edit", "resource": "*", "effect": "allow"}       # project-relative paths
    assert not any(r["action"] == "bash" and r["effect"] != "deny" for r in rules)
    assert base.turn_launcher("opencode:ses_abc") == ["/opt/bin/opencode"]


def test_opencode_prompt_is_one_argv_element(opencode_on_path):
    cmd = base.turn_command("opencode:ses_abc", "a; rm -rf / $(x) `y`", "/proj")
    assert cmd.argv[-1] == "a; rm -rf / $(x) `y`"


def test_opencode_dash_prompt_cannot_become_an_option(opencode_on_path):
    cmd = base.turn_command("opencode:ses_abc", "--help me with this", "/proj")
    assert cmd.argv[-1] == " --help me with this" and not any(a == "--help" for a in cmd.argv)


def test_opencode_turns_are_refused_on_windows(monkeypatch):
    monkeypatch.setattr(oc.shutil, "which", lambda name: "C:\\x\\opencode.cmd")
    monkeypatch.setattr(base, "is_windows", lambda: True)
    assert base.supports_turns("opencode:ses_abc") is False
    assert base.turn_command("opencode:ses_abc", "x", "C:\\proj") is None
    assert base.turn_launcher("opencode:ses_abc") is None


def test_opencode_missing_binary(monkeypatch):
    monkeypatch.setattr(oc.shutil, "which", lambda name: None)
    monkeypatch.setattr(base, "is_windows", lambda: False)
    assert base.turn_command("opencode:ses_abc", "x", "/proj") is None


# Shapes captured from `opencode run --format json` on 2.1.4, content replaced.
OPENCODE_STREAM = [
    {"type": "step_start", "timestamp": 1, "sessionID": "ses_1", "part": {"id": "p1", "type": "step-start"}},
    {"type": "tool_use", "timestamp": 2, "sessionID": "ses_1", "part": {
        "id": "p2", "type": "tool", "tool": "read", "state": {"status": "running", "input": {"path": "hello.txt"}}}},
    {"type": "tool_use", "timestamp": 3, "sessionID": "ses_1", "part": {
        "id": "p2", "type": "tool", "tool": "read", "state": {"status": "completed", "input": {"path": "hello.txt"},
                                                              "output": "Read file hello.txt, lines 1-1\n1: hello"}}},
    {"type": "step_finish", "timestamp": 4, "sessionID": "ses_1", "part": {"id": "p3", "type": "step-finish",
                                                                             "reason": "tool-calls", "cost": 0}},
    {"type": "tool_use", "timestamp": 5, "sessionID": "ses_1", "part": {
        "id": "p4", "type": "tool", "tool": "write", "state": {"status": "error", "input": {}, "error": "denied"}}},
    {"type": "text", "timestamp": 6, "sessionID": "ses_1", "part": {"id": "p5", "type": "text", "text": "hello"}},
]


def test_opencode_stream_is_translated():
    events = [e for line in OPENCODE_STREAM for e in base.turn_events("opencode:ses_1", json.dumps(line))]
    assert events == [
        {"kind": "tool", "name": "read", "input": '{"path": "hello.txt"}'},
        {"kind": "tool_result", "text": "Read file hello.txt, lines 1-1\n1: hello", "error": False},
        {"kind": "tool_result", "text": "denied", "error": True},
        {"kind": "text", "text": "hello"},
    ]
    assert oc.turn_events("garbage") == [] and oc.turn_events('{"type": "text"}') == []
    assert oc.turn_events(json.dumps({"type": "error", "part": {"type": "error", "error": "no provider"}})) == [
        {"kind": "error", "message": "no provider"}]


def test_opencode_step_failure_without_a_part_keeps_its_message():
    """`opencode run --format json` reports a failed step at the top level, with no part."""
    line = {"type": "error", "timestamp": 1, "sessionID": "ses_1",
            "error": {"name": "APIError", "data": {"message": "rate limited", "statusCode": 429}}}
    assert oc.turn_events(json.dumps(line)) == [{"kind": "error", "message": "rate limited"}]
    assert oc.turn_events(json.dumps({"type": "error", "error": {"name": "ProviderAuthError"}})) == [
        {"kind": "error", "message": "ProviderAuthError"}]
    assert oc.turn_events(json.dumps({"type": "error"})) == [{"kind": "error", "message": "turn failed"}]


def test_opencode_last_activity_is_the_session_row_not_the_file(tmp_path):
    db = tmp_path / "opencode.db"
    make_opencode_v2_db(db)
    locator = f"sqlite:{db}#ses_1"
    assert base.last_activity("opencode:ses_1", locator) == (T0 + 60_000) / 1000
    assert base.last_activity("opencode:ses_other", locator) is None      # locator and id must agree
    assert base.last_activity("opencode:ses_1", str(db)) is None           # not a sqlite locator
    assert base.last_activity("opencode:ses_1", f"sqlite:{tmp_path / 'none.db'}#ses_1") is None


def test_opencode_last_activity_reads_the_1x_layout_too(tmp_path):
    db = tmp_path / "old.db"
    make_opencode_db(db)
    assert base.last_activity("opencode:ses_1", f"sqlite:{db}#ses_1") is not None
