# gdr — Gemini Deep Research CLI

[![PyPI version](https://img.shields.io/pypi/v/gemini-deep-research.svg)](https://pypi.org/project/gemini-deep-research/)
[![Python versions](https://img.shields.io/pypi/pyversions/gemini-deep-research.svg)](https://pypi.org/project/gemini-deep-research/)
[![License: MIT](https://img.shields.io/pypi/l/gemini-deep-research.svg)](LICENSE)
[![CI](https://github.com/johnswyou/gemini-deep-research/actions/workflows/ci.yml/badge.svg)](https://github.com/johnswyou/gemini-deep-research/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)

> **Status:** Alpha. APIs and commands may change before v1.0.

> **Disclaimer:** Unofficial, community-built CLI. Not affiliated with, endorsed by, or sponsored by Google LLC. "Gemini" and "Deep Research" are trademarks of Google LLC, used here nominatively to describe the APIs this tool interacts with.

A terminal-first client for Google's **Gemini Deep Research** and **Deep Research Max** agents. Run long-horizon research tasks from your shell and get cleanly organized artifacts — markdown reports, charts, citations, and a full transcript — saved to disk.

`gdr` is a thin, honest wrapper over the [`google-genai`](https://github.com/googleapis/python-genai) SDK. The SDK does the work; `gdr` adds ergonomics: streaming UI, safe config/secret management, local history, a collaborative planning flow, resume-after-disconnect, and safe MCP wiring.

## Why

Deep Research tasks run for 5–60 minutes. Running them from a web UI means keeping a browser tab open. `gdr` decouples the task from your terminal session: start a research run, live-stream thought summaries, walk away, resume later by ID, and get artifacts on disk that fit into any downstream pipeline.

## Install

```bash
# With pipx (recommended — isolated install)
pipx install gemini-deep-research

# With uv tool
uv tool install gemini-deep-research

# From source (dev)
git clone https://github.com/johnswyou/gemini-deep-research
cd gemini-deep-research
uv sync --extra dev
uv run gdr --help
```

## Quickstart

```bash
export GEMINI_API_KEY=...             # get one at https://aistudio.google.com/apikey

# One-line research with the fast agent
gdr research "Latest trends in RISC-V adoption"

# Maximum-quality agent for due diligence
gdr research --max "Competitive landscape of EV batteries"

# Review and refine the agent's plan before it spends tokens
gdr research --plan "Impact of AI on semiconductor supply chain"

# Ground in your own documents
gdr research --file ~/Downloads/10k.pdf \
  "Compare risk factors vs our 2024 filing"
```

Each run produces a timestamped directory under `~/gdr-reports/` containing `report.md`, `sources.json`, `transcript.json`, `metadata.json`, and any generated images.

## Command reference

| Command | Purpose |
| --- | --- |
| `gdr research <query>` | Run a research task (fast agent by default, `--max` for Max) |
| `gdr research --plan <query>` | Collaborative planning — review and refine the plan before execution |
| `gdr status <id>` | Check the status of a running or completed task |
| `gdr resume <id>` | Re-attach to a running task after Ctrl+C or disconnect |
| `gdr follow-up <id> <question>` | Ask a follow-up using the previous interaction as context |
| `gdr plan refine <id> <feedback>` | Iterate on a pending plan without executing |
| `gdr plan approve <id>` | Approve and execute a pending plan |
| `gdr cancel <id>` | Cancel a running task |
| `gdr ls` | List recent interactions |
| `gdr show <id>` | Render a saved artifact |
| `gdr config {path,get,set,edit}` | Manage the TOML config file |
| `gdr doctor [--fix]` | Diagnose and optionally repair your setup |

Run `gdr --help` or `gdr <command> --help` for full flag reference.

See [`docs/USAGE.md`](docs/USAGE.md) for long-form command documentation,
[`docs/MCP.md`](docs/MCP.md) for MCP server integration, and
[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) for common failure
modes and recovery recipes. Runnable demos live in [`examples/`](examples/).

## Configuration

Config lives at `~/.config/gdr/config.toml`. Run `gdr doctor --fix` to scaffold it. Environment variables referenced with `env:VAR_NAME` are expanded at load time so secrets stay out of the file.

```toml
api_key = "env:GEMINI_API_KEY"
default_agent = "deep-research-preview-04-2026"
output_dir = "~/gdr-reports"
auto_open = true
confirm_max = true                  # prompt before running the Max agent
default_tools = ["google_search", "url_context", "code_execution"]
thinking_summaries = "auto"         # "auto" or "none"
visualization = "auto"              # "auto" or "off"
safe_untrusted = true               # auto-strip dangerous tools when --file/--url is used

[mcp_servers.factset]
url = "https://mcp.factset.com"
headers.Authorization = "Bearer env:FACTSET_TOKEN"
```

## Safety

Deep Research agents can read files and the public web. `gdr` ships with:

- **Redaction** of MCP auth headers and API keys from `transcript.json`.
- **Path confinement**: all artifacts land under the configured `output_dir`; slug names are sanitized.
- **Header validation** for MCP servers (no CRLF injection, no reserved names).
- **`--untrusted-input`** flag that disables `code_execution` and `mcp_server` tools for a run — use when grounding in attacker-controlled files or URLs.

See [`docs/MCP.md`](docs/MCP.md) for the MCP security model and
[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) for the
`--untrusted-input` recipe.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy src
uv run pytest -q
```

## Roadmap

Deferred to v1.1: HTML/PDF export, cost estimation, SQLite history
backend, interactive setup wizard. See `docs/USAGE.md` for the
currently-shipping surface.

## License

MIT. See [`LICENSE`](LICENSE).

## Credits

Built on [Google's Gemini Interactions API](https://ai.google.dev/gemini-api/docs/interactions) and the [`google-genai`](https://github.com/googleapis/python-genai) Python SDK.
