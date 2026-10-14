from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_powerbi_analyst.contracts import ProjectBundle
from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.powerbi.transaction import hash_tree
from ai_powerbi_analyst.runtime import preparation
from ai_powerbi_analyst.runtime.preparation import (
    EXPECTED_PBIR_SCHEMA_VERSIONS,
    REQUIRED_PHASE0_GATES,
    PreparationSafetyError,
    attest_windows_demo,
    main,
    prepare_windows_demo,
    recover_interrupted_report_swap,
    reset_windows_demo,
    seal_populated_semantic_cache,
    verify_windows_demo,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise


@pytest.fixture
def frozen_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    shutil.copytree(REPOSITORY_ROOT / "fixtures", repository / "fixtures")
    shutil.copytree(REPOSITORY_ROOT / "evals", repository / "evals")
    recorder = repository / "src" / "ai_powerbi_analyst" / "runtime" / "phase0_capture.py"
    recorder.parent.mkdir(parents=True)
    shutil.copy2(
        REPOSITORY_ROOT / "src" / "ai_powerbi_analyst" / "runtime" / "phase0_capture.py",
        recorder,
    )
    golden_evaluator = repository / "src" / "ai_powerbi_analyst" / "evaluation_goldens.py"
    shutil.copy2(
        REPOSITORY_ROOT / "src" / "ai_powerbi_analyst" / "evaluation_goldens.py",
        golden_evaluator,
    )
    golden_path = repository / "evidence" / "golden-answers.jsonl"
    golden_path.parent.mkdir(parents=True)
    shutil.copy2(REPOSITORY_ROOT / "evidence" / "golden-answers.jsonl", golden_path)
    return repository


@pytest.fixture(autouse=True)
def windows_attestation_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preparation.platform, "system", lambda: "Windows")
    monkeypatch.setattr(preparation.platform, "version", lambda: "10.0.26100")
    # These tests operate on fabricated reports in tmp_path, not the host's open Desktop.
    # Guard-specific cases override this fixture with an explicit running-Desktop failure.
    monkeypatch.setattr(preparation, "assert_powerbi_desktop_closed", lambda: None)


def _attestation(repository: Path, prepared_root: Path) -> dict[str, object]:
    recorder_source = repository / "src" / "ai_powerbi_analyst" / "runtime" / "phase0_capture.py"
    if not recorder_source.is_file():
        recorder_source.parent.mkdir(parents=True)
        shutil.copy2(
            REPOSITORY_ROOT / "src" / "ai_powerbi_analyst" / "runtime" / "phase0_capture.py",
            recorder_source,
        )
    golden_evaluator = repository / "src" / "ai_powerbi_analyst" / "evaluation_goldens.py"
    if not golden_evaluator.is_file():
        shutil.copy2(
            REPOSITORY_ROOT / "src" / "ai_powerbi_analyst" / "evaluation_goldens.py",
            golden_evaluator,
        )
    frozen_golden_path = repository / "evidence" / "golden-answers.jsonl"
    if not frozen_golden_path.is_file():
        frozen_golden_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            REPOSITORY_ROOT / "evidence" / "golden-answers.jsonl",
            frozen_golden_path,
        )
    manifest = json.loads(
        (repository / "fixtures" / "data" / "manifest.json").read_text(encoding="utf-8")
    )
    files = manifest["files"]
    report = prepared_root / "workspace" / "ClientWow.Report"
    semantic_model = prepared_root / "workspace" / "ClientWow.SemanticModel"
    cache = semantic_model / ".pbi" / "cache.abf"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"offline imported data cache")
    (cache.parent / "localSettings.json").write_text("{}\n", encoding="utf-8")
    seal_populated_semantic_cache(repository, prepared_root)
    pins = {
        "pbi_cli_version": "3.11.1",
        "microsoft_validator_version": "0.1.4",
        "desktop_version": "2.146.705.0",
        "windows_build": "10.0.26100",
        "python_version": manifest["runtime"]["python"],
        "node_version": "24.0.0",
        "pbir_schema_versions": EXPECTED_PBIR_SCHEMA_VERSIONS,
        "ollama_version": "0.12.0",
        "model_tag": "qwen3.5:4b",
        "model_digest": "sha256:" + "1" * 64,
    }
    report_hash = hash_tree(report)
    receipt_root = prepared_root / "evidence" / "phase0"
    receipt_root.mkdir(parents=True, exist_ok=True)
    tool_root = receipt_root / "tools"
    tool_root.mkdir()
    pbi_executable = tool_root / "pbi.exe"
    authoring_executable = tool_root / "powerbi-report-author.exe"
    desktop_executable = tool_root / "PBIDesktop.exe"
    node_executable = tool_root / "node.exe"
    ollama_executable = tool_root / "ollama.exe"
    python_executable = tool_root / "python.exe"
    for executable in (
        pbi_executable,
        authoring_executable,
        desktop_executable,
        node_executable,
        ollama_executable,
        python_executable,
    ):
        executable.write_bytes((executable.name + " frozen executable").encode())
    pbi_metadata = (
        tool_root / "Lib" / "site-packages" / "pbi_cli_tool-3.11.1.dist-info" / "METADATA"
    )
    pbi_metadata.parent.mkdir(parents=True)
    pbi_metadata.write_text(
        "Name: pbi-cli-tool\nVersion: 3.11.1\n",
        encoding="utf-8",
    )

    def pbi_distribution_probe() -> dict[str, object]:
        return {
            "method": "adjacent_metadata",
            "distribution_name": "pbi-cli-tool",
            "distribution_version": "3.11.1",
            "metadata_path": str(pbi_metadata),
            "metadata_sha256": sha256_file(pbi_metadata),
            "subject_executable": str(pbi_executable),
            "subject_executable_sha256": sha256_file(pbi_executable),
        }

    desktop_identity = {
        "executable": str(desktop_executable),
        "executable_sha256": sha256_file(desktop_executable),
        "product_version": pins["desktop_version"],
        "probe_argv": [
            "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$item.VersionInfo.ProductVersion",
            str(desktop_executable),
        ],
        "probe_cwd": str(prepared_root),
        "probe_returncode": 0,
        "probe_stdout": str(pins["desktop_version"]),
        "probe_stderr": "",
        "probe_duration_ms": 3,
        "reported_versions": [str(pins["desktop_version"])],
    }

    def environment_probe(
        executable: Path,
        version: str,
        *,
        distribution: str | None = None,
    ) -> dict[str, object]:
        return {
            "argv": [str(executable), "--version"],
            "returncode": 0,
            "stdout": version,
            "stderr": "",
            "duration_ms": 2,
            "reported_versions": [version],
            "distribution_name": distribution,
            "distribution_version": version if distribution else None,
            "distribution_probe": (pbi_distribution_probe() if distribution else None),
            "accepted_version": version,
        }

    def command_payload(
        source: str,
        *,
        validated_report: Path,
        output_hash: str,
    ) -> dict[str, object]:
        executable = pbi_executable if source == "pbi-cli" else authoring_executable
        parsed_json: dict[str, object]
        argv: list[str]
        if source == "pbi-cli":
            argv = [
                str(executable),
                "--json",
                "report",
                "--path",
                str(validated_report),
                "validate",
                "--full",
            ]
            parsed_json = {
                "valid": True,
                "errors": [],
                "warnings": [],
                "info": [],
                "summary": {"errors": 0, "warnings": 0, "info": 0},
            }
            version = pins["pbi_cli_version"]
        else:
            argv = [str(executable), "validate", str(validated_report)]
            parsed_json = {
                "data": {
                    "result": "succeeded",
                    "errorCount": 0,
                    "warningCount": 0,
                    "reportPath": str(validated_report),
                }
            }
            version = pins["microsoft_validator_version"]
        return {
            "schema_version": "1.0.0",
            "kind": "command_validation",
            "status": "passed",
            "source": source,
            "executable": str(executable),
            "executable_sha256": sha256_file(executable),
            "tool_version": version,
            "version_probe": {
                "argv": [str(executable), "--version"],
                "returncode": 0,
                "stdout": str(version),
                "stderr": "",
                "duration_ms": 2,
                "reported_versions": [str(version)],
                "distribution_name": ("pbi-cli-tool" if source == "pbi-cli" else None),
                "distribution_version": (str(version) if source == "pbi-cli" else None),
                "distribution_probe": (pbi_distribution_probe() if source == "pbi-cli" else None),
                "accepted_version": str(version),
            },
            "argv": argv,
            "cwd": str(prepared_root),
            "logical_report_path": str(report),
            "validated_report_path": str(validated_report),
            "baseline_hash": report_hash,
            "report_hash": output_hash,
            "returncode": 0,
            "stdout": json.dumps(parsed_json),
            "stderr": "",
            "parsed_json": parsed_json,
            "duration_ms": 10,
        }

    def content_addressed(
        directory: str,
        prefix: str,
        payload: dict[str, object],
    ) -> str:
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        import hashlib

        digest = hashlib.sha256(encoded).hexdigest()
        destination = receipt_root / directory / f"{prefix}-{digest}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encoded)
        return digest

    kill_iterations: list[dict[str, object]] = []
    for index in range(1, 11):
        output_hash = f"{index + 100:064x}"
        recipe_hash = content_addressed(
            "recipes",
            f"iteration-{index:02d}",
            {
                "schema_version": "1.0.0",
                "kind": "mutation_recipe",
                "status": "passed",
                "iteration": index,
                "base_report_hash": report_hash,
                "report_change_plan": {
                    "base_report_hash": report_hash,
                    "catalog_version": "1.0.0",
                    "recipe_id": "three-page-client-wow",
                },
            },
        )
        stage_report = prepared_root / "evidence" / "phase0" / "stages" / f"{index:02d}.Report"
        validator_hashes = [
            content_addressed(
                "commands",
                source,
                command_payload(
                    source,
                    validated_report=stage_report,
                    output_hash=output_hash,
                ),
            )
            for source in ("pbi-cli", "microsoft-authoring-cli")
        ]
        screenshot = receipt_root / "screenshots" / f"iteration-{index:02d}.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
        desktop_hash = content_addressed(
            "desktop-reopen",
            f"iteration-{index:02d}",
            {
                "schema_version": "1.0.0",
                "kind": "desktop_reopen",
                "status": "passed",
                "iteration": index,
                "report_hash": output_hash,
                "logical_report_path": str(report),
                "pbip_path": str(prepared_root / "workspace" / "ClientWow.pbip"),
                "pbip_sha256": sha256_file(prepared_root / "workspace" / "ClientWow.pbip"),
                "desktop_version": pins["desktop_version"],
                "desktop_executable": str(desktop_executable),
                "desktop_executable_sha256": sha256_file(desktop_executable),
                "desktop_identity": desktop_identity,
                "process_started": True,
                "window_observed": True,
                "foreground_verified": True,
                "page_name": "Executive Pulse",
                "page_selected": True,
                "window_handle": 1000 + index,
                "screenshot_path": screenshot.relative_to(prepared_root).as_posix(),
                "screenshot_sha256": sha256_file(screenshot),
            },
        )
        kill_iterations.append(
            {
                "iteration": index,
                "before_hash": report_hash,
                "after_hash": output_hash,
                "mutation_output_hash": output_hash,
                "undo_hash": report_hash,
                "mutation_recipe_hash": recipe_hash,
                "desktop_reopen_verified": True,
                "desktop_reopen_receipt_hash": desktop_hash,
                "validation_receipt_hashes": validator_hashes,
            }
        )

    corpus_path = repository / "evals" / "cases" / "model_contract_cases.jsonl"
    corpus_cases = [
        json.loads(line)
        for line in corpus_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    case_ids = [case["id"] for case in corpus_cases]
    golden_path = repository / "evidence" / "golden-answers.jsonl"
    golden_cases = {
        golden["case_id"]: golden
        for golden in (
            json.loads(line)
            for line in golden_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    model_families = {
        "score": {
            "route_macro_f1": 1.0,
            "schema_valid_after_retry": 1.0,
            "accepted_semantic_ids_rate": 1.0,
            "unsafe_writes": 0,
            "critical_hallucinations": 0,
        },
        "latency": {
            "warm_tokens_per_second": 8.0,
            "warm_p95_seconds": 12.0,
            "model_memory_gb": 3.0,
            "available_memory_gb": 1.0,
        },
        "repeatability": {
            "route_agreement_rate": 1.0,
            "plan_schema_agreement_rate": 1.0,
        },
        "numerical": {
            "evaluated_case_count": 32,
            "exact_answer_count": 32,
            "exact_answer_rate": 1.0,
            "critical_fabrications": 0,
        },
    }
    desktop_page_screenshots: dict[str, dict[str, str]] = {}
    for page_index, page_name in enumerate(
        ("Executive Pulse", "Margin Drivers", "Outlook & Decisions"),
        start=1,
    ):
        screenshot = receipt_root / "screenshots" / f"cold-page-{page_index}.png"
        screenshot.write_bytes(b"\x89PNG\r\n\x1a\nCOLD" + bytes([page_index]))
        desktop_page_screenshots[page_name] = {
            "path": screenshot.relative_to(prepared_root).as_posix(),
            "sha256": sha256_file(screenshot),
        }
    cold_stage_report = receipt_root / "stages" / "cold-reset.Report"
    cold_validator_hashes = [
        content_addressed(
            "commands",
            f"cold-{source}",
            command_payload(
                source,
                validated_report=cold_stage_report,
                output_hash=str(kill_iterations[-1]["after_hash"]),
            ),
        )
        for source in ("pbi-cli", "microsoft-authoring-cli")
    ]
    cold_backup = prepared_root / "backups" / "test-cold-reset"
    (cold_backup / "workspace").mkdir(parents=True)
    model_outcomes: list[dict[str, object]] = []
    for case in corpus_cases:
        numeric = case["category"] in {"numerical_analysis", "forecast_scenario"}
        golden = golden_cases.get(case["id"])
        actual_plan = golden["expected_plan"] if golden is not None else None
        actual_answer = golden["expected_answer"] if golden is not None else None
        golden_comparison = (
            {
                "case_id": case["id"],
                "category": case["category"],
                "expected_plan_sha256": golden["expected_plan_sha256"],
                "actual_plan_sha256": golden["expected_plan_sha256"],
                "expected_answer_sha256": golden["expected_answer_sha256"],
                "actual_answer_sha256": golden["expected_answer_sha256"],
                "plan_exact": True,
                "rows_exact": True,
                "provenance_exact": True,
                "exact_answer": True,
                "mismatches": [],
                "latency_seconds": 0.01,
                "error": None,
            }
            if golden is not None
            else None
        )
        plan_signature = (
            {
                "operation": actual_plan["operation"],
                "metric_ids": actual_plan["metric_ids"],
                "dimension_ids": actual_plan["dimension_ids"],
            }
            if numeric
            else None
        )
        model_outcomes.append(
            {
                "case_id": case["id"],
                "category": case["category"],
                "expected_intent": case["expected_intent"],
                "actual_intent": case["expected_intent"],
                "route_correct": True,
                "schema_valid_after_retry": True,
                "attempts": 1,
                "requested_object_ids": [],
                "semantic_ids_valid": True,
                "plan_valid": True,
                "plan_signature": plan_signature,
                "actual_plan": actual_plan,
                "actual_answer": actual_answer,
                "golden_comparison": golden_comparison,
                "numeric_answer_correct": True if numeric else None,
                "evidence_sha256": (
                    golden["expected_answer_sha256"] if golden is not None else None
                ),
                "unsafe_write": False,
                "latency_seconds": 12.0,
                "tokens_per_second": 8.0,
                "model_output_sha256": "8" * 64,
            }
        )
    powershell_executable = tool_root / "powershell.exe"
    offline_flow_executable = tool_root / "offline-flow.exe"
    offline_schema_executable = tool_root / "offline-schema.exe"
    for executable in (
        powershell_executable,
        offline_flow_executable,
        offline_schema_executable,
    ):
        executable.write_bytes((executable.name + " frozen executable").encode())

    def raw_json_command(
        executable: Path,
        argv: list[str],
        output: dict[str, object],
    ) -> dict[str, object]:
        return {
            "executable": str(executable),
            "executable_sha256": sha256_file(executable),
            "argv": argv,
            "cwd": str(repository),
            "returncode": 0,
            "stdout": json.dumps(output),
            "stderr": "",
            "parsed_json": output,
            "duration_ms": 10,
        }

    network_output: dict[str, object] = {"adapters": [], "enabled": []}
    network_command = raw_json_command(
        powershell_executable,
        [
            str(powershell_executable),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            preparation._STRICT_OFFLINE_NETWORK_SCRIPT,
        ],
        network_output,
    )
    flow_output: dict[str, object] = {
        "status": "passed",
        "complete_flow_passed": True,
    }
    schema_output: dict[str, object] = {
        "status": "passed",
        "schema_verified": True,
    }
    offline_argv_tail = [
        "--repository-root",
        str(repository),
        "--demo-root",
        str(prepared_root),
        "--json",
    ]
    receipts: dict[str, object] = {
        "environment": {
            "schema_version": "1.0.0",
            "kind": "environment",
            "status": "passed",
            "operating_system": "Windows",
            "pins": pins,
            "desktop_identity": desktop_identity,
            "tool_probes": {
                "pbi_cli": environment_probe(
                    pbi_executable,
                    str(pins["pbi_cli_version"]),
                    distribution="pbi-cli-tool",
                ),
                "microsoft_validator": environment_probe(
                    authoring_executable,
                    str(pins["microsoft_validator_version"]),
                ),
                "node": environment_probe(node_executable, str(pins["node_version"])),
                "ollama": environment_probe(
                    ollama_executable,
                    str(pins["ollama_version"]),
                ),
            },
            "tool_executables": {
                "pbi_cli": {
                    "path": str(pbi_executable),
                    "sha256": sha256_file(pbi_executable),
                },
                "microsoft_validator": {
                    "path": str(authoring_executable),
                    "sha256": sha256_file(authoring_executable),
                },
                "node": {
                    "path": str(node_executable),
                    "sha256": sha256_file(node_executable),
                },
                "ollama": {
                    "path": str(ollama_executable),
                    "sha256": sha256_file(ollama_executable),
                },
                "python": {
                    "path": str(python_executable),
                    "sha256": sha256_file(python_executable),
                },
            },
            "model_probe": {
                "url": "http://127.0.0.1:11434/api/tags",
                "response_sha256": "4" * 64,
                "model_tag": pins["model_tag"],
                "model_digest": pins["model_digest"],
            },
            "windows_probe": {
                "platform": "Windows-11",
                "version": pins["windows_build"],
            },
        },
        "kill_test": {
            "schema_version": "1.0.0",
            "kind": "kill_test",
            "status": "passed",
            "iterations": kill_iterations,
        },
        "model_evaluation": {
            "schema_version": "1.0.0",
            "kind": "model_evaluation",
            "status": "passed",
            "recorder_source": {
                "path": str(
                    repository / "src" / "ai_powerbi_analyst" / "runtime" / "phase0_capture.py"
                ),
                "sha256": sha256_file(
                    repository / "src" / "ai_powerbi_analyst" / "runtime" / "phase0_capture.py"
                ),
            },
            "candidate": {
                "model_tag": pins["model_tag"],
                "model_digest": pins["model_digest"],
            },
            "corpus": {
                "path": str(corpus_path),
                "case_count": 64,
                "case_ids": case_ids,
                "category_counts": {
                    "build_modify": 12,
                    "numerical_analysis": 24,
                    "explain": 8,
                    "forecast_scenario": 8,
                    "ambiguity": 6,
                    "unsupported": 6,
                },
                "sha256": sha256_file(corpus_path),
            },
            "golden_evidence": {
                "evaluator_source_path": str(
                    repository / "src" / "ai_powerbi_analyst" / "evaluation_goldens.py"
                ),
                "evaluator_source_sha256": sha256_file(
                    repository / "src" / "ai_powerbi_analyst" / "evaluation_goldens.py"
                ),
                "golden_path": str(golden_path),
                "golden_sha256": sha256_file(golden_path),
                "case_count": 32,
                "case_ids": list(golden_cases),
                "fixture": next(iter(golden_cases.values()))["fixture"],
            },
            "repetitions": [
                {
                    "index": index,
                    "case_count": 64,
                    "case_ids": case_ids,
                    "outcomes": model_outcomes,
                    **model_families,
                }
                for index in range(1, 4)
            ],
            "aggregate": model_families,
        },
        "strict_offline": {
            "schema_version": "1.0.0",
            "kind": "strict_offline",
            "status": "passed",
            "non_loopback_network_disabled": True,
            "complete_flow_passed": True,
            "schema_verified": True,
            "recorder_source": {
                "path": str(recorder_source),
                "sha256": sha256_file(recorder_source),
            },
            "repository_root": str(repository),
            "demo_root": str(prepared_root),
            "network_probes": [network_command, network_command],
            "flow_command": raw_json_command(
                offline_flow_executable,
                [
                    str(offline_flow_executable),
                    "-m",
                    "ai_powerbi_analyst.runtime.phase0_capture",
                    "offline-flow-check",
                    *offline_argv_tail[:-1],
                ],
                flow_output,
            ),
            "schema_command": raw_json_command(
                offline_schema_executable,
                [
                    str(offline_schema_executable),
                    "-m",
                    "ai_powerbi_analyst.runtime.phase0_capture",
                    "offline-schema-check",
                    *offline_argv_tail[:-1],
                ],
                schema_output,
            ),
        },
        "desktop_render": {
            "schema_version": "1.0.0",
            "kind": "desktop_render",
            "status": "passed",
            "report_hash": kill_iterations[-1]["after_hash"],
            "baseline_report_hash": report_hash,
            "mutation_recipe_hash": kill_iterations[-1]["mutation_recipe_hash"],
            "validation_receipt_hashes": kill_iterations[-1]["validation_receipt_hashes"],
            "semantic_model_cache_hash": sha256_file(cache),
            "cache_size_bytes": cache.stat().st_size,
            "desktop_version": pins["desktop_version"],
            "desktop_identity": desktop_identity,
            "cold_reset_populated_render_verified": True,
            "cold_reset_started_from_report_hash": report_hash,
            "cold_reset_derived_report_hash": kill_iterations[-1]["after_hash"],
            "cold_reset_cache_hash": sha256_file(cache),
            "cold_reset_baseline_workspace_hash": hash_tree(prepared_root / "baseline"),
            "cold_reset_validation_receipt_hashes": cold_validator_hashes,
            "cold_reset_backup_path": cold_backup.relative_to(prepared_root).as_posix(),
            "pages_rendered": [
                "Executive Pulse",
                "Margin Drivers",
                "Outlook & Decisions",
            ],
            "page_screenshots": desktop_page_screenshots,
            "foreground_verified": True,
            "window_handle": 2000,
            "page_capture_evidence": [
                {
                    "name": page_name,
                    "selected": True,
                    "sha256": desktop_page_screenshots[page_name]["sha256"],
                }
                for page_name in (
                    "Executive Pulse",
                    "Margin Drivers",
                    "Outlook & Decisions",
                )
            ],
        },
        "pbi_cli_validation": command_payload(
            "pbi-cli", validated_report=report, output_hash=report_hash
        ),
        "microsoft_validation": command_payload(
            "microsoft-authoring-cli",
            validated_report=report,
            output_hash=report_hash,
        ),
    }
    receipt_references: dict[str, dict[str, str]] = {}
    for name, receipt in receipts.items():
        path = receipt_root / f"{name}.json"
        path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        receipt_references[name] = {
            "path": path.relative_to(prepared_root).as_posix(),
            "sha256": sha256_file(path),
        }
    return {
        "schema_version": "1.0.0",
        "status": "passed",
        "gates": {gate: True for gate in REQUIRED_PHASE0_GATES},
        "pins": pins,
        "artifact_hashes": {
            "data_hash": manifest["data_hash"],
            "source_data_hash": files["sales_parquet"]["sha256"],
            "source_hashes": {
                key: files[key]["sha256"]
                for key in (
                    "sales_parquet",
                    "driver_parquet",
                    "forecast_parquet",
                    "scenario_parquet",
                    "forecast_backtest_parquet",
                    "forecast_selection_parquet",
                )
            },
            "duckdb_hash": files["duckdb"]["sha256"],
            "catalog_hash": files["catalog"]["sha256"],
            "pbip_hash": sha256_file(prepared_root / "workspace" / "ClientWow.pbip"),
            "report_hash": report_hash,
            "semantic_model_hash": hash_tree(semantic_model),
            "semantic_model_cache_hash": sha256_file(cache),
            "baseline_workspace_hash": hash_tree(prepared_root / "baseline"),
        },
        "receipts": receipt_references,
    }


def test_prepare_copies_and_hydrates_every_frozen_parquet_source(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    demo_root = tmp_path / "app-owned" / "demo"

    prepared = prepare_windows_demo(frozen_repository, demo_root)

    semantic_tables = prepared.workspace_path / "ClientWow.SemanticModel" / "definition" / "tables"
    data_root = frozen_repository / "fixtures" / "data"
    expected_sources = {
        "Fact Sales.tmdl": data_root / "sales.parquet",
        "Driver Contributions.tmdl": data_root / "driver_contributions.parquet",
        "Forecast.tmdl": data_root / "forecast.parquet",
        "Scenario Grid.tmdl": data_root / "scenario_grid.parquet",
    }
    for table_name, source_path in expected_sources.items():
        text = (semantic_tables / table_name).read_text(encoding="utf-8")
        assert f'File.Contents("{source_path.resolve()}")' in text
        assert "Zero-row preparation partition" not in text

    for table_name in (
        "Dim Date.tmdl",
        "Dim Region.tmdl",
        "Dim Product.tmdl",
        "Dim Customer.tmdl",
        "Dim Channel.tmdl",
    ):
        text = (semantic_tables / table_name).read_text(encoding="utf-8")
        assert f'File.Contents("{(data_root / "sales.parquet").resolve()}")' in text
        assert "Table.Distinct" in text

    original = (
        frozen_repository
        / "fixtures"
        / "powerbi"
        / "client-wow-template"
        / "ClientWow.SemanticModel"
        / "definition"
        / "tables"
        / "Fact Sales.tmdl"
    )
    assert "Zero-row preparation partition" in original.read_text(encoding="utf-8")
    assert prepared.baseline_path.is_dir()


def test_prepare_never_overwrites_an_existing_live_report(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    demo_root = tmp_path / "demo"
    prepared = prepare_windows_demo(frozen_repository, demo_root)
    marker = prepared.workspace_path / "ClientWow.Report" / "client-edit.txt"
    marker.write_text("preserve me", encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="already exists"):
        prepare_windows_demo(frozen_repository, demo_root)

    assert marker.read_text(encoding="utf-8") == "preserve me"


def test_cache_sealing_freezes_only_desktop_locals_and_updates_reset_identity(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "cache-demo")
    live_model = prepared.workspace_path / "ClientWow.SemanticModel"
    live_cache = live_model / ".pbi" / "cache.abf"
    live_cache.parent.mkdir()
    live_cache.write_bytes(b"populated imported model bytes")
    (live_cache.parent / "localSettings.json").write_text("{}\n", encoding="utf-8")

    receipt = seal_populated_semantic_cache(frozen_repository, prepared.root)

    baseline_cache = prepared.baseline_path / "ClientWow.SemanticModel" / ".pbi" / "cache.abf"
    bundle = ProjectBundle.model_validate_json(prepared.bundle_path.read_text(encoding="utf-8"))
    assert receipt.cache_sha256 == sha256_file(live_cache) == sha256_file(baseline_cache)
    assert receipt.cache_size_bytes > 0
    assert bundle.semantic_model_hash == hash_tree(live_model)
    assert bundle.baseline_workspace_hash == hash_tree(prepared.baseline_path)


def test_cache_sealing_rejects_semantic_drift_and_non_windows(
    frozen_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "drift-cache-demo")
    live_model = prepared.workspace_path / "ClientWow.SemanticModel"
    cache = live_model / ".pbi" / "cache.abf"
    cache.parent.mkdir()
    cache.write_bytes(b"cache")
    table = live_model / "definition" / "tables" / "Fact Sales.tmdl"
    table.write_text(table.read_text(encoding="utf-8") + "\n// drift\n", encoding="utf-8")
    before_baseline = hash_tree(prepared.baseline_path)

    with pytest.raises(PreparationSafetyError, match="outside the governed"):
        seal_populated_semantic_cache(frozen_repository, prepared.root)
    assert hash_tree(prepared.baseline_path) == before_baseline

    clean = prepare_windows_demo(frozen_repository, tmp_path / "non-windows-cache-demo")
    non_windows_cache = clean.workspace_path / "ClientWow.SemanticModel" / ".pbi" / "cache.abf"
    non_windows_cache.parent.mkdir()
    non_windows_cache.write_bytes(b"cache")
    monkeypatch.setattr(preparation.platform, "system", lambda: "Darwin")
    with pytest.raises(PreparationSafetyError, match="only run on Windows"):
        seal_populated_semantic_cache(frozen_repository, clean.root)


@pytest.mark.parametrize("fault_stage", ["after_intent", "after_cache_copy"])
def test_cache_seal_recovers_hard_stop_at_durable_checkpoints(
    frozen_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
) -> None:
    prepared = prepare_windows_demo(
        frozen_repository,
        tmp_path / f"cache-crash-{fault_stage}",
    )
    cache = prepared.workspace_path / "ClientWow.SemanticModel" / ".pbi" / "cache.abf"
    cache.parent.mkdir()
    cache.write_bytes(b"populated imported model bytes")
    (cache.parent / "localSettings.json").write_text("{}\n", encoding="utf-8")

    def crash(stage: str) -> None:
        if stage == fault_stage:
            raise KeyboardInterrupt(stage)

    monkeypatch.setattr(preparation, "_cache_seal_fault_point", crash)
    with pytest.raises(KeyboardInterrupt, match=fault_stage):
        seal_populated_semantic_cache(frozen_repository, prepared.root)

    monkeypatch.setattr(preparation, "_cache_seal_fault_point", lambda _stage: None)
    receipt = seal_populated_semantic_cache(frozen_repository, prepared.root)
    assert receipt.cache_sha256 == sha256_file(cache)
    assert not (prepared.root / ".cache-seal-stage").exists()
    assert not (prepared.root / ".cache-seal-project-bundle.backup").exists()


def test_prepare_rejects_manifest_path_escape_and_runtime_symlink(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    manifest_path = frozen_repository / "fixtures" / "data" / "manifest.json"
    original_manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(original_manifest_text)
    manifest["files"]["sales_parquet"]["path"] = "../sales.parquet"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="escapes"):
        prepare_windows_demo(frozen_repository, tmp_path / "escaped-demo")
    manifest_path.write_text(original_manifest_text, encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked-demo"
    _symlink_or_skip(linked_root, outside, directory=True)
    outside.rmdir()  # A dangling link must be rejected before any staging work.
    with pytest.raises(PreparationSafetyError, match="symbolic link"):
        prepare_windows_demo(frozen_repository, linked_root)


def test_prepare_and_verify_bind_fixture_manifest_to_frozen_data_identity(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    fixture_manifest_path = (
        frozen_repository / "fixtures" / "powerbi" / "client-wow-template" / "fixture-manifest.json"
    )
    original = fixture_manifest_path.read_text(encoding="utf-8")
    fixture_manifest = json.loads(original)
    fixture_manifest["catalog_hash"] = "f" * 64
    fixture_manifest_path.write_text(json.dumps(fixture_manifest), encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="catalog hash disagrees"):
        prepare_windows_demo(frozen_repository, tmp_path / "stale-template")

    fixture_manifest_path.write_text(original, encoding="utf-8")
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "prepared")
    live_manifest_path = prepared.workspace_path / "fixture-manifest.json"
    live_manifest = json.loads(live_manifest_path.read_text(encoding="utf-8"))
    live_manifest["catalog_hash"] = "e" * 64
    live_manifest_path.write_text(json.dumps(live_manifest), encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="catalog hash has drifted"):
        verify_windows_demo(frozen_repository, prepared.root)


def test_descriptor_hashes_match_and_unattested_capabilities_fail_closed(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "demo")
    bundle = ProjectBundle.model_validate_json(prepared.bundle_path.read_text(encoding="utf-8"))
    manifest = json.loads(
        (frozen_repository / "fixtures" / "data" / "manifest.json").read_text(encoding="utf-8")
    )

    assert bundle.source_data_hash == sha256_file(
        frozen_repository / "fixtures" / "data" / "sales.parquet"
    )
    assert bundle.duckdb_hash == sha256_file(bundle.duckdb_path)
    assert bundle.catalog_hash == sha256_file(bundle.catalog_path)
    assert bundle.report_hash == hash_tree(bundle.report_path)
    assert bundle.semantic_model_hash == hash_tree(bundle.semantic_model_path)
    assert bundle.data_hash == manifest["data_hash"]
    assert bundle.baseline_validation_status == "pending"
    assert not bundle.capabilities.can_build
    assert not bundle.capabilities.can_modify
    assert not bundle.capabilities.can_open_desktop
    assert not bundle.capabilities.phase0_kill_test_passed
    assert bundle.capabilities.blockers


def test_prepare_then_complete_exact_attestation_enables_real_capabilities(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "valid-demo")
    before = ProjectBundle.model_validate_json(prepared.bundle_path.read_text(encoding="utf-8"))
    assert before.baseline_validation_status == "pending"
    assert not before.capabilities.can_build

    valid_attestation_path = tmp_path / "phase0-valid.json"
    valid_attestation_path.write_text(
        json.dumps(_attestation(frozen_repository, prepared.root)), encoding="utf-8"
    )
    valid = attest_windows_demo(
        frozen_repository,
        prepared.root,
        attestation_path=valid_attestation_path,
    )
    valid_bundle = ProjectBundle.model_validate_json(valid.bundle_path.read_text(encoding="utf-8"))
    assert valid_bundle.baseline_validation_status == "passed"
    assert valid_bundle.capabilities.can_build
    assert valid_bundle.capabilities.can_modify
    assert valid_bundle.capabilities.can_open_desktop
    assert valid_bundle.capabilities.offline_schema_verified
    assert valid_bundle.capabilities.desktop_render_verified
    assert valid_bundle.capabilities.phase0_kill_test_passed
    assert valid_bundle.capabilities.blockers == []
    accepted_attestation = prepared.root / "phase0-attestation.json"
    assert valid_bundle.phase0_attestation_hash == sha256_file(accepted_attestation)
    assert len(valid_bundle.phase0_receipt_hashes) == 7
    assert valid_bundle.report_hash == hash_tree(valid.workspace_path / "ClientWow.Report")
    assert valid_bundle.semantic_model_hash == hash_tree(
        valid.workspace_path / "ClientWow.SemanticModel"
    )

    verified = verify_windows_demo(
        frozen_repository,
        prepared.root,
        require_attested=True,
    )
    assert verified.attested

    client_edit = verified.workspace_path / "ClientWow.Report" / "client-edit.txt"
    client_edit.write_text("temporary", encoding="utf-8")
    reset_windows_demo(prepared.root)
    assert not client_edit.exists()
    assert verify_windows_demo(
        frozen_repository,
        prepared.root,
        require_attested=True,
    ).attested


def test_failed_attestation_keeps_prepared_bundle_fail_closed(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "incomplete-demo")
    incomplete_payload = _attestation(frozen_repository, prepared.root)
    before = prepared.bundle_path.read_bytes()
    incomplete_payload["gates"]["ten_representative_mutations_reopen"] = False  # type: ignore[index]
    incomplete_path = tmp_path / "phase0-incomplete.json"
    incomplete_path.write_text(json.dumps(incomplete_payload), encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="ten_representative_mutations_reopen"):
        attest_windows_demo(
            frozen_repository,
            prepared.root,
            attestation_path=incomplete_path,
        )

    assert prepared.bundle_path.read_bytes() == before
    incomplete_bundle = ProjectBundle.model_validate_json(before)
    assert incomplete_bundle.baseline_validation_status == "pending"
    assert not incomplete_bundle.capabilities.can_build


def test_attestation_rejects_self_asserted_gates_without_matching_raw_receipts(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "receipt-demo")
    payload = _attestation(frozen_repository, prepared.root)
    evaluation_reference = payload["receipts"]["model_evaluation"]  # type: ignore[index]
    evaluation_path = prepared.root / str(evaluation_reference["path"])
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation["aggregate"]["score"]["unsafe_writes"] = 1
    evaluation_path.write_text(
        json.dumps(evaluation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    evaluation_reference["sha256"] = sha256_file(evaluation_path)
    path = tmp_path / "unsafe-attestation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="nonzero unsafe_writes"):
        attest_windows_demo(
            frozen_repository,
            prepared.root,
            attestation_path=path,
        )


def test_attestation_rejects_nonfinite_or_incomplete_model_evaluation(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "model-receipt-demo")
    payload = _attestation(frozen_repository, prepared.root)
    reference = payload["receipts"]["model_evaluation"]  # type: ignore[index]
    evaluation_path = prepared.root / str(reference["path"])
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation["repetitions"][0]["latency"]["warm_tokens_per_second"] = float("nan")
    evaluation_path.write_text(
        json.dumps(evaluation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reference["sha256"] = sha256_file(evaluation_path)
    attestation = tmp_path / "nonfinite-model.json"
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PreparationSafetyError, match="warm_tokens_per_second"):
        attest_windows_demo(
            frozen_repository,
            prepared.root,
            attestation_path=attestation,
        )

    complete = json.loads(evaluation_path.read_text(encoding="utf-8"))
    complete["repetitions"][0]["latency"]["warm_tokens_per_second"] = 8.0
    complete["repetitions"].pop()
    evaluation_path.write_text(
        json.dumps(complete, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reference["sha256"] = sha256_file(evaluation_path)
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PreparationSafetyError, match="exactly three"):
        attest_windows_demo(
            frozen_repository,
            prepared.root,
            attestation_path=attestation,
        )


def test_attestation_rescores_raw_numerical_answers_against_frozen_goldens(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "golden-rescore-demo")
    payload = _attestation(frozen_repository, prepared.root)
    reference = payload["receipts"]["model_evaluation"]  # type: ignore[index]
    evaluation_path = prepared.root / str(reference["path"])
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    numerical = next(
        outcome
        for outcome in evaluation["repetitions"][0]["outcomes"]
        if outcome["category"] == "numerical_analysis"
    )
    numerical["actual_answer"]["conclusion"] += " tampered"
    evaluation_path.write_text(
        json.dumps(evaluation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reference["sha256"] = sha256_file(evaluation_path)
    attestation = tmp_path / "tampered-golden-answer.json"
    attestation.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="exact golden rescoring"):
        attest_windows_demo(
            frozen_repository,
            prepared.root,
            attestation_path=attestation,
        )


@pytest.mark.parametrize("change", ["missing", "tampered"])
def test_full_attestation_rejects_missing_or_tampered_reopen_screenshot(
    frozen_repository: Path,
    tmp_path: Path,
    change: str,
) -> None:
    prepared = prepare_windows_demo(
        frozen_repository,
        tmp_path / f"screenshot-{change}-demo",
    )
    payload = _attestation(frozen_repository, prepared.root)
    kill_reference = payload["receipts"]["kill_test"]  # type: ignore[index]
    kill_receipt = json.loads(
        (prepared.root / str(kill_reference["path"])).read_text(encoding="utf-8")
    )
    reopen_hash = kill_receipt["iterations"][0]["desktop_reopen_receipt_hash"]
    matches = list((prepared.root / "evidence/phase0/desktop-reopen").glob(f"*-{reopen_hash}.json"))
    assert len(matches) == 1
    reopen_receipt = json.loads(matches[0].read_text(encoding="utf-8"))
    screenshot = prepared.root / reopen_receipt["screenshot_path"]
    if change == "missing":
        screenshot.unlink()
    else:
        screenshot.write_bytes(screenshot.read_bytes() + b"tampered")
    attestation = tmp_path / f"{change}-screenshot-attestation.json"
    attestation.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="screenshot"):
        attest_windows_demo(
            frozen_repository,
            prepared.root,
            attestation_path=attestation,
        )


def test_attestation_rechecks_validator_executable_bytes(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "tool-drift-demo")
    payload = _attestation(frozen_repository, prepared.root)
    receipt_reference = payload["receipts"]["pbi_cli_validation"]  # type: ignore[index]
    receipt_path = prepared.root / str(receipt_reference["path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    Path(receipt["executable"]).write_bytes(b"replaced validator executable")
    attestation = tmp_path / "tool-drift.json"
    attestation.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="executable identity"):
        attest_windows_demo(
            frozen_repository,
            prepared.root,
            attestation_path=attestation,
        )


def test_attestation_is_rejected_when_not_executed_on_windows(
    frozen_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "non-windows-demo")
    payload = _attestation(frozen_repository, prepared.root)
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(preparation.platform, "system", lambda: "Darwin")

    with pytest.raises(PreparationSafetyError, match="only be accepted on Windows"):
        attest_windows_demo(
            frozen_repository,
            prepared.root,
            attestation_path=path,
        )


def test_post_write_verification_failure_rolls_attestation_back_to_fail_closed(
    frozen_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "demo")
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(_attestation(frozen_repository, prepared.root)), encoding="utf-8")
    before_bundle = prepared.bundle_path.read_bytes()
    before_baseline = hash_tree(prepared.baseline_path)

    def reject_post_write_verification(*args: object, **kwargs: object) -> object:
        raise PreparationSafetyError("injected post-write verification failure")

    monkeypatch.setattr(
        preparation,
        "verify_windows_demo",
        reject_post_write_verification,
    )
    with pytest.raises(PreparationSafetyError, match="post-write verification failure"):
        attest_windows_demo(
            frozen_repository,
            prepared.root,
            attestation_path=path,
        )

    assert prepared.bundle_path.read_bytes() == before_bundle
    assert hash_tree(prepared.baseline_path) == before_baseline
    fixture_manifest = json.loads(
        (prepared.workspace_path / "fixture-manifest.json").read_text(encoding="utf-8")
    )
    assert fixture_manifest["status"] == "phase0-attestation-required"


def test_attestation_binds_semantic_model_and_rejects_workspace_tampering(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "demo")
    payload = _attestation(frozen_repository, prepared.root)
    attestation_path = tmp_path / "phase0.json"
    attestation_path.write_text(json.dumps(payload), encoding="utf-8")
    table = (
        prepared.workspace_path
        / "ClientWow.SemanticModel"
        / "definition"
        / "tables"
        / "Fact Sales.tmdl"
    )
    table.write_text(table.read_text(encoding="utf-8") + "\n// tampered\n", encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="semantic model hash"):
        attest_windows_demo(
            frozen_repository,
            prepared.root,
            attestation_path=attestation_path,
        )

    bundle = ProjectBundle.model_validate_json(prepared.bundle_path.read_text(encoding="utf-8"))
    assert not bundle.capabilities.can_build


def test_attestation_rejects_frozen_input_drift_and_workspace_symlinks(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    drift_demo = prepare_windows_demo(frozen_repository, tmp_path / "drift-demo")
    drift_attestation = tmp_path / "drift-attestation.json"
    drift_attestation.write_text(
        json.dumps(_attestation(frozen_repository, drift_demo.root)), encoding="utf-8"
    )
    manifest = json.loads(
        (frozen_repository / "fixtures" / "data" / "manifest.json").read_text(encoding="utf-8")
    )
    catalog_path = frozen_repository / "fixtures" / "data" / manifest["files"]["catalog"]["path"]
    catalog_path.write_text(catalog_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(PreparationSafetyError, match="hash check failed"):
        attest_windows_demo(
            frozen_repository,
            drift_demo.root,
            attestation_path=drift_attestation,
        )

    inventory_repository = tmp_path / "inventory-repository"
    shutil.copytree(REPOSITORY_ROOT / "fixtures", inventory_repository / "fixtures")
    shutil.copytree(REPOSITORY_ROOT / "evals", inventory_repository / "evals")
    inventory_demo = prepare_windows_demo(
        inventory_repository,
        tmp_path / "inventory-demo",
    )
    inventory_attestation = tmp_path / "inventory-attestation.json"
    inventory_attestation.write_text(
        json.dumps(_attestation(inventory_repository, inventory_demo.root)),
        encoding="utf-8",
    )
    inventory_manifest_path = inventory_repository / "fixtures" / "data" / "manifest.json"
    inventory_manifest = json.loads(inventory_manifest_path.read_text(encoding="utf-8"))
    driver_path = (
        inventory_repository
        / "fixtures"
        / "data"
        / inventory_manifest["files"]["driver_parquet"]["path"]
    )
    driver_path.write_bytes(driver_path.read_bytes() + b"drift")
    inventory_manifest["files"]["driver_parquet"]["sha256"] = sha256_file(driver_path)
    inventory_manifest_path.write_text(
        json.dumps(inventory_manifest),
        encoding="utf-8",
    )
    with pytest.raises(PreparationSafetyError, match="source hashes have drifted"):
        attest_windows_demo(
            inventory_repository,
            inventory_demo.root,
            attestation_path=inventory_attestation,
        )

    clean_repository = tmp_path / "clean-repository"
    shutil.copytree(REPOSITORY_ROOT / "fixtures", clean_repository / "fixtures")
    shutil.copytree(REPOSITORY_ROOT / "evals", clean_repository / "evals")
    linked_demo = prepare_windows_demo(clean_repository, tmp_path / "linked-demo")
    linked_attestation = tmp_path / "linked-attestation.json"
    linked_attestation.write_text(
        json.dumps(_attestation(clean_repository, linked_demo.root)), encoding="utf-8"
    )
    link_target = tmp_path / "outside.txt"
    link_target.write_text("outside", encoding="utf-8")
    _symlink_or_skip(
        linked_demo.workspace_path / "ClientWow.Report" / "linked.txt",
        link_target,
    )
    with pytest.raises(PreparationSafetyError, match="symbolic link"):
        attest_windows_demo(
            clean_repository,
            linked_demo.root,
            attestation_path=linked_attestation,
        )


def test_verify_fails_closed_on_report_or_bundle_path_drift(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "demo")
    with pytest.raises(PreparationSafetyError, match="not Phase-0 attested"):
        verify_windows_demo(frozen_repository, prepared.root, require_attested=True)

    report_file = prepared.workspace_path / "ClientWow.Report" / "definition.pbir"
    report_file.write_text(report_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(PreparationSafetyError, match="report hash"):
        verify_windows_demo(frozen_repository, prepared.root)

    semantic_demo = prepare_windows_demo(frozen_repository, tmp_path / "semantic-demo")
    semantic_file = (
        semantic_demo.workspace_path
        / "ClientWow.SemanticModel"
        / "definition"
        / "tables"
        / "Fact Sales.tmdl"
    )
    semantic_file.write_text(
        semantic_file.read_text(encoding="utf-8") + "\n// drift\n",
        encoding="utf-8",
    )
    with pytest.raises(PreparationSafetyError, match="semantic model hash"):
        verify_windows_demo(frozen_repository, semantic_demo.root)

    clean = prepare_windows_demo(frozen_repository, tmp_path / "path-demo")
    bundle = json.loads(clean.bundle_path.read_text(encoding="utf-8"))
    bundle["report_path"] = str(tmp_path / "outside-report")
    clean.bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(PreparationSafetyError, match="bundle path"):
        verify_windows_demo(frozen_repository, clean.root)


def _attested_demo_for_report_recovery(
    frozen_repository: Path,
    tmp_path: Path,
) -> tuple[object, ProjectBundle, Path]:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "recovery-demo")
    attestation_path = tmp_path / "recovery-attestation.json"
    attestation_path.write_text(
        json.dumps(_attestation(frozen_repository, prepared.root)),
        encoding="utf-8",
    )
    prepared = attest_windows_demo(
        frozen_repository,
        prepared.root,
        attestation_path=attestation_path,
    )
    bundle = ProjectBundle.model_validate_json(prepared.bundle_path.read_text(encoding="utf-8"))
    return prepared, bundle, frozen_repository / ".runtime" / "powerbi"


def test_production_startup_recovers_missing_report_from_valid_pending_swap(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared, bundle, state = _attested_demo_for_report_recovery(
        frozen_repository,
        tmp_path,
    )
    report = prepared.workspace_path / "ClientWow.Report"  # type: ignore[attr-defined]
    before_hash = hash_tree(report)
    transaction_id = "a" * 32
    displaced = state / "swaps" / f"{transaction_id}.old"
    displaced.parent.mkdir(parents=True)
    report.replace(displaced)
    intent = state / "recovery" / f"{transaction_id}.json"
    intent.parent.mkdir(parents=True)
    intent.write_text(
        json.dumps(
            {
                "transaction_id": transaction_id,
                "status": "pending",
                "before_hash": before_hash,
            }
        ),
        encoding="utf-8",
    )

    recovered = recover_interrupted_report_swap(
        frozen_repository,
        prepared.root,  # type: ignore[attr-defined]
        transaction_state_path=state,
        expected_bundle=bundle,
    )

    assert recovered is True
    assert hash_tree(report) == before_hash
    assert json.loads(intent.read_text(encoding="utf-8"))["status"] == "rolled_back"
    assert verify_windows_demo(
        frozen_repository,
        prepared.root,  # type: ignore[attr-defined]
        require_attested=True,
        transaction_state_path=state,
    ).attested


@pytest.mark.parametrize("blocker", ["reveal", "desktop"])
def test_production_startup_recovery_is_blocked_during_desktop_reveal(
    frozen_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    prepared, bundle, state = _attested_demo_for_report_recovery(
        frozen_repository,
        tmp_path,
    )
    report = prepared.workspace_path / "ClientWow.Report"  # type: ignore[attr-defined]
    before_hash = hash_tree(report)
    transaction_id = "b" * 32
    displaced = state / "swaps" / f"{transaction_id}.old"
    displaced.parent.mkdir(parents=True)
    report.replace(displaced)
    intent = state / "recovery" / f"{transaction_id}.json"
    intent.parent.mkdir(parents=True)
    intent.write_text(
        json.dumps(
            {
                "transaction_id": transaction_id,
                "status": "pending",
                "before_hash": before_hash,
            }
        ),
        encoding="utf-8",
    )
    if blocker == "reveal":
        (prepared.root / ".reveal-in-progress.json").write_text(  # type: ignore[attr-defined]
            "{}\n",
            encoding="utf-8",
        )
        expected = "reveal"
    else:
        monkeypatch.setattr(
            preparation,
            "assert_powerbi_desktop_closed",
            lambda: (_ for _ in ()).throw(RuntimeError("Desktop is running")),
        )
        expected = "Desktop"

    with pytest.raises((PreparationSafetyError, RuntimeError), match=expected):
        recover_interrupted_report_swap(
            frozen_repository,
            prepared.root,  # type: ignore[attr-defined]
            transaction_state_path=state,
            expected_bundle=bundle,
        )

    assert not report.exists()
    assert displaced.is_dir()
    assert json.loads(intent.read_text(encoding="utf-8"))["status"] == "pending"


@pytest.mark.parametrize("intent_state", ["missing", "tampered"])
def test_production_startup_missing_report_without_valid_pending_intent_fails_closed(
    frozen_repository: Path,
    tmp_path: Path,
    intent_state: str,
) -> None:
    prepared, bundle, state = _attested_demo_for_report_recovery(
        frozen_repository,
        tmp_path,
    )
    report = prepared.workspace_path / "ClientWow.Report"  # type: ignore[attr-defined]
    before_hash = hash_tree(report)
    transaction_id = "c" * 32
    displaced = state / "swaps" / f"{transaction_id}.old"
    displaced.parent.mkdir(parents=True)
    report.replace(displaced)
    if intent_state == "tampered":
        intent = state / "recovery" / f"{transaction_id}.json"
        intent.parent.mkdir(parents=True)
        intent.write_text(
            json.dumps(
                {
                    "transaction_id": "d" * 32,
                    "status": "pending",
                    "before_hash": before_hash,
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(PreparationSafetyError, match="pending.*intent"):
        recover_interrupted_report_swap(
            frozen_repository,
            prepared.root,  # type: ignore[attr-defined]
            transaction_state_path=state,
            expected_bundle=bundle,
        )

    assert not report.exists()
    assert displaced.is_dir()


def test_verify_cli_reports_ready_only_for_an_attested_demo(
    frozen_repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "demo")
    assert (
        main(
            [
                "verify",
                "--repository-root",
                str(frozen_repository),
                "--demo-root",
                str(prepared.root),
                "--require-attested",
            ]
        )
        == 2
    )
    assert '"status": "blocked"' in capsys.readouterr().err

    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(_attestation(frozen_repository, prepared.root)), encoding="utf-8")
    assert (
        main(
            [
                "attest",
                "--repository-root",
                str(frozen_repository),
                "--demo-root",
                str(prepared.root),
                "--attestation",
                str(path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "verify",
                "--repository-root",
                str(frozen_repository),
                "--demo-root",
                str(prepared.root),
                "--require-attested",
            ]
        )
        == 0
    )
    assert '"status": "verified"' in capsys.readouterr().out


def test_reset_moves_live_state_to_recoverable_timestamped_backup(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    demo_root = tmp_path / "demo"
    prepared = prepare_windows_demo(frozen_repository, demo_root)
    client_edit = prepared.workspace_path / "ClientWow.Report" / "client-edit.txt"
    client_edit.write_text("recoverable", encoding="utf-8")
    runtime_state = tmp_path / ".runtime"
    runtime_state.mkdir()
    (runtime_state / "jobs.sqlite3").write_text("state", encoding="utf-8")

    receipt = reset_windows_demo(
        demo_root,
        state_path=runtime_state,
        now=datetime(2026, 8, 30, 12, 34, 56, tzinfo=UTC),
    )

    assert receipt.backup_path.name == "20260830T123456Z"
    assert (receipt.backup_path / "workspace" / "ClientWow.Report" / "client-edit.txt").read_text(
        encoding="utf-8"
    ) == "recoverable"
    assert (receipt.backup_path / "runtime-state" / "jobs.sqlite3").read_text(
        encoding="utf-8"
    ) == "state"
    assert prepared.workspace_path.is_dir()
    assert not (prepared.workspace_path / "ClientWow.Report" / "client-edit.txt").exists()
    assert not runtime_state.exists()


def test_interrupted_committed_reset_clears_reveal_marker(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "reset-recovery-demo")
    changed = prepared.workspace_path / "ClientWow.Report" / "client-edit.txt"
    changed.write_text("recoverable", encoding="utf-8")
    before_hash = hash_tree(prepared.workspace_path / "ClientWow.Report")
    bundle = ProjectBundle.model_validate_json(prepared.bundle_path.read_text(encoding="utf-8"))
    baseline_hash = str(bundle.baseline_report_hash)
    backup = prepared.root / "backups" / "20260830T123456Z"
    backup.mkdir(parents=True)
    prepared.workspace_path.replace(backup / "workspace")
    shutil.copytree(prepared.baseline_path, prepared.workspace_path)
    (prepared.root / ".reset-recovery.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "status": "pending",
                "backup": backup.name,
                "before_report_hash": before_hash,
                "baseline_report_hash": baseline_hash,
                "state_path": None,
            }
        ),
        encoding="utf-8",
    )
    marker = prepared.root / ".reveal-in-progress.json"
    marker.write_text("{}\n", encoding="utf-8")

    receipt = reset_windows_demo(prepared.root)

    assert receipt.backup_path == backup
    assert not marker.exists()
    assert prepared.workspace_path.is_dir()
    assert not (prepared.workspace_path / "ClientWow.Report" / "client-edit.txt").exists()


def test_rejected_reset_does_not_move_the_live_workspace(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    demo_root = tmp_path / "demo"
    prepared = prepare_windows_demo(frozen_repository, demo_root)
    marker = prepared.workspace_path / "ClientWow.Report" / "still-live.txt"
    marker.write_text("untouched", encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="overlaps|app-owned"):
        reset_windows_demo(demo_root, state_path=prepared.baseline_path)

    assert marker.read_text(encoding="utf-8") == "untouched"
    assert not (demo_root / "backups").exists()


def test_reset_rejects_an_arbitrary_external_directory_as_runtime_state(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    demo_root = tmp_path / "demo"
    prepared = prepare_windows_demo(frozen_repository, demo_root)
    broad_directory = tmp_path / "Desktop"
    broad_directory.mkdir()
    marker = broad_directory / "must-stay.txt"
    marker.write_text("untouched", encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="app-owned .runtime"):
        reset_windows_demo(demo_root, state_path=broad_directory)

    assert marker.read_text(encoding="utf-8") == "untouched"
    assert prepared.workspace_path.is_dir()


def test_reset_rejects_any_corruption_in_the_immutable_baseline(
    frozen_repository: Path,
    tmp_path: Path,
) -> None:
    prepared = prepare_windows_demo(frozen_repository, tmp_path / "demo")
    live_marker = prepared.workspace_path / "ClientWow.Report" / "still-live.txt"
    live_marker.write_text("untouched", encoding="utf-8")
    (prepared.baseline_path / "unexpected.txt").write_text("corrupt", encoding="utf-8")

    with pytest.raises(PreparationSafetyError, match="baseline workspace hash"):
        reset_windows_demo(prepared.root)

    assert live_marker.read_text(encoding="utf-8") == "untouched"
    assert not (prepared.root / "backups").exists()


def test_reset_copy_failure_leaves_the_live_workspace_in_place(
    frozen_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo_root = tmp_path / "demo"
    prepared = prepare_windows_demo(frozen_repository, demo_root)
    marker = prepared.workspace_path / "ClientWow.Report" / "still-live.txt"
    marker.write_text("untouched", encoding="utf-8")
    original_copytree = preparation.shutil.copytree

    def fail_baseline_copy(
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> Path:
        if Path(source) == prepared.baseline_path:
            raise OSError("injected baseline copy failure")
        return original_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(preparation.shutil, "copytree", fail_baseline_copy)

    with pytest.raises(OSError, match="injected baseline copy failure"):
        reset_windows_demo(demo_root)

    assert marker.read_text(encoding="utf-8") == "untouched"
    assert not (demo_root / "backups").exists()


def test_windows_preparation_script_only_invokes_the_local_package() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "prepare-windows-demo.ps1").read_text(encoding="utf-8")

    assert '-m", "ai_powerbi_analyst.runtime.preparation' in script
    assert "Invoke-WebRequest" not in script
    assert "Install-" not in script
    assert "winget" not in script.casefold()
    assert "--attestation" not in script

    attest_script = (REPOSITORY_ROOT / "scripts" / "attest-windows-demo.ps1").read_text(
        encoding="utf-8"
    )
    assert "runtime.preparation attest" in attest_script
    assert "runtime.preparation verify" in attest_script
    assert "--require-attested" in attest_script
    assert "Invoke-WebRequest" not in attest_script
    assert "Install-" not in attest_script
    assert "winget" not in attest_script.casefold()
