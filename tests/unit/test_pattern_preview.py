"""CSV/draft patterns must evaluate the same tables and calendars the builder produces."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pandas as pd
import pytest

from ai_powerbi_analyst.powerbi.calendar_table import plan_calendar_table
from ai_powerbi_analyst.studio import pattern_preview
from ai_powerbi_analyst.studio.contracts import (
    AddDaxMeasureRequest,
    AdvancedDaxProposal,
    CreateWorkspaceRequest,
    DashboardDraft,
    DashboardPageProposal,
    DashboardVisualProposal,
    DatasetUnderstandingPlan,
    DaxEvaluateRequest,
    DaxPreviewContext,
    GovernedMeasureSpec,
    IngestRequest,
    MeasureProposal,
    RelationshipProposal,
    SemanticModelProposal,
    SemanticTableProposal,
)
from ai_powerbi_analyst.studio.dax_engine import (
    DaxEngineError,
    DaxEngineLimit,
    DaxUnsupported,
    Model,
)
from ai_powerbi_analyst.studio.dax_patterns import (
    BuiltPattern,
    MetricSelection,
    PatternSelection,
    build_pattern,
    catalog_from_model,
    select_business_pattern,
)
from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
from ai_powerbi_analyst.studio.inventory import inventory_folder
from ai_powerbi_analyst.studio.pattern_preview import (
    evaluate_pattern_model,
    preview_pattern,
    snapshot_pattern_model,
)
from ai_powerbi_analyst.studio.report_engine import DaxUnavailable, ReportModel
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def _inputs(tmp_path: Path, *, dates: bool = True, date_dimension: bool = False):
    source = tmp_path / "csv"
    source.mkdir()
    header = "OrderId,Region,SalesAmount,DiscountAmount"
    rows = ["A,East,20,4", "B,West,30,6", "C,East,50,10"]
    if dates:
        header += ",OrderDate,ShipDate"
        # Preserve non-midnight times to exercise the builder's datePartOnly joins.
        times = ["2023-12-31T16:30:00", "2024-01-01T12:34:00", "2024-03-02T18:00:00"]
        if date_dimension:
            times = [text.split("T")[0] for text in times]
        rows = [row + f",{day},2024-03-03T14:00:00" for row, day in zip(rows, times, strict=True)]
    (source / "orders.csv").write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    (source / "regions.csv").write_text("Region,Manager\nEast,Ada\nWest,Grace\n", encoding="utf-8")
    if date_dimension:
        days = pd.date_range("2023-01-01", "2024-12-31")
        (source / "dates.csv").write_text(
            "Date,Year\n" + "\n".join(f"{day:%Y-%m-%d},{day.year}" for day in days) + "\n",
            encoding="utf-8",
        )
    return StudioIngestionService(tmp_path / "state").ingest(
        "patterns", [item for item in inventory_folder(source) if item.selectable]
    )


@pytest.fixture
def inputs(tmp_path):
    return _inputs(tmp_path)


def _table(manifest, name):
    return next(table for table in manifest.tables if table.name == name)


def _column(table, name):
    return next(column for column in table.columns if column.name == name)


def _draft(snapshot, manifest, *, calendar: bool = False, names: dict[str, str] | None = None):
    orders = _table(manifest, "orders")
    total = GovernedMeasureSpec(
        id="total_sales",
        name="Total Sales",
        operation="sum",
        table_id=orders.id,
        column_id=_column(orders, "SalesAmount").id,
        format_string="#,#0.00",
    )
    now = datetime.now(UTC)
    return DashboardDraft(
        id="draft-pattern",
        workspace_id="patterns",
        snapshot_id=snapshot.id,
        prompt="Understand and calculate sales",
        understanding=DatasetUnderstandingPlan(summary="Reviewed CSV source tables."),
        semantic_model=SemanticModelProposal(
            tables=[
                SemanticTableProposal(
                    table_id=table.id,
                    role="fact" if table.name == "orders" else "dimension",
                    display_name=(names or {}).get(table.name, table.name),
                )
                for table in manifest.tables
            ],
            calendar_required=calendar,
        ),
        measure_proposal=MeasureProposal(measures=[total]),
        pages=[
            DashboardPageProposal(
                id="overview",
                title="Sales",
                purpose="Review sales",
                visuals=[
                    DashboardVisualProposal(
                        id="sales",
                        recipe="kpi",
                        title="Total Sales",
                        visual_type="cardVisual",
                        table_id=orders.id,
                        measure_id="total_sales",
                        x=0,
                        y=0,
                        width=3,
                        height=3,
                    )
                ],
            )
        ],
        base_hash="0" * 64,
        created_at=now,
        updated_at=now,
    )


def _relationship(
    manifest, *, date: bool = False, status: str = "approved", blockers=(), active=True
):
    many = _table(manifest, "orders")
    one = _table(manifest, "dates" if date else "regions")
    return RelationshipProposal(
        id="date_join" if date else "region_join",
        from_table_id=many.id,
        from_column_id=_column(many, "OrderDate" if date else "Region").id,
        to_table_id=one.id,
        to_column_id=_column(one, "Date" if date else "Region").id,
        cardinality="many_to_one",
        cross_filter_direction="single",
        is_active=active,
        confidence=1.0,
        evidence=["Reviewed key and matching values."],
        blockers=list(blockers),
        review_status=status,
    )


def _built(expression, *, kind="scalar", fmt=""):
    return BuiltPattern(expression, kind, fmt, (), ())


def test_csv_discount_share_returns_actual_twenty_percent(inputs):
    snapshot, manifest = inputs
    model = snapshot_pattern_model(snapshot, manifest, _draft(snapshot, manifest))
    built = select_business_pattern(
        "total DiscountAmount divided by Total Sales", catalog_from_model(model)
    )
    assert built is not None
    how, values = preview_pattern(model, replace(built, format_string="0.0%"))
    assert how == "local"
    assert "Value = 20.0%" in values
    assert "page slicer selections are not applied" in values


def test_preview_blank_is_a_successful_computation_not_a_fabricated_number(inputs):
    snapshot, manifest = inputs
    model = snapshot_pattern_model(snapshot, manifest, None)
    how, values = preview_pattern(model, _built("DIVIDE(20, 0)"))
    assert how == "local-blank"
    assert "Value = blank" in values


@pytest.mark.parametrize("fmt, expected", [("$#,0.00", "$0.50"), ("0.0%", "50.0%")])
def test_topn_preview_keeps_numeric_group_identity_and_formats_only_metric(fmt, expected):
    model = Model.from_frames(
        {"Sales": pd.DataFrame({"StoreKey": [1, 2, 1], "Amount": [0.1, 0.3, 0.4]})},
        measures={"Total Sales": "SUM('Sales'[Amount])"},
    )
    model.measures["total sales"].format_string = fmt
    catalog = catalog_from_model(model)
    base = next(item.index for item in catalog.fields if item.name == "Total Sales")
    group = next(item.index for item in catalog.fields if item.name == "StoreKey")
    built = build_pattern(
        PatternSelection(
            pattern="top_n",
            base=MetricSelection(field=base),
            group=group,
            scope="all",
            direction="descending",
            n=2,
        ),
        catalog,
    )
    how, reading = preview_pattern(model, built)
    assert how == "local" and built.format_string == fmt
    assert f"Sales[StoreKey] = 1, __PatternValue = {expected}" in reading
    assert "Sales[StoreKey] = 2, __PatternValue = " in reading
    result = evaluate_pattern_model(
        model,
        DaxEvaluateRequest(workspace_id="patterns", expression=built.expression),
    )
    assert result.rows == [
        {"StoreKey": 1, "__PatternValue": 0.5},
        {"StoreKey": 2, "__PatternValue": 0.3},
    ]


@pytest.mark.parametrize("fmt, expected", [("$#,0.00", "$50.00"), ("0.0%", "5000.0%")])
def test_snapshot_governed_measure_format_is_inherited_by_real_scalar_pattern(
    inputs,
    fmt,
    expected,
):
    snapshot, manifest = inputs
    draft = _draft(snapshot, manifest, names={"orders": "Sales Facts"})
    base = draft.measure_proposal.measures[0].model_copy(update={"format_string": fmt})
    draft = draft.model_copy(update={"measure_proposal": MeasureProposal(measures=[base])})
    model = snapshot_pattern_model(snapshot, manifest, draft)
    catalog = catalog_from_model(model)
    metric = next(item for item in catalog.fields if item.name == "Total Sales")
    entity = next(
        item for item in catalog.fields if item.name == "Region" and item.table == "Sales Facts"
    )
    built = build_pattern(
        PatternSelection(
            pattern="average_per",
            base=MetricSelection(field=metric.index),
            entity=entity.index,
        ),
        catalog,
    )
    assert model.measures["total sales"].format_string == metric.format_string == fmt
    assert built.format_string == fmt
    assert preview_pattern(model, built)[1].endswith(f"Value = {expected}.")
    assert evaluate_pattern_model(
        model,
        DaxEvaluateRequest(workspace_id="patterns", expression=built.expression),
    ).rows == [{"Value": 50}]


@pytest.mark.parametrize("fmt, expected", [("$#,0.00", "$10.00"), ("0.0%", "1000.0%")])
def test_snapshot_approved_advanced_format_is_inherited_by_real_scalar_pattern(
    inputs,
    fmt,
    expected,
):
    snapshot, manifest = inputs
    draft = _draft(snapshot, manifest, names={"orders": "Sales Facts"})
    advanced = AdvancedDaxProposal(
        id="discount_total",
        name="Discount Total",
        table_id=_table(manifest, "orders").id,
        expression="SUM('orders'[DiscountAmount])",
        format_string=fmt,
        assumptions=["Reviewed source discount total."],
        approved=True,
    )
    draft = draft.model_copy(
        update={
            "measure_proposal": MeasureProposal(
                measures=[*draft.measure_proposal.measures, advanced]
            ),
        }
    )
    model = snapshot_pattern_model(snapshot, manifest, draft)
    catalog = catalog_from_model(model)
    metric = next(item for item in catalog.fields if item.name == "Discount Total")
    entity = next(
        item for item in catalog.fields if item.name == "Region" and item.table == "Sales Facts"
    )
    built = build_pattern(
        PatternSelection(
            pattern="average_per",
            base=MetricSelection(field=metric.index),
            entity=entity.index,
        ),
        catalog,
    )
    assert model.measures["discount total"].format_string == metric.format_string == fmt
    assert built.format_string == fmt
    assert preview_pattern(model, built)[1].endswith(f"Value = {expected}.")


@pytest.mark.parametrize("fmt, expected", [("$#,0.00", "$33.33"), ("0.0%", "3333.3%")])
def test_generated_csv_scalar_save_retains_inherited_currency_or_percent_format(
    tmp_path,
    fmt,
    expected,
):
    _inputs(tmp_path)
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "service-state")

    class NoModel:
        def generate(self, *_, **__):
            pytest.fail("A closed pattern must not make a live model call.")

    service.model = NoModel()
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Format inheritance",
            selected_folder=str(tmp_path / "csv"),
            idempotency_key="format-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(tmp_path / "csv" / "orders.csv")],
            idempotency_key="format-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    manifest = service.ingestion.load_manifest(snapshot)
    draft = _draft(snapshot, manifest).model_copy(update={"workspace_id": workspace.id})
    base = draft.measure_proposal.measures[0].model_copy(update={"format_string": fmt})
    draft = draft.model_copy(update={"measure_proposal": MeasureProposal(measures=[base])})
    draft = draft.model_copy(update={"base_hash": service.store.content_hash(draft)})
    service.store.put(
        "draft",
        draft,
        workspace_id=workspace.id,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="format-draft",
        mutation_payload=draft,
    )
    current = service.workspace(workspace.id)
    active = current.model_copy(update={"active_draft_id": draft.id})
    active = active.model_copy(update={"base_hash": service.store.content_hash(active)})
    service.store.put(
        "workspace",
        active,
        workspace_id=workspace.id,
        expected_base_hash=current.base_hash,
        idempotency_key="format-active",
        mutation_payload={"draft": draft.id},
    )
    catalog = catalog_from_model(service._business_pattern_model(workspace.id))
    metric = next(item.index for item in catalog.fields if item.name == "Total Sales")
    entity = next(item.index for item in catalog.fields if item.name == "OrderId")

    class ClosedSelector:
        calls = 0

        def generate(self, _system, user_prompt, response_model, **_):
            assert issubclass(response_model, PatternSelection)
            assert user_prompt.startswith("Request: Average Total Sales per OrderId\nFields:")
            assert f"{metric} measure " in user_prompt and f"{entity} column " in user_prompt
            self.calls += 1
            return response_model(
                pattern="average_per",
                base=MetricSelection(field=metric),
                entity=entity,
            )

    selector = ClosedSelector()
    service.model = selector
    expression, how, reading = service.business_pattern_formula(
        workspace.id,
        "Average Total Sales per OrderId",
    )
    assert selector.calls == 1
    assert how == "local" and f"Value = {expected}" in reading
    result = service.evaluate_dax(
        DaxEvaluateRequest(
            workspace_id=workspace.id,
            expression=expression,
        )
    )
    assert result.rows[0]["Value"] == pytest.approx(100 / 3)
    saved = service.add_dax_measure(
        workspace.id,
        AddDaxMeasureRequest(
            workspace_id=workspace.id,
            name="Sales per Order",
            expression=expression,
            expected_base_hash=draft.base_hash,
            idempotency_key="format-save",
        ),
    )
    proposal = saved.measure_proposal.measures[-1]
    assert proposal.format_string == fmt and proposal.dependencies == ["total_sales"]
    assert proposal.approved is False and saved.semantic_model.approved is False


def test_generated_calendar_matches_builder_bounds_and_date_part_join(inputs):
    snapshot, manifest = inputs
    draft = _draft(snapshot, manifest, calendar=True)
    paths = [Path(item.normalized_parquet_path) for item in snapshot.tables]
    before = {path: path.read_bytes() for path in paths}
    model = snapshot_pattern_model(snapshot, manifest, draft)
    plan = plan_calendar_table(manifest)
    assert plan is not None
    calendar = model.tables[plan.table_name.casefold()]
    assert calendar.rows == (plan.end_date - plan.start_date).days + 1
    assert str(calendar.column("Date").value(0))[:10] == str(plan.start_date)
    assert str(calendar.column("Date").value(calendar.rows - 1))[:10] == str(plan.end_date)
    how, values = preview_pattern(
        model, _built("CALCULATE([Total Sales], 'Calendar'[Date] = DATE(2024,3,2))")
    )
    assert how == "local" and "Value = 50" in values
    assert {path: path.read_bytes() for path in paths} == before
    # Only the builder's selected OrderDate changes to a date-part join; ShipDate remains itself.
    orders = model.tables["orders"]
    assert all(orders.column("OrderDate").value(row).hour == 0 for row in range(orders.rows))
    assert all(orders.column("ShipDate").value(row).hour == 14 for row in range(orders.rows))


def test_calendar_exposes_same_builder_columns_for_approved_advanced_dax(inputs):
    snapshot, manifest = inputs
    model = snapshot_pattern_model(snapshot, manifest, _draft(snapshot, manifest, calendar=True))
    assert set(model.tables["calendar"].columns) == {
        "date",
        "year",
        "month number",
        "month",
        "quarter",
        "year month",
        "month start",
    }
    how, values = preview_pattern(
        model, _built("CALCULATE([Total Sales], 'Calendar'[Year] = 2024)")
    )
    assert how == "local" and "Value = 80" in values


def test_semantic_display_names_match_compiled_measure_references(inputs):
    snapshot, manifest = inputs
    draft = _draft(snapshot, manifest, names={"orders": "Sales Facts", "regions": "Territories"})
    model = snapshot_pattern_model(snapshot, manifest, draft)
    assert set(model.tables) == {"sales facts", "territories"}
    assert model.measures["total sales"].text == "SUM('Sales Facts'[SalesAmount])"
    assert preview_pattern(model, _built("[Total Sales]"))[1].endswith("Value = 100.")


def test_generated_calendar_avoids_semantic_table_name_collision(inputs):
    snapshot, manifest = inputs
    draft = _draft(snapshot, manifest, calendar=True, names={"orders": "Calendar"})
    model = snapshot_pattern_model(snapshot, manifest, draft)
    assert model.tables["calendar"].rows == 3
    assert model.tables["calendar 2"].rows == 731
    assert preview_pattern(
        model, _built("CALCULATE([Total Sales], 'Calendar 2'[Date] = DATE(2024,3,2))")
    )[1].endswith("Value = 50.")


@pytest.mark.parametrize(
    "status, blockers, active",
    [
        ("pending", (), True),
        ("rejected", (), True),
        ("approved", ("Review duplicate keys",), True),
        ("approved", (), False),
    ],
)
def test_pending_blocked_or_inactive_relationship_does_not_become_a_preview_fact(
    inputs, status, blockers, active
):
    snapshot, manifest = inputs
    draft = _draft(snapshot, manifest)
    join = _relationship(manifest, status=status, blockers=blockers, active=active)
    draft = draft.model_copy(
        update={"semantic_model": draft.semantic_model.model_copy(update={"relationships": [join]})}
    )
    model = snapshot_pattern_model(snapshot, manifest, draft)
    with pytest.raises(DaxUnsupported, match="No active many-to-one relationship"):
        preview_pattern(model, _built("CALCULATE([Total Sales], 'regions'[Manager] = \"Ada\")"))


def test_approved_join_groups_actual_values_using_semantic_names(inputs):
    snapshot, manifest = inputs
    draft = _draft(snapshot, manifest, names={"orders": "Sales Facts", "regions": "Territories"})
    draft = draft.model_copy(
        update={
            "semantic_model": draft.semantic_model.model_copy(
                update={"relationships": [_relationship(manifest)]}
            )
        }
    )
    model = snapshot_pattern_model(snapshot, manifest, draft)
    how, values = preview_pattern(
        model, _built("CALCULATE([Total Sales], 'Territories'[Manager] = \"Ada\")")
    )
    assert how == "local" and "Value = 70" in values


def test_existing_reviewed_date_dimension_is_used_by_governed_time_formula(tmp_path):
    snapshot, manifest = _inputs(tmp_path, date_dimension=True)
    draft = _draft(snapshot, manifest, names={"dates": "Dates Dimension"})
    orders = _table(manifest, "orders")
    ytd = GovernedMeasureSpec(
        id="ytd_sales",
        name="YTD Sales",
        operation="period_to_date",
        table_id=orders.id,
        base_measure_id="total_sales",
        date_column_id=_column(orders, "OrderDate").id,
        period="year",
    )
    draft = draft.model_copy(
        update={
            "semantic_model": draft.semantic_model.model_copy(
                update={"relationships": [_relationship(manifest, date=True)]}
            ),
            "measure_proposal": MeasureProposal(measures=[*draft.measure_proposal.measures, ytd]),
        }
    )
    model = snapshot_pattern_model(snapshot, manifest, draft)
    assert model.measures["ytd sales"].text == "TOTALYTD([Total Sales], 'Dates Dimension'[Date])"
    how, values = preview_pattern(
        model, _built("CALCULATE([YTD Sales], 'Dates Dimension'[Year] = 2024)")
    )
    assert how == "local" and "Value = 80" in values


@pytest.mark.parametrize("approved", [False, True])
def test_only_approved_advanced_dax_enters_preview_and_uses_builder_table_remapping(
    inputs, approved
):
    snapshot, manifest = inputs
    draft = _draft(snapshot, manifest, names={"orders": "Sales Facts"})
    advanced = AdvancedDaxProposal(
        id="discount_total",
        name="Discount Total",
        table_id=_table(manifest, "orders").id,
        expression="SUM('orders'[DiscountAmount])",
        dependencies=[],
        format_string="#,#0.00",
        assumptions=["Reviewed discount amount aggregate."],
        approved=approved,
    )
    draft = draft.model_copy(
        update={
            "measure_proposal": MeasureProposal(
                measures=[*draft.measure_proposal.measures, advanced]
            )
        }
    )
    model = snapshot_pattern_model(snapshot, manifest, draft)
    if approved:
        assert model.measures["discount total"].text == "SUM('Sales Facts'[DiscountAmount])"
        assert preview_pattern(model, _built("[Discount Total]"))[1].endswith("Value = 20.")
    else:
        assert "discount total" not in model.measures
        with pytest.raises(DaxEngineError):
            preview_pattern(model, _built("[Discount Total]"))


def test_calendar_request_without_date_fields_has_clear_preview_refusal(tmp_path):
    snapshot, manifest = _inputs(tmp_path, dates=False)
    with pytest.raises(DaxUnsupported, match="date"):
        snapshot_pattern_model(snapshot, manifest, _draft(snapshot, manifest, calendar=True))


def test_snapshot_row_limit_is_enforced_before_parquet_loading(inputs, monkeypatch):
    snapshot, manifest = inputs
    monkeypatch.setattr(pattern_preview, "MAX_ROWS", 2)
    with pytest.raises(DaxEngineLimit, match="row limit"):
        snapshot_pattern_model(snapshot, manifest, None)


def test_preview_execution_limit_is_not_reported_as_a_success(inputs, monkeypatch):
    snapshot, manifest = inputs
    model = snapshot_pattern_model(snapshot, manifest, None)
    real_engine = pattern_preview.LocalReportEngine
    monkeypatch.setattr(
        pattern_preview, "LocalReportEngine", lambda model, **_: real_engine(model, time_budget=-1)
    )
    # An already-expired deadline avoids the coarse Windows clock returning the
    # same tick during a short zero-budget query. The real engine still executes.
    with pytest.raises(DaxEngineLimit):
        preview_pattern(model, _built("SUMX(GENERATESERIES(1, 1000), [Value] * [Value])"))


def test_preview_unavailable_measure_preserves_engine_refusal(inputs):
    snapshot, manifest = inputs
    model = snapshot_pattern_model(snapshot, manifest, _draft(snapshot, manifest))
    unavailable = ReportModel(
        model.tables.values(),
        model.relationships,
        model.measures.values(),
        unavailable_measures={"total sales": "Imported model rows are stale."},
    )
    with pytest.raises(DaxUnavailable, match="stale"):
        preview_pattern(unavailable, _built("[Total Sales]"))


def test_pattern_run_result_limit_reports_actual_truncation(inputs):
    snapshot, manifest = inputs
    model = snapshot_pattern_model(snapshot, manifest, None)
    request = DaxEvaluateRequest(
        workspace_id="patterns", expression="EVALUATE GENERATESERIES(1, 3)", top_n=2
    )
    result = evaluate_pattern_model(model, request)
    assert result.columns == ["Value"]
    assert result.rows == [{"Value": 1}, {"Value": 2}]
    assert result.row_count == 2 and result.truncated is True
    complete = evaluate_pattern_model(model, request.model_copy(update={"top_n": 3}))
    assert complete.rows[-1] == {"Value": 3}
    assert complete.row_count == 3 and complete.truncated is False


def test_pattern_run_blank_and_grouped_preview_limits(inputs):
    snapshot, manifest = inputs
    model = snapshot_pattern_model(snapshot, manifest, None)
    request = DaxEvaluateRequest(workspace_id="patterns", expression="DIVIDE(20, 0)")
    assert evaluate_pattern_model(model, request).rows == [{"Value": None}]
    with pytest.raises(ValueError, match="grouped preview"):
        evaluate_pattern_model(
            model,
            request.model_copy(update={"preview": DaxPreviewContext(column_id="region")}),
        )


def test_pattern_run_rejects_unavailable_and_unrelated_context(inputs):
    snapshot, manifest = inputs
    model = snapshot_pattern_model(snapshot, manifest, _draft(snapshot, manifest))
    unavailable = ReportModel(
        model.tables.values(),
        model.relationships,
        model.measures.values(),
        unavailable_measures={"total sales": "Rows belong to an older reviewed model."},
    )
    request = DaxEvaluateRequest(workspace_id="patterns", expression="[Total Sales]")
    with pytest.raises(DaxUnavailable, match="older"):
        evaluate_pattern_model(unavailable, request)
    with pytest.raises(DaxUnsupported, match="No active many-to-one relationship"):
        evaluate_pattern_model(
            model,
            request.model_copy(
                update={"expression": "CALCULATE([Total Sales], 'regions'[Manager] = \"Ada\")"}
            ),
        )


def test_pattern_run_enforces_actual_model_row_and_result_column_limits(inputs, monkeypatch):
    snapshot, manifest = inputs
    model = snapshot_pattern_model(snapshot, manifest, None)
    request = DaxEvaluateRequest(workspace_id="patterns", expression="1")
    with monkeypatch.context() as scoped:
        scoped.setattr(pattern_preview, "MAX_ROWS", 2)
        with pytest.raises(DaxEngineLimit, match="row limit"):
            evaluate_pattern_model(model, request)
    pairs = ", ".join(f'"Column{index}", {index}' for index in range(41))
    with pytest.raises(ValueError, match="40 columns"):
        evaluate_pattern_model(
            model, request.model_copy(update={"expression": f"EVALUATE ROW({pairs})"})
        )


def test_pattern_run_preserves_actual_engine_execution_deadline(inputs, monkeypatch):
    snapshot, manifest = inputs
    model = snapshot_pattern_model(snapshot, manifest, None)
    real_engine = pattern_preview.LocalReportEngine
    monkeypatch.setattr(
        pattern_preview, "LocalReportEngine", lambda model, **_: real_engine(model, time_budget=-1)
    )
    with pytest.raises(DaxEngineLimit):
        evaluate_pattern_model(
            model,
            DaxEvaluateRequest(
                workspace_id="patterns",
                expression="SUMX(GENERATESERIES(1, 1000), [Value] * [Value])",
            ),
        )


def _service(snapshot, manifest, draft):
    service = StudioService.__new__(StudioService)
    service.workspace = lambda _: SimpleNamespace(
        id="patterns",
        active_snapshot_id=snapshot.id,
        active_draft_id=draft.id,
        imported_report_id=None,
    )
    service.snapshot = lambda _: snapshot
    service.draft = lambda _: draft
    service.ingestion = SimpleNamespace(load_manifest=lambda _: manifest)

    def stored(kind, identity, _model):
        assert kind == "draft" and identity == draft.id
        return draft

    service.store = SimpleNamespace(get=stored)

    class NoModel:
        def generate(self, *_, **__):
            pytest.fail("Explicit ratio must compile without a local model call.")

    service.model = NoModel()
    return service


def test_service_business_pattern_formula_returns_verified_csv_values(inputs):
    snapshot, manifest = inputs
    service = _service(snapshot, manifest, _draft(snapshot, manifest))
    expression, how, values = service.business_pattern_formula(
        "patterns", "total DiscountAmount divided by Total Sales"
    )
    assert expression == "DIVIDE(SUM('orders'[DiscountAmount]), [Total Sales])"
    assert how == "local" and "Value = 0.2" in values


def test_service_cancellation_skips_selection_and_evaluation(inputs):
    snapshot, manifest = inputs
    service = _service(snapshot, manifest, _draft(snapshot, manifest))
    cancelled = Event()
    cancelled.set()
    assert (
        service.business_pattern_formula(
            "patterns", "total DiscountAmount divided by Total Sales", cancelled
        )
        is None
    )


def test_service_failed_preview_returns_clarification_without_formula_or_fallback(
    inputs, monkeypatch
):
    snapshot, manifest = inputs
    service = _service(snapshot, manifest, _draft(snapshot, manifest))

    def unavailable(*_):
        raise DaxUnavailable("Rows are unavailable.")

    monkeypatch.setattr("ai_powerbi_analyst.studio.service.preview_pattern", unavailable)
    expression, how, values = service.business_pattern_formula(
        "patterns", "total DiscountAmount divided by Total Sales"
    )
    assert expression == "" and how == "clarify"
    assert "Rows are unavailable" in values and "No formula or report change was applied" in values
