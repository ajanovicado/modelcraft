"""Report tests on pinned numbers (``studio/report_tests.py``).

The synthetic report below has every filter kind the renderer compiles: a report filter (a
year range), a page filter (a channel that is not "Closed"), a slicer's saved year, a visual
filter on a column and one on a measure. Under all of them 2024's sales are 850: North 450,
South 250, Web 150 (the Closed store's 999 and 2023's sales are filtered out).
"""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from ai_powerbi_analyst.studio import live_report, report_import
from ai_powerbi_analyst.studio import report_tests as rt
from ai_powerbi_analyst.studio.dax_engine import DaxEngineError, DaxUnsupported, Model
from ai_powerbi_analyst.studio.dax_syntax import DaxParseError
from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
from ai_powerbi_analyst.studio.pbir_document import PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_model import (
    RenameColumn,
    RenameTable,
    SetColumnProperties,
)
from ai_powerbi_analyst.studio.report_edit_ops import (
    CreateMeasure,
    DeleteMeasure,
    RenameMeasure,
    UpdateMeasure,
)
from ai_powerbi_analyst.studio.report_engine import (
    LocalReportEngine,
    ReportModel,
    report_model,
)
from ai_powerbi_analyst.studio.report_import import ImportedReport

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
MEASURES = [
    ("Total Sales", "SUM(Sales[Amount])"),
    ("Units", "SUM(Sales[Qty])"),
    ("Avg Store Sales", "AVERAGEX(ALLSELECTED(Store[StoreName]), [Total Sales])"),
    ("Rank", "RANKX(ALLSELECTED(Store[StoreName]), [Total Sales])"),
    ("Target", "1000"),
]
FORMATS = {("# Measures", "Total Sales"): "\\$#,0", ("# Measures", "Avg Store Sales"): "#,0.00"}


# --- the synthetic report ------------------------------------------------------------------------


def _frames() -> dict[str, pd.DataFrame]:
    sales = [
        ("2022-05-01", 1, 100.0, 1),
        ("2023-03-01", 1, 200.0, 2),
        ("2023-06-01", 2, 300.0, 3),
        ("2024-01-15", 1, 400.0, 4),
        ("2024-02-20", 2, 250.0, 2),
        ("2024-03-10", 3, 150.0, 1),
        ("2024-04-01", 4, 999.0, 9),
        ("2024-05-05", 1, 50.0, 1),
    ]
    calendar = pd.DataFrame({"Date": pd.date_range("2022-01-01", "2024-12-31")})
    calendar["Year"] = calendar["Date"].dt.year
    return {
        "Sales": pd.DataFrame(
            {
                "Date": pd.to_datetime([s[0] for s in sales]),
                "StoreKey": [s[1] for s in sales],
                "Amount": [s[2] for s in sales],
                "Qty": [s[3] for s in sales],
            }
        ),
        "Store": pd.DataFrame(
            {
                "StoreKey": [1, 2, 3, 4],
                "StoreName": ["North", "South", "Web", "Old"],
                "Channel": ["Store", "Store", "Online", "Closed"],
            }
        ),
        "Date": calendar,
    }


def _field(kind: str, table: str, name: str, source: str | None = None) -> dict[str, Any]:
    ref = {"Source": source} if source else {"Entity": table}
    return {kind: {"Expression": {"SourceRef": ref}, "Property": name}}


def _projection(kind: str, table: str, name: str) -> dict[str, Any]:
    return {"field": _field(kind, table, name), "queryRef": f"{table}.{name}"}


def _where(entity: str, condition: dict[str, Any]) -> dict[str, Any]:
    return {
        "Version": 2,
        "From": [{"Name": "a", "Entity": entity, "Type": 0}],
        "Where": [{"Condition": condition}],
    }


def _in(prop: str, literals: list[str]) -> dict[str, Any]:
    return {
        "In": {
            "Expressions": [_field("Column", "", prop, "a")],
            "Values": [[{"Literal": {"Value": v}}] for v in literals],
        }
    }


def _compare(kind: str, prop: str, code: int, literal: str) -> dict[str, Any]:
    return {
        "Comparison": {
            "ComparisonKind": code,
            "Left": _field(kind, "", prop, "a"),
            "Right": {"Literal": {"Value": literal}},
        }
    }


def _filter(
    entity: str, prop: str, condition: dict[str, Any], kind: str = "Column"
) -> dict[str, Any]:
    return {
        "name": f"Filter{abs(hash((entity, prop, json.dumps(condition)))) % 10**20:020d}",
        "field": _field(kind, entity, prop),
        "type": "Advanced",
        "filter": _where(entity, condition),
    }


def _visual(
    name: str,
    visual_type: str,
    wells: dict[str, list[tuple[str, str, str]]],
    box: tuple[int, int, int, int],
    *,
    filters: list[dict[str, Any]] | None = None,
    objects: dict[str, Any] | None = None,
) -> dict[str, Any]:
    x, y, width, height = box
    body: dict[str, Any] = {
        "visualType": visual_type,
        "query": {
            "queryState": {
                role: {"projections": [_projection(*f) for f in fields]}
                for role, fields in wells.items()
            }
        },
    }
    if objects:
        body["objects"] = objects
    raw: dict[str, Any] = {
        "name": name,
        "position": {"x": x, "y": y, "z": 0, "width": width, "height": height},
        "visual": body,
    }
    if filters:
        raw["filterConfig"] = {"filters": filters}
    return raw


TOTAL = ("Measure", "# Measures", "Total Sales")
VISUALS = [
    _visual("card", "cardVisual", {"Values": [TOTAL]}, (0, 0, 300, 150)),
    _visual(
        "avg",
        "cardVisual",
        {"Values": [("Measure", "# Measures", "Avg Store Sales")]},
        (300, 0, 200, 100),
    ),
    _visual(
        "chart",
        "clusteredColumnChart",
        {"Category": [("Column", "Store", "StoreName")], "Y": [TOTAL]},
        (0, 200, 400, 300),
        filters=[_filter("Store", "Channel", _in("Channel", ["'Store'"]))],
    ),
    _visual(
        "table",
        "tableEx",
        {
            "Values": [
                ("Column", "Store", "StoreName"),
                TOTAL,
                ("Measure", "# Measures", "Rank"),
            ]
        },
        (400, 200, 400, 300),
        filters=[_filter("# Measures", "Rank", _compare("Measure", "Rank", 4, "2L"), "Measure")],
    ),
    _visual(
        "slicer",
        "slicer",
        {"Values": [("Column", "Date", "Year")]},
        (800, 0, 200, 60),
        objects={
            "general": [
                {
                    "properties": {
                        "filter": {"filter": _where("Date", _compare("Column", "Year", 0, "2024L"))}
                    }
                }
            ]
        },
    ),
    _visual(
        "strict",
        "cardVisual",
        {"Values": [TOTAL]},
        (0, 600, 100, 50),
        filters=[
            _filter(
                "# Measures", "Total Sales", _compare("Measure", "Total Sales", 1, "0L"), "Measure"
            )
        ],
    ),
    _visual(
        "atleast",
        "cardVisual",
        {"Values": [("Measure", "# Measures", "Units")]},
        (100, 600, 100, 50),
        filters=[_filter("# Measures", "Units", _compare("Measure", "Units", 2, "1L"), "Measure")],
    ),
    _visual(
        "either",
        "cardVisual",
        {"Values": [TOTAL]},
        (200, 600, 100, 50),
        filters=[
            _filter(
                "Store",
                "Channel",
                {
                    "Or": {
                        "Left": _compare("Column", "Channel", 0, "'Store'"),
                        "Right": _compare("Column", "Channel", 0, "'Online'"),
                    }
                },
            )
        ],
    ),
    _visual(
        "two",
        "tableEx",
        {"Values": [("Column", "Store", "StoreName"), TOTAL, ("Measure", "# Measures", "Units")]},
        (300, 600, 100, 50),
    ),
    {
        "name": "text",
        "position": {"x": 0, "y": 700, "width": 50, "height": 20},
        "visual": {"visualType": "textbox"},
    },
]
PAGE_FILTERS = [_filter("Store", "Channel", {"Not": {"Expression": _in("Channel", ["'Closed'"])}})]
REPORT_FILTERS = [
    _filter(
        "Date",
        "Year",
        {
            "And": {
                "Left": _compare("Column", "Year", 2, "2023L"),
                "Right": _compare("Column", "Year", 4, "2024L"),
            }
        },
    )
]


def _pbix(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "Report/definition/report.json",
            json.dumps({"filterConfig": {"filters": REPORT_FILTERS}}),
        )
        archive.writestr("Report/definition/pages/pages.json", json.dumps({"pageOrder": ["p1"]}))
        archive.writestr(
            "Report/definition/pages/p1/page.json",
            json.dumps(
                {"name": "p1", "displayName": "Sales", "filterConfig": {"filters": PAGE_FILTERS}}
            ),
        )
        for visual in VISUALS:
            archive.writestr(
                f"Report/definition/pages/p1/visuals/{visual['name']}/visual.json",
                json.dumps(visual),
            )
        archive.writestr("DataModel", b"fixture-decoder-substituted")
    return path


def _extract(frames: dict[str, pd.DataFrame]) -> Any:
    def model(_source: Path, destination: Path) -> dict[str, Any]:
        tables = []
        for index, (name, frame) in enumerate(frames.items()):
            frame.to_parquet(destination / f"table-{index}.parquet", index=False)
            tables.append({"name": name, "file": f"table-{index}.parquet", "rows": len(frame)})
        return {
            "tables": tables,
            # The decoder supplies current Import metadata for these exact rows.
            # Missing facts must remain unavailable, rather than bypassing that guard.
            "facts": {
                "version": 1,
                "tables": [
                    {"name": name, "source": "m", "calculated": False}
                    for name in [*frames, "# Measures"]
                ],
                "columns": [
                    {
                        "table": name,
                        "name": column,
                        "kind": "data",
                        "dataType": (
                            9
                            if pd.api.types.is_datetime64_any_dtype(dtype)
                            else 11
                            if pd.api.types.is_bool_dtype(dtype)
                            else 6
                            if pd.api.types.is_integer_dtype(dtype)
                            else 8
                            if pd.api.types.is_numeric_dtype(dtype)
                            else 2
                        ),
                    }
                    for name, frame in frames.items()
                    for column, dtype in frame.dtypes.items()
                ],
                "measures": [
                    {"table": "# Measures", "name": name, "expression": expression}
                    for name, expression in MEASURES
                ],
                "relationships": [
                    {
                        "fromTable": many,
                        "fromColumn": column,
                        "toTable": one,
                        "toColumn": column,
                        "active": True,
                        "cardinality": "M:1",
                        "crossFilter": "single",
                    }
                    for many, one, column in (
                        ("Sales", "Store", "StoreKey"),
                        ("Sales", "Date", "Date"),
                    )
                ],
                "partitions": [
                    {"table": name, "source": "m", "mode": "import"}
                    for name in [*frames, "# Measures"]
                ],
            },
            "schema": [
                {"TableName": name, "ColumnName": column, "PandasDataType": str(dtype)}
                for name, frame in frames.items()
                for column, dtype in frame.dtypes.items()
            ],
            "measures": [
                {"TableName": "# Measures", "Name": name, "Expression": expression}
                for name, expression in MEASURES
            ],
            "relationships": [
                {
                    "FromTableName": many,
                    "FromColumnName": column,
                    "ToTableName": one,
                    "ToColumnName": column,
                    "IsActive": 1,
                    "Cardinality": "M:1",
                    "CrossFilteringBehavior": "Single",
                }
                for many, one, column in (("Sales", "Store", "StoreKey"), ("Sales", "Date", "Date"))
            ],
        }

    return model


class Fixture:
    def __init__(self, report: ImportedReport, snapshot: Any, model: Model) -> None:
        self.report, self.snapshot, self.model = report, snapshot, model

    def evaluator(self) -> rt.LocalEvaluator:
        return rt.LocalEvaluator(self.report, model=self.model)

    def pin(self, visual: str, point: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return rt.pin_from_visual(None, "p1", visual, point, report=self.report, now=NOW, **kwargs)

    def value(self, test: rt.TestDefinition) -> Any:
        return self.evaluator()(test)


@pytest.fixture
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    monkeypatch.setattr(report_import, "extract_model", _extract(_frames()))
    monkeypatch.setattr(live_report, "model_formats", lambda *_: dict(FORMATS))
    monkeypatch.setattr(live_report, "page_style", lambda *_: ({}, {}))
    state = tmp_path / "state"
    state.mkdir()
    report, snapshot = report_import.import_report(
        str(_pbix(tmp_path / "Sales.pbix")), state, "ws-1", StudioIngestionService(state)
    )
    assert snapshot is not None
    return Fixture(report, snapshot, report_model(report, snapshot))


CONTEXT = [
    rt.TestFilter(table="Store", column="Channel", op="not_in", values=["Closed"]),
    rt.TestFilter(table="Date", column="Year", op="between", values=[2023.0, 2024.0]),
    rt.TestFilter(table="Date", column="Year", op="in", values=[2024.0]),
]


# --- pinning -----------------------------------------------------------------------------------


def test_a_card_is_pinned_with_every_filter_the_canvas_applies(fx: Fixture) -> None:
    test = fx.pin("card", value=850.0)
    # Page filter, report filter, then the slicer: the renderer's order.
    assert test.filters == CONTEXT
    assert test.measure == rt.MeasureRef(table="# Measures", name="Total Sales")
    assert test.name == "Total Sales"
    assert test.format == "\\$#,0"
    assert test.origin == rt.Origin(page="p1", visual="card")
    assert test.source == "pinned" and test.created_at == "2026-09-29T12:00:00+00:00"
    assert test.expect == rt.ExpectEquals(equals=850.0)
    assert test.id == rt.definition_id(test.measure, None, CONTEXT) == fx.pin("card", value=1.0).id
    assert rt.query_of(test) == (
        "EVALUATE SELECTCOLUMNS(CALCULATETABLE(CALCULATETABLE(CALCULATETABLE("
        'ROW("M0", [Total Sales]), '
        "KEEPFILTERS(FILTER(ALL('Store'), NOT('Store'[Channel] IN {\"Closed\"})))), "
        "KEEPFILTERS(FILTER(ALL('Date'), ('Date'[Year] >= 2023.0) && ('Date'[Year] <= 2024.0)))), "
        "KEEPFILTERS(FILTER(ALL('Date'), 'Date'[Year] IN {2024.0}))), \"Value\", [M0])"
    )
    assert fx.value(test) == 850.0
    assert rt.describe(test) == (
        "Total Sales should be $850 when Channel is not Closed, Year is between 2023 and 2024 "
        "and Year is 2024."
    )


def test_a_card_under_allselected_matches_the_canvas(fx: Fixture) -> None:
    payload = live_report.render_page(
        LocalReportEngine(fx.model), fx.report, "p1", live_report.LiveReportRequest()
    )
    shown = next(v for v in payload["visuals"] if v["id"] == "avg")["rows"][0][0]
    test = fx.pin("avg", value=shown)
    assert shown == pytest.approx(850 / 3, abs=1e-9)
    assert rt.run_tests([test], fx.evaluator())[0].status == "passed"


def test_a_chart_point_is_its_row_of_the_visual(fx: Fixture) -> None:
    test = fx.pin("chart", {"Store.StoreName": "North"}, value=450.0)
    assert test.name == "Total Sales for North"
    assert test.filters == [
        rt.TestFilter(table="Store", column="StoreName", op="in", values=["North"], point=True),
        *CONTEXT[:2],
        rt.TestFilter(table="Store", column="Channel", op="in", values=["Store"]),
        CONTEXT[2],
    ]
    assert fx.value(test) == 450.0
    assert rt.describe(test) == (
        "Total Sales for North should be $450 when Channel is not Closed, Year is between 2023 "
        "and 2024, Channel is Store and Year is 2024."
    )


def test_a_table_row_keeps_allselected_as_the_visual_reads_it(fx: Fixture) -> None:
    """South ranks 2nd of the stores the table shows. As a row of SUMMARIZECOLUMNS the rank
    sees every store ALLSELECTED returns; a filter on South would make it rank 1st of one."""
    test = fx.pin("table", {"Store.StoreName": "South"}, value=2.0, measure="Rank")
    # The table's filter on [Rank] only decides which rows show: it is not part of the test.
    assert not any(f.measure for f in test.filters)
    assert "FILTER(SUMMARIZECOLUMNS('Store'[StoreName],\"M0\", [Rank]), " in rt.query_of(test)
    assert fx.value(test) == 2.0
    as_filter = test.model_copy(
        update={"filters": [test.filters[0].model_copy(update={"point": False}), *CONTEXT]}
    )
    assert fx.value(as_filter) == 1.0


def test_a_cards_measure_filter_is_kept_and_a_strict_one_is_refused(fx: Fixture) -> None:
    test = fx.pin("atleast", value=8)
    # Visual filters come after the page's and before the slicers', as the renderer's plan.
    assert test.filters == [
        *CONTEXT[:2],
        rt.TestFilter(
            table="# Measures", column="Units", op="between", values=[1.0, None], measure=True
        ),
        CONTEXT[2],
    ]
    # Wrapped where the renderer wraps it: inside the slicer's filter, outside the page's.
    assert rt.query_of(test).startswith(
        "EVALUATE SELECTCOLUMNS(CALCULATETABLE(FILTER(CALCULATETABLE(CALCULATETABLE("
        'ROW("M0", [Units]), '
    )
    assert "<= 2024.0)))), [Units] >= 1.0), KEEPFILTERS(" in rt.query_of(test)
    assert fx.value(test) == 8
    with pytest.raises(rt.PinError, match="ranges that include their ends"):
        fx.pin("strict", value=850.0)
    with pytest.raises(rt.PinError, match="combines conditions"):
        fx.pin("either", value=850.0)


@pytest.mark.parametrize(
    ("visual", "point", "kwargs", "message"),
    [
        ("text", None, {}, "Pin a number from a card, a chart or a table"),
        ("slicer", None, {}, "Pin a number from a card, a chart or a table"),
        ("nowhere", None, {}, "That visual is not on this page"),
        ("chart", None, {}, "Choose a data point of this visual"),
        ("chart", {"Store.Channel": "Store"}, {}, "Say which StoreName the number is for"),
        ("two", {"Store.StoreName": "North"}, {}, r"Say which number to pin: .*\[Units\]"),
        ("two", {"Store.StoreName": "North"}, {"measure": "StoreName"}, "is a category"),
        ("card", None, {"measure": "Nope"}, "The model has no measure 'Nope'"),
    ],
)
def test_pinning_says_why_it_cannot(
    fx: Fixture, visual: str, point: Any, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(rt.PinError, match=message):
        fx.pin(visual, point, value=1.0, **kwargs)


def test_the_callers_document_must_still_have_the_visual(fx: Fixture) -> None:
    archive, _ = report_import.report_archive(fx.report)
    document = PbirDocument(PbixPackage(archive))
    assert rt.pin_from_visual(document, "p1", "card", None, value=850.0, report=fx.report).name
    del document.pages["p1"].visuals["card"]
    with pytest.raises(rt.PinError, match="no longer on this page"):
        rt.pin_from_visual(document, "p1", "card", None, value=850.0, report=fx.report)


def test_suggestions_are_the_cards_then_charts_up_to_five(fx: Fixture) -> None:
    payload = live_report.render_page(
        LocalReportEngine(fx.model), fx.report, "p1", live_report.LiveReportRequest()
    )
    suggested = rt.suggest_tests(None, [payload], report=fx.report, now=NOW)
    # Big cards first ("strict" and "either" cannot be pinned), then charts and tables fill
    # the list up to five: their first row (North, in both).
    assert [t.name for t in suggested] == [
        "Total Sales",
        "Avg Store Sales",
        "Units",
        "Total Sales for North",
        "Total Sales for North",
    ]
    assert [t.origin.visual for t in suggested if t.origin] == [
        "card",
        "avg",
        "atleast",
        "chart",
        "table",
    ]
    assert all(r.status == "passed" for r in rt.run_tests(suggested, fx.evaluator()))
    assert rt.suggest_tests(None, [payload], report=fx.report, limit=2, now=NOW) == suggested[:2]


# --- running -----------------------------------------------------------------------------------


def _test(expect: rt.Expectation, fmt: str | None = None, name: str = "Sales") -> rt.TestDefinition:
    return rt.TestDefinition(
        id="t1",
        name=name,
        measure=rt.MeasureRef(table="# Measures", name="Total Sales"),
        expect=expect,
        created_at="2026-09-29T12:00:00+00:00",
        source="user",
        format=fmt,
    )


@pytest.mark.parametrize(
    ("expect", "value", "passed", "message"),
    [
        (rt.ExpectEquals(equals=850.0), 850.0 + 1e-7, True, "Sales is still $850."),
        (rt.ExpectEquals(equals=850.0), 851.0, False, "Sales is $851 now instead of $850."),
        (rt.ExpectEquals(equals=850.0), 850.4, False, "Sales is 850.4 now instead of 850."),
        (rt.ExpectEquals(equals=850.0, tolerance_abs=0.5), 850.4, True, "Sales is still $850."),
        (rt.ExpectEquals(equals=850.0, tolerance_rel=0.01), 858.0, True, "Sales is still $858."),
        (rt.ExpectEquals(equals=850.0), None, False, "Sales is blank now instead of $850."),
        (rt.ExpectEquals(equals=None), None, True, "Sales is still blank."),
        (rt.ExpectEquals(equals=None), 3.0, False, "Sales is $3 now, not blank."),
        (rt.ExpectEquals(equals="North"), "North", True, "Sales is still North."),
        (rt.ExpectEquals(equals="North"), "South", False, "Sales is South now instead of North."),
        (rt.ExpectEquals(equals=True), 1, False, "Sales is $1 now instead of true."),
        (
            rt.ExpectBetween(between=(800, 900)),
            850,
            True,
            "Sales is still between $800 and $900: $850.",
        ),
        (rt.ExpectBetween(between=(900, None)), 850, False, "Sales is $850, not at least $900."),
        (rt.ExpectBetween(between=(None, 900)), None, False, "Sales is blank, not at most $900."),
        (rt.ExpectNotBlank(not_blank=True), 0.0, True, "Sales is still not blank ($0)."),
        (rt.ExpectNotBlank(not_blank=True), None, False, "Sales is blank now."),
    ],
)
def test_values_are_compared_with_the_expectation(
    expect: rt.Expectation, value: Any, passed: bool, message: str
) -> None:
    assert rt.compare(_test(expect, "\\$#,0"), value) == (passed, message)


@pytest.mark.parametrize(
    ("value", "fmt", "shown"),
    [
        (0.467576791808874, "0.00%;-0.00%;0.00%", "46.76%"),
        (73514.7960347328, "\\$#,0.00;(\\$#,0.00);\\$#,0.00", "$73,514.80"),
        (18914632.251, None, "18.91M"),
        (1234567890.0, "\\$#,0", "$1.23bn"),
        (19, "0", "19"),
        (12345.0, None, "12,345"),
        (0.243847369031617, None, "0.243847"),
        (None, None, "blank"),
        ("North", None, "North"),
        (False, None, "false"),
    ],
)
def test_values_are_shown_as_the_report_formats_them(value: Any, fmt: Any, shown: str) -> None:
    assert rt.shown_value(value, fmt) == shown


def test_a_run_says_not_checked_and_failed_for_the_right_reasons() -> None:
    tests = [
        _test(rt.ExpectEquals(equals=1.0), name=f"T{i}").model_copy(update={"id": f"t{i}"})
        for i in range(7)
    ]
    parse = DaxEngineError("Measure [X] has a syntax error: bad")
    parse.__cause__ = DaxParseError("bad")
    outcomes: dict[str, Any] = {
        "t0": 1.0,
        "t1": rt.NotComputable("Power BI applies the calculation group 'Time'"),
        "t2": DaxUnsupported("Studio's engine does not implement PATH"),
        "t3": rt.TestBroken("The measure [X] is no longer in the model"),
        "t4": DaxEngineError("Column 'Amount' in table 'Sales' cannot be found."),
        "t5": parse,
        "t6": 2.0,
    }
    prepared: list[list[str]] = []

    class Fake:
        label = "Fake engine"

        def prepare(self, batch: list[rt.TestDefinition]) -> None:
            prepared.append([t.id for t in batch])

        def __call__(self, test: rt.TestDefinition) -> Any:
            outcome = outcomes[test.id]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    results = rt.run_tests(tests, Fake())
    assert prepared == [[f"t{i}" for i in range(7)]]
    assert [(r.status, r.message) for r in results] == [
        ("passed", "T0 is still 1."),
        ("not_checked", "T1 was not checked: Power BI applies the calculation group 'Time'."),
        ("not_checked", "T2 was not checked: Studio's engine does not implement PATH."),
        ("failed", "T3 cannot be calculated: The measure [X] is no longer in the model."),
        (
            "failed",
            "T4 cannot be calculated: Power BI would show an error (Column 'Amount' in table "
            "'Sales' cannot be found).",
        ),
        ("not_checked", "T5 was not checked: Measure [X] has a syntax error: bad."),
        ("failed", "T6 is 2 now instead of 1."),
    ]
    assert {r.engine for r in results} == {"Fake engine"}
    assert rt.describe_results(results) == "\n".join(
        [
            "1 of 7 report tests pass, 3 fail and 3 were not checked.",
            "- T3 cannot be calculated: The measure [X] is no longer in the model.",
            "- T4 cannot be calculated: Power BI would show an error (Column 'Amount' in table "
            "'Sales' cannot be found).",
            "- T6 is 2 now instead of 1.",
            "- T1 was not checked: Power BI applies the calculation group 'Time'.",
            "- T2 was not checked: Studio's engine does not implement PATH.",
            "- T5 was not checked: Measure [X] has a syntax error: bad.",
            "- T0 is still 1.",
        ]
    )
    assert rt.describe_results([]) == "This report has no tests yet."
    assert rt.describe_results(results[:1]) == "The report test passes.\n- T0 is still 1."


def test_the_gate_blocks_only_what_the_edit_broke() -> None:
    def result(test_id: str, status: rt.Status) -> rt.TestResult:
        return rt.TestResult(
            id=test_id,
            name=test_id.upper(),
            status=status,
            expected=rt.ExpectEquals(equals=1.0),
            message=f"{test_id.upper()} is 2 now instead of 1.",
        )

    before = [
        result("a", "passed"),
        result("b", "failed"),
        result("c", "not_checked"),
        result("d", "passed"),
        result("e", "passed"),
    ]
    after = [
        result("a", "failed"),
        result("b", "failed"),
        result("c", "failed"),
        result("d", "not_checked"),
        result("e", "passed"),
        result("f", "failed"),
    ]
    assert [r.id for r in rt.gate(before, after)] == ["a"]
    with pytest.raises(rt.ReportTestsBroken) as raised:
        rt.check_gate(before, after)
    assert raised.value.kind == "report_tests_broken"
    assert raised.value.detail == (
        "This change would break a report test:\n- A is 2 now instead of 1.\nApply it anyway?"
    )
    assert [r.id for r in raised.value.results] == ["a"]
    assert [r.id for r in rt.check_gate(before, after, confirmed=True)] == ["a"]
    assert rt.check_gate(before, before) == []


# --- edits ----------------------------------------------------------------------------------


def _run(fx: Fixture, tests: list[rt.TestDefinition], report: ImportedReport) -> list[Any]:
    return rt.run_tests(tests, rt.LocalEvaluator(report, fx.snapshot))


def test_a_measure_edit_is_gated_before_power_bi_is_opened(fx: Fixture) -> None:
    tests = [
        fx.pin("card", value=850.0),
        fx.pin("chart", {"Store.StoreName": "North"}, value=450.0),
    ]
    before = rt.run_tests(tests, fx.evaluator())
    assert [r.status for r in before] == ["passed", "passed"]
    edit = [UpdateMeasure(name="Total Sales", expression="SUM(Sales[Amount]) * 1.1")]
    projected = rt.projected_report(fx.report, edit)
    assert projected is not None
    after = _run(fx, tests, projected)
    broken = rt.gate(before, after)
    assert [r.message for r in broken] == [
        "Total Sales is $935 now instead of $850.",
        "Total Sales for North is $495 now instead of $450.",
    ]
    # A rename is a refactor: the tests follow it and keep passing.
    rename = [RenameMeasure(name="Total Sales", new_name="Revenue")]
    renamed = rt.follow_renames(tests, rename)
    assert [t.measure.name for t in renamed if t.measure] == ["Revenue", "Revenue"]
    assert [t.id for t in renamed] == [t.id for t in tests]
    moved = rt.projected_report(fx.report, rename)
    assert moved is not None
    assert rt.gate(before, _run(fx, renamed, moved)) == []
    # A deleted measure fails its tests with words the user knows.
    gone = rt.projected_report(fx.report, [DeleteMeasure(name="Total Sales")])
    assert gone is not None
    assert _run(fx, tests[:1], gone)[0].message == (
        "Total Sales cannot be calculated: The measure [Total Sales] is no longer in the model."
    )
    created = rt.projected_report(
        fx.report, [CreateMeasure(table="# Measures", name="Double", expression="[Units] * 2")]
    )
    assert created is not None and created.measures[-1]["Name"] == "Double"
    # Beyond measures, only Power BI's saved model can say what a change does.
    change = SetColumnProperties(table="Sales", name="Qty", hidden=True)
    assert rt.projected_report(fx.report, [change]) is None


def test_renamed_columns_and_tables_are_followed() -> None:
    test = rt.new_user_test(
        "Doubled",
        expect=rt.ExpectEquals(equals=3698.0),
        expression="CALCULATE([Total Sales], 'Store'[Channel] <> \"Closed\") * 2",
        filters=[rt.TestFilter(table="Store", column="Channel", op="in", values=["Store"])],
        now=NOW,
    )
    renamed = rt.follow_renames(
        [test],
        [
            RenameColumn(table="Store", name="Channel", new_name="Route"),
            RenameTable(name="Store", new_name="Shop"),
            RenameMeasure(name="Total Sales", new_name="Revenue"),
        ],
    )[0]
    assert renamed.filters == [
        rt.TestFilter(table="Shop", column="Route", op="in", values=["Store"])
    ]
    assert renamed.expression == "CALCULATE([Revenue], 'Shop'[Route] <> \"Closed\") * 2"
    assert renamed.id == test.id
    assert rt.follow_renames([test], [DeleteMeasure(name="Units")]) == [test]


def test_a_typed_expression_is_computed_under_its_filters(fx: Fixture) -> None:
    test = rt.new_user_test(
        "Doubled 2024",
        expect=rt.ExpectEquals(equals=3698.0),
        expression="[Total Sales] * 2",
        filters=[rt.TestFilter(table="Date", column="Year", op="in", values=[2024])],
        now=NOW,
    )
    # 2024 with the Closed store: 400 + 250 + 150 + 999 + 50 = 1849.
    assert rt.run_tests([test], fx.evaluator())[0].status == "passed"
    assert rt.describe(test) == "Doubled 2024 should be 3,698 when Year is 2024."


def test_what_the_model_cannot_build_is_not_checked(fx: Fixture) -> None:
    broken = ReportModel(
        fx.model.tables.values(),
        fx.model.relationships,
        fx.model.measures.values(),
        unavailable_measures={"total sales": "Power BI reports a problem with [Total Sales]."},
    )
    card = fx.pin("card", value=850.0)
    (declined,) = rt.run_tests([card], rt.LocalEvaluator(fx.report, model=broken))
    assert (declined.status, declined.message) == (
        "not_checked",
        "Total Sales was not checked: Power BI reports a problem with [Total Sales].",
    )
    missing = fx.pin("card", value=850.0).model_copy(
        update={"filters": [rt.TestFilter(table="Store", column="Gone", op="in", values=["x"])]}
    )
    with pytest.raises(rt.TestBroken, match=r"'Store'\[Gone\] this test filters on"):
        fx.evaluator()(missing)
    # A model Studio cannot build leaves every test not checked, with the reason.
    unbuilt = rt.LocalEvaluator(fx.report, fx.snapshot)
    unbuilt._failure = "a two-way relationship"
    (result,) = rt.run_tests([fx.pin("card", value=850.0)], unbuilt)
    assert result.message == (
        "Total Sales was not checked: Studio cannot calculate this report's model (a two-way "
        "relationship)."
    )


def test_desktop_answers_are_read_in_one_call() -> None:
    tests = [
        _test(rt.ExpectEquals(equals=850.0)).model_copy(update={"id": "a"}),
        _test(rt.ExpectEquals(equals="02134"), name="Code").model_copy(update={"id": "b"}),
        _test(rt.ExpectEquals(equals=1.0), name="Bad").model_copy(update={"id": "c"}),
        _test(rt.ExpectEquals(equals=1.0), name="Blank").model_copy(update={"id": "d"}),
    ]
    calls: list[dict[str, str]] = []

    def run(queries: Mapping[str, str]) -> dict[str, Any]:
        calls.append(dict(queries))
        return {
            "a": {"ok": True, "rows": [{"[Value]": "850.00"}]},
            "b": {"ok": True, "rows": [{"[Value]": "02134"}]},
            "c": {"ok": False, "error": "The value for 'Total Sales' cannot be determined."},
            "d": {"ok": True, "rows": []},
        }

    results = rt.run_tests(tests, rt.QueryEvaluator(run))
    assert len(calls) == 1 and list(calls[0]) == ["a", "b", "c", "d"]
    assert calls[0]["a"] == rt.query_of(tests[0])
    assert [(r.status, r.value, r.engine) for r in results] == [
        ("passed", 850.0, "Power BI Desktop"),
        ("passed", "02134", "Power BI Desktop"),
        ("failed", None, "Power BI Desktop"),
        ("failed", None, "Power BI Desktop"),
    ]
    assert results[2].message == (
        "Bad cannot be calculated: Power BI reports an error in this number (The value for "
        "'Total Sales' cannot be determined)."
    )

    def down(_queries: Mapping[str, str]) -> dict[str, Any]:
        raise RuntimeError("Power BI Desktop's model did not answer in time")

    (result,) = rt.run_tests(tests[:1], rt.QueryEvaluator(down))
    assert (result.status, result.message) == (
        "not_checked",
        "Sales was not checked: Power BI Desktop did not answer (Power BI Desktop's model did "
        "not answer in time).",
    )


# --- definitions and the store ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"op": "between", "values": [1]}, "lowest and a highest"),
        ({"op": "between", "values": [None, None]}, "lowest and a highest"),
        ({"op": "between", "values": [5, 1]}, "above its highest"),
        ({"op": "in", "values": ["a", "b"], "point": True}, "exactly one value"),
        ({"op": "in", "values": ["2024-01-01T10:00:00"], "date": True}, "YYYY-MM-DD"),
        ({"op": "in", "values": []}, "at least 1 item"),
    ],
)
def test_filters_hold_only_what_a_query_can_use(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        rt.TestFilter.model_validate({"table": "T", "column": "C", **data})


def test_a_definition_computes_one_thing() -> None:
    base = {
        "id": "t1",
        "name": "X",
        "expect": {"equals": None},
        "created_at": "2026-09-29T12:00:00+00:00",
        "source": "user",
    }
    with pytest.raises(ValidationError, match="either a measure or a DAX expression"):
        rt.TestDefinition.model_validate(base)
    with pytest.raises(ValidationError, match="not a query"):
        rt.TestDefinition.model_validate({**base, "expression": 'EVALUATE ROW("a", 1)'})
    with pytest.raises(ValidationError, match=r"Expected \) at the end of the formula"):
        rt.TestDefinition.model_validate({**base, "expression": "SUM(Sales[Amount]"})
    with pytest.raises(ValidationError, match="Only a formula the user typed"):
        rt.TestDefinition.model_validate({**base, "source": "pinned", "expression": "1"})
    parsed = rt.TestDefinition.model_validate(
        {**base, "measure": {"table": "M", "name": "Sales"}, "expect": {"between": [1, None]}}
    )
    assert parsed.expect == rt.ExpectBetween(between=(1.0, None))
    assert rt.TestDefinition.model_validate(
        {**base, "expression": "1", "expect": {"not_blank": True}}
    ).expect == rt.ExpectNotBlank(not_blank=True)


def test_the_store_keeps_tests_per_report_and_replaces_the_file_whole(tmp_path: Path) -> None:
    store = rt.TestStore(tmp_path)
    (tmp_path / "reports" / "report-1").mkdir(parents=True)
    first = _test(rt.ExpectEquals(equals=850.0))
    second = first.model_copy(update={"id": "t2", "name": "Other"})
    assert store.tests("report-1") == [] and store.last_run("report-1") is None
    store.add("report-1", first)
    store.add("report-1", second)
    with pytest.raises(rt.TestStoreError, match="already has a test \\('Sales'\\)"):
        store.add("report-1", first)
    store.replace("report-1", first.model_copy(update={"name": "Renamed"}))
    run = rt.TestRun(
        revision="r0001",
        hash="0" * 64,
        engine="Studio's DAX engine",
        ran_at="2026-09-29T12:00:00+00:00",
        results=rt.run_tests([first], lambda _t: 850.0),
    )
    store.record_run("report-1", run)
    assert [t.name for t in store.tests("report-1")] == ["Renamed", "Other"]
    assert store.last_run("report-1") == run
    assert store.remove("report-1", "t2") is True and store.remove("report-1", "t2") is False
    path = tmp_path / "reports" / "report-1" / "tests.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 1 and [t["id"] for t in saved["tests"]] == ["t1"]
    # No temporary file is left beside it (the lock file may or may not remain).
    assert {p.name for p in path.parent.iterdir()} - {"tests.json.lock"} == {"tests.json"}
    with pytest.raises(rt.TestStoreError, match="not imported"):
        store.add("report-2", first)
    with pytest.raises(rt.TestStoreError, match="not a report Studio knows"):
        store.tests("../escape")
    with pytest.raises(rt.TestStoreError, match="no such test"):
        store.replace("report-1", second)


def test_the_store_has_a_limit_and_never_resets_a_damaged_file(tmp_path: Path) -> None:
    store = rt.TestStore(tmp_path)
    folder = tmp_path / "reports" / "report-1"
    folder.mkdir(parents=True)
    base = _test(rt.ExpectEquals(equals=1.0))
    store.replace_all(
        "report-1", [base.model_copy(update={"id": f"t{i}"}) for i in range(rt.MAX_TESTS)]
    )
    with pytest.raises(rt.TestStoreError, match="at most 200 tests"):
        store.add("report-1", base.model_copy(update={"id": "extra"}))
    (folder / "tests.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(rt.TestStoreError, match="damaged"):
        store.add("report-1", base.model_copy(update={"id": "extra"}))
    assert (folder / "tests.json").read_text(encoding="utf-8") == "{not json"


# --- the real Agents Performance dashboard (read-only; skipped when absent) --------------------

AGENTS = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"
TOP = [
    rt.TestFilter(table="Top-Bottom-N", column="Top-Bottom-N", op="in", values=[6.0]),
    rt.TestFilter(
        table="Actuals-vs-PctChange", column="Top-Bottom-Field", op="in", values=["MOM% Sales"]
    ),
]


@pytest.fixture(scope="module")
def agents(tmp_path_factory: pytest.TempPathFactory) -> tuple[ImportedReport, Any, Model, Any]:
    """A scratch import of a copy of the real file (about 25 s: the model is decoded)."""
    if not AGENTS.exists():
        pytest.skip("the real Agents Performance report is absent")
    root = tmp_path_factory.mktemp("agents")
    copy = root / "agents.pbix"
    shutil.copyfile(AGENTS, copy)
    state = root / "state"
    state.mkdir()
    report, snapshot = report_import.import_report(
        str(copy), state, "ws-agents", StudioIngestionService(state)
    )
    assert snapshot is not None
    model = report_model(report, snapshot)
    payload = live_report.render_page(
        LocalReportEngine(model), report, report.pages[0].id, live_report.LiveReportRequest()
    )
    return report, snapshot, model, payload


def test_the_agents_headline_numbers_are_suggested_and_pass(
    agents: tuple[ImportedReport, Any, Model, Any],
) -> None:
    report, _, model, payload = agents
    suggested = rt.suggest_tests(None, [payload], report=report, now=NOW)
    assert [(t.name, t.measure.name if t.measure else None) for t in suggested] == [
        ("Agents with Positive Change %", "Employees with Positive Change %"),
        ("Agents Avg MTD Sales", "Employees Avg MTD Sales"),
        ("Agents Avg MTD % Change", "Employees Avg MTD % Change"),
        ("Number of Agents Sold above 100K", "Number of Employees selling more than 100K MTD"),
        ("TopN/BottomN", "dummy"),
        ("Agents with Positive Change %, PM", "Number of Employees with Positive change PM"),
        ("Agents Avg MTD Sales, PM", "Employees Avg MTD Sales PM"),
        ("Agents Avg MTD % Change, PM", "Employees Avg MTD % change PM"),
    ]
    assert all(t.filters == TOP for t in suggested)
    expected = [
        0.467576791808874,
        73514.7960347328,
        0.353055854189281,
        19,
        18914632.251,
        0.37542662116041,
        72193.2528664122,
        0.243847369031617,
    ]
    for test, value in zip(suggested, expected, strict=True):
        assert isinstance(test.expect, rt.ExpectEquals)
        assert test.expect.equals == pytest.approx(value, rel=1e-12)
    results = rt.run_tests(suggested, rt.LocalEvaluator(report, model=model))
    assert [r.shown for r in results] == [
        "46.76%",
        "$73,514.80",
        "35.31%",
        "19",
        "18.91M",
        "37.54%",
        "72,193.3",
        "0.243847",
    ]
    text = rt.describe_results(results).splitlines()
    assert text[:3] == [
        "All 8 report tests pass.",
        "- Agents with Positive Change % is still 46.76%.",
        "- Agents Avg MTD Sales is still $73,514.80.",
    ]
    assert len(text) == 9


def test_agents_points_and_an_edit_to_their_base_measure(
    agents: tuple[ImportedReport, Any, Model, Any],
) -> None:
    report, snapshot, model, _ = agents
    page = report.pages[0].id
    others = rt.pin_from_visual(
        None,
        page,
        "b404a461ede7604a2774",
        {"Clustered Employees.Clustered Employees": "Others"},
        value=18998521.5511,
        report=report,
    )
    row = rt.pin_from_visual(
        None,
        page,
        "e82e02f93055d6b0b544",
        {"DimEmployee.EmployeeKey": 247.0},
        value=7.13320204322304,
        report=report,
        measure="vs Previous Month",
    )
    card = rt.pin_from_visual(
        None, page, "2202e311b1493069e4eb", None, value=73514.7960347328, report=report
    )
    # The table's filter on its rank measure is left out: the row's own number is tested.
    assert row.filters[1:] == TOP and not any(f.measure for f in row.filters)
    tests = [others, row, card]
    before = rt.run_tests(tests, rt.LocalEvaluator(report, model=model))
    assert [r.status for r in before] == ["passed", "passed", "passed"]
    assert rt.describe(others) == (
        "TopN/BottomN/ MTD Sales for Others should be 19.00M when Top-Bottom-N is 6 and "
        "Top-Bottom-Field is MOM% Sales."
    )
    measures = {m["Name"]: m["Expression"] for m in report.measures}
    doubled = f"({measures['Total Sales']}) * 2"
    projected = rt.projected_report(report, [UpdateMeasure(name="Total Sales", expression=doubled)])
    assert projected is not None
    after = rt.run_tests(tests, rt.LocalEvaluator(projected, snapshot))
    broken = rt.gate(before, after)
    assert [r.id for r in broken] == [others.id, card.id]
    assert broken[1].message == "Agents Avg MTD Sales is $147,029.59 now instead of $73,514.80."
    # [vs Previous Month] is a ratio of this month to last: doubling both leaves it as it is.
    assert after[1].status == "passed"
