"""Tests for the response-shape adapter (core/normalize.py).

Covers the three wire shapes the adapter must bridge: SDK objects
(attributes), plain dicts, and the docs' ``steps[].content[]`` layout —
plus the SDK 1.73 quirks discovered by introspection (ThoughtContent's
``summary`` is a *list* of TextContent; tool call/result items live
alongside renderable content).
"""

from __future__ import annotations

from types import SimpleNamespace

from gdr.core.normalize import (
    error_of,
    get_field,
    normalized_outputs,
    raw_output_items,
)
from gdr.core.rendering import build_report_text, collect_sources, extract_images


class TestGetField:
    def test_reads_attributes(self) -> None:
        assert get_field(SimpleNamespace(x=1), "x") == 1

    def test_reads_dict_keys(self) -> None:
        assert get_field({"x": 2}, "x") == 2

    def test_none_and_missing_return_default(self) -> None:
        assert get_field(None, "x", "d") == "d"
        assert get_field({}, "x", "d") == "d"
        assert get_field(SimpleNamespace(), "x", "d") == "d"


class TestOutputsShape:
    def test_text_output_with_annotations(self) -> None:
        interaction = {
            "outputs": [
                {
                    "type": "text",
                    "text": "Report body.",
                    "annotations": [{"type": "url_citation", "url": "https://a", "title": "A"}],
                }
            ]
        }
        outputs = normalized_outputs(interaction)
        assert outputs == [
            {
                "type": "text",
                "text": "Report body.",
                "annotations": [{"type": "url_citation", "url": "https://a", "title": "A"}],
            }
        ]

    def test_sdk_object_outputs(self) -> None:
        interaction = SimpleNamespace(
            outputs=[SimpleNamespace(type="text", text="Body.", annotations=None)]
        )
        assert normalized_outputs(interaction) == [{"type": "text", "text": "Body."}]

    def test_thought_content_with_list_summary(self) -> None:
        # google-genai 1.73 models ThoughtContent.summary as list[TextContent].
        interaction = SimpleNamespace(
            outputs=[
                SimpleNamespace(
                    type="thought",
                    signature="opaque",
                    summary=[
                        SimpleNamespace(type="text", text="Step one."),
                        SimpleNamespace(type="text", text="Step two."),
                    ],
                ),
                SimpleNamespace(type="text", text="Final.", annotations=None),
            ]
        )
        outputs = normalized_outputs(interaction)
        assert outputs[0] == {"type": "thought", "text": "Step one.\nStep two."}
        # Thoughts never leak into the report body.
        assert build_report_text(interaction) == "Final."

    def test_thought_content_with_string_summary(self) -> None:
        interaction = {"outputs": [{"type": "thought", "summary": "Synthesizing."}]}
        assert normalized_outputs(interaction) == [{"type": "thought", "text": "Synthesizing."}]

    def test_tool_content_is_skipped_but_kept_raw(self) -> None:
        tool_item = {"type": "google_search_call", "arguments": {"query": "x"}}
        interaction = {"outputs": [tool_item, {"type": "text", "text": "Body."}]}
        assert normalized_outputs(interaction) == [{"type": "text", "text": "Body."}]
        assert tool_item in raw_output_items(interaction)

    def test_image_output_fields(self) -> None:
        interaction = {"outputs": [{"type": "image", "data": "aGk=", "mime_type": "image/png"}]}
        assert normalized_outputs(interaction) == [
            {"type": "image", "data": "aGk=", "mime_type": "image/png", "uri": None}
        ]


class TestStepsShape:
    """The public docs describe results as steps[].content[] — the adapter
    must render that shape too (empty/missing `outputs`)."""

    def _steps_interaction(self) -> dict:
        return {
            "id": "int-steps-1",
            "status": "completed",
            "outputs": [],
            "steps": [
                {
                    "type": "thought",
                    "content": [{"type": "text", "text": "Reading sources."}],
                },
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "text",
                            "text": "# Steps Report",
                            "annotations": [
                                {"type": "url_citation", "url": "https://s", "title": "S"}
                            ],
                        },
                        {
                            "type": "image",
                            # 1x1 transparent PNG
                            "data": (
                                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
                                "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
                            ),
                            "mime_type": "image/png",
                        },
                    ],
                },
            ],
        }

    def test_report_text_from_steps(self) -> None:
        assert build_report_text(self._steps_interaction()) == "# Steps Report"

    def test_text_in_thought_step_is_typed_thought(self) -> None:
        outputs = normalized_outputs(self._steps_interaction())
        assert outputs[0] == {"type": "thought", "text": "Reading sources."}

    def test_sources_from_steps(self) -> None:
        sources = collect_sources(self._steps_interaction())
        assert sources == [{"type": "url_citation", "url": "https://s", "title": "S"}]

    def test_images_from_steps(self) -> None:
        images = extract_images(self._steps_interaction())
        assert len(images) == 1
        assert images[0][1] == "image/png"

    def test_single_content_item_not_in_list(self) -> None:
        interaction = {
            "steps": [{"type": "model_output", "content": {"type": "text", "text": "One."}}]
        }
        assert build_report_text(interaction) == "One."

    def test_outputs_win_over_steps_when_present(self) -> None:
        interaction = {
            "outputs": [{"type": "text", "text": "From outputs."}],
            "steps": [
                {"type": "model_output", "content": [{"type": "text", "text": "From steps."}]}
            ],
        }
        assert build_report_text(interaction) == "From outputs."


class TestErrorOf:
    def test_missing_error_is_none(self) -> None:
        assert error_of({"status": "failed"}) is None

    def test_string_error(self) -> None:
        assert error_of({"error": "quota exceeded"}) == "quota exceeded"

    def test_structured_error(self) -> None:
        assert error_of({"error": {"code": "429", "message": "slow down"}}) == "429: slow down"

    def test_object_error(self) -> None:
        err = SimpleNamespace(code="500", message="boom")
        assert error_of(SimpleNamespace(error=err)) == "500: boom"
