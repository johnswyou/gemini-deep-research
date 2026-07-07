# Deep Code Review — `gdr` v0.1.3

> **Remediation status (2026-07-07):** findings A1–A3, B1–B6, the
> consolidation batch, and the docs table were fixed in the commits
> following this review (see `CHANGELOG.md` → Unreleased). Live
> validation the same day confirmed the streamed-run fix on the wire
> (transcript keeps the steps timeline) and found agent-mode follow-up
> (B4) accepted and completing again — the April 400 has lifted. Still
> open: legacy stream-schema retirement and the JsonlStore compaction
> note.

**Date:** 2026-07-07 · **Tree reviewed:** `6618dd2` (v0.1.3, clean) · **Scope:** full repo — architecture, correctness, code quality, tests, docs, packaging, CI.

**References:** [Gemini Deep Research API docs](https://ai.google.dev/gemini-api/docs/deep-research) · installed `google-genai 2.10.0` (introspected directly; all schema claims below were verified against real SDK types, not assumed).

**Supersedes:** `docs/CODE_REVIEW.md` (written against v0.1.2 / tree `9a70b2e`). Nearly all of that review's P0/P1 findings were fixed in v0.1.3; that document is now historical.

---

## Verdict

The v0.1.3 remediation was real: the response adapter (`core/normalize.py`) exists and is used everywhere, records are written at create time on the polling path, `--output` no longer detonates after a paid run, `friendly_errors` is a genuine error boundary, config MCP servers merge, and the release pipeline is hardened (pinned SHAs, OIDC, version guards). Gates verified on this tree: **ruff + format clean, mypy strict clean, 440 tests passed / 1 skipped (live-gated)**.

The remaining risk is concentrated in exactly one place — the same place that produced three consecutive patch releases (0.1.1, 0.1.2, 0.1.3): **the gap between what the mocks/fixtures say the SDK returns and what google-genai 2.x actually returns.** Three confirmed bugs below (A, B, C) are all instances of code paths that are green in tests against hand-written dict shapes but do the wrong thing against real `google.genai.interactions` objects. Each was reproduced in this review by driving the shipped code with real SDK instances.

Beyond that: a handful of behavioral gaps in the planning/follow-up flows, a modest amount of dead/duplicated surface, and documentation that lags the remediation in several load-bearing places.

---

## A. Confirmed correctness bugs (verified against google-genai 2.10.0)

### A1. Every cleanly-streamed run discards the authoritative fetch and renders from the stream buffer

`src/gdr/commands/research.py:791`:

```python
if not fallback_outputs or bool(get_field(interaction, "outputs")):
    return interaction
```

The 2.x `Interaction` model **has no `outputs` field** (verified: `"outputs" not in Interaction.model_fields`; the fields are `output_text` + `steps[]`). So for every stream that completes cleanly — the default mode on a TTY — this guard is always false-y and the fetched interaction is replaced by the synthetic 5-field dict built at `research.py:801-807` from the stream buffer.

Reproduced with a real `Interaction` carrying a full `steps[]` timeline (`user_input`, `thought`, `google_search_call`, `model_output` with 2 `url_citation` annotations) plus a stream snapshot with different text and 1 annotation:

| artifact | before `_with_fallback_outputs` | after |
| --- | --- | --- |
| `report.md` body | authoritative fetch text | stream buffer text |
| `sources.json` | 2 citations | 1 citation |
| `transcript.json` | full steps timeline | stream text block only |

This inverts the documented contract in three places: `streaming.py:38-45` ("**The re-fetched interaction is preferred; the stream is the fallback**"), `rendering.py:331-334` ("the full `steps` timeline is emitted" in transcripts), and the 0.1.3 CHANGELOG ("transcript captures the full steps timeline"). It also reopens the door the reconnect feature closed: if a reconnect gap drops text/annotation deltas but the stream still ends with `interaction.completed`, the *holey* buffer wins over the *complete* fetch, silently.

The 0.1.2 premise ("`get()` can return empty outputs after a clean stream") was a **legacy-schema** observation; the 0.1.3 live validation itself proved the 2.x fetch returns the full report (the `--no-stream` C2 run rendered 406 lines / 87 citations from the fetch).

**Fix shape:** gate on whether the fetch has renderable content — `if not fallback_outputs or normalized_outputs(interaction): return interaction` (optionally also checking `output_text`) — and when the fallback *is* needed, merge into the interaction's dict form instead of rebuilding a 5-field synthetic (which drops `steps`, `created`, `updated`, `model`, …).

**Test gap that blessed this:** no test references `_with_fallback_outputs`/`fallback_outputs` at all. `test_research_command.py:370` covers "clean stream + fetch with `outputs=[]`" (the 0.1.2 case); there is no "clean stream + fetch with `steps[]`" case, and every fake `.get()` in the command tests returns objects that *have* an `outputs` attribute — a shape the real SDK can no longer produce.

### A2. Thought steps are invisible under the 2.x schema — `gdr status` can never print its "Thought:" line

`src/gdr/core/normalize.py:79-88` (`_iter_raw_items`) skips any step whose `content` is `None`. The real 2.x `ThoughtStep` has fields `['signature', 'summary', 'type']` — **no `content`** (verified). Reproduced: `normalized_outputs()` over a real in-progress `Interaction` containing a `ThoughtStep(summary=[TextContent("Working on section 2...")])` returns `[]`.

Consequences:

- `status.py:108-114` `_print_last_thought` never fires against real 2.x responses — the feature promised in the module docstring ("the most recent thought summary, when present") and in USAGE.md is dead on the wire. This is the same user-facing regression as the 2026-04-23 UX-backlog item 1, which was "fixed" in the adapter but only for shapes the API no longer sends.
- The `_thought_text` flattening (`normalize.py:91-105`) is only reachable via legacy `outputs` items or dict-shaped steps that carry thought *content items* — not via real 2.x thought *steps*.

**The contract tests assert a fictional shape:** `test_sdk_contract.py:239` builds the thought step as `{"type": "thought", "content": [{"type": "text", ...}]}` (a `content` key the SDK's `ThoughtStep` doesn't have), and the real-SDK construction at `test_sdk_contract.py:158` builds `ThoughtStep(type="thought", signature=...)` with **no summary at all** — so no test drives summary extraction through the real type.

**Fix shape:** in `_iter_raw_items`, when `step.type` is a thought type and `content` is absent, synthesize an item from `step.summary` (the existing `_thought_text` already knows how to flatten it). Add a contract test: real `ThoughtStep(summary=[TextContent(...)])` → one normalized `{"type": "thought"}` entry.

### A3. Failure diagnostics are read from a field that doesn't exist; real error details live on steps

`src/gdr/core/normalize.py:163-178` `error_of` reads `interaction.error`. The 2.x `Interaction` model **has no `error` field** (verified). The error payload the API documents surfaces on `ModelOutputStep.error` (fields: `code`, `message`, `details` — verified). Reproduced: a failed `Interaction` whose `model_output` step carries `Error(code="quota_exceeded", message=...)` → `error_of()` returns `None`.

Consequences: the `Detail:` line in `_print_not_completed` (`research.py:854-858`) and `metadata.json["error"]` (`rendering.py:313`) are empty exactly when the user most needs them — the post-mortem path the 0.1.3 changelog advertises. The docstring's rationale ("google-genai 1.73.1's `Interaction` model has no `error` field, but the public docs describe one") is stale reasoning carried over from the 1.x analysis.

**Fix shape:** `error_of` should fall back to scanning `steps[]` for the first step-level `error` (and keep the current dict/attr handling for the envelope shape). One more contract test with a real errored `ModelOutputStep`.

---

## B. Behavioral gaps (code-level, confirmed by reading; no SDK dependency)

### B1. Streamed runs are recorded only after the stream ends; a mid-stream `error` event loses the run entirely

Flow in `execute_research` (`research.py:519-567`): `create()` → `_consume_create_result` consumes the **entire** stream (potentially an hour) → only then is the `in_progress` record appended (`research.py:558-563`).

- During any streamed run (the TTY default), `gdr ls` in another terminal shows nothing; the code comment ("Record the run the moment it is addressable") and the 0.1.3 CHANGELOG ("recorded … as soon as its interaction id is known") are only true for the polling path. The id is addressable from the first `interaction.created` event, minutes-to-an-hour earlier.
- Worse: the `StreamError` path (`research.py:532-534`) prints and exits **before** the record write and without the interaction id (the aggregator had it; `StreamError` doesn't carry it). A run that dies on a wire `error` event is unlisted, unresumable via hint, and invisible to `gdr ls` — precisely the failure class the record-at-create rule exists for. Contrast the interrupt path, which returns the id and records before exiting.

**Fix shape:** thread an `on_start` callback (or reuse the aggregator's `start` emission) from `_consume_create_result` so the `in_progress` record is appended when the id first arrives; attach `agg.interaction_id` to `StreamError` (or catch it where the id is in scope) and print the same resume hint.

### B2. `gdr research --plan --file/--url` silently drops the attached inputs

Two compounding halves:

- The plan phase sends only text: `interactive_plan_loop(initial_query=query, ...)` (`research.py:370-375`) → `PlanRequest` has no parts (`planning.py:55-61`).
- The execution phase then clobbers the parts: `_build_request_kwargs` serializes `input_parts` into the `input` list, and `research.py:611-612` replaces the whole thing with `api_input` (`"Plan looks good!"`).

Net: `gdr research --plan --file 10k.pdf "compare risk factors"` reads and base64-encodes the PDF, then never sends it — in either request — with no warning. Meanwhile auto-untrusted mode still strips `code_execution`/`mcp_server` *because of* the file that was never sent. Same for `--url` (the URL text part is dropped; only the `url_context` tool survives).

**Fix shape:** either reject `--plan` + `--file/--url` loudly (cheapest honest option), or extend `PlanRequest` to carry input parts so the plan interaction grounds on them (the approval turn can then legitimately be text-only since context chains via `previous_interaction_id`).

### B3. `--max --plan` never shows the Max cost confirmation

The gate (`research.py:507-511`) requires `previous_interaction_id is None`; the plan loop sets it, and the comment claims "they've already consented". But in the `--plan` path no cost prompt is ever shown: the *planning* interaction itself is created against `AGENT_MAX` (`research.py:373`) before any confirmation, and the plan-approval prompt says nothing about Max pricing. (`gdr plan approve` passing `no_confirm=True` is a separate, documented decision — the interactive path is the surprising one.)

**Fix shape:** run `_confirm_max` before entering the plan loop when `use_max and config.confirm_max and not no_confirm`.

### B4. Agent-mode `gdr follow-up` is shipped, documented, and (as of last live evidence) rejected server-side

The 2026-04-23 live smoke found `previous_interaction_id` on a *terminal Deep Research parent* always 400s at Google's end (reproduced against the raw SDK at the time). The `--model` mode was added and live-validated in 0.1.3 — but the default agent mode was **not** re-validated after the 2.x migration, and neither USAGE.md nor TROUBLESHOOTING.md carries any caveat. Users following the README's happy path (`gdr follow-up <id> "question"`) may pay the fetch and hit an opaque 400.

**Fix shape:** live-validate agent-mode follow-up on 2.x once; if still broken, catch the 400 in the follow-up path with the actionable message drafted in the 2026-04-23 backlog and document the limitation. Either way the docs need a sentence.

### B5. `gdr plan refine` can silently switch agents mid-chain

`plan.py:93-99` builds the refinement with `config.default_agent`, always. Refining a plan created with `--max` chains a fast-agent interaction onto a Max parent. `plan approve` gained `--max` in 0.1.3 for exactly this reason (its help text: "Match this to how the plan was created"); `refine` was left out.

### B6. Smaller behavioral nits

- **`ls.py:128-135` `_shorten_agent`** matches substrings: a `--model gemini-3.1-pro-preview` follow-up record renders as agent "**preview**" in `gdr ls` — indistinguishable from the fast Deep Research agent, and any future agent name containing "max" collapses to "max".
- **`status.py:91-95`** prints `Elapsed: now − created_at` even for terminal runs — a run that completed yesterday shows `Elapsed: 20:00:00`. For terminal statuses use `record.finished_at − created_at` (duration) or label it "age".
- **`rendering.py:175`** stamps the report header `*Research conducted <now>*` at render time — wrong for `gdr resume` of an old run (the honest `finished_at` is already a parameter of `write_artifacts`, one hop away).
- **`research.py:519-522`** wraps `create()` failures as `NetworkError` (exit 5) unconditionally — an invalid API key (401) exits 5, though the documented convention says auth = 4. Worth classifying by the SDK's error status code.
- **External `gdr cancel` during a stream** still surfaces as `Stream error api_error` exit 1 rather than "cancelled" exit 2 (2026-04-23 backlog item 5; `streaming.py:423-427` raises unconditionally). Known-LOW, still open.
- **`research.py:810-817` `_warn_plaintext_mcp`** warns "credentials will be sent unencrypted" even when untrusted-mode stripping just removed those MCP servers from the request (`stripped` is known at the call site).
- **`doctor.py:112-118`** fail detail says the old SDK has "no Interactions API" — 1.55–1.x builds have the API but speak the rejected legacy schema; the message points at the right action for the wrong reason (client.py gets this right).

---

## C. Code design, quality, and smells

The architecture itself is in good shape and worth saying so: `cli → commands → core` layering is consistently respected; `execute_research` is a genuine single pipeline; `build_create_kwargs` is a real wire-shape choke point; frozen Pydantic models; `SecurityPolicy` as a value object; protocol-typed store; injectable clocks/sleeps everywhere it matters. The security posture (eager header validation, strip-last untrusted filtering, redaction, 0600 config, plaintext-MCP warning, pinned CI SHAs) is thoughtful.

The residual structural weakness, and the through-line of A1–A3: **response-shape knowledge has one designated home (`normalize.py`) but three escapees** — `_with_fallback_outputs` makes its own (wrong) shape decision on `outputs`; `build_transcript` prefers `steps` itself; `error_of` encodes a stale shape assumption. Pushing the remaining shape decisions into the adapter (a `has_renderable_content()` / `iter_errors()` surface) finishes the job the remediation started.

Specific smells:

1. **Client construction is triplicated.** `research._safe_build_client` + `_resolve_api_key` (`research.py:86-93, 616-625`), `_common.build_client` (`_common.py:78-96`), and `plan._build_plan_client` (`plan.py:54-69`) all implement the same CLI-flag → `GEMINI_API_KEY` → config chain with the same ConfigError-print-exit choreography. One survivor (`_common.build_client`) should absorb the other two.
2. **Double error handling in `plan.refine_cmd`** (`plan.py:100-104`): catches `GdrError`, re-prints (without the `Error:` prefix, to stdout), re-exits — under a `@friendly_errors` decorator that would have done it (to stderr, with the prefix). Pick one.
3. **Inconsistent error streams:** `friendly_errors` prints to **stderr**; the inline `ConfigError` catches in `research.py:347-349, 487-489` print to the stdout console. Piped consumers see different behavior per error class.
4. **Dead code:** `Record.note` (`models.py:204`) is never written or read. `Store.list_children` + its `JsonlStore` implementation (`persistence.py:79, 170-175`) have no production caller (tests only) — a vestige of an unbuilt "tree" feature. The no-op `try: … except ConfigError: raise` in `config.py:253-256`. The `Interaction.output_text` convenience field is never used (it's the cheapest authoritative body signal and would simplify A1's fix).
5. **Type looseness worked around with ignores:** `Config.thinking_summaries`/`visualization` are `str` + hand validators (`config.py:102-103, 126-138`) while `models.py:118-119` already defines the Literals — forcing `# type: ignore[arg-type]` at `research.py:158-159`. Reuse the Literal types in `Config` and the ignores disappear.
6. **`RunContext.agent` is overloaded** to carry the plain-model id in model mode (`models.py:150-153`). It's documented, but it's what makes the `ls` mislabeling (B6) possible; a `display_name` derived property or an explicit tagged union would remove the trap.
7. **`StreamAggregator` double-books text:** per-index `_TextBuilder.buffer` and the global `_text_chunks` both accumulate the same deltas (`streaming.py:347-352`), and the text builders' `finalize()` result is never used. The builder abstraction earns its keep only for images/thoughts. Similarly, `_content_type_from_start_event` returns raw step types (`google_search_call`, …) that `_make_builder` silently coerces to a text builder — harmless today, but the "content type" and "step type" vocabularies are conflated.
8. **Dual-schema streaming support** (legacy `content.*` events + fixtures) is now pure liability: the backend rejects every SDK that could emit the legacy schema. Retiring it would delete ~a third of the aggregator's branching and half the fixture matrix.
9. **Duplicate status→color palettes** in `ls.py:117-125` and `status.py:80-88` (neither knows `incomplete`/`budget_exceeded`); `EXIT_INTERRUPTED = 130` lives in `research.py:70` away from its siblings in `errors.py`.
10. **Polling status line freezes between ticks** (`progress.py:192-198` still uses per-tick `status.update`) while the streaming line got the self-rendering `_LiveStatusText` treatment — the elapsed display on `--no-stream`/resume stalls up to 15s. Inconsistent polish, easy unification.
11. **`JsonlStore`** grows without compaction (≥2 rows/run, rewrites on resume) and `recent()` with no limit materializes everything (used by `show`'s prefix match). Fine at CLI scale; worth a note in the file header and a v1.x compaction item.
12. **Exit-code table duplication:** `errors.py:10-18` comments, USAGE.md, and TROUBLESHOOTING.md each hand-maintain the mapping. The Phase-9 learning ("a test that greps USAGE.md vs `errors.py` would be worth adding") never happened and has already almost drifted (docs say exit 1 covers "stream error"; true only because `StreamError.exit_code = 1`).

---

## D. Tests

440 pass, disciplined structure (golden fixtures, autouse env isolation, injectable clocks, CliRunner exit-code assertions). Two structural observations:

1. **The SDK seam is still the soft spot — and it's the only place this project has ever actually broken.** All three patch releases were wire-shape drift. `test_sdk_contract.py` was the right idea, but it currently (a) constructs `ThoughtStep` without a `summary`, (b) hand-writes a thought step as a dict with `content` — a shape the SDK cannot emit, and (c) never pushes a real steps-shaped `Interaction` through the streamed-run path (`_with_fallback_outputs` → `write_artifacts`). All three confirmed bugs (A1–A3) live exactly in those blind spots. Rule worth adopting: **in contract tests, every response object must be constructed via `google.genai.interactions` model classes (or `Interaction.model_validate` on a docs-verbatim payload) — never hand-written `SimpleNamespace`/dict shapes.**
2. **Command-level fakes drift-lock the legacy shape.** Every fake `.get()` in `test_research_command.py` returns objects with an `outputs` attribute. A one-line helper that builds fakes from real SDK models would convert the whole command suite into a standing contract test.

Concrete additions worth their cost: (i) clean stream + steps-bearing fetch → fetch wins; (ii) real `ThoughtStep(summary=…)` → normalized thought → `gdr status` prints it; (iii) errored `ModelOutputStep` → `error_of` → `metadata.json["error"]`; (iv) `--max --plan` prompts; (v) `--plan --file` either rejects or sends parts; (vi) a `gdr ls` row for a `--model` follow-up shows the model name.

---

## E. Documentation staleness

| Doc | Location | Problem |
| --- | --- | --- |
| `docs/MCP.md` | "Note: CLI `--mcp` flags currently *replace* rather than merge with TOML-declared servers" | **Wrong since 0.1.3** — `_merge_config_mcps` merges, CLI wins by name (the changelog says so itself). |
| `docs/MCP.md` | "Path confinement — MCP servers cannot write outside the configured `output_dir` … checked via `Path.is_relative_to`" | Misleading (MCP servers never write locally; confinement governs gdr's derived artifact dirs), stale (`--output` is now exempt by design), and inaccurate (code uses `relative_to` + except, `security.py:139-148`). |
| `README.md` | "Path confinement: **all** artifacts land under the configured `output_dir`" | Stale — explicit `--output` is deliberately exempt since 0.1.3. |
| `docs/TROUBLESHOOTING.md` | Sample doctor output: `version=1.73.1 (required >= 1.55.0)` | Stale floor — the shipped minimum is 2.0.0; the pictured line would today be a FAIL. |
| `docs/TROUBLESHOOTING.md` | Quotes Ctrl+C output as `Task still running. Resume: gdr resume <id>` | Actual message differs (`research.py:862-868`). Docs promise text users will grep for and not find. |
| `docs/TROUBLESHOOTING.md` | Ctrl+C section: resume "writes artifacts to a sibling `_resumed_<ts>` directory" | Only on collision; the common Ctrl+C case (empty/missing dir) writes to the original directory (`resume.py:166-177`). |
| `docs/TROUBLESHOOTING.md` | Pointer to `tests/unit/test_inputs.py::TINY_PNG_B64` | Symbol is `_TINY_PNG_B64` and lives in `tests/unit/test_rendering.py:30`. |
| `docs/USAGE.md` | `gdr follow-up` section | No caveat about agent-mode follow-ups 400ing on terminal research parents (B4). |
| `docs/USAGE.md` + `ls --status` help | Status lists omit `budget_exceeded` | New 2.x terminal status is invisible in docs/help (and uncolored in tables). |
| `CHANGELOG.md` (0.1.3) | "recorded … as soon as its interaction id is known"; "transcript captures the full `steps` timeline" | Both false for streamed runs today (B1, A1). |
| `src/gdr/core/normalize.py` | Module + `error_of` docstrings cite "google-genai 1.73.1" | Stale version anchor; and the `error` rationale is now wrong (A3). |
| `docs/CODE_REVIEW.md` | Entire document | Describes v0.1.2-era bugs as current with no resolution marker; a newcomer reading `docs/` would believe `--output` still crashes and Ctrl+C still loses runs. Annotate as historical/resolved (this document supersedes it). |

---

## F. Packaging / CI

No findings of substance. Workflows pin actions to commit SHAs (correct given `id-token: write`), the release workflow's version-vs-tag guard runs before any build, the RC gate uses the narrowed `-rc.` matcher, CI covers 3.10–3.13 × Linux/macOS with frozen sync. `pyproject.toml` metadata, sdist contents, and the mypy/ruff configs are coherent. `uv.lock` resolves `google-genai 2.10.0`, consistent with the `>=2.0.0` floor.

---

## Priorities

1. **P0 — A1**: streamed-run artifact authority (`_with_fallback_outputs` guard + merge-don't-rebuild). Default-path data quality; also un-falsifies the changelog/transcript claims.
2. **P0 — A2 + A3**: thought-step and step-error extraction in `normalize.py` (small, same locus), plus contract tests built from real SDK models.
3. **P1 — B1**: record `in_progress` on the stream's `interaction.created`; carry the id through `StreamError`.
4. **P1 — B2**: `--plan` + `--file/--url` must either work or refuse.
5. **P2 — B3, B4, B5**: Max-confirm before the plan loop; validate/document agent-mode follow-up; `plan refine --max`.
6. **P3**: docs table above (MCP.md merge note and the TROUBLESHOOTING doctor sample first); consolidation batch (client-build triplication, dead code, Literal config types, palettes, legacy stream schema retirement).
