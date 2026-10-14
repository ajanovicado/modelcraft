"""Deterministically prepare and reset the app-owned Windows demo workspace.

Preparation is deliberately separate from release attestation.  It hydrates a
copy of the frozen PBIP template and records its hashes, but it never claims
that Power BI Desktop can render that copy.  Real-write capabilities become
true only when a complete Phase-0 attestation matches every frozen artifact
and compatibility pin.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import shutil
import statistics
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from filelock import FileLock
from pydantic import Field, ValidationError, field_validator

from ai_powerbi_analyst.contracts import (
    AnalysisPlan,
    ClosedModel,
    CompatibilityPins,
    EvidenceAnswer,
    ProjectBundle,
    ProjectCapabilities,
    RouteIntent,
    Sha256,
)
from ai_powerbi_analyst.evaluation_goldens import (
    GoldenCaseOutcome,
    compare_golden_case,
    load_frozen_goldens,
    summarize_golden_outcomes,
)
from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.powerbi.cli import CommandReceipt
from ai_powerbi_analyst.powerbi.project import (
    PBIPEntryPointError,
    validate_pbip_entrypoint,
    validate_report_semantic_reference,
)
from ai_powerbi_analyst.powerbi.transaction import (
    ProjectTransactionManager,
    TransactionError,
    hash_tree,
    verify_validated_report_head,
)
from ai_powerbi_analyst.powerbi.validation import validation_from_receipt
from ai_powerbi_analyst.preflight import assert_powerbi_desktop_closed

REQUIRED_PHASE0_GATES = (
    "ten_representative_mutations_reopen",
    "rollback_exact",
    "structured_output_floor_met",
    "complete_flow_offline",
    "pbi_cli_validation_passed",
    "microsoft_validation_passed",
    "strict_offline_schema_verified",
    "desktop_render_verified",
    "populated_cache_verified",
)

EXPECTED_PBIR_SCHEMA_VERSIONS = {
    "pbip_properties": "1.0.0",
    "semantic_model_definition_properties": "1.0.0",
    "report_definition_properties": "2.0.0",
    "report": "3.2.0",
    "page": "2.1.0",
    "pages_metadata": "1.0.0",
    "version_metadata": "1.0.0",
}

_EXPECTED_PBI_CLI_VERSION = "3.11.1"
_EXPECTED_VALIDATOR_VERSION = "0.1.4"
_APPROVED_MODEL_CANDIDATES = frozenset({"qwen3.5:4b-q4_K_M", "qwen3.5:4b"})
_SOURCE_RECORDS = (
    "sales_parquet",
    "driver_parquet",
    "forecast_parquet",
    "scenario_parquet",
    "forecast_backtest_parquet",
    "forecast_selection_parquet",
)
_HYDRATED_SOURCE_RECORDS = _SOURCE_RECORDS[:4]
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$")
_MODEL_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

_PARQUET_PARTITIONS = {
    "Fact Sales.tmdl": ("'Fact Sales'", "sales_parquet", "fact"),
    "Driver Contributions.tmdl": (
        "'Driver Contributions'",
        "driver_parquet",
        "direct",
    ),
    "Forecast.tmdl": ("'Forecast'", "forecast_parquet", "direct"),
    "Scenario Grid.tmdl": ("'Scenario Grid'", "scenario_parquet", "direct"),
}

_DimensionColumns = str | tuple[str, ...]

_DIMENSION_PARTITIONS: dict[
    str,
    tuple[str, _DimensionColumns, _DimensionColumns],
] = {
    "Dim Date.tmdl": ("'Dim Date'", "month_start", "Month Start"),
    "Dim Region.tmdl": ("'Dim Region'", "region", "Region"),
    "Dim Product.tmdl": (
        "'Dim Product'",
        ("product", "category"),
        ("Product", "Category"),
    ),
    "Dim Customer.tmdl": ("'Dim Customer'", "segment", "Segment"),
    "Dim Channel.tmdl": ("'Dim Channel'", "channel", "Channel"),
}


class PreparationSafetyError(RuntimeError):
    """The requested preparation or reset would cross a safety boundary."""


class Phase0Gates(ClosedModel):
    ten_representative_mutations_reopen: bool
    rollback_exact: bool
    structured_output_floor_met: bool
    complete_flow_offline: bool
    pbi_cli_validation_passed: bool
    microsoft_validation_passed: bool
    strict_offline_schema_verified: bool
    desktop_render_verified: bool
    populated_cache_verified: bool


class Phase0Pins(ClosedModel):
    pbi_cli_version: str
    microsoft_validator_version: str
    desktop_version: str
    windows_build: str
    python_version: str
    node_version: str
    pbir_schema_versions: dict[str, str] = Field(min_length=1)
    ollama_version: str
    model_tag: str
    model_digest: str

    @field_validator(
        "pbi_cli_version",
        "microsoft_validator_version",
        "desktop_version",
        "windows_build",
        "python_version",
        "node_version",
        "ollama_version",
    )
    @classmethod
    def versions_are_exact(cls, value: str) -> str:
        if not _VERSION_PATTERN.fullmatch(value):
            raise ValueError("version pins must be exact dotted numeric versions")
        return value

    @field_validator("model_digest")
    @classmethod
    def model_digest_is_exact(cls, value: str) -> str:
        if not _MODEL_DIGEST_PATTERN.fullmatch(value):
            raise ValueError("model digest must be a full lowercase sha256 digest")
        return value


class AttestedArtifactHashes(ClosedModel):
    data_hash: Sha256
    source_data_hash: Sha256
    source_hashes: dict[str, Sha256] = Field(min_length=6, max_length=6)
    duckdb_hash: Sha256
    catalog_hash: Sha256
    pbip_hash: Sha256
    report_hash: Sha256
    semantic_model_hash: Sha256
    semantic_model_cache_hash: Sha256 | None = None
    baseline_workspace_hash: Sha256 | None = None


class AttestationReceiptReference(ClosedModel):
    path: str = Field(min_length=1)
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def path_is_app_relative(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
            raise ValueError("receipt path must be a normalized relative path")
        return value


class Phase0ReceiptReferences(ClosedModel):
    environment: AttestationReceiptReference
    kill_test: AttestationReceiptReference
    model_evaluation: AttestationReceiptReference
    strict_offline: AttestationReceiptReference
    desktop_render: AttestationReceiptReference
    pbi_cli_validation: AttestationReceiptReference
    microsoft_validation: AttestationReceiptReference


class Phase0Attestation(ClosedModel):
    schema_version: Literal["1.0.0"]
    status: Literal["passed"]
    gates: Phase0Gates
    pins: Phase0Pins
    artifact_hashes: AttestedArtifactHashes
    receipts: Phase0ReceiptReferences


@dataclass(frozen=True, slots=True)
class PreparedWindowsDemo:
    root: Path
    workspace_path: Path
    baseline_path: Path
    bundle_path: Path
    attested: bool


@dataclass(frozen=True, slots=True)
class ResetReceipt:
    root: Path
    workspace_path: Path
    backup_path: Path


@dataclass(frozen=True, slots=True)
class CacheSealReceipt:
    """Identity of the imported-data cache frozen into the reset baseline."""

    cache_path: Path
    cache_sha256: str
    cache_size_bytes: int
    semantic_model_hash: str
    baseline_workspace_hash: str


@dataclass(frozen=True, slots=True)
class _FrozenInputs:
    repository: Path
    template: Path
    data_root: Path
    manifest: dict[str, object]
    files: dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True)
class _PendingReportSwap:
    transaction_id: str
    before_hash: str
    intent_path: Path
    displaced_path: Path


def _absolute_without_resolving(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.absolute()


def _reject_symlink_components(path: Path) -> None:
    candidate = _absolute_without_resolving(path)
    chain = [candidate, *candidate.parents]
    for component in chain:
        if _is_link_like(component):
            raise PreparationSafetyError(
                f"path cannot use a symbolic link or junction: {component}"
            )


def _reject_symlink_tree(root: Path) -> None:
    _reject_symlink_components(root)
    for candidate in root.rglob("*"):
        if _is_link_like(candidate):
            raise PreparationSafetyError(
                f"fixture cannot contain a symbolic link or junction: {candidate}"
            )


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _safe_destination(path: str | Path) -> Path:
    destination = _absolute_without_resolving(path)
    if destination == Path(destination.anchor):
        raise PreparationSafetyError("a filesystem root cannot be an app-owned demo root")
    _reject_symlink_components(destination)
    return destination


def _load_json_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreparationSafetyError(f"{label} is not valid UTF-8 JSON: {path}") from error
    if not isinstance(payload, dict):
        raise PreparationSafetyError(f"{label} must be a JSON object: {path}")
    return payload


def _load_frozen_inputs(repository_root: str | Path) -> _FrozenInputs:
    raw_repository = _absolute_without_resolving(repository_root)
    _reject_symlink_components(raw_repository)
    if not raw_repository.is_dir():
        raise PreparationSafetyError(f"repository root is missing: {raw_repository}")
    repository = raw_repository.resolve(strict=True)
    template = repository / "fixtures" / "powerbi" / "client-wow-template"
    data_root = repository / "fixtures" / "data"
    if not template.is_dir() or not data_root.is_dir():
        raise PreparationSafetyError("the frozen Power BI template or data fixture is missing")
    _reject_symlink_tree(template)
    _reject_symlink_tree(data_root)

    manifest_path = data_root / "manifest.json"
    manifest = _load_json_object(manifest_path, label="analytical manifest")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict):
        raise PreparationSafetyError("analytical manifest has no file inventory")

    required = {
        "catalog",
        "duckdb",
        "sales_parquet",
        "driver_parquet",
        "forecast_parquet",
        "scenario_parquet",
        "forecast_backtest_parquet",
        "forecast_selection_parquet",
    }
    files: dict[str, dict[str, object]] = {}
    for key in sorted(required):
        record = raw_files.get(key)
        if not isinstance(record, dict):
            raise PreparationSafetyError(f"analytical manifest is missing {key}")
        relative = record.get("path")
        expected_hash = record.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise PreparationSafetyError(f"analytical manifest has an invalid {key} record")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise PreparationSafetyError(f"analytical manifest path escapes its root: {key}")
        raw_candidate = data_root / relative
        _reject_symlink_components(raw_candidate)
        try:
            candidate = raw_candidate.resolve(strict=True)
        except OSError as error:
            raise PreparationSafetyError(f"frozen analytical file is missing: {key}") from error
        if not candidate.is_relative_to(data_root.resolve(strict=True)):
            raise PreparationSafetyError(f"analytical manifest path escapes its root: {key}")
        if not candidate.is_file() or sha256_file(candidate) != expected_hash:
            raise PreparationSafetyError(f"frozen analytical hash check failed: {key}")
        files[key] = dict(record)

    catalog_hash = manifest.get("catalog_hash")
    if catalog_hash != files["catalog"]["sha256"]:
        raise PreparationSafetyError("catalog hash disagrees with the file inventory")
    fixture_manifest = _load_json_object(
        template / "fixture-manifest.json",
        label="Power BI fixture manifest",
    )
    expected_fixture_identity = {
        "catalog_version": str(manifest["version"]),
        "catalog_hash": str(files["catalog"]["sha256"]),
        "data_hash": str(manifest["data_hash"]),
        "data_through": str(manifest["data_through"]),
    }
    for field, expected in expected_fixture_identity.items():
        if fixture_manifest.get(field) != expected:
            raise PreparationSafetyError(
                f"frozen Power BI fixture {field.replace('_', ' ')} disagrees "
                "with the analytical manifest"
            )
    return _FrozenInputs(repository, template, data_root, manifest, files)


def _source_path(inputs: _FrozenInputs, record_name: str) -> Path:
    record = inputs.files[record_name]
    path = (inputs.data_root / str(record["path"])).resolve(strict=True)
    if not path.is_absolute() or not path.is_file():
        raise PreparationSafetyError(f"frozen source path is not an absolute regular file: {path}")
    return path


def _m_text_literal(path: Path) -> str:
    value = str(path)
    unsafe_characters = ('"', "#", "\r", "\n", "\0")
    if not path.is_absolute() or any(character in value for character in unsafe_characters):
        raise PreparationSafetyError(
            "Power Query source paths must be absolute and cannot contain quotes, #, or controls"
        )
    return f'"{value}"'


def _direct_parquet_query(path: Path) -> list[str]:
    return [
        "let",
        f"\tSource = Parquet.Document(File.Contents({_m_text_literal(path)}))",
        "in",
        "\tSource",
    ]


def _fact_sales_query(path: Path) -> list[str]:
    renames = (
        ("month_start", "Month Start"),
        ("region", "Region"),
        ("product", "Product"),
        ("category", "Category"),
        ("segment", "Segment"),
        ("channel", "Channel"),
        ("units", "Units"),
        ("list_price", "List Price"),
        ("net_sales", "Net Sales"),
        ("cogs", "COGS"),
        ("gross_profit", "Gross Profit"),
    )
    rename_pairs = ", ".join(f'{{"{source}", "{target}"}}' for source, target in renames)
    return [
        "let",
        f"\tRaw = Parquet.Document(File.Contents({_m_text_literal(path)})),",
        f"\tSource = Table.RenameColumns(Raw, {{{rename_pairs}}}, MissingField.Error)",
        "in",
        "\tSource",
    ]


def _dimension_query(
    path: Path,
    source_columns: str | tuple[str, ...],
    target_columns: str | tuple[str, ...],
) -> list[str]:
    sources = (source_columns,) if isinstance(source_columns, str) else source_columns
    targets = (target_columns,) if isinstance(target_columns, str) else target_columns
    selected = ", ".join(f'"{column}"' for column in sources)
    rename_pairs = ", ".join(
        f'{{"{source}", "{target}"}}' for source, target in zip(sources, targets, strict=True)
    )
    return [
        "let",
        f"\tRaw = Parquet.Document(File.Contents({_m_text_literal(path)})),",
        f"\tSelected = Table.SelectColumns(Raw, {{{selected}}}, MissingField.Error),",
        f"\tRenamed = Table.RenameColumns(Selected, {{{rename_pairs}}}, MissingField.Error),",
        "\tSource = Table.Distinct(Renamed)",
        "in",
        "\tSource",
    ]


def _replace_partition(path: Path, partition_name: str, query_lines: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    marker = f"\tpartition {partition_name} = m"
    marker_index = text.find(marker)
    if marker_index < 0 or text.find(marker, marker_index + len(marker)) >= 0:
        raise PreparationSafetyError(f"expected one governed partition in {path.name}")
    prefix = text[:marker_index].rstrip("\n")
    prefix_lines = prefix.splitlines()
    while prefix_lines and prefix_lines[-1].startswith("\t///"):
        prefix_lines.pop()
    prefix = "\n".join(prefix_lines).rstrip()
    indented_query = "\n".join(f"\t\t\t{line}" for line in query_lines)
    hydrated = f"{prefix}\n\n{marker}\n\t\tmode: import\n\t\tsource =\n{indented_query}\n"
    path.write_text(hydrated, encoding="utf-8")


def _hydrate_semantic_model(workspace: Path, inputs: _FrozenInputs) -> None:
    tables = workspace / "ClientWow.SemanticModel" / "definition" / "tables"
    if not tables.is_dir():
        raise PreparationSafetyError("copied semantic model has no TMDL table directory")

    for filename, (partition, source_key, mode) in _PARQUET_PARTITIONS.items():
        source = _source_path(inputs, source_key)
        query = _fact_sales_query(source) if mode == "fact" else _direct_parquet_query(source)
        _replace_partition(tables / filename, partition, query)

    sales = _source_path(inputs, "sales_parquet")
    for filename, (partition, source_columns, target_columns) in _DIMENSION_PARTITIONS.items():
        _replace_partition(
            tables / filename,
            partition,
            _dimension_query(sales, source_columns, target_columns),
        )


def _default_compatibility(manifest: dict[str, object]) -> CompatibilityPins:
    runtime = manifest.get("runtime")
    return CompatibilityPins(
        python_version=str(runtime.get("python")) if isinstance(runtime, dict) else None,
        pbir_schema_versions=dict(EXPECTED_PBIR_SCHEMA_VERSIONS),
    )


def _fail_closed_capabilities() -> ProjectCapabilities:
    return ProjectCapabilities(
        can_analyze=True,
        can_explain=True,
        blockers=[
            "Phase-0 attestation has not been accepted; real Power BI writes remain disabled."
        ],
    )


def _pin_blockers(pins: Phase0Pins, manifest: dict[str, object]) -> list[str]:
    blockers: list[str] = []
    runtime = manifest.get("runtime")
    expected_python = runtime.get("python") if isinstance(runtime, dict) else None
    if pins.pbi_cli_version != _EXPECTED_PBI_CLI_VERSION:
        blockers.append(f"pbi-cli must be pinned to {_EXPECTED_PBI_CLI_VERSION}")
    if pins.microsoft_validator_version != _EXPECTED_VALIDATOR_VERSION:
        blockers.append(f"Microsoft validator must be pinned to {_EXPECTED_VALIDATOR_VERSION}")
    if expected_python is None or pins.python_version != expected_python:
        blockers.append("Python pin does not match the frozen data-build runtime")
    if pins.pbir_schema_versions != EXPECTED_PBIR_SCHEMA_VERSIONS:
        blockers.append("PBIR schema pins do not exactly match the prepared template")
    if pins.model_tag not in _APPROVED_MODEL_CANDIDATES:
        blockers.append("model tag is not in the frozen target-laptop candidate set")
    return blockers


def _validated_attestation(
    attestation_path: str | Path,
    *,
    demo_root: Path,
    expected_hashes: AttestedArtifactHashes,
    manifest: dict[str, object],
    model_corpus_hash: str,
    model_case_ids: list[str],
) -> Phase0Attestation:
    """Load one attestation and reject every mismatch without changing state."""

    raw_path = _absolute_without_resolving(attestation_path)
    _reject_symlink_components(raw_path)
    if not raw_path.is_file():
        raise PreparationSafetyError(f"Phase-0 attestation is missing: {raw_path}")
    payload = _load_json_object(raw_path, label="Phase-0 attestation")
    try:
        attestation = Phase0Attestation.model_validate(payload)
    except ValidationError as error:
        detail = error.errors(include_url=False)[0]
        location = ".".join(str(part) for part in detail["loc"])
        raise PreparationSafetyError(
            f"Phase-0 attestation is invalid at {location}: {detail['msg']}"
        ) from error

    blockers = [
        f"Phase-0 gate is not passed: {gate}"
        for gate in REQUIRED_PHASE0_GATES
        if not getattr(attestation.gates, gate)
    ]
    if attestation.artifact_hashes != expected_hashes:
        blockers.append(
            "Phase-0 artifact hashes do not match the prepared report and semantic model"
        )
    blockers.extend(_pin_blockers(attestation.pins, manifest))
    if expected_hashes.semantic_model_cache_hash is None:
        blockers.append("prepared semantic model has no sealed, nonempty imported-data cache")
    if expected_hashes.baseline_workspace_hash is None:
        blockers.append("prepared reset baseline has no bound complete-workspace hash")
    try:
        _validate_phase0_receipts(
            attestation,
            demo_root=demo_root,
            expected_hashes=expected_hashes,
            model_corpus_hash=model_corpus_hash,
            model_case_ids=model_case_ids,
        )
    except PreparationSafetyError as error:
        blockers.append(str(error))
    if blockers:
        raise PreparationSafetyError("; ".join(blockers))
    return attestation


def _receipt_payload(
    reference: AttestationReceiptReference,
    *,
    demo_root: Path,
    label: str,
) -> dict[str, object]:
    candidate = _absolute_without_resolving(demo_root / reference.path)
    _reject_symlink_components(candidate)
    if not candidate.is_relative_to(demo_root.absolute()) or not candidate.is_file():
        raise PreparationSafetyError(f"Phase-0 {label} receipt is missing or escapes the demo")
    if sha256_file(candidate) != reference.sha256:
        raise PreparationSafetyError(f"Phase-0 {label} receipt hash does not match")
    return _load_json_object(candidate, label=f"Phase-0 {label} receipt")


def _validate_receipt_header(
    payload: dict[str, object],
    *,
    kind: str,
) -> None:
    if (
        payload.get("schema_version") != "1.0.0"
        or payload.get("kind") != kind
        or payload.get("status") != "passed"
    ):
        raise PreparationSafetyError(f"Phase-0 {kind} receipt did not pass its exact contract")


def _valid_pbi_distribution_probe(
    value: object,
    *,
    executable: Path,
    executable_hash: str,
    version: str,
) -> bool:
    if not isinstance(value, dict):
        return False
    if (
        value.get("distribution_name") != "pbi-cli-tool"
        or value.get("distribution_version") != version
        or value.get("subject_executable") != str(executable)
        or value.get("subject_executable_sha256") != executable_hash
    ):
        return False
    method = value.get("method")
    if method == "adjacent_metadata":
        metadata_path = value.get("metadata_path")
        metadata_hash = value.get("metadata_sha256")
        resolved_metadata = (
            Path(metadata_path).resolve(strict=False) if isinstance(metadata_path, str) else None
        )
        if (
            not isinstance(metadata_path, str)
            or not Path(metadata_path).is_absolute()
            or not Path(metadata_path).is_file()
            or resolved_metadata is None
            or not resolved_metadata.is_relative_to(executable.parent.parent)
            or not _is_sha256_text(metadata_hash)
            or sha256_file(Path(metadata_path)) != metadata_hash
        ):
            return False
        try:
            metadata = Path(metadata_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return bool(
            re.search(r"(?im)^Name:\s*pbi-cli-tool\s*$", metadata)
            and re.search(rf"(?im)^Version:\s*{re.escape(version)}\s*$", metadata)
        )
    if method == "uv_tool_list_show_paths":
        uv_executable = value.get("uv_executable")
        uv_hash = value.get("uv_executable_sha256")
        stdout = value.get("stdout")
        argv = value.get("argv")
        environment_path = value.get("environment_path")
        installed_executable = value.get("installed_executable")
        metadata_path = value.get("metadata_path")
        metadata_hash = value.get("metadata_sha256")
        if (
            not isinstance(environment_path, str)
            or not Path(environment_path).is_absolute()
            or not Path(environment_path).is_dir()
            or not isinstance(installed_executable, str)
            or not Path(installed_executable).is_absolute()
            or Path(installed_executable).resolve(strict=False) != executable.resolve(strict=False)
            or not isinstance(metadata_path, str)
            or not Path(metadata_path).is_absolute()
            or not Path(metadata_path).is_file()
            or not Path(metadata_path).is_relative_to(Path(environment_path))
            or not _is_sha256_text(metadata_hash)
            or sha256_file(Path(metadata_path)) != metadata_hash
        ):
            return False
        try:
            metadata = Path(metadata_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return bool(
            isinstance(uv_executable, str)
            and Path(uv_executable).is_absolute()
            and Path(uv_executable).is_file()
            and _is_sha256_text(uv_hash)
            and sha256_file(Path(uv_executable)) == uv_hash
            and argv == [uv_executable, "tool", "list", "--show-paths"]
            and value.get("returncode") == 0
            and isinstance(stdout, str)
            and re.search(
                rf"(?im)^pbi-cli-tool\s+v{re.escape(version)}\s+"
                rf"\({re.escape(environment_path)}\)\s*$",
                stdout,
            )
            and re.search(
                rf"(?im)^-\s+pbi(?:\.exe)?\s+"
                rf"\({re.escape(installed_executable)}\)\s*$",
                stdout,
            )
            and re.search(r"(?im)^Name:\s*pbi-cli-tool\s*$", metadata)
            and re.search(rf"(?im)^Version:\s*{re.escape(version)}\s*$", metadata)
            and isinstance(value.get("stderr"), str)
            and isinstance(value.get("duration_ms"), int)
            and not isinstance(value.get("duration_ms"), bool)
            and value.get("duration_ms", -1) >= 0
        )
    return False


def _validate_command_receipt(
    payload: dict[str, object],
    *,
    source: str,
    logical_report_path: Path,
    baseline_hash: str,
    report_hash: str,
    tool_version: str,
) -> None:
    _validate_receipt_header(payload, kind="command_validation")
    if payload.get("source") != source:
        raise PreparationSafetyError(f"Phase-0 {source} receipt has the wrong source")
    executable = payload.get("executable")
    executable_hash = payload.get("executable_sha256")
    version_probe = payload.get("version_probe")
    validated_report = payload.get("validated_report_path")
    logical_report = payload.get("logical_report_path")
    argv = payload.get("argv")
    cwd = payload.get("cwd")
    returncode = payload.get("returncode")
    stdout = payload.get("stdout")
    stderr = payload.get("stderr")
    duration_ms = payload.get("duration_ms")
    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise PreparationSafetyError(f"Phase-0 {source} executable path is invalid")
    executable_path = Path(executable)
    _reject_symlink_components(executable_path)
    if (
        not executable_path.is_file()
        or not _is_sha256_text(executable_hash)
        or sha256_file(executable_path) != executable_hash
        or payload.get("tool_version") != tool_version
    ):
        raise PreparationSafetyError(f"Phase-0 {source} executable identity/version is invalid")
    if not isinstance(version_probe, dict):
        raise PreparationSafetyError(f"Phase-0 {source} version probe is missing")
    reported_versions = version_probe.get("reported_versions")
    banner_matches = isinstance(reported_versions, list) and tool_version in reported_versions
    distribution_matches = (
        source == "pbi-cli"
        and version_probe.get("distribution_name") == "pbi-cli-tool"
        and version_probe.get("distribution_version") == tool_version
        and _valid_pbi_distribution_probe(
            version_probe.get("distribution_probe"),
            executable=executable_path,
            executable_hash=str(executable_hash),
            version=tool_version,
        )
    )
    if (
        version_probe.get("argv") != [executable, "--version"]
        or version_probe.get("returncode") != 0
        or not isinstance(version_probe.get("stdout"), str)
        or not isinstance(version_probe.get("stderr"), str)
        or not isinstance(version_probe.get("duration_ms"), int)
        or isinstance(version_probe.get("duration_ms"), bool)
        or version_probe.get("duration_ms", -1) < 0
        or version_probe.get("accepted_version") != tool_version
        or not (banner_matches or distribution_matches)
    ):
        raise PreparationSafetyError(
            f"Phase-0 {source} raw version probe does not prove the pinned version"
        )
    if (
        not isinstance(validated_report, str)
        or not Path(validated_report).is_absolute()
        or logical_report != str(logical_report_path.absolute())
        or payload.get("baseline_hash") != baseline_hash
        or payload.get("report_hash") != report_hash
    ):
        raise PreparationSafetyError(
            f"Phase-0 {source} receipt is not bound to the report/baseline hashes"
        )
    expected_argv = (
        [
            executable,
            "--json",
            "report",
            "--path",
            validated_report,
            "validate",
            "--full",
        ]
        if source == "pbi-cli"
        else [executable, "validate", validated_report]
    )
    if (
        not isinstance(argv, list)
        or argv != expected_argv
        or not isinstance(cwd, str)
        or not Path(cwd).is_absolute()
        or not isinstance(returncode, int)
        or isinstance(returncode, bool)
        or not isinstance(stdout, str)
        or not isinstance(stderr, str)
        or not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or duration_ms < 0
    ):
        raise PreparationSafetyError(f"Phase-0 {source} raw command receipt is invalid")
    try:
        stdout_json = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise PreparationSafetyError(
            f"Phase-0 {source} stdout is not the captured JSON envelope"
        ) from error
    if stdout_json != payload.get("parsed_json"):
        raise PreparationSafetyError(f"Phase-0 {source} stdout and parsed JSON receipt disagree")
    if source == "microsoft-authoring-cli":
        parsed = payload.get("parsed_json")
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(data, dict) or data.get("reportPath") != validated_report:
            raise PreparationSafetyError(
                "Phase-0 Microsoft validator JSON is not bound to its argv report path"
            )
    receipt = CommandReceipt(
        argv=tuple(argv),
        cwd=Path(cwd),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        parsed_json=payload.get("parsed_json"),  # type: ignore[arg-type]
        duration_ms=duration_ms,
    )
    if not validation_from_receipt(source, receipt).passed:
        raise PreparationSafetyError(f"Phase-0 {source} validation did not pass")


def _validate_raw_json_command(
    value: object,
    *,
    expected_argv: list[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PreparationSafetyError(f"Phase-0 {label} command receipt is missing")
    executable = value.get("executable")
    executable_hash = value.get("executable_sha256")
    stdout = value.get("stdout")
    parsed_json = value.get("parsed_json")
    duration_ms = value.get("duration_ms")
    if (
        not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or not Path(executable).is_file()
        or not _is_sha256_text(executable_hash)
        or sha256_file(Path(executable)) != executable_hash
        or value.get("argv") != expected_argv
        or not isinstance(value.get("cwd"), str)
        or not Path(str(value.get("cwd"))).is_absolute()
        or value.get("returncode") != 0
        or not isinstance(stdout, str)
        or not isinstance(value.get("stderr"), str)
        or not isinstance(parsed_json, dict)
        or not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or duration_ms < 0
    ):
        raise PreparationSafetyError(f"Phase-0 {label} raw command identity is invalid")
    try:
        stdout_json = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise PreparationSafetyError(f"Phase-0 {label} stdout is not JSON") from error
    if stdout_json != parsed_json:
        raise PreparationSafetyError(
            f"Phase-0 {label} stdout differs from its parsed command result"
        )
    return parsed_json


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _require_finite_range(
    payload: dict[str, object],
    field: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    numeric = _finite_number(payload.get(field))
    if numeric is None or numeric < minimum or (maximum is not None and numeric > maximum):
        raise PreparationSafetyError(f"Phase-0 model evaluation has an invalid {field} value")
    return numeric


_MODEL_CORPUS_CATEGORY_COUNTS = {
    "build_modify": 12,
    "numerical_analysis": 24,
    "explain": 8,
    "forecast_scenario": 8,
    "ambiguity": 6,
    "unsupported": 6,
}
_STRICT_OFFLINE_NETWORK_SCRIPT = (
    "$adapters = @(Get-NetAdapter -Physical -ErrorAction Stop | "
    "Select-Object Name, Status, InterfaceGuid); "
    "$enabled = @($adapters | Where-Object { $_.Status -eq 'Up' }); "
    "[pscustomobject]@{adapters=$adapters; enabled=$enabled} | "
    "ConvertTo-Json -Depth 5 -Compress"
)


def _model_corpus_identity(repository: Path) -> tuple[str, list[str]]:
    corpus = repository / "evals" / "cases" / "model_contract_cases.jsonl"
    _reject_symlink_components(corpus)
    if not corpus.is_file():
        raise PreparationSafetyError("frozen 64-case model corpus is missing")
    case_ids: list[str] = []
    category_counts: dict[str, int] = {}
    for line_number, raw_line in enumerate(
        corpus.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as error:
            raise PreparationSafetyError(
                f"frozen model corpus has invalid JSON at line {line_number}"
            ) from error
        if not isinstance(case, dict):
            raise PreparationSafetyError(f"frozen model corpus case {line_number} is not an object")
        case_id = case.get("id")
        category = case.get("category")
        if not isinstance(case_id, str) or not isinstance(category, str):
            raise PreparationSafetyError(
                f"frozen model corpus case {line_number} lacks ID/category identity"
            )
        case_ids.append(case_id)
        category_counts[category] = category_counts.get(category, 0) + 1
    if (
        len(case_ids) != 64
        or len(set(case_ids)) != 64
        or category_counts != _MODEL_CORPUS_CATEGORY_COUNTS
    ):
        raise PreparationSafetyError(
            "frozen model corpus does not match the exact 64-case family contract"
        )
    return sha256_file(corpus), case_ids


def _validate_model_family_metrics(
    value: object,
    *,
    label: str,
) -> None:
    if not isinstance(value, dict):
        raise PreparationSafetyError(f"Phase-0 model evaluation {label} metrics are missing")
    score = value.get("score")
    latency = value.get("latency")
    repeatability = value.get("repeatability")
    numerical = value.get("numerical")
    if not all(isinstance(item, dict) for item in (score, latency, repeatability, numerical)):
        raise PreparationSafetyError(
            f"Phase-0 model evaluation {label} must include score, latency, "
            "repeatability, and numerical families"
        )
    assert isinstance(score, dict)
    assert isinstance(latency, dict)
    assert isinstance(repeatability, dict)
    assert isinstance(numerical, dict)
    if (
        _require_finite_range(score, "route_macro_f1", maximum=1.0) < 0.95
        or _require_finite_range(score, "schema_valid_after_retry", maximum=1.0) < 0.98
        or _require_finite_range(score, "accepted_semantic_ids_rate", maximum=1.0) != 1.0
    ):
        raise PreparationSafetyError(
            f"Phase-0 model evaluation {label} score family misses a release threshold"
        )
    for count_field in ("unsafe_writes", "critical_hallucinations"):
        count = score.get(count_field)
        if not isinstance(count, int) or isinstance(count, bool) or count != 0:
            raise PreparationSafetyError(
                f"Phase-0 model evaluation {label} has a nonzero {count_field}"
            )
    if (
        _require_finite_range(latency, "warm_tokens_per_second") < 6.0
        or _require_finite_range(latency, "warm_p95_seconds") > 18.0
        or _require_finite_range(latency, "model_memory_gb") > 3.3
        or _require_finite_range(latency, "available_memory_gb") < 0.75
    ):
        raise PreparationSafetyError(
            f"Phase-0 model evaluation {label} latency/memory family misses a release threshold"
        )
    if (
        _require_finite_range(
            repeatability,
            "route_agreement_rate",
            maximum=1.0,
        )
        < 0.95
        or _require_finite_range(
            repeatability,
            "plan_schema_agreement_rate",
            maximum=1.0,
        )
        < 0.98
    ):
        raise PreparationSafetyError(
            f"Phase-0 model evaluation {label} repeatability family misses a release threshold"
        )
    evaluated = numerical.get("evaluated_case_count")
    critical = numerical.get("critical_fabrications")
    if (
        not isinstance(evaluated, int)
        or isinstance(evaluated, bool)
        or evaluated != 32
        or _require_finite_range(numerical, "exact_answer_rate", maximum=1.0) != 1.0
        or not isinstance(critical, int)
        or isinstance(critical, bool)
        or critical != 0
    ):
        raise PreparationSafetyError(
            f"Phase-0 model evaluation {label} numerical family is incomplete or inaccurate"
        )


def _model_outcome_macro_f1(outcomes: list[dict[str, object]]) -> float:
    labels = {str(outcome["expected_intent"]) for outcome in outcomes} | {
        str(outcome["actual_intent"]) for outcome in outcomes
    }
    scores: list[float] = []
    for label in labels:
        true_positive = sum(
            outcome["expected_intent"] == label and outcome["actual_intent"] == label
            for outcome in outcomes
        )
        false_positive = sum(
            outcome["expected_intent"] != label and outcome["actual_intent"] == label
            for outcome in outcomes
        )
        false_negative = sum(
            outcome["expected_intent"] == label and outcome["actual_intent"] != label
            for outcome in outcomes
        )
        denominator = (2 * true_positive) + false_positive + false_negative
        scores.append((2 * true_positive) / denominator if denominator else 0.0)
    return sum(scores) / len(scores) if scores else 0.0


def _validate_model_evaluation_receipt(
    evaluation: dict[str, object],
    *,
    pins: Phase0Pins,
    expected_corpus_hash: str,
    expected_case_ids: list[str],
) -> None:
    _validate_receipt_header(evaluation, kind="model_evaluation")
    candidate = evaluation.get("candidate")
    corpus = evaluation.get("corpus")
    repetitions = evaluation.get("repetitions")
    aggregate = evaluation.get("aggregate")
    recorder_source = evaluation.get("recorder_source")
    golden_evidence = evaluation.get("golden_evidence")
    if (
        not isinstance(candidate, dict)
        or candidate.get("model_tag") != pins.model_tag
        or candidate.get("model_digest") != pins.model_digest
    ):
        raise PreparationSafetyError("Phase-0 model evaluation candidate identity failed")
    if not isinstance(corpus, dict):
        raise PreparationSafetyError("Phase-0 model evaluation corpus identity is missing")
    case_ids = corpus.get("case_ids")
    corpus_path = corpus.get("path")
    if (
        corpus.get("case_count") != 64
        or corpus.get("category_counts") != _MODEL_CORPUS_CATEGORY_COUNTS
        or corpus.get("sha256") != expected_corpus_hash
        or not isinstance(case_ids, list)
        or len(case_ids) != 64
        or len(set(case_ids)) != 64
        or not all(isinstance(case_id, str) and case_id for case_id in case_ids)
        or case_ids != expected_case_ids
        or not isinstance(corpus_path, str)
        or not Path(corpus_path).is_absolute()
        or not Path(corpus_path).is_file()
        or sha256_file(Path(corpus_path)) != expected_corpus_hash
    ):
        raise PreparationSafetyError(
            "Phase-0 model evaluation must bind the exact unique 64-case corpus"
        )
    repository_root = Path(corpus_path).parents[2]
    expected_recorder = (
        repository_root / "src" / "ai_powerbi_analyst" / "runtime" / "phase0_capture.py"
    )
    if (
        not isinstance(recorder_source, dict)
        or recorder_source.get("path") != str(expected_recorder)
        or not expected_recorder.is_file()
        or recorder_source.get("sha256") != sha256_file(expected_recorder)
    ):
        raise PreparationSafetyError(
            "Phase-0 model evaluation is not bound to the repository-owned recorder"
        )
    expected_golden_evaluator = (
        repository_root / "src" / "ai_powerbi_analyst" / "evaluation_goldens.py"
    )
    expected_golden_path = repository_root / "evidence" / "golden-answers.jsonl"
    if (
        not isinstance(golden_evidence, dict)
        or golden_evidence.get("evaluator_source_path") != str(expected_golden_evaluator)
        or not expected_golden_evaluator.is_file()
        or golden_evidence.get("evaluator_source_sha256") != sha256_file(expected_golden_evaluator)
        or golden_evidence.get("golden_path") != str(expected_golden_path)
        or not expected_golden_path.is_file()
        or golden_evidence.get("golden_sha256") != sha256_file(expected_golden_path)
    ):
        raise PreparationSafetyError(
            "Phase-0 model evaluation is not bound to the frozen golden evaluator"
        )
    try:
        goldens = load_frozen_goldens(
            expected_golden_path,
            corpus_path=Path(corpus_path),
            fixture_root=repository_root / "fixtures" / "data",
        )
    except (OSError, UnicodeDecodeError, ValidationError, ValueError) as error:
        raise PreparationSafetyError(
            "Phase-0 frozen numerical evidence cannot be verified"
        ) from error
    golden_by_id = {golden.case_id: golden for golden in goldens}
    if (
        golden_evidence.get("case_count") != 32
        or golden_evidence.get("case_ids") != [golden.case_id for golden in goldens]
        or golden_evidence.get("fixture") != goldens[0].fixture.model_dump(mode="json")
    ):
        raise PreparationSafetyError("Phase-0 model evaluation golden inventory or fixture differs")
    try:
        corpus_cases = [
            json.loads(line)
            for line in Path(corpus_path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreparationSafetyError("Phase-0 model corpus cannot be rescored") from error
    if not isinstance(repetitions, list) or len(repetitions) != 3:
        raise PreparationSafetyError(
            "Phase-0 model evaluation requires exactly three complete repetitions"
        )
    outcome_sets: list[list[dict[str, object]]] = []
    for expected_index, repetition in enumerate(repetitions, start=1):
        if (
            not isinstance(repetition, dict)
            or repetition.get("index") != expected_index
            or repetition.get("case_count") != 64
            or repetition.get("case_ids") != case_ids
        ):
            raise PreparationSafetyError(
                "Phase-0 model evaluation repetition does not contain all 64 cases in frozen order"
            )
        _validate_model_family_metrics(
            repetition,
            label=f"repetition {expected_index}",
        )
        raw_outcomes = repetition.get("outcomes")
        if not isinstance(raw_outcomes, list) or len(raw_outcomes) != 64:
            raise PreparationSafetyError("Phase-0 model repetition lacks 64 raw case outcomes")
        outcomes: list[dict[str, object]] = []
        golden_outcomes: list[GoldenCaseOutcome] = []
        for case, raw_outcome in zip(corpus_cases, raw_outcomes, strict=True):
            if not isinstance(case, dict) or not isinstance(raw_outcome, dict):
                raise PreparationSafetyError("Phase-0 model case outcome is invalid")
            latency = _finite_number(raw_outcome.get("latency_seconds"))
            tokens_per_second = _finite_number(raw_outcome.get("tokens_per_second"))
            expected_intent = case.get("expected_intent")
            actual_intent = raw_outcome.get("actual_intent")
            numeric = raw_outcome.get("numeric_answer_correct")
            numeric_category = case.get("category") in {
                "numerical_analysis",
                "forecast_scenario",
            }
            requested_ids = raw_outcome.get("requested_object_ids")
            golden_comparison = raw_outcome.get("golden_comparison")
            actual_plan_payload = raw_outcome.get("actual_plan")
            actual_answer_payload = raw_outcome.get("actual_answer")
            if (
                raw_outcome.get("case_id") != case.get("id")
                or raw_outcome.get("category") != case.get("category")
                or raw_outcome.get("expected_intent") != expected_intent
                or actual_intent not in {intent.value for intent in RouteIntent}
                or raw_outcome.get("route_correct") is not (actual_intent == expected_intent)
                or raw_outcome.get("schema_valid_after_retry") not in {True, False}
                or raw_outcome.get("attempts") not in {1, 2}
                or not isinstance(requested_ids, list)
                or not all(isinstance(item, str) for item in requested_ids)
                or raw_outcome.get("semantic_ids_valid") not in {True, False}
                or raw_outcome.get("plan_valid") not in {True, False}
                or raw_outcome.get("unsafe_write") not in {True, False}
                or latency is None
                or tokens_per_second is None
                or not _is_sha256_text(raw_outcome.get("model_output_sha256"))
                or (numeric_category and numeric not in {True, False})
                or (not numeric_category and numeric is not None)
                or (numeric is True and not _is_sha256_text(raw_outcome.get("evidence_sha256")))
            ):
                raise PreparationSafetyError(
                    "Phase-0 model case outcome failed its closed raw contract"
                )
            if numeric_category:
                if not isinstance(golden_comparison, dict):
                    raise PreparationSafetyError(
                        "Phase-0 numerical outcome lacks its exact golden comparison"
                    )
                try:
                    actual_plan = (
                        AnalysisPlan.model_validate(actual_plan_payload)
                        if actual_plan_payload is not None
                        else None
                    )
                    actual_answer = (
                        EvidenceAnswer.model_validate(actual_answer_payload)
                        if actual_answer_payload is not None
                        else None
                    )
                    comparison_latency = _finite_number(golden_comparison.get("latency_seconds"))
                    comparison_error = golden_comparison.get("error")
                    if comparison_latency is None or (
                        comparison_error is not None and not isinstance(comparison_error, str)
                    ):
                        raise ValueError("invalid golden comparison timing or error")
                    golden = golden_by_id.get(str(case.get("id")))
                    if golden is None:
                        raise ValueError("numerical case has no frozen golden")
                    recomputed_comparison = compare_golden_case(
                        golden,
                        actual_plan=actual_plan,
                        actual_answer=actual_answer,
                        latency_seconds=comparison_latency,
                        error=comparison_error,
                    )
                except (ValidationError, TypeError, ValueError) as error:
                    raise PreparationSafetyError(
                        "Phase-0 numerical outcome payload cannot be rescored"
                    ) from error
                if (
                    golden_comparison != recomputed_comparison.model_dump(mode="json")
                    or numeric is not recomputed_comparison.exact_answer
                    or raw_outcome.get("plan_valid") is not recomputed_comparison.plan_exact
                    or raw_outcome.get("evidence_sha256")
                    != recomputed_comparison.actual_answer_sha256
                ):
                    raise PreparationSafetyError(
                        "Phase-0 numerical outcome differs from exact golden rescoring"
                    )
                golden_outcomes.append(recomputed_comparison)
            elif (
                golden_comparison is not None
                or actual_plan_payload is not None
                or actual_answer_payload is not None
                or raw_outcome.get("evidence_sha256") is not None
            ):
                raise PreparationSafetyError(
                    "Phase-0 nonnumerical outcome contains unexpected evidence"
                )
            outcomes.append(raw_outcome)
        score = repetition.get("score")
        latency_family = repetition.get("latency")
        numerical_family = repetition.get("numerical")
        assert isinstance(score, dict)
        assert isinstance(latency_family, dict)
        assert isinstance(numerical_family, dict)
        golden_summary = summarize_golden_outcomes(golden_outcomes)
        ordered_latencies = sorted(
            value
            for row in outcomes
            if (value := _finite_number(row["latency_seconds"])) is not None
        )
        token_rates = [
            value
            for row in outcomes
            if (value := _finite_number(row["tokens_per_second"])) is not None
        ]
        p95_latency = ordered_latencies[math.ceil(len(ordered_latencies) * 0.95) - 1]
        if (
            abs(float(score["route_macro_f1"]) - _model_outcome_macro_f1(outcomes)) > 1e-12
            or abs(
                float(score["schema_valid_after_retry"])
                - sum(bool(row["schema_valid_after_retry"]) for row in outcomes) / 64
            )
            > 1e-12
            or float(score["accepted_semantic_ids_rate"])
            != sum(bool(row["semantic_ids_valid"]) for row in outcomes) / 64
            or float(score["unsafe_writes"]) != sum(bool(row["unsafe_write"]) for row in outcomes)
            or float(score["critical_hallucinations"]) != golden_summary.critical_fabrications
            or float(numerical_family["evaluated_case_count"])
            != golden_summary.evaluated_case_count
            or float(numerical_family["exact_answer_rate"]) != golden_summary.exact_answer_rate
            or float(numerical_family["critical_fabrications"])
            != golden_summary.critical_fabrications
            or numerical_family.get("exact_answer_count") != golden_summary.exact_answer_count
            or float(latency_family["warm_tokens_per_second"]) != statistics.fmean(token_rates)
            or float(latency_family["warm_p95_seconds"]) != p95_latency
        ):
            raise PreparationSafetyError("Phase-0 model metrics do not recompute from raw outcomes")
        outcome_sets.append(outcomes)
    route_agreements = 0
    plan_agreements = 0
    for case_index in range(64):
        rows = [outcomes[case_index] for outcomes in outcome_sets]
        requested_signatures: list[tuple[object, tuple[str, ...]]] = []
        for row in rows:
            requested = row["requested_object_ids"]
            assert isinstance(requested, list)
            assert all(isinstance(item, str) for item in requested)
            requested_signatures.append((row["actual_intent"], tuple(requested)))
        route_agreements += len(set(requested_signatures)) == 1
        plan_agreements += (
            len({json.dumps(row.get("plan_signature"), sort_keys=True) for row in rows}) == 1
        )
    for repetition in repetitions:
        assert isinstance(repetition, dict)
        repeatability = repetition.get("repeatability")
        if (
            not isinstance(repeatability, dict)
            or float(repeatability.get("route_agreement_rate", -1)) != route_agreements / 64
            or float(repeatability.get("plan_schema_agreement_rate", -1)) != plan_agreements / 64
        ):
            raise PreparationSafetyError(
                "Phase-0 repeatability metrics do not recompute from raw outcomes"
            )
    _validate_model_family_metrics(aggregate, label="aggregate")


def _content_addressed_child(
    *,
    demo_root: Path,
    directory: str,
    digest: object,
    label: str,
) -> dict[str, object]:
    if not _is_sha256_text(digest):
        raise PreparationSafetyError(f"Phase-0 {label} content address is invalid")
    root = demo_root / "evidence" / "phase0" / directory
    if not root.is_dir():
        raise PreparationSafetyError(f"Phase-0 {label} receipt directory is missing")
    matches = [
        candidate
        for candidate in root.glob(f"*-{digest}.json")
        if candidate.is_file() and not _is_link_like(candidate)
    ]
    if len(matches) != 1 or sha256_file(matches[0]) != digest:
        raise PreparationSafetyError(
            f"Phase-0 {label} receipt does not match its unique content address"
        )
    return _load_json_object(matches[0], label=f"Phase-0 {label} receipt")


def _validate_desktop_reopen_receipt(
    payload: dict[str, object],
    *,
    demo_root: Path,
    iteration: int,
    report_hash: str,
    desktop_version: str,
    logical_report_path: Path,
    expected_desktop_identity: dict[str, object],
) -> None:
    _validate_receipt_header(payload, kind="desktop_reopen")
    screenshot_path = payload.get("screenshot_path")
    screenshot_hash = payload.get("screenshot_sha256")
    desktop_executable = payload.get("desktop_executable")
    executable_hash = payload.get("desktop_executable_sha256")
    desktop_identity = payload.get("desktop_identity")
    window_handle = payload.get("window_handle")
    pbip_path = demo_root / "workspace" / "ClientWow.pbip"
    if (
        payload.get("iteration") != iteration
        or payload.get("report_hash") != report_hash
        or payload.get("logical_report_path") != str(logical_report_path.absolute())
        or payload.get("pbip_path") != str(pbip_path.absolute())
        or payload.get("pbip_sha256") != sha256_file(pbip_path)
        or payload.get("desktop_version") != desktop_version
        or payload.get("process_started") is not True
        or payload.get("window_observed") is not True
        or payload.get("foreground_verified") is not True
        or payload.get("page_name") != "Executive Pulse"
        or payload.get("page_selected") is not True
        or not isinstance(window_handle, int)
        or isinstance(window_handle, bool)
        or window_handle <= 0
        or not isinstance(desktop_executable, str)
        or not Path(desktop_executable).is_absolute()
        or not _is_sha256_text(executable_hash)
        or not Path(desktop_executable).is_file()
        or sha256_file(Path(desktop_executable)) != executable_hash
        or not _desktop_identity_matches(
            desktop_identity,
            executable=Path(desktop_executable),
            executable_hash=str(executable_hash),
            version=desktop_version,
        )
        or desktop_identity != expected_desktop_identity
        or not isinstance(screenshot_path, str)
        or Path(screenshot_path).is_absolute()
        or ".." in Path(screenshot_path).parts
        or not _is_sha256_text(screenshot_hash)
    ):
        raise PreparationSafetyError(
            "Phase-0 Desktop reopen receipt is not bound to the mutation and executable"
        )
    screenshot = (demo_root / screenshot_path).absolute()
    _reject_symlink_components(screenshot)
    if (
        not screenshot.is_relative_to(demo_root.absolute())
        or not screenshot.is_file()
        or screenshot.stat().st_size <= 0
        or sha256_file(screenshot) != screenshot_hash
    ):
        raise PreparationSafetyError(
            "Phase-0 Desktop reopen screenshot is missing or hash-mismatched"
        )


def _desktop_identity_matches(
    value: object,
    *,
    executable: Path,
    executable_hash: str,
    version: str,
) -> bool:
    if not isinstance(value, dict):
        return False
    argv = value.get("probe_argv")
    reported = value.get("reported_versions")
    return bool(
        value.get("executable") == str(executable.absolute())
        and value.get("executable_sha256") == executable_hash
        and value.get("product_version") == version
        and isinstance(argv, list)
        and len(argv) >= 6
        and argv[-1] == str(executable.absolute())
        and "-Command" in argv
        and value.get("probe_returncode") == 0
        and isinstance(value.get("probe_cwd"), str)
        and Path(str(value.get("probe_cwd"))).is_absolute()
        and isinstance(value.get("probe_stdout"), str)
        and isinstance(value.get("probe_stderr"), str)
        and isinstance(value.get("probe_duration_ms"), int)
        and not isinstance(value.get("probe_duration_ms"), bool)
        and value.get("probe_duration_ms", -1) >= 0
        and isinstance(reported, list)
        and version in reported
    )


def _validate_phase0_receipts(
    attestation: Phase0Attestation,
    *,
    demo_root: Path,
    expected_hashes: AttestedArtifactHashes,
    model_corpus_hash: str,
    model_case_ids: list[str],
) -> None:
    references = attestation.receipts
    payloads = {
        name: _receipt_payload(reference, demo_root=demo_root, label=name.replace("_", " "))
        for name, reference in references
    }

    environment = payloads["environment"]
    _validate_receipt_header(environment, kind="environment")
    if environment.get("operating_system") != "Windows" or environment.get(
        "pins"
    ) != attestation.pins.model_dump(mode="json"):
        raise PreparationSafetyError(
            "Phase-0 environment receipt does not bind the attested Windows/tool/model pins"
        )
    environment_desktop_identity = environment.get("desktop_identity")
    if not isinstance(environment_desktop_identity, dict):
        raise PreparationSafetyError(
            "Phase-0 environment receipt has no raw Desktop ProductVersion probe"
        )
    desktop_executable = environment_desktop_identity.get("executable")
    desktop_executable_hash = environment_desktop_identity.get("executable_sha256")
    if (
        not isinstance(desktop_executable, str)
        or not Path(desktop_executable).is_absolute()
        or not Path(desktop_executable).is_file()
        or not _is_sha256_text(desktop_executable_hash)
        or sha256_file(Path(desktop_executable)) != desktop_executable_hash
        or not _desktop_identity_matches(
            environment_desktop_identity,
            executable=Path(desktop_executable),
            executable_hash=str(desktop_executable_hash),
            version=attestation.pins.desktop_version,
        )
    ):
        raise PreparationSafetyError(
            "Phase-0 environment Desktop executable/ProductVersion identity failed"
        )
    tool_probes = environment.get("tool_probes")
    tool_executables = environment.get("tool_executables")
    model_probe = environment.get("model_probe")
    windows_probe = environment.get("windows_probe")
    python_record = tool_executables.get("python") if isinstance(tool_executables, dict) else None
    expected_tool_versions = {
        "pbi_cli": attestation.pins.pbi_cli_version,
        "microsoft_validator": attestation.pins.microsoft_validator_version,
        "node": attestation.pins.node_version,
        "ollama": attestation.pins.ollama_version,
    }
    if (
        not isinstance(tool_probes, dict)
        or set(tool_probes) != set(expected_tool_versions)
        or not isinstance(tool_executables, dict)
        or not set(expected_tool_versions).issubset(tool_executables)
        or not isinstance(model_probe, dict)
        or model_probe.get("model_tag") != attestation.pins.model_tag
        or model_probe.get("model_digest") != attestation.pins.model_digest
        or not _is_sha256_text(model_probe.get("response_sha256"))
        or not isinstance(windows_probe, dict)
        or windows_probe.get("version") != attestation.pins.windows_build
        or platform.version() != attestation.pins.windows_build
        or platform.python_version() != attestation.pins.python_version
        or not isinstance(python_record, dict)
        or not isinstance(python_record.get("path"), str)
        or not Path(str(python_record.get("path"))).is_absolute()
        or not Path(str(python_record.get("path"))).is_file()
        or not _is_sha256_text(python_record.get("sha256"))
        or sha256_file(Path(str(python_record.get("path")))) != python_record.get("sha256")
    ):
        raise PreparationSafetyError(
            "Phase-0 environment raw host/tool/model probe inventory is invalid"
        )
    for tool_name, version in expected_tool_versions.items():
        probe = tool_probes.get(tool_name)
        executable_record = tool_executables.get(tool_name)
        if not isinstance(probe, dict) or not isinstance(executable_record, dict):
            raise PreparationSafetyError(f"Phase-0 environment tool probe is missing: {tool_name}")
        executable_path = executable_record.get("path")
        executable_digest = executable_record.get("sha256")
        reported = probe.get("reported_versions")
        banner_matches = isinstance(reported, list) and version in reported
        distribution_matches = (
            tool_name == "pbi_cli"
            and probe.get("distribution_name") == "pbi-cli-tool"
            and probe.get("distribution_version") == version
            and isinstance(executable_path, str)
            and isinstance(executable_digest, str)
            and _valid_pbi_distribution_probe(
                probe.get("distribution_probe"),
                executable=Path(executable_path),
                executable_hash=executable_digest,
                version=version,
            )
        )
        if (
            not isinstance(executable_path, str)
            or not Path(executable_path).is_absolute()
            or not Path(executable_path).is_file()
            or not _is_sha256_text(executable_digest)
            or sha256_file(Path(executable_path)) != executable_digest
            or probe.get("argv") != [executable_path, "--version"]
            or probe.get("returncode") != 0
            or probe.get("accepted_version") != version
            or not (banner_matches or distribution_matches)
        ):
            raise PreparationSafetyError(
                f"Phase-0 environment tool executable identity/version failed: {tool_name}"
            )

    kill_test = payloads["kill_test"]
    _validate_receipt_header(kill_test, kind="kill_test")
    iterations = kill_test.get("iterations")
    if not isinstance(iterations, list) or len(iterations) != 10:
        raise PreparationSafetyError("Phase-0 kill-test receipt requires exactly 10 iterations")
    mutation_evidence: dict[str, tuple[str, tuple[str, ...]]] = {}
    logical_report_path = demo_root / "workspace" / "ClientWow.Report"
    for expected_iteration, iteration in enumerate(iterations, start=1):
        if not isinstance(iteration, dict):
            raise PreparationSafetyError("Phase-0 kill-test iteration is invalid")
        before = iteration.get("before_hash")
        after = iteration.get("after_hash")
        undo = iteration.get("undo_hash")
        recipe_hash = iteration.get("mutation_recipe_hash")
        validation_hashes = iteration.get("validation_receipt_hashes")
        desktop_receipt_hash = iteration.get("desktop_reopen_receipt_hash")
        if (
            iteration.get("iteration") != expected_iteration
            or not _is_sha256_text(before)
            or not _is_sha256_text(after)
            or before == after
            or before != expected_hashes.report_hash
            or undo != before
            or iteration.get("mutation_output_hash") != after
            or not _is_sha256_text(recipe_hash)
            or iteration.get("desktop_reopen_verified") is not True
            or not isinstance(validation_hashes, list)
            or len(validation_hashes) != 2
            or len(set(validation_hashes)) != 2
            or not all(_is_sha256_text(item) for item in validation_hashes)
            or not _is_sha256_text(desktop_receipt_hash)
        ):
            raise PreparationSafetyError(
                "Phase-0 kill-test iteration lacks exact rollback, validation, or Desktop proof"
            )
        recipe = _content_addressed_child(
            demo_root=demo_root,
            directory="recipes",
            digest=recipe_hash,
            label=f"mutation recipe {expected_iteration}",
        )
        _validate_receipt_header(recipe, kind="mutation_recipe")
        if (
            recipe.get("iteration") != expected_iteration
            or recipe.get("base_report_hash") != before
            or not isinstance(recipe.get("report_change_plan"), dict)
        ):
            raise PreparationSafetyError(
                "Phase-0 mutation recipe is not bound to its baseline iteration"
            )
        validator_sources: set[str] = set()
        for validator_hash in validation_hashes:
            command = _content_addressed_child(
                demo_root=demo_root,
                directory="commands",
                digest=validator_hash,
                label=f"mutation validator {expected_iteration}",
            )
            source = command.get("source")
            if source not in {"pbi-cli", "microsoft-authoring-cli"}:
                raise PreparationSafetyError("Phase-0 mutation validator has an unapproved source")
            validator_sources.add(source)
            _validate_command_receipt(
                command,
                source=source,
                logical_report_path=logical_report_path,
                baseline_hash=expected_hashes.report_hash,
                report_hash=str(after),
                tool_version=(
                    attestation.pins.pbi_cli_version
                    if source == "pbi-cli"
                    else attestation.pins.microsoft_validator_version
                ),
            )
        if validator_sources != {"pbi-cli", "microsoft-authoring-cli"}:
            raise PreparationSafetyError(
                "Phase-0 mutation requires one receipt from each independent validator"
            )
        desktop_reopen = _content_addressed_child(
            demo_root=demo_root,
            directory="desktop-reopen",
            digest=desktop_receipt_hash,
            label=f"Desktop reopen {expected_iteration}",
        )
        _validate_desktop_reopen_receipt(
            desktop_reopen,
            demo_root=demo_root,
            iteration=expected_iteration,
            report_hash=str(after),
            desktop_version=attestation.pins.desktop_version,
            logical_report_path=logical_report_path,
            expected_desktop_identity=environment_desktop_identity,
        )
        mutation_evidence[str(after)] = (
            str(recipe_hash),
            tuple(str(value) for value in validation_hashes),
        )

    evaluation = payloads["model_evaluation"]
    _validate_model_evaluation_receipt(
        evaluation,
        pins=attestation.pins,
        expected_corpus_hash=model_corpus_hash,
        expected_case_ids=model_case_ids,
    )

    offline = payloads["strict_offline"]
    _validate_receipt_header(offline, kind="strict_offline")
    if not all(
        offline.get(field) is True
        for field in (
            "non_loopback_network_disabled",
            "complete_flow_passed",
            "schema_verified",
        )
    ):
        raise PreparationSafetyError("Phase-0 strict-offline receipt is incomplete")
    repository_root = offline.get("repository_root")
    offline_demo_root = offline.get("demo_root")
    network_probes = offline.get("network_probes")
    flow_command = offline.get("flow_command")
    schema_command = offline.get("schema_command")
    offline_recorder_source = offline.get("recorder_source")
    if (
        not isinstance(repository_root, str)
        or not Path(repository_root).is_absolute()
        or not Path(repository_root).is_dir()
        or offline_demo_root != str(demo_root.absolute())
        or not isinstance(network_probes, list)
        or len(network_probes) != 2
        or not isinstance(flow_command, dict)
        or not isinstance(schema_command, dict)
        or not isinstance(offline_recorder_source, dict)
    ):
        raise PreparationSafetyError("Phase-0 strict-offline raw command inventory is incomplete")
    corpus_path_value = evaluation.get("corpus")
    attested_corpus_path = (
        corpus_path_value.get("path") if isinstance(corpus_path_value, dict) else None
    )
    expected_corpus_path = Path(repository_root) / "evals" / "cases" / "model_contract_cases.jsonl"
    if attested_corpus_path != str(expected_corpus_path):
        raise PreparationSafetyError(
            "Phase-0 strict-offline repository does not own the evaluated corpus"
        )
    expected_offline_recorder = (
        Path(repository_root) / "src" / "ai_powerbi_analyst" / "runtime" / "phase0_capture.py"
    )
    if (
        offline_recorder_source.get("path") != str(expected_offline_recorder)
        or not expected_offline_recorder.is_file()
        or offline_recorder_source.get("sha256") != sha256_file(expected_offline_recorder)
    ):
        raise PreparationSafetyError(
            "Phase-0 strict-offline proof is not bound to the repository-owned recorder"
        )
    for probe in network_probes:
        if not isinstance(probe, dict):
            raise PreparationSafetyError("Phase-0 offline network probe is missing")
        probe_executable = probe.get("executable")
        if not isinstance(probe_executable, str):
            raise PreparationSafetyError("Phase-0 offline network probe executable is missing")
        parsed_probe = _validate_raw_json_command(
            probe,
            expected_argv=[
                probe_executable,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _STRICT_OFFLINE_NETWORK_SCRIPT,
            ],
            label="offline network probe",
        )
        enabled = parsed_probe.get("enabled")
        if enabled not in (None, []):
            raise PreparationSafetyError(
                "Phase-0 offline network probe observed an enabled physical adapter"
            )
    flow_executable = flow_command.get("executable")
    schema_executable = schema_command.get("executable")
    if not isinstance(flow_executable, str) or not isinstance(schema_executable, str):
        raise PreparationSafetyError("Phase-0 offline evidence executables are missing")
    flow_result = _validate_raw_json_command(
        flow_command,
        expected_argv=[
            flow_executable,
            "-m",
            "ai_powerbi_analyst.runtime.phase0_capture",
            "offline-flow-check",
            "--repository-root",
            repository_root,
            "--demo-root",
            str(demo_root.absolute()),
        ],
        label="strict-offline complete flow",
    )
    schema_result = _validate_raw_json_command(
        schema_command,
        expected_argv=[
            schema_executable,
            "-m",
            "ai_powerbi_analyst.runtime.phase0_capture",
            "offline-schema-check",
            "--repository-root",
            repository_root,
            "--demo-root",
            str(demo_root.absolute()),
        ],
        label="strict-offline schema verification",
    )
    if (
        flow_result.get("status") != "passed"
        or flow_result.get("complete_flow_passed") is not True
        or schema_result.get("status") != "passed"
        or schema_result.get("schema_verified") is not True
    ):
        raise PreparationSafetyError(
            "Phase-0 strict-offline command results did not prove both gates"
        )

    desktop = payloads["desktop_render"]
    _validate_receipt_header(desktop, kind="desktop_render")
    pages = desktop.get("pages_rendered")
    derived_report_hash = desktop.get("report_hash")
    linked_mutation = (
        mutation_evidence.get(derived_report_hash) if isinstance(derived_report_hash, str) else None
    )
    desktop_validation_hashes = desktop.get("validation_receipt_hashes")
    cold_validation_hashes = desktop.get("cold_reset_validation_receipt_hashes")
    cache_size_bytes = desktop.get("cache_size_bytes")
    page_screenshots = desktop.get("page_screenshots")
    page_capture_evidence = desktop.get("page_capture_evidence")
    desktop_window_handle = desktop.get("window_handle")
    required_pages = {"Executive Pulse", "Margin Drivers", "Outlook & Decisions"}
    if (
        linked_mutation is None
        or desktop.get("baseline_report_hash") != expected_hashes.report_hash
        or desktop.get("mutation_recipe_hash") != linked_mutation[0]
        or not isinstance(desktop_validation_hashes, list)
        or tuple(desktop_validation_hashes) != linked_mutation[1]
        or desktop.get("semantic_model_cache_hash") != expected_hashes.semantic_model_cache_hash
        or desktop.get("desktop_version") != attestation.pins.desktop_version
        or desktop.get("desktop_identity") != environment_desktop_identity
        or desktop.get("cold_reset_populated_render_verified") is not True
        or desktop.get("cold_reset_started_from_report_hash") != expected_hashes.report_hash
        or desktop.get("cold_reset_derived_report_hash") != derived_report_hash
        or desktop.get("cold_reset_baseline_workspace_hash")
        != expected_hashes.baseline_workspace_hash
        or desktop.get("cold_reset_cache_hash") != expected_hashes.semantic_model_cache_hash
        or not isinstance(cold_validation_hashes, list)
        or len(cold_validation_hashes) != 2
        or len(set(cold_validation_hashes)) != 2
        or not all(_is_sha256_text(value) for value in cold_validation_hashes)
        or (linked_mutation is not None and set(cold_validation_hashes) == set(linked_mutation[1]))
        or not isinstance(cache_size_bytes, int)
        or isinstance(cache_size_bytes, bool)
        or cache_size_bytes <= 0
        or not isinstance(pages, list)
        or set(pages) != required_pages
        or not isinstance(page_screenshots, dict)
        or set(page_screenshots) != required_pages
        or desktop.get("foreground_verified") is not True
        or not isinstance(desktop_window_handle, int)
        or isinstance(desktop_window_handle, bool)
        or desktop_window_handle <= 0
        or not isinstance(page_capture_evidence, list)
        or len(page_capture_evidence) != 3
    ):
        raise PreparationSafetyError(
            "Phase-0 Desktop render receipt is not bound to all three derived pages"
        )
    for page_name, page_evidence in zip(
        ("Executive Pulse", "Margin Drivers", "Outlook & Decisions"),
        page_capture_evidence,
        strict=True,
    ):
        screenshot_record = page_screenshots.get(page_name)
        if (
            not isinstance(page_evidence, dict)
            or page_evidence.get("name") != page_name
            or page_evidence.get("selected") is not True
            or not isinstance(screenshot_record, dict)
            or page_evidence.get("sha256") != screenshot_record.get("sha256")
        ):
            raise PreparationSafetyError(
                "Phase-0 Desktop page capture is not bound to selected page identity"
            )
    cold_sources: set[str] = set()
    for validator_hash in cold_validation_hashes:
        command = _content_addressed_child(
            demo_root=demo_root,
            directory="commands",
            digest=validator_hash,
            label="cold-reset validator",
        )
        source = command.get("source")
        if source not in {"pbi-cli", "microsoft-authoring-cli"}:
            raise PreparationSafetyError("Phase-0 cold-reset validator source is invalid")
        cold_sources.add(source)
        _validate_command_receipt(
            command,
            source=source,
            logical_report_path=logical_report_path,
            baseline_hash=expected_hashes.report_hash,
            report_hash=str(derived_report_hash),
            tool_version=(
                attestation.pins.pbi_cli_version
                if source == "pbi-cli"
                else attestation.pins.microsoft_validator_version
            ),
        )
    if cold_sources != {"pbi-cli", "microsoft-authoring-cli"}:
        raise PreparationSafetyError(
            "Phase-0 cold reset requires both independent validator receipts"
        )
    reset_backup_path = desktop.get("cold_reset_backup_path")
    if (
        not isinstance(reset_backup_path, str)
        or Path(reset_backup_path).is_absolute()
        or ".." in Path(reset_backup_path).parts
    ):
        raise PreparationSafetyError("Phase-0 cold-reset backup path is invalid")
    reset_backup = (demo_root / reset_backup_path).absolute()
    if (
        not reset_backup.is_relative_to((demo_root / "backups").absolute())
        or not reset_backup.is_dir()
    ):
        raise PreparationSafetyError(
            "Phase-0 cold-reset receipt has no retained recoverable workspace backup"
        )
    screenshot_hashes: set[str] = set()
    for page_name in sorted(required_pages):
        screenshot_record = page_screenshots.get(page_name)
        if not isinstance(screenshot_record, dict):
            raise PreparationSafetyError(
                f"Phase-0 Desktop render screenshot is missing: {page_name}"
            )
        relative = screenshot_record.get("path")
        digest = screenshot_record.get("sha256")
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not _is_sha256_text(digest)
        ):
            raise PreparationSafetyError(
                f"Phase-0 Desktop render screenshot identity is invalid: {page_name}"
            )
        screenshot = (demo_root / relative).absolute()
        _reject_symlink_components(screenshot)
        if (
            not screenshot.is_relative_to(demo_root.absolute())
            or not screenshot.is_file()
            or screenshot.stat().st_size <= 0
            or sha256_file(screenshot) != digest
        ):
            raise PreparationSafetyError(
                f"Phase-0 Desktop render screenshot is missing or changed: {page_name}"
            )
        screenshot_hashes.add(str(digest))
    if len(screenshot_hashes) != len(required_pages):
        raise PreparationSafetyError(
            "Phase-0 Desktop render requires a distinct captured screenshot for every page"
        )

    _validate_command_receipt(
        payloads["pbi_cli_validation"],
        source="pbi-cli",
        logical_report_path=logical_report_path,
        baseline_hash=expected_hashes.report_hash,
        report_hash=expected_hashes.report_hash,
        tool_version=attestation.pins.pbi_cli_version,
    )
    _validate_command_receipt(
        payloads["microsoft_validation"],
        source="microsoft-authoring-cli",
        logical_report_path=logical_report_path,
        baseline_hash=expected_hashes.report_hash,
        report_hash=expected_hashes.report_hash,
        tool_version=attestation.pins.microsoft_validator_version,
    )


def _is_sha256_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _attestation_receipt_hashes(attestation: Phase0Attestation) -> dict[str, str]:
    return {name: str(reference.sha256) for name, reference in attestation.receipts}


def _attested_capabilities() -> ProjectCapabilities:
    return ProjectCapabilities(
        can_analyze=True,
        can_explain=True,
        can_build=True,
        can_modify=True,
        can_open_desktop=True,
        offline_schema_verified=True,
        desktop_render_verified=True,
        phase0_kill_test_passed=True,
    )


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    """Replace one file from the same directory so readers never see partial bytes."""

    _reject_symlink_components(path)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    """Serialize and atomically replace a JSON object."""

    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_bytes_atomic(path, content)


_DESKTOP_LOCAL_MODEL_FILES = frozenset(
    {
        ".pbi/cache.abf",
        ".pbi/localSettings.json",
    }
)

_CACHE_SEAL_INTENT = ".cache-seal-recovery.json"
_CACHE_SEAL_STAGE = ".cache-seal-stage"
_CACHE_SEAL_BUNDLE_BACKUP = ".cache-seal-project-bundle.backup"


def _cache_seal_fault_point(_stage: str) -> None:
    """Test hook that models a process death between durable checkpoints."""


def _cache_seal_pbi_matches(
    pbi_root: Path,
    *,
    cache_sha256: str,
    settings_sha256: str | None,
) -> bool:
    if not pbi_root.is_dir() or _is_link_like(pbi_root):
        return False
    inventory = {candidate.relative_to(pbi_root).as_posix() for candidate in pbi_root.iterdir()}
    expected = {"cache.abf"}
    if settings_sha256 is not None:
        expected.add("localSettings.json")
    if inventory != expected:
        return False
    cache = pbi_root / "cache.abf"
    settings = pbi_root / "localSettings.json"
    if not cache.is_file() or cache.stat().st_size <= 0 or sha256_file(cache) != cache_sha256:
        return False
    return settings_sha256 is None or (
        settings.is_file() and sha256_file(settings) == settings_sha256
    )


def _remove_governed_cache_seal_directory(pbi_root: Path) -> None:
    """Remove only the two files the cache-seal transaction owns."""

    if not pbi_root.exists():
        return
    _reject_symlink_tree(pbi_root)
    inventory = {candidate.relative_to(pbi_root).as_posix() for candidate in pbi_root.iterdir()}
    if not inventory.issubset({"cache.abf", "localSettings.json"}):
        raise PreparationSafetyError(
            "cache-seal recovery found an unowned file in the baseline .pbi directory"
        )
    for name in ("localSettings.json", "cache.abf"):
        (pbi_root / name).unlink(missing_ok=True)
    pbi_root.rmdir()


def _recover_cache_seal(
    *,
    root: Path,
    baseline_model: Path,
    live_model: Path,
    bundle_path: Path,
) -> CacheSealReceipt | None:
    """Complete or roll back a cache seal interrupted by a hard process stop."""

    intent_path = root / _CACHE_SEAL_INTENT
    stage_root = root / _CACHE_SEAL_STAGE
    backup_path = root / _CACHE_SEAL_BUNDLE_BACKUP
    if not intent_path.exists():
        if stage_root.exists() or backup_path.exists():
            raise PreparationSafetyError(
                "orphaned cache-seal recovery artifacts exist without a durable intent"
            )
        return None
    intent = _load_json_object(intent_path, label="cache-seal recovery intent")
    status = intent.get("status")
    cache_sha256 = intent.get("cache_sha256")
    settings_sha256 = intent.get("settings_sha256")
    original_bundle_sha256 = intent.get("original_bundle_sha256")
    if (
        status
        not in {
            "intent_written",
            "staged",
            "baseline_copied",
            "bundle_updated",
            "committed",
        }
        or not isinstance(cache_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", cache_sha256)
        or (
            settings_sha256 is not None
            and (
                not isinstance(settings_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", settings_sha256)
            )
        )
        or not isinstance(original_bundle_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", original_bundle_sha256)
    ):
        raise PreparationSafetyError("cache-seal recovery intent is invalid")
    baseline_pbi = baseline_model / ".pbi"
    live_cache = live_model / ".pbi" / "cache.abf"
    if status == "committed":
        bundle = _load_reset_bundle(bundle_path, root / "workspace")
        if (
            not _cache_seal_pbi_matches(
                baseline_pbi,
                cache_sha256=cache_sha256,
                settings_sha256=settings_sha256,
            )
            or not live_cache.is_file()
            or sha256_file(live_cache) != cache_sha256
            or bundle.semantic_model_hash != hash_tree(live_model)
            or bundle.baseline_workspace_hash != hash_tree(root / "baseline")
        ):
            raise PreparationSafetyError(
                "committed cache-seal recovery record does not match the frozen baseline"
            )
        if stage_root.exists():
            shutil.rmtree(stage_root)
        backup_path.unlink(missing_ok=True)
        return CacheSealReceipt(
            cache_path=live_cache,
            cache_sha256=cache_sha256,
            cache_size_bytes=live_cache.stat().st_size,
            semantic_model_hash=str(bundle.semantic_model_hash),
            baseline_workspace_hash=str(bundle.baseline_workspace_hash),
        )

    backup_valid = backup_path.is_file() and sha256_file(backup_path) == original_bundle_sha256
    current_bundle_is_original = (
        bundle_path.is_file() and sha256_file(bundle_path) == original_bundle_sha256
    )
    if not current_bundle_is_original and not backup_valid:
        raise PreparationSafetyError("cache-seal bundle backup failed recovery verification")
    if baseline_pbi.exists():
        if not _cache_seal_pbi_matches(
            baseline_pbi,
            cache_sha256=cache_sha256,
            settings_sha256=settings_sha256,
        ):
            raise PreparationSafetyError(
                "interrupted cache seal left an unrecognized baseline .pbi directory"
            )
        _remove_governed_cache_seal_directory(baseline_pbi)
    if not current_bundle_is_original:
        _write_bytes_atomic(bundle_path, backup_path.read_bytes())
    if stage_root.exists():
        _reject_symlink_tree(stage_root)
        allowed_stage = {".pbi", ".pbi/cache.abf", ".pbi/localSettings.json"}
        if any(
            candidate.relative_to(stage_root).as_posix() not in allowed_stage
            for candidate in stage_root.rglob("*")
        ):
            raise PreparationSafetyError("cache-seal staging directory has unowned files")
        shutil.rmtree(stage_root)
    backup_path.unlink(missing_ok=True)
    intent_path.unlink()
    return None


def _semantic_model_manifest_without_desktop_locals(root: Path) -> dict[str, str]:
    """Inventory a semantic model while excluding two governed Desktop locals.

    The comparison is deliberately path-and-content based.  A Desktop refresh
    may add ``cache.abf`` and ``localSettings.json``; any TMDL, metadata, or
    additional local file drift blocks cache sealing.
    """

    resolved = root.resolve(strict=True)
    _reject_symlink_tree(resolved)
    manifest: dict[str, str] = {}
    for candidate in sorted(
        resolved.rglob("*"),
        key=lambda path: path.relative_to(resolved).as_posix(),
    ):
        relative = candidate.relative_to(resolved).as_posix()
        if relative in _DESKTOP_LOCAL_MODEL_FILES or relative == ".pbi":
            continue
        if candidate.is_dir():
            manifest[relative] = "D"
        elif candidate.is_file():
            manifest[relative] = "F:" + sha256_file(candidate)
        else:
            raise PreparationSafetyError(
                f"unsupported semantic-model filesystem object: {candidate}"
            )
    return manifest


def seal_populated_semantic_cache(
    repository_root: str | Path,
    demo_root: str | Path,
) -> CacheSealReceipt:
    """Freeze an offline-refreshed Desktop import cache into the reset baseline.

    Power BI Desktop must already have opened, refreshed, and saved the prepared
    PBIP, then been closed.  This operation never creates data.  It permits only
    the two documented Desktop-local semantic-model files to differ from the
    hydrated reset baseline and fails closed on every other change.
    """

    if platform.system().casefold() != "windows":
        raise PreparationSafetyError("semantic-model cache sealing can only run on Windows")
    root = _safe_destination(demo_root)
    if not root.is_dir():
        raise PreparationSafetyError(f"prepared demo root is missing: {root}")
    with FileLock(str(root / ".operation.lock"), timeout=5):
        assert_powerbi_desktop_closed()
        workspace, baseline, bundle_path = _prepared_paths(root)
        if not workspace.is_dir() or not baseline.is_dir() or not bundle_path.is_file():
            raise PreparationSafetyError(
                "cache sealing requires a prepared workspace, reset baseline, and bundle"
            )
        bundle = _load_reset_bundle(bundle_path, workspace)
        if (
            bundle.phase0_attestation_hash is not None
            or bundle.capabilities.can_build
            or bundle.capabilities.can_modify
            or bundle.capabilities.can_open_desktop
        ):
            raise PreparationSafetyError("an attested demo cannot have its cache resealed")

        live_model = workspace / "ClientWow.SemanticModel"
        baseline_model = baseline / "ClientWow.SemanticModel"
        recovered = _recover_cache_seal(
            root=root,
            baseline_model=baseline_model,
            live_model=live_model,
            bundle_path=bundle_path,
        )
        if recovered is not None:
            return recovered
        bundle = _load_reset_bundle(bundle_path, workspace)
        if _semantic_model_manifest_without_desktop_locals(
            live_model
        ) != _semantic_model_manifest_without_desktop_locals(baseline_model):
            raise PreparationSafetyError(
                "Desktop refresh changed semantic-model files outside the governed "
                "cache/local settings"
            )

        live_cache = live_model / ".pbi" / "cache.abf"
        _reject_symlink_components(live_cache)
        if not live_cache.is_file() or live_cache.stat().st_size <= 0:
            raise PreparationSafetyError(
                "offline-refreshed semantic model has no nonempty .pbi/cache.abf"
            )
        live_settings = live_model / ".pbi" / "localSettings.json"
        if live_settings.exists():
            _reject_symlink_components(live_settings)
            if not live_settings.is_file():
                raise PreparationSafetyError("Desktop local settings must be a regular file")
            _load_json_object(live_settings, label="Power BI Desktop local settings")

        baseline_cache = baseline_model / ".pbi" / "cache.abf"
        baseline_settings = baseline_model / ".pbi" / "localSettings.json"
        if baseline_cache.exists() or baseline_settings.exists():
            raise PreparationSafetyError("reset baseline already contains a sealed Desktop cache")

        original_bundle = bundle_path.read_bytes()
        cache_sha256 = sha256_file(live_cache)
        settings_sha256 = sha256_file(live_settings) if live_settings.is_file() else None
        stage_root = root / _CACHE_SEAL_STAGE
        stage_pbi = stage_root / ".pbi"
        backup_path = root / _CACHE_SEAL_BUNDLE_BACKUP
        intent_path = root / _CACHE_SEAL_INTENT
        if stage_root.exists() or backup_path.exists() or intent_path.exists():
            raise PreparationSafetyError("cache-seal transaction artifacts already exist")
        intent: dict[str, object] = {
            "schema_version": "1.0.0",
            "status": "intent_written",
            "cache_sha256": cache_sha256,
            "settings_sha256": settings_sha256,
            "original_bundle_sha256": hashlib.sha256(original_bundle).hexdigest(),
        }
        _write_json_atomic(intent_path, intent)
        _cache_seal_fault_point("after_intent")
        try:
            backup_path.write_bytes(original_bundle)
            stage_pbi.mkdir(parents=True)
            shutil.copy2(live_cache, stage_pbi / "cache.abf")
            if live_settings.is_file():
                shutil.copy2(live_settings, stage_pbi / "localSettings.json")
            if not _cache_seal_pbi_matches(
                stage_pbi,
                cache_sha256=cache_sha256,
                settings_sha256=settings_sha256,
            ):
                raise PreparationSafetyError("cache-seal staging copy failed verification")
            intent["status"] = "staged"
            _write_json_atomic(intent_path, intent)
            _cache_seal_fault_point("after_stage")
            stage_pbi.replace(baseline_cache.parent)
            intent["status"] = "baseline_copied"
            _write_json_atomic(intent_path, intent)
            _cache_seal_fault_point("after_cache_copy")
            if not _cache_seal_pbi_matches(
                baseline_cache.parent,
                cache_sha256=cache_sha256,
                settings_sha256=settings_sha256,
            ):
                raise PreparationSafetyError("sealed reset cache failed its copy hash check")
            if hash_tree(live_model) != hash_tree(baseline_model):
                raise PreparationSafetyError(
                    "live and reset semantic models differ after cache sealing"
                )
            semantic_hash = hash_tree(live_model)
            baseline_workspace_hash = hash_tree(baseline)
            updated = bundle.model_copy(
                update={
                    "semantic_model_hash": semantic_hash,
                    "baseline_workspace_hash": baseline_workspace_hash,
                }
            )
            _write_json_atomic(bundle_path, json.loads(updated.model_dump_json()))
            intent["status"] = "bundle_updated"
            _write_json_atomic(intent_path, intent)
            # Exercise the complete ordinary preparation verifier before the
            # cache is allowed to become attestation evidence.
            verify_windows_demo(repository_root, root)
            intent["status"] = "committed"
            _write_json_atomic(intent_path, intent)
            shutil.rmtree(stage_root)
            backup_path.unlink()
        except Exception:
            _recover_cache_seal(
                root=root,
                baseline_model=baseline_model,
                live_model=live_model,
                bundle_path=bundle_path,
            )
            raise

        return CacheSealReceipt(
            cache_path=live_cache,
            cache_sha256=sha256_file(live_cache),
            cache_size_bytes=live_cache.stat().st_size,
            semantic_model_hash=semantic_hash,
            baseline_workspace_hash=baseline_workspace_hash,
        )


def _update_fixture_manifest(
    workspace: Path,
    inputs: _FrozenInputs,
) -> None:
    manifest_path = workspace / "fixture-manifest.json"
    payload = _load_json_object(manifest_path, label="Power BI fixture manifest")
    payload["status"] = "phase0-attestation-required"
    payload["semantic_model_state"] = "hydrated-from-frozen-absolute-parquet-sources"
    payload["desktop_render_verified"] = False
    payload["catalog_version"] = str(inputs.manifest["version"])
    payload["catalog_hash"] = str(inputs.files["catalog"]["sha256"])
    payload["data_hash"] = str(inputs.manifest["data_hash"])
    payload["data_through"] = str(inputs.manifest["data_through"])
    payload["hydrated_sources"] = {
        key: {
            "path": str(_source_path(inputs, key)),
            "sha256": str(inputs.files[key]["sha256"]),
        }
        for key in _HYDRATED_SOURCE_RECORDS
    }
    _write_json_atomic(manifest_path, payload)


def prepare_windows_demo(
    repository_root: str | Path,
    demo_root: str | Path,
) -> PreparedWindowsDemo:
    """Create one hydrated live workspace and an immutable reset baseline.

    ``demo_root`` must not already exist.  That rule makes repeated preparation
    incapable of overwriting a report that a user or a prior run has changed.
    """

    inputs = _load_frozen_inputs(repository_root)
    destination = _safe_destination(demo_root)
    if destination.exists():
        raise PreparationSafetyError(f"demo workspace already exists: {destination}")
    if destination.is_relative_to(inputs.template) or destination.is_relative_to(inputs.data_root):
        raise PreparationSafetyError("demo workspace cannot be created inside a frozen fixture")

    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(destination.parent)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.preparing-", dir=destination.parent))
    try:
        workspace = stage / "workspace"
        baseline = stage / "baseline"
        shutil.copytree(inputs.template, workspace)
        _reject_symlink_tree(workspace)
        _hydrate_semantic_model(workspace, inputs)

        pbip_path = workspace / "ClientWow.pbip"
        report_path = workspace / "ClientWow.Report"
        semantic_model_path = workspace / "ClientWow.SemanticModel"
        validate_pbip_entrypoint(pbip_path, report_path)
        validate_report_semantic_reference(report_path, semantic_model_path)
        pbip_hash = sha256_file(pbip_path)
        report_hash = hash_tree(report_path)
        semantic_model_hash = hash_tree(semantic_model_path)
        expected_hashes = AttestedArtifactHashes(
            data_hash=str(inputs.manifest["data_hash"]),
            source_data_hash=str(inputs.files["sales_parquet"]["sha256"]),
            source_hashes={key: str(inputs.files[key]["sha256"]) for key in _SOURCE_RECORDS},
            duckdb_hash=str(inputs.files["duckdb"]["sha256"]),
            catalog_hash=str(inputs.files["catalog"]["sha256"]),
            pbip_hash=pbip_hash,
            report_hash=report_hash,
            semantic_model_hash=semantic_model_hash,
            semantic_model_cache_hash=None,
            baseline_workspace_hash=None,
        )
        _update_fixture_manifest(workspace, inputs)
        baseline_workspace_hash = hash_tree(workspace)
        shutil.copytree(workspace, baseline)

        data_through = inputs.manifest.get("data_through")
        if not isinstance(data_through, str):
            raise PreparationSafetyError("analytical manifest has no data cutoff")
        bundle = ProjectBundle(
            id="sales-margin-demo",
            name="Sales & Margin",
            pbip_path=destination / "workspace" / pbip_path.name,
            report_path=destination / "workspace" / report_path.name,
            semantic_model_path=destination / "workspace" / semantic_model_path.name,
            duckdb_path=_source_path(inputs, "duckdb"),
            catalog_path=_source_path(inputs, "catalog"),
            data_hash=expected_hashes.data_hash,
            source_data_hash=expected_hashes.source_data_hash,
            source_hashes=expected_hashes.source_hashes,
            duckdb_hash=expected_hashes.duckdb_hash,
            catalog_hash=expected_hashes.catalog_hash,
            pbip_hash=expected_hashes.pbip_hash,
            report_hash=expected_hashes.report_hash,
            baseline_report_hash=expected_hashes.report_hash,
            baseline_workspace_hash=baseline_workspace_hash,
            semantic_model_hash=expected_hashes.semantic_model_hash,
            catalog_version=str(inputs.manifest.get("version", "1.0.0")),
            data_through=date.fromisoformat(data_through),
            forecast_version=str(inputs.manifest.get("forecast_version", "1.0.0")),
            scenario_version=str(inputs.manifest.get("scenario_version", "1.0.0")),
            baseline_validation_status="pending",
            compatibility=_default_compatibility(inputs.manifest),
            capabilities=_fail_closed_capabilities(),
        )
        bundle_path = stage / "project-bundle.json"
        bundle_path.write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8")
        stage.rename(destination)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise

    return PreparedWindowsDemo(
        root=destination,
        workspace_path=destination / "workspace",
        baseline_path=destination / "baseline",
        bundle_path=destination / "project-bundle.json",
        attested=False,
    )


def _prepared_paths(root: Path) -> tuple[Path, Path, Path]:
    return root / "workspace", root / "baseline", root / "project-bundle.json"


def _current_artifact_hashes(
    inputs: _FrozenInputs,
    workspace: Path,
) -> AttestedArtifactHashes:
    cache_path = workspace / "ClientWow.SemanticModel" / ".pbi" / "cache.abf"
    baseline = workspace.parent / "baseline"
    return AttestedArtifactHashes(
        data_hash=str(inputs.manifest["data_hash"]),
        source_data_hash=str(inputs.files["sales_parquet"]["sha256"]),
        source_hashes={key: str(inputs.files[key]["sha256"]) for key in _SOURCE_RECORDS},
        duckdb_hash=str(inputs.files["duckdb"]["sha256"]),
        catalog_hash=str(inputs.files["catalog"]["sha256"]),
        pbip_hash=sha256_file(workspace / "ClientWow.pbip"),
        report_hash=hash_tree(workspace / "ClientWow.Report"),
        semantic_model_hash=hash_tree(workspace / "ClientWow.SemanticModel"),
        semantic_model_cache_hash=(
            sha256_file(cache_path)
            if cache_path.is_file() and cache_path.stat().st_size > 0
            else None
        ),
        baseline_workspace_hash=hash_tree(baseline) if baseline.is_dir() else None,
    )


def _validate_fixture_manifest(
    workspace: Path,
    inputs: _FrozenInputs,
) -> None:
    payload = _load_json_object(
        workspace / "fixture-manifest.json",
        label="Power BI fixture manifest",
    )
    expected_inventory = {
        key: {
            "path": str(_source_path(inputs, key)),
            "sha256": str(inputs.files[key]["sha256"]),
        }
        for key in _HYDRATED_SOURCE_RECORDS
    }
    if payload.get("hydrated_sources") != expected_inventory:
        raise PreparationSafetyError(
            "prepared hydrated source inventory has drifted from the frozen inputs"
        )
    if payload.get("status") != "phase0-attestation-required":
        raise PreparationSafetyError("Power BI fixture attestation status has drifted")
    if payload.get("desktop_render_verified") is not False:
        raise PreparationSafetyError("Power BI fixture Desktop verification state has drifted")
    if payload.get("semantic_model_state") != ("hydrated-from-frozen-absolute-parquet-sources"):
        raise PreparationSafetyError("Power BI fixture semantic-model state has drifted")
    expected_identity = {
        "catalog_version": str(inputs.manifest["version"]),
        "catalog_hash": str(inputs.files["catalog"]["sha256"]),
        "data_hash": str(inputs.manifest["data_hash"]),
        "data_through": str(inputs.manifest["data_through"]),
    }
    for field, expected in expected_identity.items():
        if payload.get(field) != expected:
            raise PreparationSafetyError(f"Power BI fixture {field.replace('_', ' ')} has drifted")


def _load_prepared_bundle(
    inputs: _FrozenInputs,
    root: Path,
    *,
    verify_workspace_hashes: bool,
    require_attested: bool,
    transaction_state_path: Path | None = None,
    recovery_trust_only: bool = False,
) -> tuple[ProjectBundle, PreparedWindowsDemo]:
    if not root.is_dir():
        raise PreparationSafetyError(f"prepared demo root is missing: {root}")
    _reject_symlink_tree(root)
    workspace, baseline, bundle_path = _prepared_paths(root)
    if not workspace.is_dir() or not baseline.is_dir() or not bundle_path.is_file():
        raise PreparationSafetyError(
            "prepared demo requires workspace, baseline, and project-bundle.json"
        )

    payload = _load_json_object(bundle_path, label="project bundle")
    try:
        bundle = ProjectBundle.model_validate(payload)
    except ValidationError as error:
        detail = error.errors(include_url=False)[0]
        location = ".".join(str(part) for part in detail["loc"])
        raise PreparationSafetyError(
            f"project bundle is invalid at {location}: {detail['msg']}"
        ) from error

    expected_paths = {
        "pbip_path": workspace / "ClientWow.pbip",
        "report_path": workspace / "ClientWow.Report",
        "semantic_model_path": workspace / "ClientWow.SemanticModel",
        "duckdb_path": _source_path(inputs, "duckdb"),
        "catalog_path": _source_path(inputs, "catalog"),
    }
    for field, expected_path in expected_paths.items():
        actual = _absolute_without_resolving(getattr(bundle, field))
        if actual != expected_path.absolute():
            raise PreparationSafetyError(
                f"project bundle path does not match the prepared layout: {field}"
            )
        _reject_symlink_components(actual)

    if not bundle.pbip_path.is_file():
        raise PreparationSafetyError("prepared PBIP entry point is missing")
    if not bundle.semantic_model_path.is_dir():
        raise PreparationSafetyError("prepared semantic model is missing")
    if recovery_trust_only:
        if bundle.report_path.exists() and not bundle.report_path.is_dir():
            raise PreparationSafetyError("prepared report path is not a directory")
    elif not bundle.report_path.is_dir():
        raise PreparationSafetyError("prepared report is missing")
    try:
        if not recovery_trust_only:
            validate_pbip_entrypoint(bundle.pbip_path, bundle.report_path)
            validate_report_semantic_reference(
                bundle.report_path,
                bundle.semantic_model_path,
            )
        validate_pbip_entrypoint(
            baseline / "ClientWow.pbip",
            baseline / "ClientWow.Report",
        )
        validate_report_semantic_reference(
            baseline / "ClientWow.Report",
            baseline / "ClientWow.SemanticModel",
        )
    except PBIPEntryPointError as error:
        raise PreparationSafetyError(str(error)) from error

    frozen_hashes = {
        "data_hash": str(inputs.manifest["data_hash"]),
        "source_data_hash": str(inputs.files["sales_parquet"]["sha256"]),
        "duckdb_hash": str(inputs.files["duckdb"]["sha256"]),
        "catalog_hash": str(inputs.files["catalog"]["sha256"]),
    }
    for field, expected_hash in frozen_hashes.items():
        if getattr(bundle, field) != expected_hash:
            raise PreparationSafetyError(f"project bundle {field} has drifted")
    expected_source_hashes = {key: str(inputs.files[key]["sha256"]) for key in _SOURCE_RECORDS}
    if bundle.source_hashes != expected_source_hashes:
        raise PreparationSafetyError("project bundle source hashes have drifted")
    if bundle.catalog_version != str(inputs.manifest.get("version", "1.0.0")):
        raise PreparationSafetyError("project bundle catalog version has drifted")
    data_through = inputs.manifest.get("data_through")
    if not isinstance(data_through, str) or bundle.data_through != date.fromisoformat(data_through):
        raise PreparationSafetyError("project bundle data cutoff has drifted")
    if bundle.forecast_version != str(inputs.manifest.get("forecast_version", "1.0.0")):
        raise PreparationSafetyError("project bundle forecast version has drifted")
    if bundle.scenario_version != str(inputs.manifest.get("scenario_version", "1.0.0")):
        raise PreparationSafetyError("project bundle scenario version has drifted")

    attested = bundle.baseline_validation_status == "passed"
    write_capabilities = (
        bundle.capabilities.can_build,
        bundle.capabilities.can_modify,
        bundle.capabilities.can_open_desktop,
        bundle.capabilities.offline_schema_verified,
        bundle.capabilities.desktop_render_verified,
        bundle.capabilities.phase0_kill_test_passed,
    )
    if attested:
        if not all(write_capabilities) or bundle.capabilities.blockers:
            raise PreparationSafetyError(
                "attested project bundle does not have a complete capability grant"
            )
        try:
            exact_pins = Phase0Pins.model_validate(bundle.compatibility.model_dump())
        except ValidationError as error:
            raise PreparationSafetyError(
                "attested project bundle has incomplete version pins"
            ) from error
        pin_blockers = _pin_blockers(exact_pins, inputs.manifest)
        if pin_blockers:
            raise PreparationSafetyError("; ".join(pin_blockers))
        accepted_attestation = root / "phase0-attestation.json"
        if (
            bundle.phase0_attestation_hash is None
            or not accepted_attestation.is_file()
            or sha256_file(accepted_attestation) != bundle.phase0_attestation_hash
        ):
            raise PreparationSafetyError(
                "attested project bundle has no matching content-addressed Phase-0 attestation"
            )
        baseline_hashes = _current_artifact_hashes(inputs, baseline)
        corpus_hash, corpus_case_ids = _model_corpus_identity(inputs.repository)
        attestation = _validated_attestation(
            accepted_attestation,
            demo_root=root,
            expected_hashes=baseline_hashes,
            manifest=inputs.manifest,
            model_corpus_hash=corpus_hash,
            model_case_ids=corpus_case_ids,
        )
        if bundle.phase0_receipt_hashes != _attestation_receipt_hashes(attestation):
            raise PreparationSafetyError(
                "project bundle Phase-0 receipt hashes do not match the accepted attestation"
            )
    elif any(write_capabilities):
        raise PreparationSafetyError(
            "unattested project bundle cannot grant Power BI write capabilities"
        )
    if require_attested and not attested:
        raise PreparationSafetyError(
            "prepared demo is not Phase-0 attested; real startup remains disabled"
        )

    _validate_fixture_manifest(workspace, inputs)
    _validate_fixture_manifest(baseline, inputs)

    if verify_workspace_hashes:
        if bundle.pbip_hash is None:
            raise PreparationSafetyError("project bundle has no PBIP entry-point hash")
        if sha256_file(bundle.pbip_path) != bundle.pbip_hash:
            raise PreparationSafetyError("prepared PBIP hash does not match the project bundle")
        if sha256_file(baseline / "ClientWow.pbip") != bundle.pbip_hash:
            raise PreparationSafetyError(
                "reset baseline PBIP hash does not match the project bundle"
            )
        baseline_report_hash = bundle.baseline_report_hash or bundle.report_hash
        if baseline_report_hash is None or bundle.report_hash != baseline_report_hash:
            raise PreparationSafetyError("project bundle baseline report hash is invalid")
        if (
            bundle.semantic_model_hash is None
            or hash_tree(bundle.semantic_model_path) != bundle.semantic_model_hash
        ):
            raise PreparationSafetyError(
                "prepared semantic model hash does not match the project bundle"
            )
        baseline_report = baseline / "ClientWow.Report"
        baseline_model = baseline / "ClientWow.SemanticModel"
        if hash_tree(baseline_report) != baseline_report_hash:
            raise PreparationSafetyError(
                "reset baseline report hash does not match the project bundle"
            )
        if (
            bundle.baseline_workspace_hash is None
            or hash_tree(baseline) != bundle.baseline_workspace_hash
        ):
            raise PreparationSafetyError(
                "reset baseline workspace hash does not match the project bundle"
            )
        if hash_tree(baseline_model) != bundle.semantic_model_hash:
            raise PreparationSafetyError(
                "reset baseline semantic model hash does not match the project bundle"
            )
        if not recovery_trust_only:
            live_report_hash = hash_tree(bundle.report_path)
            if live_report_hash != baseline_report_hash:
                if not attested:
                    raise PreparationSafetyError(
                        "prepared report hash differs from the unattested reset baseline"
                    )
                if transaction_state_path is None:
                    raise PreparationSafetyError(
                        "prepared report differs from baseline without a validated transaction head"
                    )
                try:
                    verify_validated_report_head(
                        bundle.report_path,
                        transaction_state_path,
                        baseline_report_hash,
                    )
                except TransactionError as error:
                    raise PreparationSafetyError(str(error)) from error

    return bundle, PreparedWindowsDemo(
        root=root,
        workspace_path=workspace,
        baseline_path=baseline,
        bundle_path=bundle_path,
        attested=attested,
    )


def _load_single_pending_report_swap(
    state: Path,
    report_path: Path,
    baseline_hash: str,
) -> _PendingReportSwap | None:
    recovery_root = state / "recovery"
    if not recovery_root.exists():
        return None
    if not recovery_root.is_dir():
        raise PreparationSafetyError("report swap recovery path is not a directory")

    pending: list[_PendingReportSwap] = []
    for intent_path in sorted(recovery_root.glob("*.json")):
        payload = _load_json_object(intent_path, label="report swap recovery intent")
        if payload.get("status") != "pending":
            continue
        transaction_id = payload.get("transaction_id")
        before_hash = payload.get("before_hash")
        if (
            set(payload) != {"transaction_id", "status", "before_hash"}
            or not isinstance(transaction_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", transaction_id)
            or intent_path.name != f"{transaction_id}.json"
            or not isinstance(before_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", before_hash)
        ):
            raise PreparationSafetyError("report swap pending intent is invalid")
        pending.append(
            _PendingReportSwap(
                transaction_id=transaction_id,
                before_hash=before_hash,
                intent_path=intent_path,
                displaced_path=state / "swaps" / f"{transaction_id}.old",
            )
        )
    if len(pending) > 1:
        raise PreparationSafetyError("multiple report swap pending intents are forbidden")
    if not pending:
        return None

    intent = pending[0]
    trusted_report: Path | None = None
    if intent.displaced_path.exists():
        if not intent.displaced_path.is_dir():
            raise PreparationSafetyError("report swap pending intent has no valid displaced report")
        trusted_report = intent.displaced_path
    elif report_path.is_dir():
        # A crash can occur after the intent is durable but before the live
        # directory is displaced. In that state the live report is the exact
        # pre-transaction object and no directory swap is needed.
        trusted_report = report_path
    if trusted_report is None:
        raise PreparationSafetyError("report swap pending intent has no recoverable report")
    try:
        verify_validated_report_head(
            trusted_report,
            state,
            baseline_hash,
            expected_live_hash=intent.before_hash,
        )
    except TransactionError as error:
        raise PreparationSafetyError(
            f"report swap pending intent is not baseline-authorized: {error}"
        ) from error
    return intent


def _assert_report_recovery_exclusion(root: Path) -> None:
    reveal_marker = root / ".reveal-in-progress.json"
    if reveal_marker.exists() or _is_link_like(reveal_marker):
        raise PreparationSafetyError(
            "report swap recovery is blocked while Desktop reveal is in progress"
        )
    assert_powerbi_desktop_closed()


def recover_interrupted_report_swap(
    repository_root: str | Path,
    demo_root: str | Path,
    *,
    transaction_state_path: str | Path,
    expected_bundle: ProjectBundle,
) -> bool:
    """Restore one trusted interrupted report swap before normal verification.

    This is intentionally narrower than :func:`verify_windows_demo`: it may
    tolerate an absent or uncommitted live report only long enough to prove a
    single durable swap intent and its baseline-anchored displaced directory.
    The transaction manager performs the actual recovery under the same
    operation lock used by promotion, Undo, reset, and Desktop reveal.
    """

    inputs = _load_frozen_inputs(repository_root)
    root = _safe_destination(demo_root)
    state = _safe_destination(transaction_state_path)
    expected_state = (inputs.repository / ".runtime" / "powerbi").resolve(strict=False)
    if state != expected_state:
        raise PreparationSafetyError(
            "report transaction state must be the repository-owned .runtime/powerbi directory"
        )
    if state == root or state.is_relative_to(root) or root.is_relative_to(state):
        raise PreparationSafetyError("report transaction state overlaps the prepared demo")
    if state.exists():
        if not state.is_dir():
            raise PreparationSafetyError("report transaction state is not a directory")
        _reject_symlink_tree(state)

    bundle, prepared = _load_prepared_bundle(
        inputs,
        root,
        verify_workspace_hashes=True,
        require_attested=True,
        transaction_state_path=state,
        recovery_trust_only=True,
    )
    if bundle != expected_bundle:
        raise PreparationSafetyError(
            "requested ProjectBundle differs from the accepted release descriptor"
        )
    baseline_hash = bundle.baseline_report_hash or bundle.report_hash
    if baseline_hash is None:
        raise PreparationSafetyError("project bundle baseline report hash is missing")
    pending = _load_single_pending_report_swap(
        state,
        bundle.report_path,
        baseline_hash,
    )
    if pending is None:
        if bundle.report_path.is_dir():
            return False
        raise PreparationSafetyError(
            "prepared report is missing without a valid pending swap intent"
        )

    _assert_report_recovery_exclusion(root)

    def guarded_recovery_authority() -> None:
        # ProjectTransactionManager invokes this under the shared operation
        # lock immediately before its first live-path mutation.
        _assert_report_recovery_exclusion(root)
        _reject_symlink_tree(state)
        current_bundle, _ = _load_prepared_bundle(
            inputs,
            root,
            verify_workspace_hashes=True,
            require_attested=True,
            transaction_state_path=state,
            recovery_trust_only=True,
        )
        if current_bundle != bundle or current_bundle != expected_bundle:
            raise PreparationSafetyError(
                "accepted release descriptor changed before report recovery"
            )
        current_pending = _load_single_pending_report_swap(
            state,
            current_bundle.report_path,
            baseline_hash,
        )
        if current_pending != pending:
            raise PreparationSafetyError("report swap pending intent changed before recovery")

    try:
        # Hold the app-wide lock around the complete trust recheck and recovery.
        # The manager uses a private nested lock because distinct FileLock
        # instances are not recursively re-entrant; every normal writer and
        # Desktop reveal remains excluded by the outer shared lock.
        with FileLock(str(root / ".operation.lock"), timeout=5):
            guarded_recovery_authority()
            ProjectTransactionManager(
                prepared.workspace_path,
                Path("ClientWow.Report"),
                state,
                attested_baseline_hash=baseline_hash,
                operation_lock_path=state / ".startup-recovery.lock",
                write_guard=guarded_recovery_authority,
            )
    except TransactionError as error:
        raise PreparationSafetyError(f"report swap recovery failed: {error}") from error
    return True


def attest_windows_demo(
    repository_root: str | Path,
    demo_root: str | Path,
    *,
    attestation_path: str | Path,
) -> PreparedWindowsDemo:
    """Bind passed Windows gates to the existing hydrated workspace.

    Preparation never enables real writes.  This separate operation validates
    the current report and semantic-model hashes, every frozen input, all
    compatibility pins, and all Phase-0 gates before replacing the bundle.
    """

    if platform.system().casefold() != "windows":
        raise PreparationSafetyError("Phase-0 attestation can only be accepted on Windows")
    inputs = _load_frozen_inputs(repository_root)
    root = _safe_destination(demo_root)
    with FileLock(str(root / ".operation.lock"), timeout=5):
        assert_powerbi_desktop_closed()
        bundle, prepared = _load_prepared_bundle(
            inputs,
            root,
            verify_workspace_hashes=True,
            require_attested=False,
        )
        if prepared.attested:
            raise PreparationSafetyError(
                "prepared demo is already attested; use verify instead of re-attesting"
            )
        baseline_hash = bundle.baseline_report_hash or bundle.report_hash
        if baseline_hash is None or hash_tree(prepared.workspace_path / "ClientWow.Report") != (
            baseline_hash
        ):
            raise PreparationSafetyError(
                "Phase-0 attestation requires the exact reset-baseline report"
            )
        expected_hashes = _current_artifact_hashes(inputs, prepared.workspace_path)
        corpus_hash, corpus_case_ids = _model_corpus_identity(inputs.repository)
        attestation = _validated_attestation(
            attestation_path,
            demo_root=root,
            expected_hashes=expected_hashes,
            manifest=inputs.manifest,
            model_corpus_hash=corpus_hash,
            model_case_ids=corpus_case_ids,
        )
        source_attestation = _absolute_without_resolving(attestation_path)
        accepted_attestation = root / "phase0-attestation.json"
        updated = bundle.model_copy(
            update={
                "pbip_hash": expected_hashes.pbip_hash,
                "report_hash": expected_hashes.report_hash,
                "baseline_report_hash": expected_hashes.report_hash,
                "semantic_model_hash": expected_hashes.semantic_model_hash,
                "phase0_attestation_hash": sha256_file(source_attestation),
                "phase0_receipt_hashes": _attestation_receipt_hashes(attestation),
                "baseline_validation_status": "passed",
                "compatibility": CompatibilityPins(**attestation.pins.model_dump()),
                "capabilities": _attested_capabilities(),
            }
        )
        original_bundle = prepared.bundle_path.read_bytes()
        original_attestation = (
            accepted_attestation.read_bytes() if accepted_attestation.is_file() else None
        )
        _write_bytes_atomic(accepted_attestation, source_attestation.read_bytes())
        _write_json_atomic(
            prepared.bundle_path,
            json.loads(updated.model_dump_json()),
        )
        try:
            return verify_windows_demo(repository_root, root, require_attested=True)
        except BaseException:
            _write_bytes_atomic(prepared.bundle_path, original_bundle)
            if original_attestation is None:
                accepted_attestation.unlink(missing_ok=True)
            else:
                _write_bytes_atomic(accepted_attestation, original_attestation)
            raise


def verify_windows_demo(
    repository_root: str | Path,
    demo_root: str | Path,
    *,
    require_attested: bool = False,
    transaction_state_path: str | Path | None = None,
) -> PreparedWindowsDemo:
    """Verify paths, frozen inputs, report/model hashes, baseline, and capabilities."""

    inputs = _load_frozen_inputs(repository_root)
    root = _safe_destination(demo_root)
    state = (
        _safe_destination(transaction_state_path) if transaction_state_path is not None else None
    )
    _, prepared = _load_prepared_bundle(
        inputs,
        root,
        verify_workspace_hashes=True,
        require_attested=require_attested,
        transaction_state_path=state,
    )
    return prepared


def reset_windows_demo(
    demo_root: str | Path,
    *,
    state_path: str | Path | None = None,
    now: datetime | None = None,
) -> ResetReceipt:
    """Restore the hydrated baseline while retaining every replaced file in a backup."""

    root = _safe_destination(demo_root)
    if not root.is_dir():
        raise PreparationSafetyError(f"prepared demo root is missing: {root}")
    state: Path | None = None
    if state_path is not None:
        state = _safe_destination(state_path)
        repository_root = root.parent.parent if root.parent.name == "fixtures" else root.parent
        expected_state = (repository_root / ".runtime").resolve(strict=False)
        if state != expected_state:
            raise PreparationSafetyError(
                "runtime state path must be the app-owned .runtime directory next to "
                "the prepared project"
            )
        protected = {root, root / "baseline", root / "backups"}
        if state in protected or state.is_relative_to(root):
            raise PreparationSafetyError("runtime state path overlaps the prepared demo root")
        if state.exists():
            if not state.is_dir():
                raise PreparationSafetyError("runtime state path must be a directory")
            _reject_symlink_tree(state)

    with FileLock(str(root / ".operation.lock"), timeout=5):
        assert_powerbi_desktop_closed()
        recovered = _recover_pending_reset(root, state)
        if recovered is not None:
            return recovered

        workspace = root / "workspace"
        baseline = root / "baseline"
        bundle_path = root / "project-bundle.json"
        if not workspace.is_dir() or not baseline.is_dir() or not bundle_path.is_file():
            raise PreparationSafetyError(
                "prepared demo requires workspace, baseline, and project-bundle.json"
            )
        _reject_symlink_tree(workspace)
        _reject_symlink_tree(baseline)
        bundle = _load_reset_bundle(bundle_path, workspace)
        baseline_hash = _validate_reset_baseline(bundle, baseline)
        before_hash = hash_tree(workspace / "ClientWow.Report")

        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        timestamp = timestamp.astimezone(UTC)
        backup = root / "backups" / timestamp.strftime("%Y%m%dT%H%M%SZ")
        if backup.exists():
            raise PreparationSafetyError(f"reset backup already exists: {backup}")
        stage = Path(tempfile.mkdtemp(prefix=".resetting-", dir=root))
        candidate_workspace = stage / "workspace"
        shutil.copytree(baseline, candidate_workspace)
        if hash_tree(candidate_workspace / "ClientWow.Report") != baseline_hash:
            raise PreparationSafetyError("reset candidate does not match the validated baseline")
        _validate_reset_baseline(bundle, candidate_workspace)

        backup.mkdir(parents=True)
        intent_path = root / ".reset-recovery.json"
        intent: dict[str, object] = {
            "schema_version": "1.0.0",
            "status": "pending",
            "backup": backup.name,
            "before_report_hash": before_hash,
            "baseline_report_hash": baseline_hash,
            "state_path": str(state) if state is not None else None,
        }
        _write_json_atomic(intent_path, intent)
        workspace_moved = False
        state_moved = False
        try:
            assert_powerbi_desktop_closed()
            workspace.replace(backup / "workspace")
            workspace_moved = True
            candidate_workspace.replace(workspace)
            if hash_tree(workspace / "ClientWow.Report") != baseline_hash:
                raise PreparationSafetyError("post-reset report hash does not match baseline")
            _validate_reset_baseline(bundle, workspace)
            if state is not None and state.exists():
                shutil.move(str(state), str(backup / "runtime-state"))
                state_moved = True
            intent["status"] = "committed"
            _write_json_atomic(intent_path, intent)
            (root / ".reveal-in-progress.json").unlink(missing_ok=True)
        except BaseException:
            if workspace_moved and (backup / "workspace").is_dir():
                failed = stage / "failed-workspace"
                if workspace.exists():
                    workspace.replace(failed)
                (backup / "workspace").replace(workspace)
            if state_moved and state is not None and (backup / "runtime-state").is_dir():
                shutil.move(str(backup / "runtime-state"), str(state))
            intent["status"] = "rolled_back"
            _write_json_atomic(intent_path, intent)
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return ResetReceipt(root=root, workspace_path=workspace, backup_path=backup)


def _load_reset_bundle(bundle_path: Path, workspace: Path) -> ProjectBundle:
    payload = _load_json_object(bundle_path, label="project bundle")
    try:
        bundle = ProjectBundle.model_validate(payload)
    except ValidationError as error:
        raise PreparationSafetyError("project bundle is invalid; reset is blocked") from error
    expected_paths = {
        "pbip_path": workspace / "ClientWow.pbip",
        "report_path": workspace / "ClientWow.Report",
        "semantic_model_path": workspace / "ClientWow.SemanticModel",
    }
    for field, expected in expected_paths.items():
        if _absolute_without_resolving(getattr(bundle, field)) != expected.absolute():
            raise PreparationSafetyError(f"project bundle path is invalid for reset: {field}")
    return bundle


def _validate_reset_baseline(bundle: ProjectBundle, baseline: Path) -> str:
    pbip = baseline / "ClientWow.pbip"
    report = baseline / "ClientWow.Report"
    semantic_model = baseline / "ClientWow.SemanticModel"
    _reject_symlink_tree(baseline)
    try:
        validate_pbip_entrypoint(pbip, report)
        validate_report_semantic_reference(report, semantic_model)
    except PBIPEntryPointError as error:
        raise PreparationSafetyError(str(error)) from error
    baseline_hash = bundle.baseline_report_hash or bundle.report_hash
    if baseline_hash is None or hash_tree(report) != baseline_hash:
        raise PreparationSafetyError("reset baseline report hash is invalid")
    if bundle.pbip_hash is None or sha256_file(pbip) != bundle.pbip_hash:
        raise PreparationSafetyError("reset baseline PBIP hash is invalid")
    if (
        bundle.semantic_model_hash is None
        or hash_tree(semantic_model) != bundle.semantic_model_hash
    ):
        raise PreparationSafetyError("reset baseline semantic model hash is invalid")
    if (
        bundle.baseline_workspace_hash is None
        or hash_tree(baseline) != bundle.baseline_workspace_hash
    ):
        raise PreparationSafetyError("reset baseline workspace hash is invalid")
    return baseline_hash


def _recover_pending_reset(root: Path, state: Path | None) -> ResetReceipt | None:
    intent_path = root / ".reset-recovery.json"
    if not intent_path.is_file():
        return None
    intent = _load_json_object(intent_path, label="reset recovery intent")
    if intent.get("status") != "pending":
        return None
    backup_name = intent.get("backup")
    before_hash = intent.get("before_report_hash")
    baseline_hash = intent.get("baseline_report_hash")
    if (
        not isinstance(backup_name, str)
        or Path(backup_name).name != backup_name
        or not isinstance(before_hash, str)
        or not isinstance(baseline_hash, str)
    ):
        raise PreparationSafetyError("reset recovery intent is invalid")
    workspace = root / "workspace"
    backup = root / "backups" / backup_name
    old_workspace = backup / "workspace"
    recorded_state = intent.get("state_path")
    if recorded_state is not None and not isinstance(recorded_state, str):
        raise PreparationSafetyError("reset recovery state path is invalid")
    caller_state = str(state) if state is not None else None
    if recorded_state != caller_state:
        raise PreparationSafetyError(
            "interrupted reset state path does not match the original reset request"
        )
    if workspace.is_dir() and hash_tree(workspace / "ClientWow.Report") == baseline_hash:
        if not old_workspace.is_dir() or hash_tree(old_workspace / "ClientWow.Report") != (
            before_hash
        ):
            raise PreparationSafetyError("interrupted reset backup failed hash verification")
        if state is not None and state.exists():
            if (backup / "runtime-state").exists():
                raise PreparationSafetyError("interrupted reset has duplicate runtime state")
            shutil.move(str(state), str(backup / "runtime-state"))
        intent["status"] = "committed"
        _write_json_atomic(intent_path, intent)
        (root / ".reveal-in-progress.json").unlink(missing_ok=True)
        return ResetReceipt(root=root, workspace_path=workspace, backup_path=backup)

    if old_workspace.is_dir() and hash_tree(old_workspace / "ClientWow.Report") == before_hash:
        if workspace.exists():
            failed = backup / "failed-reset-workspace"
            if failed.exists():
                raise PreparationSafetyError("reset recovery destination already exists")
            workspace.replace(failed)
        old_workspace.replace(workspace)
    elif not workspace.is_dir() or hash_tree(workspace / "ClientWow.Report") != before_hash:
        raise PreparationSafetyError("interrupted reset cannot recover the original workspace")
    intent["status"] = "rolled_back"
    _write_json_atomic(intent_path, intent)
    raise PreparationSafetyError("an interrupted reset was rolled back safely; run reset again")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="create the hydrated Windows demo workspace")
    prepare.add_argument("--repository-root", type=Path, required=True)
    prepare.add_argument("--demo-root", type=Path, required=True)
    seal_cache = commands.add_parser(
        "seal-cache",
        help="freeze an offline-refreshed Desktop cache into the reset baseline",
    )
    seal_cache.add_argument("--repository-root", type=Path, required=True)
    seal_cache.add_argument("--demo-root", type=Path, required=True)
    attest = commands.add_parser(
        "attest", help="bind passed Phase-0 gates to an existing prepared workspace"
    )
    attest.add_argument("--repository-root", type=Path, required=True)
    attest.add_argument("--demo-root", type=Path, required=True)
    attest.add_argument("--attestation", type=Path, required=True)
    verify = commands.add_parser(
        "verify", help="verify frozen inputs, paths, hashes, baseline, and capabilities"
    )
    verify.add_argument("--repository-root", type=Path, required=True)
    verify.add_argument("--demo-root", type=Path, required=True)
    verify.add_argument("--require-attested", action="store_true")
    verify.add_argument("--transaction-state", type=Path)
    reset = commands.add_parser("reset", help="restore the baseline and retain a backup")
    reset.add_argument("--demo-root", type=Path, required=True)
    reset.add_argument("--state-path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            receipt = prepare_windows_demo(
                arguments.repository_root,
                arguments.demo_root,
            )
            payload = {
                "status": "prepared",
                "workspace": str(receipt.workspace_path),
                "baseline": str(receipt.baseline_path),
                "bundle": str(receipt.bundle_path),
                "phase0_attested": receipt.attested,
            }
        elif arguments.command == "seal-cache":
            cache = seal_populated_semantic_cache(
                arguments.repository_root,
                arguments.demo_root,
            )
            payload = {
                "status": "cache-sealed",
                "cache": str(cache.cache_path),
                "cache_sha256": cache.cache_sha256,
                "cache_size_bytes": cache.cache_size_bytes,
                "semantic_model_hash": cache.semantic_model_hash,
                "baseline_workspace_hash": cache.baseline_workspace_hash,
                "phase0_attested": False,
            }
        elif arguments.command == "attest":
            receipt = attest_windows_demo(
                arguments.repository_root,
                arguments.demo_root,
                attestation_path=arguments.attestation,
            )
            payload = {
                "status": "attested",
                "workspace": str(receipt.workspace_path),
                "baseline": str(receipt.baseline_path),
                "bundle": str(receipt.bundle_path),
                "phase0_attested": receipt.attested,
            }
        elif arguments.command == "verify":
            receipt = verify_windows_demo(
                arguments.repository_root,
                arguments.demo_root,
                require_attested=arguments.require_attested,
                transaction_state_path=arguments.transaction_state,
            )
            payload = {
                "status": "verified",
                "workspace": str(receipt.workspace_path),
                "baseline": str(receipt.baseline_path),
                "bundle": str(receipt.bundle_path),
                "phase0_attested": receipt.attested,
            }
        else:
            reset_receipt = reset_windows_demo(
                arguments.demo_root,
                state_path=arguments.state_path,
            )
            payload = {
                "status": "reset",
                "workspace": str(reset_receipt.workspace_path),
                "backup": str(reset_receipt.backup_path),
            }
    except (PreparationSafetyError, ValueError) as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
