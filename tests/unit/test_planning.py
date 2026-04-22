"""Tests for `gdr.core.planning`."""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from gdr.core.planning import (
    PlanDecision,
    PlanRequest,
    build_plan_kwargs,
    extract_interaction_id,
    extract_plan_text,
    interactive_plan_loop,
    show_plan,
)
from gdr.errors import GdrError

# ---------------------------------------------------------------------------
# build_plan_kwargs
# ---------------------------------------------------------------------------


class TestBuildPlanKwargs:
    def test_core_fields_set(self) -> None:
        req = PlanRequest(input_text="Plan my research", agent="deep-research-preview-04-2026")
        kw = build_plan_kwargs(req)
        assert kw["agent"] == "deep-research-preview-04-2026"
        assert kw["input"] == "Plan my research"
        assert kw["background"] is True

    def test_agent_config_enables_collaborative_planning(self) -> None:
        kw = build_plan_kwargs(PlanRequest(input_text="x", agent="a"))
        assert kw["agent_config"]["collaborative_planning"] is True
        # Plans skip streaming-only features to stay fast.
        assert kw["agent_config"]["thinking_summaries"] == "none"
        assert kw["agent_config"]["visualization"] == "off"

    def test_previous_interaction_id_passed_through_when_set(self) -> None:
        kw = build_plan_kwargs(
            PlanRequest(input_text="x", agent="a", previous_interaction_id="prev-123")
        )
        assert kw["previous_interaction_id"] == "prev-123"

    def test_previous_interaction_id_omitted_when_none(self) -> None:
        kw = build_plan_kwargs(PlanRequest(input_text="x", agent="a"))
        assert "previous_interaction_id" not in kw

    def test_no_stream_flag_on_plan_kwargs(self) -> None:
        # Plans never stream in our flow.
        kw = build_plan_kwargs(PlanRequest(input_text="x", agent="a"))
        assert "stream" not in kw


# ---------------------------------------------------------------------------
# extract_plan_text / extract_interaction_id
# ---------------------------------------------------------------------------


class TestExtractors:
    def test_extract_plan_text_returns_first_text_output(self) -> None:
        interaction = SimpleNamespace(
            outputs=[
                SimpleNamespace(type="thought", text="thinking"),
                SimpleNamespace(type="text", text="# Plan\n\n1. Step one\n2. Step two"),
                SimpleNamespace(type="text", text="never-reached"),
            ]
        )
        assert extract_plan_text(interaction) == "# Plan\n\n1. Step one\n2. Step two"

    def test_extract_plan_text_handles_dict_shape(self) -> None:
        interaction = {"outputs": [{"type": "text", "text": "dict plan"}]}
        assert extract_plan_text(interaction) == "dict plan"

    def test_extract_plan_text_empty_when_no_text(self) -> None:
        interaction = SimpleNamespace(outputs=[SimpleNamespace(type="thought", text="x")])
        assert extract_plan_text(interaction) == ""

    def test_extract_plan_text_empty_when_no_outputs(self) -> None:
        assert extract_plan_text(SimpleNamespace()) == ""

    def test_extract_interaction_id_object(self) -> None:
        assert extract_interaction_id(SimpleNamespace(id="abc-123")) == "abc-123"

    def test_extract_interaction_id_dict(self) -> None:
        assert extract_interaction_id({"id": "xyz"}) == "xyz"

    def test_extract_interaction_id_none_when_missing(self) -> None:
        assert extract_interaction_id(SimpleNamespace()) is None
        assert extract_interaction_id(None) is None


# ---------------------------------------------------------------------------
# show_plan (smoke — prints don't crash)
# ---------------------------------------------------------------------------


class TestShowPlan:
    def test_show_plan_renders_text_in_panel(self) -> None:
        console = Console(file=io.StringIO(), record=True, force_terminal=False, width=80)
        show_plan(
            console,
            SimpleNamespace(outputs=[SimpleNamespace(type="text", text="My plan body.")]),
        )
        output = console.export_text()
        assert "My plan body." in output
        assert "Research Plan" in output

    def test_show_plan_fallback_when_empty(self) -> None:
        console = Console(file=io.StringIO(), record=True, force_terminal=False, width=80)
        show_plan(console, SimpleNamespace(outputs=[]))
        assert "no plan text returned" in console.export_text()


# ---------------------------------------------------------------------------
# interactive_plan_loop (via mocked client + mocked prompts)
# ---------------------------------------------------------------------------


class _FakeInteractions:
    """In-memory stand-in for ``client.interactions``.

    ``create`` returns a synthetic in-progress Interaction keyed by a
    counter; ``get`` returns a completed Interaction with canned plan text
    so the run_with_live_status call finishes immediately.
    """

    def __init__(self, plan_texts: list[str]) -> None:
        self._plan_texts = plan_texts
        self._counter = 0
        self.create_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.create_calls.append(kwargs)
        plan_id = f"plan-{self._counter}"
        self._counter += 1
        return SimpleNamespace(id=plan_id, status="in_progress")

    def get(self, *, id: str) -> SimpleNamespace:
        idx = int(id.rsplit("-", maxsplit=1)[-1])
        text = self._plan_texts[min(idx, len(self._plan_texts) - 1)]
        return SimpleNamespace(
            id=id,
            status="completed",
            outputs=[SimpleNamespace(type="text", text=text, annotations=[])],
            usage=SimpleNamespace(total_tokens=100),
        )


class _FakeClient:
    def __init__(self, plan_texts: list[str]) -> None:
        self.interactions = _FakeInteractions(plan_texts)


def _capture_console() -> Console:
    return Console(file=io.StringIO(), record=True, force_terminal=False, width=120)


class TestInteractivePlanLoop:
    def test_approve_on_first_plan(self, mocker: Any) -> None:
        client = _FakeClient(plan_texts=["Initial plan."])
        mocker.patch("gdr.core.planning.typer.prompt", return_value="A")
        plan_id = interactive_plan_loop(
            client,  # type: ignore[arg-type]
            initial_query="Research TPUs",
            agent="deep-research-preview-04-2026",
            console=_capture_console(),
        )
        assert plan_id == "plan-0"
        assert len(client.interactions.create_calls) == 1

    def test_cancel_exits_without_id(self, mocker: Any) -> None:
        client = _FakeClient(plan_texts=["Initial plan."])
        mocker.patch("gdr.core.planning.typer.prompt", return_value="C")
        plan_id = interactive_plan_loop(
            client,  # type: ignore[arg-type]
            initial_query="Q",
            agent="a",
            console=_capture_console(),
        )
        assert plan_id is None

    def test_refine_then_approve(self, mocker: Any) -> None:
        client = _FakeClient(plan_texts=["Initial plan.", "Refined plan."])
        # Prompt calls in order: decision=R, feedback="focus on 2024", decision=A
        mocker.patch(
            "gdr.core.planning.typer.prompt",
            side_effect=["R", "focus on 2024", "A"],
        )
        plan_id = interactive_plan_loop(
            client,  # type: ignore[arg-type]
            initial_query="Q",
            agent="a",
            console=_capture_console(),
        )
        assert plan_id == "plan-1"
        # Second plan must carry the previous_interaction_id.
        assert client.interactions.create_calls[1]["previous_interaction_id"] == "plan-0"
        # And the feedback became the new input.
        assert client.interactions.create_calls[1]["input"] == "focus on 2024"

    def test_refine_with_empty_feedback_reprompts(self, mocker: Any) -> None:
        client = _FakeClient(plan_texts=["Initial plan."])
        # Decision=R, feedback="" (empty), decision=A → should not create a
        # second plan interaction.
        mocker.patch("gdr.core.planning.typer.prompt", side_effect=["R", "", "A"])
        plan_id = interactive_plan_loop(
            client,  # type: ignore[arg-type]
            initial_query="Q",
            agent="a",
            console=_capture_console(),
        )
        assert plan_id == "plan-0"
        # Only the initial plan was created — empty feedback doesn't cost a round trip.
        assert len(client.interactions.create_calls) == 1

    def test_raises_when_plan_has_no_id(self, mocker: Any) -> None:
        client = _FakeClient(plan_texts=["Plan text."])

        def broken_create(**_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(id=None, status="in_progress")

        client.interactions.create = broken_create  # type: ignore[method-assign]

        mocker.patch("gdr.core.planning.typer.prompt", return_value="A")
        with pytest.raises(GdrError):
            interactive_plan_loop(
                client,  # type: ignore[arg-type]
                initial_query="Q",
                agent="a",
                console=_capture_console(),
            )


# ---------------------------------------------------------------------------
# Decision enum round-trip
# ---------------------------------------------------------------------------


class TestPlanDecision:
    def test_enum_values_stable(self) -> None:
        assert PlanDecision.APPROVE.value == "approve"
        assert PlanDecision.REFINE.value == "refine"
        assert PlanDecision.CANCEL.value == "cancel"
