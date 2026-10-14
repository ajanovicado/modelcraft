from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ai_powerbi_analyst.contracts import SemanticCatalog
from ai_powerbi_analyst.powerbi.cli import (
    AuthoringCliAdapter,
    CommandReceipt,
    PbiCliAdapter,
)
from ai_powerbi_analyst.powerbi.transaction import hash_tree
from ai_powerbi_analyst.runtime.phase0_capture import (
    BoundExternalValidatorRecorder,
    _probe_exact_version,
    capture_baseline_validators,
    main,
)
from ai_powerbi_analyst.runtime.preparation import (
    PreparationSafetyError,
    prepare_windows_demo,
    seal_populated_semantic_cache,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class ValidatorRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
    ) -> CommandReceipt:
        del timeout
        if "--json" in argv:
            payload: dict[str, object] = {
                "valid": True,
                "errors": [],
                "warnings": [],
                "info": [],
                "summary": {"errors": 0, "warnings": 0, "info": 0},
            }
        else:
            payload = {
                "data": {
                    "result": "succeeded",
                    "errorCount": 0,
                    "warningCount": 0,
                    "reportPath": argv[-1],
                }
            }
        stdout = json.dumps(payload)
        return CommandReceipt(
            argv=argv,
            cwd=cwd,
            returncode=0,
            stdout=stdout,
            stderr="",
            parsed_json=payload,
            duration_ms=4,
        )


class VersionRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
    ) -> CommandReceipt:
        del timeout
        return CommandReceipt(
            argv=argv,
            cwd=cwd,
            returncode=0,
            stdout="pbi-cli 3.10.10\n",
            stderr="",
            parsed_json=None,
            duration_ms=3,
        )


class UvInventoryRunner:
    def __init__(self, *, uv: Path, environment: Path, installed_pbi: Path) -> None:
        self.uv = uv
        self.environment = environment
        self.installed_pbi = installed_pbi

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
    ) -> CommandReceipt:
        del timeout
        if Path(argv[0]) == self.uv:
            stdout = f"pbi-cli-tool v3.11.1 ({self.environment})\n- pbi ({self.installed_pbi})\n"
        else:
            stdout = "pbi-cli 3.10.10\n"
        return CommandReceipt(
            argv=argv,
            cwd=cwd,
            returncode=0,
            stdout=stdout,
            stderr="",
            parsed_json=None,
            duration_ms=3,
        )


class CaptureRunner:
    def __init__(self, *, fail_authoring: bool) -> None:
        self.fail_authoring = fail_authoring

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
    ) -> CommandReceipt:
        del timeout
        if "--version" in argv:
            version = "3.10.10" if Path(argv[0]).name.startswith("pbi") else "0.1.4"
            return CommandReceipt(
                argv=argv,
                cwd=cwd,
                returncode=0,
                stdout=version,
                stderr="",
                parsed_json=None,
                duration_ms=1,
            )
        if "--json" in argv:
            payload: dict[str, object] = {
                "valid": True,
                "errors": [],
                "warnings": [],
                "info": [],
                "summary": {"errors": 0, "warnings": 0, "info": 0},
            }
            returncode = 0
        else:
            failed = self.fail_authoring
            payload = {
                "data": {
                    "result": "failed" if failed else "succeeded",
                    "errorCount": 1 if failed else 0,
                    "warningCount": 0,
                    "reportPath": argv[-1],
                }
            }
            returncode = 1 if failed else 0
        return CommandReceipt(
            argv=argv,
            cwd=cwd,
            returncode=returncode,
            stdout=json.dumps(payload),
            stderr="",
            parsed_json=payload,
            duration_ms=2,
        )


def test_bound_validator_writes_two_content_addressed_report_receipts(
    tmp_path: Path,
) -> None:
    report = tmp_path / "workspace" / "ClientWow.Report"
    report.mkdir(parents=True)
    (report / "definition.pbir").write_text("{}\n", encoding="utf-8")
    stage = tmp_path / "stage" / "ClientWow.Report"
    stage.mkdir(parents=True)
    (stage / "definition.pbir").write_text('{"changed":true}\n', encoding="utf-8")
    pbi = tmp_path / "pbi.exe"
    authoring = tmp_path / "powerbi-report-author.exe"
    pbi.write_bytes(b"pbi executable")
    authoring.write_bytes(b"authoring executable")
    recorder = BoundExternalValidatorRecorder(
        pbi_cli=PbiCliAdapter(pbi, ValidatorRunner()),
        authoring_cli=AuthoringCliAdapter(authoring, ValidatorRunner()),
        pbi_cli_version="3.11.1",
        authoring_cli_version="0.1.4",
        logical_report_path=report,
        baseline_hash=hash_tree(report),
        receipt_root=tmp_path / "receipts",
        pbi_version_probe={
            "argv": [str(pbi), "--version"],
            "returncode": 0,
            "stdout": "3.11.1",
            "stderr": "",
            "duration_ms": 1,
            "reported_versions": ["3.11.1"],
            "distribution_name": "pbi-cli-tool",
            "distribution_version": "3.11.1",
            "accepted_version": "3.11.1",
        },
        authoring_version_probe={
            "argv": [str(authoring), "--version"],
            "returncode": 0,
            "stdout": "0.1.4",
            "stderr": "",
            "duration_ms": 1,
            "reported_versions": ["0.1.4"],
            "distribution_name": None,
            "distribution_version": None,
            "accepted_version": "0.1.4",
        },
    )

    first = recorder(stage, SemanticCatalog(version="1.0.0", metrics=[], dimensions=[]))
    second = recorder(stage, SemanticCatalog(version="1.0.0", metrics=[], dimensions=[]))

    assert first.passed
    assert len(first.receipt_hashes) == 2
    assert first.receipt_hashes == second.receipt_hashes
    files = sorted((tmp_path / "receipts").glob("*.json"))
    assert len(files) == 2
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["kind"] == "command_validation"
        assert payload["logical_report_path"] == str(report)
        assert payload["baseline_hash"] == hash_tree(report)
        assert payload["report_hash"] == hash_tree(stage)
        assert Path(payload["executable"]).is_absolute()
        assert Path(payload["cwd"]).is_absolute()


def test_phase0_capture_cli_fails_closed_outside_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "ai_powerbi_analyst.runtime.phase0_capture.platform.system",
        lambda: "Darwin",
    )

    exit_code = main(
        [
            "capture-validators",
            "--repository-root",
            str(tmp_path),
            "--demo-root",
            str(tmp_path / "demo"),
            "--pbi-executable",
            str(tmp_path / "pbi"),
            "--authoring-executable",
            str(tmp_path / "authoring"),
        ]
    )

    assert exit_code == 2
    assert "only run on Windows" in capsys.readouterr().err
    assert not (tmp_path / "demo").exists()


def test_pbi_version_probe_accepts_installed_distribution_over_stale_banner(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "tool" / "Scripts" / "pbi.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"pbi executable")
    metadata = (
        tmp_path / "tool" / "Lib" / "site-packages" / "pbi_cli_tool-3.11.1.dist-info" / "METADATA"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: pbi-cli-tool\nVersion: 3.11.1\n", encoding="utf-8")

    probe = _probe_exact_version(
        executable,
        "3.11.1",
        distribution_name="pbi-cli-tool",
        runner=VersionRunner(),
    )

    assert probe["reported_versions"] == ["3.10.10"]
    assert probe["distribution_version"] == "3.11.1"
    assert probe["accepted_version"] == "3.11.1"
    assert probe["distribution_probe"]["metadata_path"] == str(metadata)  # type: ignore[index]


def test_uv_distribution_probe_binds_the_listed_launcher_to_its_exact_tool_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uv = tmp_path / "uv.exe"
    uv.write_bytes(b"uv")
    environment = tmp_path / "uv-tools" / "pbi-cli-tool"
    installed_pbi = tmp_path / "uv-bin" / "pbi.exe"
    installed_pbi.parent.mkdir(parents=True)
    installed_pbi.write_bytes(b"pbi launcher")
    metadata = environment / "Lib/site-packages/pbi_cli_tool-3.11.1.dist-info/METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: pbi-cli-tool\nVersion: 3.11.1\n", encoding="utf-8")
    runner = UvInventoryRunner(
        uv=uv,
        environment=environment,
        installed_pbi=installed_pbi,
    )
    monkeypatch.setattr(
        "ai_powerbi_analyst.runtime.phase0_capture.shutil.which",
        lambda name: str(uv) if name == "uv" else None,
    )

    probe = _probe_exact_version(
        installed_pbi,
        "3.11.1",
        distribution_name="pbi-cli-tool",
        runner=runner,
    )
    assert probe["distribution_version"] == "3.11.1"
    assert probe["distribution_probe"]["installed_executable"] == str(installed_pbi)  # type: ignore[index]
    assert probe["distribution_probe"]["metadata_sha256"]  # type: ignore[index]

    stale = tmp_path / "stale-bin" / "pbi.exe"
    stale.parent.mkdir()
    stale.write_bytes(b"unrelated stale pbi launcher")
    with pytest.raises(PreparationSafetyError, match="does not match"):
        _probe_exact_version(
            stale,
            "3.11.1",
            distribution_name="pbi-cli-tool",
            runner=runner,
        )


def test_failed_validator_attempt_cannot_poison_canonical_passed_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ai_powerbi_analyst.runtime.preparation.assert_powerbi_desktop_closed", lambda: None
    )
    monkeypatch.setattr(
        "ai_powerbi_analyst.runtime.phase0_capture.assert_powerbi_desktop_closed", lambda: None
    )
    repository = tmp_path / "repository"
    shutil.copytree(REPOSITORY_ROOT / "fixtures", repository / "fixtures")
    shutil.copytree(REPOSITORY_ROOT / "evals", repository / "evals")
    monkeypatch.setattr(
        "ai_powerbi_analyst.runtime.phase0_capture.platform.system",
        lambda: "Windows",
    )
    monkeypatch.setattr(
        "ai_powerbi_analyst.runtime.preparation.platform.system",
        lambda: "Windows",
    )
    prepared = prepare_windows_demo(repository, tmp_path / "demo")
    cache = prepared.workspace_path / "ClientWow.SemanticModel" / ".pbi" / "cache.abf"
    cache.parent.mkdir()
    cache.write_bytes(b"populated cache")
    seal_populated_semantic_cache(repository, prepared.root)
    pbi = tmp_path / "pbi.exe"
    authoring = tmp_path / "powerbi-report-author.exe"
    pbi.write_bytes(b"pbi")
    authoring.write_bytes(b"authoring")

    with pytest.raises(PreparationSafetyError, match="validators failed"):
        capture_baseline_validators(
            repository_root=repository,
            demo_root=prepared.root,
            pbi_executable=pbi,
            authoring_executable=authoring,
            runner=CaptureRunner(fail_authoring=True),
            distribution_version=lambda _: "3.11.1",
        )
    canonical = prepared.root / "evidence" / "phase0" / "baseline-validators"
    assert not canonical.exists()
    assert len(list((prepared.root / "evidence" / "phase0" / "commands").glob("*.json"))) == 2

    pbi_receipt, authoring_receipt = capture_baseline_validators(
        repository_root=repository,
        demo_root=prepared.root,
        pbi_executable=pbi,
        authoring_executable=authoring,
        runner=CaptureRunner(fail_authoring=False),
        distribution_version=lambda _: "3.11.1",
    )

    assert pbi_receipt.path.parent == canonical
    assert authoring_receipt.path.parent == canonical
    assert {path.name for path in canonical.iterdir()} == {
        "pbi_cli_validation.json",
        "microsoft_validation.json",
    }


def test_windows_capture_and_launcher_scripts_preserve_literal_paths_and_identity() -> None:
    reopen = (REPOSITORY_ROOT / "scripts/capture-powerbi-desktop-reopen.ps1").read_text(
        encoding="utf-8"
    )
    three_pages = (REPOSITORY_ROOT / "scripts/capture-powerbi-three-pages.ps1").read_text(
        encoding="utf-8"
    )
    launcher = (REPOSITORY_ROOT / "scripts/launch-demo.ps1").read_text(encoding="utf-8")
    phase0 = (REPOSITORY_ROOT / "scripts/capture-phase0-windows.ps1").read_text(encoding="utf-8")

    for capture in (reopen, three_pages):
        assert "$QuotedPbipPath" in capture
        assert "-ArgumentList @($PbipPath)" not in capture
        assert "GetWindowRect" in capture
        assert "SetForegroundWindow" in capture
        assert "$ObservedWindowHandle" in capture
        assert "UIAutomation" in capture
    assert '"Executive Pulse"' in reopen
    for page in ("Executive Pulse", "Margin Drivers", "Outlook & Decisions"):
        assert f'"{page}"' in three_pages

    assert '$env:OLLAMA_HOST = "127.0.0.1:11434"' in launcher
    assert "$ExpectedEnvironmentReceiptHash" in launcher
    assert "$AttestedOllamaPath" in launcher
    assert "$VerifiedOwnerHash -ne $AttestedOllamaHash" in launcher
    assert "$LiteralStreamlitArguments" in launcher
    assert "-ArgumentList $LiteralStreamlitArguments" in launcher

    assert "[switch]$CaptureValidators" in phase0
    assert "Reusing the existing canonical passed validator pair." in phase0
    assert "capture-model-evaluation" in phase0
    assert "capture-strict-offline" in phase0
