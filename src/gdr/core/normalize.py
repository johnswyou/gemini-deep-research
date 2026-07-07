"""Normalize Interactions API responses into plain renderable dicts.

This is the single place that understands what a completed interaction
looks like on the wire. Everything downstream (report rendering, plan
text extraction, `gdr status`, transcripts) consumes the normalized
shape instead of poking at SDK objects directly.

Shapes accepted (verified against google-genai 1.73.1 and the current
API docs):

* **SDK objects** — ``interaction.outputs`` is a list of typed content
  items (``TextContent``, ``ImageContent``, ``ThoughtContent``, plus
  tool call/result contents). Fields are attributes.
* **Plain dicts** — the same structure with keys, as used in tests and
  raw REST payloads.
* **Steps shape** — ``interaction.steps[].content[]`` as described in
  the public Deep Research docs. Some backend revisions expose outputs
  only through steps; we flatten them and tag content from thought-type
  steps accordingly.

Normalized items are plain dicts:

* ``{"type": "text", "text": str, "annotations": [...]}``
* ``{"type": "image", "data": str|None, "mime_type": str|None, "uri": str|None}``
* ``{"type": "thought", "text": str}`` — thought summaries flattened to
  one string (the SDK models ``ThoughtContent.summary`` as a *list* of
  ``TextContent``; older shapes use a plain ``summary`` string).

Tool call/result content items are not renderable and are skipped by
:func:`normalized_outputs`; :func:`raw_output_items` preserves them for
transcripts.
"""

from __future__ import annotations

from typing import Any

# Step types whose text content is intermediate reasoning, not report body.
_THOUGHT_STEP_TYPES = frozenset({"thought", "thought_summary"})
_THOUGHT_CONTENT_TYPES = frozenset({"thought", "thought_summary"})


def get_field(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute-then-key lookup.

    The SDK returns typed objects where fields are attributes; tests and
    raw REST payloads use dicts. This helper bridges both so callers
    don't care which they got. It is the one canonical copy — command
    and core modules should import it from here.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def raw_output_items(interaction: Any) -> list[Any]:
    """Return the un-normalized output content items of an interaction.

    Prefers ``outputs``; falls back to flattening ``steps[].content[]``.
    Used by the transcript writer, which wants everything the API sent
    (including tool call/result items) in original form.
    """
    return [item for item, _step_type in _iter_raw_items(interaction)]


def _iter_raw_items(interaction: Any) -> list[tuple[Any, str | None]]:
    outputs = get_field(interaction, "outputs")
    if outputs:
        return [(item, None) for item in outputs]

    items: list[tuple[Any, str | None]] = []
    for step in get_field(interaction, "steps") or []:
        step_type = get_field(step, "type")
        content = get_field(step, "content")
        if content is None:
            continue
        # A step's content may be a list of items or a single item.
        entries = content if isinstance(content, (list, tuple)) else [content]
        step_type_str = str(step_type) if step_type is not None else None
        items.extend((entry, step_type_str) for entry in entries)
    return items


def _thought_text(item: Any) -> str:
    """Flatten a thought content item to plain text.

    ``ThoughtContent.summary`` is a list of ``TextContent`` in the SDK;
    dict shapes may carry ``summary`` as a plain string or fall back to
    a ``text`` field.
    """
    summary = get_field(item, "summary")
    if isinstance(summary, str):
        return summary
    if summary is not None:
        parts = [str(t) for entry in summary if (t := get_field(entry, "text", ""))]
        if parts:
            return "\n".join(parts)
    return str(get_field(item, "text", "") or "")


def normalized_outputs(interaction: Any) -> list[dict[str, Any]]:
    """Normalize an interaction's outputs for rendering.

    Returns plain dicts (see module docstring). Content types that are
    not renderable (tool calls/results, audio, etc.) are skipped —
    :func:`raw_output_items` keeps them for the transcript.
    """
    normalized: list[dict[str, Any]] = []
    for item, step_type in _iter_raw_items(interaction):
        item_type = get_field(item, "type")
        if item_type == "text":
            entry: dict[str, Any] = {
                "type": "thought" if step_type in _THOUGHT_STEP_TYPES else "text",
                "text": str(get_field(item, "text", "") or ""),
            }
            annotations = get_field(item, "annotations")
            if annotations:
                entry["annotations"] = list(annotations)
            normalized.append(entry)
        elif item_type == "image":
            normalized.append(
                {
                    "type": "image",
                    "data": get_field(item, "data"),
                    "mime_type": get_field(item, "mime_type"),
                    "uri": get_field(item, "uri"),
                }
            )
        elif item_type in _THOUGHT_CONTENT_TYPES:
            text = _thought_text(item)
            if text:
                normalized.append({"type": "thought", "text": text})
        # Anything else (tool calls/results, audio, ...) is not renderable.
    return normalized


def interaction_status(interaction: Any) -> str | None:
    raw = get_field(interaction, "status")
    return None if raw is None else str(raw)


def interaction_id_of(interaction: Any) -> str | None:
    raw = get_field(interaction, "id")
    return None if raw is None else str(raw)


def error_of(interaction: Any) -> str | None:
    """Best-effort failure diagnostic for a terminal interaction.

    google-genai 1.73.1's ``Interaction`` model has no ``error`` field,
    but the public docs describe one — read it defensively so we surface
    whatever the backend provides.
    """
    err = get_field(interaction, "error")
    if err is None:
        return None
    if isinstance(err, str):
        return err
    code = get_field(err, "code")
    message = get_field(err, "message")
    bits = [str(b) for b in (code, message) if b]
    return ": ".join(bits) if bits else str(err)
