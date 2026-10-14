"""DAX lab filters and groups on tables several many-to-one hops from the aggregate.

Shaped like an imported sales report: FactSales -> DimStore -> DimEmployee, plus
FactSales -> DimChannel and FactSales -> Date. The numbers are checked against
plain DuckDB LEFT JOIN chains, which is how Power BI's (Blank) member behaves: a sale
whose store, or whose store's employee, is missing lands on the blank row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from ai_powerbi_analyst.studio.aggregates import AggregatePlanError, AggregateService
from ai_powerbi_analyst.studio.contracts import (
    AdvancedDaxProposal,
    DatasetSnapshot,
    ReadOnlyAggregatePlan,
    RelationshipProposal,
)
from ai_powerbi_analyst.studio.dax import DaxSyntaxError, evaluate_dax
from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
from ai_powerbi_analyst.studio.inventory import inventory_folder

FILES = {
    "FactSales.csv": (
        "SalesKey,DateKey,StoreKey,ChannelKey,SalesAmount\n"
        "1,2024-01-05,10,1,100\n"
        "2,2024-01-20,10,2,50\n"
        "3,2024-02-03,11,1,200\n"
        "4,2024-03-10,12,1,70\n"
        "5,2025-01-02,13,2,40\n"
        "6,2025-02-14,14,1,30\n"
        # Store 99 does not exist; 2025-03-02 is missing from Date.
        "7,2025-03-01,99,2,25\n"
        "8,2025-03-02,11,,10\n"
    ),
    "DimStore.csv": (
        "StoreKey,StoreName,EmployeeKey\n"
        "10,North,1\n"
        "11,South,2\n"
        "12,East,1\n"
        # Employee 9 does not exist, Harbor has no manager, Quiet has no sales.
        "13,West,9\n"
        "14,Harbor,\n"
        "15,Quiet,3\n"
    ),
    # Department 300 does not exist.
    "DimEmployee.csv": (
        "EmployeeKey,FirstName,Gender,DepartmentKey\n"
        "1,Ann,F,100\n2,Bob,M,200\n3,Cy,M,100\n4,Dee,F,300\n"
    ),
    "DimDepartment.csv": "DepartmentKey,DepartmentName\n100,Field\n200,Retail\n",
    "DimChannel.csv": "ChannelKey,ChannelName,ManagerKey\n1,Store,2\n2,Online,4\n",
    "Date.csv": (
        "Date,Year\n2024-01-05,2024\n2024-01-20,2024\n2024-02-03,2024\n2024-03-10,2024\n"
        "2025-01-02,2025\n2025-02-14,2025\n2025-03-01,2025\n"
    ),
}


@dataclass
class Model:
    snapshot: DatasetSnapshot
    aggregates: AggregateService
    paths: dict[str, str]
    relationships: dict[str, RelationshipProposal]

    def evaluate(self, expression: str, *names: str, measures=()) -> object:  # type: ignore[no-untyped-def]
        chosen = names or ("store", "employee", "channel", "date", "department")
        result = evaluate_dax(
            self.snapshot,
            list(measures),
            expression,
            50,
            self.aggregates,
            relationships=[self.relationships[name] for name in chosen],
        )
        if expression.startswith("EVALUATE"):
            return result.rows
        (row,) = result.rows
        return next(iter(row.values()))

    def sql(self, query: str) -> list[tuple]:
        """A plain DuckDB query over the snapshot tables, named as in the model."""
        with duckdb.connect() as connection:
            for name, path in self.paths.items():
                # Views take no prepared parameters; the path is a test temp path.
                literal = path.replace("'", "''")
                connection.execute(
                    f"CREATE VIEW \"{name}\" AS SELECT * FROM read_parquet('{literal}')"
                )
            return connection.execute(query).fetchall()


_JOINED = (
    '"FactSales" f LEFT JOIN "DimStore" s ON f."StoreKey" = s."StoreKey" '
    'LEFT JOIN "DimEmployee" e ON s."EmployeeKey" = e."EmployeeKey"'
)


@pytest.fixture
def model(tmp_path: Path) -> Model:
    root = tmp_path / "report"
    root.mkdir()
    for name, text in FILES.items():
        (root / name).write_text(text, encoding="utf-8")
    snapshot, _ = StudioIngestionService(tmp_path / "state").ingest(
        "workspace-multihop", [item for item in inventory_folder(root) if item.selectable]
    )
    tables = {item.table.name: item for item in snapshot.tables}

    def column(table: str, name: str) -> str:
        return next(c.id for c in tables[table].table.columns if c.name == name)

    def relationship(
        rel_id: str, many: str, many_column: str, one: str, one_column: str, **update: object
    ) -> RelationshipProposal:
        return RelationshipProposal(
            id=rel_id,
            from_table_id=tables[many].table.id,
            from_column_id=column(many, many_column),
            to_table_id=tables[one].table.id,
            to_column_id=column(one, one_column),
            cardinality="many_to_one",
            confidence=1.0,
            evidence=["Imported from the original Power BI model"],
            review_status="approved",
            **update,  # type: ignore[arg-type]
        )

    relationships = {
        "store": relationship("rel_store", "FactSales", "StoreKey", "DimStore", "StoreKey"),
        "employee": relationship(
            "rel_employee", "DimStore", "EmployeeKey", "DimEmployee", "EmployeeKey"
        ),
        "channel": relationship(
            "rel_channel", "FactSales", "ChannelKey", "DimChannel", "ChannelKey"
        ),
        "date": relationship("rel_date", "FactSales", "DateKey", "Date", "Date"),
        "department": relationship(
            "rel_department", "DimEmployee", "DepartmentKey", "DimDepartment", "DepartmentKey"
        ),
        # A second two-hop route to DimEmployee, as a channel manager.
        "manager": relationship(
            "rel_manager", "DimChannel", "ManagerKey", "DimEmployee", "EmployeeKey"
        ),
    }
    return Model(
        snapshot=snapshot,
        aggregates=AggregateService(tmp_path / "spill"),
        paths={name: item.normalized_parquet_path for name, item in tables.items()},
        relationships=relationships,
    )


def test_groups_two_hops_away_match_a_plain_join_chain(model: Model) -> None:
    rows = model.evaluate(
        'EVALUATE SUMMARIZECOLUMNS(DimEmployee[FirstName], "Sales", SUM(FactSales[SalesAmount]))'
    )
    expected = model.sql(f'SELECT e."FirstName", sum(f."SalesAmount") FROM {_JOINED} GROUP BY 1')
    assert {(row["FirstName"], row["Sales"]) for row in rows} == set(expected)
    # Unknown store 99, missing employee 9 and Harbor's empty key share the blank row;
    # Cy and Dee sell nothing, so SUMMARIZECOLUMNS leaves them out.
    assert rows == [
        {"FirstName": "Ann", "Sales": 220},
        {"FirstName": "Bob", "Sales": 210},
        {"FirstName": None, "Sales": 95},
    ]
    ordered = model.evaluate(
        "EVALUATE SUMMARIZECOLUMNS('DimEmployee'[Gender], \"Sales\", "
        "SUM('FactSales'[SalesAmount])) ORDER BY 'DimEmployee'[Gender] ASC"
    )
    assert ordered == [
        {"Gender": None, "Sales": 95},
        {"Gender": "F", "Sales": 220},
        {"Gender": "M", "Sales": 210},
    ]
    # One hop still works beside it: channels and years.
    by_channel = model.evaluate(
        'EVALUATE SUMMARIZECOLUMNS(DimChannel[ChannelName], "Sales", SUM(FactSales[SalesAmount]))'
    )
    assert {(r["ChannelName"], r["Sales"]) for r in by_channel} == set(
        model.sql(
            'SELECT c."ChannelName", sum(f."SalesAmount") FROM "FactSales" f '
            'LEFT JOIN "DimChannel" c ON f."ChannelKey" = c."ChannelKey" GROUP BY 1'
        )
    )
    by_year = model.evaluate(
        'EVALUATE SUMMARIZECOLUMNS(Date[Year], "Sales", SUM(FactSales[SalesAmount]))'
    )
    assert {(r["Year"], r["Sales"]) for r in by_year} == {(2024, 420), (2025, 95), (None, 10)}


def test_filters_two_hops_away_keep_blank_semantics(model: Model) -> None:
    def reference(where: str) -> object:
        return model.sql(f'SELECT sum(f."SalesAmount") FROM {_JOINED} WHERE {where}')[0][0]

    total = "SUM(FactSales[SalesAmount])"
    ann = model.evaluate(f'CALCULATE({total}, DimEmployee[FirstName] = "Ann")')
    assert ann == reference("e.\"FirstName\" = 'Ann'") == 220
    # BLANK <> "Ann" is true: Bob and every unmatched sale.
    not_ann = model.evaluate(f'CALCULATE({total}, DimEmployee[FirstName] <> "Ann")')
    assert not_ann == reference("e.\"FirstName\" IS DISTINCT FROM 'Ann'") == 305
    # BLANK = "" is true in DAX: exactly the sales that reach no employee.
    assert model.evaluate(f'CALCULATE({total}, DimEmployee[FirstName] = "")') == 95
    assert model.evaluate('CALCULATE(COUNTROWS(FactSales), DimEmployee[Gender] = "F")') == 3
    # Filters through different paths combine on the same sales rows.
    assert (
        model.evaluate(
            f'CALCULATE({total}, DimEmployee[FirstName] = "Ann", DimChannel[ChannelName] = "Store")'
        )
        == 170
    )
    by_employee = model.evaluate(
        'EVALUATE SUMMARIZECOLUMNS(DimEmployee[FirstName], "Store sales", '
        f'CALCULATE({total}, DimChannel[ChannelName] = "Store"))'
    )
    assert by_employee == [
        {"FirstName": "Bob", "Store sales": 200},
        {"FirstName": "Ann", "Store sales": 170},
        {"FirstName": None, "Store sales": 30},
    ]
    # A filter on the table between keeps the grouping two hops out.
    assert model.evaluate(
        'EVALUATE SUMMARIZECOLUMNS(DimEmployee[FirstName], "Sales", '
        f'CALCULATE({total}, DimStore[StoreName] = "North"))'
    ) == [{"FirstName": "Ann", "Sales": 150}]
    # A share with its part two hops away, overall and per store.
    share = f'DIVIDE(CALCULATE({total}, DimEmployee[Gender] = "F"), {total})'
    assert model.evaluate(share) == pytest.approx(220 / 525)
    per_store = model.evaluate(
        f'EVALUATE SUMMARIZECOLUMNS(DimStore[StoreName], "F share", {share}) '
        "ORDER BY DimStore[StoreName] ASC"
    )
    assert per_store == [
        {"StoreName": "East", "F share": 1.0},
        {"StoreName": "North", "F share": 1.0},
    ]
    # A saved measure is filtered and grouped the same way.
    measure = AdvancedDaxProposal(
        id="total_sales",
        name="Total Sales",
        table_id=next(t.table.id for t in model.snapshot.tables if t.table.name == "FactSales"),
        expression=total,
        format_string="0",
        assumptions=["Imported"],
    )
    assert (
        model.evaluate(
            'CALCULATE([Total Sales], DimEmployee[FirstName] = "Bob")', measures=[measure]
        )
        == 210
    )
    # The grouped column itself cannot be filtered: DAX would replace the grouping.
    with pytest.raises(DaxSyntaxError, match="replaces the grouping"):
        model.evaluate(
            'EVALUATE SUMMARIZECOLUMNS(DimEmployee[FirstName], "S", '
            f'CALCULATE({total}, DimEmployee[FirstName] = "Ann"))'
        )


def test_two_equally_short_paths_are_refused_and_an_inactive_one_is_ignored(
    model: Model,
) -> None:
    grouped = 'EVALUATE SUMMARIZECOLUMNS(DimEmployee[FirstName], "S", SUM(FactSales[SalesAmount]))'
    with pytest.raises(DaxSyntaxError, match="more than one relationship path") as failure:
        model.evaluate(grouped, "store", "employee", "channel", "manager")
    assert "DimStore[EmployeeKey] → DimEmployee[EmployeeKey]" in str(failure.value)
    assert "DimChannel[ManagerKey] → DimEmployee[EmployeeKey]" in str(failure.value)
    model.relationships["manager"] = model.relationships["manager"].model_copy(
        update={"is_active": False}
    )
    assert len(model.evaluate(grouped, "store", "employee", "channel", "manager")) == 3
    # Alone, the manager route answers by channel manager instead.
    model.relationships["manager"] = model.relationships["manager"].model_copy(
        update={"is_active": True}
    )
    by_manager = model.evaluate(grouped, "channel", "manager")
    assert by_manager == [
        {"FirstName": "Bob", "S": 400},
        {"FirstName": "Dee", "S": 115},
        {"FirstName": None, "S": 10},
    ]


def test_the_shortest_path_wins(model: Model) -> None:
    # A direct FactSales -> DimStore hop beats a longer detour through DimChannel.
    detour = RelationshipProposal(
        id="rel_channel_store",
        from_table_id=model.relationships["manager"].from_table_id,
        from_column_id=model.relationships["manager"].from_column_id,
        to_table_id=model.relationships["store"].to_table_id,
        to_column_id=model.relationships["store"].to_column_id,
        cardinality="many_to_one",
        confidence=1.0,
        evidence=["Synthetic detour"],
        review_status="approved",
    )
    model.relationships["detour"] = detour
    rows = model.evaluate(
        'EVALUATE SUMMARIZECOLUMNS(DimStore[StoreName], "S", SUM(FactSales[SalesAmount]))',
        "store",
        "channel",
        "detour",
    )
    assert {(r["StoreName"], r["S"]) for r in rows} == set(
        model.sql(
            'SELECT s."StoreName", sum(f."SalesAmount") FROM "FactSales" f '
            'LEFT JOIN "DimStore" s ON f."StoreKey" = s."StoreKey" GROUP BY 1'
        )
    )


def test_three_hops_group_and_filter_like_the_join_chain(model: Model) -> None:
    chain = f'{_JOINED} LEFT JOIN "DimDepartment" d ON e."DepartmentKey" = d."DepartmentKey"'
    rows = model.evaluate(
        'EVALUATE SUMMARIZECOLUMNS(DimDepartment[DepartmentName], "Sales", '
        "SUM(FactSales[SalesAmount])) ORDER BY DimDepartment[DepartmentName] DESC"
    )
    assert rows == [
        {"DepartmentName": "Retail", "Sales": 210},
        {"DepartmentName": "Field", "Sales": 220},
        {"DepartmentName": None, "Sales": 95},
    ]
    assert {(r["DepartmentName"], r["Sales"]) for r in rows} == set(
        model.sql(f'SELECT d."DepartmentName", sum(f."SalesAmount") FROM {chain} GROUP BY 1')
    )
    by_channel = model.evaluate(
        'EVALUATE SUMMARIZECOLUMNS(DimChannel[ChannelName], "Field sales", '
        'CALCULATE(SUM(FactSales[SalesAmount]), DimDepartment[DepartmentName] = "Field"))'
    )
    assert {(r["ChannelName"], r["Field sales"]) for r in by_channel} == set(
        model.sql(
            f'SELECT c."ChannelName", sum(f."SalesAmount") FROM {chain} '
            'LEFT JOIN "DimChannel" c ON f."ChannelKey" = c."ChannelKey" '
            "WHERE d.\"DepartmentName\" = 'Field' GROUP BY 1"
        )
    )
    # Blank departments: unknown store, missing or empty employee, and none via Dee.
    assert (
        model.evaluate('CALCULATE(SUM(FactSales[SalesAmount]), DimDepartment[DepartmentName] = "")')
        == 95
    )


def test_a_missing_hop_is_named(model: Model) -> None:
    expression = 'CALCULATE(SUM(FactSales[SalesAmount]), DimEmployee[FirstName] = "Ann")'
    model.relationships["employee"] = model.relationships["employee"].model_copy(
        update={"review_status": "pending"}
    )
    with pytest.raises(DaxSyntaxError) as failure:
        model.evaluate(expression, "store", "employee")
    message = str(failure.value)
    assert message.startswith(
        "Include the DimStore → DimEmployee relationship in Review & build and approve it;"
    )
    assert "FactSales[StoreKey] → DimStore[StoreKey], DimStore[EmployeeKey]" in message
    # An inactive or absent hop leaves no path at all.
    model.relationships["employee"] = model.relationships["employee"].model_copy(
        update={"review_status": "approved", "is_active": False}
    )
    for names in (("store", "employee"), ("store",)):
        with pytest.raises(DaxSyntaxError, match="No active many-to-one relationship"):
            model.evaluate(expression, *names)


def test_execute_through_checks_that_each_hop_starts_where_the_last_ended(
    model: Model,
) -> None:
    fact = model.relationships["store"].from_table_id
    plan = ReadOnlyAggregatePlan(
        table_id=fact,
        operation="count",
        group_by_column_ids=["firstname"],
    )
    reversed_path = (model.relationships["employee"], model.relationships["store"])
    with pytest.raises(AggregatePlanError, match="start at the aggregate's table"):
        model.aggregates.execute_through(model.snapshot, plan, group_relationship=reversed_path)
    skipping = (model.relationships["store"], model.relationships["manager"])
    with pytest.raises(AggregatePlanError, match="start where the previous one ends"):
        model.aggregates.execute_through(model.snapshot, plan, group_relationship=skipping)
    inactive = model.relationships["employee"].model_copy(update={"is_active": False})
    with pytest.raises(AggregatePlanError, match="active relationship"):
        model.aggregates.execute_through(
            model.snapshot,
            plan,
            group_relationship=(model.relationships["store"], inactive),
        )


@pytest.mark.parametrize(
    "table,key",
    [("DimStore", "StoreKey"), ("DimEmployee", "EmployeeKey"), ("DimDepartment", "DepartmentKey")],
)
def test_multihop_checks_unique_keys_at_every_hop(model: Model, table: str, key: str) -> None:
    frame = pd.read_parquet(model.paths[table])
    pd.concat([frame, frame.iloc[[0]]], ignore_index=True).to_parquet(
        model.paths[table], index=False
    )
    with pytest.raises(AggregatePlanError, match=f"unique non-blank one-side key: {key}"):
        model.evaluate(
            'EVALUATE SUMMARIZECOLUMNS(DimDepartment[DepartmentName], "Sales", '
            "SUM(FactSales[SalesAmount]))"
        )


def test_related_filter_and_numerator_share_the_checked_join_grain(
    model: Model, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two filters use the same chain; each valid key is scanned once, and sales
    # retain their original grain even in a ratio's separately filtered numerator.
    from ai_powerbi_analyst.studio import aggregates

    checked = []
    verify = aggregates._verify_one_side_key

    def record_check(connection, path, column):  # type: ignore[no-untyped-def]
        checked.append((path, column.name))
        verify(connection, path, column)

    monkeypatch.setattr(aggregates, "_verify_one_side_key", record_check)
    assert model.evaluate(
        'DIVIDE(CALCULATE(SUM(FactSales[SalesAmount]), DimEmployee[FirstName] = "Ann", '
        'DimEmployee[Gender] = "F"), SUM(FactSales[SalesAmount]))'
    ) == pytest.approx(220 / 525)
    assert checked == [
        (model.paths["DimStore"], "StoreKey"),
        (model.paths["DimEmployee"], "EmployeeKey"),
    ]
