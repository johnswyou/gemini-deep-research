"""Build kwargs for `client.interactions.create()` from an immutable RunContext.

This module is the single choke point where gdr's domain model becomes the
wire shape expected by the Interactions API. Keeping the translation in one
place makes it easy to audit what we send and to keep the rest of gdr free
of SDK-specific details.

Design rules:

* Input translation: plain string when there are no media parts, typed parts
  list otherwise. The API accepts both.
* Tools are serialized in a stable order: simple builtin tools first, then
  file_search, then MCP servers. This keeps request snapshots (and tests)
  deterministic.
* MCP headers are validated BEFORE we serialize anything — one failing
  header aborts the whole request so a partial dict never reaches the API.
* Under `--untrusted-input`, disallowed tools are filtered *last* so the
  warning list returned to the caller reflects the final request shape.
* `agent_config` is always sent so the API receives explicit values and
  future gdr changes (e.g. toggling visualization) can't accidentally
  depend on undocumented defaults.
"""

from __future__ import annotations

from typing import Any

from gdr.core.models import (
    FileSearchSpec,
    InputPart,
    McpSpec,
    RunContext,
)
from gdr.core.security import SecurityPolicy


def _serialize_mcp(mcp: McpSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "mcp_server",
        "name": mcp.name,
        "url": mcp.url,
    }
    if mcp.headers:
        payload["headers"] = dict(mcp.headers)
    if mcp.allowed_tools is not None:
        payload["allowed_tools"] = list(mcp.allowed_tools)
    return payload


def _serialize_file_search(spec: FileSearchSpec) -> dict[str, Any]:
    return {
        "type": "file_search",
        "file_search_store_names": list(spec.file_search_store_names),
    }


def _serialize_input_part(part: InputPart) -> dict[str, Any]:
    # Both TextPart and MediaPart are Pydantic models; `model_dump()` gives us
    # a dict with exactly the fields the API expects.
    return part.model_dump(exclude_none=True)


def serialize_input(text: str, input_parts: tuple[InputPart, ...]) -> str | list[dict[str, Any]]:
    """Return either the plain text string or a parts list.

    The API accepts either form. We use the plain string when there are no
    extra parts, so requests stay readable in `--dry-run` and snapshot tests.
    Shared with the planning flow so plan interactions ground on the same
    multimodal inputs as direct research runs.
    """
    if not input_parts:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    parts.extend(_serialize_input_part(p) for p in input_parts)
    return parts


def _serialize_input(ctx: RunContext) -> str | list[dict[str, Any]]:
    return serialize_input(ctx.query, ctx.input_parts)


def build_tools(ctx: RunContext, policy: SecurityPolicy) -> tuple[list[dict[str, Any]], list[str]]:
    """Assemble the `tools` list for a create() call.

    Returns the tools list and, when applicable, the list of tool types that
    were stripped by the security policy (so callers can warn the user).
    """
    # Validate MCP headers eagerly — fail fast before any wire-shape assembly.
    for mcp in ctx.mcp_servers:
        policy.validate_mcp_headers(mcp.headers)

    tools: list[dict[str, Any]] = []
    for name in ctx.builtin_tools:
        tools.append({"type": name})
    if ctx.file_search is not None:
        tools.append(_serialize_file_search(ctx.file_search))
    for mcp in ctx.mcp_servers:
        tools.append(_serialize_mcp(mcp))

    return policy.filter_tools(tools)


def build_create_kwargs(
    ctx: RunContext, policy: SecurityPolicy
) -> tuple[dict[str, Any], list[str]]:
    """Produce the full kwargs dict for `client.interactions.create(**kwargs)`.

    The returned tuple is ``(kwargs, stripped_tools)``. ``stripped_tools`` is
    empty unless the policy removed tools under untrusted-input mode.

    Two wire shapes:

    * **Agent runs** (the default): ``agent=`` + ``agent_config=`` target a
      Deep Research agent.
    * **Model runs** (``ctx.model`` set): ``model=`` targets a plain Gemini
      model — used for lightweight follow-ups. ``agent``/``agent_config``
      must NOT be sent alongside ``model``.
    """
    tools, stripped = build_tools(ctx, policy)

    kwargs: dict[str, Any] = {
        "input": _serialize_input(ctx),
        # The docs require store=true for Deep Research; send it explicitly
        # rather than relying on the backend default.
        "store": True,
    }
    if ctx.model is not None:
        kwargs["model"] = ctx.model
        # Plain models do NOT support background interactions under the 2.x
        # API — `create()` 400s with "Model '...' does not support background
        # interactions." A `--model` follow-up is a fast synchronous call, so
        # force background off regardless of ctx.background (which defaults
        # True for the Deep Research agent path).
        kwargs["background"] = False
    else:
        kwargs["background"] = ctx.background
        kwargs["agent"] = ctx.agent
        kwargs["agent_config"] = ctx.agent_config.model_dump()
    if ctx.stream:
        kwargs["stream"] = True
    if tools:
        kwargs["tools"] = tools
    if ctx.previous_interaction_id is not None:
        kwargs["previous_interaction_id"] = ctx.previous_interaction_id

    return kwargs, stripped
