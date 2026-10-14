"""Validation for the small PBIP entry-point contract used by the demo."""

from __future__ import annotations

import json
from pathlib import Path


class PBIPEntryPointError(ValueError):
    """The PBIP file does not resolve to the one prepared report."""


def validate_pbip_entrypoint(pbip_path: Path, report_path: Path) -> None:
    """Require one relative report artifact that resolves to ``report_path``."""

    if pbip_path.suffix.casefold() != ".pbip" or not pbip_path.is_file():
        raise PBIPEntryPointError("the prepared PBIP entry point is missing")
    try:
        value = json.loads(pbip_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PBIPEntryPointError("the PBIP entry point is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise PBIPEntryPointError("the PBIP entry point must contain a JSON object")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise PBIPEntryPointError("the PBIP entry point must contain one report artifact")
    artifact = artifacts[0]
    report = artifact.get("report") if isinstance(artifact, dict) else None
    relative = report.get("path") if isinstance(report, dict) else None
    if not isinstance(relative, str) or not relative.strip():
        raise PBIPEntryPointError("the PBIP report artifact path is missing")
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.name != relative
        or relative_path.suffix.casefold() != ".report"
    ):
        raise PBIPEntryPointError("the PBIP report artifact path is unsafe")
    resolved_report = report_path.resolve(strict=True)
    resolved_artifact = (pbip_path.parent / relative_path).resolve(strict=True)
    if resolved_artifact != resolved_report or not resolved_artifact.is_dir():
        raise PBIPEntryPointError("the PBIP report artifact does not target the prepared report")


def validate_report_semantic_reference(
    report_path: Path,
    semantic_model_path: Path,
) -> None:
    """Require the report to bind by path to the one governed semantic model."""

    definition_path = report_path / "definition.pbir"
    try:
        value = json.loads(definition_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PBIPEntryPointError("the report definition.pbir is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise PBIPEntryPointError("the report definition.pbir must contain a JSON object")
    dataset_reference = value.get("datasetReference")
    by_path = dataset_reference.get("byPath") if isinstance(dataset_reference, dict) else None
    relative = by_path.get("path") if isinstance(by_path, dict) else None
    expected_relative = f"../{semantic_model_path.name}"
    if relative != expected_relative:
        raise PBIPEntryPointError(
            "the report datasetReference must target the governed semantic model by path"
        )
    resolved_model = semantic_model_path.resolve(strict=True)
    resolved_reference = (report_path / str(relative)).resolve(strict=True)
    if resolved_reference != resolved_model or not resolved_reference.is_dir():
        raise PBIPEntryPointError(
            "the report datasetReference does not resolve to the governed semantic model"
        )
