"""End-to-end tests for the `gdr research` command.

These exercise the full command path with the google-genai SDK mocked at the
``google.genai.Client`` boundary. No network; no real filesystem outside
``tmp_path``; no real polling delays.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gdr.cli import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, *, output_dir: Path, extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'output_dir = "{output_dir}"\nauto_open = false\nconfirm_max = true\n{extra}',
        encoding="utf-8",
    )
    return path


def _install_fake_sdk(
    mocker: Any,
    *,
    created: Any,
    got: Any,
) -> MagicMock:
    fake_interactions = MagicMock()
    fake_interactions.create.return_value = created
    fake_interactions.get.return_value = got
    fake_client = MagicMock()
    fake_client.interactions = fake_interactions
    mocker.patch("google.genai.Client", return_value=fake_client)
    return fake_interactions


def _fake_completed(
    *, id_: str = "int-abc123-xyz", text: str = "A body paragraph."
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        status="completed",
        outputs=[
            SimpleNamespace(
                type="text",
                text=text,
                annotations=[{"type": "url_citation", "url": "https://a.example", "title": "A"}],
            ),
        ],
        usage=SimpleNamespace(total_tokens=1234, input_tokens=1000, output_tokens=234),
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Isolate every test from real filesystem state/config locations.
    monkeypatch.setenv("GDR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GDR_CONFIG_PATH", str(tmp_path / "no-such-config.toml"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_prints_kwargs_and_does_not_call_api(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        mock_client_ctor = mocker.patch("google.genai.Client")

        result = runner.invoke(
            app,
            ["research", "test query", "--config", str(cfg), "--dry-run"],
        )

        assert result.exit_code == 0
        assert "Dry run" in result.output
        # Must NOT have tried to build a SDK client.
        mock_client_ctor.assert_not_called()
        # Output should include the query.
        assert '"test query"' in result.output or "test query" in result.output


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_completes_and_writes_all_artifacts(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        reports_dir = tmp_path / "reports"
        cfg = _write_config(tmp_path, output_dir=reports_dir)

        # Use an id without dashes so the id6 fragment is predictable.
        created = SimpleNamespace(id="intabcxyz123", status="in_progress")
        completed = _fake_completed(id_="intabcxyz123")
        _install_fake_sdk(mocker, created=created, got=completed)

        result = runner.invoke(
            app,
            [
                "research",
                "Research TPUs",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
            ],
        )

        assert result.exit_code == 0, result.output
        runs = list(reports_dir.glob("*_intabc"))
        assert len(runs) == 1, f"Expected exactly one run dir under {reports_dir}"
        run_dir = runs[0]

        assert (run_dir / "report.md").is_file()
        assert (run_dir / "sources.json").is_file()
        assert (run_dir / "metadata.json").is_file()
        assert (run_dir / "transcript.json").is_file()

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        assert "# Research TPUs" in report
        assert "A body paragraph." in report
        assert "[A](https://a.example)" in report

        metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["interaction_id"] == "intabcxyz123"
        assert metadata["status"] == "completed"
        assert metadata["usage"]["total_tokens"] == 1234

        # Local store should have a record now.
        store_file = tmp_path / "state" / "interactions.jsonl"
        assert store_file.is_file()
        assert "intabcxyz123" in store_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# missing API key
# ---------------------------------------------------------------------------


class TestMissingApiKey:
    def test_clear_error_when_key_absent(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        # Note: config does NOT set api_key and env var is cleared by the fixture.
        # Also mock google.genai.Client so if we reach it we'd fail loudly.
        mocker.patch("google.genai.Client")

        result = runner.invoke(
            app,
            ["research", "A query", "--config", str(cfg)],
        )

        assert result.exit_code == 4  # ConfigError
        assert "API key" in result.output or "GEMINI_API_KEY" in result.output


# ---------------------------------------------------------------------------
# --max behavior
# ---------------------------------------------------------------------------


class TestMaxAgentConfirmation:
    def test_no_confirm_bypasses_prompt(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        created = SimpleNamespace(id="intmaxxyz", status="in_progress")
        _install_fake_sdk(mocker, created=created, got=_fake_completed(id_="intmaxxyz"))

        result = runner.invoke(
            app,
            [
                "research",
                "Expensive query",
                "--max",
                "--no-confirm",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
            ],
        )

        assert result.exit_code == 0, result.output
        runs = list((tmp_path / "reports").glob("*_intmax"))
        assert len(runs) == 1

    def test_max_without_no_confirm_is_prompted(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        mocker.patch("google.genai.Client")

        # Send "n" to the prompt; command should abort cleanly with exit 0.
        result = runner.invoke(
            app,
            [
                "research",
                "Expensive query",
                "--max",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
            ],
            input="n\n",
        )
        assert result.exit_code == 0
        assert "Aborted" in result.output or "Heads up" in result.output


# ---------------------------------------------------------------------------
# --output override
# ---------------------------------------------------------------------------


class TestOutputOverride:
    def test_exact_output_dir_honored(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        target = tmp_path / "reports" / "my-custom-run"
        _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intcustxyz", status="in_progress"),
            got=_fake_completed(id_="intcustxyz"),
        )

        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
                "--output",
                str(target),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (target / "report.md").is_file()


# ---------------------------------------------------------------------------
# --stream / --no-stream
# ---------------------------------------------------------------------------


def _streaming_events(interaction_id: str) -> list[dict[str, Any]]:
    """A minimal SSE event sequence equivalent to fixtures/streams/happy_path."""
    return [
        {
            "event_type": "interaction.start",
            "interaction": {"id": interaction_id, "status": "in_progress"},
        },
        {"event_type": "content.start", "index": 0, "content": {"type": "text"}},
        {
            "event_type": "content.delta",
            "index": 0,
            "delta": {"type": "text", "text": "Streamed body."},
        },
        {"event_type": "content.stop", "index": 0},
        {
            "event_type": "interaction.complete",
            "interaction": {"id": interaction_id, "status": "completed"},
        },
    ]


class TestStreaming:
    def test_stream_flag_consumes_iterator_and_writes_artifacts(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        # `create(stream=True)` returns an iterator; `.get(id=...)` returns the
        # authoritative terminal interaction.
        _install_fake_sdk(
            mocker,
            created=iter(_streaming_events("intstream123")),
            got=_fake_completed(id_="intstream123"),
        )

        result = runner.invoke(
            app,
            [
                "research",
                "Streaming query",
                "--stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
            ],
        )
        assert result.exit_code == 0, result.output

        runs = list((tmp_path / "reports").glob("*_intstr*"))
        assert len(runs) == 1
        assert (runs[0] / "report.md").is_file()

    def test_disconnect_mid_stream_falls_through_to_polling(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")

        def flaky_stream() -> Any:
            yield {
                "event_type": "interaction.start",
                "interaction": {"id": "intflaky789", "status": "in_progress"},
            }
            yield {
                "event_type": "content.start",
                "index": 0,
                "content": {"type": "text"},
            }
            raise ConnectionError("simulated TCP drop")

        _install_fake_sdk(
            mocker,
            created=flaky_stream(),
            got=_fake_completed(id_="intflaky789"),
        )

        result = runner.invoke(
            app,
            [
                "research",
                "Flaky query",
                "--stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
            ],
        )
        assert result.exit_code == 0, result.output
        # Even with a disconnect, we fall through to polling and write artifacts.
        runs = list((tmp_path / "reports").glob("*_intfla*"))
        assert len(runs) == 1
        assert (runs[0] / "report.md").is_file()

    def test_stream_error_event_exits_nonzero(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        events: list[dict[str, Any]] = [
            {
                "event_type": "interaction.start",
                "interaction": {"id": "interr456", "status": "in_progress"},
            },
            {
                "event_type": "error",
                "error": {"code": "RATE_LIMITED", "message": "Quota exceeded."},
            },
        ]
        _install_fake_sdk(
            mocker,
            created=iter(events),
            got=_fake_completed(id_="interr456"),
        )

        result = runner.invoke(
            app,
            [
                "research",
                "Quota-exceeded query",
                "--stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
            ],
        )
        assert result.exit_code == 1  # StreamError maps to exit code 1
        assert "RATE_LIMITED" in result.output or "Quota exceeded" in result.output

    def test_no_stream_flag_uses_polling_path(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        # With --no-stream, create() should NOT be called with stream=True.
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake_interactions = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intnostr", status="in_progress"),
            got=_fake_completed(id_="intnostr"),
        )
        result = runner.invoke(
            app,
            [
                "research",
                "Plain query",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-1234567890",
            ],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = fake_interactions.create.call_args.kwargs
        assert call_kwargs.get("stream") is not True
