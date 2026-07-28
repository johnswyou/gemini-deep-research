# Deep Code Review — `gdr` v0.1.4

> **Remediation status (2026-07-28):** findings **A1–A4 are fixed** and
> shipped in **v0.2.0**, each verified against the real SDK: the MCP
> request now serializes intact with no `UNKNOWN` downgrade, a real
> `AuthenticationError(401)` exits 4 with the key hint, and
> `gdr follow-up --max` prompts (declining creates nothing). Auth
> classification was extended beyond the filed site to
> `status`/`resume`/`cancel`/planning and the poll loop.
>
> **Section B is also fully remediated** (see `CHANGELOG.md` →
> Unreleased): dry-run redaction behind `--reveal`, transcript
> inline-data stripping, the step-scoped stream buffer, the
> `_handle_start` guard, `JsonlStore` compaction with a retention cap,
> and the `RunSpec` refactor that took `execute_research` from 21
> keyword arguments to 3. Nothing in this review is outstanding.

**Date:** 2026-07-28 · **Tree reviewed:** `570250c` (v0.1.4, clean working tree) · **Scope:** full repo — request assembly, response adapter, streaming, commands, security, tests, docs, packaging, CI.

**References:** [Gemini Deep Research API docs](https://ai.google.dev/gemini-api/docs/deep-research) and [function-calling / MCP docs](https://ai.google.dev/gemini-api/docs/function-calling) (fetched during this review) · installed `google-genai 2.10.0`, introspected directly. Every schema claim below was verified by driving the shipped code with real SDK types or by round-tripping gdr's own request through the SDK's request model — none is assumed.

**Supersedes:** nothing. `docs/CODE_REVIEW_v0.1.3.md` remains the record of the previous cycle; its findings were remediated and are not repeated here.

---

## Verdict

The v0.1.3 remediation held. Spot-checks against real SDK objects that used to fail now pass: `normalize.py` extracts the body only from `model_output` steps and flattens `ThoughtStep.summary`; `_with_fallback_outputs` merges rather than rebuilds; `error_of` scans step-level errors; the stream aggregator handles real `StepStart`/`StepDelta`/`InteractionCompletedEvent` objects correctly (verified by feeding it constructed SDK event instances — text, thought summary, image, annotation, usage all land in the right buckets). Gates on this tree: **ruff + format clean, mypy strict clean, 464 passed / 1 skipped (live-gated), 92% coverage**.

Three defects survive, and all three share one root cause the previous review already named: **a test fixture hand-writes the shape it claims to verify.** The contract suite checks that gdr's create-kwargs *keys* are accepted by the SDK, but never checks that the *values* survive SDK serialization — so a tool payload can be silently replaced on the wire and stay green. An auth-error regression test invents an exception class with the attribute the code reads, so the code reads the wrong attribute against the real SDK. One behavioral gate infers consent from a field that means something else.

---

## A. Confirmed defects

### A1. `allowed_tools` silently destroys the entire MCP server declaration (HIGH)

`src/gdr/core/requests.py:45-46` serializes the config-declared allowlist as a list of plain strings:

```python
if mcp.allowed_tools is not None:
    payload["allowed_tools"] = list(mcp.allowed_tools)
```

The SDK models `MCPServer.allowed_tools` as `Optional[List[AllowedTools]]`, where `AllowedTools` is an object (`{mode, tools}`) — matching the docs, which show `allowed_tools` as `{"mode": ..., "tools": [...]}`. A list of bare strings fails `MCPServer` validation, and the lenient open-union parser falls back to `UnknownTool`, which re-serializes as a placeholder. Round-tripping gdr's own kwargs through `CreateInteractionRequest` + `serialize_request_body`:

| `allowed_tools` value | serialized `tools[]` entry |
| --- | --- |
| `["a", "b"]` — **what gdr sends** | `{"type":"UNKNOWN","raw":{…},"is_unknown":true}` |
| `[{"tools": ["a","b"]}]` | `{"type":"mcp_server","name":…,"url":…,"allowed_tools":[{"tools":["a","b"]}]}` |
| `[{"mode":"validated","tools":["a","b"]}]` | correct `mcp_server` entry |

So the whole `mcp_server` tool — name, URL, auth headers, allowlist — is replaced by an `UNKNOWN` placeholder. The run proceeds without the user's MCP server (or 400s), with no warning. `--dry-run` is no help: it prints gdr's pre-SDK dict, which looks perfectly correct.

Reachable through documented config (`docs/MCP.md:86` shows `allowed_tools = ["search_filings", "get_fundamentals"]`, and `docs/MCP.md:96` promises "only the named MCP tools become callable"), plumbed via `config.py:84` → `research.py:179` → `models.py:86`.

**Fix:** emit the object form (`[{"mode": …, "tools": [...]}]`), or build the payload from `google.genai.interactions.MCPServer` directly so the SDK owns the shape. Then add the contract test in A4.

### A2. Auth failures exit 5 instead of the documented 4 (MEDIUM)

`src/gdr/commands/research.py:604`:

```python
if getattr(exc, "code", None) in (401, 403):
```

`client.interactions` is `GeminiNextGenInteractions`, whose calls go through `wrap_sdk_call` → `wrap_sdk_error` → `google.genai._gaos.lib.compat_errors.APIError.generate(...)`. Those exception classes expose **`status_code`**, not `code`:

```
raised type: AuthenticationError | .code: MISSING | .status_code: 401
```

Driving the real command with a genuine `AuthenticationError(401)`:

```
EXIT: 5   (documented: 4 = auth, 5 = network)
Error: Failed to start research: API key not valid. Please pass a valid API key.
```

So an invalid or revoked key exits 5 with a generic network message instead of exit 4 with the "check your API key / run `gdr doctor`" hint — contradicting the exit-code tables in `docs/TROUBLESHOOTING.md:45` and `docs/USAGE.md:427`, and the CHANGELOG's 0.1.4 claim.

The guarding test hand-writes the shape it verifies (`tests/unit/test_research_regressions.py:587-599`):

```python
class FakeAuthError(Exception):
    code = 401
```

**Fix:** read `status_code` first, fall back to `code`; assert against a real `compat_errors.APIError.generate(status_code=401, …)` instance. Note the same classification is absent entirely in `status`/`resume`/`cancel`/`plan`, which wrap every failure as `NetworkError`.

### A3. `gdr follow-up --max` never asks for cost confirmation (MEDIUM)

The Max gate in `src/gdr/commands/research.py:508-516` skips whenever `previous_interaction_id is None` is false:

```python
if (
    use_max
    and ctx_for_kwargs.confirm_max
    and not no_confirm
    and previous_interaction_id is None
    and not _confirm_max(console)
):
```

That condition encodes "the user already consented by approving a plan". But `gdr follow-up` always sets `previous_interaction_id` (`commands/follow_up.py:127`), so `--max` on a follow-up starts a fresh ~$3–7 Max run with no prompt. Verified with `confirm_max = true` in config:

```
gdr follow-up int_parent elaborate --max --no-stream
exit: 0 · confirm prompted: False
```

`docs/USAGE.md:465` documents `confirm_max` as "Prompt before running Max agents", and the follow-up flag table (`docs/USAGE.md:274`) advertises `--no-confirm`, which implies a prompt exists to skip.

**Fix:** pass consent explicitly — e.g. a `skip_max_confirm: bool` argument set by `plan approve` and `research --plan` — instead of inferring it from `previous_interaction_id`.

### A4. The SDK contract suite checks kwarg names, never payload validity (MEDIUM — root cause of A1)

`tests/unit/test_sdk_contract.py:108,122`:

```python
unknown = set(kwargs) - accepted
assert not unknown, …
```

`tools` is an accepted key, so any tool payload passes — including the one from A1. Worse, the "maxed out" fixture at `tests/unit/test_sdk_contract.py:90` carries `allowed_tools=("list_deploys",)`, enshrining the broken shape as the reference request.

**Fix:** add a test that pushes `build_create_kwargs(...)` through the SDK's own request pipeline and asserts the serialized body is clean:

```python
body = google_genai._normalize_create_body(kwargs)
req = utils.unmarshal({"body": body}, models.CreateInteractionRequest)
content = utils.serialize_request_body(
    req.body, False, False, "json", models.CreateInteractionRequestBody
).content
assert "is_unknown" not in content
```

This catches every future silent-downgrade of a tool, input part, or agent_config value — the whole class of bug, not just A1.

---

## B. Lower-priority observations

> All six are fixed; the findings are kept verbatim below as the record of what was wrong. See `CHANGELOG.md` → Unreleased for what each became.

* **`--dry-run` prints expanded secrets.** `research.py:996-998` dumps the raw kwargs, so `headers.Authorization = "Bearer env:MCP_TOKEN"` renders as `Bearer super-secret-token-value` in terminal scrollback and CI logs. `docs/MCP.md:164` calls this intentional — but under the heading "No secret exfiltration via `--dry-run`", which asserts the opposite of what the section says. Either rename the heading, or redact by default and add `--reveal` (mirroring the `gdr config get` treatment).
* **Transcript bloat.** `rendering.py:344-353` dumps whole `steps` with `model_dump(exclude_none=True)`, so image `data` is written base64 into `transcript.json` *and* decoded into `images/`. A visualization-heavy Max run duplicates megabytes.
* **Stream text is index-agnostic.** `streaming.py:347-353` appends every `text` delta to the shared report buffer regardless of which step index produced it. Deep Research only emits text deltas on `model_output` today, so this is latent, but it is the same leak `normalize._BODY_STEP_TYPES` was tightened to prevent on the fetch path.
* **`_handle_start` can null a known id.** `streaming.py:302` assigns unconditionally; an `interaction.created` replay without an id would clear it. Guard like `_handle_status_update` does.
* **`execute_research` takes 18 keyword parameters.** `PLR0913` is globally disabled so lint won't flag it. A `RunSpec`-style value object would keep the plan/follow-up/research call sites honest — and would have made A3 hard to write.
* **`JsonlStore` still grows unbounded** and is re-opened (full reload) on each of the two `_record_run` calls per run. Already tracked for v1.x.

---

## C. What was verified as sound

Confirmed by execution, not reading:

* **Request shape.** A "maxed out" run (3 builtin tools + file_search + MCP without `allowed_tools` + document part + URL text part + agent_config) round-trips through the SDK request model with zero downgrades; input parts are correctly rewrapped as `[{"type":"user_input","content":[…]}]`.
* **Response adapter.** A real `Interaction` with a mixed `steps[]` timeline (`user_input` / `thought` / `google_search_call` / `model_output` + `url_citation` + image) renders the correct body, one deduped source, the image, and correct `usage` — with no user-input or tool text leaking into `report.md`.
* **Stream aggregator.** Real `InteractionCreatedEvent` / `StepStart` / `StepDelta(TextDelta | ThoughtSummaryDelta | ImageDelta | TextAnnotationDelta)` / `StepStop` / `InteractionCompletedEvent` objects produce the right emissions, snapshot, `last_event_id`, and token total.
* **SDK method contracts.** `get(id, stream=, last_event_id=, include_input=)` and `cancel(id)` accept exactly what gdr passes; `create()` rejects `model`+`agent` together, which matches gdr's two-branch assembly.
* **Collaborative planning.** The docs poll a plan interaction to `completed` (not `requires_action`), so `poll_until_complete`'s terminal set is correct for the plan flow.
* **Forward compatibility.** `UnrecognizedStr` subclasses `str`, so an unknown future status flows through the status comparisons without a crash.
* **Interrupts.** Ctrl+C during `create()` exits 130 via Click, matching the documented contract.
* **Packaging / CI.** Actions pinned to SHAs, OIDC trusted publishing, version-vs-tag guard, RC gating, `tmp/` and `dist/` untracked, no secrets in the tree.

---

## Priorities

1. **A1** — fix the `allowed_tools` wire shape (a documented feature is silently a no-op).
2. **A4** — add the serialization contract test; it is what makes A1 non-recurring.
3. **A2** — read `status_code`; retest with the real SDK exception class.
4. **A3** — make Max consent explicit rather than inferred.
5. **B** — dry-run redaction (or the heading fix), then the smaller items.
