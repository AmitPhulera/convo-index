"""Export, MCP server, scheduler plan and the CLI round trips."""
import io
import json
import os
from pathlib import Path

import pytest

from convo_index import cli
from convo_index import db as dbm
from convo_index import export as X
from convo_index import mcp
from convo_index import schedule as SCH
from convo_index import settings as S
from convo_index.model import Conversation, Message, UsageRecord, KIND_TOOL_CALL, KIND_TOOL_RESULT
from convo_index.sources.base import resume_command
from tests.conftest import make_settings


def _linux(monkeypatch, systemd):
    """A Linux host with a working systemd user session, or one with only crontab."""
    monkeypatch.setattr(SCH, "systemd_user_available", lambda: systemd)
    monkeypatch.setattr(SCH.shutil, "which", lambda name: {"systemctl": "/usr/bin/systemctl" if systemd else None,
                                                           "crontab": "/usr/bin/crontab"}.get(name))


def _seed(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    st["paths"]["db"] = str(tmp_path / "convos.db")
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    monkeypatch.setenv("CONVO_INDEX_DB", str(tmp_path / "convos.db"))
    db = tmp_path / "convos.db"
    conn = dbm.connect(db); dbm.init_schema(conn, db)
    from convo_index import tags as T, references as REF
    T.sync_definitions(conn, st)
    for i, (title, txt) in enumerate([("Kube deploy", "kubernetes deployment failing on SAAS-42"),
                                      ("Astrology chart", "draw an astrology chart")]):
        c = Conversation(source="claude_code", native_id=f"s{i}", title=title, first_prompt=txt, source_ref="/a", cwd="/Users/me/proj",
                         git_branch="main", start_ts=f"2026-06-1{i}T00:00:00Z", end_ts=f"2026-06-1{i}T01:00:00Z",
                         messages=[Message(seq=0, role="user", text=txt, ts="2026-06-10T00:00:00Z"),
                                   Message(seq=1, role="assistant", text="[bash] kubectl get pods", kind=KIND_TOOL_CALL),
                                   Message(seq=2, role="tool", text="pod-1 CrashLoopBackOff", kind=KIND_TOOL_RESULT),
                                   Message(seq=3, role="assistant", text="Restart the deployment.")],
                         usage=[UsageRecord(f"r{i}", 3, "anthropic", "claude-haiku-4-5", input_uncached=1_000_000, output=100_000)])
        dbm.upsert_conversation(conn, c, "unknown", None, [], None, "1", lambda t: [None] * len(t))
        REF.store(conn, c, REF.extract(c, st))
    dbm.set_summary(conn, "claude_code:s0", {"problem": "pods crash", "solution": "restart", "learning": "check limits"}, "1")
    T.set_decision(conn, "claude_code:s0", "workspace", "globex", suggest=False)
    conn.commit(); conn.close()
    return st, db


def test_export_dict_and_markdown(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    conn = dbm.connect(db)
    d = X.as_dict(conn, "claude_code:s0", st)
    assert d["title"] == "Kube deploy" and d["summary"]["problem"] == "pods crash" and d["tags"] == {"workspace": ["globex"]}
    assert [r["key"] for r in d["references"]] == ["SAAS-42"] and d["messages_total"] == 4 and len(d["messages"]) == 4
    assert abs(d["usage"]["estimated_cost"] - 1.5) < 1e-9 and d["resume_cmd"] == resume_command("claude_code:s0", "/Users/me/proj")
    slim = X.as_dict(conn, "claude_code:s0", st, include_tools=False, max_messages=1, max_chars=10)
    assert len(slim["messages"]) == 1 and slim["messages"][0]["text"].startswith("kubernetes…") and "more chars" in slim["messages"][0]["text"]
    md = X.as_markdown(conn, "claude_code:s0", st)
    assert md.startswith("# Kube deploy") and "## Summary" in md and "**Problem.** pods crash" in md
    assert "- tags: workspace:globex" in md and "- references: ticket SAAS-42" in md and "$1.50 API-equivalent estimate" in md
    assert "### Assistant → tool" not in md and "Restart the deployment." in md          # tools excluded by default
    assert "```" in X.as_markdown(conn, "claude_code:s0", st, include_tools=True)
    assert X.as_dict(conn, "claude_code:nope", st) is None and X.as_markdown(conn, "claude_code:nope", st) is None


def test_mcp_handshake_tools_and_calls(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr("convo_index.server.embed", lambda texts: [None] * len(texts))
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, st)
    assert init["result"]["protocolVersion"] == "2025-03-26" and init["result"]["serverInfo"]["name"] == "convo-index"
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, st) is None
    tools = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, st)["result"]["tools"]
    assert [t["name"] for t in tools] == ["search_conversations", "get_conversation", "get_related"]
    assert all("inputSchema" in t and t["inputSchema"]["type"] == "object" for t in tools)
    r = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "search_conversations", "arguments": {"query": "kubernetes", "limit": 5}}}, st)["result"]
    assert not r.get("isError") and r["structuredContent"]["count"] == 1
    hit = r["structuredContent"]["results"][0]
    assert hit["id"] == "claude_code:s0" and hit["source"] == "claude_code" and hit["fidelity"] == "full" and "snippet" in hit
    assert json.loads(r["content"][0]["text"])["count"] == 1
    r = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "search_conversations", "arguments": {"query": "", "tags": ["workspace:globex"]}}}, st)["result"]
    assert [h["id"] for h in r["structuredContent"]["results"]] == ["claude_code:s0"]
    g = mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "get_conversation", "arguments": {"id": "s0"}}}, st)["result"]   # legacy bare id resolves
    assert g["structuredContent"]["title"] == "Kube deploy" and len(g["structuredContent"]["messages"]) == 2   # no tools by default
    assert g["structuredContent"]["usage"]["requests"] == 1
    rel = mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                      "params": {"name": "get_related", "arguments": {"key": "SAAS-42"}}}, st)["result"]["structuredContent"]
    assert rel["kind"] == "ticket" and [c["id"] for c in rel["conversations"]] == ["claude_code:s0"]
    # errors never kill the loop
    e = mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_conversation", "arguments": {"id": "claude_code:nope"}}}, st)
    assert e["result"]["isError"] and e["result"]["content"][0]["text"] == "not found"
    e = mcp.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "search_conversations", "arguments": {"query": "x", "mode": "psychic"}}}, st)
    assert e["result"]["isError"] and "mode" in e["result"]["content"][0]["text"]
    assert mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "nope"}}, st)["error"]["code"] == -32602
    assert mcp.handle({"jsonrpc": "2.0", "id": 10, "method": "resources/list"}, st)["error"]["code"] == -32601
    # hidden conversations stay out of results
    conn = dbm.connect(db); dbm.set_hidden(conn, "claude_code:s0", True); conn.close()
    r = mcp.handle({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                    "params": {"name": "search_conversations", "arguments": {"query": "kubernetes"}}}, st)["result"]
    assert r["structuredContent"]["count"] == 0


def test_mcp_serve_over_streams(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr("convo_index.server.embed", lambda texts: [None] * len(texts))
    stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                        'not json\n'
                        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                        '{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
    out = io.StringIO()
    assert mcp.serve(stdin, out, st) == 0
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [l.get("id") for l in lines] == [1, None, 2] and lines[1]["error"]["code"] == -32700 and lines[2]["result"] == {}


def test_schedule_plans_per_platform_without_touching_the_system(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    st["paths"]["db"] = str(tmp_path / "data" / "convos.db")
    st.path = tmp_path / "cfg.toml"; st.path.write_text("")
    monkeypatch.setattr(SCH, "command", lambda sub="index": ["/opt/bin/convo-index", "index"])
    mac = SCH.plan(st, system="Darwin", home=tmp_path / "home")
    assert mac["kind"] == "launchd" and mac["hour"] == 21 and mac["minute"] == 0
    (path, plist), = mac["files"].items()
    assert SCH.Path(path).parts[-3:] == ("Library", "LaunchAgents", "com.convo-index.refresh.plist")
    assert "<string>/opt/bin/convo-index</string>" in plist and "<integer>21</integer>" in plist
    assert f"CONVO_INDEX_CONFIG</key><string>{tmp_path / 'cfg.toml'}" in plist and str(tmp_path / "data" / "refresh.log") in plist
    assert mac["commands"][1][:2] == ["launchctl", "bootstrap"]
    _linux(monkeypatch, systemd=True)
    lin = SCH.plan(st, hour=6, minute=30, system="Linux", home=tmp_path / "home")
    assert lin["kind"] == "systemd" and {SCH.Path(p).name for p in lin["files"]} == {"convo-index.service", "convo-index.timer"}
    assert "OnCalendar=*-*-* 06:30:00" in "".join(lin["files"].values()) and "ExecStart=/opt/bin/convo-index index" in "".join(lin["files"].values())
    _linux(monkeypatch, systemd=False)
    cron = SCH.plan(st, system="Linux", home=tmp_path / "home")
    assert cron["kind"] == "cron" and cron["crontab"].startswith("0 21 * * * ") and "refresh.log" in cron["crontab"]
    with pytest.raises(ValueError):
        SCH.plan(st, hour=24)
    # install writes files; commands are stubbed
    ran = []
    monkeypatch.setattr(SCH.subprocess, "run", lambda cmd, **k: ran.append(cmd) or type("R", (), {"returncode": 0, "stderr": ""})())
    out = SCH.install(st, system="Darwin", home=tmp_path / "home")
    assert all(__import__("pathlib").Path(p).exists() for p in out["files"]) and len(ran) == 2
    assert SCH.status(st, system="Darwin", home=tmp_path / "home")["installed"] is True
    rm = SCH.uninstall(st, system="Darwin", home=tmp_path / "home")
    assert rm["removed"] and SCH.status(st, system="Darwin", home=tmp_path / "home")["installed"] is False


def test_cli_search_export_backup(tmp_path, monkeypatch, capsys):
    st, db = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr("convo_index.server.embed", lambda texts: [None] * len(texts))
    assert cli.main(["search", "kubernetes", "--json"]) == 0
    hits = json.loads(capsys.readouterr().out)
    assert [h["id"] for h in hits] == ["claude_code:s0"]
    assert cli.main(["search", "", "--tag", "workspace:globex"]) == 0
    out = capsys.readouterr()
    assert "claude_code:s0" in out.out and "1 result(s)" in out.err
    assert cli.main(["search", "kubernetes", "--since", "1d", "--json"]) == 0 and json.loads(capsys.readouterr().out) == []
    assert cli.main(["search", "kubernetes", "--since", "2026-06-01", "--json"]) == 0 and len(json.loads(capsys.readouterr().out)) == 1
    with pytest.raises(SystemExit):
        cli.main(["search", "x", "--since", "yesterday"])
    assert cli.main(["search", "kubernetes", "--mode", "semantic"]) == 1            # no embedder: says why
    capsys.readouterr()
    assert cli.main(["export", "s0"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["id"] == "claude_code:s0" and d["messages_total"] == 4
    out_md = tmp_path / "x.md"
    assert cli.main(["export", "claude_code:s0", "--md", "-o", str(out_md)]) == 0 and out_md.read_text().startswith("# Kube deploy")
    assert cli.main(["export", "claude_code:nope"]) == 1
    capsys.readouterr()
    dest = tmp_path / "backups" / "copy.db"
    assert cli.main(["backup", str(dest)]) == 1                                       # parent dir missing: refused, no stray file
    assert not (tmp_path / "backups").exists()
    assert cli.main(["backup", str(tmp_path / "backups") + "/"]) == 0                 # trailing slash: create the dir, timestamped file
    assert (tmp_path / "backups").is_dir() and len(list((tmp_path / "backups").iterdir())) == 1
    assert cli.main(["backup", str(dest)]) == 0 and dest.exists()
    assert cli.main(["backup", str(dest)]) == 1                                       # never overwrites
    assert cli.main(["backup", str(tmp_path / "backups")]) == 0                       # existing directory -> timestamped file


def test_cli_resummarize_makes_the_next_run_call_the_summarizer(tmp_path, monkeypatch, capsys):
    from convo_index import jobs
    st, db = _seed(tmp_path, monkeypatch)
    conn = dbm.connect(db)
    conn.execute("INSERT INTO derived_jobs(conversation_id, kind, input_fingerprint, status, attempts, last_run) VALUES('claude_code:s0','summary','f','ok',1,'t')")
    conn.commit(); conn.close()
    assert cli.main(["resummarize"]) == 2
    assert cli.main(["resummarize", "s0"]) == 0 and "1 summary job(s)" in capsys.readouterr().out
    assert cli.main(["resummarize", "claude_code:nope"]) == 1
    # the summary was made from the current revision: without forgetting that too, the next run
    # would adopt it back instead of calling the LLM
    calls = []
    monkeypatch.setattr(jobs.SM, "summarize_conversation",
                        lambda transcript, title, settings: calls.append(title) or ({"problem": "p", "solution": "s", "learning": "l"}, None))
    llm = make_settings(tmp_path, llm={"summary": {"backend": "claude_cli", "model": "m"}}, privacy={"hosted_processing": "allow"})
    conn = dbm.connect(db)
    assert dbm.get_summarized_revision(conn, "claude_code:s0") is None
    assert jobs.run_summary(conn, "claude_code:s0", "Kube deploy", "1", "user: pods crash", llm) == "ok" and calls == ["Kube deploy"]
    assert dbm.get_summarized_revision(conn, "claude_code:s0") == "1"
    conn.close()


def test_schedule_install_print_shows_the_plan(tmp_path, monkeypatch, capsys):
    st, db = _seed(tmp_path, monkeypatch)
    assert cli.main(["schedule", "install", "--print"]) == 0
    out = capsys.readouterr().out
    assert any(k in out for k in ("com.convo-index.refresh", "convo-index.timer", "crontab", "schtasks"))
    assert cli.main(["schedule", "install", "--hour", "25", "--print"]) == 2


def test_mcp_never_returns_hidden_conversations_and_ignores_idless_requests(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    conn = dbm.connect(db); dbm.set_hidden(conn, "claude_code:s0", True); conn.close()
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_conversation", "arguments": {"id": "claude_code:s0"}}}, st)
    assert r["result"]["isError"] and r["result"]["content"][0]["text"] == "not found"
    assert mcp.handle({"jsonrpc": "2.0", "method": "initialized"}, st) is None            # id-less = notification
    assert mcp.handle({"jsonrpc": "2.0", "method": "cancelled", "params": {}}, st) is None
    out = io.StringIO()
    mcp.serve(io.StringIO('{"jsonrpc":"2.0","method":"whatever"}\n'), out, st)
    assert out.getvalue() == ""
    monkeypatch.setattr("convo_index.server.embed", lambda texts: [None] * len(texts))
    r = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "search_conversations", "arguments": {"query": "", "tags": "workspace:globex"}}}, st)
    assert r["result"]["structuredContent"]["count"] == 0                                   # string tag accepted, s0 hidden
    conn = dbm.connect(db); dbm.set_hidden(conn, "claude_code:s0", False); conn.close()
    r = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "search_conversations", "arguments": {"query": "", "tags": "workspace:globex"}}}, st)
    assert [h["id"] for h in r["result"]["structuredContent"]["results"]] == ["claude_code:s0"]


def test_markdown_fences_outlast_backticks_in_tool_output(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    conn = dbm.connect(db)
    conn.execute("UPDATE messages SET text=? WHERE conversation_id='claude_code:s0' AND seq=2", ("out\n```py\nx\n```\n````\n",))
    conn.commit()
    md = X.as_markdown(conn, "claude_code:s0", st, include_tools=True)
    block = md.split("### Tool result")[1]
    assert block.strip().startswith("`````") and block.count("`````") == 2
    assert X._fence("plain") == "```" and X._fence("a ``` b") == "````"


def test_systemd_and_cron_quoting_and_uninstall_reload(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    # the config is at its default location (whether or not it exists): nothing to bake in
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(SCH.S, "CONFIG_PATH", SCH.C.user_dir("config") / "convo-index" / "config.toml")
    monkeypatch.setattr(SCH, "command", lambda sub="index": ["/opt/my tools/convo-index", "index"])
    monkeypatch.setattr(SCH.os, "pathsep", ":")                 # the PATH below is POSIX-shaped, whatever the host
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/usr/local/bin:/bin")
    _linux(monkeypatch, systemd=True)
    lin = SCH.plan(st, system="Linux", home=tmp_path / "home")
    service = [c for p, c in lin["files"].items() if p.endswith(".service")][0]
    assert 'ExecStart="/opt/my tools/convo-index" index' in service                        # systemd: double quotes only
    assert 'Environment="PATH=/usr/local/bin:/usr/bin:/bin"' in service                    # deduplicated
    ran = []
    monkeypatch.setattr(SCH.subprocess, "run", lambda cmd, **k: ran.append(cmd) or type("R", (), {"returncode": 0, "stderr": ""})())
    SCH.install(st, system="Linux", home=tmp_path / "home")
    SCH.uninstall(st, system="Linux", home=tmp_path / "home")
    assert ran[-1] == ["systemctl", "--user", "daemon-reload"] and ran[-2][:3] == ["systemctl", "--user", "disable"]
    _linux(monkeypatch, systemd=False)
    cron = SCH.plan(st, system="Linux", home=tmp_path / "home")["crontab"]
    assert cron.startswith("0 21 * * * PATH=/usr/local/bin:/usr/bin:/bin ") and "'/opt/my tools/convo-index' index" in cron


def test_coverage_counts_only_token_bearing_requests(tmp_path):
    from convo_index import cost as K
    p = K.load(make_settings(tmp_path))
    rows = [("anthropic", "mystery", 1, 1000, 0, 0, 100, 0, None, 0),        # unpriced, has tokens
            ("anthropic", "<synthetic>", 9, 0, 0, 0, 0, 0, None, 0)]          # token-less
    a = K.aggregate(rows, p)
    assert a["requests"] == 10 and a["token_requests"] == 1 and a["priced_requests"] == 0 and a["coverage"] == 0.0
    assert K.aggregate([("anthropic", "<synthetic>", 3, 0, 0, 0, 0, 0, None, 0)], p)["coverage"] is None
    assert p.rates("gpt-4o-2024-08-06")["input"] == 2.5 and p.rates("claude-3-5-sonnet-20241022")["input"] == 3.0


def test_mcp_survives_malformed_requests_and_reports_missing_index(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    out = io.StringIO()
    mcp.serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":5}\n{"jsonrpc":"2.0","id":2,"method":"tools/list","params":[1]}\n'
                          '{"jsonrpc":"2.0","id":3,"method":"initialize","params":{"protocolVersion":"1999-01-01"}}\n'
                          '{"jsonrpc":"2.0","id":4,"method":"ping"}\n'), out, st)
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [l["id"] for l in lines] == [1, 2, 3, 4]
    assert lines[0]["error"]["code"] == -32600 and lines[1]["error"]["code"] == -32600
    assert lines[2]["result"]["protocolVersion"] == mcp.PROTOCOL and lines[2]["result"]["serverInfo"]["version"] not in ("", None)
    assert lines[3]["result"] == {}
    monkeypatch.setattr(mcp, "handle", lambda msg, settings=None: 1 / 0)
    out = io.StringIO(); mcp.serve(io.StringIO('{"jsonrpc":"2.0","id":9,"method":"ping"}\n'), out, st)
    assert json.loads(out.getvalue())["error"]["code"] == -32603                       # internal error, loop alive
    monkeypatch.undo()
    st, db = _seed(tmp_path / "b", monkeypatch)
    monkeypatch.setenv("CONVO_INDEX_DB", str(tmp_path / "nowhere" / "convos.db"))
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_related", "arguments": {"key": "X-1"}}}, st)
    assert r["result"]["isError"] and "no index" in r["result"]["content"][0]["text"] and not (tmp_path / "nowhere").exists()
    # a hybrid query embeds first; that must not open (and so create) the missing database either
    monkeypatch.setattr("convo_index.server.embed", lambda texts: 1 / 0)
    r = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "search_conversations", "arguments": {"query": "pods"}}}, st)
    assert r["result"]["isError"] and "no index" in r["result"]["content"][0]["text"] and not (tmp_path / "nowhere").exists()


def test_mcp_semantic_without_embedder_is_an_error_and_limits_are_clamped(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr("convo_index.server.embed", lambda texts: [None] * len(texts))
    monkeypatch.setattr("convo_index.server.semantic_status", lambda: (False, "no embedder configured"))
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "search_conversations", "arguments": {"query": "kubernetes", "mode": "semantic"}}}, st)["result"]
    assert r["isError"] and "no embedder" in r["content"][0]["text"]
    r = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "search_conversations", "arguments": {"query": "kubernetes"}}}, st)["result"]
    assert r["structuredContent"]["count"] == 1 and "keyword results only" in r["structuredContent"]["note"]
    g = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "get_conversation", "arguments": {"id": "claude_code:s0", "max_messages": -1, "max_chars": 1, "include_tools": True}}}, st)["result"]
    assert len(g["structuredContent"]["messages"]) == 1 and "more chars" not in g["structuredContent"]["messages"][0]["text"]   # clamped to 1 / 200


def test_schedule_passes_env_through_and_refuses_a_missing_config(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    monkeypatch.setattr(SCH, "command", lambda sub="index": ["/opt/bin/convo-index", "index"])
    monkeypatch.setenv("CODEX_HOME", "~/codex home")
    monkeypatch.setenv("CONVO_INDEX_DB", str(tmp_path / "else" / "convos.db"))
    monkeypatch.setenv("PATH", "/usr/bin:/opt/100%/bin")
    st.path = tmp_path / "cfg.toml"; st.path.write_text("")
    mac = SCH.plan(st, system="Darwin", home=tmp_path / "home")
    plist = next(iter(mac["files"].values()))
    assert "<key>CODEX_HOME</key>" in plist and str(Path("~/codex home").expanduser().resolve()) in plist
    assert "<key>CONVO_INDEX_DB</key>" in plist and str(tmp_path / "else" / "refresh.log") in plist   # log next to the effective db
    _linux(monkeypatch, systemd=True)
    service = [c for p, c in SCH.plan(st, system="Linux", home=tmp_path / "home")["files"].items() if p.endswith(".service")][0]
    assert 'Environment="PATH=/usr/bin:/opt/100%%/bin"' in service                  # systemd: %% is a literal percent sign
    _linux(monkeypatch, systemd=False)
    cron = SCH.plan(st, system="Linux", home=tmp_path / "home")["crontab"]
    assert "100\\%" in cron and "CODEX_HOME=" in cron
    st.path = tmp_path / "missing.toml"
    with pytest.raises(ValueError):
        SCH.plan(st, system="Darwin", home=tmp_path / "home")


def test_markdown_export_escapes_heading_lines_and_single_lines_the_title(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    conn = dbm.connect(db)
    conn.execute("UPDATE conversations SET title=? WHERE id='claude_code:s0'", ("multi\nline title",))
    conn.execute("UPDATE messages SET text=? WHERE conversation_id='claude_code:s0' AND seq=3", ("### Assistant · spoofed\nreal text",))
    conn.commit()
    md = X.as_markdown(conn, "claude_code:s0", st)
    assert md.startswith("# multi line title\n") and "\\### Assistant · spoofed" in md and md.count("\n### Assistant") == 1


def test_export_refuses_to_overwrite_without_force_and_since_is_a_usage_error(tmp_path, monkeypatch, capsys):
    st, db = _seed(tmp_path, monkeypatch)
    out = tmp_path / "x.json"; out.write_text("keep")
    assert cli.main(["export", "s0", "-o", str(out)]) == 1 and out.read_text() == "keep"
    assert cli.main(["export", "s0", "-o", str(out), "--force"]) == 0 and out.read_text() != "keep"
    with pytest.raises(SystemExit) as e:
        cli.main(["search", "x", "--since", "yesterday"])
    assert e.value.code == 2


def test_markdown_export_keeps_code_blocks_as_they_are(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    conn = dbm.connect(db)
    conn.execute("UPDATE messages SET text=? WHERE conversation_id='claude_code:s0' AND seq=3",
                 ("look:\n```python\n# a comment\nprint(1)\n```\n# not a heading\n    # indented code\n"
                  "~~~~markdown\n~~~\n# still code\n~~~~",))
    conn.commit()
    md = X.as_markdown(conn, "claude_code:s0", st)
    assert "\n# a comment\n" in md and "\n\\# not a heading\n" in md and "\n    # indented code" in md
    assert "\n# still code\n" in md                                      # a shorter fence does not close a longer one


def test_service_plans_the_ui_server_from_login(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(SCH, "command", lambda sub="index": ["/opt/convo-index", sub])
    mac = SCH.plan(st, system="Darwin", home=tmp_path / "home", job="serve")
    plist = next(iter(mac["files"].values()))
    assert "com.convo-index.serve" in plist and "<string>serve</string>" in plist
    assert "<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>" in plist   # restart only after a crash
    assert "StartCalendarInterval" not in plist and mac["log"].endswith("serve.log") and mac["hour"] is None
    _linux(monkeypatch, systemd=True)
    lin = SCH.plan(st, system="Linux", home=tmp_path / "home", job="serve")
    assert list(lin["files"]) == [str(tmp_path / "home" / ".config" / "systemd" / "user" / "convo-index-serve.service")]
    unit = next(iter(lin["files"].values()))
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit and lin["commands"][-1][-1] == "convo-index-serve.service"
    assert SCH.uninstall(st, system="Linux", home=tmp_path / "home", dry_run=True, job="serve")["commands"][0][-1] == "convo-index-serve.service"
    _linux(monkeypatch, systemd=False)
    cron = SCH.plan(st, system="Linux", home=tmp_path / "home", job="serve")["crontab"]
    assert cron.startswith("@reboot ") and "/opt/convo-index serve" in cron
    assert SCH.status(st, system="Darwin", home=tmp_path / "home", job="serve")["installed"] is False
    # the refresh job is untouched by the second job
    assert SCH.plan(st, system="Darwin", home=tmp_path / "home")["hour"] == 21


def test_windows_jobs_are_task_scheduler_tasks(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    monkeypatch.setattr(SCH, "command", lambda sub="index": ["C:\\Tools&A\\convo index\\convo-index.exe", sub])
    monkeypatch.setenv("PATH", "C:\\Tools;C:\\100%\\bin")
    ref = SCH.plan(st, system="Windows", home=tmp_path / "home")
    data = Path(ref["log"]).parent
    assert ref["kind"] == "schtasks" and sorted(ref["files"]) == [str(data / "convo-index.cmd"), str(data / "convo-index.xml")]
    launcher, xml = ref["files"][str(data / "convo-index.cmd")], ref["files"][str(data / "convo-index.xml")]
    assert '"C:\\Tools&A\\convo index\\convo-index.exe" "index" >> "' in launcher      # quoted: & stays literal
    assert 'set "PATH=C:\\Tools;C:\\100%%\\bin"' in launcher                             # a batch file expands %x%: doubled
    assert "CONVO_INDEX_CONFIG" not in launcher                                      # default config location: nothing to bake in
    assert xml.startswith('<?xml version="1.0" encoding="UTF-16"?>')                 # what schtasks /XML reads
    assert "<Arguments>/c convo-index.cmd</Arguments>" in xml and f"<WorkingDirectory>{data}</WorkingDirectory>" in xml
    assert launcher.startswith("@echo off\nchcp 65001 >nul\n")                        # UTF-8 paths under any code page
    assert "<CalendarTrigger>" in xml and "T21:00:00" in xml and "<ExecutionTimeLimit>PT12H" in xml
    assert ref["commands"] == [["schtasks", "/Create", "/F", "/TN", "convo-index refresh", "/XML", str(data / "convo-index.xml")]]
    srv = SCH.plan(st, system="Windows", home=tmp_path / "home", job="serve")
    sxml = srv["files"][str(data / "convo-index-serve.xml")]
    assert "<LogonTrigger>" in sxml and "<ExecutionTimeLimit>PT0S" in sxml and "<RestartOnFailure>" in sxml   # a server, not a 72-hour task
    assert srv["commands"][1] == ["schtasks", "/Run", "/TN", "convo-index serve"]
    assert SCH.uninstall(st, system="Windows", home=tmp_path / "home", dry_run=True, job="serve")["commands"] == \
        [["schtasks", "/End", "/TN", "convo-index serve"], ["schtasks", "/Delete", "/F", "/TN", "convo-index serve"]]
    monkeypatch.setenv("CONVO_INDEX_DB", str(tmp_path / "else" / "convos.db"))      # shell overrides reach the task
    launcher = [c for p, c in SCH.plan(st, system="Windows", home=tmp_path / "home")["files"].items() if p.endswith(".cmd")][0]
    assert f'set "CONVO_INDEX_DB={tmp_path / "else" / "convos.db"}"' in launcher


@pytest.mark.skipif(os.name != "nt", reason="needs Task Scheduler")
def test_windows_tasks_register_run_and_unregister(tmp_path, monkeypatch):
    """The real thing: register a task from the generated XML, run it, see the launcher run."""
    import subprocess
    import sys
    import time
    st = make_settings(tmp_path)
    st["paths"]["db"] = str(tmp_path / "Jos\u00e9 data" / "convos.db")   # no data directory yet; a non-ASCII path
    monkeypatch.delenv("CONVO_INDEX_DB", raising=False)
    marker = tmp_path / "ran.txt"
    monkeypatch.setattr(SCH, "command", lambda sub="index": [
        sys.executable, "-c", "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])", str(marker), sub])
    monkeypatch.setitem(SCH.JOBS, "refresh", {**SCH.JOBS["refresh"], "task": "convo-index test refresh", "unit": "convo-index-test"})
    out = SCH.install(st, system="Windows")
    assert [r["ok"] for r in out["results"]] == [True], out["results"]
    assert SCH.status(st, system="Windows")["installed"] is True
    subprocess.run(["schtasks", "/Run", "/TN", "convo-index test refresh"], check=True, capture_output=True)
    for _ in range(120):
        if marker.exists():
            break
        time.sleep(0.5)
    assert marker.read_text() == "index"
    un = SCH.uninstall(st, system="Windows")
    assert all(r["ok"] for r in un["results"]), un["results"]
    assert SCH.status(st, system="Windows")["installed"] is False and not (tmp_path / "Jos\u00e9 data" / "convo-index-test.xml").exists()


def test_mcp_streams_are_utf8_whatever_the_console_code_page(tmp_path, monkeypatch):
    st, db = _seed(tmp_path, monkeypatch)
    conn = dbm.connect(db)
    conn.execute("UPDATE conversations SET title='\u65e5\u672c\u8a9e' WHERE id='claude_code:s0'"); conn.commit(); conn.close()
    req = ('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_conversation","arguments":{"id":"claude_code:s0"}}}\n'
           '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_conversations","arguments":{"query":"\u00e9","mode":"keyword"}}}\n')
    stdin = io.TextIOWrapper(io.BytesIO(req.encode("utf-8")), encoding="cp1252")        # what Windows hands a console app
    raw = io.BytesIO(); stdout = io.TextIOWrapper(raw, encoding="cp1252")
    assert mcp.serve(stdin, stdout, st) == 0
    lines = [json.loads(l) for l in raw.getvalue().decode("utf-8").splitlines()]
    assert "\u65e5\u672c\u8a9e" in lines[0]["result"]["content"][0]["text"] and "error" not in lines[1]


def test_linux_without_a_user_session_falls_back_and_says_why(tmp_path, monkeypatch, capsys):
    """systemctl on PATH but no user manager (a container): cron when crontab exists, else
    nothing to install into, with the command to run by hand."""
    st = make_settings(tmp_path)
    monkeypatch.setattr(SCH, "command", lambda sub="index": ["/opt/convo-index", sub])
    ran = []
    monkeypatch.setattr(SCH.subprocess, "run", lambda cmd, **k: ran.append(cmd) or type("R", (), {"returncode": 1, "stderr": ""})())
    which = {"systemctl": "/usr/bin/systemctl", "crontab": "/usr/bin/crontab"}
    monkeypatch.setattr(SCH.shutil, "which", lambda name: which.get(name))
    cron = SCH.plan(st, system="Linux", home=tmp_path / "home")
    assert ran == [["systemctl", "--user", "show-environment"]]                        # asked the user manager
    assert cron["kind"] == "cron" and "no systemd user session" in cron["reason"] and not cron["files"]
    del which["crontab"]
    none = SCH.plan(st, system="Linux", home=tmp_path / "home", job="serve")
    assert none["kind"] == "none" and none["reason"].endswith(", and there is no crontab") and not none["commands"]
    assert none["manual"].startswith("nohup /opt/convo-index serve >> ") and none["manual"].endswith(" 2>&1 &")
    out = SCH.install(st, system="Linux", home=tmp_path / "home", job="serve")
    assert out["results"] == [] and not (tmp_path / "home" / ".config").exists()          # nothing written
    monkeypatch.setattr(SCH, "_kind_and_reason", lambda system=None: ("none", "no scheduler here"))
    monkeypatch.setattr(S, "load", lambda *a, **k: st)
    assert cli.main(["service", "install"]) == 1
    err = capsys.readouterr().err
    assert "no scheduler here" in err and "nohup /opt/convo-index serve" in err
    monkeypatch.setattr(SCH.subprocess, "run", lambda cmd, **k: (_ for _ in ()).throw(FileNotFoundError(cmd[0])))
    assert SCH.systemd_user_available() is False                                         # a broken binary is no systemd
    line = SCH.manual_line(["/opt/convo-index", "serve"], "/d/serve.log", "/my cfg.toml", background=True)
    assert line == "CONVO_INDEX_CONFIG='/my cfg.toml' nohup /opt/convo-index serve >> /d/serve.log 2>&1 &"   # env before nohup
    assert SCH.manual_line(["/opt/convo-index", "index"], None, "/c.toml") == "CONVO_INDEX_CONFIG=/c.toml /opt/convo-index index"
    assert SCH.plan(st, system="Linux", home=tmp_path / "home")["manual"] == "/opt/convo-index index"   # refresh: in front, no log


def test_existing_systemd_units_are_found_without_a_user_session(tmp_path, monkeypatch):
    """Units installed earlier stay visible to status and uninstall over an SSH login with no
    user manager: uninstall removes them and reports the systemctl failure, not success."""
    st = make_settings(tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(SCH, "command", lambda sub="index": ["/opt/convo-index", sub])
    home = tmp_path / "home"
    _linux(monkeypatch, systemd=True)
    for path, content in SCH.plan(st, system="Linux", home=home)["files"].items():
        Path(path).parent.mkdir(parents=True, exist_ok=True); Path(path).write_text(content)
    monkeypatch.setattr(SCH, "systemd_user_available", lambda: False)                    # the user bus is gone
    assert SCH.plan(st, system="Linux", home=home)["kind"] == "cron"                      # a new install would not use it
    stat = SCH.status(st, system="Linux", home=home)
    assert stat["kind"] == "systemd" and stat["installed"] is True
    monkeypatch.setattr(SCH.subprocess, "run", lambda cmd, **k: type("R", (), {"returncode": 1, "stderr": "no bus"})())
    out = SCH.uninstall(st, system="Linux", home=home)
    assert out["kind"] == "systemd" and not all(r["ok"] for r in out["results"])          # the failure is reported
    assert sorted(Path(p).name for p in out["removed"]) == ["convo-index.service", "convo-index.timer"]
