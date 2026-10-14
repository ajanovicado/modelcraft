from __future__ import annotations

from ai_powerbi_analyst.data.onboarding import DataOnboardingService, UploadedSource


def test_csv_import_is_snapshotted_and_profiled(tmp_path) -> None:
    service = DataOnboardingService(tmp_path)
    manifest = service.import_uploads(
        [
            UploadedSource(
                name="sales.csv",
                content=(b"Date,Region,Sales,Cost\n2026-01-01,East,100,60\n2026-02-01,West,150,\n"),
            )
        ]
    )

    assert service.load_latest() == manifest
    assert len(manifest.sources) == 1
    table = manifest.tables[0]
    assert table.row_count == 2
    sales = next(column for column in table.columns if column.id == "sales")
    cost = next(column for column in table.columns if column.id == "cost")
    assert sales.data_type == "integer"
    assert sales.minimum == 100
    assert sales.maximum == 150
    assert cost.null_count == 1
    assert not cost.candidate_key


def test_relationship_proposals_block_unverified_many_to_many(tmp_path) -> None:
    service = DataOnboardingService(tmp_path)
    manifest = service.import_uploads(
        [
            UploadedSource(name="orders.csv", content=b"customer_id,total\n1,10\n1,20\n"),
            UploadedSource(name="events.csv", content=b"customer_id,event\n1,A\n1,B\n"),
        ]
    )

    candidates = service.propose_relationships(manifest)

    customer = next(
        item
        for item in candidates
        if item.from_column_id == "customer_id" and item.to_column_id == "customer_id"
    )
    assert customer.cardinality == "many_to_many"
    assert customer.blockers == ["Neither side is a verified unique key"]
