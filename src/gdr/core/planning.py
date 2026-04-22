"""Collaborative planning helpers.

Deep Research supports a three-step planning workflow:

1. **Request a plan** — create an interaction with
   ``agent_config.collaborative_planning = True``. The agent returns a
   plan rather than a full report.
2. **Refine** (optional) — iterate with ``previous_interaction_id`` and
   ``collaborative_planning = True`` again, giving feedback as input.
3. **Approve and execute** — set ``collaborative_planning = False`` with
   ``previous_interaction_id`` pointing at the approved plan; the agent
   runs the full research.

This module provides:

* pure helpers for assembling plan requests and extracting the plan text
  from a completed interaction,
* a terminal-UI loop (``interactive_plan_loop``) used by
  ``gdr research --plan``,
* thin primitives reused by ``gdr plan refine`` for async workflows.

The helpers are intentionally agnostic about which ``client`` is passed
in, so tests can drive the flow with a mock SDK surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from gdr.core.client import GdrClient
from gdr.core.models import AgentConfig
from gdr.errors import GdrError
from gdr.ui.progress import run_with_live_status

# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


class PlanDecision(str, Enum):
    """What the user chose to do after seeing a plan."""

    APPROVE = "approve"
    REFINE = "refine"
    CANCEL = "cancel"


@dataclass(frozen=True)
class PlanRequest:
    """Everything required to create one plan interaction."""

    input_text: str
    agent: str
    previous_interaction_id: str | None = None


# ---------------------------------------------------------------------------
# Request assembly
# ---------------------------------------------------------------------------


def build_plan_kwargs(req: PlanRequest) -> dict[str, Any]:
    """Create() kwargs for a collaborative-planning interaction.

    Plans are short (<30s typically), so we deliberately disable streaming
    bits: ``thinking_summaries = "none"`` and ``visualization = "off"``
    keep the agent focused on producing a plan fast. Streaming adds UI
    complexity with no user win in this phase.
    """
    kwargs: dict[str, Any] = {
        "agent": req.agent,
        "input": req.input_text,
        "background": True,
        "agent_config": AgentConfig(
            thinking_summaries="none",
            visualization="off",
            collaborative_planning=True,
        ).model_dump(),
    }
    if req.previous_interaction_id is not None:
        kwargs["previous_interaction_id"] = req.previous_interaction_id
    return kwargs


# ---------------------------------------------------------------------------
# Interaction access (attribute-then-key, same trick as rendering._get)
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def extract_plan_text(interaction: Any) -> str:
    """Pull the plan body from a completed plan interaction.

    Walks the first ``text`` output found. Deep Research always emits the
    plan as a single text output per the documented flow.
    """
    outputs = _get(interaction, "outputs") or []
    for output in outputs:
        if _get(output, "type") == "text":
            text = _get(output, "text", "") or ""
            if text.strip():
                return text
    return ""


def extract_interaction_id(interaction: Any) -> str | None:
    raw = _get(interaction, "id")
    return None if raw is None else str(raw)


# ---------------------------------------------------------------------------
# Polling + display
# ---------------------------------------------------------------------------


def run_plan_phase(
    client: GdrClient,
    *,
    req: PlanRequest,
    console: Console,
) -> Any:
    """Submit one plan request and poll it to completion.

    Returns the completed interaction object from the SDK. The caller is
    responsible for extracting the plan text and asking the user what to
    do next.
    """
    try:
        initial = client.interactions.create(**build_plan_kwargs(req))
    except Exception as exc:
        raise GdrError(f"Failed to create plan: {exc}") from exc

    interaction_id = extract_interaction_id(initial)
    if not interaction_id:
        raise GdrError("Plan request returned no interaction id.")

    # Plans are fast; reuse the polling helper with a short descriptive
    # query so the spinner tells the user what's going on.
    return run_with_live_status(
        client.interactions.get,
        interaction_id,
        console=console,
        query="Planning",
    )


def show_plan(console: Console, interaction: Any) -> None:
    """Render the plan body in a Rich panel."""
    text = extract_plan_text(interaction) or "(no plan text returned)"
    console.print()
    console.print(Panel(text, title="Research Plan", border_style="cyan", expand=True))
    console.print()


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------


def prompt_plan_decision(console: Console) -> PlanDecision:
    """Ask the user whether to approve, refine, or cancel.

    Falls through to APPROVE on empty input (the most common case — the
    user is happy with the plan and just hits Enter). Anything starting
    with 'r' means refine; anything starting with 'c' means cancel.
    """
    console.print("[bold]What next?[/bold]")
    console.print("  [cyan][A][/cyan]pprove   run the research with this plan")
    console.print("  [cyan][R][/cyan]efine    iterate on the plan with feedback")
    console.print("  [cyan][C][/cyan]ancel    abort this run")
    raw = typer.prompt("Choice", default="A").strip().lower()
    if raw.startswith("r"):
        return PlanDecision.REFINE
    if raw.startswith("c"):
        return PlanDecision.CANCEL
    return PlanDecision.APPROVE


def prompt_refinement_feedback() -> str:
    """Collect refinement feedback. Returns empty string on empty input."""
    raw = typer.prompt("What would you like to change", default="")
    return str(raw).strip()


# ---------------------------------------------------------------------------
# Interactive loop
# ---------------------------------------------------------------------------


def interactive_plan_loop(
    client: GdrClient,
    *,
    initial_query: str,
    agent: str,
    console: Console,
) -> str | None:
    """Run the approve / refine / cancel loop until we have a final plan.

    Returns the id of the approved plan interaction, or ``None`` if the
    user cancelled. The returned id is what the caller feeds back as
    ``previous_interaction_id`` for the execution phase.
    """
    request = PlanRequest(input_text=initial_query, agent=agent)

    while True:
        plan_interaction = run_plan_phase(client, req=request, console=console)
        plan_id = extract_interaction_id(plan_interaction)
        if not plan_id:
            raise GdrError("Plan interaction had no id.")
        show_plan(console, plan_interaction)

        # Inner loop: keep asking until the user gives us real feedback or
        # approves/cancels. This handles the "I typed R but forgot what to
        # say" case without burning another plan request.
        while True:
            decision = prompt_plan_decision(console)
            if decision == PlanDecision.APPROVE:
                return plan_id
            if decision == PlanDecision.CANCEL:
                return None

            feedback = prompt_refinement_feedback()
            if feedback:
                request = PlanRequest(
                    input_text=feedback,
                    agent=agent,
                    previous_interaction_id=plan_id,
                )
                break  # exit inner; outer loop creates the refined plan
            console.print("[yellow]No feedback provided; choose again or approve as-is.[/yellow]")
