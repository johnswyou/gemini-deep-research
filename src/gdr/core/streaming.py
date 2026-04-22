"""SSE event aggregator for the Gemini Interactions API streaming channel.

## Event model

Deep Research streams six event types:

| Event type             | Description                                     |
| ---------------------- | ----------------------------------------------- |
| `interaction.start`    | First event; carries the new interaction id    |
| `content.start`        | New output block opens at ``index`` with type  |
| `content.delta`        | Incremental update at ``index`` (see below)    |
| `content.stop`         | Output block at ``index`` is finalized         |
| `interaction.complete` | Terminal; ``outputs`` is documented as None   |
| `error`                | Unrecoverable stream error — abort             |

Delta types observed for Deep Research:

| `delta.type`       | Payload                                                    |
| ------------------ | ---------------------------------------------------------- |
| `text`             | ``delta.text`` — a chunk of final report text              |
| `thought_summary`  | ``delta.content.text`` — one intermediate reasoning step  |
| `image`            | ``delta.data`` — base64-encoded image bytes               |

## What this module does

The aggregator reduces raw events into a stream of semantic
:class:`StreamEvent` emissions and keeps typed per-index builders so a
final snapshot is available after the stream ends. The UI layer subscribes
to emissions for live rendering; the research command uses the aggregator's
final state for logging/diagnostics only.

**The final report is never reconstructed from the stream.** Per the docs,
``interaction.complete`` during streaming has ``outputs=None``, so the
command always re-fetches via ``client.interactions.get(id=...)`` after the
stream ends (whether cleanly or by disconnect). Partial delta buffers are
discarded on disconnect. This is the contract the plan committed to.

Disconnect detection is pushed up to the caller — :meth:`feed` surfaces
:class:`StreamError` only when an ``error`` event arrives over the wire.
IOErrors and httpx exceptions that kill the iterator should be caught at
the call site (see :mod:`gdr.commands.research`) and turned into a
polling fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from gdr.errors import StreamError

# ---------------------------------------------------------------------------
# Emission types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamEvent:
    """UI-friendly semantic event emitted by the aggregator.

    ``kind`` values:

    * ``"start"``          — interaction has a new id; ``interaction_id`` set
    * ``"content_start"``  — new output block opened; ``index``, ``content_type``
    * ``"text_delta"``     — ``text`` chunk appended to output at ``index``
    * ``"thought"``        — a finalized thought summary (``text``)
    * ``"image"``          — base64 image chunk arrived (``image_data``)
    * ``"content_stop"``   — output at ``index`` completed
    * ``"complete"``       — interaction finished; ``status`` set

    Thought summaries arrive as whole strings (the API emits them as single
    deltas, not incremental chunks), so we surface each one as a finished
    event rather than bookending with start/stop.
    """

    kind: str
    interaction_id: str | None = None
    status: str | None = None
    index: int | None = None
    content_type: str | None = None
    text: str = ""
    image_data: str = ""


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@dataclass
class _TextBuilder:
    index: int
    buffer: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "text"

    def add(self, delta: Any) -> str:
        chunk = _get(delta, "text", "") or ""
        if chunk:
            self.buffer.append(chunk)
        return chunk

    def finalize(self) -> str:
        return "".join(self.buffer)


@dataclass
class _ImageBuilder:
    index: int
    chunks: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "image"

    def add(self, delta: Any) -> str:
        data = _get(delta, "data", "") or ""
        if data:
            self.chunks.append(data)
        return data

    def finalize(self) -> str:
        return "".join(self.chunks)


@dataclass
class _ThoughtBuilder:
    """Container for thought-summary deltas on a content.start index.

    Thoughts are almost always whole-message deltas, but we accept multiple
    chunks defensively and concatenate.
    """

    index: int
    buffer: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "thought"

    def add(self, delta: Any) -> str:
        # The SDK exposes thought text under delta.content.text; dict mocks
        # may put it under delta["content"]["text"] or just delta["text"].
        content = _get(delta, "content", None)
        chunk = _get(content, "text", "") if content is not None else _get(delta, "text", "")
        chunk = chunk or ""
        if chunk:
            self.buffer.append(chunk)
        return chunk

    def finalize(self) -> str:
        return "".join(self.buffer)


_Builder = _TextBuilder | _ImageBuilder | _ThoughtBuilder


def _make_builder(index: int, content_type: str | None) -> _Builder:
    if content_type == "image":
        return _ImageBuilder(index=index)
    if content_type in ("thought", "thought_summary"):
        return _ThoughtBuilder(index=index)
    return _TextBuilder(index=index)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute-then-key lookup (mirrors rendering._get)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


@dataclass
class AggregatedSnapshot:
    """Final state of the aggregator after a stream ends.

    This is *not* the authoritative report shape — callers should re-fetch
    via ``client.interactions.get(id=...)`` for the canonical outputs. The
    snapshot is for debugging and for situations where a best-effort view
    of the streamed content is useful (e.g. CLI --verbose logging).
    """

    interaction_id: str | None = None
    status: str | None = None
    text: str = ""
    thoughts: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    completed_cleanly: bool = False


class StreamAggregator:
    """Fold a raw SSE event sequence into :class:`StreamEvent` emissions."""

    def __init__(
        self,
        *,
        on_event: Callable[[StreamEvent], None] | None = None,
    ) -> None:
        self._builders: dict[int, _Builder] = {}
        self._text_chunks: list[str] = []
        self._thoughts: list[str] = []
        self._images: list[str] = []
        self._interaction_id: str | None = None
        self._status: str | None = None
        self._completed_cleanly = False
        self._on_event: Callable[[StreamEvent], None] = on_event or (lambda _e: None)

    # -- properties ----------------------------------------------------

    @property
    def interaction_id(self) -> str | None:
        return self._interaction_id

    @property
    def status(self) -> str | None:
        return self._status

    def snapshot(self) -> AggregatedSnapshot:
        return AggregatedSnapshot(
            interaction_id=self._interaction_id,
            status=self._status,
            text="".join(self._text_chunks),
            thoughts=list(self._thoughts),
            images=list(self._images),
            completed_cleanly=self._completed_cleanly,
        )

    # -- ingestion -----------------------------------------------------

    def feed(self, event: Any) -> None:
        """Process one SSE event.

        Raises :class:`StreamError` on an ``error`` event. Unknown event
        types are ignored (forward-compatible with future SDK additions).
        """
        event_type = _get(event, "event_type")

        if event_type == "interaction.start":
            self._handle_start(event)
        elif event_type == "content.start":
            self._handle_content_start(event)
        elif event_type == "content.delta":
            self._handle_content_delta(event)
        elif event_type == "content.stop":
            self._handle_content_stop(event)
        elif event_type == "interaction.complete":
            self._handle_complete(event)
        elif event_type == "error":
            self._handle_error(event)
        # Unknown event types: ignore silently.

    def consume(self, stream: Iterable[Any]) -> None:
        """Iterate an entire stream. Exceptions during iteration propagate."""
        for event in stream:
            self.feed(event)

    # -- per-event handlers --------------------------------------------

    def _handle_start(self, event: Any) -> None:
        interaction = _get(event, "interaction")
        self._interaction_id = _get(interaction, "id")
        self._status = _get(interaction, "status")
        self._on_event(
            StreamEvent(kind="start", interaction_id=self._interaction_id, status=self._status)
        )

    def _handle_content_start(self, event: Any) -> None:
        index = _get(event, "index")
        if index is None:
            return
        content = _get(event, "content")
        content_type = _get(content, "type")
        self._builders[int(index)] = _make_builder(int(index), content_type)
        self._on_event(
            StreamEvent(kind="content_start", index=int(index), content_type=content_type)
        )

    def _handle_content_delta(self, event: Any) -> None:
        index = _get(event, "index")
        if index is None:
            return
        delta = _get(event, "delta")
        delta_type = _get(delta, "type")
        builder = self._builders.get(int(index))
        # Out-of-order safety: if we never saw content.start, invent a
        # builder based on the delta type. Most APIs emit content.start,
        # but we never want to silently drop real data.
        if builder is None:
            builder = _make_builder(int(index), _infer_content_type(delta_type))
            self._builders[int(index)] = builder

        if delta_type == "text":
            chunk = _get(delta, "text", "") or ""
            if isinstance(builder, _TextBuilder):
                builder.add(delta)
            if chunk:
                self._text_chunks.append(chunk)
                self._on_event(StreamEvent(kind="text_delta", index=int(index), text=chunk))
        elif delta_type == "thought_summary":
            # The API emits thought summaries as whole messages; finalize
            # eagerly so the UI can render one per event.
            content = _get(delta, "content", None)
            text = (
                _get(content, "text", "") if content is not None else _get(delta, "text", "")
            ) or ""
            if text:
                self._thoughts.append(text)
                self._on_event(StreamEvent(kind="thought", index=int(index), text=text))
        elif delta_type == "image":
            data = _get(delta, "data", "") or ""
            if isinstance(builder, _ImageBuilder):
                builder.add(delta)
            if data:
                self._on_event(StreamEvent(kind="image", index=int(index), image_data=data))

    def _handle_content_stop(self, event: Any) -> None:
        index = _get(event, "index")
        if index is None:
            return
        builder = self._builders.pop(int(index), None)
        if builder is None:
            return
        final = builder.finalize()
        if isinstance(builder, _ImageBuilder) and final:
            self._images.append(final)
        self._on_event(StreamEvent(kind="content_stop", index=int(index)))

    def _handle_complete(self, event: Any) -> None:
        interaction = _get(event, "interaction")
        if interaction is not None:
            status = _get(interaction, "status")
            if status:
                self._status = status
        self._completed_cleanly = True
        self._on_event(
            StreamEvent(
                kind="complete",
                interaction_id=self._interaction_id,
                status=self._status,
            )
        )

    def _handle_error(self, event: Any) -> None:
        err = _get(event, "error", {}) or {}
        code = str(_get(err, "code", "unknown"))
        message = str(_get(err, "message", ""))
        raise StreamError(f"Stream error {code}: {message}".rstrip(": "))


def _infer_content_type(delta_type: str | None) -> str | None:
    if delta_type == "image":
        return "image"
    if delta_type == "thought_summary":
        return "thought"
    if delta_type == "text":
        return "text"
    return None
