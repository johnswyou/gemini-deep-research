# Deep Code Review — `gdr` (gemini-deep-research)

> **HISTORICAL DOCUMENT.** This review describes the v0.1.2 tree; its
> P0/P1 findings were remediated in v0.1.3 (see `CHANGELOG.md`) and it
> is superseded by [`CODE_REVIEW_v0.1.3.md`](CODE_REVIEW_v0.1.3.md).
> Do not treat the defects below as current.

**Date:** 2026-07-07 · **Tree reviewed:** `9a70b2e` (v0.1.2 + hotfix PRs #1/#2) · **Scope:** full repo — architecture, correctness, code quality, tests, docs, packaging, CI.

**Reference:** [Gemini Deep Research API docs](https://ai.google.dev/gemini-api/docs/deep-research)

---

## Verdict

The codebase is well above average for a young CLI: clean `cli → commands → core` layering, a single execution pipeline (`execute_research`), a single wire-shape choke point (`build_create_kwargs`), frozen Pydantic domain models, injectable clocks/sleeps, real security primitives (header validation, redaction, path confinement), and strict ruff/mypy gates. That discipline is real and worth keeping.

The gap between this and a *robust* tool is concentrated in a few systemic weaknesses rather than scattered sloppiness:

1. **The response-shape problem was patched, not fixed.** The v0.1.2 hotfix ("Persist streamed outputs when final fetch is empty") bandaged the streaming happy path only; every other consumer of `interaction.outputs` (polling mode, `resume`, `plan`, `status`) still reads the source the hotfix itself declares empty.
2. **State is persisted too late.** Records are written only after a fully successful run+render, which makes the recovery features (`resume`, `ls`, the timeout hint) unable to serve the exact failure cases they exist for.
3. **The error-handling story is aspirational.** The documented exception→exit-code boundary, retry budget, and `NetworkError` don't exist in code; several commands leak raw tracebacks; one path exits 0 on a failed research.
4. **A meaningful amount of dead/duplicated surface** has accumulated (unused config keys, dead exceptions, four copies of `_get`, two divergent config templates, vestigial stream builders).

The test suite (354 tests) is disciplined but mock-heavy at the SDK seam — the two live-API hotfixes were exactly the class of bug it structurally cannot catch. **The suite is also not green on a clean checkout** (one environment-dependent failure, itself exposing a product bug).

---

## P0 — Correctness bugs / data loss

### 1. Every non-streamed path still reads `outputs` that the project itself says can be empty

The hotfix commit `3680ed8` and `AggregatedSnapshot`'s docstring (`src/gdr/core/streaming.py:199-204`) state the premise: *"The current Interactions API may return an empty `outputs` list from `.get()` after a clean stream."* Google's current docs describe results under `steps[].content[]`, not a flat `outputs[]`, which is consistent with that premise. The fallback added in v0.1.2 injects the *streamed* snapshot only in `_consume_create_result` (`src/gdr/commands/research.py:647-653`). Everything else still reads `.get().outputs` directly:

- `rendering._outputs_of` (`src/gdr/core/rendering.py:68-72`) — **`--no-stream` runs write empty reports** (`*(No final report text was returned.)*`). Streaming defaults **off** whenever stdout is not a TTY (`research.py:152-155`), so every scripted/CI/cron invocation takes this broken path.
- `resume.py:110` → same reader — `gdr resume` renders empty artifacts.
- `planning.extract_plan_text` (`src/gdr/core/planning.py:104-116`) — the entire `--plan` / `plan refine` flow shows "(no plan text returned)".
- `status._print_last_thought` (`src/gdr/commands/status.py:99-106`) — reads output types/fields (`summary`) that only the tests' mocks exhibit.
- `collect_sources` (`rendering.py:146-166`) — even on the streamed path, citations come only from stream `text_annotation_delta` events; a `.get()` with empty outputs means `sources.json` from polling/resume is always `[]`.

**Fix shape:** verify what `interactions.get()` actually returns today (SDK ≥1.55), then build **one** response adapter (`interaction → normalized outputs`) that understands both shapes and is used by rendering, planning, status, and resume. Bonus: the stream carries `event_id` on every event and the API supports `last_event_id` reconnection — currently ignored, so disconnects discard the partial stream and fall back to the (possibly empty) fetch.

### 2. `--output` outside the configured root: the paid run completes, then everything is thrown away

**Reproduced under the test harness.** `_allocate_output_dir` returns a user `--output` override verbatim — its docstring claims it's "still subject to `SecurityPolicy.confine`" but the code never calls it (`src/gdr/commands/research.py:86-100`). `write_artifacts` *does* confine against `config.output_dir` (`src/gdr/core/rendering.py:384`). Net effect for `gdr research "q" --output ./results` (any path outside the configured root, e.g. a project directory):

- the interaction is created and runs to completion (up to 60 min, $1–$7),
- then `ConfigError: Refusing to write outside the configured output_dir` is raised **unhandled** (raw traceback, exit 1 — not even the documented exit 4),
- no artifacts are written, no record is appended → `gdr resume` is impossible (see #3). The report is unrecoverable through gdr.

Same landmine in `resume`: artifacts are re-confined against the *current* `config.output_dir` (`resume.py:104-110`), so changing `output_dir` in config makes every older record unresumable.

**Fix shape:** decide the contract — either confine the override *at parse time* (fail before spending money) or treat explicit `--output` as user intent and exempt it from confinement (confine only derived paths). The latter matches the flag's help text.

### 3. Records are only written after success — the recovery features can't recover anything

`_record_run` is called at the end of `_finalize_and_render`, after `write_artifacts` (`src/gdr/commands/research.py:599-613`), and nowhere else. Consequences:

- **Ctrl+C mid-run:** no record. `resume.py`'s module docstring (use case #1: "exited with code 130 and a resume hint") describes machinery that doesn't exist — there is no `KeyboardInterrupt` handler anywhere; Click converts it to `Abort` (exit 1, "Aborted!", no hint, no record).
- **Timeout:** `ResearchTimedOutError` says "Resume: gdr resume <id>" (`src/gdr/ui/progress.py:125-128`) — but no record was written, so that exact command prints "No local record … re-run via gdr research". The hint is a dead end.
- **Failure/crash during render or polling:** interaction id exists server-side, is absent locally; `gdr ls` shows only successes.

**Fix shape:** append a `status="in_progress"` record immediately after `create()` returns an id, and append the terminal update at the end. `JsonlStore.append` is already last-write-wins per id — the store was built for exactly this and no caller changes are needed.

### 4. A research that fails fast exits 0 and `interaction.error` is never read

`_finalize_and_render` takes the first `.get()` result if the status is terminal — *any* terminal status, including `failed`/`cancelled` — and proceeds to write artifacts and print "Done." (`src/gdr/commands/research.py:578-615`). Only the polling path raises `ResearchFailedError`. So the exit-code contract (`1 = research failed`, `errors.py`) holds only when the failure happens to be observed *during* polling. Scripts checking `$?` will treat fast failures as success.

Compounding it: **no code path ever reads `interaction.error`** — the field Google documents as the failure diagnostic. Failed runs produce a report saying "(No final report text was returned.)" and nothing else.

### 5. No outer exception boundary — the documented exit codes are not enforced

`errors.py`'s header comment promises "the CLI convert[s] any exception into a deterministic exit code at the outer boundary." No such boundary exists (`cli.py` registers commands bare). Verified consequences:

- Malformed config TOML → Rich-formatted `ConfigError` traceback, exit 1 (documented: friendly message, exit 4). `load_config` is called outside try/except in `research.run` (`research.py:293`), `plan._build_plan_client`, and every command using `_common.load_cfg` (ls/show/status/resume/cancel/follow-up).
- Transient network exceptions from bare `client.interactions.get` calls (`research.py:579`, and see #6) → raw tracebacks.
- `config set` on a corrupt existing TOML → `_load_toml_raw` raises `ConfigError` uncaught (`src/gdr/commands/config.py:140`).

Meanwhile exit code 5 is hard-coded as a magic number at seven call sites (`research.py:465,481`, `plan.py:108`, `status.py:48`, `cancel.py:42,64`, `resume.py:85`) instead of using a typed error — and `NetworkError` (the class that owns exit 5) is **never raised anywhere**. Docs describe a retry budget ("network error after retries exhausted"; `TROUBLESHOOTING.md` mentions a 429/5xx retry budget) and `client.py:14-16` refers to a `gdr.core.retry` module — none of which exist.

**Fix shape:** one `try/except GdrError` boundary (either a Typer callback wrapper or a small decorator applied to each command) + actually wrap SDK calls into typed errors. Delete or implement the retry story — don't document it.

### 6. The 60-minute polling loop has zero fault tolerance

`poll_until_complete` calls `get(id=...)` bare (`src/gdr/ui/progress.py:106-130`). One transient DNS blip, 429, or 5xx during a 20-60 minute run kills the CLI with a traceback — and because of #3, with no local record. For the *primary use case of this tool* (very long-running paid tasks), the poll loop needs retry-with-backoff on transient errors more than any other line in the repo needs anything.

---

## P1 — Functional bugs

### 7. Config-declared `[mcp_servers.*]` are parsed, validated, documented … and never used

`Config.mcp_servers` (`src/gdr/config.py:78`) has no consumer — no code merges config MCP servers into a `RunContext` (grep: only `ctx.mcp_servers` from CLI flags is ever read). The config docstring, README, `docs/MCP.md`, and the `config edit` template all showcase `[mcp_servers.factset]`. Users who configure it get silently nothing.

### 8. The documented MCP auth-header form silently doesn't expand

`env:` expansion is prefix-only (`src/gdr/config.py:119-141`), but the canonical example everywhere — config.py's own docstring (line 17), README, `docs/MCP.md`, the `config edit` template (`commands/config.py:324`) — is `headers.Authorization = "Bearer env:FACTSET_TOKEN"`. That value doesn't start with `env:`, so the literal string `Bearer env:FACTSET_TOKEN` would be sent as the header. Worse, `tests/unit/test_config.py:154-168` *asserts* the non-expansion as intended behavior, and transcript redaction masks the header so users can't see what was sent. (Moot today only because of #7.) Either support token-level expansion inside values, or fix every example to `"env:VAR"` holding the full `Bearer …` string.

### 9. `gdr show` corrupts machine-readable output; suite is red on a clean checkout

`show` prints report text and image paths via `console.print` (`src/gdr/commands/show.py:102,126-127`), which hard-wraps at terminal width (80 on pipes). `gdr show <id> > report.md` inserts newlines into long lines; `--part images` splits file paths mid-name. This is precisely the footgun the project's own progress-log pattern documents ("machine-readable output must use `typer.echo`"). It also makes `test_show_command.py::test_images_part_lists_files` fail whenever pytest's tmp path is long enough — which it is in this environment: **353 passed, 1 failed** on `main`.

### 10. `gdr plan approve` silently downgrades the agent

`approve_cmd` hard-codes `use_max=False` (`src/gdr/commands/plan.py:163`) and offers no `--max` flag. A plan created with `gdr research --max --plan`, refined, then approved asynchronously executes on the default (fast) agent with no warning. Also inconsistent: the interactive `--plan` path correctly carries `use_max` through.

### 11. `auto_open` is a documented no-op

`Config.auto_open` → `RunContext.auto_open` (`research.py:147`) is never read. `docs/USAGE.md` promises "Open the report … when done." Implement (`typer.launch`/`webbrowser`) or remove the key from config, models, template, and docs.

### 12. `--url` doesn't guarantee `url_context` when config tools were narrowed

`ensure_url_context_tool` runs only when `--tool` flags were passed (`research.py:196-197`). A user with `default_tools = ["google_search"]` in config who runs `gdr research q --url https://…` gets URLs injected as text with no `url_context` tool. The comment's assumption ("config defaults already include url_context") only holds for the *shipped* default.

### 13. Streaming aggregator edge losses

- Image chunks move from `_image_chunks_by_index` to `_images` only on `step.stop` (`src/gdr/core/streaming.py:357-370`). If `interaction.completed` arrives without a preceding `step.stop` (or on disconnect), buffered image data is silently dropped from the snapshot — the very snapshot v0.1.2 now uses as an artifact source. `_handle_complete` should flush open builders.
- `snapshot_outputs` hard-codes `"mime_type": "image/png"` (`streaming.py:419`) — the delta's mime type (if any) is discarded.
- The `usage` carried on `interaction.completed` (present in the current-schema fixture) is ignored; when the final fetch is empty, streamed runs record `total_tokens=None`.
- `interaction.completed` with `status="failed"` still sets `completed_cleanly=True`, so failed streamed outputs would be attached as a fallback "report".

### 14. `_record_run` can't read usage from dict-shaped interactions

`getattr(getattr(interaction, "usage", None), "total_tokens", None)` (`research.py:724`) uses attribute-only access in a function that otherwise carefully handles both attribute and dict shapes. Dict-shaped interactions (which `_with_fallback_outputs` produces for dict inputs!) always record `total_tokens=None`.

### 15. `gdr config` sharp edges

- `gdr config get [api_key]` prints resolved secrets post-`env:` expansion in plaintext (`commands/config.py:91-104`); `policy.redact` exists and isn't used here. (Flagged by the project's own security audit; still open.)
- `config set` claims list support ("scalar/list key" — `commands/config.py:112`) but `_infer_type` can never produce a list, so `default_tools` is unsettable via `set` (Pydantic rejects the string) — misleading help.
- `config set api_key <literal>` writes the secret to disk with default (usually world-readable) permissions; no `chmod 600`, no nudge toward `env:`.

### 16. Follow-up ergonomics diverge from docs and from the API's design

- `docs/USAGE.md` says follow-up takes "same execution-time flags as research minus `--plan`"; it actually lacks `--tool/--mcp/--mcp-header/--file/--url/--file-search-store/--visualization/--untrusted-input` (`src/gdr/commands/follow_up.py:30-54`).
- The parent run's untrusted posture is not inherited (Record doesn't persist it) — their own audit note, still open.
- Google's docs show follow-ups can target a *lighter model* (`model="gemini-3.1-pro-preview"`) for cheap clarifications; gdr always re-runs a full deep-research agent, so "Elaborate on point 2" costs $1–3 and minutes. Worth a `--model`/`--light` escape hatch, or at least a docs note.

### 17. `doctor`'s SDK check verifies nothing

`_check_genai` passes if `google.genai` merely imports; it never compares the version to `MIN_GENAI_VERSION` despite printing "(required >= 1.55.0)" (`src/gdr/commands/doctor.py:91-105`). It also reads `genai.__version__` (may not exist) while `client.py` correctly uses `importlib.metadata`. An SDK too old for the Interactions API — the one failure this check exists to catch — passes.

### 18. Plan-decision prompt approves on any unrecognized input

`prompt_plan_decision` returns APPROVE for anything not starting with r/c (`src/gdr/core/planning.py:184-189`). Typing `x`, `q`, or garbage at the prompt approves and spends money. Unrecognized input should re-prompt.

### 19. `parse_file` has no size guard

`--file` slurps and base64-encodes anything (`src/gdr/core/inputs.py:72-88`) — a 2 GB video becomes ~2.7 GB of RAM and an oversized request the API will reject after upload time. Check size up front and point at the Files API for large media.

---

## P2 — Design & robustness observations

- **Verify `store=True`.** Google's docs list `background=True` *and* `store=True` as required for Deep Research; `build_create_kwargs` never sends `store` (`src/gdr/core/requests.py:107-118`). If the SDK defaults it, fine — but pin it explicitly at the choke point whose whole job is making the wire shape auditable.
- **Four `_get` implementations** (`rendering.py:53`, `streaming.py:181`, `planning.py:96`, `_common.py:137` as `get_attr_or_key`). The dual attribute/dict tolerance exists *only* because tests feed dicts. One shared helper (or a normalization boundary at the client façade) removes both the duplication and the test-shape leakage into production code.
- **`resume` inconsistencies:** `--force` overwrites the original `metadata.json` with an empty `tools` list (the docstring's "the original run already wrote the authoritative version" is exactly wrong under `--force`, `resume.py:151-169`); resumed `duration_seconds` = wall time since the *original* start, so resuming yesterday's run reports a 20-hour duration; resume never updates the record's status.
- **`http://` MCP URLs are accepted with auth headers** (`models.py:88-93`) — plaintext credential transmission deserves at least a warning.
- **`--config` is accepted and silently ignored** by `ls` and `show` (`ls.py:63`, `show.py:52`, "reserved for future"). Accepting a no-op flag misleads; drop it until it does something.
- **Ambiguous `show` prefix matches** report "No record found" (`show.py:130-139`) — a "prefix matches N records" message would prevent head-scratching.
- **`_print_done`'s non-TTY behavior** prints the styled block *plus* a raw report path, and the comment demonstrates a `--quiet` flag that doesn't exist (`research.py:753-758`). `$(gdr research …)` captures everything, not the last line. Same double-print pattern in `plan refine` (`plan.py:117-119`).
- **Duplicated logic:** `_stdout_is_tty` ×3 (`research.py:152`, `plan.py:180`, `follow_up.py:76`); id6-fragment regex ×3 (`research.py:97`, `security.py:254`, `plan.py:185`); API-key resolution ×3 (`research.py:67-74`, `plan.py:61`, `_common.py:50`); config template ×2 **already divergent** (`commands/config.py:307-326` vs `doctor.py:227-241` — doctor's is missing five keys; the stated circular-import rationale doesn't hold, a shared constant module would do); key fingerprint ×2 with different thresholds (`client.py:48-57` vs `doctor.py:191-194`); tools-summary assembly ×2 (`research.py:719-722` vs `rendering.py:326-328`); status color palette ×2 (`ls.py:118-126`, `status.py:71-79`).
- **Timer only advances on events** — `stream_with_live_ui` updates the status line per event; during long quiet periods the elapsed display freezes (the comment at `live.py:191-192` claims the opposite).
- **`except StreamError: raise` inside the `next(iterator)` try is unreachable** (`live.py:199-200`) — `StreamError` is raised by `agg.feed`, which is outside the try.

---

## Code smells, dead code, naming

**Dead code (delete or wire up):**

| Item | Location | Note |
|---|---|---|
| `_ThoughtBuilder.add` / `_ImageBuilder.add` + the `elif isinstance(builder, _ImageBuilder)` branch | `streaming.py:117-163, 368-369` | Deltas bypass builders (thoughts→`_thoughts`, images→`_image_chunks_by_index`); `_TextBuilder.finalize()`'s value is discarded too. Two parallel accumulation systems, one vestigial. |
| `NetworkError` | `errors.py:50-53` | Never raised; exit 5 is hand-rolled everywhere. |
| `SecurityPolicy.output_subdir` | `security.py:245-256` | No callers; duplicates `_allocate_output_dir`. |
| `SecurityPolicy.safe_untrusted` field | `security.py:227` | Stored, never read (decision happens in `research.py:314`). |
| `KNOWN_AGENTS`, `STATUS_IN_PROGRESS` | `constants.py:16,53` | No consumers (status literals re-typed in palettes). |
| `Record.note` | `models.py:200` | Never set or displayed. |
| `default_agent_config`, `default_run_context_for_query` | `models.py:208-219` | Test-only helpers living in the production module. |
| `Store.list_children` + impl | `persistence.py:79,174-179` | Follow-ups set `parent_id`, nothing ever lists children. |
| `_SENTINEL = object()` + `_ = _SENTINEL` | `commands/config.py:193,336-338` | Dead object kept alive purely to silence lint. |
| `_loaded` flag; `_ = lineno  # reserved` | `persistence.py:103,137` | Set/assigned, never read. |
| `platformdirs`, `httpx` deps | `pyproject.toml:36-37` | Code deliberately avoids platformdirs (documented!); httpx never imported. |
| `live` pytest marker + conftest claim | `pyproject.toml:157`, `tests/conftest.py:3` | No live test exists; the promised `RUN_LIVE_TESTS` gate is unimplemented — the first live test added would run in CI. |

**Docstrings that describe fiction** (each cost real review time):
`client.py:14-16` (nonexistent `gdr.core.retry`); `streaming.py:38-42` module header ("final report is never reconstructed from the stream") contradicted by the hotfix behavior four screens down; `_allocate_output_dir`'s confine claim (`research.py:86-92`); `resume.py:4-7` (exit-130-plus-hint flow that doesn't exist); `run_with_live_status` ("writes an interaction-id footer" — the transient status line is erased on exit); `render_report_markdown` ("otherwise we recollect" — images are never recollected).

**Naming / intuition:**

- `_resolve_agent` (`research.py:103-110`) — the `if config.default_agent not in (…)` branch returns the same expression as the fallthrough. Dead conditional; the function reads as if it validates but doesn't. (The test-suite review confirmed nothing e2e asserts `--max` picks `AGENT_MAX` — this function is one typo away from shipping the wrong agent, green.)
- `load_cfg` vs `load_config` — an alias whose docstring admits it exists to avoid an import line; two names for one thing across the codebase.
- `_build_request_kwargs` (command layer) wrapping `build_create_kwargs` (core) — near-identical names, different signatures; the `plan_mode_for_dry_run and dry_run` double-flag dance is hard to follow. A `PlanPreview` vs `Execution` enum or separate function would read better.
- `AGENT_FAST` — invented vocabulary. Google says "Deep Research" and "Deep Research Max"; `ls` displays a third vocabulary ("preview"/"max", `ls.py:129-136`). Pick one.
- `gdr show --part text` prints the artifact everyone else calls `report` (paths dict key, filename). `--part report` would be the intuitive name.
- `AggregatedSnapshot.completed_cleanly` vs `LiveStreamResult.completed_cleanly` vs `_CreateOutcome.fallback_outputs` — three types shuttle the same facts through one call chain (`aggregator → live UI → command`); consider collapsing.
- errors.py's exit-code comment says 130 is "raised by Typer / signal handler" — Click actually converts Ctrl+C to `Abort` → exit 1. The table in USAGE.md inherits the fiction.

---

## Tests (summary of the dedicated pass)

Full detail in the review session; highlights:

1. **The SDK seam is unspecced.** Every command test patches `google.genai.Client` with a bare `MagicMock` and invents the response shape (`SimpleNamespace(outputs=[…])`). Request kwargs and response shapes are asserted against themselves — wrong method names, wrong kwarg names, or the `outputs`→`steps` migration all ship green. This is exactly the class of bug that produced v0.1.1/v0.1.2. Recommend `MagicMock(spec=…)` from the real SDK surface plus one recorded-fixture test of a real completed interaction.
2. **`--max` → `AGENT_MAX` is never asserted end-to-end** (no test inspects `create.call_args.kwargs["agent"]` on the `--max` path) — pairs dangerously with the `_resolve_agent` dead branch.
3. **Fixture drift:** 5 of 6 stream fixtures use the *superseded* schema (`interaction.start`/`content.*`); only the happy path exists in the current schema. Error, disconnect, out-of-order, and image cases have no current-schema coverage.
4. **The polling fallback is never driven at the command level** — every command test returns `completed` on the first `.get()`; even `test_disconnect_mid_stream_falls_through_to_polling` never actually polls.
5. `test_config.py:154-168` asserts the broken `"Bearer env:…"` non-expansion as intended (see #8).
6. One environment-dependent failure on clean checkout (see #9); brittle Rich-width string assertions are a latent class of the same.

---

## Docs / packaging / CI (summary of the dedicated pass)

- `docs/MCP.md` + `USAGE.md` show `--mcp-header '…Bearer $DEPLOY_TOKEN'` in **single quotes** — the variable never expands (the `examples/03_mcp.sh` version is correct).
- `examples/05_follow_up.sh:26` — `awk 'NR > 2'` on a `box=None` Rich table (header + row = 2 lines) always prints nothing → script always exits "No prior interactions found". Should be `NR > 1`.
- `examples/02_planning.sh:27` — pipes `C` (Cancel) into `research --plan` then greps for `plan-…` shaped ids that don't exist; the captured id is always empty.
- Exit-code docs describe retries that don't exist (see #5); `USAGE.md` documents `auto_open` (see #11) and follow-up flags (see #16).
- `release.yml` holds `id-token: write` (PyPI trusted publishing) while pinning actions to mutable tags (`@v4`, `@v3`, `@release/v1`) — pin to SHAs on the publish-privileged workflow (also flagged in the repo's own security audit). The RC gating itself is *correct* (uses `-rc.` and strips it in the version guard — a `v1.0.0-search` tag fails the version match before publish).
- `CHANGELOG.md` hard-codes "348 unit tests, 93% coverage" — already stale (354 tests).
- Positive: exit-code table, defaults, model ids, artifact names, redaction lists, and `uv.lock` (google-genai 1.73.1 ≥ 1.55.0) all verified consistent.

---

## Priority fix list

| # | Action | Findings |
|---|---|---|
| 1 | Verify current `interactions.get()` response shape against SDK 1.73; build one response adapter used by rendering/planning/status/resume; send `store=True` explicitly | P0-1, P2 |
| 2 | Write the record at `create()` time (in_progress) + terminal update; add Ctrl+C handler printing the resume hint | P0-3 |
| 3 | Resolve the `--output` confinement contract (confine at parse time or exempt overrides) | P0-2 |
| 4 | Add the outer GdrError→exit-code boundary; wrap SDK calls in typed errors; retry transient errors in `poll_until_complete`; check terminal status before declaring success | P0-4/5/6 |
| 5 | Wire up or delete: config `mcp_servers`, `auto_open`; fix the `env:` header story and its docs/tests | P1-7/8/11 |
| 6 | `typer.echo` for `show` output; fix the red test; add `--max` to `plan approve`; real version check in `doctor` | P1-9/10/17 |
| 7 | Test hardening: spec'd mocks, current-schema error/disconnect fixtures, e2e `--max` agent assertion, command-level polling test | Tests 1-4 |
| 8 | Dead-code sweep + dedupe (`_get`, templates, fingerprints, tty checks, id6); fix fiction docstrings | Smells |

*Generated by an automated deep-review session; findings above were verified by reading the full source and, where noted, by empirical reproduction under the test harness.*
