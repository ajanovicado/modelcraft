"""Computed chat answers on an imported report decline rather than guess.

Review of the imported-PBIX fixes (2026-09-26) found computed answers that were
confidently wrong: a growth verdict over only the 8 largest members, names and periods
the planner could not read silently dropped ("total sales for Nuria Gonzalez" got the
grand total), PY/MAT/YOY measures answered as the plain total, a "this month" note on a
named month, pay listed per person, and an HR table's BirthDate used as the data dates.
"""

from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from ai_powerbi_analyst.studio.data_answers import (
    ImportedAnswerPlan,
    ImportedQuery,
    Period,
    compose_imported_answer,
    plan_imported_answer,
)
from ai_powerbi_analyst.studio.report_import import (
    ImportReportRequest,
    imported_measures,
    imported_relationships,
)
from ai_powerbi_analyst.studio.service import StudioService

SHOW = "VAR __Last = MAX ( 'FactSales'[DateKey] ) RETURN __Last > 0"
PREVIOUS = "CALCULATETABLE ( DATEADD ( 'Date'[Date], -1, {unit} ), 'Date'[Year] > 0 )"
SALES_MEASURES = [
    ("Total Sales", "SUM( FactSales[SalesAmount] )"),
    ("_ShowValueForDates", SHOW),
    (
        "MTD Total Sales",
        "IF ( [_ShowValueForDates], CALCULATE ( [Total Sales], DATESMTD ( 'Date'[Date] ) ) )",
    ),
    (
        "PY Total Sales",
        "IF ( [_ShowValueForDates], CALCULATE ( [Total Sales], "
        + PREVIOUS.format(unit="YEAR")
        + " ) )",
    ),
    (
        "PM Total Sales",
        "IF ( [_ShowValueForDates], CALCULATE ( [Total Sales], "
        + PREVIOUS.format(unit="MONTH")
        + " ) )",
    ),
    (
        "PMTD Total Sales",
        "IF ( [_ShowValueForDates], CALCULATE ( [MTD Total Sales], "
        + PREVIOUS.format(unit="MONTH")
        + " ) )",
    ),
    (
        "MAT Total Sales",
        "IF ( [_ShowValueForDates], CALCULATE ( [Total Sales], "
        "DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -1, YEAR ) ) )",
    ),
    (
        "YOY Total Sales",
        "VAR __Now = [Total Sales] VAR __Before = [PY Total Sales] "
        "RETURN IF ( NOT ISBLANK ( __Now ) && NOT ISBLANK ( __Before ), __Now - __Before )",
    ),
    (
        "Number of Employees selling more than 100K MTD",
        "COUNTX( FILTER( ALL( DimEmployee[EmployeeKey] ), [MTD Total Sales] >= 100000 ), "
        "DimEmployee[EmployeeKey] )",
    ),
    ("Total Commission Pay", "SUM( FactSales[CommissionPay] )"),
    ("Quantity Sold", "SUM( FactSales[SalesQuantity] )"),
    ("Paid Commission", "[Total Commission Pay]"),  # personal through the measure it uses
]
SALES_RELATIONSHIPS = [
    ("FactSales", "StoreKey", "DimStore", "StoreKey"),
    ("DimStore", "EmployeeKey", "DimEmployee", "EmployeeKey"),
    ("FactSales", "channelKey", "DimChannel", "ChannelKey"),
    ("FactSales", "DateKey", "Date", "Date"),
]
NAMES = [
    ("Ana", "Lopez"),
    ("Bo", "Chen"),
    ("Cy", "Diaz"),
    ("Dee", "Evans"),
    ("Eli", "Ford"),
    ("Fay", "Gomez"),
    ("Gus", "Hill"),
    ("Hal", "Ives"),
    ("Ida", "Jones"),
    ("Jo", "Khan"),
    ("Kim", "Lee"),
    ("Lu", "Moss"),
    ("Zed", "Tiny"),
]
# Growth 2008 -> 2009 by employee: 1-11 grow 1-11%, Lu Moss (12) halves.
GROWTH = {e: 1 + e / 100 for e in range(1, 12)} | {12: 0.5}


def _sales_frames() -> dict[str, pd.DataFrame]:
    rows = []  # date, store, channel, amount
    for store in range(1, 121):  # 120 stores: more than the 100 rows a query returns
        employee = (store - 1) % 12 + 1
        before = 1000.0 + 10 * store
        rows.append(("2008-06-15", store, store % 3 + 1, before))
        rows.append(("2009-06-15", store, store % 3 + 1, round(before * GROWTH[employee], 2)))
    # Zed Tiny's one small store grows most but is last in 2009: past any top 8.
    rows += [("2008-06-15", 121, 1, 10.0), ("2009-06-15", 121, 1, 1000.0)]
    rows += [("2009-11-10", 1, 2, 150_000.0), ("2009-12-10", 2, 3, 120_000.0)]
    calendar = pd.DataFrame({"Date": pd.date_range("2008-01-01", "2009-12-31")})
    calendar["Year"] = calendar["Date"].dt.year
    return {
        "FactSales": pd.DataFrame(
            {
                "DateKey": pd.to_datetime([row[0] for row in rows]),
                "StoreKey": [row[1] for row in rows],
                "channelKey": [row[2] for row in rows],
                "SalesAmount": [row[3] for row in rows],
                "CommissionPay": [row[3] / 100 for row in rows],
                "SalesQuantity": [2] * len(rows),
            }
        ),
        "DimStore": pd.DataFrame(
            {
                "StoreKey": list(range(1, 122)),
                "StoreName": [f"Store {key:03d}" for key in range(1, 122)],
                "EmployeeKey": [(key - 1) % 12 + 1 for key in range(1, 121)] + [13],
            }
        ),
        "DimEmployee": pd.DataFrame(
            {
                "EmployeeKey": list(range(1, 14)),
                "FirstName": [first for first, _ in NAMES],
                "LastName": [last for _, last in NAMES],
                "BirthDate": pd.to_datetime(
                    [f"19{60 + key}-03-0{key % 9 + 1}" for key in range(13)]
                ),
                "Gender": ["Female", "Male"] * 6 + ["Female"],
                "BaseRate": [20.0 + key for key in range(13)],
            }
        ),
        "DimChannel": pd.DataFrame(
            {"ChannelKey": [1, 2, 3], "ChannelName": ["Store", "Online", "Reseller"]}
        ),
        "Date": calendar,
    }


def _pbix(tmp_path: Path, name: str) -> Path:
    file = tmp_path / f"{name}.pbix"
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr("Report/definition/pages/pages.json", json.dumps({"pageOrder": ["p1"]}))
        archive.writestr(
            "Report/definition/pages/p1/page.json", json.dumps({"name": "p1", "displayName": "1"})
        )
        archive.writestr("DataModel", b"fixture-decoder-substituted")
    return file


def _import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frames: dict[str, pd.DataFrame],
    measures: list[tuple[str, str]],
    relationships: list[tuple[str, str, str, str]],
) -> tuple[StudioService, str]:
    def model(_source, destination):  # type: ignore[no-untyped-def]
        tables = []
        for index, (name, frame) in enumerate(frames.items()):
            frame.to_parquet(destination / f"table-{index}.parquet", index=False)
            tables.append({"name": name, "file": f"table-{index}.parquet", "rows": len(frame)})
        return {
            "tables": tables,
            # These answers exercise a current Import model with known metadata. A decoder
            # that exports rows without facts now correctly leaves local values unavailable.
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
                    for name, expression in measures
                ],
                "relationships": [
                    {
                        "fromTable": many,
                        "fromColumn": many_column,
                        "toTable": one,
                        "toColumn": one_column,
                        "active": True,
                        "cardinality": "M:1",
                        "crossFilter": "single",
                    }
                    for many, many_column, one, one_column in relationships
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
                for name, expression in measures
            ],
            "relationships": [
                {
                    "FromTableName": many,
                    "FromColumnName": many_column,
                    "ToTableName": one,
                    "ToColumnName": one_column,
                    "IsActive": 1,
                    "Cardinality": "M:1",
                    "CrossFilteringBehavior": "Single",
                }
                for many, many_column, one, one_column in relationships
            ],
        }

    monkeypatch.setattr("ai_powerbi_analyst.studio.report_import.extract_model", model)
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.import_existing_report(
        ImportReportRequest(path=str(_pbix(tmp_path, "Agents")), idempotency_key="answers-fix")
    )
    return service, workspace.id


@pytest.fixture
def sales(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[StudioService, str]:
    return _import(tmp_path, monkeypatch, _sales_frames(), SALES_MEASURES, SALES_RELATIONSHIPS)


def _plan(service: StudioService, workspace_id: str, question: str) -> ImportedAnswerPlan | None:
    workspace = service.workspace(workspace_id)
    report = service.imported_report(workspace_id)
    snapshot = service.snapshot(workspace.active_snapshot_id)
    return plan_imported_answer(
        question,
        snapshot,
        imported_measures(report, snapshot),
        imported_relationships(report, snapshot),
    )


def _answer(service: StudioService, workspace_id: str, question: str) -> str | None:
    return service._data_answer(service.workspace(workspace_id), question)


def _truth() -> pd.DataFrame:
    frames = _sales_frames()
    sales = frames["FactSales"].merge(frames["DimStore"], on="StoreKey")
    sales = sales.merge(frames["DimEmployee"], on="EmployeeKey")
    sales["Agent"] = sales["FirstName"] + " " + sales["LastName"]
    sales["Year"] = sales["DateKey"].dt.year
    return sales.pivot_table(index="Agent", columns="Year", values="SalesAmount", aggfunc="sum")


# --- Two-period comparisons -------------------------------------------------------------


def test_growth_verdict_covers_every_member_not_the_listed_top_eight(sales) -> None:
    truth = _truth()
    rates = (truth[2009] - truth[2008]) / truth[2008]
    assert rates.idxmax() == "Zed Tiny"
    assert "Zed Tiny" not in truth[2009].nlargest(8).index  # the old verdict never saw it
    answer = _answer(*sales, "Which agent grew the most from 2008 to 2009?")
    assert answer is not None
    assert "**Zed Tiny** grew the most (+9900.0%: 10 → 1,000)." in answer
    assert answer.count(" → ") == 8 + 1  # eight listed agents plus the verdict's values
    # 2009 ends on 10 Dec: 2008 runs to 10 Dec too, as DATEADD shifts the days (a day
    # count stopped leap-year 2008 on 9 Dec).
    assert "so 2008 is compared over the same days (to 10 Dec)" in answer
    assert "DATE(2008, 12, 11)" in answer


def test_growth_verdict_follows_the_direction_asked(sales) -> None:
    # The best grower was named even when the question asked which declined most.
    answer = _answer(*sales, "Which agent declined the most from 2008 to 2009?")
    assert answer is not None
    assert "**Lu Moss** declined the most (-50.0%: 16,600 → 8,300)." in answer
    least = _answer(*sales, "Which agent grew the least from 2008 to 2009?")
    assert least is not None and "**Lu Moss** declined the most (-50.0%)." in least
    assert "- Lu Moss: 16,600 in 2008 → 8,300 in 2009" in least


def test_two_period_list_keeps_the_asked_limit(sales) -> None:
    plan = _plan(*sales, "Top 3 agents by total sales in 2008 vs 2009")
    assert plan is not None and plan.limit == 3
    answer = _answer(*sales, "Top 3 agents by total sales in 2008 vs 2009")
    assert answer is not None
    listed = [line for line in answer.splitlines() if line.startswith("- ")]
    assert len(listed) == 3


def test_capped_comparison_is_left_to_the_model(sales) -> None:
    # 121 stores: each year's query returns 100, so a store past the cap would read
    # "(Blank) in 2008" and the verdict would miss most stores.
    assert _plan(*sales, "Which store grew the most from 2008 to 2009?") is not None
    assert _answer(*sales, "Which store grew the most from 2008 to 2009?") is None


def test_compose_declines_capped_results_and_missing_picks() -> None:
    query = ImportedQuery("q", "q", Period("2008", date(2008, 1, 1), date(2008, 12, 31)))
    later = ImportedQuery("q", "q", Period("2009", date(2009, 1, 1), date(2009, 12, 31)))
    plan = ImportedAnswerPlan(
        measure="Total Sales",
        queries=(query, later),
        entity="store",
        label_columns=("StoreName",),
        growth=True,
    )
    rows = [{"StoreName": "A", "Total Sales": 5.0}, {"StoreName": "B", "Total Sales": 4.0}]
    assert compose_imported_answer(plan, [(rows, True), (rows, False)]) is None
    assert compose_imported_answer(plan, [(rows, False), (rows, True)]) is None
    picked = ImportedAnswerPlan(**{**plan.__dict__, "picks": ("A", "C")})
    assert compose_imported_answer(picked, [(rows, True), (rows, False)]) is None
    whole = compose_imported_answer(plan, [(rows, False), (rows, False)])
    assert whole is not None and "(Blank)" not in whole


# --- Names, values and periods the planner cannot read ---------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What were total sales for Ana Lopez?",  # used to be the grand total
        "What were total sales for Ana Lopez in December 2009?",  # all agents' December
        "What were sales in Seattle?",
        "What were sales for store managers in 2009?",  # used to list sales by store
        "What are total sales for ana?",
        "What were total sales and quantity sold by channel?",  # was [Quantity Sold] alone
        "What were sales for Store 001?",  # was a list of every store
    ],
)
def test_unmapped_names_and_values_are_left_to_the_model(sales, question: str) -> None:
    assert _plan(*sales, question) is None


@pytest.mark.parametrize(
    ("polite", "plain"),
    [
        ("Could you show me total sales in 2009?", "Show me total sales in 2009?"),
        ("Would you please list the top 3 agents by sales?", "List the top 3 agents by sales?"),
        ("Can you tell me total sales by channel?", "Tell me total sales by channel?"),
        ("Please give me total sales in 2009", "Give me total sales in 2009"),
        ("Could you name the top agent by total sales?", "Show the top agent by total sales?"),
    ],
)
def test_a_polite_opener_is_the_same_question(sales, polite: str, plain: str) -> None:
    """ "could" and "would" in the opener ask for an answer; they used to read as discussion
    words, so a polite question never reached the computed path."""
    asked = _plan(*sales, polite)
    assert asked is not None
    assert asked == _plan(*sales, plain)


@pytest.mark.parametrize(
    "question",
    [
        "Could you explain why sales dropped in 2009?",
        "What would total sales be in 2009 without the Online channel?",
        "How could we raise total sales?",
    ],
)
def test_a_real_discussion_is_still_left_to_the_model(sales, question: str) -> None:
    assert _plan(*sales, question) is None


def test_the_verb_sell_still_belongs_to_quantity_sold(sales) -> None:
    answer = _answer(*sales, "How many units did we sell in 2009?")
    assert answer is not None and answer.startswith("Quantity Sold in 2009 is **")


@pytest.mark.parametrize(
    "question",
    [
        "What were total sales on 2009-12-15?",
        "What were total sales on 15 December 2009?",
        "What were total sales on December 15, 2009?",
        "What were total sales in 2009/12/15?",
        "What were total sales in 2009-12?",
        "What were total sales in 12/2009?",
        "What were total sales in '09?",
        "What were total sales in December?",  # used to be all data
        "What were total sales in May?",
        "What were sales in the first half of 2009?",  # used to group by FirstName
        "What were total sales in H1 2009?",
        "What were total sales in fall 2009?",
        "What were total sales in the last three months?",
        "What were total sales a year ago?",
        "What were total sales at the end of 2009?",
        "What were total sales in 2009 through March?",
        "What were total sales in the first month of 2009?",
        "What were total sales by date?",
        "What is total sales year over year?",
        "What were total sales in 2008 compared to the previous year?",
        "What were sales in the market 2009?",
        "What were total sales in December 2009 vs 2008?",  # a month against a year
        "What were total sales at 3 pm?",
    ],
)
def test_periods_it_cannot_filter_exactly_are_left_to_the_model(sales, question: str) -> None:
    assert _plan(*sales, question) is None


@pytest.mark.parametrize(
    ("question", "label"),
    [
        ("What were total sales in 2009?", "2009"),
        ("What were total sales in the year 2009?", "2009"),
        ("What were total sales in Dec 2009?", "December 2009"),
        ("What were total sales in the month of December 2009?", "December 2009"),
        ("What were total sales this year so far?", "2009"),
    ],
)
def test_periods_it_reads_still_answer(sales, question: str, label: str) -> None:
    plan = _plan(*sales, question)
    assert plan is not None
    assert [query.period.label for query in plan.queries if query.period] == [label]


# --- Period-prefixed measures ------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What is the MAT Total Sales?",
        "What is PMTD Total Sales?",
        "What is YOY Total Sales by channel?",
        "What was PY Total Sales in 2009?",  # the year before 2009, not 2009 or 2008 vs 2009
        "What were MTD sales last month?",
    ],
)
def test_period_measures_it_cannot_apply_are_left_to_the_model(sales, question: str) -> None:
    assert _plan(*sales, question) is None


def test_py_and_pm_are_answered_for_their_period_and_say_so(sales) -> None:
    frames = _sales_frames()["FactSales"]
    year_2008 = frames[frames["DateKey"].dt.year == 2008]["SalesAmount"].sum()
    answer = _answer(*sales, "What is PY Total Sales?")
    assert answer is not None
    assert answer.startswith(f"Total Sales in 2008 is **{year_2008:,.0f}**.")
    assert "“PY” is read as last year, 2008" in answer
    month = _answer(*sales, "What is PM Total Sales?")
    assert month is not None
    assert month.startswith("Total Sales in November 2009 is **150,000**.")
    assert "“PM” is read as last month, November 2009" in month


def test_named_month_threshold_has_no_this_month_note(sales) -> None:
    answer = _answer(*sales, "Which agents hit the 100K target in November 2009?")
    assert answer is not None
    assert "in November 2009" in answer and "Ana Lopez: 150,000" in answer
    assert "This month" not in answer and "this month" not in answer
    assert "tests [MTD Total Sales] >= 100000" in answer
    implied = _answer(*sales, "Which agents hit the 100K target this month?")
    assert implied is not None and "“This month” is December 2009" in implied


# --- Personal fields ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Which agents have the highest Total Commission Pay?",
        "What is the Total Commission Pay by channel?",
        "What is the Total Commission Pay for Online?",
        "Which agents have the highest Paid Commission?",
    ],
)
def test_pay_is_never_listed_per_person_or_group(sales, question: str) -> None:
    assert _plan(*sales, question) is None


def test_personal_wording_is_left_out_with_a_note(sales) -> None:
    answer = _answer(*sales, "Who are the top 2 agents by total sales and what is their salary?")
    assert answer is not None and answer.startswith("Top 2 agents by Total Sales")
    assert "I left out salary: these are personal fields" in answer


def test_pay_total_across_everyone_is_an_aggregate(sales) -> None:
    answer = _answer(*sales, "What is the Total Commission Pay?")
    assert answer is not None and answer.startswith("Total Commission Pay for all data")
    assert "I left out" not in answer


def test_person_table_dates_are_never_the_data_dates(tmp_path, monkeypatch) -> None:
    frames = {
        "Employees": pd.DataFrame(
            {
                "EmployeeKey": [1, 2, 3],
                "DepartmentKey": [1, 1, 2],
                "FirstName": ["Ana", "Bo", "Cy"],
                "BirthDate": pd.to_datetime(["1955-02-03", "1980-05-05", "2002-01-17"]),
                "HireDate": pd.to_datetime(["2019-02-01", "2020-06-01", "2021-09-01"]),
                "Hours": [10, 20, 30],
            }
        ),
        "Departments": pd.DataFrame({"DepartmentKey": [1, 2], "DepartmentName": ["A", "B"]}),
    }
    service, workspace_id = _import(
        tmp_path,
        monkeypatch,
        frames,
        [("Total Hours", "SUM( Employees[Hours] )")],
        [("Employees", "DepartmentKey", "Departments", "DepartmentKey")],
    )
    for question in (
        "What were total hours this month?",  # was January 2002, the latest birthday
        "What were total hours in 2002?",
        "What were total hours in 2021?",
    ):
        assert _plan(service, workspace_id, question) is None
    plan = _plan(service, workspace_id, "What were total hours by department?")
    assert plan is not None and plan.all_data == ""
    assert all("Date" not in query.expression for query in plan.queries)
    answer = _answer(service, workspace_id, "What were total hours by department?")
    assert answer is not None and "1955" not in answer and "2002" not in answer
