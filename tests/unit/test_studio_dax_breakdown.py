import csv
from pathlib import Path

import pytest

from ai_powerbi_analyst.studio.contracts import (
    CreateWorkspaceRequest,
    DaxEvaluateRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


@pytest.fixture
def categories(tmp_path: Path):
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    source = tmp_path / "customers.csv"
    rows = [
        [f"Customer {i:03}", "Consumer" if i % 2 else "Corporate", 1000 - i, i, i % 2 == 0]
        for i in range(110)
    ]
    rows.extend(
        [
            ["O'Brien", "Consumer", 12, 2, False],
            ["00123", "Corporate", 5, 2, True],
            [None, "Corporate", 7, 1, False],
        ]
    )
    with source.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(["Customer Name", "Segment", "Sales", "Cost", "VIP"])
        writer.writerows(rows)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Dynamic breakdown QA",
            selected_folder=str(tmp_path),
            idempotency_key="groups-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="groups-import",
            expected_base_hash=workspace.base_hash,
        )
    )
    table = snapshot.tables[0].table
    return service, workspace.id, table, rows


def run(categories, field=None, expression=None, **kwargs):
    service, wid, table, _ = categories
    preview = (
        None
        if field is None
        else {
            "column_id": next(c.id for c in table.columns if c.name == field),
            **kwargs.pop("context", {}),
        }
    )
    return service.evaluate_dax(
        DaxEvaluateRequest(
            workspace_id=wid,
            expression=expression or f"SUM('{table.name}'[Sales]) - SUM('{table.name}'[Cost])",
            preview=preview,
            **kwargs,
        )
    )


def test_fields_are_schema_driven_without_requiring_a_date(categories):
    service, wid, _, rows = categories
    before = service.workspace(wid).base_hash
    overall = run(categories)
    assert overall.date_fields == []
    assert {field.name for field in overall.preview_fields} == {
        "Customer Name",
        "Segment",
        "Sales",
        "Cost",
        "VIP",
    }
    grouped = run(categories, "Segment")
    assert grouped.date_preview is None and grouped.breakdown.grain is None
    expected = {
        segment: sum(r[2] - r[3] for r in rows if r[1] == segment)
        for segment in {r[1] for r in rows}
    }
    assert {r["Segment"]: r["Sales - Cost"] for r in grouped.rows} == expected
    assert sum(expected.values()) == overall.rows[0]["Sales - Cost"]
    assert service.workspace(wid).base_hash == before


def test_customer_search_covers_all_source_rows_and_keeps_literal_names(categories):
    bounded = run(categories, "Customer Name", top_n=100)
    assert bounded.truncated and len(bounded.rows) == 100
    assert "00123" not in {row["Customer Name"] for row in bounded.rows}
    for search, expected in [
        ("00123", "00123"),
        ("o'BRIEN", "O'Brien"),
        ("customer 109", "Customer 109"),
    ]:
        found = run(categories, "Customer Name", context={"search": search})
        assert len(found.rows) == 1 and found.rows[0]["Customer Name"] == expected
        details = run(
            categories,
            "Customer Name",
            context={"search": search},
            detail_group={"value": expected},
        )
        assert len(details.rows) == 1 and details.rows[0]["Customer Name"] == expected
    assert run(categories, "Customer Name", context={"search": "' OR 1=1 --"}).rows == []


def test_group_inspection_distinguishes_blanks_booleans_and_numeric_types(categories):
    _, _, _, rows = categories
    blank = run(categories, "Customer Name", detail_group={"value": None})
    assert len(blank.rows) == 1 and blank.rows[0]["Customer Name"] is None
    false_rows = run(categories, "VIP", detail_group={"value": False}, top_n=100)
    assert len(false_rows.rows) == sum(r[4] is False for r in rows)
    assert all(r["VIP"] is False for r in false_rows.rows)
    numeric = run(categories, "Cost", detail_group={"value": 0})
    assert len(numeric.rows) == 1 and numeric.rows[0]["Cost"] == 0
    with pytest.raises(ValueError, match="type"):
        run(categories, "Customer Name", detail_group={"value": 123})


def test_nonadditive_calculations_and_formula_filters_are_retained(categories):
    _, _, table, rows = categories
    average = run(categories, "Segment", expression=f"AVERAGE('{table.name}'[Sales])")
    for row in average.rows:
        source = [r[2] for r in rows if r[1] == row["Segment"]]
        assert row["AVERAGE(Sales)"] == pytest.approx(sum(source) / len(source))
    expr = f"CALCULATE(SUM('{table.name}'[Sales]), '{table.name}'[Segment] = \"Consumer\")"
    assert "Segment" not in {f.name for f in run(categories, expression=expr).preview_fields}
    details = run(
        categories, "Customer Name", expression=expr, detail_group={"value": "Customer 000"}
    )
    assert details.rows == []  # Customer 000 is Corporate, excluded by the formula.
    with pytest.raises(ValueError, match="without an explicit filter"):
        run(categories, "Segment", expression=expr)


def test_dynamic_preview_preserves_request_and_filter_bounds(categories):
    with pytest.raises(ValueError):
        run(categories, "Customer Name", context={"search": "x" * 101})
    with pytest.raises(ValueError):
        run(categories, "Customer Name", top_n=101)
    with pytest.raises(ValueError, match="date or datetime"):
        run(categories, "Segment", context={"grain": "month"})
    with pytest.raises(ValueError):
        run(categories, detail_group={"value": "Consumer"})
    _, _, table, _ = categories
    filters = ", ".join([f"'{table.name}'[Sales] > 0"] * 8)
    expr = f"CALCULATE(SUM('{table.name}'[Sales]), {filters})"
    with pytest.raises(ValueError, match="at most 8 filters"):
        run(categories, "Customer Name", expression=expr, context={"search": "Customer"})
    with pytest.raises(ValueError, match="at most 8 filters"):
        run(categories, "Customer Name", expression=expr, detail_group={"value": None})
