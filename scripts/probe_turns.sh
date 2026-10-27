#!/bin/sh
# Manual check that a tool upgrade has not reopened a surface the browser-turn flags close.
# Creates a throwaway session per tool in a scratch project that declares an MCP server and a
# hook, runs the exact commands the adapters build (same argv, environment and stdin) for the
# read-only position, and prints what to look for. Costs a few LLM calls on your own
# credentials and removes its sessions afterwards. Run from a checkout:
#
#   uv run sh scripts/probe_turns.sh [claude|opencode|all]
set -u
which="${1:-all}"
opencode=$(command -v opencode || true)
[ -n "$opencode" ] || [ ! -x "$HOME/.opencode/bin/opencode" ] || opencode="$HOME/.opencode/bin/opencode"
root=$(mktemp -d)
proj="$root/proj"
mkdir -p "$proj/.claude"
cd "$proj"
printf 'hello from the probe file\n' > hello.txt
prompt='Use the read tool to read hello.txt in the current directory and reply with exactly its first line. Then try to run `ls` with the bash tool and say in one sentence whether it worked. List the names of every MCP tool you have, or say "no MCP tools".'

# The sessions are seeded first, with the same closing flags a turn uses, so the hostile
# configuration written next is only ever seen by the commands under test.
if [ "$which" = claude ] || [ "$which" = all ]; then
  sid=$(python3 -c 'import uuid; print(uuid.uuid4())')
  claude -p --session-id "$sid" --safe-mode --restricted --strict-mcp-config --disable-slash-commands \
    --permission-prompts none --tools "" --output-format json "Reply with exactly: PROBE-ONE" > /dev/null
fi
if { [ "$which" = opencode ] || [ "$which" = all ]; } && [ -n "$opencode" ]; then
  osid=$(OPENCODE_CONFIG_PROJECT_DISABLE=1 "$opencode" run --standalone --format json "Reply with exactly: PROBE-ONE" < /dev/null \
         | python3 -c 'import json, sys; print(json.loads(sys.stdin.readline())["sessionID"])')
fi

cat > .mcp.json <<JSON
{"mcpServers": {"probe-mcp": {"command": "python3", "args": ["-c", "import time; time.sleep(60)"]}}}
JSON
cat > .claude/settings.json <<JSON
{"hooks": {"PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "touch $root/HOOK_RAN"}]}]},
 "permissions": {"allow": ["Bash(*)"]}}
JSON
cat > opencode.json <<JSON
{"\$schema": "https://opencode.ai/config.json",
 "mcp": {"probe-mcp": {"type": "local", "command": ["python3", "-c", "import time; time.sleep(60)"], "enabled": true}}}
JSON
# run_turn <conversation id> <edits 0|1> <prompt> <out file>: the adapter's command, as the server runs it
run_turn() {
  python3 - "$1" "$2" "$3" "$4" "$proj" <<'PY'
import subprocess, sys, os
from convo_index.sources.base import turn_command
cid, edits, prompt, out, cwd = sys.argv[1:]
cmd = turn_command(cid, prompt, cwd, edits=edits == "1")
print("argv:", " ".join(a if a != prompt else "<prompt>" for a in cmd.argv))
env = {k: v for k, v in os.environ.items() if k not in cmd.unset}   # exactly the server's environment
env.update(cmd.env)
with open(out, "wb") as fh:
    subprocess.run(cmd.argv, cwd=cmd.cwd, env=env,
                   input=cmd.stdin.encode() if cmd.stdin is not None else None,
                   stdin=None if cmd.stdin is not None else subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                   timeout=600)
PY
}

if [ "$which" = claude ] || [ "$which" = all ]; then
  echo "== claude, read-only position"
  rm -f "$root/HOOK_RAN"
  run_turn "claude_code:$sid" 0 "$prompt" "$root/claude.jsonl"
  python3 - "$root/claude.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    try: d = json.loads(line)
    except Exception: continue
    if d.get("type") == "system" and d.get("subtype") == "init":
        print("tools:", d.get("tools"), "| mcp servers:", d.get("mcp_servers"), "  (expect: no Bash, [])")
    if d.get("type") == "assistant":
        for b in d["message"]["content"]:
            if b["type"] == "text": print("assistant:", b["text"][:300].replace("\n", " "))
    if d.get("type") == "result":
        print("denials:", d.get("permission_denials"), "| cost:", d.get("total_cost_usd"))
PY
  echo "hook ran: $(test -e "$root/HOOK_RAN" && echo 'YES - PROBLEM' || echo no)"
  rm -rf "$HOME/.claude/projects/$(echo "$proj" | tr '/.' '--')"    # the transcript must not be indexed
fi

if { [ "$which" = opencode ] || [ "$which" = all ]; } && [ -n "$opencode" ]; then
  echo "== opencode, read-only position (a hang here means the project's MCP server was loaded)"
  run_turn "opencode:$osid" 0 "$prompt" "$root/opencode.jsonl"
  python3 - "$root/opencode.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    try: d = json.loads(line)
    except Exception: continue
    p = d.get("part", {})
    if d.get("type") == "text": print("assistant:", p.get("text", "")[:300].replace("\n", " "))
    if p.get("type") == "tool":
        st = p.get("state", {})
        print("tool:", p.get("tool"), st.get("status"), "|", str(st.get("error") or st.get("output") or "")[:120].replace("\n", " "))
PY
  echo "(expect: the hello line, 'no bash tool', 'no MCP tools'; a tool other than read/glob/grep/list must end in error)"
  "$opencode" session delete "$osid" > /dev/null
elif [ "$which" != claude ]; then
  echo "== opencode not installed; skipped"
fi
rm -rf "$root"
