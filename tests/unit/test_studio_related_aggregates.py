from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from ai_powerbi_analyst.studio.aggregates import AggregatePlanError, AggregateService
from ai_powerbi_analyst.studio.contracts import ReadOnlyAggregatePlan, RelationshipProposal
from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
from ai_powerbi_analyst.studio.inventory import inventory_folder


def _related_snapshot(tmp_path: Path):  # type: ignore[no-untyped-def]
    source_root = tmp_path / "selected"
    source_root.mkdir()
    (source_root / "orders.csv").write_text(
        "Order ID,Region,Sales\nA-1,East,100\nA-2,West,250\nA-3,East,150\n",
        encoding="utf-8",
    )
    (source_root / "regions.csv").write_text(
        "Region,Manager\nEast,Ada\nWest,Grace\n",
        encoding="utf-8",
    )
    items = [item for item in inventory_folder(source_root) if item.selectable]
    return StudioIngestionService(tmp_path / "state").ingest("workspace-test", items)


def test_execute_related_groups_a_fact_measure_by_reviewed_dimension(
    tmp_path: Path,
) -> None:
    snapshot, manifest = _related_snapshot(tmp_path)
    orders = next(table for table in manifest.tables if table.name == "orders")
    regions = next(table for table in manifest.tables if table.name == "regions")
    sales = next(column for column in orders.columns if column.name == "Sales")
    order_region = next(column for column in orders.columns if column.name == "Region")
    region = next(column for column in regions.columns if column.name == "Region")
    relationship = RelationshipProposal(
        id="rel_orders_region_regions_region",
        from_table_id=orders.id,
        from_column_id=order_region.id,
        to_table_id=regions.id,
        to_column_id=region.id,
        cardinality="many_to_one",
        confidence=1,
        evidence=["The dimension key is unique and every fact key has a match"],
        review_status="approved",
    )

    receipt = AggregateService(tmp_path / "spill").execute_related(
        snapshot,
        ReadOnlyAggregatePlan(
            table_id=orders.id,
            operation="sum",
            value_column_id=sales.id,
            group_by_column_ids=[region.id],
        ),
        group_table_id=regions.id,
        relationship=relationship,
    )

    assert receipt.rows == [
        {"group_1": "East", "value": 250},
        {"group_1": "West", "value": 250},
    ]
    assert receipt.plan_hash
    assert receipt.result_hash


def test_execute_related_rejects_unreviewed_and_many_to_many_joins(
    tmp_path: Path,
) -> None:
    snapshot, manifest = _related_snapshot(tmp_path)
    orders = next(table for table in manifest.tables if table.name == "orders")
    regions = next(table for table in manifest.tables if table.name == "regions")
    sales = next(column for column in orders.columns if column.name == "Sales")
    order_region = next(column for column in orders.columns if column.name == "Region")
    region = next(column for column in regions.columns if column.name == "Region")
    plan = ReadOnlyAggregatePlan(
        table_id=orders.id,
        operation="sum",
        value_column_id=sales.id,
        group_by_column_ids=[region.id],
    )
    service = AggregateService(tmp_path / "spill")
    pending = RelationshipProposal(
        id="rel_orders_region_regions_region",
        from_table_id=orders.id,
        from_column_id=order_region.id,
        to_table_id=regions.id,
        to_column_id=region.id,
        cardinality="many_to_one",
        confidence=1,
        evidence=["Candidate only"],
    )
    with pytest.raises(AggregatePlanError, match="approved relationship"):
        service.execute_related(
            snapshot,
            plan,
            group_table_id=regions.id,
            relationship=pending,
        )

    many_to_many = pending.model_copy(
        update={"review_status": "approved", "cardinality": "many_to_many"}
    )
    with pytest.raises(AggregatePlanError, match="many-to-many"):
        service.execute_related(
            snapshot,
            plan,
            group_table_id=regions.id,
            relationship=many_to_many,
        )


def test_unmatched_fact_rows_remain_visible_as_the_blank_dimension_group(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "orders.csv").write_text(
        "Region,Sales\nEast,100\nWest,200\nUnknown,300\n",
        encoding="utf-8",
    )
    (source_root / "regions.csv").write_text("Region\nEast\nWest\n", encoding="utf-8")
    snapshot, manifest = StudioIngestionService(tmp_path / "state").ingest(
        "workspace-test",
        inventory_folder(source_root),
    )
    orders = next(table for table in manifest.tables if table.name == "orders")
    regions = next(table for table in manifest.tables if table.name == "regions")
    relationship = RelationshipProposal(
        id="rel_test",
        from_table_id=orders.id,
        from_column_id="region",
        to_table_id=regions.id,
        to_column_id="region",
        cardinality="many_to_one",
        confidence=1,
        evidence=["Unique dimension, one unmatched fact key reviewed"],
        review_status="approved",
    )
    receipt = AggregateService(tmp_path / "spill").execute_related(
        snapshot,
        ReadOnlyAggregatePlan(
            table_id=orders.id,
            operation="sum",
            value_column_id="sales",
            group_by_column_ids=["region"],
        ),
        group_table_id=regions.id,
        relationship=relationship,
    )
    assert sum(row["value"] for row in receipt.rows) == 600
    assert {"group_1": None, "value": 300} in receipt.rows


@pytest.mark.parametrize("method", ["direct", "through"])
@pytest.mark.parametrize(
    "bad_keys", [["East", "East", "West"], ["East", "east ", "West"], ["East", None, "West"]]
)
def test_related_aggregate_rechecks_one_side_keys_before_joining(
    tmp_path: Path, method: str, bad_keys: list[str | None]
) -> None:
    snapshot, manifest = _related_snapshot(tmp_path)
    orders = next(table for table in manifest.tables if table.name == "orders")
    regions = next(table for table in manifest.tables if table.name == "regions")
    dimension = next(item for item in snapshot.tables if item.table.id == regions.id)
    # Keep the previously reviewed profile intact: execution must check the actual rows.
    pd.DataFrame({"Region": bad_keys, "Manager": ["Ada", "Other", "Grace"]}).to_parquet(
        dimension.normalized_parquet_path, index=False
    )
    relationship = RelationshipProposal(
        id="rel_stale_keys",
        from_table_id=orders.id,
        from_column_id="region",
        to_table_id=regions.id,
        to_column_id="region",
        cardinality="many_to_one",
        confidence=1,
        evidence=["Previously unique key"],
        review_status="approved",
    )
    plan = ReadOnlyAggregatePlan(
        table_id=orders.id,
        operation="sum",
        value_column_id="sales",
        group_by_column_ids=["region"],
    )
    service = AggregateService(tmp_path / "spill")
    with pytest.raises(AggregatePlanError, match="unique non-blank"):
        if method == "direct":
            service.execute_related(
                snapshot, plan, group_table_id=regions.id, relationship=relationship
            )
        else:
            service.execute_through(snapshot, plan, group_relationship=relationship)


def test_direct_related_aggregate_rejects_inactive_relationship(tmp_path: Path) -> None:
    snapshot, manifest = _related_snapshot(tmp_path)
    orders = next(table for table in manifest.tables if table.name == "orders")
    regions = next(table for table in manifest.tables if table.name == "regions")
    relationship = RelationshipProposal(
        id="rel_inactive",
        from_table_id=orders.id,
        from_column_id="region",
        to_table_id=regions.id,
        to_column_id="region",
        cardinality="many_to_one",
        confidence=1,
        evidence=["Inactive role-playing relationship"],
        review_status="approved",
        is_active=False,
    )
    with pytest.raises(AggregatePlanError, match="active relationship"):
        AggregateService(tmp_path / "spill").execute_related(
            snapshot,
            ReadOnlyAggregatePlan(
                table_id=orders.id,
                operation="sum",
                value_column_id="sales",
                group_by_column_ids=["region"],
            ),
            group_table_id=regions.id,
            relationship=relationship,
        )


@pytest.mark.parametrize("method", ["direct", "through"])
def test_text_join_keys_follow_import_case_and_trailing_space_semantics(
    tmp_path: Path, method: str
) -> None:
    snapshot, manifest = _related_snapshot(tmp_path)
    orders = next(table for table in manifest.tables if table.name == "orders")
    regions = next(table for table in manifest.tables if table.name == "regions")
    fact = next(item for item in snapshot.tables if item.table.id == orders.id)
    pd.DataFrame(
        {
            "Order ID": ["A", "B", "C", "D"],
            "Region": ["EAST", "east ", "West", " West"],
            "Sales": [100, 150, 200, 300],
        }
    ).to_parquet(fact.normalized_parquet_path, index=False)
    relationship = RelationshipProposal(
        id="rel_text_keys",
        from_table_id=orders.id,
        from_column_id="region",
        to_table_id=regions.id,
        to_column_id="region",
        cardinality="many_to_one",
        confidence=1,
        evidence=["Unique dimension"],
        review_status="approved",
    )
    plan = ReadOnlyAggregatePlan(
        table_id=orders.id, operation="sum", value_column_id="sales", group_by_column_ids=["region"]
    )
    service = AggregateService(tmp_path / "spill")
    receipt = (
        service.execute_related(
            snapshot, plan, group_table_id=regions.id, relationship=relationship
        )
        if method == "direct"
        else service.execute_through(snapshot, plan, group_relationship=relationship)
    )
    values = {row["group_1"]: row["value"] for row in receipt.rows}
    assert values == {"East": 250, "West": 200, None: 300}


@pytest.mark.parametrize("method", ["direct", "through"])
@pytest.mark.parametrize("direction", ["single", "both"])
def test_one_to_one_runtime_checks_both_direction_and_both_unique_keys(
    tmp_path: Path, method: str, direction: str
) -> None:
    snapshot, manifest = _related_snapshot(tmp_path)
    orders = next(table for table in manifest.tables if table.name == "orders")
    regions = next(table for table in manifest.tables if table.name == "regions")
    relationship = RelationshipProposal(
        id="rel_invalid_one_to_one",
        from_table_id=orders.id,
        from_column_id="region",
        to_table_id=regions.id,
        to_column_id="region",
        cardinality="one_to_one",
        cross_filter_direction=direction,
        confidence=1,
        evidence=["Incorrect persisted cardinality"],
        review_status="approved",
    )
    plan = ReadOnlyAggregatePlan(
        table_id=orders.id, operation="sum", value_column_id="sales", group_by_column_ids=["region"]
    )
    service = AggregateService(tmp_path / "spill")
    with pytest.raises(
        AggregatePlanError,
        match="Both filter direction" if direction == "single" else "unique non-blank",
    ):
        if method == "direct":
            service.execute_related(
                snapshot, plan, group_table_id=regions.id, relationship=relationship
            )
        else:
            service.execute_through(snapshot, plan, group_relationship=relationship)


@pytest.mark.parametrize("method", ["direct", "through"])
def test_datetime_join_does_not_match_same_date_with_a_different_time(
    tmp_path: Path, method: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    pd.DataFrame(
        {
            "TimestampKey": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 12:00", "2024-01-02 00:00"]
            ),
            "Sales": [100, 300, 200],
        }
    ).to_parquet(source / "Orders.parquet", index=False)
    pd.DataFrame(
        {
            "TimestampKey": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "Label": ["First", "Second"],
        }
    ).to_parquet(source / "Dates.parquet", index=False)
    snapshot, manifest = StudioIngestionService(tmp_path / "state").ingest(
        "workspace-test", inventory_folder(source)
    )
    orders = next(table for table in manifest.tables if table.name == "Orders")
    dates = next(table for table in manifest.tables if table.name == "Dates")
    order_key = next(column for column in orders.columns if column.name == "TimestampKey")
    date_key = next(column for column in dates.columns if column.name == "TimestampKey")
    assert order_key.data_type.value == date_key.data_type.value == "datetime"
    relationship = RelationshipProposal(
        id="rel_timestamp",
        from_table_id=orders.id,
        from_column_id=order_key.id,
        to_table_id=dates.id,
        to_column_id=date_key.id,
        cardinality="many_to_one",
        confidence=1,
        evidence=["Exact timestamp relationship"],
        review_status="approved",
    )
    plan = ReadOnlyAggregatePlan(
        table_id=orders.id,
        operation="sum",
        value_column_id=next(column.id for column in orders.columns if column.name == "Sales"),
        group_by_column_ids=[next(column.id for column in dates.columns if column.name == "Label")],
    )
    service = AggregateService(tmp_path / "spill")
    receipt = (
        service.execute_related(snapshot, plan, group_table_id=dates.id, relationship=relationship)
        if method == "direct"
        else service.execute_through(snapshot, plan, group_relationship=relationship)
    )
    assert {row["group_1"]: row["value"] for row in receipt.rows} == {
        "First": 100,
        "Second": 200,
        None: 300,
    }
