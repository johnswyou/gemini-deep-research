# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Non-streaming runs, `gdr resume`, `gdr plan`, and `gdr status` now
  render responses whose outputs arrive under `steps[].content[]` (or
  as SDK content objects) via a single response normalizer — previously
  only the streamed happy path survived an empty `outputs` fetch.
- `gdr research --output DIR` with a directory outside the configured
  `output_dir` no longer completes the (paid) run and then crashes at
  render time: explicit `--output` paths are honored verbatim, while
  derived paths remain confined to `output_dir`.
- A run is recorded in the local store as soon as its interaction id is
  known (status `in_progress`), so `gdr ls` / `gdr status` /
  `gdr resume` now work after Ctrl+C, timeouts, and crashes — not just
  after clean completions. Ctrl+C prints a resume hint and exits 130.
- Runs that end `failed` / `cancelled` / `incomplete` now exit 1 / 2 / 1
  (previously a fast failure exited 0) while still writing metadata and
  transcript artifacts for post-mortems; failure details are captured
  in `metadata.json` when the API provides them.
- Malformed config files and other configuration errors print a
  one-line message and exit 4 in every command instead of a traceback.
- Polling now retries transient network failures with backoff (up to 5
  consecutive attempts) instead of abandoning a running task on the
  first blip; exhaustion exits 5 with reattach instructions.
- `interactions.create()` requests now send `store=true` explicitly and
  `incomplete` is recognized as a terminal status (no more polling it
  for the full 60-minute cap).
- Embedded `env:VAR` references in config values now expand
  (`"Bearer env:TOKEN"` works as documented); unset variables are a
  config error instead of being sent as literal text.
- Config-declared `[mcp_servers.*]` entries are actually attached to
  research runs (CLI `--mcp` flags win on name collision).
- `auto_open = true` now opens the finished report (TTY only) — the key
  previously did nothing.
- `gdr show` prints reports, JSON artifacts, and image paths through
  plain stdout instead of Rich, so piped output is no longer wrapped
  (and corrupted) at 80 columns; ambiguous id prefixes now say so
  instead of claiming no record exists.
- `gdr plan approve` gained `--max`; approving a Max plan no longer
  silently downgrades to the default agent. Cancelling an interactive
  plan review prints the plan id for later `gdr plan refine/approve`.
- `gdr doctor` actually compares the installed google-genai version
  against the required minimum (previously any importable version
  passed) and shares the key fingerprint / config template with the
  rest of the CLI.
- `gdr config get` redacts `api_key` and auth-like MCP header values
  unless `--reveal` is passed; scalar values print unwrapped for shell
  capture. `gdr resume` updates the stored record's status/tokens.
- Metadata usage now reads the SDK's `total_input_tokens` /
  `total_output_tokens` spellings; unrecognized plan-prompt input
  re-prompts instead of approving; `--file` inputs over ~20 MB are
  rejected up front with a pointer to File Search; `--url` guarantees
  the `url_context` tool even when config narrows `default_tools`.

### Removed

- Unused `platformdirs` and `httpx` dependencies; dead `ls`/`show`
  `--config` no-op flags; vestigial internal helpers.

## [0.1.2] - 2026-05-25

### Fixed

- Fixed a streaming artifact regression in `v0.1.1` where the final
  report could print correctly in the terminal but `report.md`,
  `sources.json`, and `transcript.json` were written empty when the
  terminal `interactions.get(...)` response returned `outputs=[]`.
  Cleanly completed streams are now retained as a fallback artifact
  source when the final fetch has no outputs.

## [0.1.1] - 2026-05-24

### Fixed

- Updated live streaming to parse the current Gemini Interactions API
  event schema (`interaction.created`, `interaction.status_update`,
  `step.start`, `step.delta`, `step.stop`, and
  `interaction.completed`) while retaining compatibility with the
  previous `interaction.start` / `content.*` event names.
- Documented the current streaming schema and the `--no-stream`
  polling fallback for diagnosing streaming transport or schema issues.

## [0.1.0] - 2026-04-23

First public release. Ships the full CLI surface for driving Google's
Deep Research / Deep Research Max via the Gemini Interactions API.

### Added

- `gdr research <query>` with polling, live streaming of thought
  summaries, collaborative planning (`--plan`), and auto-recovery from
  stream disconnects.
- Tool and multimodal input flags: `--tool`, `--mcp NAME=URL`,
  `--mcp-header NAME=K:V`, `--file PATH`, `--url URL`,
  `--file-search-store NAME`, `--visualization auto|off`,
  `--untrusted-input`.
- Collaborative planning commands: `gdr plan refine` and
  `gdr plan approve` for iterating on research plans before execution.
- History and follow-up commands: `gdr ls`, `gdr show`, `gdr status`,
  `gdr resume`, `gdr follow-up`, `gdr cancel`.
- Operator commands: `gdr config {path,get,set,edit}` and
  `gdr doctor [--fix]` (7 diagnostic checks, auto-creates missing
  dirs + config template).
- XDG-style local state and config (`$XDG_STATE_HOME/gdr/`,
  `$XDG_CONFIG_HOME/gdr/config.toml`) with env-var overrides
  (`GDR_STATE_DIR`, `GDR_CONFIG_PATH`, `GEMINI_API_KEY`).
- Security hardening: MCP header validation, secret redaction in
  `transcript.json`, path confinement under `output_dir`, and
  untrusted-input tool filtering (drops `code_execution` and
  `mcp_server` when untrusted content enters the context).
- Artifact layout per run: `report.md`, `sources.json`,
  `metadata.json`, `transcript.json`, and `images/image_NNN.<ext>`
  when outputs contain image payloads.
- Documentation: `docs/USAGE.md`, `docs/MCP.md`,
  `docs/TROUBLESHOOTING.md`, and five runnable examples under
  `examples/`.
- Prominent "unofficial, not affiliated with Google" disclaimer in
  README and PyPI description. "Gemini" and "Deep Research" are
  trademarks of Google LLC, used nominatively throughout.
- 348 unit tests, 93% line coverage, Ruff + Mypy strict clean.

[Unreleased]: https://github.com/johnswyou/gemini-deep-research/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/johnswyou/gemini-deep-research/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/johnswyou/gemini-deep-research/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/johnswyou/gemini-deep-research/releases/tag/v0.1.0
