from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_powerbi_analyst.studio.contracts import CreateWorkspaceRequest, IngestRequest
from ai_powerbi_analyst.studio.data_access import TablePreviewRequest, preview_table
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


@pytest.fixture
def browse(tmp_path):
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    source = tmp_path / "rows.csv"
    source.write_text(
        "Name,Value,Day,Flag\n"
        + "".join(
            f"{'Group ' + str(i % 3) if i else ''},{i},2026-01-{i % 28 + 1:02d},"
            f"{'true' if i % 2 else 'false'}\n"
            for i in range(160)
        ),
        encoding="utf-8",
    )
    w = service.create_workspace(
        CreateWorkspaceRequest(
            name="Filter QA",
            selected_folder=str(tmp_path),
            expected_base_hash=EMPTY_HASH,
            idempotency_key="filter-create",
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=w.id,
            item_paths=[str(source)],
            expected_base_hash=w.base_hash,
            idempotency_key="filter-import",
        )
    )
    table = snapshot.tables[0].table
    ids = {c.name: c.id for c in table.columns}

    def query(**kwargs):
        return preview_table(
            snapshot, TablePreviewRequest(table_id=table.id, **kwargs), tmp_path / "spill"
        )

    return query, ids, service, snapshot


def test_filter_sort_and_pagination_cover_full_snapshot_without_mutation(browse):
    query, ids, service, snapshot = browse
    before = service.workspace(snapshot.workspace_id).base_hash
    options = dict(
        column_ids=[ids["Value"]],
        filters=[{"column_id": ids["Value"], "operator": "gte", "value": "100"}],
        sort={"column_id": ids["Value"], "direction": "desc"},
        page_size=25,
    )
    pages = [query(offset=offset, **options) for offset in (0, 25, 50)]
    assert [r[0] for p in pages for r in p["rows"]] == list(range(159, 99, -1))
    assert [p["has_more"] for p in pages] == [True, True, False]
    assert pages[0]["total_rows"] == 60 and pages[0]["snapshot_rows"] == 160
    assert service.workspace(snapshot.workspace_id).base_hash == before


def test_ties_are_stable_across_pages_and_nulls_sort_last(browse):
    query, ids, *_ = browse
    pages = [
        query(sort={"column_id": ids["Name"], "direction": "asc"}, page_size=40, offset=offset)
        for offset in (0, 40, 80, 120)
    ]
    values = [row[1] for page in pages for row in page["rows"]]
    assert values == sorted(range(1, 160), key=lambda i: (i % 3, i)) + [0]
    assert len(set(values)) == 160


def test_filters_combine_and_contains_treats_sql_wildcards_as_literal_text(browse):
    query, ids, *_ = browse
    result = query(
        filters=[
            {"column_id": ids["Name"], "operator": "contains", "value": "GROUP 1"},
            {"column_id": ids["Value"], "operator": "lt", "value": "10"},
        ]
    )
    assert [row[1] for row in result["rows"]] == [1, 4, 7]
    for text in ["%", "_", "' OR 1=1 --"]:
        assert (
            query(filters=[{"column_id": ids["Name"], "operator": "contains", "value": text}])[
                "total_rows"
            ]
            == 0
        )


def test_blank_date_and_boolean_filters_use_column_types(browse):
    query, ids, *_ = browse
    assert query(filters=[{"column_id": ids["Name"], "operator": "is_blank"}])["rows"][0][1] == 0
    assert (
        query(filters=[{"column_id": ids["Name"], "operator": "is_not_blank"}])["total_rows"] == 159
    )
    result = query(
        filters=[
            {"column_id": ids["Day"], "operator": "gte", "value": "2026-01-28"},
            {"column_id": ids["Flag"], "operator": "equals", "value": "true"},
        ]
    )
    assert [row[1] for row in result["rows"]] == [27, 55, 83, 111, 139]


def test_filter_and_sort_validation_rejects_injection_and_unbounded_requests(browse):
    query, ids, *_ = browse
    for kwargs in [
        {"sort": {"column_id": "value; DROP TABLE x", "direction": "asc"}},
        {"filters": [{"column_id": "unknown", "operator": "equals", "value": "1"}]},
        {"filters": [{"column_id": ids["Value"], "operator": "gte", "value": "NaN"}]},
        {"filters": [{"column_id": ids["Value"], "operator": "contains", "value": "1"}]},
        {"filters": [{"column_id": ids["Day"], "operator": "equals", "value": "yesterday"}]},
    ]:
        with pytest.raises(ValueError):
            query(**kwargs)
    for kwargs in [
        {"sort": {"column_id": ids["Value"], "direction": "DESC; DROP"}},
        {"filters": [{"column_id": ids["Value"], "operator": "equals"}] * 5},
        {"filters": [{"column_id": ids["Value"], "operator": "equals", "value": "1" * 501}]},
    ]:
        with pytest.raises(ValidationError):
            query(**kwargs)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e99999999", "1e-99999999", "oops"])
def test_numeric_filters_fail_closed_for_nonfinite_or_unrepresentable_values(browse, value):
    query, ids, *_ = browse
    with pytest.raises(ValueError, match="valid"):
        query(filters=[{"column_id": ids["Value"], "operator": "equals", "value": value}])
