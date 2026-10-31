"""Per-harness browser-turn commands: the closing flags, where the prompt travels, how the
harness's stream is translated, and how recent activity is measured."""
import json
import os
import sys
import time

import pytest

from convo_index.sources import base
from convo_index.sources import claude_code as cc


# ---- Claude Code ------------------------------------------------------------------------------

@pytest.fixture
def claude_mcp(monkeypatch, tmp_path):
    """A user config with three MCP servers, found through CLAUDE_CONFIG_DIR; turn scratch in tmp."""
    home = tmp_path / "claude-home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "atlassian": {"type": "http", "url": "https://mcp.example/a", "headers": {"Authorization": "Bearer t"}},
        "claude_ai_Datadog": {"type": "stdio", "command": "dd-mcp"},
        "slack": {"type": "http", "url": "https://mcp.example/s"}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setattr(cc.tempfile, "tempdir", str(tmp_path))


@pytest.fixture
def claude_on_path(monkeypatch):
    monkeypatch.setattr(base.shutil, "which", lambda name: "/opt/bin/claude" if name == "claude" else None)
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


def test_claude_new_session_keeps_the_closing_flags_and_opens_only_the_read_dirs(claude_on_path, claude_mcp):
    cmd, sid = cc.new_session_command("take over", "/proj", read_dirs=["/data/handoffs/x"], model="claude-sonnet-5-5")
    assert cmd.argv == ["/opt/bin/claude", "--session-id", sid,
                        "-p", "--output-format", "stream-json", "--verbose", "--safe-mode", "--restricted",
                        "--strict-mcp-config", "--disable-slash-commands", "--permission-prompts", "none",
                        "--tools", "Read,Glob,Grep", "--add-dir", "/data/handoffs/x", "--model", "claude-sonnet-5-5"]
    assert cmd.stdin == "take over" and len(sid) == 36 and "--resume" not in cmd.argv
    edit, sid2 = cc.new_session_command("go on", "/proj", edits=True, read_dirs=["/d"])
    assert sid2 != sid and edit.argv[1:3] == ["--session-id", sid2] and edit.argv[-2:] == ["--add-dir", "/d"]
    assert "Bash" in edit.argv[edit.argv.index("--tools") + 1] and "--safe-mode" not in edit.argv
    init = json.dumps({"type": "system", "subtype": "init", "session_id": sid})
    assert cc.session_id(init) == sid and cc.session_id('{"type": "assistant"}') is None and cc.session_id("nope") is None


def test_claude_read_only_turn_ignores_the_mcp_allowlist(claude_on_path, claude_mcp):
    cmd = base.turn_command("claude_code:abc", "what changed?", "/proj", mcp_tools=("mcp__atlassian__*",))
    assert "--safe-mode" in cmd.argv and "--mcp-config" not in cmd.argv and "Bash" not in " ".join(cmd.argv)
    assert cmd.scratch == ()


def test_claude_edit_turn_adds_file_tools_and_shell_and_no_mcp_without_an_allowlist(claude_on_path, claude_mcp):
    cmd = base.turn_command("claude_code:abc", "fix it", "/proj", edits=True)
    assert cmd.argv == ["/opt/bin/claude", "--resume", "abc",
                        "-p", "--output-format", "stream-json", "--verbose", "--setting-sources", "", "--restricted",
                        "--strict-mcp-config", "--disable-slash-commands", "--permission-prompts", "none",
                        "--tools", "Read,Glob,Grep,Edit,Write,MultiEdit,Bash", "--permission-mode", "acceptEdits",
                        "--allowedTools", "Bash"]
    assert "--safe-mode" not in cmd.argv            # it would drop --mcp-config servers too
    assert cmd.scratch == () and cmd.stdin == "fix it"


def test_claude_edit_turn_loads_only_the_allowlisted_servers(claude_on_path, claude_mcp):
    cmd = base.turn_command("claude_code:abc", "fix it", "/proj", edits=True,
                            mcp_tools=("mcp__atlassian__getJiraIssue", "mcp__claude_ai_Datadog__*", "mcp__gone__x"))
    assert cmd.argv[-2:] == ["--allowedTools", "Bash,mcp__atlassian__getJiraIssue,mcp__claude_ai_Datadog"]
    config = cmd.argv[cmd.argv.index("--mcp-config") + 1]
    assert os.path.dirname(config) == cmd.scratch[0]                     # removed by the runner with the turn
    assert json.loads(open(config).read()) == {"mcpServers": {
        "atlassian": {"type": "http", "url": "https://mcp.example/a", "headers": {"Authorization": "Bearer t"}},
        "claude_ai_Datadog": {"type": "stdio", "command": "dd-mcp"}}}    # "slack" is configured but not allowlisted
    if os.name != "nt":
        assert os.stat(config).st_mode & 0o777 == 0o600


def test_claude_mcp_servers_reads_the_user_config(claude_mcp, monkeypatch, tmp_path):
    assert sorted(cc.mcp_servers()) == ["atlassian", "claude_ai_Datadog", "slack"]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    assert cc.mcp_servers() == {}


def test_claude_turn_helpers_leave_the_transcript_parser_alone():
    # the turn translator has its own, shorter clip; the indexer's limit and helper are untouched
    assert cc.TOOL_TEXT_LIMIT == 4000 and cc.TURN_TEXT_LIMIT == 400
    long_result = {"type": "tool_result", "content": "r" * 1000}
    assert len(cc._tool_result_text(long_result)) == 1000
    assert len(cc._turn_result_text(long_result)) == cc.TURN_TEXT_LIMIT + 1


def test_claude_missing_binary_means_no_command(monkeypatch):
    monkeypatch.setattr(base.shutil, "which", lambda name: None)
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
    monkeypatch.setattr(base.shutil, "which", lambda name: {"claude": str(shim), "node": "C:\\node\\node.exe"}.get(name))
    cmd = cc.turn_command("abc", "x", "C:\\proj")
    assert cmd.argv[:2] == ["C:\\node\\node.exe", str(script)]
    assert not any(a.lower().endswith(".cmd") for a in cmd.argv)


def test_claude_windows_npm6_shim_is_bypassed_for_node(monkeypatch, tmp_path):
    shim = tmp_path / "claude.cmd"
    script = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("// entry")
    shim.write_text('@IF EXIST "%~dp0\\node.exe" (\r\n  "%~dp0\\node.exe"  '
                    '"%~dp0\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n) ELSE (\r\n'
                    '  node  "%~dp0\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n)\r\n')
    monkeypatch.setattr(base, "is_windows", lambda: True)
    monkeypatch.setattr(base.shutil, "which", lambda name: {"claude": str(shim), "node": "C:\\node\\node.exe"}.get(name))
    assert cc.turn_command("abc", "x", "C:\\proj").argv[:2] == ["C:\\node\\node.exe", str(script)]


def test_claude_windows_shim_without_a_script_is_refused(monkeypatch, tmp_path):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@ECHO off\r\nsomething else\r\n")
    monkeypatch.setattr(base, "is_windows", lambda: True)
    monkeypatch.setattr(base.shutil, "which", lambda name: {"claude": str(shim), "node": "node"}.get(name))
    assert cc.turn_command("abc", "x", "C:\\proj") is None
    assert base.turn_launcher("claude_code:abc") is None            # what doctor and the UI see


def test_claude_windows_native_exe_is_used_directly(monkeypatch):
    monkeypatch.setattr(base, "is_windows", lambda: True)
    monkeypatch.setattr(base.shutil, "which", lambda name: "C:\\Users\\me\\.local\\bin\\claude.exe")
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


def test_opencode_edit_turn_allows_edit_and_bash(opencode_on_path):
    cmd = base.turn_command("opencode:ses_abc", "fix it", "/proj", edits=True, mcp_tools=("mcp__atlassian__*",))
    assert "--agent" not in cmd.argv and "--auto" not in cmd.argv
    rules = json.loads(cmd.env["OPENCODE_CONFIG_CONTENT"])["permissions"]
    assert [r["action"] for r in rules] == ["*", "read", "glob", "grep", "list", "edit", "bash"]
    assert rules[-2] == {"action": "edit", "resource": "*", "effect": "allow"}       # project-relative paths
    assert rules[-1] == {"action": "bash", "resource": "*", "effect": "allow"}
    assert os.listdir(cmd.env["XDG_CONFIG_HOME"]) == []      # no MCP server: the allowlist is Claude Code's
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


def test_opencode_turn_names_the_sessions_own_model(opencode_on_path, tmp_path):
    db = tmp_path / "opencode.db"
    make_opencode_v2_db(db)
    cmd = base.turn_command("opencode:ses_1", "again", "/proj", locator=f"sqlite:{db}#ses_1")
    assert cmd.argv[cmd.argv.index("--session") + 1:cmd.argv.index("--session") + 4] == ["ses_1", "-m", "ollama/qwen3.8:27b-mlx"]
    for locator in [None, f"sqlite:{db}#ses_other", f"sqlite:{tmp_path / 'none.db'}#ses_1"]:
        assert "-m" not in base.turn_command("opencode:ses_1", "again", "/proj", locator=locator).argv


def test_opencode_last_activity_reads_the_1x_layout_too(tmp_path):
    db = tmp_path / "old.db"
    make_opencode_db(db)
    assert base.last_activity("opencode:ses_1", f"sqlite:{db}#ses_1") is not None


# ---- pi ---------------------------------------------------------------------------------------

from convo_index.sources import pi   # noqa: E402
from tests.test_pi import SID as PI_SID, write_session as write_pi_session   # noqa: E402


@pytest.fixture
def pi_on_path(monkeypatch):
    monkeypatch.setattr(base.shutil, "which", lambda name: "/opt/bin/pi" if name == "pi" else None)
    monkeypatch.setattr(base, "is_windows", lambda: False)


@pytest.fixture
def pi_file(tmp_path):
    return str(write_pi_session(tmp_path / "sessions" / "--proj--"))


def test_pi_read_only_turn_resumes_the_file_with_read_tools_and_the_prompt_on_stdin(pi_on_path, pi_file):
    cmd = base.turn_command(f"pi:{PI_SID}", "what changed?", "/proj", locator=pi_file)
    assert cmd.argv[0] == "/opt/bin/pi" and cmd.cwd == "/proj" and cmd.stdin == "what changed?"
    assert "what changed?" not in cmd.argv
    for flag in ["-p", "--no-extensions", "--no-mcp", "--no-skills", "--no-prompt-templates", "--no-context-files",
                 "--no-themes", "--no-approve", "--offline"]:
        assert flag in cmd.argv
    assert cmd.argv[cmd.argv.index("--mode") + 1] == "json"
    assert cmd.argv[cmd.argv.index("--session") + 1] == pi_file          # by file: no fork question on stdin
    assert cmd.argv[cmd.argv.index("--tools") + 1] == "read,grep,find,ls"


def test_pi_edit_turn_adds_edits_and_shell_but_no_mcp(pi_on_path, pi_file):
    cmd = base.turn_command(f"pi:{PI_SID}", "fix it", "/proj", edits=True, mcp_tools=("mcp__atlassian__*",), locator=pi_file)
    assert cmd.argv[cmd.argv.index("--tools") + 1] == "read,grep,find,ls,edit,write,bash"
    assert "--no-mcp" in cmd.argv and not any("mcp__" in a for a in cmd.argv)


def test_pi_refuses_a_session_file_that_is_gone_or_holds_another_session(pi_on_path, pi_file, tmp_path):
    other = str(write_pi_session(tmp_path / "other", name="x.jsonl"))
    for locator in [None, str(tmp_path / "gone.jsonl"), "relative.jsonl", other]:
        sid = PI_SID if locator != other else "0f1e2d3c-1234-4abc-8def-0123456789ab"
        with pytest.raises(ValueError, match="no longer holds"):
            base.turn_command(f"pi:{sid}", "x", "/proj", locator=locator)


def test_pi_missing_binary_means_no_command(monkeypatch, pi_file):
    monkeypatch.setattr(base.shutil, "which", lambda name: None)
    assert base.turn_command(f"pi:{PI_SID}", "x", "/proj", locator=pi_file) is None
    assert base.turn_launcher(f"pi:{PI_SID}") is None


def test_pi_windows_npm_shim_is_bypassed_for_node(monkeypatch, tmp_path):
    shim = tmp_path / "pi.cmd"
    script = tmp_path / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("// entry")
    shim.write_text('@ECHO off\r\nendLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
                    '"%dp0%\\node_modules\\@earendil-works\\pi-coding-agent\\dist\\bundle\\cli.js" %*\r\n')
    monkeypatch.setattr(base, "is_windows", lambda: True)
    monkeypatch.setattr(base.shutil, "which", lambda name: {"pi": str(shim), "node": "C:\\node\\node.exe"}.get(name))
    assert base.supports_turns(f"pi:{PI_SID}")
    assert base.turn_launcher(f"pi:{PI_SID}") == ["C:\\node\\node.exe", str(script)]


# Captured from pi 1.4.0 `--mode json` (streaming deltas, the system and user messages and most
# of each message's fields dropped; paths shortened).
PI_USAGE = {"input": 1317, "output": 35, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 1352,
            "cost": {"input": 0.001, "output": 0.002, "cacheRead": 0, "cacheWrite": 0, "total": 0.003}}
PI_ASSISTANT_1 = {"role": "assistant", "provider": "ollama", "model": "flint:40b-a2b-qat-int4", "usage": PI_USAGE,
                  "stopReason": "toolUse",
                  "content": [{"type": "thinking", "thinking": "Let me read both.", "thinkingSignature": "reasoning"},
                              {"type": "toolCall", "id": "call_sce8utoy", "name": "read", "arguments": {"path": "note.txt"}},
                              {"type": "toolCall", "id": "call_cleuv6bk", "name": "read", "arguments": {"path": "missing.txt"}}]}
PI_ASSISTANT_2 = {"role": "assistant", "provider": "ollama", "model": "flint:40b-a2b-qat-int4", "usage": PI_USAGE,
                  "stopReason": "stop",
                  "content": [{"type": "thinking", "thinking": "No bash tool.", "thinkingSignature": "reasoning"},
                              {"type": "text", "text": "note.txt says hello; missing.txt does not exist; no bash tool."}]}
PI_STREAM = [
    {"type": "session", "version": 3, "id": "4b72a124-c133-4526-ab9e-b92764ae3b9e", "cwd": "/proj"},
    {"type": "agent_start"},
    {"type": "turn_start"},
    {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "Use the read tool"}]}},
    {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 1, "delta": "P"}},
    {"type": "message_end", "message": PI_ASSISTANT_1},
    {"type": "tool_execution_start", "toolCallId": "call_sce8utoy", "toolName": "read", "args": {"path": "note.txt"}},
    {"type": "tool_execution_end", "toolCallId": "call_sce8utoy", "toolName": "read",
     "result": {"content": [{"type": "text", "text": "hello from probe\n"}]}, "isError": False},
    {"type": "message_end", "message": {"role": "toolResult", "toolCallId": "call_sce8utoy", "toolName": "read",
                                        "content": [{"type": "text", "text": "hello from probe\n"}], "isError": False}},
    {"type": "message_end", "message": {"role": "toolResult", "toolCallId": "call_cleuv6bk", "toolName": "read",
                                        "content": [{"type": "text", "text": "ENOENT: no such file or directory, access '/proj/missing.txt'"}],
                                        "details": {}, "isError": True}},
    {"type": "turn_end", "message": PI_ASSISTANT_1},
    {"type": "message_end", "message": PI_ASSISTANT_2},
    {"type": "agent_end", "messages": [{"role": "user", "content": [{"type": "text", "text": "Use the read tool"}]},
                                       PI_ASSISTANT_1, PI_ASSISTANT_2]},
    {"type": "agent_settled", "aborted": False},
]


def test_pi_stream_is_translated_and_noise_dropped():
    events = [e for line in PI_STREAM for e in base.turn_events(f"pi:{PI_SID}", json.dumps(line))]
    assert [e["kind"] for e in events] == ["tool", "tool", "tool_result", "tool_result", "text", "done"]
    assert events[0] == {"kind": "tool", "name": "read", "input": '{"path": "note.txt"}'}
    assert events[2] == {"kind": "tool_result", "text": "hello from probe\n", "error": False}
    assert events[3]["error"] is True and events[3]["text"].startswith("ENOENT")
    assert events[4]["text"].startswith("note.txt says hello")
    assert events[5] == {"kind": "done", "cost_usd": pytest.approx(0.006), "turns": 2}


def _pi_assistant(stop, text="", error=None):
    m = {"role": "assistant", "content": [{"type": "text", "text": text}] if text else [], "stopReason": stop,
         "usage": {"input": 1, "output": 1, "cost": {"total": 0.01}}}
    return {**m, "errorMessage": error} if error else m


def test_pi_failed_request_is_an_error_though_pi_exits_0():
    failed = _pi_assistant("error", error="404: model 'nosuchmodel-xyz' not found")
    assert pi.turn_events(json.dumps({"type": "message_end", "message": failed})) == []
    assert pi.turn_events(json.dumps({"type": "agent_end", "messages": [failed], "willRetry": False})) == [
        {"kind": "error", "message": "404: model 'nosuchmodel-xyz' not found"}]
    assert pi.turn_events(json.dumps({"type": "agent_end", "messages": [_pi_assistant("aborted")]})) == [
        {"kind": "error", "message": "request aborted"}]
    assert pi.turn_events(json.dumps({"type": "agent_end", "messages": []})) == [{"kind": "done", "cost_usd": None, "turns": 0}]
    assert pi.turn_events("not json") == [] and pi.turn_events("[1]") == [] and pi.turn_events('{"type": "message_end"}') == []


def test_pi_truncated_final_response_is_an_error():
    cut = _pi_assistant("length", text="Partial reply")
    assert pi.turn_events(json.dumps({"type": "message_end", "message": cut})) == [{"kind": "text", "text": "Partial reply"}]
    (ev,) = pi.turn_events(json.dumps({"type": "agent_end", "messages": [cut], "willRetry": False}))
    assert ev["kind"] == "error" and "output limit" in ev["message"]


def test_pi_retried_failure_ends_as_the_retry_does():
    """Shaped like pi 1.4.0's auto-retry: a failed run whose agent_end says willRetry, the retry
    notice, then a second run. Through the runner, the turn's outcome is the last verdict."""
    from convo_index import turns
    failed = _pi_assistant("error", error="503 overloaded")
    lines = [{"type": "message_end", "message": failed},
             {"type": "agent_end", "messages": [failed], "willRetry": True},
             {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "delayMs": 2000, "errorMessage": "503 overloaded"},
             {"type": "message_end", "message": _pi_assistant("stop", text="Recovered.")},
             {"type": "agent_end", "messages": [_pi_assistant("stop", text="Recovered.")], "willRetry": False},
             {"type": "agent_settled", "aborted": False}]
    events = [e for line in lines for e in pi.turn_events(json.dumps(line))]
    assert [e["kind"] for e in events] == ["text", "text", "done"]
    assert "retrying (attempt 1 of 3) after: 503 overloaded" in events[0]["text"]
    script = "import sys\nfor l in sys.argv[1:]: print(l)"
    cmd = base.TurnCommand(argv=[sys.executable, "-c", script] + [json.dumps(x) for x in lines], cwd=os.getcwd())
    r = turns.TurnRunner(refresh=lambda: {"ok": True, "busy": False})
    t = r.start(f"pi:{PI_SID}", cmd, pi.turn_events)
    last = [ev for _, ev in r.events(t.id) if ev][-1]
    assert last["kind"] == "done" and last["cost_usd"] == pytest.approx(0.01)
    lines[-2] = {"type": "agent_end", "messages": [failed], "willRetry": False}     # the retries ran out
    cmd = base.TurnCommand(argv=[sys.executable, "-c", script] + [json.dumps(x) for x in lines], cwd=os.getcwd())
    t = r.start(f"pi:{PI_SID}", cmd, pi.turn_events)
    last = [ev for _, ev in r.events(t.id) if ev][-1]
    assert last["kind"] == "error" and last["message"] == "503 overloaded"


def test_pi_long_tool_input_is_clipped():
    line = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "name": "write", "arguments": {"content": "x" * 5000}}]}})
    (ev,) = pi.turn_events(line)
    assert len(ev["input"]) <= pi.TURN_TEXT_LIMIT + 1 and ev["input"].endswith("…")


def test_pi_last_activity_is_the_session_file_mtime(pi_file, tmp_path):
    then = time.time() - 600
    os.utime(pi_file, (then, then))
    assert abs(base.last_activity(f"pi:{PI_SID}", pi_file) - then) < 2
    assert base.last_activity(f"pi:{PI_SID}", str(tmp_path / "gone.jsonl")) is None


# ---- Codex ------------------------------------------------------------------------------------

from convo_index.sources import codex   # noqa: E402

CODEX_TID = "01a19110-163e-7c21-8b8d-c6a1e61530f4"


@pytest.fixture
def codex_on_path(monkeypatch):
    monkeypatch.setattr(base.shutil, "which", lambda name: "/opt/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(base, "is_windows", lambda: False)


def write_rollout(directory, tid=CODEX_TID, provider="openai", models=("gpt-6.1-terra", "gpt-6.1-sol")):
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / f"rollout-2026-10-31T14-05-01-{tid}.jsonl"
    lines = [{"timestamp": "2026-10-31T08:35:01Z", "type": "session_meta",
              "payload": {"id": tid, "session_id": tid, "cwd": "/proj", "cli_version": "0.166.0", "model_provider": provider}}]
    for m in models:
        lines.append({"timestamp": "2026-10-31T08:35:02Z", "type": "turn_context",
                      "payload": {"model": m, "cwd": "/proj", "sandbox_policy": {"type": "read-only"}, "approval_policy": "never"}})
        lines.append({"timestamp": "2026-10-31T08:35:03Z", "type": "response_item",
                      "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}})
    f.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return str(f)


@pytest.fixture
def rollout(tmp_path):
    return write_rollout(tmp_path / "sessions" / "2026" / "10" / "31")


def test_codex_read_only_turn_closes_the_surface_and_resumes_with_the_recorded_model(codex_on_path, rollout):
    cmd = base.turn_command(f"codex:{CODEX_TID}", "what changed?", "/proj", locator=rollout)
    a = cmd.argv
    assert a[:3] == ["/opt/bin/codex", "exec", "--json"] and cmd.stdin == "what changed?" and "what changed?" not in a
    for flag in ["--strict-config", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"]:
        assert flag in a
    disabled = {a[i + 1] for i, x in enumerate(a) if x == "--disable"}
    assert {"apps", "plugins", "hooks", "multi_agent", "computer_use", "browser_use", "memories"} <= disabled
    overrides = {a[i + 1] for i, x in enumerate(a) if x == "-c"}
    assert {"project_doc_max_bytes=0", "skills.include_instructions=false", 'web_search="disabled"',
            'approval_policy="never"', 'model_provider="openai"'} <= overrides
    assert a[a.index("-m") + 1] == "gpt-6.1-sol"                        # the thread's latest model, not Codex's default
    assert a[a.index("-s") + 1] == "read-only" and a[a.index("-C") + 1] == "/proj"
    assert a[-3:] == ["resume", CODEX_TID, "-"]                          # options before the subcommand; prompt from stdin


def test_codex_edit_turn_uses_the_workspace_write_sandbox_and_no_mcp(codex_on_path, rollout):
    cmd = base.turn_command(f"codex:{CODEX_TID}", "fix it", "/proj", edits=True, mcp_tools=("mcp__atlassian__*",), locator=rollout)
    assert cmd.argv[cmd.argv.index("-s") + 1] == "workspace-write"
    assert not any("mcp__" in x or "mcp_servers" in x for x in cmd.argv) and "danger-full-access" not in cmd.argv


def test_codex_refuses_names_other_threads_and_missing_rollouts(codex_on_path, rollout, tmp_path):
    other = write_rollout(tmp_path / "other", tid="0f1e2d3c-1234-4abc-8def-0123456789ab")
    cases = [("my-thread-name", rollout),          # resume would look a thread name up
             (CODEX_TID, None), (CODEX_TID, "relative.jsonl"), (CODEX_TID, str(tmp_path / "gone.jsonl")), (CODEX_TID, other)]
    for tid, locator in cases:
        with pytest.raises(ValueError, match="no longer holds"):
            base.turn_command(f"codex:{tid}", "x", "/proj", locator=locator)


def test_codex_rollout_of_a_release_that_wrote_only_session_id(codex_on_path, tmp_path):
    from tests.test_codex import TID, default_rollout
    f = tmp_path / f"rollout-{TID}.jsonl"
    f.write_text("\n".join(default_rollout("hello")) + "\n")
    cmd = base.turn_command(f"codex:{TID}", "x", "/proj", locator=str(f))
    assert cmd.argv[-3:] == ["resume", TID, "-"]


def test_codex_malformed_records_in_the_rollout_are_skipped(codex_on_path, rollout):
    with open(rollout, "a") as fh:
        fh.write('["turn_context"]\n3\nnot json\n')
    assert base.turn_command(f"codex:{CODEX_TID}", "x", "/proj", locator=rollout).argv[-3:] == ["resume", CODEX_TID, "-"]


def test_codex_recorded_names_that_could_be_options_are_not_passed(codex_on_path, tmp_path):
    f = write_rollout(tmp_path, provider="--danger", models=("-m",))
    cmd = base.turn_command(f"codex:{CODEX_TID}", "x", "/proj", locator=f)
    assert "-m" not in cmd.argv and not any("--danger" in x for x in cmd.argv)


def test_codex_turns_are_not_offered_on_windows_or_without_the_binary(monkeypatch, rollout):
    monkeypatch.setattr(base, "is_windows", lambda: True)
    assert base.supports_turns(f"codex:{CODEX_TID}") is False and base.turn_launcher(f"codex:{CODEX_TID}") is None
    monkeypatch.setattr(base, "is_windows", lambda: False)
    monkeypatch.setattr(base.shutil, "which", lambda name: None)
    assert base.turn_command(f"codex:{CODEX_TID}", "x", "/proj", locator=rollout) is None


# Captured from codex-cli 0.166.0 `exec --json` (reasoning items and most of each payload
# dropped; paths shortened).
CODEX_STREAM = [
    {"type": "thread.started", "thread_id": CODEX_TID},
    {"type": "item.completed", "item": {"id": "item_0", "type": "error",
                                        "message": "This session was recorded with model `a` but is resuming with `b`."}},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_1", "type": "reasoning", "text": "Read it first."}},
    {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "I'll read `fact.txt`."}},
    {"type": "item.started", "item": {"id": "item_3", "type": "command_execution", "command": "/bin/zsh -lc 'cat fact.txt'",
                                      "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_3", "type": "command_execution", "command": "/bin/zsh -lc 'cat fact.txt'",
                                        "aggregated_output": "the answer is 42\n", "exit_code": 0, "status": "completed"}},
    {"type": "item.started", "item": {"id": "item_4", "type": "command_execution", "command": "/bin/zsh -lc 'touch ro.txt'",
                                      "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_4", "type": "command_execution", "command": "/bin/zsh -lc 'touch ro.txt'",
                                        "aggregated_output": "touch: ro.txt: Operation not permitted\n", "exit_code": 1, "status": "failed"}},
    {"type": "item.started", "item": {"id": "item_5", "type": "mcp_tool_call", "server": "codex_apps", "tool": "github.get_profile",
                                      "arguments": {}, "result": None, "error": None, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_5", "type": "mcp_tool_call", "server": "codex_apps", "tool": "github.get_profile",
                                        "arguments": {}, "result": None,
                                        "error": {"message": "MCP tool call requires approval, but approval policy is never"},
                                        "status": "failed"}},
    {"type": "item.completed", "item": {"id": "item_6", "type": "file_change", "status": "completed",
                                        "changes": [{"path": "/proj/note.txt", "kind": "update"}]}},
    {"type": "item.completed", "item": {"id": "item_7", "type": "agent_message", "text": "`fact.txt`: the answer is 42"}},
    {"type": "turn.completed", "usage": {"input_tokens": 95487, "cached_input_tokens": 87544, "output_tokens": 3126}},
]


def test_codex_stream_is_translated_and_noise_dropped():
    events = [e for line in CODEX_STREAM for e in base.turn_events(f"codex:{CODEX_TID}", json.dumps(line))]
    assert [e["kind"] for e in events] == ["text", "tool", "tool_result", "tool", "tool_result", "tool", "tool_result",
                                           "tool", "tool_result", "text", "done"]
    assert events[1] == {"kind": "tool", "name": "shell", "input": "/bin/zsh -lc 'cat fact.txt'"}
    assert events[2] == {"kind": "tool_result", "text": "the answer is 42\n", "error": False}
    assert events[4]["error"] is True and events[4]["text"].startswith("exit 1\ntouch: ro.txt")
    assert events[5]["name"] == "codex_apps.github.get_profile" and events[6]["error"] is True
    assert events[7] == {"kind": "tool", "name": "edit", "input": "update /proj/note.txt"}
    assert events[10] == {"kind": "done", "cost_usd": None}


def test_codex_failed_turn_is_an_error_and_a_stream_notice_is_not():
    failed = {"type": "turn.failed", "error": {"message": "unknown input item type"}}
    assert codex.turn_events(json.dumps(failed)) == [{"kind": "error", "message": "unknown input item type"}]
    assert codex.turn_events(json.dumps({"type": "error", "message": "Reconnecting... 2/5"})) == [
        {"kind": "text", "text": "[codex: Reconnecting... 2/5]"}]
    assert codex.turn_events("not json") == [] and codex.turn_events("[1]") == [] and codex.turn_events('{"type": "item.completed"}') == []


def test_codex_last_activity_is_the_rollout_mtime(rollout, tmp_path):
    then = time.time() - 600
    os.utime(rollout, (then, then))
    assert abs(base.last_activity(f"codex:{CODEX_TID}", rollout) - then) < 2
    assert base.last_activity(f"codex:{CODEX_TID}", str(tmp_path / "gone.jsonl")) is None
