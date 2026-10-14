"""Filter operations through the edit layer: ``apply_operations`` on the multipage fixture.

report_filters.py is tested on its own in test_report_filters.py (what each variant writes). This
file checks what the integration adds: the operations join the union and parse from chat's dicts,
one set of filter names serves a whole batch, a replaced filter keeps its name, a variant the
verified table turns off is refused, Studio's canvas limits become a warning, and ``ModelInfo``
answers the questions the filters ask of a model (types from the metadata, fields without a table).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import report_edit_filters, report_filters
from ai_powerbi_analyst.studio import report_edits as edits
from ai_powerbi_analyst.studio.pbir_document import DocumentError, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import ModelInfo

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
PAGE1 = "ReportSection8454c812b60b8bb83b99"
DETAILS = "ReportSection3f1c0d5e7a9b2c4d6e8f"
COLUMN = "b404a461ede7604a2774"  # a column chart with no filters
TEXT_BOX_LIKE = "81ab76274a5b4e9e62da"  # an image on Details
NAME = re.compile(r"Filter[0-9a-f]{20}")


def load() -> PbirDocument:
    return PbirDocument(PbixPackage(FIXTURES / "multipage.pbix"))


def sales_model(*, with_types: bool = True) -> ModelInfo:
    types = {
        "columns": [
            {"table": "Sales", "name": "Country", "dataType": 2},
            {"table": "Sales", "name": "Year", "dataType": 6},
            {"table": "Sales", "name": "Amount", "dataType": 10},
            {"table": "Sales", "name": "OrderDate", "dataType": 9},
            {"table": "DimStore", "name": "StoreName", "dataType": 2},
            {"table": "DimStore", "name": "Country", "dataType": 2},
        ],
        "measures": [
            {"table": "# Measures", "name": "Total Sales", "dataType": 8},
            {"table": "# Measures", "name": "Rank Filtering Dynamics", "dataType": 6},
        ],
    }
    return ModelInfo.from_report(
        [
            {"TableName": "Sales", "ColumnName": "Country", "PandasDataType": "object"},
            {"TableName": "Sales", "ColumnName": "Year", "PandasDataType": "int64"},
            {"TableName": "Sales", "ColumnName": "Amount", "PandasDataType": "float64"},
            {"TableName": "Sales", "ColumnName": "OrderDate", "PandasDataType": "datetime64[ns]"},
            {"TableName": "DimStore", "ColumnName": "StoreName", "PandasDataType": "object"},
            {"TableName": "DimStore", "ColumnName": "Country", "PandasDataType": "object"},
        ],
        [
            {"Name": "Total Sales", "TableName": "# Measures"},
            {"Name": "Rank Filtering Dynamics", "TableName": "# Measures"},
        ],
        types if with_types else None,
    )


def countries(*values: str, table: str | None = "Sales") -> dict[str, Any]:
    return {
        "kind": "categorical",
        "field": {"table": table, "name": "Country"},
        "values": list(values),
    }


def visual_filter(visual: str, spec: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"op": "set_visual_filter", "visual": visual, "filter": spec, **extra}


def page_filter(page: str, spec: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"op": "set_page_filter", "page": page, "filter": spec, **extra}


def report_filter(spec: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"op": "set_report_filter", "filter": spec, **extra}


def run(document: PbirDocument, *ops: dict[str, Any], model: ModelInfo | None = None) -> Any:
    return edits.apply_operations(
        document, edits.parse_operations(list(ops)), model or sales_model()
    )


def refuses(document: PbirDocument, *ops: dict[str, Any], model: ModelInfo | None = None) -> str:
    with pytest.raises(DocumentError) as raised:
        run(document, *ops, model=model)
    return str(raised.value)


def filters_of(holder: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = holder.get("filterConfig", {}).get("filters", [])
    return found


# --- the operations are part of the union ---------------------------------------------------


def test_the_filter_operations_join_the_union_and_the_handlers() -> None:
    names = [cls.model_fields["op"].default for cls in report_edit_filters.FILTER_OPERATIONS]
    assert names == ["set_page_filter", "set_visual_filter", "set_report_filter", "clear_filter"]
    assert sorted(report_edit_filters.FILTER_HANDLERS) == sorted(names)
    assert all(name in edits._HANDLERS for name in names)
    assert edits.FILTER_OPERATIONS is report_filters.FILTER_OPERATIONS
    assert set(edits.FILTER_OPERATIONS) <= set(edits.REPORT_OPERATIONS)
    assert edits.FilterFieldRef is report_filters.FieldRef
    assert edits.FieldRef is not edits.FilterFieldRef  # operations' field refs need a table


def test_filter_operations_parse_from_chat_dictionaries() -> None:
    parsed = edits.parse_operations(
        [
            page_filter("1", countries("Canada")),
            visual_filter(COLUMN, countries("Canada"), locked=True),
            report_filter(countries("Canada"), hidden=True),
            {"op": "clear_filter", "scope": "report"},
        ]
    )
    assert [type(op).__name__ for op in parsed] == [
        "SetPageFilter",
        "SetVisualFilter",
        "SetReportFilter",
        "ClearFilter",
    ]
    batch = edits.EditBatch.model_validate({"operations": [page_filter("1", countries("x"))]})
    assert isinstance(batch.operations[0], edits.SetPageFilter)


@pytest.mark.parametrize(
    ("step", "message"),
    [
        (
            page_filter("1", {"kind": "categorical", "field": {"name": "Country"}, "values": []}),
            "Step 1 (set_page_filter): 'filter' (values) is not valid: list should have at "
            "least 1 item after validation, not 0",
        ),
        (
            page_filter("1", {"kind": "categorical", "field": {"name": "Country"}}),
            "Step 1 (set_page_filter) needs 'filter' (values)",
        ),
        (
            page_filter("1", {"kind": "bogus", "field": {"name": "Country"}}),
            "Step 1 (set_page_filter): 'filter' is not valid: input tag 'bogus' found using "
            "'kind' does not match any of the expected tags: 'categorical', 'advanced', "
            "'top_n', 'relative_date'",
        ),
        (
            visual_filter(
                "v", {"kind": "top_n", "field": {"name": "X"}, "n": 0, "by": {"name": "Y"}}
            ),
            "Step 1 (set_visual_filter): 'filter' (n) is not valid: input should be greater "
            "than or equal to 1",
        ),
        (
            {"op": "clear_filter", "scope": "galaxy"},
            "Step 1 (clear_filter): 'scope' is not valid: input should be 'visual', 'page' or "
            "'report'",
        ),
        (
            {"op": "set_page_filter", "filter": countries("a")},
            "Step 1 (set_page_filter) needs 'page'",
        ),
    ],
)
def test_a_bad_filter_step_names_the_step_and_the_field(step: dict[str, Any], message: str) -> None:
    with pytest.raises(DocumentError) as raised:
        edits.parse_operations([step])
    assert str(raised.value) == message


def test_a_step_that_is_not_a_known_operation_is_still_reported_as_that() -> None:
    with pytest.raises(DocumentError) as raised:
        edits.parse_operations([{"op": "set_page_filtr", "page": "1"}])
    assert str(raised.value) == (
        "Step 1: 'set_page_filtr' is not an operation Studio knows; did you mean 'set_page_filter'?"
    )


# --- each operation through apply_operations ------------------------------------------------


def test_a_visual_filter_is_written_the_way_desktop_writes_it() -> None:
    document = load()
    outcome = run(document, visual_filter(COLUMN, countries("Canada", "USA"), locked=True))
    (added,) = filters_of(document.visual(COLUMN)[1])
    assert NAME.fullmatch(added["name"])
    assert added["type"] == "Categorical"
    assert added["howCreated"] == "User"
    assert added["isLockedInViewMode"] is True
    assert added["field"] == {
        "Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Country"}
    }
    body = added["filter"]
    assert body["Version"] == 2
    assert body["From"] == [{"Name": "s", "Entity": "Sales", "Type": 0}]
    (row1, row2) = body["Where"][0]["Condition"]["In"]["Values"]
    assert [row1[0]["Literal"]["Value"], row2[0]["Literal"]["Value"]] == ["'Canada'", "'USA'"]
    assert body["Where"][0]["Condition"]["In"]["Expressions"][0]["Column"]["Expression"] == {
        "SourceRef": {"Source": "s"}
    }
    assert outcome.summaries == [
        "Added a filter on 'Total MTD Sales': Country is one of Canada, USA (locked)"
    ]
    assert outcome.warnings == []
    assert outcome.report_changed is True
    assert f"Report/definition/pages/{PAGE1}/visuals/{COLUMN}/visual.json" in document.commit()


def test_a_page_filter_finds_the_page_by_its_display_name_and_is_written_to_page_json() -> None:
    document = load()
    run(document, page_filter("Details", countries("Canada")))
    (added,) = filters_of(document.page(DETAILS).data)
    assert added["type"] == "Categorical"
    assert f"Report/definition/pages/{DETAILS}/page.json" in document.commit()


def test_a_report_filter_goes_to_the_report_beside_the_existing_one() -> None:
    document = load()
    before = len(filters_of(document.report))
    run(document, report_filter(countries("Canada", table="DimStore")))
    assert len(filters_of(document.report)) == before + 1
    assert "Report/definition/report.json" in document.commit()


def test_an_advanced_filter_on_a_measure_and_a_top_n_filter() -> None:
    document = load()
    outcome = run(
        document,
        visual_filter(
            COLUMN,
            {
                "kind": "advanced",
                "field": {"name": "Rank Filtering Dynamics"},
                "conditions": [{"operator": "is", "value": 1}],
            },
        ),
        page_filter(
            "1",
            {
                "kind": "top_n",
                "field": {"table": "DimStore", "name": "StoreName"},
                "n": 3,
                "by": {"name": "Total Sales"},
            },
        ),
    )
    advanced = filters_of(document.visual(COLUMN)[1])[0]
    assert advanced["type"] == "Advanced"
    assert advanced["field"]["Measure"]["Property"] == "Rank Filtering Dynamics"
    comparison = advanced["filter"]["Where"][0]["Condition"]["Comparison"]
    assert comparison["Right"]["Literal"]["Value"] == "1L"  # the measure's type is a whole number
    top = filters_of(document.page(PAGE1).data)[-1]
    assert top["type"] == "TopN"
    assert top["filter"]["From"][0]["Name"] == "subquery"
    assert top["filter"]["From"][0]["Expression"]["Subquery"]["Query"]["Top"] == 3
    # The canvas draws the measure filter, not the top N.
    assert outcome.warnings == [
        "Studio's canvas cannot draw this filter; the affected visuals will say Needs Power BI, "
        "and Power BI applies it. A top or bottom N filter is drawn by Power BI, not by "
        "Studio's canvas."
    ]


def test_a_relative_date_filter_warns_that_the_canvas_cannot_draw_it() -> None:
    document = load()
    outcome = run(
        document,
        report_filter(
            {
                "kind": "relative_date",
                "field": {"table": "Sales", "name": "OrderDate"},
                "unit": "days",
                "direction": "last",
                "count": 30,
            }
        ),
    )
    (added,) = [f for f in filters_of(document.report) if f["type"] == "RelativeDate"]
    assert added["field"]["Column"]["Property"] == "OrderDate"
    assert outcome.warnings == [
        "Studio's canvas cannot draw this filter; the affected visuals will say Needs Power BI, "
        "and Power BI applies it. A relative date filter is drawn by Power BI, not by Studio's "
        "canvas."
    ]


@pytest.mark.parametrize(
    ("spec", "quiet"),
    [
        (countries("Canada"), True),
        (
            {
                "kind": "advanced",
                "field": {"table": "Sales", "name": "Year"},
                "conditions": [{"operator": "greater_than", "value": 2015}],
            },
            True,
        ),
        (
            {
                "kind": "advanced",
                "field": {"table": "Sales", "name": "Country"},
                "conditions": [{"operator": "contains", "value": "an"}],
            },
            False,
        ),
        (
            {
                "kind": "advanced",
                "field": {"table": "Sales", "name": "OrderDate"},
                "conditions": [{"operator": "greater_than", "value": "2019-01-01"}],
            },
            False,
        ),
    ],
)
def test_only_filters_the_canvas_cannot_compile_get_the_warning(
    spec: dict[str, Any], quiet: bool
) -> None:
    outcome = run(load(), visual_filter(COLUMN, spec))
    assert (outcome.warnings == []) is quiet


# --- replace, not duplicate ------------------------------------------------------------------


def test_setting_a_filter_on_the_same_field_replaces_it_and_keeps_the_name() -> None:
    document = load()
    first = run(document, visual_filter(COLUMN, countries("Canada")))
    name = filters_of(document.visual(COLUMN)[1])[0]["name"]
    second = run(document, visual_filter(COLUMN, countries("USA", "Mexico")))
    (only,) = filters_of(document.visual(COLUMN)[1])
    assert only["name"] == name
    values = [
        row[0]["Literal"]["Value"]
        for row in only["filter"]["Where"][0]["Condition"]["In"]["Values"]
    ]
    assert values == ["'USA'", "'Mexico'"]
    assert first.summaries[0].startswith("Added a filter on ")
    assert second.summaries[0].startswith("Replaced a filter on ")


def test_a_different_field_is_added_next_to_it() -> None:
    document = load()
    run(
        document,
        visual_filter(COLUMN, countries("Canada")),
        visual_filter(COLUMN, countries("Berlin", table="DimStore")),
    )
    assert len(filters_of(document.visual(COLUMN)[1])) == 2


# --- names are unique across the report and the batch ---------------------------------------


def all_definitions(document: PbirDocument) -> list[str]:
    names: list[str] = [f["name"] for f in filters_of(document.report)]
    for page in document.pages.values():
        names += [f["name"] for f in filters_of(page.data)]
        for visual in page.visuals.values():
            names += [f["name"] for f in filters_of(visual)]
    return names


def test_names_stay_unique_across_the_multipage_report_and_its_bookmarks() -> None:
    document = load()
    bookmark_names = report_filters.collect_filter_names(document) - set(all_definitions(document))
    before = all_definitions(document)
    steps = [report_filter(countries("A", table="DimStore"))]
    steps += [page_filter(page, countries(f"P{n}")) for n, page in enumerate(document.pages)]
    steps += [
        visual_filter(visual, countries(f"V{n}"))
        for n, (_, visual, _) in enumerate(document.all_visuals())
        if document.visual(visual)[1].get("visual", {}).get("visualType")
        not in {"textbox", "image", "shape", "basicShape", "actionButton", None}
    ]
    run(document, *steps)
    names = all_definitions(document)
    assert len(names) > len(before) + 10
    assert len(names) == len(set(names))
    assert all(NAME.fullmatch(n) for n in names)
    assert not bookmark_names & (set(names) - set(before))


def test_the_names_of_a_batch_come_from_one_set_read_at_its_first_filter() -> None:
    document = load()
    seen: list[set[str]] = []
    real = report_edit_filters.apply_filter

    def spy(
        doc: PbirDocument, op: Any, model: Any, names: set[str] | None = None, **kw: Any
    ) -> Any:
        assert names is not None
        seen.append(names)
        return real(doc, op, model, names, **kw)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(report_edit_filters, "apply_filter", spy)
        run(document, page_filter("1", countries("A")), visual_filter(COLUMN, countries("B")))
        assert seen[0] is seen[1]
        run(document, report_filter(countries("C", table="DimStore")))
        assert seen[2] is not seen[0]  # a new batch reads the names again


def test_a_handler_called_on_its_own_reads_the_names_itself() -> None:
    document = load()
    handler = report_edit_filters.FILTER_HANDLERS["set_page_filter"]
    op = edits.parse_operations([page_filter("1", countries("A"))])[0]
    result = handler(document, op, sales_model())
    assert result.summary.startswith("Added a filter")


# --- variants ---------------------------------------------------------------------------------


def test_a_variant_the_verified_table_turns_off_is_refused_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(report_filters.VERIFIED_VARIANTS, "categorical_not_in", False)
    document = load()
    spec = {**countries("Canada"), "exclude": True}
    message = refuses(document, visual_filter(COLUMN, spec))
    assert message == (
        "Studio has not yet checked in Power BI how to write this kind of filter "
        "(categorical not in), so it will not add it. Add it in Power BI."
    )
    assert filters_of(document.visual(COLUMN)[1]) == []
    assert document.commit() == []
    assert run(document, visual_filter(COLUMN, countries("Canada"))).summaries  # the others work


def test_a_locked_filter_needs_its_own_variant_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(report_filters.VERIFIED_VARIANTS, "locked_in_view_mode", False)
    message = refuses(load(), visual_filter(COLUMN, countries("Canada"), locked=True))
    assert "(locked in view mode)" in message


def test_every_variant_is_verified_today() -> None:
    assert all(report_filters.VERIFIED_VARIANTS.values())


# --- clearing ---------------------------------------------------------------------------------


def test_clear_removes_one_field_or_all_of_them() -> None:
    document = load()
    run(
        document,
        visual_filter(COLUMN, countries("Canada")),
        visual_filter(COLUMN, countries("Berlin", table="DimStore")),
    )
    outcome = run(
        document,
        {
            "op": "clear_filter",
            "scope": "visual",
            "target": COLUMN,
            "field": {"table": "DimStore", "name": "Country"},
        },
    )
    assert outcome.summaries[0].startswith("Removed the filter on Country from ")
    (left,) = filters_of(document.visual(COLUMN)[1])
    assert left["field"]["Column"]["Expression"]["SourceRef"]["Entity"] == "Sales"
    run(document, {"op": "clear_filter", "scope": "visual", "target": COLUMN})
    assert "filterConfig" not in document.visual(COLUMN)[1]


def test_clear_says_when_there_is_nothing_to_clear() -> None:
    message = refuses(load(), {"op": "clear_filter", "scope": "visual", "target": COLUMN})
    assert message.startswith("There are no filters on ")
    message = refuses(load(), {"op": "clear_filter", "scope": "page"})
    assert message == "Say which page to clear the filter from"


def test_clear_a_field_resolves_it_without_a_table() -> None:
    document = load()
    run(document, page_filter("1", countries("Canada", table="DimStore")))
    ambiguous = refuses(
        document,
        {"op": "clear_filter", "scope": "page", "target": "1", "field": {"name": "Country"}},
    )
    assert ambiguous == (
        "'Country' could mean the column Sales[Country], the column DimStore[Country]; "
        "say which table it is in"
    )
    run(
        document,
        {
            "op": "clear_filter",
            "scope": "page",
            "target": "1",
            "field": {"table": "dimstore", "name": "country"},
        },
    )


# --- ModelInfo as the filters' model --------------------------------------------------------


def test_a_field_without_a_table_resolves_to_the_only_column_or_measure_of_that_name() -> None:
    document = load()
    run(
        document,
        visual_filter(
            COLUMN,
            {"kind": "categorical", "field": {"name": "StoreName"}, "values": ["Store 1"]},
        ),
        visual_filter(
            COLUMN,
            {
                "kind": "advanced",
                "field": {"name": "Total Sales"},
                "conditions": [{"operator": "greater_than", "value": 5}],
            },
        ),
    )
    fields = [
        next(iter(f["field"].values()))["Expression"]["SourceRef"]["Entity"]
        for f in filters_of(document.visual(COLUMN)[1])
    ]
    assert fields == ["DimStore", "# Measures"]


def test_an_ambiguous_field_without_a_table_asks_for_the_table() -> None:
    message = refuses(load(), visual_filter(COLUMN, countries("Canada", table=None)))
    assert message == (
        "'Country' could mean the column Sales[Country], the column DimStore[Country]; "
        "say which table it is in"
    )


def test_literals_follow_the_model_type_ids_not_the_pandas_dtype() -> None:
    document = load()
    run(
        document,
        visual_filter(
            COLUMN,
            {
                "kind": "advanced",
                "field": {"table": "Sales", "name": "Amount"},
                "conditions": [{"operator": "greater_than", "value": 5}],
            },
        ),
    )
    literal = json.dumps(filters_of(document.visual(COLUMN)[1])[0])
    assert '"Value": "5M"' in literal  # fixed decimal in the model, float64 in pandas
    plain = load()
    run(
        plain,
        visual_filter(
            COLUMN,
            {
                "kind": "advanced",
                "field": {"table": "Sales", "name": "Amount"},
                "conditions": [{"operator": "greater_than", "value": 5}],
            },
        ),
        model=sales_model(with_types=False),
    )
    assert '"Value": "5D"' in json.dumps(filters_of(plain.visual(COLUMN)[1])[0])


def test_a_date_column_takes_a_datetime_literal() -> None:
    document = load()
    run(
        document,
        visual_filter(
            COLUMN,
            {
                "kind": "advanced",
                "field": {"table": "Sales", "name": "OrderDate"},
                "conditions": [{"operator": "greater_or_equal", "value": "2019-01-01"}],
            },
        ),
    )
    text = json.dumps(filters_of(document.visual(COLUMN)[1])[0])
    assert "datetime'2019-01-01T00:00:00'" in text


def test_a_value_that_does_not_fit_the_column_says_which_column_and_what_it_holds() -> None:
    message = refuses(
        load(),
        visual_filter(
            COLUMN,
            {
                "kind": "categorical",
                "field": {"table": "Sales", "name": "Year"},
                "values": ["last year"],
            },
        ),
    )
    assert message == "Year is a whole number; 'last year' is not"


def test_a_filter_on_a_shape_or_an_unknown_target_is_refused_in_plain_words() -> None:
    assert refuses(load(), visual_filter(TEXT_BOX_LIKE, countries("x"))) == (
        "Text boxes, images and shapes cannot be filtered"
    )
    assert refuses(load(), page_filter("Nowhere", countries("x"))) == (
        "There is no page named 'Nowhere'"
    )


def test_filter_errors_in_a_batch_name_their_step() -> None:
    message = refuses(
        load(),
        visual_filter(COLUMN, countries("Canada")),
        page_filter("Nowhere", countries("x")),
    )
    assert message == "Step 2: There is no page named 'Nowhere'"


# --- review fixes ---------------------------------------------------------------------------


def amount_filter(operator: str, value: Any) -> dict[str, Any]:
    return visual_filter(
        COLUMN,
        {
            "kind": "advanced",
            "field": {"table": "Sales", "name": "Amount"},
            "conditions": [{"operator": operator, "value": value}],
        },
    )


def written_value(document: PbirDocument) -> str:
    comparison = filters_of(document.visual(COLUMN)[1])[0]["filter"]["Where"][0]["Condition"][
        "Comparison"
    ]
    return str(comparison["Right"]["Literal"]["Value"])


@pytest.mark.parametrize(
    ("value", "written"),
    [
        (5, "5M"),
        (1.5, "1.5M"),
        (100, "100M"),
        (2.50, "2.5M"),
        (1e-7, "0M"),  # below the fourth decimal: no exponent, no false precision
        (-1e-7, "0M"),
        (1.23456, "1.2346M"),
        (9e14, "900000000000000M"),
        ("1,250.5", "1250.5M"),
        # The most a float carries of the largest fixed decimal, 922337203685477.5807.
        ("922337203685477.5", "922337203685477.5M"),
        ("-922337203685477.5", "-922337203685477.5M"),
    ],
)
def test_a_fixed_decimal_literal_is_plain_digits_with_at_most_four_decimals(
    value: Any, written: str
) -> None:
    document = load()
    run(document, amount_filter("greater_than", value))
    assert written_value(document) == written


@pytest.mark.parametrize("value", [1e308, -1e308, 1e15, "922337203685478"])
def test_a_fixed_decimal_beyond_its_range_is_refused(value: Any) -> None:
    message = refuses(load(), amount_filter("greater_than", value))
    assert message == (
        f"{value!r} is too large for Amount, a fixed decimal number "
        "(the limit is about 922 trillion)"
    )


def test_a_filter_on_a_wrong_measure_of_a_measure_only_table_names_the_real_problem() -> None:
    spec: dict[str, Any] = {"kind": "categorical", "values": ["x"]}
    spec["field"] = {"table": "# Measures", "name": "Nope"}
    assert refuses(load(), visual_filter(COLUMN, spec)) == (
        "# Measures has no column or measure named 'Nope'"
    )
    spec["field"] = {"table": "Nowhere", "name": "Nope"}
    assert refuses(load(), visual_filter(COLUMN, spec)) == "The model has no table 'Nowhere'"
