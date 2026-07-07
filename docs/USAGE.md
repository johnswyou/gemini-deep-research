# gdr — Usage Guide

The authoritative reference for every command and flag that `gdr`
ships with. If this document drifts from `gdr <command> --help`, trust
the `--help` output.

---

## Command index

| Command | Purpose |
| --- | --- |
| [`gdr research`](#gdr-research-query) | Start a Deep Research task and save artifacts |
| [`gdr plan refine`](#gdr-plan-refine-id-feedback) | Iterate on a pending plan |
| [`gdr plan approve`](#gdr-plan-approve-id) | Execute an approved plan |
| [`gdr ls`](#gdr-ls) | List recent interactions from the local store |
| [`gdr show`](#gdr-show-id) | Print a saved artifact |
| [`gdr status`](#gdr-status-id) | Check the current status of an interaction |
| [`gdr resume`](#gdr-resume-id) | Reattach to a running or completed interaction |
| [`gdr follow-up`](#gdr-follow-up-id-question) | Ask a follow-up question |
| [`gdr cancel`](#gdr-cancel-id) | Cancel an in-progress interaction |
| [`gdr config`](#gdr-config) | Manage the TOML config file |
| [`gdr doctor`](#gdr-doctor) | Validate the local environment |

---

## `gdr research <query>`

Run a single Deep Research task end-to-end: submit a query, stream or
poll until the agent is done, write artifacts, append a local history
record.

### Flags

| Flag | Purpose |
| --- | --- |
| `--max` | Use Deep Research Max (higher quality, longer runtime, higher cost). |
| `--plan` | Review and refine the agent's plan before execution. |
| `--stream / --no-stream` | Toggle live thought summaries and text deltas. Defaults to on when stdout is a TTY. |
| `-o / --output DIR` | Write artifacts to an exact directory (overrides the default `<ts>_<slug>_<id>` layout). |
| `--tool NAME` | Enable a simple builtin tool. Repeatable. Valid: `google_search`, `url_context`, `code_execution`. Overrides config defaults when specified. |
| `--mcp NAME=URL` | Attach an MCP server. Repeatable. See [`docs/MCP.md`](MCP.md). |
| `--mcp-header NAME=Key:Value` | Attach a header to an MCP server. Repeatable. |
| `--file PATH` | Attach a local file (PDF, image, audio, video, CSV, …) as input. Repeatable. |
| `--url URL` | Attach a URL for the agent to ground on. Enables `url_context`. Repeatable. |
| `--file-search-store NAME` | Enable File Search on a named store. Bare names are auto-prefixed with `fileSearchStores/`. |
| `--visualization auto\|off` | Control chart/infographic generation. |
| `--untrusted-input` | Treat inputs as untrusted. Strips `code_execution` and `mcp_server` tools. |
| `--dry-run` | Print the request body as JSON and exit without calling the API. |
| `--api-key KEY` | Override `GEMINI_API_KEY` for this run only. |
| `--no-confirm` | Skip the Max cost-confirmation prompt. |
| `--config PATH` | Use an alternate config TOML. |

Streaming uses the current Interactions API event schema
(`interaction.created`, `interaction.status_update`, `step.start`,
`step.delta`, `step.stop`, `interaction.completed`). If you need to
avoid the live SSE connection in a script or while debugging a network
issue, pass `--no-stream`; the task still runs in the background and
`gdr` polls until completion.

### Examples

```bash
# Shortest path
gdr research "Latest trends in RISC-V adoption"

# Max quality, no confirmation prompt (scripts)
gdr research --max --no-confirm "Competitive landscape of EV batteries"

# Collaborative planning
gdr research --plan "Impact of AI on semiconductor supply chain"

# Ground in a local PDF
gdr research --file ~/Downloads/10k.pdf \
  "Compare risk factors vs our 2024 filing"

# MCP server with Bearer auth
gdr research \
  --mcp deploys=https://mcp.example.com \
  --mcp-header "deploys=Authorization:Bearer $DEPLOY_TOKEN" \
  "Summarize our last 10 production deploys"

# Dry-run — see the request shape without calling the API
gdr research --dry-run --api-key AIza-xxxx-xxxx \
  --tool google_search --visualization off "Anything"
```

### `--plan` flow

When `--plan` is set, `gdr` first creates a *plan interaction*
(`collaborative_planning=True`). You see the plan as Markdown and are
prompted:

```
[A]pprove / [R]efine / [C]ancel
```

* **A** — approve and execute. The final interaction uses
  `previous_interaction_id=<plan_id>` and the user "input" becomes the
  sentinel string `"Plan looks good!"`.
* **R** — refine. You're prompted for feedback. Empty feedback skips
  the round trip and re-prompts.
* **C** — cancel. Exit 0 without executing.

Plans always run with `thinking_summaries="none"` and
`visualization="off"` (plans should return fast; streaming UI adds no
value at this step).

`--file` and `--url` inputs ground the *plan* interaction, and the
executed research inherits them through `previous_interaction_id` —
you don't need to re-attach anything at approval time. With
`--max --plan`, the Max cost confirmation appears before the plan
interaction is created (the plan itself already runs on the Max
agent).

---

## `gdr plan refine <id> <feedback>`

One-shot plan refinement for use across terminal sessions. Creates a
new plan interaction with `previous_interaction_id=<id>` and
`collaborative_planning=True`, using your feedback as the input.
Pass `--max` when the plan was created with the Max agent — like
`plan approve`, refinement does not remember the original choice.

Prints the new plan and the new plan id. On non-TTY stdout the new id
is also written to stdout alone on the last line, so scripts can
capture it:

```bash
new_id=$(gdr plan refine $OLD_PLAN_ID "focus on 2024 data" | tail -n 1)
gdr plan approve "$new_id"
```

### Flags

| Flag | Purpose |
| --- | --- |
| `--max` | Refine with Deep Research Max (match how the plan was created). |
| `--api-key KEY` | Override the API key. |
| `--config PATH` | Alternate config TOML. |

---

## `gdr plan approve <id>`

Approve a plan and kick off the full research run. Internally this is
the same execution pipeline as `gdr research --plan` resuming from the
approval step.

### Flags

| Flag | Purpose |
| --- | --- |
| `-q / --query LABEL` | Label for the output directory slug (defaults to `approved-plan-<id6>`). |
| `--max` | Execute with Deep Research Max (match how the plan was created). |
| `--stream / --no-stream` | Toggle live streaming. |
| `-o / --output DIR` | Exact output directory. |
| `--dry-run` | Print the request body without calling the API. |
| `--api-key KEY` | Override the API key. |
| `--config PATH` | Alternate config TOML. |

---

## `gdr ls`

List recent interactions from the local JsonlStore. Pure local
operation — no API calls.

### Flags

| Flag | Purpose |
| --- | --- |
| `-n / --limit N` | Maximum rows (most recent first). Default 20. |
| `--status S` | Filter by status: `completed`, `failed`, `cancelled`, `incomplete`, `budget_exceeded`, `in_progress`. |
| `--since DATE` | Filter by creation time. Accepts relative (`7d`, `24h`, `30m`, `2w`), dates (`YYYY-MM-DD`), and ISO 8601. |
| `--full-id` | Show full interaction ids instead of truncated. |

### Example

```bash
gdr ls --status completed --since 7d
```

---

## `gdr show <id>`

Print a saved artifact from a prior research run. Accepts a unique
prefix of the interaction id for convenience (so you can type
`gdr show intabc` when the full id is `intabcxyz123`, provided no other
run shares that prefix).

### Flags

| Flag | Purpose |
| --- | --- |
| `-p / --part {text,sources,metadata,transcript,images}` | Which artifact to render. Default `text` → prints `report.md`. |

Output is plain stdout (no wrapping), so `gdr show <id> > report.md`
round-trips exactly.

### Examples

```bash
gdr show intabcxyz123                          # print report.md
gdr show intabcxyz123 --part sources           # citations as JSON
gdr show intabcxyz123 --part images            # list image file paths
```

---

## `gdr status <id>`

One-shot status check on an in-progress or completed interaction.
Prints current status, timing (when a local record exists: elapsed
wallclock while running, run duration once finished), token usage,
and the last thought summary if the agent is still running.

Useful for quick visibility after you detached from a streaming run.

### Flags

| Flag | Purpose |
| --- | --- |
| `--api-key KEY` | Override the API key. |
| `--config PATH` | Alternate config TOML. |

---

## `gdr resume <id>`

Reattach to a running or completed interaction and finish writing
artifacts to disk. Common triggers:

1. You Ctrl+C'd a streaming run. The original command exited with
   code 130 and a "resume" hint.
2. The run completed while you were away.

### Behavior

* The local store must have a record for `<id>` so `gdr` can
  reconstruct a `RunContext` (for artifact rendering). Without a
  record the command exits 4 and suggests re-running via
  `gdr research`.
* If the interaction is still `in_progress`, `gdr resume` polls to
  completion with the same live-status UI as `gdr research`.
* **Collision handling**: if the original output directory has files,
  `gdr resume` writes to a sibling directory suffixed
  `_resumed_<timestamp>` — nothing is overwritten. Pass `--force` to
  overwrite in place.

### Flags

| Flag | Purpose |
| --- | --- |
| `--force` | Overwrite artifacts in the original run directory. |
| `--api-key KEY` | Override the API key. |
| `--config PATH` | Alternate config TOML. |

---

## `gdr follow-up <id> <question>`

Ask a follow-up question using a prior interaction as context. Creates
a new interaction with `previous_interaction_id=<id>`. The follow-up
runs the full research pipeline — streaming, artifacts, local history
record, the lot.

### Flags

A subset of the `gdr research` flags: `--max`, `--model`,
`--stream/--no-stream`, `-o/--output`, `--untrusted-input`,
`--dry-run`, `--api-key`, `--no-confirm`, and `--config`. Tool and
input flags (`--tool`, `--mcp`, `--file`, `--url`,
`--file-search-store`, `--visualization`) are not available on
follow-ups — the parent interaction's context carries over instead.
There is no `--plan` (follow-ups skip planning).

Two execution modes:

* Default — re-runs the full Deep Research agent grounded in the
  parent context (minutes, ~$1-3).
* `--model gemini-3.1-pro-preview` — answers with a plain Gemini
  model instead: fast and cheap, right for "elaborate on section 3"
  clarifications. Mutually exclusive with `--max`; sends no research
  tools.

> **API stability note:** in April 2026 the Gemini API rejected
> agent-mode follow-ups on *completed* research parents with an opaque
> HTTP 400. As of 2026-07-07 the same request is accepted and completes
> (validated live, end-to-end). If the rejection ever returns, gdr
> prints the `--model` fallback and alternatives — see
> [`docs/TROUBLESHOOTING.md`](TROUBLESHOOTING.md#gdr-follow-up-fails-with-http-400-on-a-completed-research-run).

If the parent run executed in untrusted-input mode, the follow-up
inherits that posture automatically (persisted in the local record);
`--untrusted-input` forces it when no local record exists.

### Example

```bash
gdr follow-up intabcxyz123 "Elaborate on section 3"
```

---

## `gdr cancel <id>`

Cancel an in-progress Deep Research interaction. Idempotent — calling
it on an already-terminal interaction is a no-op with a friendly
message.

If the installed `google-genai` SDK build doesn't expose
`interactions.cancel`, `gdr` prints a clear message and exits 4; no
AttributeError leaks through.

### Flags

| Flag | Purpose |
| --- | --- |
| `--api-key KEY` | Override the API key. |
| `--config PATH` | Alternate config TOML. |

---

## `gdr config`

Manage the TOML configuration file. Four subcommands.

### `gdr config path`

Print the resolved config file path on stdout (plain, unformatted —
script-friendly).

```bash
CFG=$(gdr config path)
```

Resolution order: `$GDR_CONFIG_PATH` → `$XDG_CONFIG_HOME/gdr/config.toml`
→ `~/.config/gdr/config.toml`.

### `gdr config get [KEY]`

Print the full resolved config, or the value at a dot-separated path:

```bash
gdr config get                          # full config (secrets redacted)
gdr config get default_agent            # scalar
gdr config get mcp_servers.factset.url  # nested
gdr config get api_key --reveal         # print the actual secret
```

Secrets (`api_key`, auth-like MCP headers) print as `[REDACTED]`
unless `--reveal` is passed. Exit 4 if `KEY` doesn't resolve.

### `gdr config set KEY VALUE`

Write a *top-level* scalar key into the config TOML (lists and
nested tables need `gdr config edit`). Types are inferred:

* `true` / `false` → bool
* `42` → int
* `3.14` → float
* anything else → string

Nested keys (e.g. `mcp_servers.factset.url`) are **not** supported —
use `gdr config edit` for those. The value is validated against the
`Config` Pydantic model before writing; invalid values abort the
write. Comments in the file are lost (see `gdr config edit`).

### `gdr config edit`

Open the config file in `$EDITOR` (falling back to `$VISUAL`, then
`vi`). Creates a minimal template if the file doesn't exist yet.

---

## `gdr doctor`

Validate the local environment. Runs seven checks in sequence and
prints a Rich table with PASS / WARN / FAIL per check.

### Checks

1. **Python version** ≥ 3.10
2. **google-genai installed** — package importable; version string reported.
3. **config file** present (WARN when missing — fixable).
4. **API key available** — checks `GEMINI_API_KEY` env then
   `config.api_key`. Prints a fingerprint (first 4 + last 4 chars),
   never the raw key.
5. **Network reachable** — DNS lookup for
   `generativelanguage.googleapis.com`. **Does not** call the API;
   zero quota impact.
6. **output_dir** exists and is writable.
7. **state_dir** exists and is writable.

### Flags

| Flag | Purpose |
| --- | --- |
| `--fix` | Create missing directories and a minimal config template. Idempotent. |
| `--config PATH` | Alternate config TOML. |

### Example

```bash
gdr doctor --fix    # first-time setup
gdr doctor          # routine health check
```

Exits 0 on all-pass or warn-only. Exits 4 on any failing check.

---

## Exit codes

Every command uses the same exit-code convention. Scripting `gdr`
invocations can branch on these.

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Research failed (`status=failed`) OR stream error |
| 2 | Research cancelled (`status=cancelled`) |
| 3 | Research timed out (60-minute cap) |
| 4 | Auth / config / validation error |
| 5 | Network error (request failed, or polling failed 5x in a row) |
| 130 | User Ctrl+C (task may still be running — resume with `gdr resume <id>`) |

---

## Artifact layout

Each `gdr research` run writes a timestamped directory under
`output_dir`:

```
<output_dir>/2026-04-22T14-32_history-of-google-tpus_abc123/
├── report.md          # Final synthesized text with citations
├── sources.json       # Deduplicated citation list
├── metadata.json      # Interaction id, timings, tools, usage
├── transcript.json    # Raw outputs with MCP/auth redaction applied
└── images/
    ├── image_001.png  # Charts/infographics (if any)
    └── image_002.png
```

The slug is derived from the query: lowercased, non-alphanumerics
collapsed to dashes, capped at 64 chars, suffixed with the first 6
alphanumerics of the interaction id to disambiguate.

---

## Configuration reference

All keys are optional; every one has a default.

| Key | Type | Default | Purpose |
| --- | --- | --- | --- |
| `api_key` | string | `None` | Gemini API key. Accepts `env:VAR` to expand from environment. |
| `default_agent` | string | `"deep-research-preview-04-2026"` | Agent used when `--max` is not set. |
| `output_dir` | path | `~/gdr-reports` | Root for all artifact directories. |
| `auto_open` | bool | `true` | Open the finished report with the system opener (`open`/`xdg-open`) when stdout is a TTY. |
| `confirm_max` | bool | `true` | Prompt before running Max agents. |
| `default_tools` | list\[string\] | `["google_search", "url_context", "code_execution"]` | Tools enabled when no `--tool` flags are passed. |
| `thinking_summaries` | `"auto"` or `"none"` | `"auto"` | Whether the agent produces thought summaries. |
| `visualization` | `"auto"` or `"off"` | `"auto"` | Chart/infographic generation. |
| `safe_untrusted` | bool | `true` | When `--file`/`--url` is passed, auto-enable untrusted mode. |
| `mcp_servers.<name>.url` | string | — | MCP server endpoint. |
| `mcp_servers.<name>.headers.<Header>` | string | — | Headers. `env:VAR` expansion supported. |
| `mcp_servers.<name>.allowed_tools` | list\[string\] | `None` | Optional allowlist of MCP tool names. |

---

## Environment variables

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | API key (takes precedence over `config.api_key`). |
| `GDR_CONFIG_PATH` | Override config location. |
| `GDR_STATE_DIR` | Override local-history store location. |
| `XDG_CONFIG_HOME` / `XDG_STATE_HOME` | XDG overrides (respected when `GDR_*` unset). |
| `EDITOR` / `VISUAL` | Used by `gdr config edit`. |
