# Generic Conversation Index — Design

Date: 2026-09-16

## Goal

A tool anyone can install that:

1. Indexes conversations from **Claude Code, Opencode, Codex CLI, Antigravity CLI and the pi
   coding agent** (and new tools via a small adapter).
2. Lets the user **define their own tags** and classifies conversations into them with a
   cheap model (Haiku by default, Sonnet or any other model by config).
3. **Learns from manual tags**: tagging one conversation feeds the classifier and suggests
   the same tag for similar conversations, for the user to accept or reject.
4. **Shows related work**: every other conversation, across tools, that touched the same
   PR, ticket or branch.
5. Records **token usage and approximate cost** per request, conversation, project, ticket,
   tool and model, with the estimate clearly separated from tool-reported cost.
6. Talks to LLMs and embedding models through **generic provider interfaces** so it is not
   tied to Anthropic or Ollama.

## Non-goals

- Editing or resuming conversations from the UI.
- Multi-machine sync. The DB is one SQLite file; `convo-index backup` produces a consistent
  copy (WAL-safe) that can be moved.
- Perfect fidelity for every tool. Each conversation carries a `fidelity` flag and the UI says
  when text is partial.
- Cross-tool "which agent is better" leaderboards. Resolution and failure labels are LLM
  guesses over truncated transcripts and are shown as such, never ranked.

## Sources (formats verified 2026-09-16)

| Tool | Location | Shape | Change key | Usage / model |
|---|---|---|---|---|
| **Claude Code** | `~/.claude/projects/<encoded-cwd>/<session>.jsonl` (437 sessions, 201MB) | JSONL, one event per line with `uuid`, `parentUuid`, `type`. `user`/`assistant` events carry `message.content` (string or typed parts: text, thinking, tool_use, tool_result), `cwd`, `gitBranch`, `timestamp`, `entrypoint`, `isSidechain`, `agentId` on subagent turns. `ai-title` carries the title. | file mtime + size | `message.model` and `message.usage` on every assistant event: `input_tokens` (uncached), `output_tokens`, `cache_creation_input_tokens` (split by 5m/1h in `cache_creation`), `cache_read_input_tokens`, `output_tokens_details.thinking_tokens`. One assistant *event* per content block; usage repeats per block for the same `requestId`, so dedupe on `requestId`. |
| **Opencode** | `opencode db path` → default `~/.local/share/opencode/opencode.db`; `OPENCODE_DB` overrides. Preferred read path: `opencode export <session-id>` → `{info, messages:[{info, parts}]}`. | 1.x: `session(id, title, directory, project_id, parent_id, model JSON, cost, tokens_*, revert, time_created, time_updated, time_archived)`; `message.data`: user rows have `model{providerID,modelID}`, assistant rows have `modelID`, `providerID`, `tokens{input,output,reasoning,cache{read,write}}`, `cost`, `time`; `part.data.type ∈ text, tool (callID, state{status,input,output}), reasoning, step-start, step-finish (tokens, cost), patch`. 2.x (verified on 2.1): `session_v2` keeps those columns, `session_message(id, session_id, type ∈ user/assistant/idle/synthetic/compaction, seq, data)` replaces message+part; user `data.text`, assistant `data.model{id,providerID}`, `tokens`, `cost`, `content[]` of text/reasoning/tool items (`id`, `name`, `state{status,input,content[{text}],error{message}}`); no branch on 2.x workspaces. | `session.time_updated` (1.x) / `session_v2.time_updated` (2.x) | Authoritative usage: **assistant `message.tokens` + `message.cost`**. `step-finish` parts repeat the same numbers and must not be summed. `cost` defaults to 0 and free models legitimately report 0, so 0 means "reported zero", not "unknown". |
| **Codex CLI** (0.154) | `$CODEX_HOME` (default `~/.codex`). `state_5.sqlite` `threads(id, rollout_path, cwd, title, first_user_message, model, model_provider, tokens_used, git_branch, git_origin_url, git_sha, history_mode, archived, created_at_ms, updated_at_ms)` pointing at `sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl`. `thread_history_1.sqlite` holds `thread_turns` / `thread_items` projections. | Rollout events: `session_meta` (cwd, cli_version, model_provider, originator), `turn_context` (turn_id, cwd, model, sandbox), `response_item` with `payload.type ∈ message (role user/developer/assistant, content input_text/output_text), reasoning (may be encrypted), custom_tool_call, custom_tool_call_output, function_call…`, `event_msg` with `payload.type ∈ task_started, item_completed, token_count, task_complete`, `token_usage_record`, `world_state`. | `threads.updated_at_ms` | `event_msg/token_count.info.last_token_usage{input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens}` per request; `total_token_usage` is cumulative and must not be summed. `cached_input_tokens` is a **subset** of `input_tokens`. Model from `turn_context` / `threads.model`. Use `response_item` as the canonical transcript, not `item_completed`, to avoid duplicates. |
| **Antigravity CLI** | `~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl` (`transcript.jsonl` had identical line counts for every local conversation; `chunks/` unused). `conversation_summaries.db` (title, workspace_uris, parent_conversation_id) is incomplete (11 rows for 15 transcripts on 2026-09-16); when absent the title is the first prompt and the workspace comes from the first tool call's `Cwd`/`SearchDirectory`. `history.jsonl` has user prompts (`display`, `timestamp`, `workspace`, optional `conversationId`); 7 of 24 records lack the id. `conversations/<id>.db` holds protobuf step blobs and is **not** used. | Transcript records: `step_index`, `source ∈ USER_EXPLICIT, MODEL`, `type ∈ USER_INPUT, PLANNER_RESPONSE, GENERIC`, `status`, `created_at`, `content`, `tool_calls[{name,args}]`, `thinking`. `USER_INPUT.content` is wrapped in `<USER_REQUEST>`. `GENERIC` rows are tool output. | transcript file mtime + size | No model or token data in the transcript. Model name exists only inside `conversations/<id>.db` blobs; leave `model = null`, `usage = none`. Workspace from `conversation_summaries.db` when present, else from `tool_calls[].args.Cwd`, else from `history.jsonl` records that carry `conversationId`. |
| **pi coding agent** (1.4) | `$PI_CODING_AGENT_DIR/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl` (default `~/.pi/agent`); `$PI_CODING_AGENT_SESSION_DIR` replaces the sessions dir with a flat directory of session files. | JSONL, format version 3: a `session` header (`id`, `cwd`, `timestamp`) then entries with `id`/`parentId` forming a tree; the live conversation is the path to the last entry, other branches are skipped and counted. `message` entries carry `{role, content}`: `user` (string or text blocks), `assistant` (text, thinking, toolCall blocks plus `provider`, `model`, `usage`), `toolResult` (`toolCallId`, `isError`, text blocks), `bashExecution` (a `!command` run from the prompt). `session_info.name` is the user's title; `model_change` adds to the model list; `compaction`, `label`, `branch_summary`, `context_edit`, `custom*` are skipped. | file mtime + size + parser version | `usage` per assistant message (`input`, `output`, `cacheRead`, `cacheWrite`, `reasoning`, `cost.total`); models as `provider/model`. |

Each adapter ships with a **fixture** captured from this machine (one real session, secrets
scrubbed) and a `supported_versions` note. Adapters check the version they find and set
`fidelity = "partial"` with a warning when they see an unknown layout rather than guessing.

Observed reference patterns in the Claude Code corpus: `github.com/<o>/<r>/pull/<n>` URLs
(hundreds of mentions of a handful of PRs), `PR #37998`, Jira keys, and branch names such as
`ap/user-offboarding-tool` shared by many sessions that never mention a ticket. The literal
string "PR 123" appears 181 times as an example in prompts and must not create links.

## Architecture

```
convo_index/
  sources/                 adapters: available() / discover() / load(ref) -> (Conversation, revision)
    claude_code.py  opencode.py  codex.py  antigravity.py  pi.py
    fixtures/              one scrubbed real session per adapter, used by tests
  model.py                 Conversation / Message / ToolCall / UsageRecord dataclasses
  providers/
    llm.py                 complete(request) -> LLMResult
    embeddings.py          embed(texts) -> EmbedResult, with chunking/batching inside
    backends/              claude_cli, anthropic_api, openai_compat
  pipeline/
    ingest.py              reconcile messages by native id; write usage records
    references.py          tickets, PR/issue URLs, branches, commits: span + method + confidence
    jobs.py                derived-job table: summary, vector, tags; retry state
    summarize.py           provider-agnostic; output flagged as uncertain
    tagging.py             rules -> LLM (few-shot from user decisions) -> suggestions
    cost.py                usage x pricing at query time
  db.py                    schema + numbered migrations (schema_migrations table)
  search.py                filters inside retrieval, pagination
  server.py / static/      UI; filter rows and link bases rendered from config
  cli.py                   convo-index init | index | serve | search | tag | related | doctor | backup | mcp
~/.config/convo-index/config.toml     XDG config; CONVO_INDEX_CONFIG overrides
~/.config/convo-index/pricing.toml    model -> rates; ships with a seed and a revision id
~/.local/share/convo-index/convos.db  data
```

Per-conversation pipeline is a fixed DAG:

```
adapter.load -> ingest (messages, tool calls, usage, metadata)
            -> references (regex; no LLM)
            -> job: summary        (LLM)
            -> job: vector         (embed title + first prompt + summary)   depends on summary*
            -> job: tags           (rules, then LLM with few-shot neighbours) depends on vector*
* falls back to title + first prompt + last assistant text when the summary job has not succeeded
```

### 1. Normalized model

```python
@dataclass
class UsageRecord:
    native_request_id: str | None   # requestId / message id / turn_id; dedupe key within a conversation
    message_seq: int | None         # the assistant message this request produced, if known
    provider: str | None; model: str | None
    input_uncached: int | None      # disjoint buckets; None = not reported
    cache_read: int | None
    cache_write: int | None
    output: int | None              # includes reasoning for Anthropic and OpenAI
    reasoning: int | None           # informational only, never priced separately
    reported_cost_usd: float | None # what the tool itself recorded (Opencode); None if it does not report
    ts: str | None

@dataclass
class ToolCall:
    native_id: str | None; name: str
    args: dict | None; result_text: str | None
    file_actions: list[tuple[str, str]]   # (action ∈ read|edit|create|delete|run, path)

@dataclass
class Message:
    native_id: str | None           # uuid / message id / step_index; None -> position fallback
    seq: int; role: str             # user | assistant | system | tool
    kind: str                       # text | tool_call | tool_result | thinking
    text: str; ts: str | None
    model: str | None
    tool_call: ToolCall | None
    content_hash: str               # sha1(role, kind, text) — for change detection and vector reuse, NOT identity

@dataclass
class Conversation:
    id: str                         # "<source>:<native id>"; never collides across tools
    source: str; native_id: str
    title: str; first_prompt: str
    cwd: str | None; project_root: str | None
    git_branch: str | None; git_origin_url: str | None
    relation: tuple[str, str] | None   # ("subagent" | "fork", parent conversation id)
    start_ts: str | None; end_ts: str | None
    provider: str | None; models: list[str]
    fidelity: str                   # full | partial
    warnings: list[str]             # normalization problems worth surfacing
    metadata: dict                  # bounded, source-specific: cli_version, agent, entrypoint, os…
    messages: list[Message]
    usage: list[UsageRecord]
    raw_ref: str                    # path or "sqlite:<db>#<rowid>"
```

Rules:

- **Identity is the native id.** Reconciliation on reindex: match by `native_id`, fall back
  to `(seq, content_hash)`; insert new, update changed, delete rows no longer present (JSONL
  truncation, Opencode `revert`, Codex archived turns). Message row ids are stable across
  runs so usage, references and vectors keep their links.
- Tool calls and results are messages with `kind` set. They are indexed in FTS and drive the
  "files touched" facet, but are excluded from summaries, vectors and few-shot examples.
- The `role/kind/text` shape is a **display and classification transcript**, rendered
  provider-neutrally before any LLM call. It is not claimed to be replayable as API messages.
- Adapters never call an LLM.
- Sessions the indexer generates itself are excluded per adapter (Claude Code: prompt marker
  and `entrypoint`; Codex: `originator = codex_exec` with our marker; Antigravity/Opencode:
  marker in first prompt) and, more generally, via `[sources.<name>].exclude_cwd` globs.

### 2. Adapter protocol and consistent reads

```python
class SourceAdapter(Protocol):
    name: str; supported_versions: str
    def available(self) -> bool: ...
    def discover(self) -> Iterable[SourceRef]: ...        # (native_id, change_key, locator)
    def load(self, ref: SourceRef) -> tuple[Conversation, str]: ...  # returns the revision actually read
```

- `load()` returns the revision it consumed (mtime+size after reading, or `time_updated`
  read inside the same transaction). The indexer stores *that*, not the discovery-time key.
- JSONL: read to the last complete line; ignore a trailing partial line and record the byte
  offset so the next run resumes there.
- SQLite sources: open read-only (`?mode=ro`), read session + messages + parts in one
  `BEGIN` so they are one snapshot. Prefer `opencode export` when the binary is present.
- A **cross-process file lock** (`convos.db.lock`) serializes the scheduler, the CLI and the
  UI's refresh button.
- Writes are one transaction per conversation, kept short, so MCP/UI readers do not hit
  `busy_timeout`.
- `convo-index backup <path>` uses the SQLite backup API; copying the file by hand while WAL
  is active is documented as unsafe.

### 3. Provider interfaces

```python
@dataclass
class LLMRequest: prompt: str; model: str; schema: dict | None; max_input_chars: int; max_output_tokens: int
@dataclass
class LLMResult:
    ok: bool; data: dict | str | None
    error: str | None               # auth | rate_limit | timeout | malformed | provider
    model_used: str | None; usage: UsageRecord | None

class LLM(Protocol):
    def complete(self, req: LLMRequest) -> LLMResult: ...

@dataclass
class EmbedResult: vectors: list[list[float] | None]; fingerprint: str
class Embedder(Protocol):
    fingerprint: str                # provider + model + revision/digest + preprocessing + metric
    def embed(self, texts: list[str]) -> EmbedResult: ...   # chunks, truncates and batches internally
```

Backends:

| Backend | Chat | Embeddings | Notes |
|---|---|---|---|
| `gemini_cli` | `gemini -p … --output-format json` when the Gemini CLI is installed, else `agy --print=… --output-format json --json-schema <schema>` (Antigravity CLI, also Google-hosted) | no | Zero config for Gemini / Antigravity users. Prompt must be attached to `--print=`; headless `agy` auto-denies file reads, so all input is inlined. |
| `codex_cli` | `codex exec --ephemeral --output-schema <file> -s read-only --skip-git-repo-check -o <out> …` | no | Zero config for Codex users. `--ephemeral` keeps the call out of `~/.codex/sessions`, so it never pollutes the index. |
| `claude_cli` | `claude -p --model … --output-format json` | no | Zero config for Claude Code users. Writes a session under `~/.claude/projects/`; filtered by the prompt marker and `entrypoint`, as today. |
| `anthropic_api` | Messages API; JSON via a tool-use schema | no | Key from `ANTHROPIC_API_KEY` or `api_key_env` in config. |
| `openai_compat` | `POST /v1/chat/completions` | `POST /v1/embeddings` | Covers Ollama, OpenAI, OpenRouter, LM Studio, vLLM, and Gemini's OpenAI-compatible endpoint. Key from `api_key_env`. |

**Default backend resolution.** An explicit `[llm].tagging` / `[llm].summary` entry always
wins. When a job has no entry, `init` (and `doctor`) probe the CLIs on PATH in this fixed
order and pick the first one that is installed and authenticated:

1. `gemini_cli` (`gemini`, then `agy`)
2. `codex_cli`
3. `claude_cli`

If none is found, the job is disabled with a `doctor` warning telling the user to configure a
backend (for example `openai_compat` against a local Ollama). The resolved choice is written
into `config.toml` by `init` so it is visible and editable, not re-derived silently on every
run. Each CLI backend carries a default model for the "cheap" tier used by tagging
(Gemini Flash, the Codex fast model, Haiku) and a "capable" tier for summaries; the user can
override either per job.

Embeddings have no CLI backend, so `[embeddings]` defaults to `openai_compat` against Ollama
and `doctor` reports when Ollama is not reachable.

- Structured output uses native JSON mode or tool-use where available, then **validates**:
  tag names must exist, exclusive groups return a single value, multi groups return arrays,
  truncated output is `malformed`.
- Retry once on `rate_limit`/`timeout`; never on `auth`/`malformed`. Failures are recorded in
  the job table (section 6), not swallowed.
- `index_meta` stores the embedder fingerprint. A different fingerprint triggers
  `convo-index reembed`, which builds `vec_*_new` tables and swaps atomically; keyword search
  keeps working throughout.
- Distance metric is cosine on L2-normalized vectors. Messages longer than the model window
  are chunked with offsets; a chunk hit points at the passage, not just the message.

Config:

```toml
[llm]
# Written by `init` after probing gemini -> codex -> claude. Edit freely; explicit entries win.
tagging = { backend = "gemini_cli", model = "gemini-3.8-flash-low" }
summary = { backend = "gemini_cli", model = "gemini-3.8-flash-high" }
# tagging = { backend = "claude_cli", model = "claude-haiku-4-5" }
# summary = { backend = "openai_compat", base_url = "http://localhost:11434/v1", model = "qwen3:8b" }

[embeddings]
backend = "openai_compat"
base_url = "http://localhost:11434/v1"
model = "nomic-embed-text"

[privacy]
hosted_processing = "ask"      # ask | allow | deny  — `init` sets this explicitly
exclude_cwd = ["~/secret-client/**"]
```

### 4. What leaves the machine

| Path | Data sent | Controlled by |
|---|---|---|
| Summary job | title + head/tail of the transcript (tool results excluded) | `[llm].summary` backend |
| Tag job | title, first prompt, summary, few-shot examples from other conversations | `[llm].tagging` backend |
| Embeddings | every indexed message text and every search query | `[embeddings]` backend |
| MCP server | search results returned to whichever agent asked; that agent may be hosted | user's agent config |

- `hosted_processing = "deny"` refuses any backend whose `base_url` host is not loopback and
  any `gemini_cli` / `codex_cli` / `claude_cli` / `anthropic_api` use. This is an endpoint policy, not a guarantee: a local
  proxy can still forward. `convo-index doctor` prints the resolved outbound paths.
- `exclude_cwd` globs and per-source `enabled` flags apply to ingestion, so excluded projects
  never reach any provider or the MCP server.
- No regex "secret redaction" is offered. Reviewers agreed it gives false confidence; the
  controls are exclusion and backend choice.

### 5. Config and scheduling

`~/.config/convo-index/config.toml`. `convo-index init` detects installed tools, asks about
hosted processing, and writes the file.

```toml
[paths]
db = "~/.local/share/convo-index/convos.db"

[server]
host = "127.0.0.1"
port = 9753

[sources]
claude_code = { enabled = true }
opencode    = { enabled = true }                  # db path from `opencode db path` / OPENCODE_DB
codex       = { enabled = true }                  # honours CODEX_HOME
antigravity = { enabled = true }
pi          = { enabled = true }                  # honours PI_CODING_AGENT_DIR / PI_CODING_AGENT_SESSION_DIR

[references]
ticket_regex = "\\b[A-Z]{2,}-\\d+\\b"
ticket_prefix_blocklist = ["UTF", "SHA", "ISO", "RFC", "EC2", "S3"]
ticket_url = "https://example.atlassian.net/browse/{key}"
default_branches = ["main", "master", "develop", "HEAD"]

[schedule]
hour = 21
minute = 0
```

`convo-index schedule install` writes a LaunchAgent on macOS, a systemd user timer on Linux,
or prints a crontab line. Nothing in the repo contains a user path.

### 6. Derived jobs and incremental indexing

```sql
schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)

derived_jobs(conversation_id TEXT, kind TEXT,            -- summary | vector | tags
             input_fingerprint TEXT,                      -- hash of the inputs the job reads
             status TEXT,                                 -- pending | ok | failed
             attempts INTEGER, last_error TEXT, last_run TEXT,
             PRIMARY KEY(conversation_id, kind))
```

- Ingestion records the source revision **independently** of derived jobs. A failed summary
  is retried on the next run without the source changing (bounded attempts, then surfaced in
  `doctor` and the UI).
- `input_fingerprint` for `summary` = hash of message hashes + prompt version + model;
  for `vector` = summary text + embedder fingerprint; for `tags` = summary + vector
  fingerprint + tag-definition revision. A job reruns when its fingerprint changes.
- Summaries also refresh when the conversation has been idle for `summary_idle_hours`
  (default 12) since the last run, so a short decisive final exchange is not missed.
- Embedding only for messages whose `content_hash` has no vector yet.
- Indexes: `conversations(start_ts)`, `conversations(source)`, `references_(kind, key)`,
  `messages(conversation_id, seq)`.

### 7. Tags

```sql
tag_groups(name TEXT PRIMARY KEY, exclusive INTEGER, description TEXT)
tags(name TEXT PRIMARY KEY, group_name TEXT, description TEXT, examples TEXT,
     rules TEXT, revision INTEGER, updated_at TEXT)

-- what the UI shows and filters on: one row per (conversation, tag), exclusive groups enforced in code
tag_assignments(conversation_id TEXT, tag TEXT, source TEXT, confidence REAL, evidence_id INTEGER,
                ts TEXT, PRIMARY KEY(conversation_id, tag))
-- source ∈ rule | llm | user

-- everything the pipeline proposed, kept for explanation and undo
tag_evidence(id INTEGER PRIMARY KEY, conversation_id TEXT, tag TEXT, method TEXT,
             confidence REAL, origin_conversation_id TEXT, definition_revision INTEGER, ts TEXT)
-- method ∈ rule | llm | neighbour

-- explicit user decisions; the classifier must never override these
tag_decisions(conversation_id TEXT, group_name TEXT, tag TEXT,   -- tag NULL = "none of these"
              decision TEXT,                                      -- accept | reject
              ts TEXT, PRIMARY KEY(conversation_id, group_name, tag))
```

Pipeline for one conversation and one group:

1. If a `tag_decisions` row exists for the group, apply it and stop.
2. **Rules** (path globs, regexes, branch prefixes) produce evidence with confidence 1.0.
3. **LLM**, one call for all unresolved groups. Prompt is generated from the definitions plus
   up to `k` (default 6) nearest conversations that have *user* decisions in that group,
   including rejections as negative examples. Input is title + first prompt + summary.
   Output is validated: exclusive groups one value or null, multi groups an array.
4. Effective assignment per group is resolved atomically: user > rule > llm; for an exclusive
   group only the top-confidence candidate is kept.

Seeded from config on first run and editable in the UI. The repo author's `globex`/`personal`
becomes the default `workspace` group with path rules; nothing employer-specific stays in
code. Filter rows in the UI render from `tag_groups`.

`convo-index tag --group topic --all` reruns step 2–4 for a group after definitions change
(revision bump), skipping conversations with decisions.

### 8. Learning from manual tags

A manual tag or rejection is a `tag_decisions` row. Two effects, both explainable:

- **Few-shot** (above): the classifier sees the user's nearest decisions on every call.
- **Neighbour suggestions**: on a new decision, find conversations with cosine similarity ≥
  `suggest_threshold` on the conversation vector that have no decision in that group and do
  not already carry the specific tag. Write `tag_evidence(method = neighbour, origin = the
  tagged conversation)`. The UI shows "N similar conversations suggested for *tag*: review",
  with the origin conversation and score visible on each. Accepting writes a decision;
  rejecting writes a rejection that also becomes a negative example.

Nothing is auto-applied in v1. Suggestions do not chain (a suggestion never spawns further
suggestions). Thresholds default to 0.80 and are per group and per embedder fingerprint; a
`convo-index tag calibrate` command reports precision of past suggestions against decisions
so the user can tune them before ever enabling auto-apply (kept as a future option).

### 9. References and related work

```sql
references_(id INTEGER PRIMARY KEY, conversation_id TEXT, kind TEXT, key TEXT,
            namespace TEXT,             -- tracker host, "github.com/o/r", or repo for branches
            message_seq INTEGER, span TEXT, method TEXT, confidence REAL, linkable INTEGER)
-- kind ∈ ticket | pr | issue | branch | commit | url
```

Extraction runs over user, assistant and tool text and stores the matched span and method so
every link is inspectable and correctable in the UI.

| kind | linkable when | not linkable |
|---|---|---|
| ticket | key matches `ticket_regex`, prefix not blocklisted, and the key appears in user or assistant text | appears only inside tool output (changelogs, docs) |
| pr / issue | full URL, or `o/r#n` with an explicit repo, or `#n` / `PR #n` when the conversation's own `git_origin_url` is recorded by the source | bare `PR 123` with no repo; repo inferred from *today's* remote of the cwd |
| branch | from the source's recorded `git_branch` or an explicit `checkout -b` / `gh pr create --head`, and not in `default_branches` | default branches; branch names found only in prose |
| commit, url | never link; kept for display and search | |

**Related work** (replaces "threads"): for a conversation, list other conversations that share
at least one *linkable* reference, grouped by that reference, newest first, across tools,
with the tool badge and the shared key shown. A conversation with a `relation` (subagent or
fork) is nested under its parent and inherits the parent's related list. There is no
transitive connected-component merge: two conversations appear together only if they share
a key directly, so one broad review session cannot glue a whole repository together. Users
can mark a reference on a conversation as a false match, which sets `linkable = 0`.

`/related/<kind>/<key>` renders one reference's timeline: every conversation on that ticket,
PR or branch in order, with resolution status and cost coverage.

### 10. Usage and cost

```sql
usage_records(id INTEGER PRIMARY KEY, conversation_id TEXT, native_request_id TEXT,
              message_id INTEGER, provider TEXT, model TEXT,
              input_uncached INTEGER, cache_read INTEGER, cache_write INTEGER,
              output INTEGER, reasoning INTEGER, reported_cost_usd REAL, ts TEXT,
              cache_write_1h INTEGER,   -- v7: Anthropic's 1-hour cache writes (2x input), NULL when not split
              UNIQUE(conversation_id, native_request_id))
```

- Buckets are **disjoint**. Adapters map into them: Anthropic `input_tokens` → `input_uncached`;
  Codex `input_tokens - cached_input_tokens` → `input_uncached`, `cached_input_tokens` →
  `cache_read`; Opencode `tokens.input` → `input_uncached`, `cache.read/write` as named.
  A source that does not report a bucket stores `NULL`, never 0.
- One record per request. Claude Code repeats usage on each content-block event of a
  request → dedupe on `requestId`. Codex `total_token_usage` is cumulative → ignored;
  `last_token_usage` is used. Opencode `step-finish` parts → ignored; assistant message
  tokens are used.
- **Cost is computed at query time**, never stored, from `pricing.toml`:

  ```toml
  revision = "2026-09"
  [models."claude-haiku-4-5"]
  input = 1.0
  output = 5.0
  cache_read = 0.1
  cache_write_5m = 1.25
  cache_write_1h = 2.0
  [aliases]
  "claude-haiku-4-5-20251001" = "claude-haiku-4-5"
  ```

  Exact id, then explicit alias, else **unpriced**. No prefix guessing. The seed file is
  generated from vendor pricing pages at release time and `doctor` lists model ids with no
  entry.
- **Prompt-size tiers.** A model may add `long_context = { above = 272000, input = .., output = .. }`:
  the rates for a request whose prompt (all input buckets, cached ones included) is over
  `above` tokens, as OpenAI gpt-6.1 (272k) and Gemini Pro (200k) bill. The tier is chosen per
  request, but usage is summed in SQL (SQLite for one conversation, DuckDB for analytics), so
  both queries also group by a size band: how many of the distinct `above` values the prompt
  exceeds. A band's floor (just above its highest threshold) picks the right tier for every
  model in it.
- Two numbers, never merged: `reported_cost` (sum of `reported_cost_usd` where not NULL) and
  `estimated_cost` (priced buckets × rates). Every aggregate also returns `coverage`: the
  share of records that were priceable. The UI shows partial totals as "≥ $x (72% priced)".
- Labelled "API-equivalent estimate" everywhere. Subscription users are not billed this way.
- Attribution to a ticket or PR counts each conversation once per reference key it is linked
  to; a conversation on two tickets appears in both totals and the page says so.

Analytics: tokens and estimated cost by month, project, source, model, and reference key;
top 20 conversations by tokens; coverage per source. No per-tool-call cost (no sound
attribution model) and no cross-tool "failure rate" ranking.

### 11. CLI and MCP

```
convo-index init | index [--source X] [--full] | serve | doctor | backup <path>
convo-index search "query" [--tag work --source codex --since 30d --json]
convo-index tag <id> <tag> | tag reject <id> <group> | tag --group X --all | tag calibrate
convo-index related <kind> <key>
convo-index reembed | resummarize | retag | schedule install
convo-index export <id> [--md] | mcp
```

`doctor` reports: sources found and versions, last index run and failures, pending/failed
derived jobs, outbound paths, unpriced models, embedder fingerprint.

The MCP server exposes `search_conversations`, `get_conversation`, `get_related` over stdio so
Claude Code, Opencode and Codex can query the archive. It respects the same exclusions and
returns `fidelity` and `source` on every hit.

**Browser turns.** The UI server can continue a conversation in its own tool. The source
adapter supplies `turn_command(native_id, prompt, cwd, edits, mcp_tools, locator)` (argv with fixed closing flags,
the validated session id, or for pi the indexed session file after its header was checked
against the id, and nothing else conversation-derived; the prompt on stdin where the tool reads it), `turn_events(line)` (its stream translated to `text`, `tool`, `tool_result`,
`denied`, `done`, `error`) and `last_activity(native_id, locator)`. `turns.TurnRunner` owns the
process: one per conversation, a global cap, its own process group (ended on timeout, stop,
server exit and after the harness returns), stdout and stderr drained concurrently into
buffers bounded by count and bytes, a watchdog, per-turn scratch directories removed when the
turn ends, and a terminal event appended only after an ingest-only refresh (`indexer
--no-jobs`: derived LLM jobs wait for the next full run), so the new turn is indexed when the
client reloads without the turn waiting on a summary queue. The
server exposes `POST /api/conversation/<id>/turn`, `GET /api/turn/<id>` (server-sent events,
resumable with `after`) and `DELETE /api/turn/<id>`, only on a loopback listener and from a
loopback peer, behind the Host/Origin/CSRF checks, and refuses disabled or excluded sources,
inferred directories and sessions that changed in the last 30 s.

### 12. Deferred or cut

| Item | Decision | Why |
|---|---|---|
| Protobuf decoding of Antigravity step blobs | cut | The JSONL transcripts make it unnecessary |
| Auto-applied neighbour tags | deferred behind calibration | Both reviewers: cosine ≥ 0.9 is not a probability; erodes trust |
| Regex secret redaction | cut | False sense of security; replaced by exclusions and endpoint policy |
| Daily digest | deferred | Nice-to-have once retrieval is dependable |
| Tool/model failure leaderboards | cut | Labels are LLM guesses over truncated transcripts |
| Tokens per tool call | cut | No sound attribution |
| Transitive thread merging | cut | Over-merges; replaced by per-reference related lists |
