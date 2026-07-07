# gdr — Troubleshooting

Common failure modes and how to recover. If you hit something not
covered here, `gdr doctor` is usually the right first step.

---

## First responder: `gdr doctor`

Before deep-diving into any specific issue, run:

```bash
gdr doctor
```

You'll see a table like:

```
Check                   Status  Detail
Python version          PASS    Python 3.12.12 (required >= 3.10)
google-genai installed  PASS    version=2.10.0 (required >= 2.0.0)
config file             WARN    not found at ~/.config/gdr/config.toml
API key available       PASS    from GEMINI_API_KEY env, fingerprint AIza…YVyE
Network reachable       PASS    DNS OK for generativelanguage.googleapis.com
output_dir              PASS    /Users/you/gdr-reports
state_dir               WARN    missing; run --fix to create
```

Any `FAIL` line should be the first thing you fix. `WARN` lines are
safe to defer or auto-fix with `gdr doctor --fix`.

---

## Exit codes

`gdr` commands exit with documented codes. Shell scripts can branch
on these without parsing stderr.

| Code | Meaning | Typical cause |
| --- | --- | --- |
| 0 | Success | — |
| 1 | Research failed / stream error | API returned `status=failed`, or a mid-stream error event (e.g. `RATE_LIMITED`). |
| 2 | Research cancelled | Run was cancelled (by user via `gdr cancel <id>` or externally). |
| 3 | Research timed out | Hit the documented 60-minute cap on a task. |
| 4 | Auth / config / validation error | Missing/invalid API key, malformed TOML, unknown flag value, bad MCP header, etc. |
| 5 | Network error | A request failed, or polling failed 5 times in a row (polls retry with backoff first). |
| 130 | User interrupt (Ctrl+C) | Stream or poll was interrupted. Task may still be running — `gdr resume <id>`. |

---

## `Error: API key is missing`  (exit 4)

You're running a command that needs to call the API but no key was
found. `gdr` checks:

1. `--api-key` flag
2. `GEMINI_API_KEY` env var
3. `config.api_key` (with `env:VAR` expansion)

Fix:

```bash
# Option 1 — one-off
export GEMINI_API_KEY=AIza…

# Option 2 — persist in config
gdr config set api_key env:GEMINI_API_KEY
```

Check what `gdr` sees:

```bash
gdr doctor
# API key available   PASS   from GEMINI_API_KEY env, fingerprint AIza…YVyE
```

---

## `Stream disconnected (ReadError); falling through to polling.`

Not an error — this is `gdr` recovering from a dropped SSE connection.
When the live stream dies mid-run (TCP reset, network flap, 600s
idle), `gdr` automatically switches to `.get(id=...)` polling. You
lose the partial thought-summary buffer but get the full final report
as long as the task succeeds upstream.

If polling also fails, you'll see the `ID printed` hint — use
`gdr resume <id>` to reattach once your network is stable.

---

## The spinner starts but no interaction id appears

Run `gdr doctor` first. If the API key and network checks pass, try:

```bash
gdr research --no-stream "your question"
```

`--no-stream` bypasses the live SSE parser and uses background polling
only. If polling works but streaming does not, the likely cause is a
streaming event schema mismatch or a transport issue. Current
Interactions streams use `interaction.created`, `step.start`,
`step.delta`, `step.stop`, and `interaction.completed` events.

---

## `Research timed out after 60:00` (exit 3)

A single Deep Research task is capped at 60 minutes. If your run
reaches the cap, it exits with code 3 and the interaction id is
printed.

Options:

* Run again with a more focused query.
* Use `--max` for tougher questions — it's designed for longer
  horizons.
* Split the work: run a planning phase with `--plan`, approve, then
  optionally chain follow-ups with `gdr follow-up <id>`.

---

## Ctrl+C during a streaming run (exit 130)

Ctrl+C cleanly disconnects the stream but **does not** cancel the
interaction. The task continues running upstream. `gdr` prints:

```
Interrupted. The research continues server-side.
  Check on it:  gdr status <id>
  Reattach:     gdr resume <id>
```

When you're ready:

```bash
gdr resume <interaction-id>
```

This polls to completion (or reports the terminal status if it's
already done) and writes artifacts. If the run's directory already
contains files, resume writes to a sibling `_resumed_<ts>` directory
instead of clobbering them (the common Ctrl+C case has no artifacts
yet, so the original directory is used); pass `--force` to overwrite
in place.

If you want to actually kill the task, `gdr cancel <id>` — but note
that cancellation also costs the tokens used up to that point.

---

## `Refusing to write invalid config`  (exit 4)

`gdr config set` validates the resulting config dict against the
Pydantic model before touching disk. If you're trying to set a value
that fails validation (enum member not allowed, wrong type, unknown
key), nothing gets written.

Common culprits:

* `thinking_summaries` — only `"auto"` or `"none"`.
* `visualization` — only `"auto"` or `"off"`.
* `default_tools` — must be a list of simple builtin names.

Escape hatch: `gdr config edit` opens `$EDITOR` on the file directly,
bypassing the CLI's validation. Misconfigurations will still error
on the next `gdr research` (also exit 4).

---

## `--mcp-header references unknown MCP server`  (exit 4)

You passed a `--mcp-header NAME=...` for a `NAME` that has no matching
`--mcp NAME=URL` declaration. Likely a typo.

```bash
# BAD
gdr research --mcp deploys=https://mcp.example.com \
             --mcp-header deploy=Authorization:Bearer abc
#                         ^^^^^^ different name

# GOOD
gdr research --mcp deploys=https://mcp.example.com \
             --mcp-header deploys=Authorization:Bearer abc
```

---

## `Invalid MCP header value: CR, LF, and NUL characters are not permitted`

Header injection protection kicked in. If this is legitimate (a
value happens to contain a newline), base64-encode it before passing.
See [`docs/MCP.md`](MCP.md#security-model) for the validation rules.

---

## `Untrusted-input mode stripped tools: code_execution, mcp_server`

Not an error. `gdr` has automatically stripped dangerous tools from
your run because either:

* You passed `--untrusted-input` explicitly, **or**
* You passed `--file` / `--url` while `config.safe_untrusted = true`
  (the default).

If you need `code_execution` or `mcp_server` despite attaching
untrusted inputs, set `safe_untrusted = false` in config:

```bash
gdr config set safe_untrusted false
```

You're now opting in to the risk that an attacker-controlled file
could induce tool use. Read [`docs/MCP.md`](MCP.md) first.

---

## `No record found for id X`  (exit 4 from `gdr show` or `gdr resume`)

The local JsonlStore doesn't have a record for `<id>`. Causes:

* Typo — try `gdr ls` to see known ids.
* Record was created on a different machine — `gdr` history is local,
  not synced.
* Prefix collision — `gdr show` auto-tries prefix match if exact
  lookup fails, but if two ids share the prefix, neither wins.

`gdr resume` requires a record to reconstruct the `RunContext`. If
you genuinely lost the record but the interaction still exists
upstream, re-run with `gdr research` for a fresh run.

---

## `Network reachable: FAIL: cannot resolve generativelanguage.googleapis.com`

DNS can't reach Google's AI endpoint. Usually one of:

* Corporate firewall blocking Google domains → ask IT for the
  allowlist.
* VPN misrouting DNS → disconnect and retry.
* `/etc/hosts` override gone stale → `sudo cat /etc/hosts`.

`gdr doctor`'s check is a pure DNS lookup — it doesn't call the API,
so it won't consume quota. If DNS passes but real calls fail, it's a
routing or TLS issue past the lookup.

---

## `'Invalid TOML in ~/.config/gdr/config.toml'`  (exit 4)

The config parser couldn't read your file. Check the error message
for a line number, then fix via `gdr config edit`.

Nuclear option:

```bash
mv ~/.config/gdr/config.toml{,.bak}
gdr doctor --fix   # regenerates a minimal template
```

Don't lose the backup — `config set` can't restore nested
`mcp_servers` entries from scratch.

---

## `gdr follow-up` fails with HTTP 400 on a completed research run

In April 2026 the Gemini API rejected Deep Research follow-ups whose
parent is a *completed* research interaction with an opaque
`400 - There was a problem processing your request.` — server-side
behavior, not a gdr bug (the same request 400'd against the raw SDK).
**As of 2026-07-07 this works again**: an agent-mode follow-up on a
completed research parent was validated live end-to-end.

This entry stays because the server behavior has changed before. If
you hit the 400, gdr prints the alternatives:

```bash
# Cheap clarification over the same context (plain model, unaffected):
gdr follow-up <id> "Elaborate on section 3" --model gemini-3.1-pro-preview

# Or a fresh research run with the relevant context quoted in the query.
```

`previous_interaction_id` chaining for the planning flow
(`gdr plan refine` / `gdr plan approve`) has worked throughout.

---

## `interactions.cancel` not found on SDK

You're running `gdr cancel` against an older `google-genai` build that
doesn't expose the cancel endpoint. Fix:

```bash
uv pip install -U google-genai
# or: pipx upgrade gemini-deep-research
```

`gdr cancel` checks for the method via `getattr` guard before calling,
so you see the "upgrade the SDK" message instead of a raw
AttributeError.

---

## Tests that need a PNG

If you're hacking on `gdr` itself and writing a test that needs a
real image file, use the 67-byte inline PNG used in existing tests
(`tests/unit/test_rendering.py::_TINY_PNG_B64`). It's a valid
1×1-pixel transparent PNG that `mimetypes.guess_type` recognizes.

---

## Getting more detail

Most commands accept a `--config PATH` flag so you can point `gdr` at
a non-default file and A/B test. If something looks wrong, try the
`--dry-run` flag on `gdr research` / `gdr follow-up` — it prints the
full request JSON without calling the API, which is usually enough to
spot a flag that got dropped.

If you're still stuck, `gdr --version` and `gdr doctor` output are
what you'll want to include in a bug report.
