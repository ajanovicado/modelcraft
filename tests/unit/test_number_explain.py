"""Investigating a number: why a card, bar or table cell shows what it shows.

The report layer is the Desktop-authored structure twin of the Agents dashboard (15 visuals,
saved slicers N = 6 and "MOM% Sales", per-category bar colours, a table whose font colour is a
measure and whose rows a measure filter picks). The model is a small synthetic one written for
these tests: 9 agents, 8 stores, sales on 10 Nov and 5 Dec 2009. Every expected number below is
worked out by hand from that data, so the tests check the engine path, not just "no error".
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio import live_report
from ai_powerbi_analyst.studio import number_explain as ne
from ai_powerbi_analyst.studio.dax_engine import DaxUnsupported, Model
from ai_powerbi_analyst.studio.live_report import LiveReportRequest, VisualSelection, render_page
from ai_powerbi_analyst.studio.report_engine import LocalReportEngine
from ai_powerbi_analyst.studio.report_import import ImportedReport, read_report

TWIN = Path(__file__).resolve().parents[2] / "fixtures" / "imported" / "structure-twin.pbix"
REAL = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"
PAGE = "ReportSection8454c812b60b8bb83b99"
CARD = "fb4c5aa34e0b084b62b9"  # Agents with Positive Change %
AVG_CARD = "2202e311b1493069e4eb"  # Agents Avg MTD Sales
COUNT_CARD = "71a164765d9c63200302"  # Number of Agents Sold above 100K
CHANGE_CHART = "ab13db00c71dcec86e99"  # Avg MTD % change by Agents
SALES_CHART = "b404a461ede7604a2774"  # Total MTD Sales
TABLE = "e82e02f93055d6b0b544"
N_SLICER = "953a59a3cad98b0b5a39"
PERCENT = "0.00%;-0.00%;0.00%"

RANK = (
    "RANKX( FILTER( ALL( DimEmployee[EmployeeKey] ), NOT ISBLANK( [MTD Total Sales] ) ), "
    "[MTD Total Sales], , {order} )"
)


def _group(inner: str) -> str:
    """A TopN/Others/BottomN measure over one per-agent measure (the twin's charts)."""
    part = (
        "CALCULATE( {m}, FILTER( ALL( DimEmployee[EmployeeKey] ), NOT ISBLANK( [MTD Total Sales] )"
        " && {rank} ) )"
    )
    return (
        "VAR _N = 'Top-Bottom-N'[Top-Bottom-N Value] "
        f"VAR _Top = {part.format(m=inner, rank='[Rank MTD Desc] <= _N')} "
        f"VAR _Others = {part.format(m=inner, rank='[Rank MTD Desc] > _N')} "
        f"VAR _Bottom = {part.format(m=inner, rank='[Rank MTD Asc] <= _N')} "
        "RETURN SWITCH( TRUE(), SELECTEDVALUE( 'Clustered Employees'[Order] ) = 1, _Top, "
        "SELECTEDVALUE( 'Clustered Employees'[Order] ) = 2, _Others, "
        "SELECTEDVALUE( 'Clustered Employees'[Order] ) = 3, _Bottom )"
    )


MEASURES: list[tuple[str, str, str]] = [
    ("# Measures", "Total Sales", "SUM( FactSales[SalesAmount] )"),
    (
        "Date",
        "_ShowValueForDates",
        "VAR __Last = CALCULATE( MAX( 'FactSales'[DateKey] ), REMOVEFILTERS() ) "
        "RETURN MIN( 'Date'[Date] ) <= __Last",
    ),
    (
        "# Measures",
        "MTD Total Sales",
        "IF( [_ShowValueForDates], CALCULATE( [Total Sales], DATESMTD( 'Date'[Date] ) ) )",
    ),
    (
        "# Measures",
        "PMTD Total Sales",
        "IF( [_ShowValueForDates], CALCULATE( [MTD Total Sales], "
        "DATEADD( 'Date'[Date], -1, MONTH ) ) )",
    ),
    (
        "# Measures",
        "MOMTD Total Sales",
        "VAR __Now = [MTD Total Sales] VAR __Before = [PMTD Total Sales] "
        "RETURN IF( NOT ISBLANK( __Now ) && NOT ISBLANK( __Before ), __Now - __Before )",
    ),
    ("# Measures", "MOMTD % Total Sales", "DIVIDE( [MOMTD Total Sales], [PMTD Total Sales] )"),
    (
        "# Measures",
        "Number of Employees with Positive Change",
        "COUNTX( FILTER( ALL( DimEmployee[EmployeeKey] ), [MOMTD Total Sales] > 0 ), "
        "DimEmployee[EmployeeKey] )",
    ),
    (
        "# Measures",
        "Employees with Positive Change %",
        "VAR _Total_Employees = CALCULATE( COUNT( DimEmployee[EmployeeKey] ), ALL( DimEmployee ) )"
        " VAR _Result = DIVIDE( [Number of Employees with Positive Change], _Total_Employees ) "
        "RETURN _Result",
    ),
    (
        "# Measures",
        "Employees Avg MTD Sales",
        "AVERAGEX( ALLSELECTED( DimEmployee[EmployeeKey] ), [MTD Total Sales] )",
    ),
    (
        "# Measures",
        "Number of Employees selling more than 100K MTD",
        "COUNTX( FILTER( ALL( DimEmployee[EmployeeKey] ), [MTD Total Sales] >= 100000 ), "
        "DimEmployee[EmployeeKey] )",
    ),
    ("Top-Bottom-N", "Top-Bottom-N Value", "SELECTEDVALUE( 'Top-Bottom-N'[Top-Bottom-N], 10 )"),
    ("# Measures", "Rank MTD Desc", RANK.format(order="DESC")),
    ("# Measures", "Rank MTD Asc", RANK.format(order="ASC")),
    (
        "# Measures",
        "Employees Avg MTD % Change",
        "AVERAGEX( FILTER( VALUES( DimEmployee[EmployeeKey] ), NOT ISBLANK( [MTD Total Sales] ) )"
        ", [MOMTD % Total Sales] )",
    ),
    ("# Measures", "TopN/BottomN/ MTD Sales", _group("[MTD Total Sales]")),
    ("# Measures", "TopN/BottomN/ MTD % Change", _group("[Employees Avg MTD % Change]")),
    ("# Measures", "TopN/BottomN/ Avg MTD Sales", _group("[Employees Avg MTD Sales]")),
    ("# Measures", "TopN/BottomN/ Avg Total Discount", "0"),
    ("# Measures", "Employee Name", "SELECTEDVALUE( DimEmployee[FirstName] )"),
    ("# Measures", "Rank", "[Rank MTD Desc]"),
    ("# Measures", "Table MTD Sales", 'FORMAT( [MTD Total Sales], "$#,##0" )'),
    ("# Measures", "vs PM MOM%", "[MOMTD % Total Sales]"),
    (
        "# Measures",
        "vs Previous Month",
        "VAR _N = 'Top-Bottom-N'[Top-Bottom-N Value] VAR _Rank = [Rank MTD Desc] "
        "RETURN IF( _Rank IN { _N + 1, _N + 2 }, BLANK(), [MOMTD % Total Sales] )",
    ),
    (
        "# Measures",
        "CF Table",
        "VAR _N = [Top-Bottom-N Value] "
        'RETURN IF( [Rank MTD Desc] IN { _N + 1, _N + 2 }, "white", "black" )',
    ),
    ("# Measures", "Rank Filtering Dynamics", "IF( NOT ISBLANK( [MTD Total Sales] ), 1, 0 )"),
    ("# Measures", "Rank Filtering Employyees Sales MTD CF", '"black"'),
    (
        "# Measures",
        "Rank Filtering Employyees MTD% CF",
        'IF( [Rank MTD Desc] <= [Top-Bottom-N Value], "#0D4FBD", "black" )',
    ),
    ("# Measures", "dummy", "[PMTD Total Sales]"),
]
# The card helpers (reference labels, SVG bars): values of their own, not explained here.
for _name in (
    "Target Employees Positive Change %", "Employees AVG MTD Sales Target",
    "Employees Avg MTD % change Target", "Number of Employees selling more than 100K Target",
    "Number of Employees with Positive change PM", "Employees Avg MTD Sales PM",
    "Employees Avg MTD % change PM", "Number of Employees selling more than 100K PM",
    "Employees Avg MTD % Change vs Previous Month", "Employees Avg MTD Sales vs Previous Month",
    "Employees Positive Change vs Previous Month",
    "Number of Employees sold more than 100K vs Previous Month",
):  # fmt: skip
    MEASURES.append(("# Measures", _name, "1"))
for _name in (
    "KPI, Avg MTD Sales towards Target", "KPI, Employees Avg MTD % Change towards Target",
    "KPI, Number of Employees sold above 100K towards Target",
    "KPI, Positive Change towards Target",
    "SVG 100% Stacked Bar Chart Employees Avg MTD % change",
    "SVG 100% Stacked Bar Chart Employees Avg MTD Sales",
    "SVG 100% Stacked Bar Chart Employees Positive Change",
    "SVG 100% Stacked Bar Chart Number of Employees Selling more than 100K",
):  # fmt: skip
    MEASURES.append(("# Measures", _name, '"ok"'))
RELATIONSHIPS = [
    ("FactSales", "StoreKey", "DimStore", "StoreKey"),
    ("DimStore", "EmployeeKey", "DimEmployee", "EmployeeKey"),
    ("FactSales", "channelKey", "DimChannel", "ChannelKey"),
    ("FactSales", "DateKey", "Date", "Date"),
]
NOVEMBER = [10_000.0 * k for k in range(1, 9)]  # agents 1..8
DECEMBER = [15_000.0, 5_000.0, 45_000.0, 30_000.0, 70_000.0, 50_000.0, 120_000.0]  # 1..7


def _frames() -> dict[str, pd.DataFrame]:
    rows = [("2009-11-10", 100 + k, 1 + k % 2, amount) for k, amount in enumerate(NOVEMBER, 1)]
    rows += [("2009-12-05", 100 + k, 1 + k % 2, amount) for k, amount in enumerate(DECEMBER, 1)]
    return {
        "FactSales": pd.DataFrame(
            {
                "DateKey": pd.to_datetime([r[0] for r in rows]),
                "StoreKey": [r[1] for r in rows],
                "channelKey": [r[2] for r in rows],
                "SalesAmount": [r[3] for r in rows],
            }
        ),
        "DimStore": pd.DataFrame(
            {
                "StoreKey": [100 + k for k in range(1, 9)],
                "StoreName": [f"Store {k}" for k in range(1, 9)],
                "EmployeeKey": list(range(1, 9)),
            }
        ),
        "DimEmployee": pd.DataFrame(
            {"EmployeeKey": list(range(1, 10)), "FirstName": [f"Agent{k}" for k in range(1, 10)]}
        ),
        "DimChannel": pd.DataFrame({"ChannelKey": [1, 2], "ChannelName": ["Store", "Online"]}),
        "Date": pd.DataFrame({"Date": pd.date_range("2009-10-01", "2009-12-31")}),
        "Top-Bottom-N": pd.DataFrame({"Top-Bottom-N": list(range(1, 31))}),
        "Actuals-vs-PctChange": pd.DataFrame({"Top-Bottom-Field": ["MTD Sales", "MOM% Sales"]}),
        "Clustered Employees": pd.DataFrame(
            {"Clustered Employees": ["TopN", "Others", "BottomN"], "Order": [1, 2, 3]}
        ),
    }


def _report(path: Path) -> ImportedReport:
    pages, format_name, _ = read_report(path)
    digest = sha256_file(path)
    frames = _frames()
    return ImportedReport(
        id="report-explain",
        workspace_id="workspace-explain",
        name="Agents twin",
        source_path=str(path),
        archived_path=str(path),
        source_hash=digest,
        base_hash=digest,
        format=format_name,
        pages=pages,
        tables=[
            {"TableName": table, "ColumnName": column, "PandasDataType": str(dtype)}
            for table, frame in frames.items()
            for column, dtype in frame.dtypes.items()
        ],
        measures=[{"TableName": t, "Name": n, "Expression": e} for t, n, e in MEASURES],
        relationships=[
            {
                "FromTableName": many,
                "FromColumnName": many_column,
                "ToTableName": one,
                "ToColumnName": one_column,
                "IsActive": 1,
                "Cardinality": "M:1",
                "CrossFilteringBehavior": "Single",
            }
            for many, many_column, one, one_column in RELATIONSHIPS
        ],  # fmt: skip
        relationships_version=2,
    )


@pytest.fixture
def twin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ImportedReport, Any]:
    path = tmp_path / "twin.pbix"
    shutil.copyfile(TWIN, path)
    formats = {
        ("# Measures", name): PERCENT
        for name in (
            "Employees with Positive Change %", "TopN/BottomN/ MTD % Change",
            "MOMTD % Total Sales", "vs Previous Month",
        )
    }  # fmt: skip
    formats[("# Measures", "Employees Avg MTD Sales")] = "#,0.00"
    # The twin's model part is filler: the number formats come from here instead.
    monkeypatch.setattr(live_report, "model_formats", lambda *_: formats)
    model = Model.from_frames(
        _frames(), RELATIONSHIPS, {n: e for _, n, e in MEASURES}, measure_table="# Measures"
    )
    return _report(path), LocalReportEngine(model)


def _step(explanation: ne.Explanation, name: str) -> ne.Step:
    return next(s for s in explanation.steps if s.name == name)


# --- a card ----------------------------------------------------------------------------


def test_a_card_is_its_ratio_with_both_parts_computed_in_its_own_context(twin) -> None:
    report, engine = twin
    found = ne.explain_point(report, PAGE, CARD, engine=engine)
    # Agents 1, 3, 5 and 7 sold more in December than in November: 4 of the 9 agents.
    assert found.value == pytest.approx(4 / 9)
    assert found.formatted == "44.44%" and found.computed
    assert found.measure == "Employees with Positive Change %"
    assert found.formula is not None and found.formula.startswith("VAR _Total_Employees")
    # The date table runs to 31 Dec; the last sale is on 5 Dec, so no "last date in the data".
    assert found.period == "for December 2009, month to date (through 31 Dec 2009)"
    total = _step(found, "_Total_Employees")
    assert (total.value, total.formatted, total.kind, total.usage) == (9, "9", "variable", "direct")
    count = _step(found, "[Number of Employees with Positive Change]")
    assert (count.value, count.computed) == (4, True)
    # Worked out once per agent inside FILTER: described, never given one number.
    per_agent = _step(found, "[MOMTD Total Sales]")
    assert (per_agent.usage, per_agent.computed, per_agent.value) == ("per_row", False, None)
    assert "- So 44.44% = 4 ÷ 9." in found.text
    assert (
        "It counts 4 of the 9 DimEmployee[EmployeeKey] values it checks: those where "
        "[MOMTD Total Sales] is more than 0." in found.text
    )
    assert "_Result" not in {s.name for s in found.steps}  # the value itself, not a step
    assert "RETURN" not in found.text  # the formula stays in the payload


def test_filters_say_where_they_come_from_and_whether_they_reach_the_number(twin) -> None:
    report, engine = twin
    found = ne.explain_point(report, PAGE, CARD, engine=engine)
    slicers = [(f.source, f.field, f.condition, f.origin, f.reaches) for f in found.filters]
    assert slicers == [
        ("slicer", "Top-Bottom-N[Top-Bottom-N]", "is 6", "'TopN/BottomN'", False),
        ("slicer", "Actuals-vs-PctChange[Top-Bottom-Field]", "is MOM% Sales",
         "'Top/Bottom Field'", False),
    ]  # fmt: skip
    assert "it does not change this number" in found.filters[0].how
    chains = {p.tables: p.joins for p in found.relationships}
    assert chains[("FactSales", "DimStore", "DimEmployee")] == (
        "FactSales[StoreKey] → DimStore[StoreKey]",
        "DimStore[EmployeeKey] → DimEmployee[EmployeeKey]",
    )
    # A user's selection replaces the saved one, and a measure that reads it is reached.
    request = LiveReportRequest(selections=[VisualSelection(visual_id=N_SLICER, values=[2])])
    chart = ne.explain_point(
        report,
        PAGE,
        CHANGE_CHART,
        {"values": {"Clustered Employees.Clustered Employees": "TopN"}},
        engine=engine,
        request=request,
    )
    use = next(f for f in chart.filters if f.field == "Top-Bottom-N[Top-Bottom-N]")
    assert (use.source, use.condition, use.reaches) == ("slicer", "is 2", True)
    assert use.how == "[TopN/BottomN/ MTD % Change] reads Top-Bottom-N itself"


def test_an_average_says_how_many_rows_it_averages_and_their_sum(twin) -> None:
    report, engine = twin
    found = ne.explain_point(report, PAGE, AVG_CARD, engine=engine)
    assert found.value == pytest.approx(335_000 / 7)
    # AVERAGEX over the 9 agents: agents 8 and 9 have no December sales and are left out.
    assert (
        "It averages over 9 rows; 7 of them have a value (AVERAGEX leaves blanks out), adding up "
        "to 335,000.00. So 47,857.14 = 335,000.00 ÷ 7." in found.text
    )


# --- a chart point ------------------------------------------------------------------------


def test_a_bar_follows_its_switch_and_says_why_it_is_red(twin) -> None:
    report, engine = twin
    found = ne.explain_point(
        report,
        PAGE,
        CHANGE_CHART,
        {"values": {"Clustered Employees.Clustered Employees": "BottomN"}},
        engine=engine,
        focus="colour",
    )
    # The 6 lowest December sellers: agents 2, 1, 4, 3, 6, 5, and their Dec/Nov change.
    expected = (0.5 - 0.75 + 0.5 - 0.25 + 0.4 - 1 / 6) / 6
    assert found.value == pytest.approx(expected)
    assert found.formatted == "3.89%"
    assert found.point == {"Clustered Employees": "BottomN"}
    assert _step(found, "_Bottom").formatted == "3.89%"
    assert _step(found, "_N").value == 6
    assert (
        "The first SWITCH condition that holds is the selected Clustered Employees[Order] (blank "
        "when not exactly one is selected) is 3 (the 2 before it do not), so it returns _Bottom = "
        "3.89%." in found.text
    )
    colour = found.formatting
    assert colour is not None
    assert (colour.rule, colour.color, colour.name) == ("fixed", "#DC143C", "red")
    assert found.text.index("**Why this colour**") < found.text.index("**How it is calculated**")


def test_a_bar_without_a_point_is_the_visual_as_a_whole(twin) -> None:
    report, engine = twin
    found = ne.explain_point(report, PAGE, SALES_CHART, engine=engine)
    # No Order is selected for the whole chart, so no SWITCH case holds: blank by its own logic.
    assert (found.value, found.computed, found.formatted) == (None, True, "blank")
    assert found.blank is not None
    assert found.blank.startswith("[TopN/BottomN/ MTD Sales] returns BLANK by its own logic here")
    assert "no bar picked" in found.text


# --- a table row --------------------------------------------------------------------------


def test_a_blank_cell_is_blank_by_the_measures_own_if(twin) -> None:
    report, engine = twin
    point = {"values": {"DimEmployee.EmployeeKey": 2}, "measure": "# Measures.vs PM"}
    found = ne.explain_point(report, PAGE, TABLE, point, engine=engine, focus="blank")
    # Agent 2 is 7th of the 7 December sellers, the N + 1 separator row under N = 6.
    assert (found.value, found.measure) == (None, "vs Previous Month")
    assert _step(found, "_Rank").value == 7
    assert found.blank == (
        "[vs Previous Month] returns BLANK by its own logic here: _Rank is _N plus 1 or _N plus 2 "
        "holds here, so it returns BLANK"
    )
    visual = next(f for f in found.filters if f.source == "visual")
    assert (visual.field, visual.condition, visual.on_measure) == (
        "[Rank Filtering Dynamics]",
        "is 1",
        True,
    )
    assert visual.how.startswith("it decides which rows the visual shows")
    assert found.filters[0].source == "point" and found.filters[0].condition == "is 2"


def test_a_field_value_font_colour_is_explained_by_its_measure(twin) -> None:
    report, engine = twin
    point = {"values": {"DimEmployee.EmployeeKey": 2}, "measure": "Rank"}
    found = ne.explain_point(report, PAGE, TABLE, point, engine=engine, focus="colour")
    colour = found.formatting
    assert colour is not None
    assert (colour.rule, colour.target, colour.color, colour.measure) == (
        "fieldValue",
        "font",
        "white",
        "CF Table",
    )
    assert (
        '[Rank MTD Desc] is _N plus 1 or _N plus 2 holds here, so it returns "white"' in found.text
    )


# --- what the engine cannot compute -------------------------------------------------------


class _Refusing(LocalReportEngine):
    """Refuses any query that reads the whole DimEmployee table (as Desktop-only parts are)."""

    def query(self, report: ImportedReport, query: str, max_rows: int = 101) -> dict[str, Any]:
        if "ALL(DimEmployee)" in query:
            raise DaxUnsupported("This part needs Power BI Desktop to calculate exactly.")
        return super().query(report, query, max_rows)


def test_a_part_the_engine_refuses_is_named_and_the_rest_is_kept(twin) -> None:
    report, engine = twin
    refusing = _Refusing(engine.model)
    found = ne.explain_point(report, PAGE, CARD, engine=refusing)
    assert found.value == pytest.approx(4 / 9) and found.computed
    total = _step(found, "_Total_Employees")
    assert (total.computed, total.value, total.formatted) == (False, None, "not calculated")
    assert total.reason == "This part needs Power BI Desktop to calculate exactly."
    assert _step(found, "[Number of Employees with Positive Change]").value == 4
    assert "_Total_Employees = not calculated: This part needs Power BI Desktop" in found.text
    assert "So 44.44% =" not in found.text  # never a ratio with a missing part


def test_a_visual_without_a_number_and_unknown_columns() -> None:
    assert ne.Point.of({"values": {"T.C": 1}, "measure": "T.M"}) == ne.Point({"T.C": 1}, "T.M")
    with pytest.raises(ValueError):
        ne.Point.of({"values": {"T.C": [1]}})


def test_unknown_visual_and_column_are_lookup_errors(twin) -> None:
    report, engine = twin
    with pytest.raises(LookupError):
        ne.explain_point(report, PAGE, "nope", engine=engine)
    with pytest.raises(LookupError):
        ne.explain_point(report, PAGE, TABLE, {"values": {"T.Nope": 1}}, engine=engine)


def test_the_payload_is_json_safe(twin) -> None:
    report, engine = twin
    payload = ne.explain_point(report, PAGE, CARD, engine=engine).to_dict()
    assert payload["formatted"] == "44.44%"
    assert {"name", "kind", "owner", "formula", "value", "formatted", "usage"} <= set(
        payload["steps"][0]
    )
    assert payload["filters"][0]["source"] == "slicer"


# --- pieces -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("predicate", "words"),
    [
        ("'DimChannel'[ChannelName] IN {\"Store\",\"Online\"}",
         ("DimChannel[ChannelName]", "is one of Store, Online", "DimChannel")),
        ("'Top-Bottom-N'[Top-Bottom-N] = 6.0", ("Top-Bottom-N[Top-Bottom-N]", "is 6",
                                               "Top-Bottom-N")),
        ("'Date'[Date] IN {DATE(2009, 12, 5),BLANK()}",
         ("Date[Date]", "is one of 2009-12-05, (Blank)", "Date")),
        ("('T'[A] = 1) || ('T'[B] = 2)", ("a combination of fields",
                                          "matches a combined condition", None)),
    ],
)  # fmt: skip
def test_compiled_filters_read_as_words(predicate: str, words: tuple[Any, ...]) -> None:
    assert ne.predicate_words(predicate) == words


@pytest.mark.parametrize(
    ("color", "name"),
    [("#DC143C", "red"), ("#A30024", "red"), ("#1E90FF", "blue"), ("#cccccc", "grey"),
     ("white", "white"), ("#0D4FBD", "blue"), ("#00A651", "green"), ("#F2C80F", "yellow"),
     (None, None), ("url(x)", None)],
)  # fmt: skip
def test_colour_names(color: str | None, name: str | None) -> None:
    assert ne.colour_name(color) == name


@pytest.mark.parametrize(
    ("message", "asks"),
    [
        ("Why does this card show 46.76%?", True),
        ("why is this bar red?", True),
        ("Why is Store blank?", True),
        ("How is Agents with Positive Change % calculated?", True),
        ("explain this number", True),
        ("Where does the 19 come from?", True),
        ("Why is my model slow?", False),
        ("Make the bars red", False),
        ("Why don't you make the bars red", False),
        ("What is total sales by channel?", False),
        ("Explain the data model", False),
    ],
)
def test_chat_triggers(message: str, asks: bool) -> None:
    assert ne.is_explain_question(message) is asks


# --- chat ---------------------------------------------------------------------------------


@pytest.fixture
def asked(twin) -> ne.AskContext:
    report, engine = twin
    payload = render_page(engine, report, PAGE, LiveReportRequest())
    return ne.AskContext(report, engine, PAGE, payload=payload)


def test_chat_finds_the_card_by_the_number_it_shows(asked: ne.AskContext) -> None:
    text = ne.answer("Why does the card show 44.44%?", asked)
    assert text is not None
    assert text.startswith("**Agents with Positive Change %** shows **44.44%**")
    assert "So 44.44% = 4 ÷ 9." in text


def test_chat_asks_back_when_several_bars_fit(asked: ne.AskContext) -> None:
    text = ne.answer("Why is the BottomN bar red?", asked)
    assert text is not None and text.startswith("Several numbers on this page fit")
    assert "the BottomN bar of **Avg MTD % change by Agents** (3.89%)" in text
    chosen = ne.AskContext(
        asked.report, asked.engine, PAGE, visual_id=CHANGE_CHART, payload=asked.payload
    )
    answer = ne.answer("Why is the BottomN bar red?", chosen)
    assert (
        answer is not None
        and "**Why this colour**: The BottomN bar has a colour of its own, #DC143C (red)" in answer
    )


def test_chat_reads_a_name_with_a_number_as_a_name(asked: ne.AskContext) -> None:
    text = ne.answer("How is Number of Agents Sold above 100K calculated?", asked)
    assert text is not None
    assert text.startswith("**Number of Agents Sold above 100K** shows **1**")
    assert "It counts 1 of the 9 DimEmployee[EmployeeKey] values it checks" in text


def test_chat_leaves_other_questions_to_the_model(asked: ne.AskContext) -> None:
    assert ne.answer("Why is my model slow?", asked) is None
    assert ne.answer("Why does it show 77%?", asked) == (
        "No visual on this page shows 77% right now (with its current filters and slicers). "
        "Select the number on the canvas and ask again."
    )
    # Agents 1 and 3 both grew by 50%: two rows fit, so chat asks which.
    text = ne.answer("Why does it show 50%?", asked)
    assert text is not None and text.startswith("Several numbers on this page fit")
    assert "- vs PM % in the 1 row of **EmployeeKey** (0.5)" in text


# --- the user's real dashboard (read-only; skipped where it is absent) -------------------


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> tuple[ImportedReport, Any]:
    if not REAL.is_file():
        pytest.skip("the real Agents Performance report is not on this machine")
    from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
    from ai_powerbi_analyst.studio.report_engine import report_model
    from ai_powerbi_analyst.studio.report_import import import_report

    state = tmp_path_factory.mktemp("state")
    # The import copies the file into ``state`` first and decodes that copy.
    report, snapshot = import_report(str(REAL), state, "ws-explain", StudioIngestionService(state))
    assert snapshot is not None
    return report, LocalReportEngine(report_model(report, snapshot))


def test_the_real_positive_change_card(real) -> None:
    report, engine = real
    found = ne.explain_point(report, PAGE, CARD, engine=engine)
    assert found.formatted == "46.76%" and found.value == pytest.approx(137 / 293)
    assert _step(found, "_Total_Employees").value == 293
    assert _step(found, "[Number of Employees with Positive Change]").value == 137
    assert "- So 46.76% = 137 ÷ 293." in found.text
    # ALL(DimEmployee[EmployeeKey]) has a (Blank) member: a sale's store has no agent.
    assert (
        "It counts 137 of the 294 DimEmployee[EmployeeKey] values it checks (one is the "
        "(Blank) DimEmployee member that unmatched keys add)" in found.text
    )
    assert [f.reaches for f in found.filters] == [False, False]


def test_the_real_bar_table_and_chat(real) -> None:
    report, engine = real
    bar = ne.explain_point(
        report,
        PAGE,
        CHANGE_CHART,
        {"values": {"Clustered Employees.Clustered Employees": "BottomN"}},
        engine=engine,
        focus="colour",
    )
    assert bar.formatted == "-86.82%"
    assert bar.formatting is not None and bar.formatting.name == "red"
    cell = ne.explain_point(
        report,
        PAGE,
        TABLE,
        {"values": {"DimEmployee.EmployeeKey": 201}, "measure": "# Measures.vs PM"},
        engine=engine,
    )
    assert cell.value is None and cell.blank is not None
    assert "returns BLANK by its own logic" in cell.blank
    assert _step(cell, "_Rank_Middle_Empty_Bar").value == 7
    text = ne.answer("Why does the card show 46.76%?", ne.AskContext(report, engine, PAGE))
    assert text is not None and "So 46.76% = 137 ÷ 293." in text
