from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ai_powerbi_analyst.contracts import ProjectBundle
from ai_powerbi_analyst.data.catalog import load_semantic_catalog
from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.preflight import EnvironmentReport, ToolProbe
from ai_powerbi_analyst.settings import AppSettings
from ai_powerbi_analyst.ui import production_runtime
from ai_powerbi_analyst.ui.production_runtime import (
    _assert_write_authority,
    _AttestedRuntimeToolchain,
    _build_desktop_launcher,
    _build_request_interpreter,
    _ExecutableIdentity,
    _load_attested_runtime_toolchain,
    _require_real_runtime_gate,
    build_real_workspace,
)

CATALOG_PATH = Path(__file__).parents[2] / "fixtures" / "data" / "semantic_catalog.yaml"


def _environment(*, operating_system: str = "Windows") -> EnvironmentReport:
    return EnvironmentReport(
        operating_system=operating_system,
        operating_system_version="10.0.26100",
        python_version="3.12.12",
        total_memory_gb=16,
        available_memory_gb=8,
        desktop_running=False,
        tools={
            "pbi": ToolProbe(name="pbi", available=True, version="3.11.1"),
            "powerbi-report-author": ToolProbe(
                name="powerbi-report-author", available=True, version="0.1.4"
            ),
            "ollama": ToolProbe(name="ollama", available=True, version="0.12.0"),
            "node": ToolProbe(name="node", available=True, version="24.0.0"),
            "powerbi-desktop": ToolProbe(
                name="powerbi-desktop", available=True, version="2.146.705.0"
            ),
        },
    )


def _unattested_bundle(tmp_path) -> ProjectBundle:  # type: ignore[no-untyped-def]
    return ProjectBundle(
        id="sales-margin-demo",
        name="Sales & Margin",
        pbip_path=tmp_path / "Demo.pbip",
        report_path=tmp_path / "Demo.Report",
        semantic_model_path=tmp_path / "Demo.SemanticModel",
        duckdb_path=tmp_path / "analytics.duckdb",
        catalog_path=tmp_path / "semantic_catalog.yaml",
        data_hash="a" * 64,
        catalog_version="1.0.0",
        data_through=date(2025, 12, 31),
    )


def test_direct_real_runtime_is_blocked_without_phase0_attestation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RuntimeError, match="Phase 0 mutation/reopen kill test"):
        _require_real_runtime_gate(
            _unattested_bundle(tmp_path),
            AppSettings(),
            _environment(),
        )


def test_direct_real_runtime_is_blocked_off_windows_even_if_launcher_is_bypassed(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RuntimeError, match="Windows"):
        _require_real_runtime_gate(
            _unattested_bundle(tmp_path),
            AppSettings(),
            _environment(operating_system="Darwin"),
        )


def test_real_runtime_rejects_state_outside_repository_owned_runtime(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(RuntimeError, match="repository-owned .runtime"):
        build_real_workspace(
            _unattested_bundle(tmp_path),
            AppSettings(),
            state_root=repository / "baseline",
            environment=_environment(),
            repository_root=repository,
        )


def test_real_runtime_attempts_trusted_recovery_before_full_authority_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    workspace = tmp_path / "demo" / "workspace"
    workspace.mkdir(parents=True)
    bundle = _unattested_bundle(tmp_path).model_copy(
        update={"pbip_path": workspace / "ClientWow.pbip"}
    )
    events: list[str] = []

    def recover(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        events.append("recover")
        return True

    def verify(*args: object, **kwargs: object) -> None:
        del args, kwargs
        events.append("verify")
        raise RuntimeError("stop after ordering assertion")

    monkeypatch.setattr(
        production_runtime,
        "recover_interrupted_report_swap",
        recover,
    )
    monkeypatch.setattr(
        production_runtime,
        "_verify_phase0_release_authority",
        verify,
    )

    with pytest.raises(RuntimeError, match="ordering assertion"):
        build_real_workspace(
            bundle,
            AppSettings(),
            state_root=repository / ".runtime",
            environment=_environment(),
            repository_root=repository,
        )

    assert events == ["recover", "verify"]


def test_write_authority_rechecks_bundle_and_desktop_exclusion(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    events: list[tuple[str, Path]] = []
    bundle = _unattested_bundle(tmp_path)
    transaction_state = tmp_path / ".runtime" / "powerbi"
    marker = tmp_path / ".reveal-in-progress.json"
    monkeypatch.setattr(
        production_runtime,
        "_validate_bundle_paths_and_hashes",
        lambda checked, *, transaction_state_path: events.append(
            ("bundle", transaction_state_path)
        ),
    )
    monkeypatch.setattr(
        production_runtime,
        "assert_powerbi_write_allowed",
        lambda checked_marker: events.append(("desktop", Path(checked_marker))),
    )

    _assert_write_authority(
        bundle,
        transaction_state_path=transaction_state,
        reveal_marker_path=marker,
    )

    assert events == [("bundle", transaction_state), ("desktop", marker)]


def test_runtime_toolchain_comes_from_content_addressed_environment_receipt(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    demo = tmp_path / "demo"
    workspace = demo / "workspace"
    workspace.mkdir(parents=True)
    tools: dict[str, Path] = {}
    for name in ("pbi", "authoring", "node", "ollama", "desktop"):
        path = tmp_path / f"{name}.exe"
        path.write_bytes(name.encode("utf-8"))
        tools[name] = path
    environment = {
        "tool_executables": {
            "pbi_cli": {"path": str(tools["pbi"]), "sha256": sha256_file(tools["pbi"])},
            "microsoft_validator": {
                "path": str(tools["authoring"]),
                "sha256": sha256_file(tools["authoring"]),
            },
            "node": {"path": str(tools["node"]), "sha256": sha256_file(tools["node"])},
            "ollama": {
                "path": str(tools["ollama"]),
                "sha256": sha256_file(tools["ollama"]),
            },
        },
        "desktop_identity": {
            "executable": str(tools["desktop"]),
            "executable_sha256": sha256_file(tools["desktop"]),
        },
    }
    receipt = demo / "evidence" / "environment.json"
    receipt.parent.mkdir()
    receipt.write_text(json.dumps(environment), encoding="utf-8")
    receipt_hash = sha256_file(receipt)
    (demo / "phase0-attestation.json").write_text(
        json.dumps(
            {
                "receipts": {
                    "environment": {
                        "path": "evidence/environment.json",
                        "sha256": receipt_hash,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    bundle = _unattested_bundle(tmp_path).model_copy(
        update={
            "pbip_path": workspace / "ClientWow.pbip",
            "phase0_receipt_hashes": {"environment": receipt_hash},
        }
    )

    toolchain = _load_attested_runtime_toolchain(bundle)

    assert toolchain.pbi_cli.path == tools["pbi"]
    assert toolchain.microsoft_validator.path == tools["authoring"]
    assert toolchain.node.path == tools["node"]
    assert toolchain.ollama.path == tools["ollama"]
    assert toolchain.powerbi_desktop.path == tools["desktop"]


def test_scripted_real_runtime_uses_no_ollama_client_or_reveal_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding_client_factory(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("scripted mode must not construct or contact an Ollama client")

    interpreter = _build_request_interpreter(
        AppSettings(),
        load_semantic_catalog(CATALOG_PATH),
        scripted=True,
        client_factory=exploding_client_factory,
    )

    assert interpreter is None

    workspace = tmp_path / "demo" / "workspace"
    workspace.mkdir(parents=True)
    pbip = workspace / "ClientWow.pbip"
    pbip.write_text("{}", encoding="utf-8")
    ollama = tmp_path / "ollama.exe"
    ollama.write_bytes(b"not an executable: any spawn would fail")
    desktop = tmp_path / "PBIDesktop.exe"
    desktop.write_bytes(b"attested desktop")
    placeholder = tmp_path / "placeholder.exe"
    placeholder.write_bytes(b"placeholder")
    placeholder_identity = _ExecutableIdentity(placeholder, sha256_file(placeholder))
    toolchain = _AttestedRuntimeToolchain(
        pbi_cli=placeholder_identity,
        microsoft_validator=placeholder_identity,
        node=placeholder_identity,
        ollama=_ExecutableIdentity(ollama, sha256_file(ollama)),
        powerbi_desktop=_ExecutableIdentity(desktop, sha256_file(desktop)),
    )
    bundle = _unattested_bundle(tmp_path).model_copy(update={"pbip_path": pbip})
    authority_checks: list[Path] = []
    monkeypatch.setattr(
        production_runtime,
        "_validate_bundle_paths_and_hashes",
        lambda checked, *, transaction_state_path: authority_checks.append(transaction_state_path),
    )
    launcher = _build_desktop_launcher(
        bundle,
        AppSettings(),
        toolchain,
        transaction_state=tmp_path / ".runtime" / "powerbi",
        operation_lock_path=tmp_path / "demo" / ".operation.lock",
        scripted=True,
    )
    launched: list[tuple[Path, Path]] = []
    launcher.platform_name = "Windows"
    launcher.desktop_running_check = lambda: False
    launcher.desktop_identity_check = lambda executable: executable == desktop
    launcher.launch_desktop = lambda executable, project: launched.append((executable, project))

    result = launcher()

    assert result["opened"] is True
    assert launched == [(desktop, pbip.resolve())]
    assert authority_checks == [tmp_path / ".runtime" / "powerbi"]


def test_real_workspace_environment_forwards_explicit_scripted_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = tmp_path / "project-bundle.json"
    descriptor.write_text(
        _unattested_bundle(tmp_path).model_dump_json(),
        encoding="utf-8",
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    sentinel = object()
    captured: dict[str, object] = {}

    def fake_build_real_workspace(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setenv("AI_POWERBI_BUNDLE_PATH", str(descriptor))
    monkeypatch.setenv("AI_POWERBI_PROJECT_ROOT", str(repository))
    monkeypatch.setenv("AI_POWERBI_SCRIPTED", "1")
    monkeypatch.setattr(production_runtime, "load_settings", lambda path: AppSettings())
    monkeypatch.setattr(
        production_runtime,
        "build_real_workspace",
        fake_build_real_workspace,
    )

    result = production_runtime.build_real_workspace_from_environment()

    assert result is sentinel
    assert captured["scripted"] is True
