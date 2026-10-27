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
