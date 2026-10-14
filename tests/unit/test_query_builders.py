"""Computed answers for counts, lists, ratios and year-over-year on an imported report.

The 2026-09-27 held-out battery on the Agents Performance report sent these to the 4B
model: H09 (stores per store type and their sales), H10 (stores closed for relocation),
BRE-07 (how many stores closed, why, and what they sold), H17 (sales per transaction by
year) and DEV-07 (year-over-year % by year). It had no numbers, claimed a join was
missing, and a query it wrote took up to 142 s. The planner also missed "Which 3 stores
sold the most in 2009?" (H05), "How did Reseller sales change from 2008 to 2009?" (H12)
and "December 2009 compared with December 2008, by channel" (H19).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_imported_report_chat import MEASURES, _ask, _frames, _import

from ai_powerbi_analyst.studio.card_answers import measure_facts
from ai_powerbi_analyst.studio.data_answers import plan_imported_answer
from ai_powerbi_analyst.studio.dax_engine import run_query
from ai_powerbi_analyst.studio.model_answers import missing_fields_answer, model_walkthrough
from ai_powerbi_analyst.studio.query_builders import build_query
from ai_powerbi_analyst.studio.report_engine import report_model
from ai_powerbi_analyst.studio.report_import import imported_measures, imported_relationships

ADJUSTED = (
    "AVERAGEX( FILTER( VALUES(DimEmployee[EmployeeKey]), NOT ISBLANK([MTD Total Sales]) "
    "&& NOT ISBLANK( DimEmployee[EmployeeKey] ) ), [MTD Total Sales] )"
)
EXTRA_MEASURES = [
    ("Transactions", "COUNTROWS( FactSales )"),
    ("PY Total Sales", "CALCULATE ( [Total Sales], SAMEPERIODLASTYEAR ( 'Date'[Date] ) )"),
    ("YOY % Total Sales", "DIVIDE ( [Total Sales] - [PY Total Sales], [PY Total Sales] )"),
    ("Quantity Sold", "SUM( FactSales[SalesQuantity] )"),
    ("Return Quantity", "SUM( FactSales[ReturnQuantity] )"),
    ("Avg MTD Adjusted", ADJUSTED),
]


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The chat fixture with store types and close reasons, as the real DimStore has."""
    frames = _frames()
    # Store 10 North (On), 11 South (Off: relocated), 12 Web (On, an online store).
    frames["DimStore"]["StoreType"] = ["Store", "Store", "Online"]
    frames["DimStore"]["CloseReason"] = ["Not Yet Closed", "Relocation", "Not Yet Closed"]
    frames["DimStore"]["GeographyKey"] = [7, 8, 9]  # no geography table behind it
    # Units sold and returned on each of the ten sales rows (channels 2, 3, 2, 3, 1 ...).
    frames["FactSales"]["SalesQuantity"] = [10, 20, 10, 20, 100, 100, 100, 50, 100, 1]
    frames["FactSales"]["ReturnQuantity"] = [1, 0, 1, 2, 5, 5, 10, 0, 0, 0]
    service, workspace_id = _import(
        tmp_path, monkeypatch, frames=frames, measures=MEASURES + EXTRA_MEASURES
    )
    report = service.imported_report(workspace_id)
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    return service, workspace_id, report, snapshot, report_model(report, snapshot)


def _rows(model, question: str) -> list[list[object]]:
    built = build_query(question, model)
    assert built is not None, question
    return run_query(model, built.dax, time_budget=30).rows


def test_closed_stores_are_counted_with_their_reasons_and_sales(stores) -> None:
    service, workspace_id, _report, _snapshot, model = stores
    text = _ask(
        service,
        workspace_id,
        "i need a number before 3pm: how many stores are closed (not active anymore), what "
        "were the reasons they closed, and how much did those closed stores sell in total "
        "over all the years in the data?",
    )
    assert "- Relocation: Stores 1, Total Sales 130,350" in text
    assert '“closed” is read as \'DimStore\'[Status] = "Off" (the other value is "On").' in text
    assert "FILTER(ALL('DimStore'[Status]), 'DimStore'[Status] = \"Off\")" in text
    assert any(e["type"] == "chat.formula_answer" for e in service.events(workspace_id))
    # H10: a named reason is a value filter; the state still applies.
    assert _rows(
        model, "How many stores closed because of relocation, and what did they sell in total?"
    ) == [[1, 130350.0]]


def test_counts_by_a_column_get_a_total_row(stores) -> None:
    service, workspace_id, *_ = stores
    text = _ask(
        service,
        workspace_id,
        "How many stores does each store type have, and how much did each type sell in total?",
    )
    assert "- Store: Stores 2, Total Sales 300,540" in text
    assert "- Online: Stores 1, Total Sales 5,000" in text
    # Power BI's grand total: sales of store 99, missing from DimStore, count too.
    assert "**Total**: Stores 3, Total Sales 305,547" in text


def test_which_stores_lists_them_with_a_count(stores) -> None:
    service, workspace_id, _report, _snapshot, model = stores
    text = _ask(service, workspace_id, "Which stores are not active anymore?")
    assert text.startswith("**1** store:\n- South Store")
    assert _rows(model, "List the Online stores") == [["Web Store"]]
    assert _rows(model, "How many Online stores are there?") == [[1]]
    # Reseller is only a channel here, and channels do not filter stores.
    assert build_query("How many Reseller stores are there?", model) is None


@pytest.mark.parametrize(
    "question",
    [
        "How many stores per region?",  # no such field
        "How many stores per channel?",  # stores are not filtered by channel
        "Which stores are there?",  # every store is no filtered list
        "Which stores sold the most?",  # a ranking: the planner's
        "How many agents are closed?",  # DimEmployee has no On/Off column
        "How many stores opened in 2008?",  # a period on OpenDate is not read
        "Why did stores close?",
        # Periods the builders do not read would give all-time figures.
        "How many stores closed this year?",
        "What is the average sale per transaction this month?",
        "How did sales change year over year this month?",
    ],
)
def test_builders_decline_what_they_do_not_read(stores, question: str) -> None:
    *_, model = stores
    assert build_query(question, model) is None


def test_a_ratio_per_transaction_uses_the_reports_measures(stores) -> None:
    service, workspace_id, *_rest, model = stores
    built = build_query("Average sales amount per transaction by year", model)
    assert built is not None
    assert built.dax == (
        "EVALUATE SUMMARIZECOLUMNS('Date'[Year], \"Total Sales per Transaction\", "
        "DIVIDE([Total Sales], [Transactions])) ORDER BY 'Date'[Year]"
    )
    assert run_query(model, built.dax, time_budget=30).rows == [
        [2008, 150.0],
        [2009, 305247 / 8],
    ]
    formula = service.imported_lab_formula(
        workspace_id, "Average sales amount per transaction by year"
    )
    assert formula is not None and formula[0] == built.dax and formula[1] == "snapshot"
    assert "divided by [Transactions]" in formula[2]


def test_a_divided_by_ratio_keeps_its_breakdown_and_name(stores) -> None:
    *_, model = stores
    # H18: the model wrote DIVIDE([Return Quantity], [Quantity Sold], 0) with no channels.
    built = build_query("Return rate % by channel: returned units divided by sold units", model)
    assert built is not None
    assert built.dax == (
        "EVALUATE SUMMARIZECOLUMNS('DimChannel'[ChannelName], \"Return rate %\", "
        "DIVIDE([Return Quantity], [Quantity Sold])) ORDER BY [Return rate %] DESC"
    )
    assert run_query(model, built.dax, time_budget=30).rows == [
        ["Reseller", 2 / 40],
        ["Store", 20 / 401],
        ["Online", 2 / 70],
    ]
    per_unit = build_query("Total sales per unit by channel", model)
    assert per_unit is not None and "DIVIDE([Total Sales], [Quantity Sold])" in per_unit.dax


def test_explanations_get_each_variable_and_the_blank_member(stores) -> None:
    _service, _workspace_id, report, _snapshot, model = stores
    from ai_powerbi_analyst.studio.dax_describe import describe_parts

    parts = dict(
        describe_parts(
            "VAR _N = [Top-Bottom-N Value] "
            "VAR _Rank = RANKX( FILTER( ALL( DimEmployee[EmployeeKey] ), "
            "NOT ISBLANK( [MTD Total Sales] ) ), [MTD Total Sales], , DESC ) "
            'RETURN IF( _Rank IN { _N + 1, _N + 2 }, "⋮", '
            'SELECTEDVALUE( DimEmployee[FirstName] ) & " " & '
            "SELECTEDVALUE( DimEmployee[LastName] ) )",
            {"Top-Bottom-N Value", "MTD Total Sales"},
        )
    )
    assert parts["_Rank"].startswith(
        "the rank of [MTD Total Sales] among every DimEmployee[EmployeeKey] where "
        "[MTD Total Sales] is not blank (1 = the highest"
    )
    assert parts["RETURN"].startswith('"⋮" if _Rank is _N plus 1 or _N plus 2, otherwise the text')
    # H21: AVERAGEX skips blanks; the difference is the (Blank) agent of store 99.
    facts = measure_facts(report, model, ADJUSTED, "How is this different from [Total Sales]?")
    assert "AVERAGEX leaves out rows where the averaged value is blank" in facts
    # Each condition's effect is computed: here neither changes the result.
    assert "- Without `NOT (ISBLANK([MTD Total Sales]))` the result is the same" in facts
    assert "DimEmployee has a (Blank) member" not in facts  # every store has an employee
    assert "[Total Sales] is the sum of FactSales[SalesAmount]." in facts


def test_year_over_year_reuses_the_reports_yoy_measure(stores) -> None:
    service, workspace_id, *_rest, model = stores
    goal = (
        "Write DAX that shows Total Sales and its year-over-year % change by calendar year "
        "('Date'[Year]) so I can check 2008 and 2009."
    )
    built = build_query(goal, model)
    assert built is not None and "[YOY % Total Sales]" in built.dax
    rows = run_query(model, built.dax, time_budget=30).rows
    assert rows[0] == [2008, 300.0, None] and rows[1][0] == 2009
    assert rows[1][2] == pytest.approx((305247 - 300) / 300)
    # Without such a measure, SAMEPERIODLASTYEAR gives the same change.
    fallback = build_query("Year over year growth of profit", model)
    assert fallback is not None and "SAMEPERIODLASTYEAR('Date'[Date])" in fallback.dax
    assert service.imported_lab_formula(workspace_id, goal)[0] == built.dax


def test_planner_reads_which_n_period_comparisons_and_compared_with(stores) -> None:
    service, workspace_id, report, snapshot, _model = stores
    measures = imported_measures(report, snapshot)
    relationships = imported_relationships(report, snapshot)

    def plan(question: str):
        return plan_imported_answer(question, snapshot, measures, relationships)

    top = plan("Which 2 stores sold the most in 2009?")
    assert top is not None and top.limit == 2 and top.direction == "desc"
    # "Online" is a channel and a store type: the channel (one row per value) is used.
    change = plan("How did Online sales change from 2008 to 2009?")
    assert change is not None and change.growth
    assert "'DimChannel'[ChannelName] = \"Online\"" in change.queries[0].display
    assert any("also a value of 'DimStore'[StoreType]" in note for note in change.notes)
    assert plan("Online sales in 2008 vs 2009") is not None
    assert plan("Online sales between 2008 and 2009?") is None  # a range, not a change
    assert plan("How did you calculate sales in 2008 and 2009?") is None
    text = _ask(service, workspace_id, "How did Online sales change between 2008 and 2009?")
    assert "was **5,090** in 2009 and **100** in 2008" in text
    # H19: "compared with" named DateWithTransactions' "with"; both months side by side.
    lab = service.imported_lab_formula(
        workspace_id, "Sales in December 2009 compared with November 2009, by channel"
    )
    assert lab is not None
    assert lab[0].startswith("EVALUATE ADDCOLUMNS(SUMMARIZECOLUMNS('DimChannel'[ChannelName], ")
    assert (
        '"Change %", DIVIDE([Total Sales December 2009] - [Total Sales November 2009]' in (lab[0])
    )


def test_walkthrough_and_formula_facts(stores) -> None:
    _service, _workspace_id, report, _snapshot, model = stores
    assert model_walkthrough(
        "How are the tables in this report connected to each other?", report, model
    )
    # H14: one table asked about gets its own answer, not the whole model.
    focused = model_walkthrough(
        "Why does the Top-Bottom-N table have no relationships?", report, model
    )
    assert focused is not None
    assert focused.startswith("**Top-Bottom-N** has no relationships")
    assert "[Top-Bottom-N Value]" in focused and "default 10" in focused
    assert "FactSales[StoreKey]" not in focused
    related = model_walkthrough("Why is DimStore not related to DimChannel?", report, model)
    assert related is not None and "DimStore" in related
    facts = measure_facts(
        report, model, "VAR __Last = MAX ( 'FactSales'[DateKey] ) RETURN __Last > 0", "explain"
    )
    assert "- __Last = 31 Dec 2009" in facts
    assert "It returns TRUE or FALSE" in facts
    assert "2 measures return their value only when [_ShowValueForDates] is TRUE" in facts


def test_fields_the_model_lacks_are_named_with_what_exists(stores) -> None:
    service, workspace_id, report, _snapshot, model = stores
    # BRE-01 took 56-131 s through the model and never named DimStore[GeographyKey].
    text = _ask(
        service, workspace_id, "What were total sales by region, and who is our biggest customer?"
    )
    assert text.startswith("This report's model has no **region** or **customer** field")
    assert "DimStore[GeographyKey] is a key with no table joined to it" in text
    assert "- **customer**: no table or column holds it." in text
    assert "DimChannel (ChannelName)" in text
    # Partly answerable questions, and fields that exist, stay with the model or planner.
    for question in ("Sales by region and channel", "Which store sells the most?"):
        assert missing_fields_answer(question, report, model) is None


def test_a_measure_comparison_is_answered_from_both_formulas(stores) -> None:
    service, workspace_id, report, snapshot, model = stores
    from threading import Event
    from uuid import uuid4

    from ai_powerbi_analyst.studio.card_answers import measure_comparison
    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    goal = "How is this measure different from [Total Sales]?"
    text = measure_comparison(report, model, ADJUSTED, goal)
    assert text is not None
    assert text.startswith(
        "**[Avg MTD Adjusted]** is the average of [MTD Total Sales] over each "
        "DimEmployee[EmployeeKey] where"
    )
    assert "**[Total Sales]** is the sum of FactSales[SalesAmount]." in text
    assert "- Without `NOT (ISBLANK([MTD Total Sales]))` the result is the same" in text
    assert measure_comparison(report, model, ADJUSTED, "Explain this measure") is None
    # The DAX Lab answers it without the model (the fixture model fails if called).
    request = DaxAssistRequest(
        workspace_id=workspace_id,
        snapshot_id=snapshot.id,
        request_id="compare-" + uuid4().hex,
        goal=goal,
        expression=ADJUSTED,
        mode="explain",
    )
    events = list(dax_assistant_events(service, request, Event()))
    answer = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    assert answer == text
