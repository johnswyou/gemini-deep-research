"""Render a completed Interaction into on-disk artifacts.

Artifact layout written by ``render_artifacts``::

    <output_dir>/
    ├── report.md       # Final synthesized text + Images + Sources
    ├── sources.json    # Deduplicated citation list
    ├── metadata.json   # Interaction id, timings, tools, usage
    ├── transcript.json # Raw outputs with MCP/auth redaction applied
    └── images/
        ├── image_001.png
        └── image_002.jpg

Image outputs are base64-decoded and written under ``images/`` with a
predictable name. The final report links them as Markdown image refs.

The module is tolerant of two output shapes:

* **SDK objects** — attributes (``output.type``, ``output.text``, …), as
  returned by ``google.genai.Client.interactions.get(...)``.
* **Dict mocks** — plain ``dict[str, Any]``, used in tests to keep fixtures
  readable without mocking the SDK's internal classes.

We normalize via ``getattr`` with a ``dict.get`` fallback so both work
without a separate adapter layer.
"""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from gdr.core.models import RunContext
from gdr.core.security import SecurityPolicy

_UTC = timezone.utc

# Fallback extension when the MIME type is missing or unknown — matches the
# default the Files API uses.
_DEFAULT_IMAGE_EXT = ".png"

# ---------------------------------------------------------------------------
# Output / attribute helpers
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute-then-key lookup.

    The SDK returns typed objects where fields are attributes; tests pass
    plain dicts where they're keys. This helper bridges both without forcing
    callers to care which is which.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    value = getattr(obj, name, default)
    return value


def _outputs_of(interaction: Any) -> list[Any]:
    raw = _get(interaction, "outputs", None)
    if raw is None:
        return []
    return list(raw)


# ---------------------------------------------------------------------------
# Report body
# ---------------------------------------------------------------------------


def _is_final_text_output(output: Any) -> bool:
    """Does this output carry final report text?

    Deep Research emits multiple intermediate `text` outputs for tool/plan
    steps; the final report is always the last text block. For the MVP we
    simply concatenate every ``text`` output in order — the agent produces
    the report as a single text block in practice, and concatenation is
    harmless for the rare multi-block case.
    """
    return bool(_get(output, "type") == "text")


def build_report_text(interaction: Any) -> str:
    """Concatenate the text outputs in order, skipping empties."""
    chunks: list[str] = []
    for output in _outputs_of(interaction):
        if not _is_final_text_output(output):
            continue
        text = _get(output, "text", "") or ""
        if text.strip():
            chunks.append(text)
    return "\n\n".join(chunks).strip()


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------


def _annotations_of(output: Any) -> list[Any]:
    raw = _get(output, "annotations", []) or []
    return list(raw)


def _citation_key(citation: dict[str, Any]) -> tuple[str, str]:
    """A stable dedup key for a citation.

    ``url_citation`` entries dedupe on URL; ``file_citation`` on document
    URI; ``place_citation`` on place_id. Fallback key lets us still dedupe
    unfamiliar citation shapes.
    """
    kind = str(citation.get("type", ""))
    if kind == "url_citation":
        return (kind, str(citation.get("url", "")))
    if kind == "file_citation":
        return (kind, str(citation.get("document_uri", "")))
    if kind == "place_citation":
        return (kind, str(citation.get("place_id", "")))
    return (kind, json.dumps(citation, sort_keys=True))


def _normalize_citation(annotation: Any) -> dict[str, Any] | None:
    """Turn an SDK annotation into a plain, JSON-safe dict."""
    kind = _get(annotation, "type")
    if not isinstance(kind, str):
        return None
    out: dict[str, Any] = {"type": kind}
    # Copy the well-known fields we care about; anything exotic lands in
    # the raw dict via `model_dump` if present.
    for field in ("url", "title", "document_uri", "file_name", "source", "place_id", "name"):
        value = _get(annotation, field)
        if value is not None:
            out[field] = value
    return out


def collect_sources(interaction: Any) -> list[dict[str, Any]]:
    """Extract a deduplicated list of citations from all outputs.

    Order is preserved by first occurrence so the numbering matches the
    reading order of the final report.
    """
    seen: set[tuple[str, str]] = set()
    collected: list[dict[str, Any]] = []
    for output in _outputs_of(interaction):
        if _get(output, "type") != "text":
            continue
        for annotation in _annotations_of(output):
            citation = _normalize_citation(annotation)
            if citation is None:
                continue
            key = _citation_key(citation)
            if key in seen:
                continue
            seen.add(key)
            collected.append(citation)
    return collected


def _render_source_line(index: int, source: dict[str, Any]) -> str:
    kind = source.get("type")
    if kind == "url_citation":
        title = source.get("title") or source.get("url") or "(untitled)"
        url = source.get("url", "")
        return f"{index}. [{title}]({url})"
    if kind == "file_citation":
        name = source.get("file_name") or source.get("document_uri") or "(file)"
        return f"{index}. {name}"
    if kind == "place_citation":
        name = source.get("name") or source.get("place_id") or "(place)"
        return f"{index}. {name}"
    return f"{index}. {source}"


def render_report_markdown(
    interaction: Any,
    *,
    query: str,
    agent: str,
    sources: Iterable[dict[str, Any]] | None = None,
    image_filenames: Iterable[str] | None = None,
) -> str:
    """Assemble the final ``report.md`` body.

    Structure: H1 (the query), italic context line, the report body, an
    optional ``## Images`` section (when ``image_filenames`` are given),
    and an optional ``## Sources`` section. Passing explicit ``sources``
    or ``image_filenames`` keeps the rendering deterministic when callers
    have already collected them (the tests, notably); otherwise we
    recollect from the interaction.
    """
    source_list = list(sources) if sources is not None else collect_sources(interaction)
    image_list = list(image_filenames) if image_filenames is not None else []
    body = build_report_text(interaction) or "*(No final report text was returned.)*"

    ts = datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        f"# {query}",
        "",
        f"*Research conducted {ts} using `{agent}`.*",
        "",
        body,
    ]
    if image_list:
        lines += ["", "---", "", "## Images", ""]
        for idx, name in enumerate(image_list, start=1):
            lines.append(f"![Image {idx}](images/{name})")
    if source_list:
        lines += ["", "---", "", "## Sources", ""]
        for idx, src in enumerate(source_list, start=1):
            lines.append(_render_source_line(idx, src))
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Image outputs
# ---------------------------------------------------------------------------


def _image_extension(mime: str | None) -> str:
    """Pick a filesystem extension for a given image MIME type.

    Falls back to ``.png`` when MIME is missing — most Deep Research
    visualization payloads are PNGs and matching the wire default is the
    least surprising behavior.
    """
    if not mime:
        return _DEFAULT_IMAGE_EXT
    guessed = mimetypes.guess_extension(mime)
    return guessed or _DEFAULT_IMAGE_EXT


def extract_images(interaction: Any) -> list[tuple[bytes, str]]:
    """Return a list of ``(decoded_bytes, mime_type)`` for image outputs.

    Skips outputs whose ``data`` field is missing or can't be decoded —
    garbage on the wire shouldn't abort the whole render, it just gets
    silently dropped. (The raw transcript still captures the original
    payload for debugging.)
    """
    collected: list[tuple[bytes, str]] = []
    for output in _outputs_of(interaction):
        if _get(output, "type") != "image":
            continue
        raw_data = _get(output, "data")
        if not raw_data:
            continue
        try:
            decoded = base64.b64decode(str(raw_data), validate=True)
        except (ValueError, binascii.Error):
            continue
        mime = _get(output, "mime_type") or "image/png"
        collected.append((decoded, str(mime)))
    return collected


def write_images(output_dir: Path, images: list[tuple[bytes, str]]) -> list[Path]:
    """Write ``images`` under ``<output_dir>/images/`` and return their paths.

    Filenames are ``image_NNN<.ext>`` with NNN zero-padded to 3 digits —
    avoids sorting surprises in shells that do lexical sort.
    """
    if not images:
        return []
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, (data, mime) in enumerate(images, start=1):
        ext = _image_extension(mime)
        path = images_dir / f"image_{index:03d}{ext}"
        path.write_bytes(data)
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def _usage_dict(interaction: Any) -> dict[str, Any]:
    usage = _get(interaction, "usage")
    if usage is None:
        return {}
    total = _get(usage, "total_tokens")
    inp = _get(usage, "input_tokens")
    out = _get(usage, "output_tokens")
    d: dict[str, Any] = {}
    if total is not None:
        d["total_tokens"] = total
    if inp is not None:
        d["input_tokens"] = inp
    if out is not None:
        d["output_tokens"] = out
    return d


def build_metadata(
    interaction: Any,
    *,
    ctx: RunContext,
    started_at: datetime,
    finished_at: datetime,
    output_dir: Path,
) -> dict[str, Any]:
    duration_seconds = max(0, int((finished_at - started_at).total_seconds()))
    return {
        "interaction_id": _get(interaction, "id"),
        "previous_interaction_id": ctx.previous_interaction_id,
        "query": ctx.query,
        "agent": ctx.agent,
        "status": _get(interaction, "status"),
        "created_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": duration_seconds,
        "usage": _usage_dict(interaction),
        "tools": list(ctx.builtin_tools)
        + ([] if ctx.file_search is None else ["file_search"])
        + ["mcp_server"] * len(ctx.mcp_servers),
        "output_dir": str(output_dir),
    }


# ---------------------------------------------------------------------------
# Transcript (redacted)
# ---------------------------------------------------------------------------


def build_transcript(interaction: Any, *, policy: SecurityPolicy) -> dict[str, Any]:
    """Raw outputs + minimal envelope, with sensitive fields redacted.

    Useful for debugging and auditing after the fact. We emit outputs in
    the order the API returned them so reconstruction is trivial.
    """
    outputs = _outputs_of(interaction)
    serialized: list[Any] = []
    for output in outputs:
        if isinstance(output, dict):
            serialized.append(output)
        elif hasattr(output, "model_dump"):
            serialized.append(output.model_dump(exclude_none=True))
        else:
            # Best-effort: grab __dict__ attrs that are JSON-safe.
            serialized.append({k: v for k, v in vars(output).items() if not k.startswith("_")})

    raw: dict[str, Any] = {
        "interaction_id": _get(interaction, "id"),
        "status": _get(interaction, "status"),
        "outputs": serialized,
    }
    return cast("dict[str, Any]", policy.redact(raw))


# ---------------------------------------------------------------------------
# Artifact writer
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


def write_artifacts(
    interaction: Any,
    *,
    ctx: RunContext,
    output_dir: Path,
    policy: SecurityPolicy,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Path]:
    """Write the full artifact set and return a map of name → path."""
    policy.confine(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sources = collect_sources(interaction)
    image_paths = write_images(output_dir, extract_images(interaction))
    report = render_report_markdown(
        interaction,
        query=ctx.query,
        agent=ctx.agent,
        sources=sources,
        image_filenames=[p.name for p in image_paths],
    )
    metadata = build_metadata(
        interaction,
        ctx=ctx,
        started_at=started_at,
        finished_at=finished_at,
        output_dir=output_dir,
    )
    transcript = build_transcript(interaction, policy=policy)

    report_path = output_dir / "report.md"
    sources_path = output_dir / "sources.json"
    metadata_path = output_dir / "metadata.json"
    transcript_path = output_dir / "transcript.json"

    report_path.write_text(report, encoding="utf-8")
    _write_json(sources_path, {"interaction_id": metadata["interaction_id"], "sources": sources})
    _write_json(metadata_path, metadata)
    _write_json(transcript_path, transcript)

    return {
        "report": report_path,
        "sources": sources_path,
        "metadata": metadata_path,
        "transcript": transcript_path,
    }
