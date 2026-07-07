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

import pytest

from gdr.constants import STATUS_IN_PROGRESS, TERMINAL_STATUSES
from gdr.core.models import AgentConfig, FileSearchSpec, McpSpec, RunContext, TextPart
from gdr.core.normalize import normalized_outputs
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
