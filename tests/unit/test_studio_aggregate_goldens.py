from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from ai_powerbi_analyst.studio import aggregates
from ai_powerbi_analyst.studio.aggregates import AggregateService, _bounded_connection
from ai_powerbi_analyst.studio.contracts import ReadOnlyAggregatePlan
from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
from ai_powerbi_analyst.studio.inventory import inventory_folder
from ai_powerbi_analyst.studio.planner import StudioPlanningService


def test_query_memory_tracks_available_ram(monkeypatch):
    for available_mib, expected in [(512, 128), (2048, 256), (4096, 512), (32768, 512)]:
        monkeypatch.setattr(
            aggregates.psutil,
            "virtual_memory",
            lambda mib=available_mib: SimpleNamespace(available=mib * 1024**2),
        )
        assert aggregates._query_memory_mib() == expected


def test_query_spill_is_isolated_and_cleaned_even_after_failure(tmp_path):
    with _bounded_connection(tmp_path, 5) as first:
        one = Path(first.execute("SELECT current_setting('temp_directory')").fetchone()[0])
        with _bounded_connection(tmp_path, 5) as second:
            two = Path(second.execute("SELECT current_setting('temp_directory')").fetchone()[0])
            assert one != two and one.parent == two.parent == tmp_path
            with (
                pytest.raises(aggregates.AggregatePlanError, match="busy"),
                _bounded_connection(tmp_path, 0.01),
            ):
                pass
    with pytest.raises(ValueError, match="test failure"), _bounded_connection(tmp_path, 5):
        raise ValueError("test failure")
    assert list(tmp_path.iterdir()) == []
    with _bounded_connection(tmp_path, 5) as recovered:
        assert recovered.execute("SELECT 42").fetchone() == (42,)


def test_large_sort_spills_to_disk_and_preserves_results(monkeypatch, tmp_path):
    monkeypatch.setattr(aggregates, "_query_memory_mib", lambda: 128)
    with _bounded_connection(tmp_path, 30) as connection:
        connection.execute(
            "CREATE TABLE stress AS SELECT i, repeat(md5(i::VARCHAR), 4) AS value "
            "FROM range(1000000) t(i) ORDER BY value"
        )
        assert connection.execute("SELECT count(*), sum(i) FROM stress").fetchone() == (
            1000000,
            499999500000,
        )
        assert (
            connection.execute(
                "SELECT coalesce(sum(size), 0) FROM duckdb_temporary_files()"
            ).fetchone()[0]
            > 0
        )
    assert list(tmp_path.iterdir()) == []


def test_aggregate_deadline_interrupts_work_and_disables_extension_downloads(
    tmp_path: Path,
) -> None:
    with _bounded_connection(tmp_path, 2) as connection:
        assert connection.execute(
            "SELECT current_setting('autoload_known_extensions')"
        ).fetchone() == (False,)
        assert connection.execute(
            "SELECT current_setting('autoinstall_known_extensions')"
        ).fetchone() == (False,)
    with (
        pytest.raises(duckdb.InterruptException),
        _bounded_connection(tmp_path, 0.05) as connection,
    ):
        connection.execute("SELECT sum(sin(i)) FROM range(1000000000) AS t(i)").fetchall()


@pytest.mark.parametrize(
    ("operation", "column", "expected"),
    [
        ("count", None, 4),
        ("count", "order_id", 3),
        ("distinct_count", "order_id", 3),
        ("sum", "sales", 600),
        ("average", "sales", 150),
    ],
)
def test_count_and_numeric_goldens_match_powerbi_semantics(
    tmp_path: Path,
    operation: str,
    column: str | None,
    expected: float,
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "orders.csv").write_text(
        "Order ID,Region,Sales\nA1,East,100\nA1,East,100\nA2,West,200\n,West,200\n",
        encoding="utf-8",
    )
    snapshot, manifest = StudioIngestionService(tmp_path / "state").ingest(
        "workspace-test",
        inventory_folder(source_root),
    )
    aggregates = AggregateService(tmp_path / "spill")
    plan = ReadOnlyAggregatePlan.model_validate(
        {
            "table_id": manifest.tables[0].id,
            "operation": operation,
            "value_column_id": column,
        }
    )
    assert aggregates.execute(snapshot, plan).rows == [{"value": expected}]
    empty = ReadOnlyAggregatePlan.model_validate(
        {
            **plan.model_dump(),
            "filters": [{"column_id": "region", "operator": "eq", "value": "None"}],
        }
    )
    assert aggregates.execute(snapshot, empty).rows == [{"value": None}]


def test_unique_sales_values_in_small_data_still_produce_real_kpis(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "orders.csv").write_text(
        "Order ID,Order Date,Region,Sales,Profit\n"
        "A1,2025-01-01,East,100.5,20.25\n"
        "A2,2025-02-01,West,250,40\n"
        "A3,2025-03-01,East,149.5,29.75\n",
        encoding="utf-8",
    )
    snapshot, manifest = StudioIngestionService(tmp_path / "state").ingest(
        "workspace-test",
        inventory_folder(sources),
    )
    planner = StudioPlanningService(None, AggregateService(tmp_path / "spill"))
    draft = planner.create_draft("workspace-test", snapshot, manifest, [], "Sales and profit")
    values = {preview.visual_id: preview.rows for preview in draft.previews}
    kpis = {
        visual.title: values[visual.id]
        for page in draft.pages
        for visual in page.visuals
        if visual.recipe == "kpi"
    }
    assert kpis["Total Sales"] == [{"value": 500.0}]
    assert kpis["Total Profit"] == [{"value": 90.0}]
    assert kpis["Distinct Order ID"] == [{"value": 3}]
