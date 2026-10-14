from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ai_powerbi_analyst.powerbi.cli import (
    AuthoringCliAdapter,
    CommandFailed,
    CommandReceipt,
    PbiCliAdapter,
    SubprocessRunner,
    discover_capabilities,
)

PBI_EXECUTABLE = str(Path("/tools/pbi"))
AUTHORING_EXECUTABLE = str(Path("/tools/powerbi-report-author"))


class RecordingRunner:
    def __init__(self, receipts: list[CommandReceipt] | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], Path | None, float]] = []
        self.receipts = list(receipts or [])

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
    ) -> CommandReceipt:
        self.calls.append((argv, cwd, timeout))
        if self.receipts:
            return self.receipts.pop(0)
        return CommandReceipt(
            argv=argv,
            cwd=cwd,
            returncode=0,
            stdout=json.dumps({"ok": True}),
            stderr="",
            parsed_json={"ok": True},
            duration_ms=1,
        )


def test_subprocess_runner_uses_literal_argument_array(tmp_path: Path) -> None:
    sentinel = tmp_path / "must-not-exist"
    hostile_argument = f"hello; touch {sentinel}"

    receipt = SubprocessRunner().run(
        (
            sys.executable,
            "-c",
            "import json, sys; print(json.dumps(sys.argv[1:]))",
            hostile_argument,
        )
    )

    assert receipt.succeeded
    assert receipt.parsed_json == [hostile_argument]
    assert not sentinel.exists()


def test_subprocess_runner_returns_typed_failure_receipt() -> None:
    receipt = SubprocessRunner().run(
        (sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)")
    )

    assert not receipt.succeeded
    assert receipt.returncode == 7
    assert receipt.stderr.strip() == "bad"
    with pytest.raises(CommandFailed, match="exit code 7"):
        receipt.require_success()


def test_subprocess_runner_rejects_empty_or_nul_arguments() -> None:
    with pytest.raises(ValueError, match="argv"):
        SubprocessRunner().run(())
    with pytest.raises(ValueError, match="NUL"):
        SubprocessRunner().run((sys.executable, "bad\x00value"))


def test_pbi_cli_adapter_builds_no_sync_json_argument_arrays(tmp_path: Path) -> None:
    report = tmp_path / "Sales.Report"
    runner = RecordingRunner()
    adapter = PbiCliAdapter(executable=Path("/tools/pbi"), runner=runner)

    adapter.add_page(report, page_id="a" * 20, display_name="Executive Pulse")
    adapter.add_visual(
        report,
        page_id="a" * 20,
        visual_id="b" * 20,
        visual_type="cardVisual",
        x=32,
        y=32,
        width=280,
        height=120,
    )
    adapter.bind_visual(
        report,
        page_id="a" * 20,
        visual_id="b" * 20,
        bindings=(("field", "Sales[Net Sales]"),),
    )
    adapter.delete_visual(
        report,
        page_id="a" * 20,
        visual_id="b" * 20,
    )

    assert runner.calls[0][0] == (
        PBI_EXECUTABLE,
        "--json",
        "report",
        "--path",
        str(report.resolve()),
        "--no-sync",
        "add-page",
        "--display-name",
        "Executive Pulse",
        "--name",
        "a" * 20,
        "--width",
        "1280",
        "--height",
        "720",
    )
    assert runner.calls[1][0][:7] == (
        PBI_EXECUTABLE,
        "--json",
        "visual",
        "--path",
        str(report.resolve()),
        "--no-sync",
        "add",
    )
    assert runner.calls[2][0][-5:] == (
        "b" * 20,
        "--field",
        "Sales[Net Sales]",
        "--page",
        "a" * 20,
    )
    assert runner.calls[3][0][-4:] == (
        "delete",
        "b" * 20,
        "--page",
        "a" * 20,
    )


def test_validation_adapters_return_receipts_without_interpreting_shell_text(
    tmp_path: Path,
) -> None:
    report = tmp_path / "Sales.Report"
    runner = RecordingRunner()

    pbi_receipt = PbiCliAdapter(Path("/tools/pbi"), runner).validate(report, full=True)
    official_receipt = AuthoringCliAdapter(Path("/tools/powerbi-report-author"), runner).validate(
        report
    )

    assert pbi_receipt.argv == (
        PBI_EXECUTABLE,
        "--json",
        "report",
        "--path",
        str(report.resolve()),
        "validate",
        "--full",
    )
    assert official_receipt.argv == (
        AUTHORING_EXECUTABLE,
        "validate",
        str(report.resolve()),
    )


def test_pbi_cli_adapter_builds_exact_theme_and_container_commands(
    tmp_path: Path,
) -> None:
    report = tmp_path / "Sales.Report"
    theme = tmp_path / "ExecutiveLight-7f3a2c1d.json"
    runner = RecordingRunner()
    adapter = PbiCliAdapter(Path("/tools/pbi"), runner)

    adapter.set_theme(report, theme_path=theme)
    adapter.set_visual_container(
        report,
        page_id="a" * 20,
        visual_id="b" * 20,
        title="Monthly Net Sales trend",
    )

    assert runner.calls[0][0] == (
        PBI_EXECUTABLE,
        "--json",
        "report",
        "--path",
        str(report.resolve()),
        "--no-sync",
        "set-theme",
        "--file",
        str(theme.resolve()),
    )
    assert runner.calls[1][0] == (
        PBI_EXECUTABLE,
        "--json",
        "visual",
        "--path",
        str(report.resolve()),
        "--no-sync",
        "set-container",
        "b" * 20,
        "--page",
        "a" * 20,
        "--border-show",
        "true",
        "--background-show",
        "true",
        "--title",
        "Monthly Net Sales trend",
    )


def test_capability_discovery_fails_closed_when_a_validator_is_missing() -> None:
    version_receipt = CommandReceipt(
        argv=("/tools/pbi", "--version"),
        cwd=None,
        returncode=0,
        stdout="pbi-cli, version 3.11.1\n",
        stderr="",
        parsed_json=None,
        duration_ms=1,
    )
    runner = RecordingRunner([version_receipt])

    capabilities = discover_capabilities(
        runner=runner,
        locator=lambda name: "/tools/pbi" if name == "pbi" else None,
        platform_name="Darwin",
    )

    assert capabilities.pbi_cli.available
    assert capabilities.pbi_cli.version == "3.11.1"
    assert not capabilities.authoring_cli.available
    assert not capabilities.can_compose
    assert not capabilities.desktop_render_verified
    assert "Microsoft report-authoring CLI is unavailable" in capabilities.blockers


def test_capability_discovery_requires_exact_global_cli_names() -> None:
    receipts = [
        CommandReceipt(
            argv=("/tools/pbi", "--version"),
            cwd=None,
            returncode=0,
            stdout="pbi-cli, version 3.11.1\n",
            stderr="",
            parsed_json=None,
            duration_ms=1,
        ),
        CommandReceipt(
            argv=("/tools/powerbi-report-author", "--version"),
            cwd=None,
            returncode=0,
            stdout="0.1.4\n",
            stderr="",
            parsed_json=None,
            duration_ms=1,
        ),
    ]
    runner = RecordingRunner(receipts)
    located = {
        "pbi": "/tools/pbi",
        "powerbi-report-author": "/tools/powerbi-report-author",
    }

    capabilities = discover_capabilities(
        runner=runner,
        locator=located.get,
        platform_name="Windows",
    )

    assert capabilities.tooling_available
    assert not capabilities.can_compose
    assert "Offline Microsoft schema availability is unverified" in capabilities.blockers
    assert capabilities.authoring_cli.version == "0.1.4"
    assert not capabilities.desktop_render_verified
    assert [call[0] for call in runner.calls] == [
        (PBI_EXECUTABLE, "--version"),
        (AUTHORING_EXECUTABLE, "--version"),
    ]


def test_capability_discovery_requires_explicit_offline_schema_verification() -> None:
    receipts = [
        CommandReceipt(
            argv=("/tools/pbi", "--version"),
            cwd=None,
            returncode=0,
            stdout="3.11.1",
            stderr="",
            parsed_json=None,
            duration_ms=1,
        ),
        CommandReceipt(
            argv=("/tools/powerbi-report-author", "--version"),
            cwd=None,
            returncode=0,
            stdout="0.1.4",
            stderr="",
            parsed_json=None,
            duration_ms=1,
        ),
    ]
    capabilities = discover_capabilities(
        runner=RecordingRunner(receipts),
        locator={
            "pbi": "/tools/pbi",
            "powerbi-report-author": "/tools/powerbi-report-author",
        }.get,
        platform_name="Windows",
        official_schema_cache_verified=True,
    )

    assert capabilities.can_compose
