"""Normalize Interactions API responses into plain renderable dicts.

This is the single place that understands what a completed interaction
looks like on the wire. Everything downstream (report rendering, plan
text extraction, `gdr status`, transcripts) consumes the normalized
shape instead of poking at SDK objects directly.

Shapes accepted (verified against google-genai 2.x and the current
API docs):

* **2.x SDK objects** — ``interaction.steps[]`` is the full timeline of
  typed step objects. ``ModelOutputStep.content[]`` carries the report
  body; ``ThoughtStep`` carries ``summary`` (a list of ``TextContent``)
  and NO ``content``; tool call/result steps have neither. There is no
  ``outputs`` attribute on the 2.x ``Interaction``.
* **Plain dicts** — the same structures with keys, as used in tests and
  raw REST payloads, including the docs' ``steps[].content[]`` shape.
* **Legacy ``outputs`` lists** — flat content-item lists from pre-2.x
  payloads and gdr's own streamed-fallback synthesis; preferred over
  ``steps`` when non-empty.

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
# Step types whose text content IS the report body. Under the 2.x schema a
# completed interaction's ``steps`` is the full timeline — ``user_input`` and
# tool call/result steps must NOT bleed into the report, only ``model_output``.
# Legacy ``outputs`` items arrive with ``step_type is None`` and are always body.
_BODY_STEP_TYPES = frozenset({"model_output"})


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
        step_type_str = str(step_type) if step_type is not None else None
        content = get_field(step, "content")
        if content is None:
            # The 2.x ``ThoughtStep`` carries ``summary`` instead of
            # ``content``. Pass the step itself through as a thought item;
            # ``_thought_text`` knows how to flatten its summary list.
            if step_type_str in _THOUGHT_STEP_TYPES and get_field(step, "summary") is not None:
                items.append((step, step_type_str))
            continue
        # A step's content may be a list of items or a single item.
        entries = content if isinstance(content, (list, tuple)) else [content]
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
            if step_type in _THOUGHT_STEP_TYPES:
                kind = "thought"
            elif step_type is None or step_type in _BODY_STEP_TYPES:
                kind = "text"
            else:
                # user_input / tool call/result step text is timeline context,
                # not report body — never render it. (The full step data is
                # still preserved for the transcript.)
                continue
            entry: dict[str, Any] = {
                "type": kind,
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


def has_report_content(interaction: Any) -> bool:
    """True when the interaction carries renderable report body content.

    Text or image outputs count; thoughts alone do not — a terminal fetch
    whose only renderable items are thought summaries has no report for
    the artifact writer, so a cleanly streamed buffer may still stand in.
    This is the ONE place that decides whether a fetch is authoritative;
    callers must not re-derive it from raw fields (the 2.x ``Interaction``
    has no ``outputs`` attribute to key on).
    """
    return any(item["type"] in ("text", "image") for item in normalized_outputs(interaction))


def interaction_status(interaction: Any) -> str | None:
    raw = get_field(interaction, "status")
    return None if raw is None else str(raw)


def interaction_id_of(interaction: Any) -> str | None:
    raw = get_field(interaction, "id")
    return None if raw is None else str(raw)


def _format_error(err: Any) -> str | None:
    if err is None:
        return None
    if isinstance(err, str):
        return err
    code = get_field(err, "code")
    message = get_field(err, "message")
    bits = [str(b) for b in (code, message) if b]
    return ": ".join(bits) if bits else str(err)


def error_of(interaction: Any) -> str | None:
    """Best-effort failure diagnostic for a terminal interaction.

    The 2.x ``Interaction`` model has no top-level ``error`` field —
    failure details ride on the steps (``ModelOutputStep.error``, with
    ``code``/``message``). Check the envelope first (dict shapes and
    forward compatibility), then fall back to the first step-level error.
    """
    top = _format_error(get_field(interaction, "error"))
    if top is not None:
        return top
    for step in get_field(interaction, "steps") or []:
        found = _format_error(get_field(step, "error"))
        if found is not None:
            return found
    return None
