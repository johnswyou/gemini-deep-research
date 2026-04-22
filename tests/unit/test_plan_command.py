"""Tests for ``gdr plan refine`` and ``gdr plan approve``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gdr.cli import app

# ---------------------------------------------------------------------------
# Fixtures and helpers (isolation + SDK mock)
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GDR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GDR_CONFIG_PATH", str(tmp_path / "no-such-config.toml"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


def _write_config(tmp_path: Path, *, output_dir: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'output_dir = "{output_dir}"\nauto_open = false\nconfirm_max = true\n',
        encoding="utf-8",
    )
    return path


def _fake_plan_interaction(
    id_: str = "plan-xyz", text: str = "## Plan\n1. Step."
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        status="completed",
        outputs=[SimpleNamespace(type="text", text=text, annotations=[])],
        usage=SimpleNamespace(total_tokens=100),
    )


def _fake_completed(id_: str = "run-xyz") -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        status="completed",
        outputs=[
            SimpleNamespace(
                type="text",
                text="Full report body.",
                annotations=[{"type": "url_citation", "url": "https://x", "title": "X"}],
            )
        ],
        usage=SimpleNamespace(total_tokens=5000),
    )


def _install_fake_sdk(mocker: Any, *, create_returns: Any, get_returns: Any) -> MagicMock:
    fake_interactions = MagicMock()
    fake_interactions.create.return_value = create_returns
    fake_interactions.get.return_value = get_returns
    fake_client = MagicMock()
    fake_client.interactions = fake_interactions
    mocker.patch("google.genai.Client", return_value=fake_client)
    return fake_interactions


# ---------------------------------------------------------------------------
# gdr plan refine
# ---------------------------------------------------------------------------


class TestRefine:
    def test_refine_creates_new_plan_and_prints_new_id(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake = _install_fake_sdk(
            mocker,
            create_returns=SimpleNamespace(id="plan-new-001", status="in_progress"),
            get_returns=_fake_plan_interaction(id_="plan-new-001", text="Refined plan."),
        )

        result = runner.invoke(
            app,
            [
                "plan",
                "refine",
                "plan-old-000",
                "Focus on 2024 and drop the methodology section.",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "plan-new-001" in result.output
        assert "Refined plan." in result.output
        # The create call should carry the old plan id as previous_interaction_id
        # and the feedback as the new input.
        call_kwargs = fake.create.call_args.kwargs
        assert call_kwargs["previous_interaction_id"] == "plan-old-000"
        assert "Focus on 2024" in call_kwargs["input"]
        # And it must enable collaborative_planning.
        assert call_kwargs["agent_config"]["collaborative_planning"] is True

    def test_refine_errors_without_api_key(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        mocker.patch("google.genai.Client")

        result = runner.invoke(
            app,
            ["plan", "refine", "plan-abc", "feedback", "--config", str(cfg)],
        )
        assert result.exit_code == 4


# ---------------------------------------------------------------------------
# gdr plan approve
# ---------------------------------------------------------------------------


class TestApprove:
    def test_approve_kicks_off_full_research(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake = _install_fake_sdk(
            mocker,
            create_returns=SimpleNamespace(id="run-approved-001", status="in_progress"),
            get_returns=_fake_completed(id_="run-approved-001"),
        )

        result = runner.invoke(
            app,
            [
                "plan",
                "approve",
                "plan-xyz-789",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )

        assert result.exit_code == 0, result.output
        call_kwargs = fake.create.call_args.kwargs
        # Must reference the approved plan.
        assert call_kwargs["previous_interaction_id"] == "plan-xyz-789"
        # Must turn OFF collaborative_planning (this is the execute step).
        assert call_kwargs["agent_config"]["collaborative_planning"] is False
        # And use the confirmation input.
        assert call_kwargs["input"] == "Plan looks good!"

    def test_approve_dry_run_does_not_hit_api(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        mock_client_ctor = mocker.patch("google.genai.Client")

        result = runner.invoke(
            app,
            [
                "plan",
                "approve",
                "plan-xyz-789",
                "--dry-run",
                "--config",
                str(cfg),
            ],
        )
        assert result.exit_code == 0, result.output
        mock_client_ctor.assert_not_called()
        # Dry-run output should show the approve request shape.
        assert '"previous_interaction_id"' in result.output
        assert "plan-xyz-789" in result.output
        assert '"Plan looks good!"' in result.output

    def test_approve_custom_query_used_for_slug(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        _install_fake_sdk(
            mocker,
            create_returns=SimpleNamespace(id="run-approved-002", status="in_progress"),
            get_returns=_fake_completed(id_="run-approved-002"),
        )

        result = runner.invoke(
            app,
            [
                "plan",
                "approve",
                "plan-xyz-789",
                "--query",
                "TPU history research",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        # The slug in the output directory should reflect the --query label.
        runs = list((tmp_path / "reports").glob("*tpu*"))
        assert len(runs) == 1

    def test_approve_default_slug_falls_back_to_short_id(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        _install_fake_sdk(
            mocker,
            create_returns=SimpleNamespace(id="run-approved-003", status="in_progress"),
            get_returns=_fake_completed(id_="run-approved-003"),
        )

        result = runner.invoke(
            app,
            [
                "plan",
                "approve",
                "plan-abcdef-0",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        # Default slug contains the 6-char fragment of the plan id.
        runs = list((tmp_path / "reports").glob("*approved-plan-planab*"))
        assert len(runs) == 1


# ---------------------------------------------------------------------------
# Help text sanity
# ---------------------------------------------------------------------------


class TestHelp:
    def test_plan_top_level_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["plan", "--help"])
        assert result.exit_code == 0
        assert "refine" in result.output
        assert "approve" in result.output

    def test_refine_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["plan", "refine", "--help"])
        assert result.exit_code == 0
        assert "plan" in result.output.lower()

    def test_approve_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["plan", "approve", "--help"])
        assert result.exit_code == 0
