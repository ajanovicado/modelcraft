from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_powerbi_analyst.contracts import AnalysisOperation, RouteIntent
from ai_powerbi_analyst.jobs.journal import JobStatus
from ai_powerbi_analyst.orchestration.conversation import ConversationState
from ai_powerbi_analyst.powerbi.transaction import hash_tree
from ai_powerbi_analyst.runtime import build_local_workspace, local_runtime_paths

PROJECT_ROOT = Path(__file__).parents[2]
BUILD_PROMPT = (
    "Build a three-page executive dashboard for sales and margin. Show what changed, "
    "what drove it, and next-quarter outlook. Include our approved 3% price scenario."
)
DRIVER_QUESTION = "Revenue is up, so why did margin fall in the West last quarter?"
SUPPORTED_EVIDENCE_QUESTION = "Which categories contributed most to net sales last quarter?"


def test_local_workspace_runs_grounded_build_analysis_evidence_and_undo(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = build_local_workspace(PROJECT_ROOT, runtime_root, powerbi_mode="mock")
    paths = local_runtime_paths(runtime_root)
    manifest = json.loads(
        (PROJECT_ROOT / "fixtures/data/manifest.json").read_text(encoding="utf-8")
    )

    assert paths.pbip_path.is_file()
    assert paths.report_path.is_dir()
    assert paths.semantic_model_path.is_dir()
    assert workspace.config.project_id == "sales-margin-demo"
    assert workspace.config.can_open_power_bi is False
    assert workspace.initial_plan is not None
    assert [page.key for page in workspace.initial_plan.pages] == [
        "executive_pulse",
        "margin_drivers",
        "outlook_decisions",
    ]
    governed_metric_ids = {
        "net_sales",
        "units",
        "gross_profit",
        "gross_margin_pct",
        "average_selling_price",
        "discount_rate",
        "unit_cost",
        "sales_growth",
    }
    governed_dimension_ids = {
        "month",
        "region",
        "product",
        "category",
        "segment",
        "channel",
    }
    assert {
        metric_id for page in workspace.initial_plan.pages for metric_id in page.metric_ids
    } <= governed_metric_ids
    assert {
        dimension_id for page in workspace.initial_plan.pages for dimension_id in page.dimension_ids
    } <= governed_dimension_ids

    state = ConversationState(project_id=workspace.config.project_id)
    build = workspace.orchestrator.review(BUILD_PROMPT, state)
    assert build.decision.intent is RouteIntent.BUILD
    assert build.requires_review is True
    assert build.plan is not None
    assert build.plan.base_report_hash == hash_tree(paths.report_path)

    baseline_hash = hash_tree(paths.report_path)
    applied = workspace.orchestrator.apply_plan(
        workspace.config.project_id,
        build.plan,
        idempotency_key="canonical-build",
    )
    report_hash = hash_tree(paths.report_path)
    restarted = build_local_workspace(PROJECT_ROOT, runtime_root, powerbi_mode="mock")
    duplicate = restarted.orchestrator.apply_plan(
        restarted.config.project_id,
        build.plan,
        idempotency_key="canonical-build",
    )
    assert applied.status is JobStatus.SUCCEEDED
    assert duplicate.id == applied.id
    assert report_hash != baseline_hash
    assert hash_tree(paths.report_path) == report_hash
    assert len(restarted.orchestrator.job_events(applied.id)) >= 6

    fresh_build = restarted.orchestrator.review(BUILD_PROMPT, build.state)
    assert fresh_build.plan is not None
    reapplied = restarted.orchestrator.apply_plan(
        restarted.config.project_id,
        fresh_build.plan,
        idempotency_key="canonical-build-noop",
    )
    pages_metadata = json.loads(
        (paths.report_path / "definition/pages/pages.json").read_text(encoding="utf-8")
    )
    assert reapplied.status is JobStatus.SUCCEEDED
    assert hash_tree(paths.report_path) == report_hash
    assert len(pages_metadata["pageOrder"]) == 3
    assert len(set(pages_metadata["pageOrder"])) == 3

    driver = restarted.orchestrator.review(DRIVER_QUESTION, build.state)
    assert driver.decision.intent is RouteIntent.ANALYZE
    assert driver.analysis_plan is not None
    assert driver.analysis_plan.operation is AnalysisOperation.DRIVER_BRIDGE
    assert driver.analysis_plan.metric_ids == ["gross_profit"]
    assert driver.analysis_plan.dimension_ids == []
    assert driver.analysis_plan.filters == {"region": "West"}
    assert driver.evidence is not None
    assert driver.evidence.data_hash == manifest["data_hash"]
    assert driver.evidence.catalog_version == "1.0.0"
    assert [row["driver"] for row in driver.evidence.rows] == [
        "Price",
        "Volume",
        "Mix",
        "Discount",
        "Cost",
    ]
    assert driver.evidence.rows[1]["contribution_value"] == pytest.approx(164_219.802004434)
    assert driver.evidence.rows[4]["contribution_value"] == pytest.approx(-136_577.56)
    assert all(row["current_period"] == "2025 Q4" for row in driver.evidence.rows)
    assert all(row["comparison_period"] == "2024 Q4" for row in driver.evidence.rows)

    supported = restarted.orchestrator.review(
        SUPPORTED_EVIDENCE_QUESTION,
        driver.state,
    )
    assert supported.analysis_plan is not None
    assert supported.analysis_plan.operation is AnalysisOperation.CONTRIBUTION
    assert supported.analysis_plan.metric_ids == ["net_sales"]
    assert supported.analysis_plan.dimension_ids == ["category"]
    assert supported.evidence is not None
    assert supported.evidence.data_hash == manifest["data_hash"]
    assert supported.evidence.rows[0]["contributor"] == "Infrastructure"
    assert supported.evidence.rows[0]["contribution"] == pytest.approx(282_029.8)
    assert supported.evidence.add_to_report_supported is False
    assert supported.evidence.report_visual_plan is None

    evidence_job = restarted.orchestrator.apply_evidence(
        restarted.config.project_id,
        driver.evidence,
        idempotency_key="west-driver-evidence",
        report_plan=fresh_build.plan,
        analysis_plan=driver.analysis_plan,
    )
    evidence_hash = hash_tree(paths.report_path)
    assert evidence_job.status is JobStatus.SUCCEEDED
    assert evidence_hash != report_hash
    written_visuals = list(paths.report_path.glob("definition/pages/*/visuals/*/visual.json"))
    assert any(
        "Driver Contribution — West" in path.read_text(encoding="utf-8") for path in written_visuals
    )

    restarted_after_evidence = build_local_workspace(PROJECT_ROOT, runtime_root)
    undone = restarted_after_evidence.orchestrator.undo(
        restarted_after_evidence.config.project_id,
        idempotency_key="undo-west-driver-evidence",
    )
    assert undone.status is JobStatus.SUCCEEDED
    assert hash_tree(paths.report_path) == report_hash


def test_local_workspace_explainer_uses_catalog_definition(tmp_path: Path) -> None:
    workspace = build_local_workspace(PROJECT_ROOT, tmp_path / "runtime")

    outcome = workspace.orchestrator.review(
        "Explain how gross margin is calculated.",
        ConversationState(project_id=workspace.config.project_id),
    )

    assert outcome.decision.intent is RouteIntent.EXPLAIN
    assert outcome.explanation is not None
    assert "Gross Profit divided by Net Sales" in outcome.explanation
    assert "'Measures'[Gross Margin %]" in outcome.explanation
    assert "DIVIDE([Gross Profit], [Net Sales])" in outcome.explanation
    assert "Exact stored DAX" in outcome.explanation


def test_real_powerbi_mode_fails_closed_until_windows_gate_is_proven(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="Windows"):
        build_local_workspace(PROJECT_ROOT, tmp_path / "runtime", powerbi_mode="real")
