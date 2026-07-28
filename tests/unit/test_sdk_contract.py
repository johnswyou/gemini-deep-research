"""Contract tests against the real installed google-genai SDK.

The rest of the suite mocks the SDK at ``google.genai.Client``, which
means it can never catch a renamed method, a renamed kwarg, or a changed
response model — exactly the class of bug behind the v0.1.1/v0.1.2
hotfixes and the 2.x Interactions API migration. These tests use the
*installed* SDK (a runtime dependency, so always present) as the source
of truth:

* every kwarg gdr sends to ``interactions.create()`` must be an accepted
  input parameter of the SDK's typed create-params, and ``get()`` /
  ``cancel()`` must accept the kwargs gdr passes, and
* the response adapter must handle *real* SDK response types (a 2.x
  ``Interaction`` whose ``steps`` timeline mixes ``ModelOutputStep``,
  ``ThoughtStep``, ``TextContent``, ``URLCitation``, ...), not just the
  SimpleNamespace stand-ins used elsewhere.

If an SDK upgrade breaks these tests, that is the signal to revisit
`core/requests.py` / `core/normalize.py` before shipping.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from pydantic import TypeAdapter

from gdr.commands.research import _with_fallback_outputs
from gdr.constants import STATUS_IN_PROGRESS, TERMINAL_STATUSES
from gdr.core.client import is_auth_error
from gdr.core.models import AgentConfig, FileSearchSpec, McpSpec, MediaPart, RunContext, TextPart
from gdr.core.normalize import error_of, normalized_outputs
from gdr.core.planning import PlanRequest, build_plan_kwargs
from gdr.core.rendering import (
    _usage_dict,
    build_report_text,
    build_transcript,
    collect_sources,
    extract_images,
)
from gdr.core.requests import build_create_kwargs
from gdr.core.security import SecurityPolicy

genai_interactions = pytest.importorskip(
    "google.genai.interactions", reason="google-genai not installed"
)
from google import genai  # noqa: E402 — guarded by the importorskip above

# 1x1 transparent PNG
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _interactions_resource_params(method: str) -> set[str]:
    """Named parameter names of the real SDK interactions resource method."""
    client = genai.Client(api_key="AIza-contract-test-key")
    resource = client.interactions
    fn = getattr(resource, method)
    return set(inspect.signature(fn).parameters)


def _create_input_params() -> set[str]:
    """Accepted ``interactions.create()`` input keys per the SDK's typed params.

    Under the 2.x SDK, ``create()`` takes ``**body`` (a VAR_KEYWORD
    catch-all), so its signature no longer names the individual inputs.
    The authoritative allowlist is the typed create-params TypedDicts —
    a renamed input param there is exactly what we want this test to catch.
    """
    params: set[str] = set()
    for name in ("CreateAgentInteractionParam", "CreateModelInteractionParam"):
        td = getattr(genai_interactions, name)
        params |= set(getattr(td, "__annotations__", {}))
        params |= set(getattr(td, "__required_keys__", set()))
        params |= set(getattr(td, "__optional_keys__", set()))
    return params


def _maxed_out_ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        query="Contract test query",
        agent="deep-research-preview-04-2026",
        builtin_tools=("google_search", "url_context", "code_execution"),
        mcp_servers=(
            McpSpec(
                name="deploys",
                url="https://mcp.example.com",
                headers={"Authorization": "Bearer abc"},
                allowed_tools=("list_deploys",),
            ),
        ),
        file_search=FileSearchSpec(file_search_store_names=("fileSearchStores/kb",)),
        input_parts=(TextPart(text="Extra context"),),
        output_dir=tmp_path,
        stream=True,
        previous_interaction_id="int-parent-1",
        agent_config=AgentConfig(),
    )


class TestCreateKwargsContract:
    def test_agent_run_kwargs_are_all_accepted_by_the_sdk(self, tmp_path: Path) -> None:
        accepted = _create_input_params()
        kwargs, _ = build_create_kwargs(
            _maxed_out_ctx(tmp_path), SecurityPolicy(output_root=tmp_path)
        )
        unknown = set(kwargs) - accepted
        assert not unknown, f"gdr sends create kwargs the SDK does not accept: {unknown}"

    def test_model_run_kwargs_are_all_accepted_by_the_sdk(self, tmp_path: Path) -> None:
        accepted = _create_input_params()
        ctx = RunContext(
            query="Elaborate on point 2",
            agent="gemini-3.1-pro-preview",
            model="gemini-3.1-pro-preview",
            output_dir=tmp_path,
            stream=False,
            previous_interaction_id="int-parent-1",
        )
        kwargs, _ = build_create_kwargs(ctx, SecurityPolicy(output_root=tmp_path))
        unknown = set(kwargs) - accepted
        assert not unknown, f"gdr sends create kwargs the SDK does not accept: {unknown}"
        assert "agent" not in kwargs
        assert "agent_config" not in kwargs
        # Plain models reject background interactions under the 2.x API.
        assert kwargs["background"] is False

    def test_get_supports_the_kwargs_gdr_uses(self) -> None:
        params = _interactions_resource_params("get")
        # Polling and status: get(id=...). Stream reconnect:
        # get(id=..., stream=True, last_event_id=...).
        assert {"id", "stream", "last_event_id"} <= params

    def test_cancel_exists_and_takes_id(self) -> None:
        params = _interactions_resource_params("cancel")
        assert "id" in params

    def test_agent_config_shape_matches_sdk_model(self) -> None:
        sdk_fields = set(genai_interactions.DeepResearchAgentConfig.model_fields)
        gdr_fields = set(AgentConfig().model_dump())
        unknown = gdr_fields - sdk_fields
        assert not unknown, f"AgentConfig sends fields the SDK doesn't know: {unknown}"


class TestCreatePayloadContract:
    """Kwarg *names* are not the wire contract — the *values* are.

    The SDK parses every polymorphic field (tools, input content,
    agent_config) through a **lenient** open union: a payload that fails
    its concrete model is not an error, it is silently rewritten to an
    ``Unknown*`` placeholder that still gets sent. So a malformed tool
    can leave `create()` happy, leave `--dry-run` looking correct, and
    still never reach the API — which is exactly how the bare-string
    ``allowed_tools`` bug shipped. These tests validate what gdr builds
    against the SDK's own unions and fail on any such downgrade.
    """

    @staticmethod
    def _assert_no_downgrades(kwargs: dict[str, Any]) -> None:
        im = genai_interactions

        for tool in kwargs.get("tools", []):
            parsed = TypeAdapter(im.Tool).validate_python(tool)
            assert not isinstance(parsed, im.UnknownTool), (
                f"the SDK cannot parse this tool and would send an UNKNOWN "
                f"placeholder instead: {tool}"
            )

        payload = kwargs["input"]
        for part in payload if isinstance(payload, list) else []:
            parsed_part = TypeAdapter(im.Content).validate_python(part)
            assert not isinstance(parsed_part, im.UnknownContent), (
                f"the SDK cannot parse this input part: {part}"
            )

        if "agent_config" in kwargs:
            parsed_config = TypeAdapter(im.InteractionAgentConfig).validate_python(
                kwargs["agent_config"]
            )
            assert not isinstance(parsed_config, im.UnknownInteractionAgentConfig), (
                f"the SDK cannot parse this agent_config: {kwargs['agent_config']}"
            )

    def test_maxed_out_agent_request_survives_sdk_parsing(self, tmp_path: Path) -> None:
        kwargs, _ = build_create_kwargs(
            _maxed_out_ctx(tmp_path), SecurityPolicy(output_root=tmp_path)
        )
        self._assert_no_downgrades(kwargs)

    def test_multimodal_request_survives_sdk_parsing(self, tmp_path: Path) -> None:
        ctx = RunContext(
            query="What is in this document?",
            agent="deep-research-preview-04-2026",
            builtin_tools=("google_search",),
            input_parts=(
                MediaPart(type="document", data="Zm9v", mime_type="application/pdf"),
                MediaPart(type="image", data=_TINY_PNG_B64, mime_type="image/png"),
                TextPart(text="Additional URLs to consider:\nhttps://example.com"),
            ),
            output_dir=tmp_path,
        )
        kwargs, _ = build_create_kwargs(ctx, SecurityPolicy(output_root=tmp_path))
        self._assert_no_downgrades(kwargs)

    def test_plan_request_survives_sdk_parsing(self) -> None:
        kwargs = build_plan_kwargs(
            PlanRequest(
                input_text="Do some research on Google TPUs.",
                agent="deep-research-preview-04-2026",
                input_parts=(MediaPart(type="document", data="Zm9v", mime_type="application/pdf"),),
            )
        )
        self._assert_no_downgrades(kwargs)


class TestErrorClassificationContract:
    """Pin the shape of the exception the SDK raises for a rejected key.

    `client.interactions` routes every call through the SDK's compat
    error layer, whose exceptions carry the HTTP status as
    ``status_code`` — NOT ``code``, which is what an earlier version of
    the classifier read (and so never matched: a bad key exited 5 as a
    "network error" instead of the documented 4).

    The private import is deliberate: this is the only place that shape
    exists, and an import failure here is exactly the signal to re-check
    `gdr.core.client.is_auth_error` against the new SDK layout.
    """

    @staticmethod
    def _sdk_error(status: int) -> BaseException:
        # Imported here, not at module scope, so a future SDK reshuffle
        # fails THIS test (the signal to re-check the classifier) instead
        # of collapsing the whole contract module at import time.
        from google.genai._gaos.lib.compat_errors import (  # noqa: PLC0415
            APIError as CompatAPIError,
        )

        return CompatAPIError.generate(
            status_code=status,
            body={"error": {"code": status, "message": "API key not valid.", "status": "PERM"}},
            message="API key not valid.",
            response=httpx.Response(
                status,
                request=httpx.Request("POST", "https://generativelanguage.googleapis.com/"),
            ),
        )

    @pytest.mark.parametrize("status", [401, 403])
    def test_real_auth_errors_are_classified_as_auth(self, status: int) -> None:
        assert is_auth_error(self._sdk_error(status)) is True

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_real_non_auth_errors_are_not_classified_as_auth(self, status: int) -> None:
        assert is_auth_error(self._sdk_error(status)) is False

    def test_status_is_exposed_as_status_code_not_code(self) -> None:
        # The regression this whole class exists for. If a future SDK adds
        # `.code` back, the classifier reads either — but the stubs used by
        # the command tests must keep mirroring what the SDK really carries.
        exc = self._sdk_error(401)
        assert exc.status_code == 401  # type: ignore[attr-defined]
        assert getattr(exc, "code", None) is None


class TestResponseAdapterAgainstRealTypes:
    """Feed real 2.x SDK response models through the adapter and renderer."""

    def _real_interaction(self) -> Any:
        im = genai_interactions
        # 2.x: outputs live in a ``steps`` timeline of typed step objects.
        return im.Interaction(
            id="int-real-1",
            created="2026-07-07T00:00:00Z",
            updated="2026-07-07T00:00:00Z",
            status="completed",
            agent="deep-research-preview-04-2026",
            steps=[
                im.ThoughtStep(type="thought", signature="sig-abc"),
                im.ModelOutputStep(
                    type="model_output",
                    content=[
                        im.TextContent(
                            type="text",
                            text="# Real Report\n\nFindings.",
                            annotations=[
                                im.URLCitation(
                                    type="url_citation",
                                    url="https://example.com/a",
                                    title="Example A",
                                )
                            ],
                        ),
                        im.ImageContent(type="image", data=_TINY_PNG_B64, mime_type="image/png"),
                    ],
                ),
            ],
            usage=im.Usage(total_tokens=1000, total_input_tokens=600, total_output_tokens=400),
        )

    def test_report_text_from_real_interaction(self) -> None:
        assert build_report_text(self._real_interaction()) == "# Real Report\n\nFindings."

    def test_sources_from_real_annotations(self) -> None:
        sources = collect_sources(self._real_interaction())
        assert sources == [
            {"type": "url_citation", "url": "https://example.com/a", "title": "Example A"}
        ]

    def test_images_decode_from_real_content(self) -> None:
        images = extract_images(self._real_interaction())
        assert len(images) == 1
        assert images[0][1] == "image/png"

    def test_usage_spellings_from_real_model(self) -> None:
        usage = _usage_dict(self._real_interaction())
        assert usage == {"total_tokens": 1000, "input_tokens": 600, "output_tokens": 400}

    def test_transcript_preserves_full_step_timeline(self, tmp_path: Path) -> None:
        transcript = build_transcript(
            self._real_interaction(), policy=SecurityPolicy(output_root=tmp_path)
        )
        step_types = [s.get("type") for s in transcript["outputs"]]
        assert "model_output" in step_types
        assert "thought" in step_types

    def test_statuses_gdr_knows_cover_the_sdk_literal(self) -> None:
        # 2.x wraps the status Literal in Union[Literal[...], UnrecognizedStr];
        # dig the string literals out of whatever shape the annotation takes.
        annotation = genai_interactions.Interaction.model_fields["status"].annotation
        sdk_statuses: set[str] = set()
        for arg in get_args(annotation):
            literal_args = get_args(arg)
            if literal_args:
                sdk_statuses |= {a for a in literal_args if isinstance(a, str)}
            elif isinstance(arg, str):
                sdk_statuses.add(arg)
        known = TERMINAL_STATUSES | {STATUS_IN_PROGRESS, "requires_action"}
        unknown = sdk_statuses - known
        assert not unknown, (
            f"The SDK models statuses gdr has never considered: {unknown}. "
            f"Decide whether they are terminal and update constants.py."
        )


class TestTimelineDoesNotLeakIntoReport:
    """Only ``model_output`` steps form the report body under the 2.x timeline.

    A 2.x ``get(id)`` returns the full timeline — user input, thoughts, and
    tool call/result steps precede the model output. None of those may bleed
    into ``report.md``.
    """

    def _timeline(self) -> dict[str, Any]:
        return {
            "id": "int-x",
            "status": "completed",
            "steps": [
                {"type": "user_input", "content": [{"type": "text", "text": "the user query"}]},
                {"type": "thought", "content": [{"type": "text", "text": "internal reasoning"}]},
                {"type": "google_search_call", "arguments": "{}"},
                {"type": "model_output", "content": [{"type": "text", "text": "THE REPORT"}]},
            ],
        }

    def test_only_model_output_is_report_body(self) -> None:
        assert build_report_text(self._timeline()) == "THE REPORT"

    def test_user_input_and_tool_text_are_excluded(self) -> None:
        outputs = normalized_outputs(self._timeline())
        body = [o["text"] for o in outputs if o["type"] == "text"]
        assert body == ["THE REPORT"]

    def test_thought_step_text_is_typed_thought(self) -> None:
        outputs = normalized_outputs(self._timeline())
        assert {"type": "thought", "text": "internal reasoning"} in outputs


class TestRealThoughtAndErrorSteps:
    """The real 2.x step models: ``ThoughtStep`` carries ``summary`` (no
    ``content``), and failure details live on ``ModelOutputStep.error``
    (the 2.x ``Interaction`` has no top-level ``error`` field)."""

    def test_thought_step_summary_surfaces_as_thought(self) -> None:
        im = genai_interactions
        interaction = im.Interaction(
            id="int-thought-1",
            created="2026-07-07T00:00:00Z",
            updated="2026-07-07T00:01:00Z",
            status="in_progress",
            steps=[
                im.ThoughtStep(
                    type="thought",
                    summary=[im.TextContent(type="text", text="Reading sources on X")],
                )
            ],
        )
        outputs = normalized_outputs(interaction)
        assert {"type": "thought", "text": "Reading sources on X"} in outputs

    def test_step_level_error_reaches_error_of(self) -> None:
        im = genai_interactions
        interaction = im.Interaction.model_validate(
            {
                "id": "int-fail-1",
                "created": "2026-07-07T00:00:00Z",
                "updated": "2026-07-07T00:01:00Z",
                "status": "failed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [],
                        "error": {"code": "quota_exceeded", "message": "You ran out"},
                    }
                ],
            }
        )
        assert error_of(interaction) == "quota_exceeded: You ran out"


class TestStreamedFallbackPrefersAuthoritativeFetch:
    """`_with_fallback_outputs` must keep a fetch that has report content.

    The 2.x ``Interaction`` has no ``outputs`` field, so the guard cannot
    key on it: a terminal fetch whose ``steps`` carry the report body is
    authoritative, and the streamed buffer is only the fallback for a
    fetch with no renderable report content (the v0.1.2 empty-fetch case).
    """

    _STREAM_BUFFER = ({"type": "text", "text": "STREAM BUFFER TEXT"},)

    def _fetch_with_report(self) -> Any:
        im = genai_interactions
        return im.Interaction.model_validate(
            {
                "id": "int-stream-1",
                "created": "2026-07-07T00:00:00Z",
                "updated": "2026-07-07T00:10:00Z",
                "status": "completed",
                "steps": [
                    {
                        "type": "google_search_call",
                        "id": "call-1",
                        "arguments": {"queries": ["example"]},
                    },
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "text",
                                "text": "AUTHORITATIVE FETCH BODY",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com/a",
                                        "title": "Example A",
                                    },
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com/b",
                                        "title": "Example B",
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
        )

    def test_fetch_with_report_content_wins_over_stream_buffer(self) -> None:
        merged = _with_fallback_outputs(self._fetch_with_report(), self._STREAM_BUFFER)
        assert build_report_text(merged) == "AUTHORITATIVE FETCH BODY"
        assert len(collect_sources(merged)) == 2

    def test_transcript_keeps_timeline_on_cleanly_streamed_runs(self, tmp_path: Path) -> None:
        merged = _with_fallback_outputs(self._fetch_with_report(), self._STREAM_BUFFER)
        transcript = build_transcript(merged, policy=SecurityPolicy(output_root=tmp_path))
        step_types = [s.get("type") for s in transcript["outputs"]]
        assert "google_search_call" in step_types

    def test_fallback_engages_when_fetch_has_no_report_content(self) -> None:
        im = genai_interactions
        empty_fetch = im.Interaction(
            id="int-stream-2",
            created="2026-07-07T00:00:00Z",
            updated="2026-07-07T00:10:00Z",
            status="completed",
            steps=[],
        )
        merged = _with_fallback_outputs(empty_fetch, self._STREAM_BUFFER)
        assert build_report_text(merged) == "STREAM BUFFER TEXT"

    def test_thought_only_fetch_does_not_shadow_streamed_body(self, tmp_path: Path) -> None:
        im = genai_interactions
        thought_only = im.Interaction(
            id="int-stream-3",
            created="2026-07-07T00:00:00Z",
            updated="2026-07-07T00:10:00Z",
            status="completed",
            steps=[
                im.ThoughtStep(
                    type="thought",
                    summary=[im.TextContent(type="text", text="pondering")],
                )
            ],
        )
        merged = _with_fallback_outputs(thought_only, self._STREAM_BUFFER)
        assert build_report_text(merged) == "STREAM BUFFER TEXT"
        # Merge, don't rebuild: the fetch's step timeline must survive into
        # the transcript even when the streamed body stands in for the report.
        transcript = build_transcript(merged, policy=SecurityPolicy(output_root=tmp_path))
        assert "thought" in [s.get("type") for s in transcript["outputs"]]
