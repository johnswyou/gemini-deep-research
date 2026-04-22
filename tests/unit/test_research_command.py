"""End-to-end tests for the `gdr research` command.

These exercise the full command path with the google-genai SDK mocked at the
``google.genai.Client`` boundary. No network; no real filesystem outside
``tmp_path``; no real polling delays.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gdr.cli import app

# 1x1 transparent PNG, for --file round-trip tests.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_TINY_PNG_BYTES = base64.b64decode(_TINY_PNG_B64)

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


# ---------------------------------------------------------------------------
# --plan (collaborative planning)
# ---------------------------------------------------------------------------


def _fake_plan_interaction(
    id_: str = "planxyz", text: str = "1. Search sources\n2. Synthesize"
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        status="completed",
        outputs=[SimpleNamespace(type="text", text=text, annotations=[])],
        usage=SimpleNamespace(total_tokens=100),
    )


class _PlanAndRunSDK:
    """Mock ``client.interactions`` that routes plan vs execution calls.

    The command calls ``create`` for a plan (collaborative_planning=True)
    and again for the final run (collaborative_planning=False). We inspect
    the kwargs to decide which fixture to return.
    """

    def __init__(
        self,
        *,
        plan_ids: list[str],
        plan_texts: list[str],
        final_id: str,
        final_text: str = "Final report body.",
    ) -> None:
        self._plan_ids = plan_ids
        self._plan_texts = plan_texts
        self._final_id = final_id
        self._final_text = final_text
        self._plan_count = 0
        self.create_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.create_calls.append(kwargs)
        cp = kwargs["agent_config"]["collaborative_planning"]
        if cp:
            plan_id = self._plan_ids[min(self._plan_count, len(self._plan_ids) - 1)]
            self._plan_count += 1
            return SimpleNamespace(id=plan_id, status="in_progress")
        return SimpleNamespace(id=self._final_id, status="in_progress")

    def get(self, *, id: str) -> SimpleNamespace:
        self.get_calls.append(id)
        if id == self._final_id:
            return _fake_completed(id_=self._final_id)
        # plan lookup
        try:
            idx = self._plan_ids.index(id)
            text = self._plan_texts[min(idx, len(self._plan_texts) - 1)]
        except ValueError:
            text = "generic plan"
        return _fake_plan_interaction(id_=id, text=text)


def _install_plan_sdk(mocker: Any, sdk: _PlanAndRunSDK) -> None:
    fake_client = MagicMock()
    fake_client.interactions = sdk
    mocker.patch("google.genai.Client", return_value=fake_client)


class TestPlanFlag:
    def test_approve_on_first_plan(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        sdk = _PlanAndRunSDK(
            plan_ids=["planfirst"],
            plan_texts=["Initial plan."],
            final_id="runfinal",
        )
        _install_plan_sdk(mocker, sdk)
        mocker.patch("gdr.core.planning.typer.prompt", return_value="A")  # approve

        result = runner.invoke(
            app,
            [
                "research",
                "TPU history",
                "--plan",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output

        # Should have made 2 create calls: 1 plan + 1 execution.
        assert len(sdk.create_calls) == 2

        plan_call = sdk.create_calls[0]
        exec_call = sdk.create_calls[1]

        assert plan_call["agent_config"]["collaborative_planning"] is True
        assert plan_call["input"] == "TPU history"
        assert "previous_interaction_id" not in plan_call

        assert exec_call["agent_config"]["collaborative_planning"] is False
        assert exec_call["previous_interaction_id"] == "planfirst"
        assert exec_call["input"] == "Plan looks good!"

        # Output directory created for the final run.
        runs = list((tmp_path / "reports").glob("*_runfin*"))
        assert len(runs) == 1

    def test_refine_then_approve(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        sdk = _PlanAndRunSDK(
            plan_ids=["planinitial", "planrefined"],
            plan_texts=["Initial plan.", "Refined plan."],
            final_id="runfinal",
        )
        _install_plan_sdk(mocker, sdk)
        # R → "drop the methodology section" → A
        mocker.patch(
            "gdr.core.planning.typer.prompt",
            side_effect=["R", "drop the methodology section", "A"],
        )

        result = runner.invoke(
            app,
            [
                "research",
                "EV batteries",
                "--plan",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output

        # 3 creates: initial plan, refined plan, execution.
        assert len(sdk.create_calls) == 3
        initial_plan, refined_plan, exec_call = sdk.create_calls

        assert initial_plan["agent_config"]["collaborative_planning"] is True
        assert "previous_interaction_id" not in initial_plan

        assert refined_plan["agent_config"]["collaborative_planning"] is True
        assert refined_plan["previous_interaction_id"] == "planinitial"
        assert refined_plan["input"] == "drop the methodology section"

        assert exec_call["agent_config"]["collaborative_planning"] is False
        assert exec_call["previous_interaction_id"] == "planrefined"

    def test_cancel_exits_without_execution(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        sdk = _PlanAndRunSDK(
            plan_ids=["planabandoned"],
            plan_texts=["Initial plan."],
            final_id="(never)",
        )
        _install_plan_sdk(mocker, sdk)
        mocker.patch("gdr.core.planning.typer.prompt", return_value="C")

        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--plan",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0
        # Only the plan create happened — no execution.
        assert len(sdk.create_calls) == 1
        # No report directory.
        assert (
            not list((tmp_path / "reports").iterdir()) if (tmp_path / "reports").exists() else True
        )

    def test_plan_dry_run_prints_plan_kwargs_and_skips_api(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        mock_client_ctor = mocker.patch("google.genai.Client")
        # No prompt mock needed — --dry-run bypasses the interactive loop.

        result = runner.invoke(
            app,
            [
                "research",
                "Some topic",
                "--plan",
                "--dry-run",
                "--config",
                str(cfg),
            ],
        )
        assert result.exit_code == 0
        mock_client_ctor.assert_not_called()
        # Dry-run output reflects the plan-phase request shape.
        assert (
            '"collaborative_planning": true' in result.output.lower()
            or '"collaborative_planning":true' in result.output.lower()
            or "collaborative_planning" in result.output
        )


# ---------------------------------------------------------------------------
# Phase 6: --tool / --mcp / --mcp-header / --file / --url / --file-search-store /
# --visualization / --untrusted-input
# ---------------------------------------------------------------------------


class TestToolFlag:
    def test_tool_flag_overrides_defaults(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intttool01", status="in_progress"),
            got=_fake_completed(id_="intttool01"),
        )
        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--tool",
                "google_search",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = fake.create.call_args.kwargs
        tool_types = [t["type"] for t in call_kwargs["tools"]]
        # Only google_search — defaults (url_context, code_execution) are dropped.
        assert tool_types == ["google_search"]

    def test_configured_tool_name_rejected_with_hint(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        mocker.patch("google.genai.Client")
        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--tool",
                "file_search",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 4
        assert "--file-search-store" in result.output


class TestMcpFlag:
    def test_mcp_adds_server_tool_with_header(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intmcp01", status="in_progress"),
            got=_fake_completed(id_="intmcp01"),
        )
        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--mcp",
                "deploys=https://mcp.example.com",
                "--mcp-header",
                "deploys=Authorization:Bearer test-token",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = fake.create.call_args.kwargs
        mcp_tool = next(t for t in call_kwargs["tools"] if t["type"] == "mcp_server")
        assert mcp_tool["name"] == "deploys"
        assert mcp_tool["url"] == "https://mcp.example.com"
        assert mcp_tool["headers"] == {"Authorization": "Bearer test-token"}

    def test_mcp_header_injection_rejected(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        mock_client_ctor = mocker.patch("google.genai.Client")
        # CR/LF in header value = injection attempt → ConfigError → exit 4.
        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--mcp",
                "bad=https://mcp.example.com",
                "--mcp-header",
                "bad=X-Custom:line1\r\nX-Evil: yes",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 4
        # Client should never have been built (and certainly never called).
        mock_client_ctor.assert_not_called()

    def test_mcp_header_for_unknown_server_rejected(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        mocker.patch("google.genai.Client")
        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--mcp",
                "known=https://k.example.com",
                "--mcp-header",
                "typo=Authorization:Bearer abc",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 4
        assert "unknown MCP server" in result.output or "typo" in result.output


class TestFileFlag:
    def test_file_attached_as_media_part(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        png = tmp_path / "tiny.png"
        png.write_bytes(_TINY_PNG_BYTES)
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intfile01", status="in_progress"),
            got=_fake_completed(id_="intfile01"),
        )
        result = runner.invoke(
            app,
            [
                "research",
                "Describe this image",
                "--file",
                str(png),
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = fake.create.call_args.kwargs
        assert isinstance(call_kwargs["input"], list)
        text_part = call_kwargs["input"][0]
        image_part = call_kwargs["input"][1]
        assert text_part == {"type": "text", "text": "Describe this image"}
        assert image_part["type"] == "image"
        assert image_part["mime_type"] == "image/png"
        assert image_part["data"] == _TINY_PNG_B64

    def test_missing_file_exits_four(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        mocker.patch("google.genai.Client")
        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--file",
                str(tmp_path / "nope.pdf"),
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 4
        assert "does not exist" in result.output or "nope.pdf" in result.output


class TestUrlFlag:
    def test_url_adds_url_context_tool_when_tools_overridden(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="inturl01", status="in_progress"),
            got=_fake_completed(id_="inturl01"),
        )
        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--tool",
                "google_search",  # deliberately excludes url_context
                "--url",
                "https://a.example/page",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = fake.create.call_args.kwargs
        tool_types = [t["type"] for t in call_kwargs["tools"]]
        # url_context auto-added even though user didn't pass it.
        assert "url_context" in tool_types
        # URLs appended as a TextPart after the main query.
        assert isinstance(call_kwargs["input"], list)
        url_text_parts = [p for p in call_kwargs["input"] if p.get("type") == "text"]
        assert any("https://a.example/page" in p["text"] for p in url_text_parts)


class TestFileSearchStoreFlag:
    def test_bare_store_name_prefixed(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intfs01", status="in_progress"),
            got=_fake_completed(id_="intfs01"),
        )
        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--file-search-store",
                "kb-2025",  # bare name
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = fake.create.call_args.kwargs
        fs_tool = next(t for t in call_kwargs["tools"] if t["type"] == "file_search")
        assert fs_tool["file_search_store_names"] == ["fileSearchStores/kb-2025"]


class TestVisualizationFlag:
    def test_off_flips_agent_config(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intvis01", status="in_progress"),
            got=_fake_completed(id_="intvis01"),
        )
        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--visualization",
                "off",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = fake.create.call_args.kwargs
        assert call_kwargs["agent_config"]["visualization"] == "off"

    def test_invalid_value_rejected(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        mocker.patch("google.genai.Client")
        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--visualization",
                "maybe",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 4


class TestUntrustedInputFlag:
    def test_strips_code_execution_and_mcp(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intuntr01", status="in_progress"),
            got=_fake_completed(id_="intuntr01"),
        )
        result = runner.invoke(
            app,
            [
                "research",
                "Q",
                "--tool",
                "google_search",
                "--tool",
                "code_execution",
                "--mcp",
                "svc=https://svc.example.com",
                "--untrusted-input",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = fake.create.call_args.kwargs
        tool_types = [t["type"] for t in call_kwargs["tools"]]
        assert "code_execution" not in tool_types
        assert "mcp_server" not in tool_types
        assert "google_search" in tool_types
        # The CLI should have surfaced the stripped warning to the user.
        assert "stripped tools" in result.output.lower()

    def test_auto_untrusted_triggered_by_file_flag(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        """By default (safe_untrusted=true), attaching --file should flip
        the policy into untrusted mode even without --untrusted-input."""
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        png = tmp_path / "pic.png"
        png.write_bytes(_TINY_PNG_BYTES)
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intauto01", status="in_progress"),
            got=_fake_completed(id_="intauto01"),
        )
        result = runner.invoke(
            app,
            [
                "research",
                "Describe",
                "--file",
                str(png),
                "--tool",
                "google_search",
                "--tool",
                "code_execution",  # should get stripped
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXXXXXXXXXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = fake.create.call_args.kwargs
        tool_types = [t["type"] for t in call_kwargs["tools"]]
        assert "code_execution" not in tool_types
