"""Stored Import keys and snapshot DAX must not diverge from model relationship facts."""

from __future__ import annotations

import pandas as pd
import pytest

from ai_powerbi_analyst.studio.dax_engine import (
    DaxEngineError,
    Engine,
    EngineRelationship,
    Model,
    run_query,
)


def model(keys: list[object], *, related: bool = True) -> Model:
    return Model.from_frames(
        {
            "Orders": pd.DataFrame({"Customer ID": ["A ", "B", " a"], "Sales": [100, 50, 20]}),
            "Customers": pd.DataFrame(
                {"Customer ID": keys, "Name": [f"Customer {i}" for i in range(len(keys))]}
            ),
        },
        relationships=[("Orders", "Customer ID", "Customers", "Customer ID")] if related else [],
        measures={"Total Sales": "SUM(Orders[Sales])"},
        measure_table="Orders",
    )


@pytest.mark.parametrize("keys", [[1, 1], ["A", "a"], ["A", "A "], [None, None]])
def test_duplicate_one_side_keys_refuse_snapshot_results(keys: list[object]) -> None:
    with pytest.raises(DaxEngineError, match="unique.*one-side|one-side.*unique"):
        model(keys)


def test_inactive_relationship_still_requires_a_unique_one_side() -> None:
    disconnected = model(["A", "a"], related=False)
    with pytest.raises(DaxEngineError, match="unique.*one-side|one-side.*unique"):
        Model(
            disconnected.tables.values(),
            [
                EngineRelationship(
                    "orders",
                    "customer id",
                    "customers",
                    "customer id",
                    active=False,
                )
            ],
            disconnected.measures.values(),
        )


def test_import_text_keys_trim_trailing_spaces_keep_leading_spaces_and_match_case() -> None:
    imported = model(["a", "B", " a"])
    assert imported.tables["orders"].column("Customer ID").value(0) == "A"
    assert imported.tables["orders"].column("Customer ID").value(2) == " a"
    assert imported.tables["customers"].blank_row is False
    engine = Engine(imported)
    assert engine.evaluate_scalar('CALCULATE([Total Sales], Customers[Name] = "Customer 0")') == 100
    assert engine.evaluate_scalar('CALCULATE([Total Sales], Customers[Name] = "Customer 2")') == 20


def test_native_import_model_allows_a_single_physical_blank_key() -> None:
    imported = model([None, "a", "B", " a"])
    assert Engine(imported).evaluate_scalar("[Total Sales]") == 170


def test_related_iterator_and_grouping_use_the_same_import_text_keys() -> None:
    imported = Model.from_frames(
        {
            "Orders": pd.DataFrame({"Key": ["A ", "b"], "Sales": [100, 50]}),
            "Customers": pd.DataFrame(
                {"Key": ["a", "B"], "Rate": [2, 3], "Name": ["North", "South"]}
            ),
        },
        relationships=[("Orders", "Key", "Customers", "Key")],
        measures={"Total Sales": "SUM(Orders[Sales])"},
        measure_table="Orders",
    )
    assert (
        Engine(imported).evaluate_scalar("SUMX(Orders, Orders[Sales] * RELATED(Customers[Rate]))")
        == 350
    )
    result = run_query(
        imported, 'EVALUATE SUMMARIZE(Orders, Customers[Name], "Sales", [Total Sales])'
    )
    assert sorted(result.rows) == [["North", 100], ["South", 50]]


def test_datetime_relationships_compare_full_timestamps() -> None:
    imported = Model.from_frames(
        {
            "Orders": pd.DataFrame(
                {
                    "Date": pd.to_datetime(["2025-01-01 00:00", "2025-01-01 12:00"]),
                    "Sales": [10, 30],
                }
            ),
            "Calendar": pd.DataFrame(
                {"Date": pd.to_datetime(["2025-01-01 00:00"]), "Label": ["First day"], "Rate": [2]}
            ),
        },
        relationships=[("Orders", "Date", "Calendar", "Date")],
        measures={"Sales": "SUM(Orders[Sales])"},
        measure_table="Orders",
    )
    assert (
        Engine(imported).evaluate_scalar('CALCULATE([Sales], Calendar[Label] = "First day")') == 10
    )
    assert (
        Engine(imported).evaluate_scalar("SUMX(Orders, Orders[Sales] * RELATED(Calendar[Rate]))")
        == 20
    )
    for query in [
        'EVALUATE SUMMARIZE(Orders, Calendar[Label], "Sales", [Sales])',
        'EVALUATE SUMMARIZECOLUMNS(Calendar[Label], "Sales", [Sales])',
    ]:
        assert dict(run_query(imported, query).rows) == {"First day": 10, None: 30}
    midnight = pd.Timestamp("2025-01-01 00:00").to_pydatetime()
    noon = pd.Timestamp("2025-01-01 12:00").to_pydatetime()
    assert dict(
        run_query(imported, 'EVALUATE SUMMARIZE(Orders, Calendar[Date], "Sales", [Sales])').rows
    ) == {midnight: 10, None: 30}
    assert dict(
        run_query(imported, 'EVALUATE SUMMARIZE(Orders, Orders[Date], "Sales", [Sales])').rows
    ) == {midnight: 10, noon: 30}


def test_physical_blank_key_joins_without_absorbing_unmatched_fact_rows() -> None:
    imported = Model.from_frames(
        {
            "Orders": pd.DataFrame({"Key": [None, "a", "Z"], "Sales": [10, 20, 7]}),
            "Customers": pd.DataFrame(
                {"Key": [None, "A"], "Name": ["Physical blank", "A"], "Rate": [3, 2]}
            ),
        },
        relationships=[("Orders", "Key", "Customers", "Key")],
        measures={"Sales": "SUM(Orders[Sales])"},
        measure_table="Orders",
    )
    engine = Engine(imported)
    assert engine.evaluate_scalar('CALCULATE([Sales], Customers[Name] = "Physical blank")') == 10
    assert engine.evaluate_scalar("SUMX(Orders, Orders[Sales] * RELATED(Customers[Rate]))") == 70
    assert (
        engine.evaluate_scalar('CONCATENATEX(Orders, RELATED(Customers[Name]), ",")')
        == "Physical blank,A,"
    )
    expected = {"Physical blank": 10, "A": 20, None: 7}
    for query in [
        'EVALUATE SUMMARIZE(Orders, Customers[Name], "Sales", [Sales])',
        'EVALUATE SUMMARIZECOLUMNS(Customers[Name], "Sales", [Sales])',
    ]:
        assert dict(run_query(imported, query).rows) == expected


def test_import_case_variants_form_one_group_without_repeating_the_total() -> None:
    imported = Model.from_frames(
        {
            "Orders": pd.DataFrame({"Key": ["A", "B"], "Sales": [100, 50]}),
            "Customers": pd.DataFrame({"Key": ["A", "B"], "Region": ["North", "north "]}),
        },
        relationships=[("Orders", "Key", "Customers", "Key")],
        measures={"Sales": "SUM(Orders[Sales])"},
        measure_table="Orders",
    )
    for query in [
        'EVALUATE SUMMARIZE(Orders, Customers[Region], "Sales", [Sales])',
        'EVALUATE SUMMARIZECOLUMNS(Customers[Region], "Sales", [Sales])',
    ]:
        assert run_query(imported, query).rows == [["North", 150]]
