from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ai_powerbi_analyst.contracts import (
    ColumnDataType,
    DashboardPagePlan,
    DashboardPlan,
    DslMeasureSpec,
    SemanticModelPlan,
    SemanticTablePlan,
    SourceManifest,
    VisualForecast,
    VisualPlan,
    VisualRoleBinding,
)
from ai_powerbi_analyst.data.onboarding import DataOnboardingService, UploadedSource
from ai_powerbi_analyst.powerbi.semantic_model import SemanticProjectBuilder
from ai_powerbi_analyst.powerbi.visual_forecast import visual_forecast_objects
from ai_powerbi_analyst.studio.contracts import DashboardVisualProposal

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _proposal(**changes: Any) -> DashboardVisualProposal:
    return DashboardVisualProposal.model_validate(
        {
            "id": "sales_trend",
            "recipe": "trend",
            "title": "Sales over time",
            "visual_type": "lineChart",
            "table_id": "orders",
            "measure_id": "total_sales",
            "category_column_id": "order_date",
            "x": 0,
            "y": 0,
            "width": 6,
            "height": 4,
            "forecast": {},
            **changes,
        }
    )


def test_forecast_contract_defaults_and_settings_are_shared() -> None:
    settings = VisualForecast()
    assert settings.model_dump() == {
        "forecast_length": 6,
        "confidence": 95,
        "seasonality": None,
        "ignore_last": 0,
    }
    assert _proposal().forecast == settings
    custom = VisualForecast(forecast_length=60, confidence=99, seasonality=120, ignore_last=500)
    visual = VisualPlan(
        id="trend",
        visual_type="lineChart",
        title="Trend",
        x=0,
        y=0,
        width=400,
        height=240,
        forecast=custom,
    )
    assert VisualPlan.model_validate(visual.model_dump()).forecast == custom
    assert _proposal(forecast=custom).forecast == custom


@pytest.mark.parametrize(
    "settings",
    [
        {"forecast_length": 0},
        {"forecast_length": 61},
        {"forecast_length": True},
        {"forecast_length": 1.5},
        {"confidence": 94},
        {"confidence": 100},
        {"seasonality": 0},
        {"seasonality": 121},
        {"ignore_last": -1},
        {"ignore_last": 501},
        {"algorithm": "local_fit"},
    ],
)
def test_forecast_contract_rejects_invalid_settings(settings: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VisualForecast.model_validate(settings)


@pytest.mark.parametrize("confidence", [75, 80, 85, 90, 95, 99])
def test_forecast_contract_accepts_supported_confidence(confidence: int) -> None:
    assert VisualForecast.model_validate({"confidence": confidence}).confidence == confidence


@pytest.mark.parametrize(
    "changes",
    [
        {"recipe": "category"},
        {"visual_type": "areaChart"},
        {"category_column_id": None},
        {"measure_id": None},
    ],
)
def test_forecast_proposal_requires_a_bound_line_trend(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="lineChart trend with a date category and measure"):
        _proposal(**changes)
    assert _proposal(forecast=None, **changes).forecast is None


@pytest.fixture
def report_inputs(tmp_path: Path) -> tuple[SourceManifest, DashboardPlan]:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="orders.csv",
                content=(
                    b"Order Date,Region,Sales\n"
                    b"2025-01-01,East,100\n2025-02-01,West,150\n2025-03-01,East,180\n"
                    b"2025-04-01,West,190\n2025-05-01,East,210\n2025-06-01,West,220\n"
                ),
            )
        ]
    )
    table = manifest.tables[0]

    def binding(role: str, field: str, *, measure: bool = False) -> VisualRoleBinding:
        return VisualRoleBinding(
            role=role,
            table_id=table.id,
            field_id=field,
            field_kind="measure" if measure else "column",
        )

    return manifest, DashboardPlan(
        prompt="Review sales over time",
        semantic_model=SemanticModelPlan(
            manifest_import_id=manifest.import_id,
            tables=[SemanticTablePlan(table_id=table.id, display_name="Reviewed Orders")],
            measures=[
                DslMeasureSpec(
                    id="total_sales",
                    name="Reviewed Sales",
                    operation="sum",
                    table_id=table.id,
                    column_id="sales",
                )
            ],
            create_calendar=True,
        ),
        pages=[
            DashboardPagePlan(
                id="overview",
                title="Sales overview",
                visuals=[
                    VisualPlan(
                        id="sales_trend",
                        visual_type="lineChart",
                        title="Sales over time",
                        bindings=[
                            binding("Category", "order_date"),
                            binding("Y", "total_sales", measure=True),
                        ],
                        x=0,
                        y=0,
                        width=400,
                        height=240,
                    ),
                    VisualPlan(
                        id="sales_region",
                        visual_type="clusteredBarChart",
                        title="Sales by region",
                        bindings=[
                            binding("Category", "region"),
                            binding("Y", "total_sales", measure=True),
                        ],
                        x=400,
                        y=0,
                        width=400,
                        height=240,
                    ),
                    VisualPlan(
                        id="sales_card",
                        visual_type="cardVisual",
                        title="Reviewed Sales",
                        bindings=[binding("Data", "total_sales", measure=True)],
                        x=800,
                        y=0,
                        width=400,
                        height=240,
                    ),
                ],
            )
        ],
    )


def _export(root: Path, manifest: SourceManifest, plan: DashboardPlan) -> dict[str, dict[str, Any]]:
    builder = SemanticProjectBuilder(
        root / "build", PROJECT_ROOT / "fixtures/powerbi/client-wow-template"
    )
    builder._write_report(root / "report", manifest, plan)
    return {
        value["name"]: value
        for path in (root / "report").rglob("visual.json")
        for value in [json.loads(path.read_text(encoding="utf-8"))]
    }


@pytest.mark.parametrize(
    "settings,expected",
    [
        (
            VisualForecast(),
            [
                ("Unit", "7L"),
                ("ForecastLength", "6L"),
                ("IgnoreLast", "0L"),
                ("ConfidenceLevel", "0.95D"),
            ],
        ),
        (
            VisualForecast(forecast_length=12, confidence=90, seasonality=12, ignore_last=2),
            [
                ("Unit", "7L"),
                ("ForecastLength", "12L"),
                ("IgnoreLast", "2L"),
                ("ConfidenceLevel", "0.9D"),
                ("MaxSeasonality", "12L"),
            ],
        ),
    ],
)
def test_native_forecast_export_preserves_query_and_other_charts(
    tmp_path: Path,
    report_inputs: tuple[SourceManifest, DashboardPlan],
    settings: VisualForecast,
    expected: list[tuple[str, str]],
) -> None:
    manifest, base = report_inputs
    target = base.pages[0].visuals[0].model_copy(update={"forecast": settings})
    updated = base.model_copy(
        update={
            "pages": [
                base.pages[0].model_copy(update={"visuals": [target, *base.pages[0].visuals[1:]]})
            ]
        }
    )
    baseline = _export(tmp_path / "baseline", manifest, base)
    emitted = _export(tmp_path / "forecast", manifest, updated)
    changed = [name for name, visual in emitted.items() if visual != baseline[name]]
    assert len(changed) == 1
    before, after = baseline[changed[0]]["visual"], emitted[changed[0]]["visual"]
    assert after["visualType"] == "lineChart"
    assert after["query"] == before["query"]
    assert after["visualContainerObjects"] == before["visualContainerObjects"]
    category = after["query"]["queryState"]["Category"]["projections"][0]
    assert category["queryRef"] == "Calendar.Month Start"
    query_ref = after["query"]["queryState"]["Y"]["projections"][0]["queryRef"]
    assert query_ref == "Reviewed Orders.Reviewed Sales"
    assert after["objects"] == {
        "forecast": [
            {
                "properties": {
                    "transform": {
                        "algorithm": "Forecast",
                        "parameters": [
                            {"Literal": {"Value": value}, "Name": name} for name, value in expected
                        ],
                    },
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "displayName": {"expr": {"Literal": {"Value": "'Forecast 1'"}}},
                },
                "selector": {"metadata": query_ref, "id": "1"},
            }
        ],
        "categoryAxis": [
            {"properties": {"axisType": {"expr": {"Literal": {"Value": "'Scalar'"}}}}}
        ],
    }


@pytest.mark.parametrize("invalid", ["chart", "axis", "values", "legend", "projection"])
def test_native_forecast_refuses_incompatible_bindings(
    report_inputs: tuple[SourceManifest, DashboardPlan],
    invalid: str,
) -> None:
    manifest, plan = report_inputs
    trend = plan.pages[0].visuals[0]
    updates: dict[str, object] = {"forecast": VisualForecast()}
    query_state: dict[str, object] = {
        "Y": {"projections": [{"queryRef": "Reviewed Orders.Reviewed Sales"}]}
    }
    if invalid == "chart":
        updates["visual_type"] = "areaChart"
    elif invalid == "axis":
        updates["bindings"] = [
            trend.bindings[0].model_copy(update={"field_id": "region"}),
            trend.bindings[1],
        ]
    elif invalid == "values":
        updates["bindings"] = [*trend.bindings, trend.bindings[1]]
    elif invalid == "legend":
        updates["bindings"] = [
            *trend.bindings,
            trend.bindings[0].model_copy(update={"role": "Series"}),
        ]
    elif invalid == "projection":
        query_state = {"Y": {"projections": [{}]}}
    with pytest.raises(ValueError, match="native forecast"):
        visual_forecast_objects(
            trend.model_copy(update=updates),
            query_state,
            {table.id: table for table in manifest.tables},
        )


def test_native_forecast_accepts_datetime_and_does_nothing_without_settings(
    report_inputs: tuple[SourceManifest, DashboardPlan],
) -> None:
    manifest, plan = report_inputs
    trend = plan.pages[0].visuals[0]
    assert visual_forecast_objects(trend, {}, {}) is None
    table = manifest.tables[0]
    table = table.model_copy(
        update={
            "columns": [
                column.model_copy(update={"data_type": ColumnDataType.DATETIME})
                if column.id == "order_date"
                else column
                for column in table.columns
            ]
        }
    )
    assert (
        visual_forecast_objects(
            trend.model_copy(update={"forecast": VisualForecast()}),
            {"Y": {"projections": [{"queryRef": "Reviewed Orders.Reviewed Sales"}]}},
            {table.id: table},
        )
        is not None
    )
