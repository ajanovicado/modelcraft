from __future__ import annotations

from pathlib import Path

import pytest

from ai_powerbi_analyst.data.onboarding import DataOnboardingService, UploadedSource
from ai_powerbi_analyst.powerbi import semantic_model as semantic_model_module
from ai_powerbi_analyst.powerbi.semantic_model import SemanticProjectBuilder
from ai_powerbi_analyst.preflight import ToolProbe
from ai_powerbi_analyst.workspace.planner import DashboardPlanningService

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def unavailable_external_validators(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calendar compilation unit tests use local validation without host downloads."""
    monkeypatch.setattr(
        semantic_model_module, "probe_pbi_cli", lambda: ToolProbe(name="pbi", available=False)
    )
    monkeypatch.setattr(
        semantic_model_module, "probe_tool", lambda name: ToolProbe(name=name, available=False)
    )


def test_builder_compiles_calendar_table_reference_and_date_relationship_without_external_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="sales.csv",
                content=(b"Order Date,Region,Sales\n2024-07-15,East,100\n2025-02-03,West,150\n"),
            )
        ]
    )
    plan = DashboardPlanningService(None).plan("Build a sales trend", manifest, [])
    assert plan.semantic_model.create_calendar
    plan = plan.model_copy(
        update={
            "approved": True,
            "semantic_model": plan.semantic_model.model_copy(update={"approved": True}),
        }
    )
    monkeypatch.setattr(
        semantic_model_module,
        "probe_pbi_cli",
        lambda: ToolProbe(name="pbi", available=False),
    )
    monkeypatch.setattr(
        semantic_model_module,
        "probe_tool",
        lambda name: ToolProbe(name=name, available=False),
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    pbip, receipt = builder.build("Calendar Integration", manifest, plan)

    assert receipt.passed
    assert receipt.issues == []
    assert "pbi-cli" not in receipt.validators
    assert "microsoft-pbir-validator" not in receipt.validators
    definition = next(pbip.parent.glob("*.SemanticModel/definition"))
    calendar_tmdl = (definition / "tables" / "Calendar.tmdl").read_text(encoding="utf-8")
    assert calendar_tmdl.startswith("table Calendar\n")
    assert "StartDate = #date(2024, 1, 1)" in calendar_tmdl
    assert "EndDate = #date(2025, 12, 31)" in calendar_tmdl
    assert "column 'Year Month'" in calendar_tmdl
    model_tmdl = (definition / "model.tmdl").read_text(encoding="utf-8")
    assert "\nref table sales\n" in model_tmdl
    assert "\nref table Calendar\n" in model_tmdl
    assert "\n\tref table" not in model_tmdl
    relationships_tmdl = (definition / "relationships.tmdl").read_text(encoding="utf-8")
    assert "fromColumn: sales.'Order Date'" in relationships_tmdl
    assert "toColumn: Calendar.Date" in relationships_tmdl
    assert "joinOnDateBehavior: datePartOnly" in relationships_tmdl
    assert "column 'Month Start'" in calendar_tmdl
    assert "Date.StartOfMonth([Date])" in calendar_tmdl

    # Desktop must draw what the preview shows: monthly trends, ranked categories.
    import json

    visuals = [
        json.loads(path.read_text(encoding="utf-8"))["visual"]
        for path in pbip.parent.glob("*.Report/definition/pages/*/visuals/*/visual.json")
    ]
    date_axes = [
        projection["field"]["Column"]
        for visual in visuals
        for projection in visual["query"]["queryState"].get("Category", {}).get("projections", [])
        if projection["field"].get("Column", {}).get("Property") in {"Order Date", "Month Start"}
    ]
    assert date_axes
    assert all(
        axis == {"Expression": {"SourceRef": {"Entity": "Calendar"}}, "Property": "Month Start"}
        for axis in date_axes
    )
    ranked = [
        visual
        for visual in visuals
        if visual["query"]["queryState"]
        .get("Category", {})
        .get("projections", [{}])[0]
        .get("field", {})
        .get("Column", {})
        .get("Property")
        == "Region"
    ]
    assert ranked
    for visual in ranked:
        order = visual["query"]["sortDefinition"]["sort"][0]
        assert order["direction"] == "Descending"
        assert "Measure" in order["field"]
    # A card titled with its measure name shows that name once, not twice.
    # A custom card title ("Primary measure") keeps the measure label under the value.
    cards = [visual for visual in visuals if visual["visualType"] == "cardVisual"]
    assert cards and all("objects" not in card for card in cards)


def test_related_date_dimension_trends_plot_months(tmp_path: Path) -> None:
    """A source Dates table gets a calculated Month Start; trends bind to it, not days."""
    import json

    from ai_powerbi_analyst.contracts import RelationshipCandidate

    days = "\n".join(f"2024-01-{day:02d}" for day in range(1, 32))
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="sales.csv",
                content=b"Order Date,Region,Sales\n2024-01-03,East,100\n2024-01-20,West,150\n",
            ),
            UploadedSource(name="dates.csv", content=("Date\n" + days + "\n").encode()),
        ]
    )
    sales = next(table for table in manifest.tables if table.name == "sales")
    dates = next(table for table in manifest.tables if table.name == "dates")
    relationship = RelationshipCandidate(
        id="sales_dates",
        from_table_id=sales.id,
        from_column_id="order_date",
        to_table_id=dates.id,
        to_column_id="date",
        cardinality="many_to_one",
        confidence=1,
        evidence=["Unique daily date dimension"],
        review_status="approved",
    )
    plan = DashboardPlanningService(None).plan("Build a sales trend", manifest, [relationship])
    plan = plan.model_copy(
        update={
            "approved": True,
            "semantic_model": plan.semantic_model.model_copy(
                update={"approved": True, "create_calendar": False}
            ),
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    pbip, receipt = builder.build("Date Dimension", manifest, plan)

    assert receipt.passed, receipt.issues
    dates_tmdl = next(pbip.parent.glob("*.SemanticModel/definition/tables/dates.tmdl")).read_text(
        encoding="utf-8"
    )
    assert "\tdataCategory: Time" in dates_tmdl
    assert "\tcolumn 'Month Start' = DATE(YEAR(dates[Date]), MONTH(dates[Date]), 1)" in dates_tmdl
    date_names = {"Order Date", "Date", "Month Start"}
    axes = [
        projection["field"]["Column"]
        for path in pbip.parent.glob("*.Report/definition/pages/*/visuals/*/visual.json")
        for projection in json.loads(path.read_text(encoding="utf-8"))["visual"]["query"][
            "queryState"
        ]
        .get("Category", {})
        .get("projections", [])
        if projection["field"].get("Column", {}).get("Property") in date_names
    ]
    assert axes
    assert all(
        axis == {"Expression": {"SourceRef": {"Entity": "dates"}}, "Property": "Month Start"}
        for axis in axes
    )


def test_card_titled_with_its_measure_shows_the_name_once() -> None:
    from ai_powerbi_analyst.contracts import VisualPlan, VisualRoleBinding
    from ai_powerbi_analyst.powerbi.semantic_model import _card_label_objects

    card = VisualPlan(
        id="kpi",
        visual_type="cardVisual",
        title="Total Sales",
        bindings=[
            VisualRoleBinding(
                role="Data", table_id="orders", field_id="sales", field_kind="measure"
            )
        ],
        x=0,
        y=0,
        width=300,
        height=120,
    )
    state = {
        "Data": {
            "projections": [
                {"queryRef": "Orders.Total Sales", "nativeQueryRef": "Total Sales", "field": {}}
            ]
        }
    }
    label = _card_label_objects(card, state)["label"][0]
    assert label["selector"] == {"metadata": "Orders.Total Sales"}
    assert label["properties"]["show"]["expr"]["Literal"]["Value"] == "false"
    assert _card_label_objects(card.model_copy(update={"title": "Revenue"}), state) is None
