# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/johnswyou/gemini-deep-research/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/johnswyou/gemini-deep-research/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/johnswyou/gemini-deep-research/releases/tag/v0.1.0
