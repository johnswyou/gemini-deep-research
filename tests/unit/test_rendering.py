"""Tests for `gdr.core.rendering`."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gdr.constants import AGENT_FAST, DEFAULT_TOOLS
from gdr.core.models import RunContext
from gdr.core.rendering import (
    build_metadata,
    build_report_text,
    build_transcript,
    collect_sources,
    extract_images,
    render_report_markdown,
    write_artifacts,
    write_images,
)
from gdr.core.security import SecurityPolicy
from gdr.errors import ConfigError

_UTC = timezone.utc

# A valid 1x1 transparent PNG, base64. Small enough to inline, real enough
# for filesystem round-trip tests.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_TINY_PNG_BYTES = base64.b64decode(_TINY_PNG_B64)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _text_output(text: str, annotations: list[dict[str, Any]] | None = None) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text, annotations=annotations or [])


def _fake_interaction(
    *,
    id_: str = "int-abc-123",
    status: str = "completed",
    outputs: list[Any] | None = None,
    usage: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(id=id_, status=status, outputs=outputs or [], usage=usage)


def _ctx(output_dir: Path) -> RunContext:
    return RunContext(
        query="Research TPUs",
        agent=AGENT_FAST,
        builtin_tools=DEFAULT_TOOLS,
        output_dir=output_dir,
    )


# ---------------------------------------------------------------------------
# build_report_text
# ---------------------------------------------------------------------------


class TestBuildReportText:
    def test_concatenates_text_outputs(self) -> None:
        interaction = _fake_interaction(
            outputs=[
                _text_output("First paragraph."),
                _text_output("Second paragraph."),
            ]
        )
        assert build_report_text(interaction) == "First paragraph.\n\nSecond paragraph."

    def test_skips_empty_and_whitespace_text(self) -> None:
        interaction = _fake_interaction(
            outputs=[_text_output(""), _text_output("   \n  "), _text_output("Real text.")]
        )
        assert build_report_text(interaction) == "Real text."

    def test_ignores_non_text_outputs(self) -> None:
        interaction = _fake_interaction(
            outputs=[
                SimpleNamespace(type="thought", summary="thinking", signature="abc"),
                _text_output("Only this."),
                SimpleNamespace(type="function_call", id="c1", name="fn", arguments={}),
            ]
        )
        assert build_report_text(interaction) == "Only this."

    def test_works_with_dict_shaped_outputs(self) -> None:
        interaction = {"outputs": [{"type": "text", "text": "Dict works."}]}
        assert build_report_text(interaction) == "Dict works."


# ---------------------------------------------------------------------------
# collect_sources
# ---------------------------------------------------------------------------


class TestCollectSources:
    def test_collects_url_citations(self) -> None:
        interaction = _fake_interaction(
            outputs=[
                _text_output(
                    "Intro.",
                    annotations=[
                        {"type": "url_citation", "url": "https://a", "title": "A"},
                        {"type": "url_citation", "url": "https://b", "title": "B"},
                    ],
                )
            ]
        )
        sources = collect_sources(interaction)
        assert [s["url"] for s in sources] == ["https://a", "https://b"]

    def test_dedupes_by_url(self) -> None:
        interaction = _fake_interaction(
            outputs=[
                _text_output("X", annotations=[{"type": "url_citation", "url": "https://a"}]),
                _text_output("Y", annotations=[{"type": "url_citation", "url": "https://a"}]),
            ]
        )
        assert len(collect_sources(interaction)) == 1

    def test_preserves_first_occurrence_order(self) -> None:
        interaction = _fake_interaction(
            outputs=[
                _text_output(
                    "",
                    annotations=[
                        {"type": "url_citation", "url": "https://b"},
                        {"type": "url_citation", "url": "https://a"},
                    ],
                )
            ]
        )
        urls = [s["url"] for s in collect_sources(interaction)]
        assert urls == ["https://b", "https://a"]

    def test_collects_file_citations(self) -> None:
        interaction = _fake_interaction(
            outputs=[
                _text_output(
                    "Ref.",
                    annotations=[
                        {
                            "type": "file_citation",
                            "document_uri": "file:///x.pdf",
                            "file_name": "x.pdf",
                        }
                    ],
                )
            ]
        )
        sources = collect_sources(interaction)
        assert sources[0]["type"] == "file_citation"
        assert sources[0]["file_name"] == "x.pdf"

    def test_ignores_unknown_annotation_types(self) -> None:
        interaction = _fake_interaction(
            outputs=[_text_output("X", annotations=[{"type": 42}])]  # wrong kind type
        )
        assert collect_sources(interaction) == []


# ---------------------------------------------------------------------------
# render_report_markdown
# ---------------------------------------------------------------------------


class TestRenderReportMarkdown:
    def test_contains_title_agent_and_body(self) -> None:
        interaction = _fake_interaction(outputs=[_text_output("Body text.")])
        md = render_report_markdown(interaction, query="What is a TPU?", agent=AGENT_FAST)
        assert md.startswith("# What is a TPU?")
        assert AGENT_FAST in md
        assert "Body text." in md
        assert md.endswith("\n")

    def test_emits_sources_section_when_available(self) -> None:
        interaction = _fake_interaction(
            outputs=[
                _text_output(
                    "Body",
                    annotations=[{"type": "url_citation", "url": "https://a", "title": "A"}],
                )
            ]
        )
        md = render_report_markdown(interaction, query="Q", agent=AGENT_FAST)
        assert "## Sources" in md
        assert "[A](https://a)" in md

    def test_fallback_for_empty_body(self) -> None:
        interaction = _fake_interaction(outputs=[])
        md = render_report_markdown(interaction, query="Q", agent=AGENT_FAST)
        assert "No final report text" in md


# ---------------------------------------------------------------------------
# build_metadata + build_transcript
# ---------------------------------------------------------------------------


class TestMetadataAndTranscript:
    def test_metadata_contains_timings_and_tools(self, tmp_path: Path) -> None:
        started = datetime(2026, 4, 22, 14, 30, 0, tzinfo=_UTC)
        finished = datetime(2026, 4, 22, 14, 35, 0, tzinfo=_UTC)
        interaction = _fake_interaction(
            usage=SimpleNamespace(total_tokens=12345, input_tokens=10000, output_tokens=2345)
        )
        meta = build_metadata(
            interaction,
            ctx=_ctx(tmp_path),
            started_at=started,
            finished_at=finished,
            output_dir=tmp_path,
        )
        assert meta["duration_seconds"] == 300
        assert meta["status"] == "completed"
        assert meta["usage"]["total_tokens"] == 12345
        assert meta["agent"] == AGENT_FAST
        assert meta["tools"] == list(DEFAULT_TOOLS)

    def test_transcript_redacts_sensitive_headers(self, tmp_path: Path) -> None:
        interaction = _fake_interaction(
            outputs=[
                {
                    "type": "mcp_server_call",
                    "headers": {"Authorization": "Bearer secret", "Accept": "*/*"},
                }
            ]
        )
        policy = SecurityPolicy(output_root=tmp_path)
        transcript = build_transcript(interaction, policy=policy)
        header_entry = transcript["outputs"][0]["headers"]
        assert header_entry["Authorization"] == "[REDACTED]"
        assert header_entry["Accept"] == "*/*"


# ---------------------------------------------------------------------------
# write_artifacts (end-to-end)
# ---------------------------------------------------------------------------


class TestWriteArtifacts:
    def test_writes_all_four_files(self, tmp_path: Path) -> None:
        interaction = _fake_interaction(
            outputs=[
                _text_output(
                    "A report body.",
                    annotations=[{"type": "url_citation", "url": "https://a", "title": "A"}],
                )
            ]
        )
        policy = SecurityPolicy(output_root=tmp_path)
        started = datetime(2026, 4, 22, 14, 30, tzinfo=_UTC)
        finished = datetime(2026, 4, 22, 14, 35, tzinfo=_UTC)
        output_dir = tmp_path / "run1"
        paths = write_artifacts(
            interaction,
            ctx=_ctx(output_dir),
            output_dir=output_dir,
            policy=policy,
            started_at=started,
            finished_at=finished,
        )

        assert paths["report"].is_file()
        assert paths["sources"].is_file()
        assert paths["metadata"].is_file()
        assert paths["transcript"].is_file()

        report = paths["report"].read_text(encoding="utf-8")
        assert "A report body." in report

        sources = json.loads(paths["sources"].read_text(encoding="utf-8"))
        assert sources["sources"][0]["url"] == "https://a"

        metadata = json.loads(paths["metadata"].read_text(encoding="utf-8"))
        assert metadata["interaction_id"] == "int-abc-123"

    def test_refuses_to_write_outside_output_root(self, tmp_path: Path) -> None:
        interaction = _fake_interaction(outputs=[_text_output("x")])
        policy = SecurityPolicy(output_root=tmp_path)
        # This path resolves outside tmp_path.
        escaping = tmp_path.parent / "escapes"

        with pytest.raises(ConfigError):
            write_artifacts(
                interaction,
                ctx=_ctx(escaping),
                output_dir=escaping,
                policy=policy,
                started_at=datetime(2026, 4, 22, tzinfo=_UTC),
                finished_at=datetime(2026, 4, 22, tzinfo=_UTC),
            )


# ---------------------------------------------------------------------------
# Image extraction + writing
# ---------------------------------------------------------------------------


def _image_output(data_b64: str, mime: str = "image/png") -> SimpleNamespace:
    return SimpleNamespace(type="image", data=data_b64, mime_type=mime)


class TestExtractImages:
    def test_returns_decoded_bytes_and_mime(self) -> None:
        interaction = _fake_interaction(outputs=[_image_output(_TINY_PNG_B64)])
        images = extract_images(interaction)
        assert len(images) == 1
        data, mime = images[0]
        assert data == _TINY_PNG_BYTES
        assert mime == "image/png"

    def test_ignores_non_image_outputs(self) -> None:
        interaction = _fake_interaction(
            outputs=[_text_output("body"), _image_output(_TINY_PNG_B64)]
        )
        assert len(extract_images(interaction)) == 1

    def test_skips_entries_without_data(self) -> None:
        interaction = _fake_interaction(
            outputs=[SimpleNamespace(type="image", data=None, mime_type="image/png")]
        )
        assert extract_images(interaction) == []

    def test_skips_unparseable_base64(self) -> None:
        interaction = _fake_interaction(
            outputs=[_image_output("not@@valid$$base64!!", mime="image/png")]
        )
        assert extract_images(interaction) == []

    def test_defaults_missing_mime_to_png(self) -> None:
        interaction = _fake_interaction(
            outputs=[SimpleNamespace(type="image", data=_TINY_PNG_B64, mime_type=None)]
        )
        images = extract_images(interaction)
        assert images[0][1] == "image/png"


class TestWriteImages:
    def test_writes_numbered_files_under_images_dir(self, tmp_path: Path) -> None:
        images = [(_TINY_PNG_BYTES, "image/png"), (_TINY_PNG_BYTES, "image/jpeg")]
        paths = write_images(tmp_path, images)
        assert len(paths) == 2
        assert paths[0].name == "image_001.png"
        # image/jpeg → .jpe on stdlib mimetypes. Just verify JPEG-adjacent.
        assert paths[1].name.startswith("image_002.")
        assert paths[0].parent == tmp_path / "images"
        # Files round-trip.
        assert paths[0].read_bytes() == _TINY_PNG_BYTES

    def test_empty_list_does_not_create_dir(self, tmp_path: Path) -> None:
        paths = write_images(tmp_path, [])
        assert paths == []
        assert not (tmp_path / "images").exists()


class TestReportWithImages:
    def test_report_links_images_section(self) -> None:
        md = render_report_markdown(
            _fake_interaction(outputs=[_text_output("Body.")]),
            query="Q",
            agent=AGENT_FAST,
            image_filenames=["image_001.png", "image_002.jpg"],
        )
        assert "## Images" in md
        assert "![Image 1](images/image_001.png)" in md
        assert "![Image 2](images/image_002.jpg)" in md

    def test_no_images_section_when_list_empty(self) -> None:
        md = render_report_markdown(
            _fake_interaction(outputs=[_text_output("Body.")]),
            query="Q",
            agent=AGENT_FAST,
            image_filenames=[],
        )
        assert "## Images" not in md


class TestWriteArtifactsWithImages:
    def test_images_written_and_linked_from_report(self, tmp_path: Path) -> None:
        interaction = _fake_interaction(
            outputs=[
                _text_output("Body."),
                _image_output(_TINY_PNG_B64, mime="image/png"),
            ]
        )
        policy = SecurityPolicy(output_root=tmp_path)
        started = datetime(2026, 4, 22, 14, 30, tzinfo=_UTC)
        finished = datetime(2026, 4, 22, 14, 35, tzinfo=_UTC)
        output_dir = tmp_path / "run1"
        paths = write_artifacts(
            interaction,
            ctx=_ctx(output_dir),
            output_dir=output_dir,
            policy=policy,
            started_at=started,
            finished_at=finished,
        )
        assert paths["report"].is_file()
        assert (output_dir / "images" / "image_001.png").is_file()
        assert (output_dir / "images" / "image_001.png").read_bytes() == _TINY_PNG_BYTES
        report = paths["report"].read_text(encoding="utf-8")
        assert "![Image 1](images/image_001.png)" in report
