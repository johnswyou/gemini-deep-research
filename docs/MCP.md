# gdr — MCP Server How-To

[Model Context Protocol](https://modelcontextprotocol.io/) (MCP)
servers extend Deep Research with your own tools and data sources.
`gdr` supports attaching MCP servers two ways: at the CLI with
`--mcp` / `--mcp-header`, and persistently in `config.toml`.

This document covers the security model, wire shape, and edge cases.
If you want a runnable example, see
[`examples/03_mcp.sh`](../examples/03_mcp.sh).

---

## TL;DR

```bash
# One-shot: attach an MCP server for a single run
gdr research \
  --mcp deploys=https://mcp.example.com \
  --mcp-header 'deploys=Authorization:Bearer $DEPLOY_TOKEN' \
  "Summarize our last 10 production deploys"
```

```toml
# Persistent: declare in ~/.config/gdr/config.toml
[mcp_servers.deploys]
url = "https://mcp.example.com"
headers.Authorization = "Bearer env:DEPLOY_TOKEN"
```

---

## Flag anatomy

### `--mcp NAME=URL`

Declares an MCP server with a stable local `NAME` and an HTTPS `URL`.

* `NAME` must be unique within a single `gdr` invocation. Duplicate
  names raise `ConfigError` (exit 4).
* `URL` must use `http://` or `https://`. Other schemes are rejected.
* Splitting is on the *first* `=` only, so URLs containing `=` (query
  strings, filters) survive:

  ```bash
  --mcp svc=https://mcp.example.com/path?tenant=acme
  #          ^^ split here
  ```

Repeatable — attach multiple servers with multiple `--mcp` flags.

### `--mcp-header NAME=Key:Value`

Attach a single HTTP header to the MCP request to `NAME`.

* Splitting is on the *first* `=` for the name, then the *first* `:`
  for the header key / value. This lets values contain `:` (e.g.
  `Bearer abc:123`) without needing escaping:

  ```
  --mcp-header deploys=Authorization:Bearer abc:123
                       ^           ^ first colon: split key / value
                       ^ first equals: split NAME from rest
  ```

* Header names must match `^[A-Za-z0-9][A-Za-z0-9\-]{0,63}$`.
* Header values must not contain `\r`, `\n`, or `\0` — rejected as
  header injection attempts.
* Reserved / hop-by-hop headers (`Host`, `Content-Length`,
  `Connection`, `Transfer-Encoding`, `Upgrade`,
  `Proxy-Authorization`, `TE`, `Trailer`, `Expect`) are rejected.

Repeatable — and `--mcp-header` entries can come in any order relative
to their `--mcp` declaration.

---

## TOML form

For persistent configuration, declare servers under `[mcp_servers.<name>]`:

```toml
[mcp_servers.factset]
url = "https://mcp.factset.com"
headers.Authorization = "Bearer env:FACTSET_TOKEN"
allowed_tools = ["search_filings", "get_fundamentals"]

[mcp_servers.deploys]
url = "https://mcp.example.com"
headers.Authorization = "Bearer env:DEPLOY_TOKEN"
headers.X-Workspace = "production"
```

* `env:VAR` is expanded at config load time. Missing env vars raise
  `ConfigError`.
* `allowed_tools` is optional. When set, only the named MCP tools
  become callable; the rest are filtered server-side.

> **Note:** CLI `--mcp` flags currently *replace* rather than merge
> with TOML-declared servers. If you want to combine CLI overrides
> with a persistent server, repeat the CLI flag or use config.

---

## Security model

### Header validation is eager

`build_tools()` validates every MCP header before any wire-shape
assembly. One bad header aborts the whole request — partial kwargs
never reach the API.

### Redaction

When `transcript.json` is written, MCP headers whose names contain
any of `auth`, `token`, `key`, `secret`, `cookie` (case-insensitive)
are replaced with `[REDACTED]`. Original values are never touched
in-memory past the initial send.

Example transcript snippet:

```json
{
  "type": "mcp_server_call",
  "server": "deploys",
  "headers": {
    "Authorization": "[REDACTED]",
    "Accept": "application/json"
  }
}
```

### Path confinement

MCP servers cannot write outside the configured `output_dir`. Every
artifact path is resolved and checked against the configured root via
`Path.is_relative_to`. Refused escapes raise `ConfigError`.

### Untrusted input mode

When `--untrusted-input` is set, **or** when `--file`/`--url` is
supplied *and* `config.safe_untrusted = true` (default), the
following tools are stripped from the outgoing request:

* `code_execution`
* `mcp_server`

Rationale: both let external content influence code paths the agent
can execute. If you're grounding in attacker-controlled documents,
you probably don't want those documents to be able to trigger an MCP
call or run Python.

The CLI prints a yellow warning listing the stripped tools so users
see what changed.

To **keep** MCP servers while grounding in a file, set
`safe_untrusted = false` in config (accepts the risk). `gdr` will
still strip them if `--untrusted-input` is passed explicitly.

### No secret exfiltration via `--dry-run`

The `--dry-run` output includes the full request JSON, which
includes MCP header values. This is intentional — the point of
`--dry-run` is to show exactly what would hit the wire. If you don't
want secrets in your terminal scrollback, unset the env var before
running:

```bash
(unset GEMINI_API_KEY DEPLOY_TOKEN; gdr research --dry-run --mcp ...)
```

---

## Wire shape

The tools list sent to the API is deterministic: simple builtin tools
first, then `file_search` (if any), then `mcp_server` entries in
declaration order.

```json
{
  "tools": [
    {"type": "google_search"},
    {"type": "url_context"},
    {"type": "mcp_server",
     "name": "deploys",
     "url": "https://mcp.example.com",
     "headers": {"Authorization": "Bearer ..."}}
  ]
}
```

Tests in `tests/unit/test_requests.py::TestCombinedToolsAndMultimodal`
assert this ordering explicitly; if you notice it has drifted, file
a bug.

---

## Troubleshooting

**`--mcp-header` complains about `unknown MCP server`**
: You referenced a NAME in `--mcp-header` that has no matching
  `--mcp NAME=URL` declaration. Likely a typo — MCP names are
  case-sensitive.

**`Invalid MCP header name`**
: Header names must match `^[A-Za-z0-9][A-Za-z0-9-]{0,63}$`. Check
  for trailing spaces or non-ASCII.

**`Invalid MCP header value: CR, LF, and NUL characters are not permitted`**
: Classic header-injection shape. If this is a legitimate value,
  base64-encode it yourself before passing.

**Bearer token keeps getting redacted in the transcript**
: That's by design. The transcript is for audit, not for resuming
  the run — use the actual interaction id for that.

**MCP tool silently disappeared**
: Check the command output for a yellow "Untrusted-input mode
  stripped tools" warning. `--file`/`--url` + default
  `safe_untrusted=true` triggers this automatically.
