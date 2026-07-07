"""Command-level regression tests for defects found in the 2026-07 review.

Each test here pins a behavior that previously shipped broken:

* ``--max`` must put the Max agent id on the wire.
* ``--output`` outside the configured root must work (previously the paid
  run completed and THEN died on confinement, unrendered and unrecorded).
* A run that failed fast must exit 1, not 0 — and still write artifacts.
* The local record must exist from the moment the run is addressable, so
  interrupts and timeouts leave something for ``gdr resume``.
* ``store=True`` and config-declared MCP servers must reach the request.
* A malformed config file must exit 4 with a message, not a traceback.
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
from gdr.constants import AGENT_MAX
from gdr.core.persistence import JsonlStore

# ---------------------------------------------------------------------------
# Harness (mirrors test_research_command.py)
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, *, output_dir: Path, extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'output_dir = "{output_dir}"\nauto_open = false\nconfirm_max = true\n{extra}',
        encoding="utf-8",
    )
    return path


def _install_fake_sdk(mocker: Any, *, created: Any, got: Any) -> MagicMock:
    fake_interactions = MagicMock()
    fake_interactions.create.return_value = created
    fake_interactions.get.return_value = got
    fake_client = MagicMock()
    fake_client.interactions = fake_interactions
    mocker.patch("google.genai.Client", return_value=fake_client)
    return fake_interactions


def _completed(id_: str = "intabcxyz123", *, status: str = "completed") -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        status=status,
        outputs=[SimpleNamespace(type="text", text="Body.", annotations=[])],
        usage=SimpleNamespace(total_tokens=42),
    )


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


def _store(tmp_path: Path) -> JsonlStore:
    return JsonlStore.open(tmp_path / "state" / "interactions.jsonl")


_KEY = "AIzaSy-test-key-1234567890"


# ---------------------------------------------------------------------------
# Wire-shape regressions
# ---------------------------------------------------------------------------


class TestWireShape:
    def test_max_flag_selects_max_agent_on_the_wire(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake = _install_fake_sdk(mocker, created=_completed(), got=_completed())

        result = runner.invoke(
            app,
            [
                "research",
                "q",
                "--config",
                str(cfg),
                "--api-key",
                _KEY,
                "--max",
                "--no-confirm",
                "--no-stream",
            ],
        )

        assert result.exit_code == 0
        assert fake.create.call_args.kwargs["agent"] == AGENT_MAX

    def test_store_true_is_sent(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake = _install_fake_sdk(mocker, created=_completed(), got=_completed())

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--no-stream"],
        )

        assert result.exit_code == 0
        assert fake.create.call_args.kwargs["store"] is True

    def test_config_declared_mcp_servers_reach_the_request(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(
            tmp_path,
            output_dir=tmp_path / "reports",
            extra=(
                "[mcp_servers.deploys]\n"
                'url = "https://mcp.example.com"\n'
                'headers.Authorization = "Bearer abc"\n'
            ),
        )
        fake = _install_fake_sdk(mocker, created=_completed(), got=_completed())

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--no-stream"],
        )

        assert result.exit_code == 0
        tools = fake.create.call_args.kwargs["tools"]
        mcp_entries = [t for t in tools if t.get("type") == "mcp_server"]
        assert mcp_entries == [
            {
                "type": "mcp_server",
                "name": "deploys",
                "url": "https://mcp.example.com",
                "headers": {"Authorization": "Bearer abc"},
            }
        ]


# ---------------------------------------------------------------------------
# --output contract
# ---------------------------------------------------------------------------


class TestOutputOverride:
    def test_output_outside_configured_root_writes_artifacts(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "root")
        _install_fake_sdk(mocker, created=_completed(), got=_completed())
        elsewhere = tmp_path / "elsewhere" / "run"

        result = runner.invoke(
            app,
            [
                "research",
                "q",
                "--config",
                str(cfg),
                "--api-key",
                _KEY,
                "--no-stream",
                "--output",
                str(elsewhere),
            ],
        )

        assert result.exit_code == 0
        assert (elsewhere / "report.md").is_file()


# ---------------------------------------------------------------------------
# Terminal-status exit codes + record lifecycle
# ---------------------------------------------------------------------------


class TestTerminalStates:
    def test_fast_failed_run_exits_1_and_still_writes_artifacts(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        failed = SimpleNamespace(id="intfailfast1", status="failed", outputs=[], usage=None)
        _install_fake_sdk(mocker, created=failed, got=failed)

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--no-stream"],
        )

        assert result.exit_code == 1
        record = _store(tmp_path).find_by_id("intfailfast1")
        assert record is not None
        assert record.status == "failed"
        metadata_files = list((tmp_path / "reports").rglob("metadata.json"))
        assert len(metadata_files) == 1
        assert json.loads(metadata_files[0].read_text())["status"] == "failed"

    def test_cancelled_run_exits_2(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        cancelled = SimpleNamespace(id="intcancel1", status="cancelled", outputs=[], usage=None)
        _install_fake_sdk(mocker, created=cancelled, got=cancelled)

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--no-stream"],
        )

        assert result.exit_code == 2

    def test_interrupted_stream_exits_130_and_leaves_in_progress_record(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")

        def _events() -> Any:
            yield {
                "event_type": "interaction.start",
                "interaction": {"id": "intctrlc1", "status": "in_progress"},
            }
            raise KeyboardInterrupt

        fake = _install_fake_sdk(mocker, created=_events(), got=_completed())
        _ = fake

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--stream"],
        )

        assert result.exit_code == 130
        assert "gdr resume intctrlc1" in result.output
        record = _store(tmp_path).find_by_id("intctrlc1")
        assert record is not None
        assert record.status == "in_progress"
        assert record.finished_at is None

    def test_completed_run_record_supersedes_in_progress_row(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        _install_fake_sdk(mocker, created=_completed(), got=_completed())

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--no-stream"],
        )

        assert result.exit_code == 0
        store = _store(tmp_path)
        record = store.find_by_id("intabcxyz123")
        assert record is not None
        assert record.status == "completed"
        assert record.finished_at is not None
        # Two rows on disk (in_progress + terminal), one live record.
        raw_lines = (tmp_path / "state" / "interactions.jsonl").read_text().strip().splitlines()
        assert len(raw_lines) == 2
        assert len(store) == 1


# ---------------------------------------------------------------------------
# Error boundary
# ---------------------------------------------------------------------------


class TestErrorBoundary:
    def test_malformed_config_exits_4_without_traceback(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text("output_dir = [broken\n", encoding="utf-8")

        result = runner.invoke(app, ["research", "q", "--config", str(bad), "--dry-run"])

        assert result.exit_code == 4
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Invalid TOML" in result.output

    def test_create_failure_exits_5(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        fake_interactions = MagicMock()
        fake_interactions.create.side_effect = RuntimeError("connection reset")
        fake_client = MagicMock()
        fake_client.interactions = fake_interactions
        mocker.patch("google.genai.Client", return_value=fake_client)

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--no-stream"],
        )

        assert result.exit_code == 5
        assert "Failed to start research" in result.output


# ---------------------------------------------------------------------------
# auto_open
# ---------------------------------------------------------------------------


class TestAutoOpen:
    def test_auto_open_launches_report_on_tty(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'output_dir = "{tmp_path / "reports"}"\nauto_open = true\n', encoding="utf-8"
        )
        _install_fake_sdk(mocker, created=_completed(), got=_completed())
        mocker.patch("gdr.commands.research.stdout_is_tty", return_value=True)
        launch = mocker.patch("gdr.commands.research.typer.launch")

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--no-stream"],
        )

        assert result.exit_code == 0
        launch.assert_called_once()
        assert launch.call_args.args[0].endswith("report.md")

    def test_auto_open_false_never_launches(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")  # auto_open = false
        _install_fake_sdk(mocker, created=_completed(), got=_completed())
        mocker.patch("gdr.commands.research.stdout_is_tty", return_value=True)
        launch = mocker.patch("gdr.commands.research.typer.launch")

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--no-stream"],
        )

        assert result.exit_code == 0
        launch.assert_not_called()


# ---------------------------------------------------------------------------
# Streamed usage fallback (2026-07 follow-up round)
# ---------------------------------------------------------------------------


class TestStreamedUsageFallback:
    def test_record_gets_tokens_from_stream_when_fetch_is_empty(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")

        events = [
            {
                "event_type": "interaction.start",
                "interaction": {"id": "intusage1", "status": "in_progress"},
            },
            {
                "event_type": "content.delta",
                "index": 0,
                "delta": {"type": "text", "text": "Streamed report body."},
            },
            {
                "event_type": "interaction.complete",
                "interaction": {
                    "id": "intusage1",
                    "status": "completed",
                    "usage": {"total_tokens": 12345},
                },
            },
        ]
        # Terminal fetch: completed but empty — the v0.1.2 hotfix scenario.
        empty_fetch = SimpleNamespace(id="intusage1", status="completed", outputs=[], usage=None)
        _install_fake_sdk(mocker, created=iter(events), got=empty_fetch)

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--stream"],
        )

        assert result.exit_code == 0
        record = _store(tmp_path).find_by_id("intusage1")
        assert record is not None
        assert record.total_tokens == 12345
        # The rendered report used the streamed fallback text.
        reports = list((tmp_path / "reports").rglob("report.md"))
        assert len(reports) == 1
        assert "Streamed report body." in reports[0].read_text()
        # metadata.json carries the fallback usage too.
        metadata = json.loads(next((tmp_path / "reports").rglob("metadata.json")).read_text())
        assert metadata["usage"] == {"total_tokens": 12345}

    def test_untrusted_flag_is_persisted_on_the_record(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        _install_fake_sdk(mocker, created=_completed(), got=_completed())

        result = runner.invoke(
            app,
            [
                "research",
                "q",
                "--config",
                str(cfg),
                "--api-key",
                _KEY,
                "--no-stream",
                "--untrusted-input",
            ],
        )

        assert result.exit_code == 0
        record = _store(tmp_path).find_by_id("intabcxyz123")
        assert record is not None
        assert record.untrusted is True


# ---------------------------------------------------------------------------
# Plaintext-MCP warning (2026-07 polish round)
# ---------------------------------------------------------------------------


class TestPlaintextMcpWarning:
    def test_http_mcp_with_headers_warns(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        _install_fake_sdk(mocker, created=_completed(), got=_completed())

        result = runner.invoke(
            app,
            [
                "research",
                "q",
                "--config",
                str(cfg),
                "--api-key",
                _KEY,
                "--no-stream",
                "--mcp",
                "local=http://mcp.internal:8080",
                "--mcp-header",
                "local=Authorization:Bearer abc",
            ],
        )

        assert result.exit_code == 0
        assert "sent unencrypted" in result.output

    def test_https_mcp_with_headers_does_not_warn(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        _install_fake_sdk(mocker, created=_completed(), got=_completed())

        result = runner.invoke(
            app,
            [
                "research",
                "q",
                "--config",
                str(cfg),
                "--api-key",
                _KEY,
                "--no-stream",
                "--mcp",
                "secure=https://mcp.example.com",
                "--mcp-header",
                "secure=Authorization:Bearer abc",
            ],
        )

        assert result.exit_code == 0
        assert "sent unencrypted" not in result.output


class TestStreamErrorRecovery:
    """A mid-stream ``error`` event must leave a recoverable trail.

    The run is recorded (in_progress) the moment the stream announces its
    interaction id — not after the stream ends — and the failure message
    says how to reattach. Previously the StreamError path exited before
    any record was written and the id was lost with it.
    """

    def test_stream_error_event_leaves_record_and_resume_hint(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        events = [
            {
                "event_type": "interaction.created",
                "interaction": {"id": "interrstream1", "status": "in_progress"},
            },
            {
                "event_type": "error",
                "error": {"code": "RATE_LIMITED", "message": "Quota exceeded."},
            },
        ]
        _install_fake_sdk(mocker, created=iter(events), got=_completed(id_="interrstream1"))

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--stream"],
        )

        assert result.exit_code == 1
        assert "gdr resume interrstream1" in result.output
        record = _store(tmp_path).find_by_id("interrstream1")
        assert record is not None
        assert record.status == "in_progress"

    def test_streamed_clean_run_writes_exactly_two_record_rows(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        # in_progress at interaction.created + terminal at the end — the
        # post-stream path must not append a redundant third row.
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        events = [
            {
                "event_type": "interaction.created",
                "interaction": {"id": "intstreamrec1", "status": "in_progress"},
            },
            {
                "event_type": "interaction.completed",
                "interaction": {"id": "intstreamrec1", "status": "completed"},
            },
        ]
        _install_fake_sdk(mocker, created=iter(events), got=_completed(id_="intstreamrec1"))

        result = runner.invoke(
            app,
            ["research", "q", "--config", str(cfg), "--api-key", _KEY, "--stream"],
        )

        assert result.exit_code == 0, result.output
        raw_lines = (tmp_path / "state" / "interactions.jsonl").read_text().strip().splitlines()
        assert len(raw_lines) == 2
        record = _store(tmp_path).find_by_id("intstreamrec1")
        assert record is not None
        assert record.status == "completed"


class TestSmallBehaviors:
    def test_plaintext_mcp_warning_suppressed_when_untrusted_strips_it(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        # Untrusted mode strips mcp_server from the request — warning
        # about credentials that will never be sent is just noise.
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        result = runner.invoke(
            app,
            [
                "research",
                "q",
                "--dry-run",
                "--untrusted-input",
                "--mcp",
                "deploys=http://mcp.example.com",
                "--mcp-header",
                "deploys=Authorization:Bearer abc",
                "--config",
                str(cfg),
                "--api-key",
                _KEY,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "stripped tools" in result.output
        assert "sent unencrypted" not in result.output

    def test_create_401_is_a_config_error_exit_4(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        # An invalid API key is an auth problem (documented exit 4), not
        # a network failure (exit 5).
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")

        class FakeAuthError(Exception):
            code = 401

        fake_interactions = MagicMock()
        fake_interactions.create.side_effect = FakeAuthError("API key not valid")
        fake_client = MagicMock()
        fake_client.interactions = fake_interactions
        mocker.patch("google.genai.Client", return_value=fake_client)

        result = runner.invoke(
            app,
            ["research", "q", "--no-stream", "--config", str(cfg), "--api-key", _KEY],
        )
        assert result.exit_code == 4
        assert "key" in result.output.lower()

    def test_stream_error_after_external_cancel_exits_2(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        # `gdr cancel` from another terminal kills the stream with a
        # generic api_error event; the run's real status is `cancelled`
        # and the exit code must say so (documented exit 2).
        cfg = _write_config(tmp_path, output_dir=tmp_path / "reports")
        events = [
            {
                "event_type": "interaction.created",
                "interaction": {"id": "intextcancel1", "status": "in_progress"},
            },
            {
                "event_type": "error",
                "error": {"code": "api_error", "message": "There was a problem."},
            },
        ]
        cancelled = SimpleNamespace(id="intextcancel1", status="cancelled", outputs=[], usage=None)
        _install_fake_sdk(mocker, created=iter(events), got=cancelled)

        result = runner.invoke(
            app,
            ["research", "q", "--stream", "--config", str(cfg), "--api-key", _KEY],
        )
        assert result.exit_code == 2
        assert "cancelled" in result.output.lower()
