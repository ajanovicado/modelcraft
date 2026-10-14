"""Hermetic external-validator failure gates; no installed tools or network calls."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_powerbi_analyst.data.onboarding import DataOnboardingService, UploadedSource
from ai_powerbi_analyst.powerbi import semantic_model as semantic_model_module
from ai_powerbi_analyst.powerbi.semantic_model import SemanticProjectBuilder, SemanticProjectError
from ai_powerbi_analyst.preflight import ToolProbe
from ai_powerbi_analyst.workspace.planner import DashboardPlanningService

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("required", [False, True])
def test_missing_external_validators_only_allowed_when_explicitly_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required: bool
) -> None:
    monkeypatch.setattr(
        semantic_model_module, "probe_pbi_cli", lambda: ToolProbe(name="pbi", available=False)
    )
    monkeypatch.setattr(
        semantic_model_module, "probe_tool", lambda name: ToolProbe(name=name, available=False)
    )
    monkeypatch.setattr(
        semantic_model_module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Unavailable validators launched a subprocess"),
    )
    arguments = {} if required else {"require_external_validators": False}
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures/powerbi/client-wow-template",
        desktop_closed_check=lambda: None,
        **arguments,
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "Demo.pbip").write_text("{}", encoding="utf-8")
    (project / "Demo.Report").mkdir()

    issues, validators = builder._external_validation(project)

    assert builder.require_external_validators is required
    assert validators == []
    assert issues == (
        ["Pinned pbi-cli validator is unavailable", "Microsoft PBIR validator is unavailable"]
        if required
        else []
    )


@pytest.mark.parametrize(
    ("tool", "payload", "returncode", "expected", "attempts"),
    [
        (
            "author",
            {
                "data": {
                    "result": "succeededWithWarnings",
                    "errorCount": 0,
                    "warningCount": 1,
                    "diagnostics": {
                        "PBIR_SCHEMA_UNREACHABLE": {
                            "severity": "warning",
                            "items": [{"message": "unavailable"}],
                        }
                    },
                }
            },
            0,
            "could not download Microsoft's report schemas",
            2,
        ),
        (
            "author",
            {"data": {"result": "succeeded", "errorCount": 0, "warningCount": 1}},
            0,
            "reported errors or warnings",
            1,
        ),
        ("author", "invalid JSON", 0, "returned invalid JSON", 1),
        ("author", {}, 1, "failed: injected external-validator failure", 1),
        ("pbi", {"valid": False}, 0, "rejected the report", 1),
        ("pbi", {"valid": True, "summary": {"warnings": 1}}, 0, "reported warnings", 1),
        ("author", None, 0, "could not run", 1),
    ],
)
def test_discovered_optional_validator_failure_still_blocks_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    payload: object,
    returncode: int,
    expected: str,
    attempts: int,
) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Sales\n100\n150\n")]
    )
    plan = DashboardPlanningService(None).plan("Build a sales dashboard", manifest, [])
    plan = plan.model_copy(
        update={
            "approved": True,
            "semantic_model": plan.semantic_model.model_copy(update={"approved": True}),
        }
    )
    monkeypatch.setattr(
        semantic_model_module,
        "probe_pbi_cli",
        lambda: ToolProbe(
            name="pbi", available=tool == "pbi", path="scripted-pbi.exe" if tool == "pbi" else None
        ),
    )
    monkeypatch.setattr(
        semantic_model_module,
        "probe_tool",
        lambda name: ToolProbe(
            name=name,
            available=tool == "author",
            path="scripted-author.exe" if tool == "author" else None,
        ),
    )
    calls = []

    def scripted_run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs == {
            "check": False,
            "capture_output": True,
            "text": True,
            "timeout": 90,
            "shell": False,
        }
        assert argv[0] == f"scripted-{tool}.exe"
        if payload is None:
            raise subprocess.TimeoutExpired(argv, 90)
        return subprocess.CompletedProcess(
            argv,
            returncode,
            stdout=payload if isinstance(payload, str) else json.dumps(payload),
            stderr="injected external-validator failure" if returncode else "",
        )

    monkeypatch.setattr(semantic_model_module.subprocess, "run", scripted_run)
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures/powerbi/client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    with pytest.raises(SemanticProjectError, match=expected):
        builder.build("External failure", manifest, plan)

    assert len(calls) == attempts
    assert not (builder.projects_root / "external_failure").exists()
    assert not list(builder.projects_root.glob(".*.stage-*"))
    assert not (builder.evidence_root / "external_failure-validation-receipt.json").exists()
    assert not (builder.undo_root / "external_failure.json").exists()


def _schema_unreachable_payload() -> dict[str, object]:
    return {
        "data": {
            "result": "succeededWithWarnings",
            "errorCount": 0,
            "warningCount": 1,
            "diagnostics": {
                "PBIR_SCHEMA_UNREACHABLE": {
                    "severity": "warning",
                    "items": [{"message": "Microsoft schema fetch timed out"}],
                }
            },
        }
    }


@pytest.mark.parametrize(
    ("second_payload", "expected"),
    [
        ({"data": {"result": "succeeded", "errorCount": 0, "warningCount": 0}}, []),
        (_schema_unreachable_payload(), ["could not download Microsoft's report schemas"]),
        (
            {"data": {"result": "failed", "errorCount": 1, "warningCount": 0}},
            ["rejected the report"],
        ),
        (
            {"data": {"result": "succeeded", "errorCount": 0, "warningCount": 1}},
            ["reported errors or warnings"],
        ),
    ],
    ids=["retry-passes", "both-unreachable", "retry-invalid-report", "retry-warning"],
)
def test_schema_unreachable_retries_once_and_requires_a_clean_second_validation(
    monkeypatch: pytest.MonkeyPatch, second_payload: object, expected: list[str]
) -> None:
    argv = ("scripted-author.exe", "validate", "Demo.pbip")
    payloads = [_schema_unreachable_payload(), second_payload]
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert command == argv
        assert kwargs == {
            "check": False,
            "capture_output": True,
            "text": True,
            "timeout": 90,
            "shell": False,
        }
        assert payloads, "Schema failures must not cause an unbounded retry"
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payloads.pop(0)), stderr=""
        )

    monkeypatch.setattr(semantic_model_module.subprocess, "run", run)

    issues = semantic_model_module._validator_issues(argv, "Microsoft PBIR validator")

    assert calls == [argv, argv]
    assert not payloads
    assert len(issues) == len(expected)
    assert all(fragment in issue for fragment, issue in zip(expected, issues, strict=True))


@pytest.mark.parametrize(
    "data",
    [
        {
            "result": "failed",
            "errorCount": 1,
            "warningCount": 1,
            "diagnostics": {"PBIR_SCHEMA_UNREACHABLE": {}, "PBIR_SCHEMA_VALIDATION_ERROR": {}},
        },
        {
            "result": "failed",
            "errorCount": 1,
            "warningCount": 1,
            "diagnostics": {"PBIR_SCHEMA_UNREACHABLE": {}},
        },
        {
            "result": "succeededWithWarnings",
            "errorCount": 0,
            "warningCount": 2,
            "diagnostics": {"PBIR_SCHEMA_UNREACHABLE": {}, "PBIR_LAYOUT_WARNING": {}},
        },
        {
            "result": "succeededWithWarnings",
            "errorCount": False,
            "warningCount": 1,
            "diagnostics": {"PBIR_SCHEMA_UNREACHABLE": {}},
        },
    ],
    ids=[
        "schema-plus-validation-error",
        "nonzero-errors",
        "other-warning",
        "malformed-error-count",
    ],
)
def test_schema_failure_retry_is_not_used_for_other_report_failures(
    monkeypatch: pytest.MonkeyPatch, data: object
) -> None:
    calls = []

    def run(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert len(calls) == 1, "Report errors and unrelated warnings must fail immediately"
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"data": data}), stderr="")

    monkeypatch.setattr(semantic_model_module.subprocess, "run", run)

    issues = semantic_model_module._validator_issues(
        ("scripted-author.exe", "validate", "Demo.pbip"), "Microsoft PBIR validator"
    )

    assert issues
    assert len(calls) == 1


def test_pbi_cli_schema_named_diagnostics_do_not_enable_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    payload = {
        "valid": True,
        "summary": {"warnings": 1},
        **_schema_unreachable_payload(),
    }

    def run(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert len(calls) == 1
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(semantic_model_module.subprocess, "run", run)

    assert semantic_model_module._validator_issues(("scripted-pbi.exe",), "pbi-cli") == [
        "pbi-cli reported warnings"
    ]
    assert len(calls) == 1
