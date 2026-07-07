"""Tests for ``gdr follow-up``."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gdr.cli import app
from gdr.core.models import Record
from gdr.core.persistence import JsonlStore


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GDR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GDR_CONFIG_PATH", str(tmp_path / "no-such.toml"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'output_dir = "{tmp_path / "reports"}"\nconfirm_max = false\n',
        encoding="utf-8",
    )
    return path


def _fake_completed(id_: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        status="completed",
        outputs=[SimpleNamespace(type="text", text="Follow-up answer.", annotations=[])],
        usage=SimpleNamespace(total_tokens=500),
    )


def _install_fake_sdk(mocker: Any, *, created: Any, got: Any) -> MagicMock:
    fake_interactions = MagicMock()
    fake_interactions.create.return_value = created
    fake_interactions.get.return_value = got
    fake_client = MagicMock()
    fake_client.interactions = fake_interactions
    mocker.patch("google.genai.Client", return_value=fake_client)
    return fake_interactions


class TestFollowUp:
    def test_passes_previous_id_and_question(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intfu-xyz", status="in_progress"),
            got=_fake_completed("intfu-xyz"),
        )

        result = runner.invoke(
            app,
            [
                "follow-up",
                "intparent-abc",
                "Elaborate on section 3.",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = fake.create.call_args.kwargs
        assert call_kwargs["previous_interaction_id"] == "intparent-abc"
        assert call_kwargs["input"] == "Elaborate on section 3."
        # Follow-up uses the execution agent_config (collaborative_planning=False).
        assert call_kwargs["agent_config"]["collaborative_planning"] is False

    def test_dry_run_skips_api(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path)
        mock_client_ctor = mocker.patch("google.genai.Client")
        result = runner.invoke(
            app,
            [
                "follow-up",
                "intparent-abc",
                "What about next quarter?",
                "--dry-run",
                "--config",
                str(cfg),
            ],
        )
        assert result.exit_code == 0, result.output
        mock_client_ctor.assert_not_called()
        assert "Dry run" in result.output
        assert "intparent-abc" in result.output

    def test_writes_artifacts_to_output_dir(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intfu-001", status="in_progress"),
            got=_fake_completed("intfu-001"),
        )
        result = runner.invoke(
            app,
            [
                "follow-up",
                "intparent-xyz",
                "Tell me more.",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        runs = list((tmp_path / "reports").glob("*_intfu*"))
        assert len(runs) == 1
        assert (runs[0] / "report.md").read_text(encoding="utf-8").startswith("# Tell me more.")


def _seed_parent_record(tmp_path: Path, *, untrusted: bool) -> str:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = JsonlStore.open(state_dir / "interactions.jsonl")
    record = Record(
        id="intparent1",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        status="completed",
        agent="deep-research-preview-04-2026",
        query="Parent query",
        output_dir=tmp_path / "reports" / "parent",
        untrusted=untrusted,
    )
    store.append(record)
    return record.id


class TestUntrustedInheritance:
    def test_follow_up_inherits_parent_untrusted_posture(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        parent_id = _seed_parent_record(tmp_path, untrusted=True)
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intfu-sec", status="in_progress"),
            got=_fake_completed("intfu-sec"),
        )

        result = runner.invoke(
            app,
            [
                "follow-up",
                parent_id,
                "Elaborate on section 3",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
            ],
        )

        assert result.exit_code == 0
        assert "inherited from the parent run" in result.output
        tools = fake.create.call_args.kwargs.get("tools", [])
        tool_types = {t["type"] for t in tools}
        assert "code_execution" not in tool_types
        assert "mcp_server" not in tool_types

    def test_follow_up_of_trusted_parent_keeps_full_tools(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        parent_id = _seed_parent_record(tmp_path, untrusted=False)
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intfu-ok", status="in_progress"),
            got=_fake_completed("intfu-ok"),
        )

        result = runner.invoke(
            app,
            [
                "follow-up",
                parent_id,
                "More please",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
            ],
        )

        assert result.exit_code == 0
        tool_types = {t["type"] for t in fake.create.call_args.kwargs.get("tools", [])}
        assert "code_execution" in tool_types


class TestModelFollowUp:
    def test_model_follow_up_sends_model_without_agent_or_tools(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intfu-model", status="in_progress"),
            got=_fake_completed("intfu-model"),
        )

        result = runner.invoke(
            app,
            [
                "follow-up",
                "intparent1",
                "Elaborate on point 2",
                "--model",
                "gemini-3.1-pro-preview",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
            ],
        )

        assert result.exit_code == 0
        kwargs = fake.create.call_args.kwargs
        assert kwargs["model"] == "gemini-3.1-pro-preview"
        assert "agent" not in kwargs
        assert "agent_config" not in kwargs
        assert "tools" not in kwargs
        assert kwargs["previous_interaction_id"] == "intparent1"

    def test_model_and_max_are_mutually_exclusive(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="x", status="in_progress"),
            got=_fake_completed("x"),
        )

        result = runner.invoke(
            app,
            [
                "follow-up",
                "intparent1",
                "q",
                "--model",
                "gemini-3.1-pro-preview",
                "--max",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
            ],
        )

        assert result.exit_code == 4
        assert "mutually exclusive" in result.output
