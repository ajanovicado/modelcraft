"""Contexts the chat edit evals run against.

``agents`` is the committed structure twin of the Agents Performance dashboard
(fixtures/imported/structure-twin.pbix): one 1920 x 1200 page, 15 visuals, with the
report's real visible captions. The twin's DataModel is filler, so the model (tables,
columns, measures) is written here by hand in the shape of the real one; nothing personal
is in it. ``multipage`` is the 5-page / 40-visual synthetic context that the 4B index
budget is measured on.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ai_powerbi_analyst.studio.edit_context import (
    EditContext,
    LastEdit,
    MeasureEntry,
    PageEntry,
    VisualEntry,
)
from ai_powerbi_analyst.studio.report_import import ImportedReport, read_report

REPO = Path(__file__).resolve().parents[2]
TWIN = REPO / "fixtures" / "imported" / "structure-twin.pbix"

_TABLES: dict[str, dict[str, str]] = {
    "FactSales": {
        "SalesAmount": "float64",
        "TotalCost": "float64",
        "DiscountAmount": "float64",
        "DateKey": "datetime64[ns]",
        "StoreKey": "int64",
        "channelKey": "int64",
    },
    "DimStore": {"StoreKey": "int64", "StoreName": "object"},
    "DimChannel": {"ChannelKey": "int64", "ChannelName": "object"},
    "DimEmployee": {"EmployeeKey": "int64", "FirstName": "object", "LastName": "object"},
    "Date": {"Date": "datetime64[ns]", "Year": "int64", "MonthName": "object"},
    "Clustered Employees": {"Clustered Employees": "object"},
    "Top-Bottom-N": {"Top-Bottom-N": "int64"},
    "Actuals-vs-PctChange": {"Top-Bottom-Field": "object"},
}
_MEASURES: dict[str, str] = {
    "Total Sales": "SUM ( FactSales[SalesAmount] )",
    "MTD Total Sales": "CALCULATE ( [Total Sales], DATESMTD ( 'Date'[Date] ) )",
    "Profit": "SUMX ( FactSales, FactSales[SalesAmount] - FactSales[TotalCost] )",
    # (No "Sales Target" measure: the real dashboard has none. Its only fixed number is the 100K
    # threshold inside the measure below, so "change the target to 120K" must find that one.)
    "Number of Employees selling more than 100K MTD": (
        "COUNTX ( FILTER ( ALL ( DimEmployee[EmployeeKey] ), [MTD Total Sales] >= 100000 ), "
        "DimEmployee[EmployeeKey] )"
    ),
    "Employees Avg MTD Sales": "AVERAGEX ( ALL ( DimEmployee[EmployeeKey] ), [MTD Total Sales] )",
    "Employees Avg MTD % Change": "DIVIDE ( [MTD Total Sales], [Total Sales] ) - 1",
    "Employees with Positive Change %": "DIVIDE ( 3, 10 )",
    "Table MTD Sales": "[MTD Total Sales]",
    "TopN/BottomN/ MTD Sales": "[MTD Total Sales]",
    "Rank": "RANKX ( ALL ( DimEmployee ), [MTD Total Sales] )",
    "dummy": "0",
    # measures the twin's visuals bind that nothing here changes
    "Employee Name": "SELECTEDVALUE ( DimEmployee[FirstName] )",
    "vs PM MOM%": "0",
    "vs Previous Month": "0",
    "TopN/BottomN/ Avg MTD Sales": "[Employees Avg MTD Sales]",
    "TopN/BottomN/ MTD % Change": "[Employees Avg MTD % Change]",
    "TopN/BottomN/ Avg Total Discount": "0",
}


def agents_report() -> ImportedReport:
    """The twin's report definition with the hand-written model."""
    pages, _, _ = read_report(TWIN)
    tables = [
        {"TableName": table, "ColumnName": column, "PandasDataType": dtype}
        for table, columns in _TABLES.items()
        for column, dtype in columns.items()
    ]
    measures = [
        {"TableName": "# Measures", "Name": name, "Expression": expression}
        for name, expression in _MEASURES.items()
    ]
    return ImportedReport(
        id="report-eval",
        workspace_id="workspace-eval",
        name="Agents Performance - Dashboard",
        source_path=str(TWIN),
        archived_path=str(TWIN),
        source_hash="0" * 64,
        base_hash="0" * 64,
        format="PBIR",
        pages=pages,
        tables=tables,
        measures=measures,
    )


def agents_context(**options: Any) -> EditContext:
    return EditContext.from_report(agents_report(), **options)


def _entry(
    index: int, page: PageEntry, kind: str, caption: str, x: float, y: float, w: float, h: float
) -> VisualEntry:
    return VisualEntry(
        id=f"{page.id}-v{index:02d}",
        page_id=page.id,
        page_name=page.name,
        visual_type=kind,
        caption=caption,
        fields=(caption,),
        x=x,
        y=y,
        width=w,
        height=h,
        z=index * 1000,
    )


def multipage_context() -> EditContext:
    """Five pages of eight visuals each: what the 4B index budget is measured on."""
    pages = [
        PageEntry(id=f"page{i}", name=name, index=i, width=1280.0, height=720.0)
        for i, name in enumerate(
            ["Executive Summary", "Regional Performance", "Product Mix", "Store Detail", "Forecast"]
        )
    ]
    kinds = [
        ("cardVisual", "Total revenue this quarter compared with plan"),
        ("cardVisual", "Gross margin percentage after discounts"),
        ("clusteredColumnChart", "Revenue by region and product family"),
        ("lineChart", "Monthly revenue trend against last year"),
        ("pieChart", "Share of revenue by sales channel"),
        ("tableEx", "Top customers by revenue and margin"),
        ("slicer", "Fiscal year"),
        ("slicer", "Sales region"),
    ]
    visuals: list[VisualEntry] = []
    for page in pages:
        for i, (kind, caption) in enumerate(kinds):
            row, column = divmod(i, 4)
            visuals.append(
                _entry(
                    i,
                    page,
                    kind,
                    f"{caption} ({page.name})",
                    20 + column * 310,
                    20 + row * 340,
                    290,
                    300,
                )
            )
    measures = [
        MeasureEntry(f"Measure {i}", "# Measures", f"SUM ( Sales[Amount{i}] )") for i in range(12)
    ]
    return EditContext(
        pages=pages,
        visuals=visuals,
        measures=measures,
        tables={"Sales": [f"Amount{i}" for i in range(12)]},
        active_page="page0",
    )


def agents_sync_qa_context(**options: Any) -> EditContext:
    """The active Agents page plus a newly added blank Sync QA destination.

    Keep the real ChannelName/Channel slicer metadata. The destination reuses the
    synthetic Forecast page's id, renamed and placed second; it has no visuals.
    """
    context = agents_context(**options)
    destination = multipage_context().pages[-1]
    context.pages.append(replace(destination, name="Sync QA", index=1))
    context.active_page = context.active_page or context.pages[0].id
    return context


def context_for(context_id: str, options: dict[str, Any] | None = None) -> EditContext:
    """A named context; ``options`` may set selection, active_page and last_edit."""
    options = dict(options or {})
    last = options.pop("last_edit", None)
    selection = options.pop("selection", None)
    if context_id == "agents":
        context = agents_context(**options)
    elif context_id == "agents-sync-qa":
        context = agents_sync_qa_context(**options)
    elif context_id == "multipage":
        context = multipage_context()
    else:
        raise KeyError(f"unknown context {context_id!r}")
    if selection:
        matches = [v.id for v in context.visuals if v.id.startswith(str(selection))]
        if len(matches) != 1:
            raise KeyError(f"selection {selection!r} matches {len(matches)} visuals")
        context.selection = matches[0]
    if last:
        context.last_edit = LastEdit(
            revision_id=str(last.get("revision", "r0001")),
            ops=tuple(last.get("ops", ())),
            summary=str(last.get("summary", "")),
        )
    return context
