"""Native proposal names are current, bounded and never a local evaluation receipt."""

from __future__ import annotations

from copy import deepcopy

import pytest

from ai_powerbi_analyst.studio.dax_patterns import (
    MetricSelection,
    PatternCatalog,
    PatternError,
    PatternField,
    PatternSelection,
    build_pattern,
    catalog_for_native_proposal,
    select_business_pattern,
)
from ai_powerbi_analyst.studio.report_import import ImportedReport


@pytest.fixture
def report() -> ImportedReport:
    measures = [
        {"TableName": "Sales", "Name": name, "Expression": expression}
        for name, expression in [
            ("Revenue", "SUM('Sales'[Amount])"),
            ("Discount", "SUM('Sales'[DiscountAmount])"),
            ("Net", "[Revenue] - [Discount]"),
            ("Rows", "COUNTROWS('Sales')"),
            ("Broken", "SUM('Sales'[Missing])"),
            ("Circular", "[Circular] + 1"),
            ("Target", "125"),
        ]
    ]
    return ImportedReport(
        id="native-pattern-report",
        workspace_id="native-pattern-workspace",
        name="Native proposals",
        source_path="fixture-only.pbix",
        archived_path="fixture-only.pbix",
        source_hash="a" * 64,
        base_hash="a" * 64,
        format="PBIX",
        pages=[],
        measures=measures,
        model={
            "tables": [
                {"name": "Sales"},
                {"name": "Calendar", "isDateTable": True, "dateColumn": "Date"},
                {"name": "LocalProducts"},
                {"name": "Unreviewed"},
            ],
            "columns": [
                {"table": table, "name": name, "dataTypeName": dtype}
                for table, name, dtype in [
                    ("Sales", "Amount", "double"),
                    ("Sales", "DiscountAmount", "decimal"),
                    ("Sales", "Date", "dateTime"),
                    ("Calendar", "Date", "dateTime"),
                    ("LocalProducts", "ID", "int64"),
                    ("Unreviewed", "Value", "double"),
                ]
            ],
            "partitions": [
                {"table": table, "mode": mode}
                for table, mode in [
                    ("Sales", "DirectQuery"),
                    ("Calendar", "Dual"),
                    ("LocalProducts", "Import"),
                    ("Unreviewed", "Unknown"),
                ]
            ],
            "measures": [{"table": item["TableName"], "name": item["Name"]} for item in measures],
            "relationships": [
                {
                    "fromTable": "Sales",
                    "fromColumn": "Date",
                    "toTable": "Calendar",
                    "toColumn": "Date",
                    "cardinality": "M:1",
                    "crossFilter": "single",
                    "active": True,
                }
            ],
        },
    )


@pytest.fixture
def local() -> PatternCatalog:
    return PatternCatalog(
        (
            PatternField(
                1,
                "measure",
                "Sales",
                "Revenue",
                available=False,
                unavailable_reason="Native storage cannot be evaluated locally.",
            ),
            PatternField(2, "column", "LocalProducts", "ID", "number", ("LocalProducts",)),
            PatternField(
                3, "table", "LocalProducts", "LocalProducts", source_tables=("LocalProducts",)
            ),
            PatternField(
                4,
                "column",
                "Unreviewed",
                "Value",
                available=False,
                unavailable_reason="Storage metadata is unknown.",
            ),
        ),
        revision="head-current",
    )


def field(catalog: PatternCatalog, table: str, name: str, kind: str = "column") -> PatternField:
    return next(
        item
        for item in catalog.fields
        if item.table == table and item.name == name and item.kind == kind
    )


def ytd(catalog: PatternCatalog):
    return build_pattern(
        PatternSelection(
            pattern="period_to_date",
            base=MetricSelection(field=field(catalog, "Sales", "Revenue", "measure").index),
            date=field(catalog, "Calendar", "Date").index,
            period="year",
        ),
        catalog,
    )


@pytest.mark.parametrize("mode", ["DirectQuery", "Dual", "DirectLake", "Hybrid"])
def test_native_modes_add_names_types_and_keep_local_choice_ids(report, local, mode):
    report.model["partitions"][0]["mode"] = mode if mode != "Hybrid" else "Import"
    if mode == "Hybrid":
        report.model["partitions"].append({"table": "Sales", "mode": "DirectQuery"})
    catalog = catalog_for_native_proposal(report, local=local, revision="head-current")
    assert catalog.revision == "head-current"
    assert field(catalog, "LocalProducts", "ID").index == 2
    revenue = field(catalog, "Sales", "Revenue", "measure")
    assert revenue.index == 1 and revenue.available and revenue.source_tables == ("Sales",)
    assert field(catalog, "Sales", "Amount").data_type == "number"
    assert field(catalog, "Unreviewed", "Value").available is False
    built = select_business_pattern("total DiscountAmount divided by Revenue", catalog)
    assert (
        built is not None and built.expression == "DIVIDE(SUM('Sales'[DiscountAmount]), [Revenue])"
    )
    assert not any(
        "computed" in note.casefold() or "evaluated" in note.casefold() for note in built.notes
    )


def test_current_measure_dependency_names_are_recursive_and_never_replaced_with_values(report):
    catalog = catalog_for_native_proposal(report)
    assert field(catalog, "Sales", "Net", "measure").source_tables == ("Sales",)
    assert field(catalog, "Sales", "Rows", "measure").source_tables == ("Sales",)
    assert field(catalog, "Sales", "Broken", "measure").available is False
    assert field(catalog, "Sales", "Circular", "measure").available is False
    assert field(catalog, "Sales", "Target", "measure").available is True
    assert all(
        not hasattr(item, "expression") and not hasattr(item, "values") for item in catalog.fields
    )


def test_current_errors_missing_measure_receipts_and_unknown_types_cannot_be_lifted(report, local):
    report.model["columns"][0].pop("dataTypeName")
    report.model["measures"][1]["state"] = "needsRecalculation"
    report.model["measures"] = [
        item for item in report.model["measures"] if item["name"] != "Target"
    ]
    catalog = catalog_for_native_proposal(report, local=local)
    assert field(catalog, "Sales", "Amount").available is False
    for name in ("Revenue", "Discount", "Target"):
        assert field(catalog, "Sales", name, "measure").available is False
    with pytest.raises(PatternError, match="metadata|type"):
        select_business_pattern("total Amount divided by Target", catalog)


def test_marked_native_calendar_is_not_a_contiguity_or_values_claim(report):
    catalog = catalog_for_native_proposal(report)
    key = field(catalog, "Calendar", "Date")
    assert key.date_key and key.calendar_contiguous is None
    built = ytd(catalog)
    assert built.expression == "TOTALYTD([Revenue], 'Calendar'[Date])"
    assert any(
        "calendar" in note.casefold() and "review" in note.casefold() for note in built.notes
    )
    report.model["tables"][1]["isDateTable"] = False
    unmarked = catalog_for_native_proposal(report)
    assert field(unmarked, "Calendar", "Date").date_key is False
    with pytest.raises(PatternError, match="date key"):
        ytd(unmarked)


@pytest.mark.parametrize(
    "change",
    [
        {"crossFilter": "both"},
        {"cardinality": "M:M"},
        {"cardinality": "1:1"},
        {"toColumn": "Missing"},
        {"joinOnDate": "datePartOnly"},
        {"active": False},
    ],
)
def test_unsupported_or_inactive_paths_cannot_enable_context_patterns(report, change):
    report.model["relationships"][0].update(change)
    catalog = catalog_for_native_proposal(report)
    with pytest.raises(PatternError, match="relationships|active relationship"):
        ytd(catalog)
    assert select_business_pattern("total DiscountAmount divided by Revenue", catalog) is not None


def test_reversed_supported_many_to_one_metadata_preserves_filter_direction(report):
    report.model["relationships"][0].update(
        {
            "fromTable": "Calendar",
            "fromColumn": "Date",
            "toTable": "Sales",
            "toColumn": "Date",
            "cardinality": "1:M",
        }
    )
    catalog = catalog_for_native_proposal(report)
    assert catalog.relationships_complete is True
    assert [(item.many_table, item.one_table) for item in catalog.relationships] == [
        ("Sales", "Calendar")
    ]


@pytest.mark.parametrize(
    "section",
    ["tables", "columns", "partitions", "measures", "calculationGroups", "fieldParameters"],
)
def test_truncated_metadata_is_terminal_for_closed_native_proposals(report, section):
    report.model["truncated"] = [section]
    with pytest.raises(PatternError, match="truncated"):
        catalog_for_native_proposal(report)


@pytest.mark.parametrize("facts", [None, {}, {"tables": [], "columns": [], "status": "reading"}])
def test_unknown_or_still_reading_metadata_cannot_supply_names(report, facts):
    report = report.model_copy(update={"model": facts})
    with pytest.raises(PatternError, match="metadata"):
        catalog_for_native_proposal(report)


def test_unavailable_import_rows_or_mismatched_revision_are_never_revived(report, local):
    blocked = PatternField(
        9,
        "column",
        "LocalProducts",
        "ID",
        "number",
        ("LocalProducts",),
        available=False,
        unavailable_reason="Rows changed after the snapshot.",
    )
    catalog = catalog_for_native_proposal(report, local=PatternCatalog((blocked,)))
    assert field(catalog, "LocalProducts", "ID") == blocked
    assert not any(item.table == "Unreviewed" for item in catalog.fields)
    with pytest.raises(PatternError, match="different model revision"):
        catalog_for_native_proposal(report, local=local, revision="head-new")


def test_missing_closed_slots_and_unknown_names_stay_terminal(report):
    catalog = catalog_for_native_proposal(report)
    with pytest.raises(PatternError):
        select_business_pattern("total Revenue divided by", catalog)
    with pytest.raises(PatternError, match="field"):
        select_business_pattern("total InventedSales divided by Revenue", catalog)
    with pytest.raises(PatternError, match="denominator"):
        build_pattern(
            PatternSelection(
                pattern="ratio",
                numerator=MetricSelection(
                    field=field(catalog, "Sales", "Revenue", "measure").index
                ),
            ),
            catalog,
        )
    before = deepcopy(report.model)
    catalog_for_native_proposal(report)
    assert report.model == before
