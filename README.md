# Conversation Index

[![CI](https://github.com/AmitPhulera/convo-index/actions/workflows/ci.yml/badge.svg)](https://github.com/AmitPhulera/convo-index/actions/workflows/ci.yml)

Local index + browser for AI coding-assistant conversations: Claude Code, Opencode, Codex
CLI, Antigravity CLI and the pi coding agent. Full-text + semantic search, summaries and user-defined tags through
whichever LLM CLI you already have, related work by ticket/PR/branch, token usage with
API-equivalent cost estimates, and an MCP server so your agents can search the archive.
Design: `docs/design.md`.

## Install

    curl -fsSL https://raw.githubusercontent.com/AmitPhulera/convo-index/main/install.sh | sh

That installs [uv](https://docs.astral.sh/uv/) if needed and the `convo-index` command as a uv
tool, then runs `convo-index up`: it writes the config, keeps the UI server running from login
(LaunchAgent on macOS, systemd user service on Linux), schedules the daily refresh, starts the
index build in the background and opens http://127.0.0.1:9753. Re-run the installer to upgrade; set
`CONVO_INDEX_NO_UP=1` to install the command only. The database lives in
`~/.local/share/convo-index/`. Nothing goes to a hosted model until you allow it: run
`convo-index init` on a terminal to decide (the installer cannot ask). `up` ends with a numbered
"Needs your attention" list of anything left for you to do.

In a container or anywhere else without a systemd user session (systemctl present but no
`$XDG_RUNTIME_DIR`), `up` starts the server for the current session only and tells you how to
start it again and how to refresh the index by hand, or gives the crontab lines if `crontab` exists.

On Windows (PowerShell):

    powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/AmitPhulera/convo-index/main/install.ps1 | iex"

The jobs become Task Scheduler tasks (a launcher `.cmd` and the task XML sit next to the
database), the data lives in `%LOCALAPPDATA%\convo-index`, the config in
`%APPDATA%\convo-index\config.toml`, and the UI's resume commands are for PowerShell.

### From a checkout

    uv sync                  # creates .venv, installs the package, pytest and in-process embeddings

That is the whole install; prefix the commands below with `uv run`. Semantic search uses [fastembed](https://github.com/qdrant/fastembed)
in-process: the first `index` fetches the quantised `nomic-embed-text-v1.5` model (~130 MB) from
Hugging Face into `models/` next to the database, and nothing leaves the machine after that.

## Use
    convo-index up      # first run: init + UI server from login + daily refresh + index build
    convo-index init    # detect sources, LLM CLIs and the embedder; write ~/.config/convo-index/config.toml
    convo-index doctor  # what will be indexed, which backends will be called, what is failing
    convo-index index   # build/refresh convos.db (incremental)
    convo-index serve   # browse at http://127.0.0.1:9753
    convo-index stop    # stop the running UI server (running browser turns end first)
    convo-index search "kubernetes crash" --source codex --since 30d   # from the terminal
    convo-index export <id> --md          # one conversation as Markdown (or JSON without --md)
    convo-index handoff <id> [--review]   # a prompt to continue (or review) it in another harness
    convo-index backup ~/backups/         # WAL-safe copy of the database
    uv run pytest       # tests (from a checkout)

The browser UI is a two-pane reader: results on the left, the conversation on the right.
`/` focuses the search box, `↑`/`↓` (or `j`/`k`) walk the results, `Enter` opens one and
`Esc` closes it. The theme follows the operating system and the sun/moon button overrides it.

## Configuration
`~/.config/convo-index/config.toml` (or `$CONVO_INDEX_CONFIG`). Every key is optional.
`init` probes the installed coding CLIs in the order **gemini → codex → claude** and uses the
first one found for classification and summaries; an explicit `[llm]` entry always wins.

    [llm]
    classify = { backend = "gemini_cli", model = "gemini-3.8-flash-low" }
    summary  = { backend = "claude_cli", model = "claude-haiku-4-5" }
    # backends: gemini_cli (agy), codex_cli, claude_cli, anthropic_api, openai_compat

    [embeddings]              # default: in-process fastembed
    backend = "fastembed"
    model = "nomic-ai/nomic-embed-text-v1.5-Q"   # the full-precision "…-v1.5" (520 MB) also works
    # Or any OpenAI-compatible /v1/embeddings server, e.g. Ollama after `ollama pull nomic-embed-text`:
    # backend = "openai_compat"
    # base_url = "http://localhost:11434/v1"
    # model = "nomic-embed-text"
    # Switching backend or model needs `convo-index reembed`; `init` picks whichever is present.

    [privacy]
    hosted_processing = "allow"   # allow | deny | ask. deny AND the unresolved default "ask" refuse
                                  # every non-localhost backend (LLMs and embeddings); `init` resolves it

    [category]                # conversation categories; hints decide from the cwd without an LLM
    labels = ["work", "personal"]
    hints = { work = ["acme", "client-x"], personal = ["hobby"] }

    [sources.claude_code]
    exclude_cwd = ["~/secret-client"]     # that directory and everything below it: never indexed,
                                          # never sent anywhere (globs such as "*/acme-*" work too)

    [ui.turns]
    enabled = true                        # continue a conversation from the browser (see below)
    timeout_seconds = 1200                # a turn is killed after this long
    max_concurrent = 2                    # harness processes the UI server runs at once

    [server]
    host = "127.0.0.1"                    # the UI answers only to loopback Host names
    allowed_hosts = []                    # extra Host names, e.g. ["mybox.lan"] when binding 0.0.0.0
                                          # or behind a proxy; a bare name matches any port, "mybox.lan:8080"
                                          # only that one (localhost / 127.0.0.1 always match)

Summaries are derived jobs: a failed provider call is retried on later runs (up to
`[summary].max_attempts`) without the conversation changing. Changing the embedding model
or server requires `convo-index reembed`; vectors are skipped until then so spaces never mix.

`CONVO_INDEX_DB` and `CONVO_INDEX_ARCHIVE` override where the database and archive
copies live. Defaults: the repo root when running from a checkout, otherwise
`~/.local/share/convo-index/` (respects `XDG_DATA_HOME`).

`convo-index index` exits 0 on success, 1 if any source or conversation failed (details
on stderr), and 75 if another indexer already holds the lock. Only one indexer runs at a
time across the CLI, the scheduler and the UI's refresh button.

## Sources
| Tool | Where it is read from | Override | Resume from the UI |
|---|---|---|---|
| **Claude Code** | `~/.claude/projects/*/*.jsonl` (text, thinking, tool_use and tool_result blocks) | — | `claude --resume <id>` |
| **Opencode** | the SQLite DB reported by `opencode db path` (1.x `session`/`message`/`part` and 2.x `session_v2`/`session_message` layouts) | `OPENCODE_DB` | `opencode --session <id>` |
| **Codex CLI** | `~/.codex/state_*.sqlite` threads + `sessions/**/rollout-*.jsonl` | `CODEX_HOME` | `codex resume <id>` |
| **Antigravity CLI** | `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl` (+ `conversation_summaries.db` for titles/workspaces) | `ANTIGRAVITY_HOME` | `agy --conversation <id>` |
| **pi coding agent** | `~/.pi/agent/sessions/*/*.jsonl` (the live branch of each session tree; text, thinking, tool calls and results) | `PI_CODING_AGENT_DIR`, `PI_CODING_AGENT_SESSION_DIR` | `pi --session <id>` |

Tool calls, tool results and thinking are indexed as their own messages (keyword-searchable,
collapsed in the UI, never embedded or summarized). Antigravity records no model or token data;
Codex records the repository URL, which lets bare "PR 123" mentions link. Disable a source with
`[sources.<name>] enabled = false`.

Conversation ids are namespaced by source, e.g. `claude_code:<session-uuid>`,
`opencode:<session-id>`, `pi:<session-uuid>`. Databases created before 0.2 are migrated in place on first run,
after an automatic backup to `convos.db.bak-<timestamp>`.

## Continue a conversation from the browser

The UI server can run one more turn of an indexed conversation in the tool that owns it
(`claude -p --resume`, `opencode run --session`, `pi -p --session <file>`, `codex exec resume`). The tool appends the turn to its own
transcript, so `claude --resume <id>`, `opencode --session <id>`, `pi --session <id>` or `codex resume <id>` in a terminal afterwards
sees exactly what happened in the browser, and the index is refreshed before the turn is
reported finished (ingest only: its summary and tags come with the next full refresh). Claude Code, Opencode, pi and Codex are supported today; Antigravity will follow.

In the UI, such a conversation has a message box under its transcript. Type, press Send (or
Cmd/Ctrl+Enter), and the tool's replies, tool calls and results stream in as it works; the Mode
picker chooses Read-only or Edit (below), and Stop ends the turn. When the turn
finishes the transcript reloads with it; tools that were denied stay named in the status line.
The same endpoints serve any other client:

    POST   /api/conversation/<id>/turn   {"prompt": "...", "edits": false}   -> {"turn_id": ...}
    GET    /api/turn/<turn_id>?after=<n>  server-sent events: text, tool, tool_result, denied, done | error
    DELETE /api/turn/<turn_id>            stop it
    GET    /api/turns/tools               the MCP servers Edit mode can use and the allowlist
    POST   /api/turns/tools               {"mcp_tools": ["mcp__<server>__<tool>" | "mcp__<server>__*", ...]}

Every turn runs with the tool's customisation surface closed: no hooks, plugins, project or
user settings, or slash commands (Claude Code's `--restricted --disable-slash-commands`, with
`--safe-mode` in Read-only and `--setting-sources ""` in Edit; for Opencode a fresh, empty
configuration home per turn, the project configuration switched off, its configuration-path
variables unset and a deny-all permission ruleset; for pi `--no-extensions --no-mcp --no-skills
--no-prompt-templates --no-context-files --no-approve`, so no extension, MCP server, skill,
AGENTS.md/CLAUDE.md or project-local file loads; for Codex `--ignore-user-config --ignore-rules`
with ChatGPT apps, plugins, hooks, skills, multi-agent tools, web search and the project's
AGENTS.md switched off (your own `~/.codex/AGENTS.md` still applies), and `--strict-config`, so a
Codex upgrade that renames one of those switches fails the turn instead of reopening it;
administrator-managed settings of Claude Code, Opencode and Codex stay in force). Anything the tool would normally ask about is denied and reported as a
`denied` event, so the step can be finished in a terminal.

- **Read-only** (the default): `Read`/`Glob`/`Grep` for Claude Code, the `plan` agent for
  Opencode, `read`/`grep`/`find`/`ls` for pi. No shell and no MCP servers. Codex keeps its shell
  but runs it in its `read-only` sandbox: commands can read, not write, and have no network.
- **Edit** (`"edits": true`): adds the file-editing tools, confined to the conversation's
  directory for Claude Code (by `--restricted`) and Opencode (by its `external_directory`
  permission staying denied) but not for pi (below), **the shell**, and for Claude Code **the MCP tools on your allowlist**.
  None of these ask before running: a shell command from the browser can do whatever your
  user account can, and an allowlisted MCP tool can act on the service behind it (send,
  post, create). The allowlist is edited with the Tools button in the message box and kept
  in `turn-tools.json` next to the database (readable by you only; a file others can write
  is ignored); `convo-index doctor` prints it. Only servers from your user-scope Claude Code
  config (`claude mcp add --scope user`) can be listed: the turn loads just those servers,
  from a per-turn config file that is removed afterwards. claude.ai account connectors and
  a project's `.mcp.json` are never loaded. Opencode, pi and Codex turns get the shell but no MCP tools.
  Codex runs it in its `workspace-write` sandbox: writes are limited to the conversation's
  directory and the temporary directories, and there is no network.

pi has no sandbox and no permission prompts: the tool list is its whole policy, and its tools
(the read tools in Read-only too) reach any file your account can, not just the conversation's
directory.

Turns are accepted only on a loopback listener, from a loopback peer, when the browser
addresses the server by a loopback name, with no proxy header present, and with the server's
turn token: a secret kept in `turn-token` next to the database (readable by you only; a
restarted server keeps it, a file others can read gets a new one) and carried in the fragment of
the URL that `convo-index serve` prints on a terminal and `up` prints and opens. The page moves
it out of the address bar into the tab's session storage, so a reload keeps it; the login
service's log never holds it. A page reached any other way, a reverse proxy on this machine, or a
client that only has what the HTTP API serves cannot run turns; everything else keeps
working, and with `[server] host = "0.0.0.0"` the endpoint answers 403. A
conversation whose transcript changed in the last 30 seconds is refused as well (it is
probably open in a terminal), as is one whose directory is gone or excluded by
`exclude_cwd`. Turns belong to the server process: closing the page does not stop one,
reopening the conversation resumes its stream, and stopping the server stops every turn. `convo-index doctor` lists which tools are on
PATH for this. A Codex turn resumes with the model and provider the thread recorded (with your
config.toml ignored, Codex would otherwise use its defaults), at Codex's default reasoning effort;
a thread on a custom provider defined in config.toml cannot be continued from the browser. On
Windows, Claude Code and pi turns run the tool through `node` rather than the npm
`.cmd` shim, inside a job object that ends the whole process tree; Opencode and Codex turns are not
offered there yet. `scripts/probe_turns.sh` is the manual
check that the closing flags still hold after a tool upgrade.

## Hand a conversation to another tool

In the browser, **Hand off** in a conversation's toolbar does all of this without a terminal:
pick Continue elsewhere or Second opinion, the tool and (to continue) Read-only or Edit, then
**Prepare prompt**. The prompt opens to edit, with the known problems, anything that looks
like a credential and its size beside it; **Start** runs the new session and shows its
replies. When it is done the new conversation opens from the panel, both conversations link
to each other in their details, and **Bring back** puts a second opinion in this
conversation's message box for the first agent. It needs the turn token in the URL, like the
message box, and the same `hosted_processing = allow` as `--start`.

`convo-index handoff <id>` prints a prompt that picks the work up in a new session of any
other tool: paste it there. It is built for two cases. **Continue** (the default) is for
when one tool hits its limits: the prompt carries a handoff note written by the `summary`
backend (goal, decisions, where it stands, the next step, and the claims the agent made that
the transcript does not show it checking). **`--review`** asks for a second opinion: the
prompt carries the conversation's own words and no note, so the reviewer is not steered by
the first agent's account. Both include where and when it happened, the repository's state
now (`git status`, diff stat, recent commits), problems found without an LLM (tool errors,
interruptions, compactions, user messages that look like corrections), every user message
verbatim, and the path of a full transcript the new session can read on demand (under
`handoffs/` next to the database, readable only by you, with credential-looking strings
masked). Older assistant text is left out
first when the prompt would pass ~100k tokens; user messages never are.

Only the prompt goes to stdout. Its size, the known problems, anything in the prompt that
looks like a credential (remove it before pasting into a tool from another provider) and how
many were masked in the transcript go to stderr. A
note that cannot be written (no backend, `hosted_processing` not `allow`, a timeout) leaves
the prompt without one and says why; `--no-digest` skips it.

`--start claude_code` or `--start opencode` runs the new session here instead of printing the
prompt, with the same closed surface as a browser turn (read-only unless `--edit`, which a
review ignores), and prints its replies, the new conversation's id and the command that
resumes it in that tool. The two conversations are linked in the index as soon as the tool
reports its session. It sends the conversation to that tool's provider, so it needs
`[privacy].hosted_processing = allow`, like the handoff note; with `deny` or `ask` it is
refused and the prompt can still be printed and pasted by hand. The new session may read the transcript copy's directory and nothing
else outside the project. It is not started when the prompt holds what looks like a
credential: print the prompt, remove it, and pass the result with `--prompt-file`, or pass
`--ignore-secrets` to send the prompt as it is (the transcript copy stays masked). The model comes from `--model`, else `[handoff.models]`:

    [handoff.models]
    claude_code = ""                                  # "" = Claude Code's own default
    opencode = "opencode-go/deepseek-v4.1-flash"      # "" = the first of opencode's recent models

opencode needs one: in the turn's empty configuration home it falls back to a default model
that a workspace's privacy settings may refuse.

## Daily refresh and the server from login
    convo-index schedule install            # LaunchAgent (macOS) or systemd user timer (Linux); crontab line elsewhere
                                            # (no systemd user session and no crontab: prints the command to run yourself)
    convo-index schedule install --print    # show what would be written, change nothing
    convo-index schedule status
    convo-index schedule uninstall
    convo-index service install             # keep `convo-index serve` running from login (same mechanisms; @reboot line for cron)
    convo-index service status
    convo-index service uninstall

`convo-index stop`, or the power button in the UI's header, stops the running server without
uninstalling the service: running browser turns end, the server exits cleanly, and the service
(which restarts it only after a crash) starts it again at the next login; `convo-index serve`
or `up` start it sooner. Both go through `POST /api/shutdown`, which needs the turn token, a
request from this machine and no proxy in between; the button is disabled in a tab that was
not opened through the URL `serve` or `up` prints. `service uninstall` keeps it off for good.

Time comes from `[schedule] hour/minute` in the config (default 21:00 local); the log is
`refresh.log` next to the database. The job runs `convo-index index`, which is incremental and
exits 75 when another indexer holds the lock.

## Tokens and cost
Every source that reports token usage is stored per request (`usage_records`, disjoint
buckets: uncached input, cache read, cache write (5-minute), cache write (1-hour, which
Anthropic bills at 2x input and Claude Code uses heavily), output). Cost is **computed at query time**
and always shown as two separate numbers:

- **reported** — what the tool itself recorded (Opencode); absent for tools that report none.
- **estimated** — an *API-equivalent estimate* from list prices in `convo_index/pricing.toml`
  (revision date inside the file). Exact model id or explicit alias only; anything else is
  *unpriced* and the total shows as `≥ $x (n% priced)`. Subscription plans are not billed
  this way; treat the number as "what this would have cost on the API".

Add or override prices in `~/.config/convo-index/pricing.toml` (same layout: `[models]`,
`[aliases]`, `zero_cost_providers`); `convo-index doctor` lists model ids without a price.
The Analytics page breaks tokens and cost down by model, source, month, project and
ticket/PR/branch, plus the top conversations by tokens.

## MCP server
    convo-index mcp     # JSON-RPC over stdio

Tools: `search_conversations(query, source?, tags?, project?, mode?, limit?)`,
`get_conversation(id, max_messages?, max_chars?, include_tools?)`, `get_related(key)`.
Hidden and deleted conversations never appear. Register it with your agent, e.g. for Claude
Code:

    claude mcp add convo-index -- convo-index mcp

Whatever an agent retrieves goes to that agent's model, which may be hosted. The MCP server
makes no summary or tag calls of its own; semantic search embeds the query with the configured
embedder under the same privacy policy as the CLI.

## Tags
Conversations are tagged into **user-defined groups**. The simple `[category]` block defines
one exclusive `workspace` group (labels + cwd hints). For several groups use the advanced form:

    [[tag_groups]]
    name = "topic"
    exclusive = false
    description = "what the conversation is about"
    [[tags]]
    name = "infra"
    group = "topic"
    description = "servers, deploys, CI"
    rules = { branch_prefix = ["infra/"], regex = ["ansible|deploy"] }
    [[tags]]
    name = "review"
    group = "topic"
    description = "reviewing someone's PR"
    rules = { regex = ["\\bPR\\b"] }
    # rule kinds: path_contains (cwd), branch_prefix, regex (title + first prompt), source (tool)

Per conversation and group: your decision wins, then rules (applied at ingest, no LLM), then one
LLM call for whatever is still open (bounded per run like summaries). Filter rows in the UI
render from the groups; in the detail pane click a tag to accept it, shift-click to reject it,
"none" for none of these, ↺ to drop your decision. `convo-index tag <id> workspace=personal`,
`convo-index tag --list`, `convo-index tag --retag` do the same from the shell.
## References and related work
Every conversation is scanned for Jira-style tickets (`[references].ticket_regex`), GitHub PR and
issue links on `[references].github_hosts`, `owner/repo#123`, branch names (the session's own
branch, or an explicit `checkout -b`), commits and URLs. Each hit is kept with the message it came
from and how it matched, so it can be explained and, if wrong, switched off with the × on its
badge. Only *linkable* references connect conversations: tickets seen in prose (not only in tool
output), full PR/issue links, and non-default branches. A bare "PR 123" links only when the source
recorded the repository. The detail pane's **Related work** lists every other conversation sharing
a linkable key, grouped by key, across tools, without transitive merging; `convo-index related
<key>` prints the timeline for a ticket, PR or branch. `default_branches` and `github_hosts` are
configurable.
