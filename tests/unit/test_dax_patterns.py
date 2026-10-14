"""Business formulas with unequal operands, real filter context and calendar boundaries."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest
from pydantic import ValidationError

from ai_powerbi_analyst.studio import report_engine
from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest
from ai_powerbi_analyst.studio.dax_engine import Model
from ai_powerbi_analyst.studio.dax_patterns import (
    MetricSelection,
    PatternCatalog,
    PatternError,
    PatternField,
    PatternSelection,
    build_pattern,
    catalog_from_context,
    catalog_from_model,
    parse_business_pattern,
    pattern_prompt,
    resolve_field,
    select_business_pattern,
)
from ai_powerbi_analyst.studio.edit_context import EditContext, MeasureEntry


@pytest.fixture
def model() -> Model:
    return Model.from_frames(
        {
            "FactSales": pd.DataFrame(
                {
                    "SalesAmount": [10.0, 20.0, 30.0, 30.0, 10.0],
                    "DiscountAmount": [2.0, 4.0, 6.0, 6.0, 2.0],
                    "CustomerId": [1, 1, 2, 3, 4],
                    "GroupId": [1, 1, 2, 3, 4],
                    "Zero": [0.0] * 5,
                }
            ),
            "Groups": pd.DataFrame(
                {
                    "GroupId": [1, 2, 3, 4],
                    "Name": ["A", "B", "C", "D"],
                    "Region": ["West", "West", "East", "East"],
                }
            ),
        },
        [("FactSales", "GroupId", "Groups", "GroupId")],
        {"Total Sales": "SUM('FactSales'[SalesAmount])", "Target": "125"},
    )


@pytest.fixture
def time_model() -> Model:
    dates = pd.date_range("2023-01-01", "2024-03-31")
    return Model.from_frames(
        {
            "FactSales": pd.DataFrame(
                {
                    "OrderDate": pd.to_datetime(
                        [
                            "2023-01-01",
                            "2023-03-01",
                            "2024-01-01",
                            "2024-02-01",
                            "2024-03-01",
                            "2024-03-02",
                        ]
                    ),
                    "SalesAmount": [10.0, 30.0, 20.0, 40.0, 60.0, 80.0],
                }
            ),
            "Calendar": pd.DataFrame({"Date": dates, "Year": dates.year}),
        },
        [("FactSales", "OrderDate", "Calendar", "Date")],
        {"Total Sales": "SUM('FactSales'[SalesAmount])"},
    )


def _field(catalog: PatternCatalog, name: str, table: str = "") -> int:
    return next(
        item.index
        for item in catalog.fields
        if item.name == name and (not table or item.table == table)
    )


def _metric(
    catalog: PatternCatalog, name: str = "Total Sales", aggregation: str | None = None
) -> MetricSelection:
    return MetricSelection(field=_field(catalog, name), aggregation=aggregation)


def _evaluate(monkeypatch: pytest.MonkeyPatch, model: Model, expression: str):
    # Use the actual report_engine entry point, including its availability/relationship
    # guards and truthful result receipt; only file-backed model loading is replaced.
    monkeypatch.setattr(report_engine, "report_model", lambda *_: model)
    return report_engine.evaluate(
        None, None, DaxEvaluateRequest(workspace_id="patterns", expression=expression)
    )


def _value(monkeypatch: pytest.MonkeyPatch, model: Model, expression: str):
    return _evaluate(monkeypatch, model, expression).rows[0]["Value"]


def _at(expression: str, day: str) -> str:
    year, month, date = (int(part) for part in day.split("-"))
    return f"CALCULATE({expression}, 'Calendar'[Date] = DATE({year}, {month}, {date}))"


def test_discount_share_names_and_computes_twenty_divided_by_one_hundred(model, monkeypatch):
    catalog = catalog_from_model(model)
    goal = (
        "Create a measure called QA Discount Share that is total DiscountAmount "
        "divided by Total Sales"
    )
    built = select_business_pattern(goal, catalog)
    assert built is not None
    assert built.expression == "DIVIDE(SUM('FactSales'[DiscountAmount]), [Total Sales])"
    result = _evaluate(monkeypatch, model, built.expression)
    assert result.rows == [{"Value": 0.2}]
    assert "Page slicer selections are not applied" in result.plan
    assert all("evaluated" not in note.casefold() for note in built.notes)


def test_model_selection_cannot_exchange_named_operands_or_aggregation(model):
    catalog = catalog_from_model(model)
    goal = "total DiscountAmount divided by Total Sales"
    correct = parse_business_pattern(goal, catalog)
    assert correct is not None
    for wrong in (
        correct.model_copy(update={"numerator": correct.denominator}),
        correct.model_copy(
            update={
                "numerator": MetricSelection(field=correct.numerator.field, aggregation="average")
            }
        ),
        correct.model_copy(
            update={"numerator": correct.denominator, "denominator": correct.numerator}
        ),
    ):
        with pytest.raises(PatternError, match="numerator"):
            build_pattern(wrong, catalog, goal=goal)


def test_constant_ratio_rejected_without_business_grounding(model):
    catalog = catalog_from_model(model)
    metric = _metric(catalog)
    with pytest.raises(PatternError, match="constant"):
        build_pattern(
            PatternSelection(pattern="ratio", numerator=metric, denominator=metric), catalog
        )


@pytest.mark.parametrize("numerator", ["DiscountAmount", "SalesAmount"])
def test_ratio_zero_denominator_returns_blank(model, monkeypatch, numerator):
    catalog = catalog_from_model(model)
    selection = PatternSelection(
        pattern="ratio",
        numerator=_metric(catalog, numerator, "sum"),
        denominator=_metric(catalog, "Zero", "sum"),
    )
    assert _value(monkeypatch, model, build_pattern(selection, catalog).expression) is None


def test_distinct_count_and_average_per_customer_differ_from_transaction_count(model, monkeypatch):
    catalog = catalog_from_model(model)
    entity = _field(catalog, "CustomerId")
    count = build_pattern(PatternSelection(pattern="distinct_count", entity=entity), catalog)
    average = build_pattern(
        PatternSelection(pattern="average_per", base=_metric(catalog), entity=entity), catalog
    )
    assert _value(monkeypatch, model, count.expression) == 4
    assert _value(monkeypatch, model, average.expression) == 25


@pytest.mark.parametrize(
    "comparison, expected", [("variance", -25), ("variance_percent", -0.2), ("attainment", 0.8)]
)
def test_actual_target_options_are_different_calculations(model, monkeypatch, comparison, expected):
    catalog = catalog_from_model(model)
    built = build_pattern(
        PatternSelection(
            pattern="target_actual",
            actual=_metric(catalog),
            target=_metric(catalog, "Target"),
            comparison=comparison,
        ),
        catalog,
    )
    assert _value(monkeypatch, model, built.expression) == expected


@pytest.mark.parametrize("scope, expected", [("all", 0.3), ("selected", 0.5)])
def test_share_total_distinguishes_all_groups_from_selected_groups(
    model, monkeypatch, scope, expected
):
    catalog = catalog_from_model(model)
    built = build_pattern(
        PatternSelection(
            pattern="share_total",
            base=_metric(catalog),
            group=_field(catalog, "Name", "Groups"),
            scope=scope,
        ),
        catalog,
    )
    query = (
        "EVALUATE CALCULATETABLE(SUMMARIZECOLUMNS('Groups'[Name], "
        f'"Share", {built.expression}), '
        "'Groups'[Name] IN {\"A\", \"B\"}) ORDER BY 'Groups'[Name]"
    )
    assert _evaluate(monkeypatch, model, query).rows == [
        {"Name": "A", "Share": expected},
        {"Name": "B", "Share": expected},
    ]


def test_share_clears_only_group_and_preserves_region_filter(model, monkeypatch):
    catalog = catalog_from_model(model)
    built = build_pattern(
        PatternSelection(
            pattern="share_total",
            base=_metric(catalog),
            group=_field(catalog, "Name", "Groups"),
            scope="all",
        ),
        catalog,
    )
    assert (
        _value(
            monkeypatch,
            model,
            f"CALCULATE({built.expression}, 'Groups'[Name] = \"A\", 'Groups'[Region] = \"West\")",
        )
        == 0.5
    )


@pytest.mark.parametrize("ties, expected", [("dense", 2), ("skip", 4)])
def test_rank_ties_use_context_transition_for_column_aggregates(model, monkeypatch, ties, expected):
    catalog = catalog_from_model(model)
    built = build_pattern(
        PatternSelection(
            pattern="rank",
            base=_metric(catalog, "SalesAmount", "sum"),
            group=_field(catalog, "Name", "Groups"),
            scope="all",
            direction="descending",
            ties=ties,
        ),
        catalog,
    )
    assert (
        _value(monkeypatch, model, f"CALCULATE({built.expression}, 'Groups'[Name] = \"D\")")
        == expected
    )


def test_top_n_is_a_query_and_keeps_boundary_ties(model, monkeypatch):
    catalog = catalog_from_model(model)
    built = build_pattern(
        PatternSelection(
            pattern="top_n",
            base=_metric(catalog),
            group=_field(catalog, "Name", "Groups"),
            scope="all",
            direction="descending",
            n=1,
        ),
        catalog,
    )
    assert built.kind == "query"
    rows = _evaluate(monkeypatch, model, built.expression).rows
    assert {row["Name"] for row in rows} == {"A", "B", "C"}
    assert {row["__PatternValue"] for row in rows} == {30}


@pytest.mark.parametrize("period, expected", [("month", 140), ("quarter", 200), ("year", 200)])
def test_to_date_resets_at_calendar_period_boundaries(time_model, monkeypatch, period, expected):
    catalog = catalog_from_model(time_model)
    built = build_pattern(
        PatternSelection(
            pattern="period_to_date",
            base=_metric(catalog),
            date=_field(catalog, "Date", "Calendar"),
            period=period,
        ),
        catalog,
    )
    assert _value(monkeypatch, time_model, _at(built.expression, "2024-03-02")) == expected
    assert _value(monkeypatch, time_model, _at(built.expression, "2024-01-01")) == 20


def test_yoy_percent_change_uses_previous_calendar_year_and_first_year_blank(
    time_model, monkeypatch
):
    catalog = catalog_from_model(time_model)
    built = build_pattern(
        PatternSelection(
            pattern="percent_change",
            base=_metric(catalog),
            date=_field(catalog, "Date", "Calendar"),
            period="year",
        ),
        catalog,
    )
    assert built.format_string == "0.0%"
    assert _value(monkeypatch, time_model, _at(built.expression, "2024-03-01")) == 1
    assert _value(monkeypatch, time_model, _at(built.expression, "2023-03-01")) is None


@pytest.mark.parametrize("mode, expected", [("calendar", 40), ("shifted", None)])
def test_previous_month_complete_period_differs_from_shifted_day(
    time_model, monkeypatch, mode, expected
):
    catalog = catalog_from_model(time_model)
    built = build_pattern(
        PatternSelection(
            pattern="previous_period",
            base=_metric(catalog),
            date=_field(catalog, "Date", "Calendar"),
            period="month",
            previous_mode=mode,
        ),
        catalog,
    )
    assert _value(monkeypatch, time_model, _at(built.expression, "2024-03-02")) == expected


def test_running_total_changes_only_dates_and_uses_selected_window(time_model, monkeypatch):
    catalog = catalog_from_model(time_model)
    kwargs = {
        "pattern": "running_total",
        "base": _metric(catalog),
        "date": _field(catalog, "Date", "Calendar"),
    }
    all_dates = build_pattern(PatternSelection(**kwargs, scope="all"), catalog)
    assert _value(monkeypatch, time_model, _at(all_dates.expression, "2024-03-01")) == 160
    selected = build_pattern(PatternSelection(**kwargs, scope="selected"), catalog)
    query = (
        "EVALUATE CALCULATETABLE(SUMMARIZECOLUMNS('Calendar'[Date], "
        f'"Total", {selected.expression}), '
        "'Calendar'[Date] >= DATE(2024,2,1), 'Calendar'[Date] <= DATE(2024,3,1)) "
        "ORDER BY 'Calendar'[Date]"
    )
    rows = _evaluate(monkeypatch, time_model, query).rows
    assert rows[0]["Total"] == 40
    assert rows[-1]["Total"] == 100


def test_moving_average_uses_daily_values_and_ignores_blank_days(time_model, monkeypatch):
    catalog = catalog_from_model(time_model)
    selection = PatternSelection(
        pattern="moving_average",
        base=_metric(catalog),
        date=_field(catalog, "Date", "Calendar"),
        period="day",
        window=2,
    )
    built = build_pattern(selection, catalog)
    assert _value(monkeypatch, time_model, _at(built.expression, "2024-03-02")) == 70
    assert _value(monkeypatch, time_model, _at(built.expression, "2024-03-01")) == 60
    assert any("BLANK days are excluded" in note for note in built.notes)


@pytest.mark.parametrize(
    "goal, pattern",
    [
        ("Total Sales YTD", "period_to_date"),
        ("Total Sales QoQ", "percent_change"),
        ("3-day moving average of Total Sales", "moving_average"),
        ("running total of Total Sales over all dates", "running_total"),
    ],
)
def test_common_time_wording_uses_deterministic_slots(time_model, goal, pattern):
    catalog = catalog_from_model(time_model)
    selection = parse_business_pattern(goal, catalog)
    assert selection is not None and selection.pattern == pattern
    build_pattern(selection, catalog, goal=goal)


def test_ambiguous_field_does_not_pick_first_table():
    catalog = PatternCatalog(
        (
            PatternField(1, "column", "A", "Amount", "number"),
            PatternField(2, "column", "B", "Amount", "number"),
        )
    )
    with pytest.raises(PatternError) as error:
        resolve_field("total Amount", catalog)
    assert error.value.choices == (1, 2)
    assert resolve_field("SUM('B'[Amount])", catalog).index == 2


def test_names_are_escaped_as_identifiers_not_executable_text():
    catalog = PatternCatalog(
        (
            PatternField(1, "column", "Order's", "Discount]Amount", "number"),
            PatternField(2, "measure", "Order's", "Total]Sales"),
        )
    )
    built = build_pattern(
        PatternSelection(
            pattern="ratio",
            numerator=MetricSelection(field=1, aggregation="sum"),
            denominator=MetricSelection(field=2),
        ),
        catalog,
    )
    assert built.expression == "DIVIDE(SUM('Order''s'[Discount]]Amount]), [Total]]Sales])"


def test_edit_context_types_and_dependencies_are_used_without_inventing_joins():
    context = EditContext(
        [],
        [],
        [MeasureEntry("Total Sales", "Measures", "SUM('Sales'[Amount])")],
        {"Sales": ["Amount", "When"]},
        column_types={"Sales.Amount": "float64", "Sales.When": "datetime64[ns]"},
        head_revision="head-one",
    )
    catalog = catalog_from_context(context)
    assert catalog.revision == "head-one"
    assert catalog.fields[0].source_tables == ("Sales",)
    assert resolve_field("Amount", catalog).data_type == "number"
    with pytest.raises(PatternError, match="calendar"):
        build_pattern(
            PatternSelection(
                pattern="period_to_date",
                base=_metric(catalog),
                date=_field(catalog, "When"),
                period="year",
            ),
            catalog,
        )


def test_unrelated_group_and_unproved_joins_are_refused(model):
    catalog = catalog_from_model(model)
    selection = PatternSelection(
        pattern="share_total",
        base=_metric(catalog),
        group=_field(catalog, "Name", "Groups"),
        scope="all",
    )
    with pytest.raises(PatternError, match="relationships"):
        build_pattern(selection, replace(catalog, relationships_complete=False))
    with pytest.raises(PatternError, match="No active relationship"):
        build_pattern(selection, replace(catalog, relationships=()))


def test_incomplete_and_duplicate_calendar_dates_are_refused(time_model):
    catalog = catalog_from_model(time_model)
    day = _field(catalog, "Date", "Calendar")
    bad_fields = tuple(
        replace(item, calendar_contiguous=False) if item.index == day else item
        for item in catalog.fields
    )
    with pytest.raises(PatternError, match="complete daily calendar"):
        build_pattern(
            PatternSelection(
                pattern="period_to_date", base=_metric(catalog), date=day, period="year"
            ),
            replace(catalog, fields=bad_fields),
        )


def test_unavailable_measure_is_retained_but_cannot_be_compiled(model):
    blocked = report_engine.ReportModel(
        model.tables.values(),
        model.relationships,
        model.measures.values(),
        unavailable_measures={"total sales": "The model rows are stale; use Power BI Desktop."},
    )
    catalog = catalog_from_model(blocked)
    with pytest.raises(PatternError, match="stale"):
        build_pattern(
            PatternSelection(
                pattern="ratio", numerator=_metric(catalog), denominator=_metric(catalog, "Target")
            ),
            catalog,
        )
    prompt = pattern_prompt("Total Sales divided by Target", catalog)
    assert all(item.name != "Total Sales" for item in prompt.catalog.fields)


@pytest.mark.parametrize(
    "goal",
    [
        "Total Sales YTD for 2024",
        "ratio of DiscountAmount to Total Sales excluding returns",
        "fiscal YTD Total Sales",
    ],
)
def test_requested_filters_or_fiscal_calendars_are_not_dropped(model, goal):
    with pytest.raises(PatternError, match="filter or fiscal calendar"):
        build_pattern(
            PatternSelection(
                pattern="ratio",
                numerator=_metric(catalog_from_model(model), "DiscountAmount", "sum"),
                denominator=_metric(catalog_from_model(model)),
            ),
            catalog_from_model(model),
            goal=goal,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"pattern": "ratio", "expression": 'EVALUATE ROW("x", 1)'},
        {"pattern": "freeform"},
        {"pattern": "top_n", "n": 101},
        {"pattern": "moving_average", "window": 0},
        {"pattern": "distinct_count", "entity": 1.2},
        {"pattern": "ratio", "numerator": {"field": 1, "aggregation": "SQL"}},
    ],
)
def test_model_contract_rejects_code_unknown_patterns_and_unbounded_choices(payload):
    with pytest.raises(ValidationError):
        PatternSelection.model_validate(payload)


def test_prompt_is_bounded_formula_free_and_retains_original_field_numbers():
    fields = tuple(
        PatternField(index, "column", "LongTable", f"Field{index}" + "x" * 60, "number")
        for index in range(1, 200)
    )
    catalog = PatternCatalog(
        (*fields, PatternField(200, "measure", "Measures", "Total Sales")), revision="head"
    )
    prompt = pattern_prompt("total sales with let Source = SECRET_DATA in Source", catalog)
    assert prompt.size < 3000
    assert "SECRET_DATA" not in prompt.user
    properties = prompt.response_model.model_json_schema()["properties"]
    assert not {"expression", "dax", "m"} & properties.keys()
    ids = [item.index for item in prompt.catalog.fields]
    assert 200 in ids and len(ids) < 200
    assert (
        prompt.response_model.model_json_schema()["$defs"]["MetricSelection"]["properties"][
            "field"
        ]["enum"]
        == ids
    )


def test_deterministic_selection_never_calls_model(model):
    class ShouldNotRun:
        def generate(self, *args, **kwargs):
            pytest.fail("An explicit simple ratio must not call the model")

    built = select_business_pattern(
        "total DiscountAmount divided by Total Sales", catalog_from_model(model), ShouldNotRun()
    )
    assert built is not None


def test_model_selects_slots_then_python_compiles_and_evaluates(model, monkeypatch):
    catalog = catalog_from_model(model)

    class SelectAverage:
        def generate(self, system, user, response_model, **kwargs):
            assert len((system + user).encode()) < 3000
            assert kwargs["repair_attempts"] == 0
            return response_model(
                pattern="average_per", base=_metric(catalog), entity=_field(catalog, "CustomerId")
            )

    built = select_business_pattern("average sales per customer", catalog, SelectAverage())
    assert built is not None
    assert _value(monkeypatch, model, built.expression) == 25


def test_out_of_catalog_model_choices_fail_before_any_evaluation(model):
    class UnknownField:
        def generate(self, system, user, response_model, **kwargs):
            return response_model(
                pattern="ratio", numerator={"field": 99999}, denominator={"field": 1}
            )

    with pytest.raises(PatternError, match="known numerator"):
        select_business_pattern(
            "compare metrics as a ratio", catalog_from_model(model), UnknownField()
        )


def test_model_can_decline_unsupported_patterns(model):
    class Decline:
        def generate(self, system, user, response_model, **kwargs):
            return response_model(pattern=None)

    assert (
        select_business_pattern("predict sales next year", catalog_from_model(model), Decline())
        is None
    )


def test_new_measure_is_excluded_from_both_deterministic_and_model_choices(model):
    catalog = catalog_from_model(model)
    prompt = pattern_prompt("update Total Sales", catalog, exclude_measure="Total Sales")
    assert all(item.name != "Total Sales" for item in prompt.catalog.fields)
    with pytest.raises(PatternError, match="Name the model field"):
        select_business_pattern(
            "Total Sales divided by Target", catalog, exclude_measure="Total Sales"
        )


def test_missing_total_scope_requires_a_choice(model):
    catalog = catalog_from_model(model)
    with pytest.raises(PatternError) as error:
        build_pattern(
            PatternSelection(
                pattern="share_total",
                base=_metric(catalog),
                group=_field(catalog, "Name", "Groups"),
            ),
            catalog,
        )
    assert error.value.slot == "scope"


def test_extra_selected_options_are_not_silently_ignored(model):
    catalog = catalog_from_model(model)
    with pytest.raises(PatternError, match="couldn't combine grouping") as error:
        build_pattern(
            PatternSelection(
                pattern="ratio",
                numerator=_metric(catalog, "DiscountAmount", "sum"),
                denominator=_metric(catalog),
                group=_field(catalog, "Name", "Groups"),
            ),
            catalog,
        )
    assert error.value.slot == "group"
    assert "Which grouping or filter behavior" in str(error.value)
    assert "Your current formula is unchanged" in str(error.value)


def test_fixed_grain_requires_explicit_filter_decision_before_model_selection(model):
    catalog = catalog_from_model(model)

    class NoModel:
        def generate(self, *_args, **_kwargs):
            pytest.fail("A missing fixed-grain filter decision reached the model")

    with pytest.raises(PatternError, match="ignore other filters while keeping") as error:
        select_business_pattern("Create fixed Total Sales by CustomerId", catalog, NoModel())
    assert error.value.slot == "context"
    assert "'FactSales'[CustomerId]" in str(error.value)


def test_fixed_grain_preserves_requested_group_and_removes_other_filters(model, monkeypatch):
    catalog = catalog_from_model(model)
    built = select_business_pattern(
        "Create fixed Total Sales by CustomerId, ignore other filters", catalog
    )
    assert built is not None
    assert (
        built.expression
        == "CALCULATE([Total Sales], ALLEXCEPT('FactSales', 'FactSales'[CustomerId]))"
    )
    filters = "'FactSales'[CustomerId] = 1, 'FactSales'[DiscountAmount] = 2"
    assert _value(monkeypatch, model, f"CALCULATE([Total Sales], {filters})") == 10
    assert _value(monkeypatch, model, f"CALCULATE({built.expression}, {filters})") == 30
    assert (
        _value(
            monkeypatch,
            model,
            f"CALCULATE({built.expression}, 'FactSales'[CustomerId] = 1, "
            "'Groups'[Region] = \"East\")",
        )
        == 30
    )
    assert _value(monkeypatch, model, built.expression) == 100
    assert (
        _value(monkeypatch, model, f"CALCULATE({built.expression}, 'FactSales'[CustomerId] = 1)")
        == 30
    )
    notes = " ".join(built.notes)
    assert "expanded related tables" in notes
    assert "A grand total still keeps any existing filter on that grouping" in notes


def test_fixed_grain_cannot_be_replaced_with_share_or_another_metric(model):
    catalog = catalog_from_model(model)
    goal = "Create fixed Total Sales by CustomerId, ignore other filters"
    group = _field(catalog, "CustomerId", "FactSales")
    with pytest.raises(PatternError, match="not a share or time calculation"):
        build_pattern(
            PatternSelection(
                pattern="share_total", base=_metric(catalog), group=group, scope="all"
            ),
            catalog,
            goal=goal,
        )
    with pytest.raises(PatternError, match="metric and fixed grouping"):
        build_pattern(
            PatternSelection(
                pattern="fixed_grain", base=_metric(catalog, "DiscountAmount", "sum"), group=group
            ),
            catalog,
            goal=goal,
        )


@pytest.mark.parametrize(
    "goal, message",
    [
        (
            "fixed Total Sales by CustomerId, keep other filters",
            "source table and its expanded related tables",
        ),
        (
            "fixed Total Sales by CustomerId, do not ignore other filters",
            "source table and its expanded related tables",
        ),
        ("fixed Total Sales by Name, ignore other filters", "single source table"),
        (
            "fixed Total Sales by CustomerId and GroupId, ignore other filters",
            "one source-table grouping",
        ),
        (
            "fixed Total Sales by CustomerId, GroupId, ignore other filters",
            "one source-table grouping",
        ),
        (
            "fixed Total Sales by CustomerId, ignore other filters and keep DiscountAmount",
            "additional filters",
        ),
        (
            "YTD fixed Total Sales by CustomerId, ignore other filters",
            "other calculation first",
        ),
        (
            "Year-to-date fixed Total Sales by CustomerId, ignore other filters",
            "other calculation first",
        ),
    ],
)
def test_fixed_grain_declines_unrepresented_filter_or_group_semantics(model, goal, message):
    with pytest.raises(PatternError, match=message):
        select_business_pattern(goal, catalog_from_model(model))


def test_fixed_in_a_metric_name_does_not_force_fixed_grain(model):
    catalog = catalog_from_model(model)
    discount = next(field for field in catalog.fields if field.name == "DiscountAmount")
    catalog = replace(
        catalog,
        fields=(
            *catalog.fields,
            replace(discount, index=len(catalog.fields) + 1, name="Fixed Cost"),
        ),
    )
    built = select_business_pattern("Fixed Cost divided by Total Sales", catalog)
    assert built is not None
    assert built.expression.startswith("DIVIDE(")


def test_period_clause_in_ratio_is_not_lost(model):
    with pytest.raises(PatternError, match="period calculation"):
        select_business_pattern(
            "YTD DiscountAmount divided by Total Sales", catalog_from_model(model)
        )


def test_multiple_time_patterns_require_resolution(time_model):
    with pytest.raises(PatternError, match="one calculation pattern"):
        select_business_pattern("YoY of YTD Total Sales", catalog_from_model(time_model))


def test_explicit_calendar_column_does_not_replace_the_base_metric(time_model):
    catalog = catalog_from_model(time_model)
    built = select_business_pattern("YTD Total Sales using 'Calendar'[Date]", catalog)
    assert built is not None
    assert built.expression == "TOTALYTD([Total Sales], 'Calendar'[Date])"


@pytest.mark.parametrize(
    "value, qualifier",
    [
        ("West", "for West"),
        ("East", "for East"),
        ("West", "in West"),
        ("East", "in the eastern region"),
        ("West", "with Region West"),
        ("East", "with Region = East"),
        ("West", "within the western territory"),
        ("East", "within Region equals East"),
        ("West", "on West region"),
        ("East", "during East sales"),
        ("West", "at West"),
        ("East", "among eastern-region orders"),
        ("West", "except East"),
        ("East", "without West"),
        ("West", "not including East"),
        ("East", "when Region is East"),
        ("West", "unless Region is East"),
        ("East", "that match Region East"),
        ("West", "who match the western territory"),
        ("East", "Region is East"),
        ("West", "Region = West"),
        ("West", "Amount < 50"),
        ("East", "Amount >= 50"),
        ("West", "that is for West"),
    ],
)
def test_named_region_filter_is_refused_instead_of_returning_all_regions(
    monkeypatch, value, qualifier
):
    model = Model.from_frames(
        {
            "Sales": pd.DataFrame(
                {
                    "Day": pd.to_datetime(["2024-01-01", "2024-03-01"]),
                    "Amount": [30.0, 70.0],
                    "Region": ["West", "East"],
                }
            ),
            "Calendar": pd.DataFrame({"Date": pd.date_range("2024-01-01", "2024-12-31")}),
        },
        [("Sales", "Day", "Calendar", "Date")],
        {"Total Sales": "SUM('Sales'[Amount])"},
    )
    expected = 30 if value == "West" else 70
    assert (
        _value(monkeypatch, model, f'CALCULATE([Total Sales], Sales[Region] = "{value}")')
        == expected
    )
    assert _value(monkeypatch, model, "TOTALYTD([Total Sales], 'Calendar'[Date])") == 100
    with pytest.raises(PatternError, match="filter") as error:
        select_business_pattern("YTD Total Sales " + qualifier, catalog_from_model(model))
    assert error.value.slot == "context"


@pytest.mark.parametrize(
    "qualifier",
    [
        "this year",
        "current month",
        "today",
        "yesterday",
        "2024",
        "in 2024",
        "for year 2024",
        "in March",
        "during March",
        "during the first quarter",
        "within last month",
        "on March 1",
        "with March dates",
        "in Q1",
        "within January through March",
        "between January and March",
        "except last month",
        "without January",
        "when Date is March 1",
        "not including February",
    ],
)
def test_unrepresented_current_or_named_period_is_not_ignored(time_model, qualifier):
    with pytest.raises(PatternError, match="filter|date"):
        select_business_pattern("YTD Total Sales " + qualifier, catalog_from_model(time_model))


def test_for_recognized_metric_is_harmless_and_still_grounded(time_model):
    catalog = catalog_from_model(time_model)
    built = select_business_pattern("YTD for Total Sales", catalog)
    assert built is not None and built.expression == "TOTALYTD([Total Sales], 'Calendar'[Date])"


def test_moving_average_for_last_seven_days_keeps_window_grounding(time_model):
    catalog = catalog_from_model(time_model)
    selection = PatternSelection(
        pattern="moving_average",
        base=_metric(catalog),
        date=_field(catalog, "Date", "Calendar"),
        period="day",
        window=7,
    )
    build_pattern(selection, catalog, goal="moving average of Total Sales for last 7 days")
    with pytest.raises(PatternError, match="moving window"):
        build_pattern(
            selection.model_copy(update={"window": 30}),
            catalog,
            goal="moving average of Total Sales for last 7 days",
        )


@pytest.mark.parametrize("preposition", ["for", "in", "with", "during", "within", "on"])
def test_explicit_selected_calendar_reference_is_a_represented_clause(
    time_model, monkeypatch, preposition
):
    built = select_business_pattern(
        f"YTD Total Sales {preposition} 'Calendar'[Date]", catalog_from_model(time_model)
    )
    assert built is not None
    assert built.expression == "TOTALYTD([Total Sales], 'Calendar'[Date])"
    assert _value(monkeypatch, time_model, built.expression) == 200


def test_scope_window_and_format_clauses_must_match_compiler_slots(model, time_model, monkeypatch):
    catalog = catalog_from_model(time_model)
    selection = PatternSelection(
        pattern="running_total",
        base=_metric(catalog),
        date=_field(catalog, "Date", "Calendar"),
        scope="all",
    )
    built = build_pattern(selection, catalog, goal="running total Total Sales within all dates")
    assert _value(monkeypatch, time_model, _at(built.expression, "2024-03-01")) == 160
    with pytest.raises(PatternError, match="filter or grain"):
        build_pattern(selection, catalog, goal="running total Total Sales within selected dates")
    moving = PatternSelection(
        pattern="moving_average", base=_metric(catalog), date=selection.date, period="day", window=7
    )
    moving_goal = "7-day moving average Total Sales with last 7 days on 'Calendar'[Date]"
    assert build_pattern(moving, catalog, goal=moving_goal).expression.startswith("AVERAGEX(")
    with pytest.raises(PatternError, match="window"):
        build_pattern(moving.model_copy(update={"window": 30}), catalog, goal=moving_goal)
    ratio = select_business_pattern(
        "total DiscountAmount divided by Total Sales with percentage formatting",
        catalog_from_model(model),
    )
    assert ratio is not None and ratio.format_string == "0.0%"
    assert _value(monkeypatch, model, ratio.expression) == 0.2
    with pytest.raises(PatternError, match="filter or grain"):
        select_business_pattern("YTD Total Sales with percentage formatting", catalog)
    currency = replace(
        catalog,
        fields=tuple(
            replace(item, format_string="$#,0.00") if item.name == "Total Sales" else item
            for item in catalog.fields
        ),
    )
    assert (
        select_business_pattern("YTD Total Sales with currency formatting", currency).format_string
        == "$#,0.00"
    )


def test_existing_metric_identity_containing_a_preposition_remains_grounded(monkeypatch):
    model = Model.from_frames(
        {
            "Sales": pd.DataFrame(
                {
                    "Day": pd.to_datetime(["2024-01-01", "2024-03-01"]),
                    "Amount": [30.0, 70.0],
                    "Region": ["West", "East"],
                }
            ),
            "Calendar": pd.DataFrame({"Date": pd.date_range("2024-01-01", "2024-12-31")}),
        },
        [("Sales", "Day", "Calendar", "Date")],
        {"Sales in West": 'CALCULATE(SUM(Sales[Amount]), Sales[Region] = "West")'},
    )
    built = select_business_pattern("YTD Sales in West", catalog_from_model(model))
    assert built is not None
    assert built.expression == "TOTALYTD([Sales in West], 'Calendar'[Date])"
    assert _value(monkeypatch, model, built.expression) == 30


@pytest.mark.parametrize(
    "goal",
    [
        "share of Total Sales in West",
        "rank Total Sales among West",
        "top 3 Regions within West",
        "average Total Sales per CustomerId without West",
        "avg Total Sales per CustomerId except East",
        "distinct count CustomerId when Region is West",
        "actual Total Sales versus Target that match Region West",
        "running total Total Sales on March 1",
        "YTD Total Sales not including West",
        "share of Total Sales Region = West",
        "top 3 Regions SalesAmount >= 30",
        "average Total Sales per CustomerId Region is East",
    ],
)
def test_unrepresented_selectors_clarify_before_a_model_can_decline(model, goal):
    class Decline:
        calls = 0

        def generate(self, system, user, response_model, **kwargs):
            self.calls += 1
            return response_model(pattern=None)

    decline = Decline()
    with pytest.raises(PatternError, match="filter") as error:
        select_business_pattern(goal, catalog_from_model(model), decline)
    assert error.value.slot == "context" and decline.calls == 0


def test_genuinely_unrecognized_freeform_goal_keeps_its_existing_fallback(model):
    class Decline:
        calls = 0

        def generate(self, system, user, response_model, **kwargs):
            self.calls += 1
            return response_model(pattern=None)

    decline = Decline()
    assert (
        select_business_pattern("Explain data quality in West", catalog_from_model(model), decline)
        is None
    )
    assert decline.calls == 1


@pytest.mark.parametrize(
    "declaration",
    [
        "create a measure Discount Rate for",
        "create a measure called Discount Rate for",
        "create a measure named Discount Rate for",
        "make a new measure Discount Rate for",
        "add a measure for",
        "create a measure called 'Discount Rate for West' for",
    ],
)
def test_named_measure_for_declaration_keeps_ratio_operands_and_real_values(
    model, monkeypatch, declaration
):
    goal = declaration + " the discount amount divided by the sales amount"
    built = select_business_pattern(goal, catalog_from_model(model))
    assert built is not None
    assert built.expression == (
        "DIVIDE(SUM('FactSales'[DiscountAmount]), SUM('FactSales'[SalesAmount]))"
    )
    assert _value(monkeypatch, model, built.expression) == 0.2


@pytest.mark.parametrize(
    "declaration",
    [
        "create a measure Discount Rate for",
        "create a measure called Discount Rate for",
        "create a measure named Discount Rate for",
        "make a new measure Discount Rate for",
        "add a measure for",
        "create a measure called 'Discount Rate for West' for",
    ],
)
def test_named_measure_for_declaration_cannot_hide_a_nested_real_filter(model, declaration):
    goal = declaration + " the discount amount divided by the sales amount for West"
    with pytest.raises(PatternError, match="filter") as error:
        select_business_pattern(goal, catalog_from_model(model))
    assert error.value.slot == "context"


@pytest.fixture
def period_change_model():
    return Model.from_frames(
        {
            "Sales": pd.DataFrame(
                {"Day": pd.to_datetime(["2023-01-01", "2024-01-01"]), "Amount": [100.0, 200.0]}
            ),
            "Calendar": pd.DataFrame({"Date": pd.date_range("2023-01-01", "2024-12-31")}),
        },
        [("Sales", "Day", "Calendar", "Date")],
        {"Total Sales": "SUM(Sales[Amount])"},
    )


@pytest.mark.parametrize(
    "goal",
    [
        "year-over-year absolute change in Total Sales",
        "YoY difference Total Sales",
        "YoY currency change Total Sales",
        "year over year amount variance Total Sales",
        "YoY delta Total Sales",
    ],
)
def test_absolute_period_change_returns_currency_delta_and_first_period_blank(
    period_change_model, monkeypatch, goal
):
    catalog = catalog_from_model(period_change_model)
    catalog = replace(
        catalog,
        fields=tuple(
            replace(item, format_string="$#,0.00") if item.name == "Total Sales" else item
            for item in catalog.fields
        ),
    )
    built = select_business_pattern(goal, catalog, None)
    assert built is not None and built.format_string == "$#,0.00"
    assert _value(monkeypatch, period_change_model, _at(built.expression, "2024-01-01")) == 100
    assert _value(monkeypatch, period_change_model, _at(built.expression, "2023-01-01")) is None
    assert "absolute difference" in " ".join(built.notes)


@pytest.mark.parametrize(
    "goal",
    [
        "YoY percentage change Total Sales",
        "year-over-year percent difference Total Sales",
        "YoY % change Total Sales",
        "YoY Total Sales",
    ],
)
def test_percentage_period_change_retains_relative_result_and_default(
    period_change_model, monkeypatch, goal
):
    built = select_business_pattern(goal, catalog_from_model(period_change_model), None)
    assert built is not None and built.format_string == "0.0%"
    assert _value(monkeypatch, period_change_model, _at(built.expression, "2024-01-01")) == 1.0
    assert _value(monkeypatch, period_change_model, _at(built.expression, "2023-01-01")) is None


@pytest.mark.parametrize(
    "goal, expected",
    [
        ("YoY absolute change Total Sales", "variance"),
        ("YoY percentage change Total Sales", "variance_percent"),
    ],
)
@pytest.mark.parametrize("comparison", [None, "variance", "variance_percent", "attainment"])
def test_model_period_change_mode_must_match_explicit_request(
    period_change_model, goal, expected, comparison
):
    catalog = catalog_from_model(period_change_model)
    selection = PatternSelection(
        pattern="percent_change",
        base=_metric(catalog),
        date=_field(catalog, "Date", "Calendar"),
        period="year",
        comparison=comparison,
    )
    if comparison == expected:
        assert build_pattern(selection, catalog, goal=goal).expression
    else:
        with pytest.raises(PatternError) as error:
            build_pattern(selection, catalog, goal=goal)
        assert error.value.slot == "comparison"


def test_conflicting_absolute_and_percentage_change_clarifies(period_change_model):
    with pytest.raises(PatternError, match="absolute or percentage"):
        select_business_pattern(
            "YoY absolute change as percentage Total Sales", catalog_from_model(period_change_model)
        )


def test_absolute_change_without_reviewed_calendar_cannot_bypass_to_freeform(model):
    class Decline:
        def generate(self, *_, **__):
            pytest.fail("Missing calendar must clarify before any model fallback.")

    with pytest.raises(PatternError) as error:
        select_business_pattern(
            "YoY currency change Total Sales", catalog_from_model(model), Decline()
        )
    assert error.value.slot == "date"


@pytest.mark.parametrize(
    "goal",
    [
        "3-month moving average of Total Sales calculated monthly",
        "moving average of monthly totals of Total Sales",
        "monthly moving average Total Sales",
        "monthly moving avg Total Sales",
        "moving average Total Sales by month",
        "4-quarter moving average Total Sales calculated quarterly",
        "moving average of quarterly totals of Total Sales",
        "quarterly rolling average Total Sales",
        "weekly moving average Total Sales",
        "moving average of weekly totals of Total Sales",
        "rolling avg Total Sales by week",
    ],
)
def test_unsupported_averaging_grain_clarifies_before_model_or_formula(monkeypatch, goal):
    days = pd.date_range("2024-01-01", "2024-03-31")
    model = Model.from_frames(
        {
            "Sales": pd.DataFrame(
                {
                    "Day": pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01", "2024-03-02"]),
                    "Amount": [20.0, 40.0, 60.0, 80.0],
                }
            ),
            "Calendar": pd.DataFrame(
                {
                    "Date": days,
                    "Year Month": days.strftime("%Y-%m"),
                    "Year Quarter": days.to_period("Q").astype(str),
                    "Week Start": days.to_period("W").start_time,
                }
            ),
        },
        [("Sales", "Day", "Calendar", "Date")],
        {"Total Sales": "SUM(Sales[Amount])"},
    )
    # Grouped means come from actual period totals, not daily metric values.
    grain = (
        "Year Quarter" if "quarter" in goal else "Week Start" if "week" in goal else "Year Month"
    )
    expected = _value(
        monkeypatch, model, f"AVERAGEX(VALUES('Calendar'[{grain}]), CALCULATE([Total Sales]))"
    )
    assert expected == pytest.approx(200 if grain == "Year Quarter" else 200 / 3)

    class Decline:
        calls = 0

        def generate(self, system, user, response_model, **kwargs):
            self.calls += 1
            return response_model(pattern=None)

    decline = Decline()
    for catalog in (catalog_from_model(model), PatternCatalog(())):
        for selector in (None, decline):
            with pytest.raises(PatternError, match="averaging grain") as error:
                select_business_pattern(goal, catalog, selector)
            assert error.value.slot == "grain"
    assert decline.calls == 0


@pytest.mark.parametrize(
    "goal",
    [
        "3-month moving average Total Sales",
        "3-month moving average of daily Total Sales",
        "3-month moving avg Total Sales calculated daily",
    ],
)
def test_month_window_with_daily_averaging_remains_supported(time_model, monkeypatch, goal):
    built = select_business_pattern(goal, catalog_from_model(time_model))
    assert built is not None
    assert _value(monkeypatch, time_model, built.expression) == 50


def test_absolute_zero_previous_value_is_valid_while_percentage_is_blank(monkeypatch):
    model = Model.from_frames(
        {
            "Sales": pd.DataFrame(
                {"Day": pd.to_datetime(["2023-01-01", "2024-01-01"]), "Amount": [0.0, 200.0]}
            ),
            "Calendar": pd.DataFrame({"Date": pd.date_range("2023-01-01", "2024-12-31")}),
        },
        [("Sales", "Day", "Calendar", "Date")],
        {"Total Sales": "SUM(Sales[Amount])"},
    )
    catalog = catalog_from_model(model)
    absolute = select_business_pattern("YoY absolute change Total Sales", catalog)
    percentage = select_business_pattern("YoY percentage change Total Sales", catalog)
    assert _value(monkeypatch, model, _at(absolute.expression, "2024-01-01")) == 200
    assert _value(monkeypatch, model, _at(percentage.expression, "2024-01-01")) is None
