"""Studio's DAX engine: the Power BI semantics it was checked against, on a tiny model.

Every rule here was compared with Power BI Desktop on the imported Agents Performance
report (2026-09-27): 102 measures by nine breakdowns and its dashboard page matched,
and each case below is one Desktop answered differently from the first version of the
engine (blank rows, filter arguments on one column, month ends, periods before the
calendar). The model is synthetic so the expected values can be read off the rows.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from ai_powerbi_analyst.studio.dax_engine import (
    DaxEngineError,
    DaxEngineLimit,
    DaxUnsupported,
    Engine,
    Model,
    run_query,
)

MEASURES = {
    "Total Sales": "SUM( FactSales[SalesAmount] )",
    "Profit": "SUMX( FactSales, FactSales[SalesAmount] - FactSales[TotalCost] )",
    "MTD Sales": "CALCULATE( [Total Sales], DATESMTD( 'Date'[Date] ) )",
    "PM Sales": "CALCULATE( [Total Sales], DATEADD( 'Date'[Date], -1, MONTH ) )",
    "PY Sales": "CALCULATE( [Total Sales], SAMEPERIODLASTYEAR( 'Date'[Date] ) )",
    "Big Sellers": (
        "COUNTX( FILTER( ALL( DimEmployee[EmployeeKey] ), [Total Sales] >= 100 ), "
        "DimEmployee[EmployeeKey] )"
    ),
    "Rank": "RANKX( ALL( DimEmployee[EmployeeKey] ), [Total Sales] )",
    "Label": 'FORMAT( [Total Sales], "#,##0.00" )',
}


def _model() -> Model:
    dates = pd.date_range("2008-01-01", "2009-12-31")
    sales = [
        # date, store, channel, amount, cost
        ("2008-02-29", 10, 1, 50.0, 20.0),
        ("2008-03-15", 11, 2, 40.0, 10.0),
        ("2009-01-10", 10, 1, 100.0, 60.0),
        ("2009-02-28", 11, 1, 30.0, 10.0),
        ("2009-03-31", 10, 2, 70.0, 30.0),
        ("2009-12-02", 10, 1, 120.0, 50.0),
        ("2009-12-15", 11, 2, 60.0, 20.0),
        ("2009-12-20", 12, 1, 5.0, 1.0),  # store 12's agent 99 is unknown
        ("2009-12-31", 50, 1, 7.0, 2.0),  # store 50 is unknown
    ]
    return Model.from_frames(
        {
            "FactSales": pd.DataFrame(
                {
                    "DateKey": pd.to_datetime([s[0] for s in sales]),
                    "StoreKey": [s[1] for s in sales],
                    "ChannelKey": [s[2] for s in sales],
                    "SalesAmount": [s[3] for s in sales],
                    "TotalCost": [s[4] for s in sales],
                }
            ),
            "DimStore": pd.DataFrame(
                {
                    "StoreKey": [10, 11, 12],
                    "StoreName": ["North", "South", "Web"],
                    "Status": ["On", "Off", "On"],
                    "EmployeeKey": [1, 2, 99],
                }
            ),
            "DimEmployee": pd.DataFrame(
                {"EmployeeKey": [1, 2, 3], "FirstName": ["Ana", "Bo", "Cy"]}
            ),
            "DimChannel": pd.DataFrame({"ChannelKey": [1, 2], "ChannelName": ["Store", "Online"]}),
            "Date": pd.DataFrame({"Date": dates, "Year": dates.year}),
        },
        [
            ("FactSales", "StoreKey", "DimStore", "StoreKey"),
            ("DimStore", "EmployeeKey", "DimEmployee", "EmployeeKey"),
            ("FactSales", "ChannelKey", "DimChannel", "ChannelKey"),
            ("FactSales", "DateKey", "Date", "Date"),
        ],
        MEASURES,
    )


@pytest.fixture(scope="module")
def model() -> Model:
    return _model()


def value(model: Model, expression: str) -> object:
    return Engine(model).evaluate_scalar(expression)


def rows(model: Model, query: str) -> list[list[object]]:
    return run_query(model, query).rows


def test_blank_arithmetic_comparison_and_text_case(model) -> None:
    assert value(model, "1 + BLANK()") == 1
    assert value(model, "BLANK() * 5") is None
    assert value(model, "BLANK() = 0") is True
    assert value(model, "BLANK() == 0") is False
    assert value(model, '"a" = "A"') is True  # DAX compares text without case
    assert value(model, '"x" & BLANK()') == "x"
    assert value(model, "DIVIDE(5, 0, -1)") == -1
    assert value(model, "0 IN {BLANK()}") is False  # IN is strict
    with pytest.raises(DaxEngineError, match="comparing values of type Integer"):
        value(model, '1 = "a"')


def test_filters_flow_from_dimensions_and_ignore_text_case(model) -> None:
    assert value(model, "[Total Sales]") == 482
    assert value(model, 'CALCULATE([Total Sales], DimChannel[ChannelName] = "online")') == 170
    assert value(model, 'CALCULATE([Total Sales], DimEmployee[FirstName] = "Ana")') == 340
    assert value(model, 'CALCULATE([Total Sales], DimStore[Status] = "Off")') == 130
    assert value(model, "[Profit]") == 482 - 203


def test_blank_rows_as_power_bi_counts_them(model) -> None:
    """Desktop: COUNTROWS(DimEmployee) 293, VALUES 294, DISTINCTCOUNT 293 on the real report."""
    assert value(model, "COUNTROWS(DimEmployee)") == 3
    assert value(model, "COUNTROWS(VALUES(DimEmployee))") == 4
    assert value(model, "COUNTROWS(ALL(DimEmployee[EmployeeKey]))") == 4
    assert value(model, "COUNTROWS(DISTINCT(DimEmployee[EmployeeKey]))") == 3
    assert value(model, "COUNTROWS(ALLNOBLANKROW(DimEmployee))") == 3
    assert value(model, "DISTINCTCOUNT(DimEmployee[EmployeeKey])") == 3
    assert value(model, "COUNTROWS(FILTER(DimEmployee, TRUE()))") == 3
    assert value(model, "COUNTROWS(FILTER(ALL(DimEmployee), TRUE()))") == 4
    # A sale of an unknown store and one of an unknown agent's store: both (Blank).
    by_agent = rows(model, 'EVALUATE SUMMARIZECOLUMNS(DimEmployee[FirstName], "S", [Total Sales])')
    assert sorted(by_agent, key=lambda r: str(r[0])) == [
        ["Ana", 340.0],
        ["Bo", 130.0],
        [None, 12.0],
    ]


def test_filter_arguments_on_one_column_intersect(model) -> None:
    """The first engine kept only the second: 'total sales in 2008' came out as 510M."""
    december = (
        "CALCULATE([Total Sales], FactSales[DateKey] >= DATE(2009,12,1), "
        "FactSales[DateKey] < DATE(2010,1,1))"
    )
    assert value(model, december) == 192
    assert (
        value(model, "CALCULATE([Total Sales], 'Date'[Year] = 2009, 'Date'[Year] = 2008)") is None
    )
    # KEEPFILTERS intersects with the outer filter; a plain filter replaces it.
    assert (
        value(
            model,
            "CALCULATE(CALCULATE([Total Sales], KEEPFILTERS('Date'[Year] = 2009)), "
            "'Date'[Year] = 2008)",
        )
        is None
    )
    assert (
        value(
            model, "CALCULATE(CALCULATE([Total Sales], 'Date'[Year] = 2009), 'Date'[Year] = 2008)"
        )
        == 392
    )


def test_context_transition_in_an_iterator(model) -> None:
    assert value(model, "[Big Sellers]") == 2  # Ana 340, Bo 130; the blank row has 12
    assert value(model, "CALCULATE([Big Sellers], 'Date'[Year] = 2008)") is None
    # Ranked against every key including the blank row (12); a blank value ranks as 0.
    ranks = rows(model, 'EVALUATE SUMMARIZECOLUMNS(DimEmployee[EmployeeKey], "R", [Rank])')
    assert dict((r[0], r[1]) for r in ranks) == {1: 1, 2: 2, None: 3, 3: 4}
    # Grouped by name, the name filter stays while RANKX replaces the key's: each agent
    # is ranked among the rows of that name only, as Power BI does.
    by_name = rows(model, 'EVALUATE SUMMARIZECOLUMNS(DimEmployee[FirstName], "R", [Rank])')
    assert {r[1] for r in by_name} == {1}


def test_month_to_date_and_the_date_table_rule(model) -> None:
    assert value(model, "[MTD Sales]") == 192  # December 2009, the calendar's last month
    # A filter on the date key removes the Date table's outer filters, as in Power BI:
    # the Year = 2009 filter no longer applies inside.
    both_years = "DATESBETWEEN('Date'[Date], DATE(2008,1,1), DATE(2009,12,31))"
    assert (
        value(model, f"CALCULATE(CALCULATE([Total Sales], {both_years}), 'Date'[Year] = 2009)")
        == 482
    )
    # Filter arguments of one CALCULATE still intersect with it.
    assert value(model, f"CALCULATE([Total Sales], 'Date'[Year] = 2009, {both_years})") == 392


def _first(model: Model, dates: str) -> object:
    return value(model, f"CALCULATE(MIN('Date'[Date]), {dates})")


def _last(model: Model, dates: str) -> object:
    return value(model, f"CALCULATE(MAX('Date'[Date]), {dates})")


def test_dateadd_maps_whole_months_and_moves_other_days(model) -> None:
    february_2009 = (
        "FILTER(ALL('Date'[Date]), 'Date'[Date] >= DATE(2009,2,1) && "
        "'Date'[Date] <= DATE(2009,2,28))"
    )
    assert value(model, f"COUNTROWS(DATEADD({february_2009}, 1, MONTH))") == 31
    assert value(model, f"COUNTROWS(DATEADD({february_2009}, -12, MONTH))") == 29  # leap Feb
    # One day moves as one day: 28 Feb 2009 a year back is 28 Feb 2008 only.
    single = "DATEADD(FILTER(ALL('Date'[Date]), 'Date'[Date] = DATE(2009,2,28)), -1, YEAR)"
    assert _first(model, single) == _last(model, single) == datetime(2008, 2, 28)
    march_31 = "DATEADD(FILTER(ALL('Date'[Date]), 'Date'[Date] = DATE(2009,3,31)), -1, MONTH)"
    assert _first(model, march_31) == datetime(2009, 2, 28)
    assert value(model, "CALCULATE([PM Sales], 'Date'[Year] = 2009)") == 392 - 192  # Dec-08..Nov-09
    assert value(model, "CALCULATE([PY Sales], 'Date'[Year] = 2009)") == 90


def test_datesinperiod_clamps_to_the_calendar_and_keeps_month_ends(model) -> None:
    def window(start: str, n: int, unit: str) -> tuple[object, object, object]:
        dates = f"DATESINPERIOD('Date'[Date], {start}, {n}, {unit})"
        return value(model, f"COUNTROWS({dates})"), _first(model, dates), _last(model, dates)

    # 28 Feb 2009 back a year ends at 29 Feb 2008, so the window starts on 1 March.
    assert window("DATE(2009,2,28)", -1, "YEAR")[1] == datetime(2008, 3, 1)
    # A blank start or one before the calendar starts at its first date.
    assert window("BLANK()", -1, "YEAR") == (1, datetime(2008, 1, 1), datetime(2008, 1, 1))
    assert window("DATE(2007,6,1)", 1, "YEAR")[0] == 366
    # One after the calendar returns no dates.
    assert window("DATE(2010,6,30)", -1, "YEAR") == (None, None, None)


def test_rankx_ties_and_topn_ties(model) -> None:
    table = "{10, 20, 20, 30}"
    assert value(model, f"RANKX({table}, [Value], 20)") == 2
    assert value(model, f"RANKX({table}, [Value], 10)") == 4
    assert value(model, f"RANKX({table}, [Value], 10, , DENSE)") == 3
    assert value(model, f"RANKX({table}, [Value], 20, ASC)") == 2
    # TOPN keeps every row tied at the cut.
    assert len(rows(model, f"EVALUATE TOPN(2, {table}, [Value], DESC)")) == 3


def test_queries_name_columns_as_power_bi_does(model) -> None:
    result = run_query(
        model,
        'EVALUATE ADDCOLUMNS(VALUES(DimChannel[ChannelName]), "Sales", [Total Sales]) '
        "ORDER BY [Sales] DESC",
    )
    assert result.columns == ["DimChannel[ChannelName]", "[Sales]"]
    assert result.rows == [["Store", 312.0], ["Online", 170.0]]
    top = rows(
        model,
        'EVALUATE TOPN(1, SUMMARIZECOLUMNS(DimStore[StoreName], "S", [Total Sales]), [S], DESC)',
    )
    assert top == [["North", 340.0]]
    assert value(model, "[Label]") == "482.00"


def test_errors_read_like_power_bis(model) -> None:
    with pytest.raises(
        DaxEngineError, match="SUMMARIZE expects a column name as argument number 2"
    ):
        rows(model, "EVALUATE SUMMARIZE(FactSales, [Total Sales])")
    with pytest.raises(DaxEngineError, match="A single value for column 'SalesAmount'"):
        value(model, "FactSales[SalesAmount]")
    with pytest.raises(DaxEngineError, match="cannot be found"):
        value(model, "SUM(FactSales[Units])")
    with pytest.raises(DaxUnsupported, match="USERELATIONSHIP"):
        value(model, "CALCULATE([Total Sales], USERELATIONSHIP(FactSales[DateKey], 'Date'[Date]))")
    with pytest.raises(DaxEngineLimit):
        Engine(model, time_budget=0).evaluate_scalar(
            "SUMX(GENERATESERIES(1, 200000), [Value] * [Value])"
        )
