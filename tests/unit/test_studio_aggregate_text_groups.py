"""Case variants must form one Import group without changing fact grain or Blank rows."""

from pathlib import Path

import pandas as pd
import pytest

from ai_powerbi_analyst.studio.aggregates import AggregateService
from ai_powerbi_analyst.studio.contracts import (
    AggregateFilter,
    ReadOnlyAggregatePlan,
    RelationshipProposal,
)
from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
from ai_powerbi_analyst.studio.inventory import inventory_folder


def _inputs(tmp_path: Path):  # type: ignore[no-untyped-def]
    source = tmp_path / "sources"
    source.mkdir()
    pd.DataFrame(
        {
            "CustomerKey": ["A", "B", "C", "D", "Orphan"],
            "Region": ["North", "north ", "South", " North", None],
            "Segment": ["Retail", "retail ", "Wholesale", "Retail", None],
            "Sales": [100, 50, 20, 30, 40],
        }
    ).to_parquet(source / "Orders.parquet", index=False)
    pd.DataFrame(
        {
            "CustomerKey": ["A", "B", "C", "D"],
            "Region": ["North", "north ", "South", " North"],
            "Segment": ["Retail", "retail ", "Wholesale", "Retail"],
        }
    ).to_parquet(source / "Customers.parquet", index=False)
    snapshot, manifest = StudioIngestionService(tmp_path / "state").ingest(
        "text-group-test", inventory_folder(source)
    )
    orders = next(table for table in manifest.tables if table.name == "Orders")
    customers = next(table for table in manifest.tables if table.name == "Customers")
    relationship = RelationshipProposal(
        id="rel_customer",
        from_table_id=orders.id,
        from_column_id="customerkey",
        to_table_id=customers.id,
        to_column_id="customerkey",
        cardinality="many_to_one",
        confidence=1,
        evidence=["Distinct customer keys; unmatched order remains Blank"],
        review_status="approved",
    )
    return snapshot, orders, customers, relationship


@pytest.mark.parametrize("method", ["standalone", "direct", "through"])
@pytest.mark.parametrize("groups", [["region"], ["region", "segment"]])
def test_text_groups_collapse_import_case_and_trailing_space_variants(
    tmp_path: Path, method: str, groups: list[str]
) -> None:
    snapshot, orders, customers, relationship = _inputs(tmp_path)
    plan = ReadOnlyAggregatePlan(
        table_id=orders.id,
        operation="sum",
        value_column_id="sales",
        group_by_column_ids=groups,
    )
    service = AggregateService(tmp_path / "spill")
    if method == "standalone":
        receipt = service.execute(snapshot, plan)
    elif method == "direct":
        receipt = service.execute_related(
            snapshot, plan, group_table_id=customers.id, relationship=relationship
        )
    else:
        receipt = service.execute_through(snapshot, plan, group_relationship=relationship)
    assert {row["group_1"]: row["value"] for row in receipt.rows} == {
        "North": 150,
        "South": 20,
        " North": 30,
        None: 40,
    }
    assert len(receipt.rows) == 4
    if len(groups) == 2:
        assert {row["group_1"]: row["group_2"] for row in receipt.rows} == {
            "North": "Retail",
            "South": "Wholesale",
            " North": "Retail",
            None: None,
        }


@pytest.mark.parametrize("groups", [["region"], ["region", "segment"]])
def test_text_group_grand_total_keeps_blank_group_separate(
    tmp_path: Path, groups: list[str]
) -> None:
    snapshot, orders, _, _ = _inputs(tmp_path)
    receipt = AggregateService(tmp_path / "spill").execute(
        snapshot,
        ReadOnlyAggregatePlan(
            table_id=orders.id,
            operation="sum",
            value_column_id="sales",
            group_by_column_ids=groups,
        ),
        grand_total=True,
    )
    total = next(row for row in receipt.rows if row["is_grand_total"])
    assert total["value"] == 240
    assert all(total[f"group_{index + 1}"] is None for index in range(len(groups)))
    detail = [row for row in receipt.rows if not row["is_grand_total"]]
    assert len(detail) == 4
    assert next(row["value"] for row in detail if row["group_1"] is None) == 40
    assert sum(row["value"] for row in detail) == total["value"]


@pytest.mark.parametrize("method", ["standalone", "direct", "through", "related"])
@pytest.mark.parametrize("value", ["North", "NORTH "])
def test_selecting_a_text_group_filters_the_entire_import_group(
    tmp_path: Path, method: str, value: str
) -> None:
    snapshot, orders, customers, relationship = _inputs(tmp_path)
    condition = AggregateFilter(column_id="region", operator="eq", value=value)
    plan = ReadOnlyAggregatePlan(
        table_id=orders.id,
        operation="sum",
        value_column_id="sales",
        group_by_column_ids=["region"],
        filters=[] if method == "related" else [condition],
    )
    service = AggregateService(tmp_path / "spill")
    if method == "standalone":
        receipt = service.execute(snapshot, plan)
    elif method == "direct":
        receipt = service.execute_related(
            snapshot, plan, group_table_id=customers.id, relationship=relationship
        )
    else:
        receipt = service.execute_through(
            snapshot,
            plan,
            group_relationship=relationship,
            related_filters=[(relationship, condition)] if method == "related" else None,
        )
    assert receipt.rows == [{"group_1": "North", "value": 150}]


@pytest.mark.parametrize("related", [False, True])
def test_text_group_filters_preserve_ratio_numerator_grain(tmp_path: Path, related: bool) -> None:
    snapshot, orders, _, relationship = _inputs(tmp_path)
    condition = AggregateFilter(column_id="region", operator="eq", value="NORTH ")
    plan = ReadOnlyAggregatePlan(
        table_id=orders.id,
        operation="ratio",
        value_column_id="sales",
        denominator_column_id="sales",
        numerator_operation="sum",
        denominator_operation="sum",
        numerator_filters=[] if related else [condition],
    )
    service = AggregateService(tmp_path / "spill")
    receipt = (
        service.execute_through(
            snapshot, plan, numerator_related_filters=[(relationship, condition)]
        )
        if related
        else service.execute(snapshot, plan)
    )
    assert receipt.rows == [{"value": pytest.approx(150 / 240)}]


def test_text_group_selection_row_preview_returns_all_matching_rows(tmp_path: Path) -> None:
    snapshot, orders, _, _ = _inputs(tmp_path)
    receipt = AggregateService(tmp_path / "spill").scan(
        snapshot,
        orders.id,
        filters=[AggregateFilter(column_id="region", operator="eq", value="North")],
    )
    assert sum(row["Sales"] for row in receipt.rows) == 150
    assert len(receipt.rows) == 2
