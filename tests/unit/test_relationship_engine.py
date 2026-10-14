from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from ai_powerbi_analyst.contracts import (
    RelationshipCandidate,
    SourceFile,
    SourceKind,
    SourceManifest,
)
from ai_powerbi_analyst.data.onboarding import (
    DataOnboardingService,
    _identifier,
    _profile_frames,
)
from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio.relationship_engine import RelationshipGovernanceEngine


def _manifest(tmp_path: Path, tables: dict[str, pd.DataFrame]) -> SourceManifest:
    tmp_path.mkdir(parents=True, exist_ok=True)
    sources: list[SourceFile] = []
    profiles = []
    for name, frame in tables.items():
        table_id = _identifier(name)
        path = tmp_path / f"{table_id}.parquet"
        frame.to_parquet(path, index=False)
        profile = _profile_frames(
            (frame,),
            table_id,
            name,
            sheet=None,
            source_id=table_id,
        )
        profiles.append(profile)
        sources.append(
            SourceFile(
                id=table_id,
                original_name=path.name,
                snapshot_path=str(path),
                sha256=sha256_file(path),
                size_bytes=path.stat().st_size,
                kind=SourceKind.PARQUET,
            )
        )
    return SourceManifest(
        import_id="snapshot-relationship-test",
        imported_at=datetime.now(UTC),
        sources=sources,
        tables=profiles,
    )


def test_full_snapshot_relationship_is_normalized_toward_unique_side(tmp_path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "Customers": pd.DataFrame({"customer_id": [1, 2]}),
            "Orders": pd.DataFrame({"customer_id": [1, 1, 2], "amount": [10, 20, 30]}),
        },
    )

    result = RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest)

    relationship = next(
        item
        for item in result.relationships
        if item.from_column_id == "customer_id" and item.to_column_id == "customer_id"
    )
    assert relationship.from_table_id == "orders"
    assert relationship.to_table_id == "customers"
    assert relationship.cardinality == "many_to_one"
    assert relationship.cross_filter_direction == "single"
    assert relationship.is_active
    assert relationship.rely_on_referential_integrity
    assert not relationship.blockers
    assert any("100.00%" in evidence for evidence in relationship.evidence)


def test_fresh_unique_keys_require_both_direction_even_when_model_requests_single(tmp_path):
    manifest = _manifest(
        tmp_path,
        {
            "Devices": pd.DataFrame({"serial_id": [31, 47, 82]}),
            "Warranties": pd.DataFrame({"serial_id": [31, 47, 82]}),
        },
    )
    engine = RelationshipGovernanceEngine(tmp_path / "spill")
    proposed = engine.propose(manifest).relationships[0]
    assert proposed.cardinality == "one_to_one"
    assert proposed.cross_filter_direction == "both"
    stale = proposed.model_copy(update={"cross_filter_direction": "single"})
    checked = engine.govern(manifest, [stale]).relationships[0]
    assert checked.cross_filter_direction == "both"
    assert any("require Both" in item for item in checked.evidence)


def test_orphans_block_referential_integrity_claim(tmp_path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "Customers": pd.DataFrame({"customer_id": [1, 2]}),
            "Orders": pd.DataFrame({"customer_id": [1, 1, 3]}),
        },
    )

    relationship = (
        RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest).relationships[0]
    )

    assert relationship.from_table_id == "orders"
    assert relationship.to_table_id == "customers"
    assert not relationship.rely_on_referential_integrity
    assert any("orphan row" in blocker for blocker in relationship.blockers)
    assert any("66.67%" in evidence for evidence in relationship.evidence)


def test_keyed_subset_list_relates_without_blocker(tmp_path) -> None:
    """Superstore Returns lists 296 of 5,009 orders: low coverage, but not a wrong join."""
    manifest = _manifest(
        tmp_path,
        {
            "Orders": pd.DataFrame(
                {
                    "Order ID": ["A", "A", "B", "C", "C", "D", "E", "F", "G", "H"],
                    "Sales": [1.0] * 10,
                }
            ),
            "Returns": pd.DataFrame({"Returned": ["Yes", "Yes"], "Order ID": ["A", "C"]}),
        },
    )

    relationship = (
        RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest).relationships[0]
    )

    assert relationship.from_table_id == "orders"
    assert relationship.to_table_id == "returns"
    assert relationship.cardinality == "many_to_one"
    assert relationship.is_active
    assert not relationship.blockers
    assert not relationship.rely_on_referential_integrity
    assert any(line.startswith("Subset table:") for line in relationship.evidence)


def test_many_to_many_is_blocked_and_bridge_is_only_recommended(tmp_path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "Orders": pd.DataFrame({"customer_id": [1, 1]}),
            "Events": pd.DataFrame({"customer_id": [1, 1]}),
        },
    )

    result = RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest)
    relationship = result.relationships[0]

    assert relationship.cardinality == "many_to_many"
    assert any("Unsupported many-to-many" in blocker for blocker in relationship.blockers)
    assert any("duplicate row" in blocker for blocker in relationship.blockers)
    assert any("bridge" in evidence.casefold() for evidence in relationship.evidence)
    assert result.recommendations
    assert len(manifest.tables) == 2
    assert all("bridge" not in table.id for table in manifest.tables)


def test_nulls_on_proposed_one_side_are_blocked(tmp_path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "Customers": pd.DataFrame({"customer_id": [1, None]}),
            "Orders": pd.DataFrame({"customer_id": [1, 1]}),
        },
    )

    relationship = (
        RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest).relationships[0]
    )

    assert any("One-side column contains 1 null" in item for item in relationship.blockers)
    assert not relationship.rely_on_referential_integrity


def test_zero_full_snapshot_overlap_is_an_explicit_blocker(tmp_path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "Customers": pd.DataFrame({"customer_id": [1, 2]}),
            "Orders": pd.DataFrame({"customer_id": [3, 3, 4]}),
        },
    )

    relationship = (
        RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest).relationships[0]
    )

    assert "No non-null key values match across the full snapshots" in relationship.blockers
    assert any("0.00%" in evidence for evidence in relationship.evidence)


def test_onboarding_routes_normalized_parquet_through_full_governance(tmp_path) -> None:
    manifest = _manifest(
        tmp_path / "snapshots",
        {
            "Customers": pd.DataFrame({"customer_id": [1, 2]}),
            "Orders": pd.DataFrame({"customer_id": [3, 3, 4]}),
        },
    )

    relationship = DataOnboardingService(tmp_path / "state").propose_relationships(manifest)[0]

    assert "No non-null key values match across the full snapshots" in relationship.blockers


def test_type_mismatch_is_blocked_before_relationship_can_be_approved(tmp_path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "Integer Keys": pd.DataFrame({"customer_id": [1, 2]}),
            "Text Keys": pd.DataFrame({"customer_id": ["A", "B"]}),
        },
    )

    relationship = (
        RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest).relationships[0]
    )

    assert any("data types differ" in blocker for blocker in relationship.blockers)
    assert not relationship.rely_on_referential_integrity


def test_active_cycle_and_parallel_filter_paths_are_blocked(tmp_path) -> None:
    cycle_manifest = _manifest(
        tmp_path / "cycle",
        {
            "Alpha": pd.DataFrame({"key": [1, 2]}),
            "Beta": pd.DataFrame({"key": [1, 2]}),
            "Gamma": pd.DataFrame({"key": [1, 2]}),
        },
    )
    engine = RelationshipGovernanceEngine(tmp_path / "spill")

    cycle_result = engine.propose(cycle_manifest)

    assert len(cycle_result.relationships) == 3
    assert (
        sum(
            any("cycle" in blocker.casefold() for blocker in item.blockers)
            for item in cycle_result.relationships
        )
        == 1
    )

    parallel_manifest = _manifest(
        tmp_path / "parallel",
        {
            "Left": pd.DataFrame({"id": [1, 2], "code": ["x", "y"]}),
            "Right": pd.DataFrame({"id": [1, 2], "code": ["x", "y"]}),
        },
    )

    parallel_result = engine.propose(parallel_manifest)

    assert len(parallel_result.relationships) == 2
    assert (
        sum(
            any("another relationship" in blocker for blocker in item.blockers)
            for item in parallel_result.relationships
        )
        == 1
    )


def test_govern_preserves_reviewed_filter_settings_after_revalidation(tmp_path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "Customers": pd.DataFrame({"customer_id": [1, 2]}),
            "Orders": pd.DataFrame({"customer_id": [1, 1, 2]}),
        },
    )
    proposed = RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest).relationships[0]
    reviewed = proposed.model_copy(
        update={
            "cross_filter_direction": "both",
            "is_active": False,
            "review_status": "approved",
        }
    )

    relationship = (
        RelationshipGovernanceEngine(tmp_path / "spill")
        .govern(manifest, [reviewed])
        .relationships[0]
    )

    assert relationship.cross_filter_direction == "both"
    assert not relationship.is_active
    assert relationship.review_status == "approved"
    assert relationship.rely_on_referential_integrity


def test_legacy_cross_filter_input_is_accepted_but_serializes_canonical_field() -> None:
    relationship = RelationshipCandidate(
        id="legacy_relationship",
        from_table_id="orders",
        from_column_id="customer_id",
        to_table_id="customers",
        to_column_id="customer_id",
        cardinality="many_to_one",
        cross_filter="both",  # type: ignore[call-arg]
        confidence=1,
        evidence=["Reviewed"],
    )

    payload = relationship.model_dump()

    assert relationship.cross_filter_direction == "both"
    assert payload["cross_filter_direction"] == "both"
    assert "cross_filter" not in payload


def test_star_schema_traps_resolve_to_best_practice_relationships(tmp_path) -> None:
    days = pd.date_range("2024-01-01", periods=60, freq="D")
    lines = range(1, 121)
    manifest = _manifest(
        tmp_path,
        {
            "Customers": pd.DataFrame(
                {"CustomerID": range(1, 31), "Segment": ["A", "B", "C"] * 10}
            ),
            "Dates": pd.DataFrame({"Date": days, "Month": days.month}),
            "Products": pd.DataFrame(
                {"ProductKey": range(1, 11), "SubcategoryKey": [1, 2, 3, 1, 2, 3, 1, 2, 3, 1]}
            ),
            "Subcategories": pd.DataFrame(
                {"SubcategoryKey": [1, 2, 3], "Subcategory": ["Road", "Lights", "Gloves"]}
            ),
            "Sales": pd.DataFrame(
                {
                    "SalesLineID": lines,
                    "CustomerKey": [1 + i % 30 for i in lines],
                    # One unknown product: a warning, not a reason to drop the join.
                    "ProductKey": [99 if i == 7 else 1 + i % 10 for i in lines],
                    "OrderDate": [days[i % 50] for i in lines],
                    "ShipDate": [days[i % 50 + 3] for i in lines],
                    # Values 1-10 overlap every 1-based surrogate key.
                    "Quantity": [1 + i % 10 for i in lines],
                }
            ),
            "Returns": pd.DataFrame(
                {
                    "ReturnID": range(1, 21),
                    "ProductKey": [1 + i % 10 for i in range(20)],
                    "CustomerID": [1 + i % 30 for i in range(20)],
                    "ReturnDate": [days[i + 5] for i in range(20)],
                }
            ),
        },
    )

    result = RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest)
    usable = {
        (item.from_table_id, item.from_column_id, item.to_table_id, item.to_column_id): item
        for item in result.relationships
        if not item.blockers
    }

    assert set(usable) == {
        ("sales", "customerkey", "customers", "customerid"),
        ("sales", "productkey", "products", "productkey"),
        ("sales", "orderdate", "dates", "date"),
        ("sales", "shipdate", "dates", "date"),
        ("returns", "productkey", "products", "productkey"),
        ("returns", "customerid", "customers", "customerid"),
        ("returns", "returndate", "dates", "date"),
        ("products", "subcategorykey", "subcategories", "subcategorykey"),
    }
    assert all(item.cardinality == "many_to_one" for item in usable.values())
    assert all(item.cross_filter_direction == "single" for item in usable.values())
    ship = usable[("sales", "shipdate", "dates", "date")]
    assert not ship.is_active
    assert any("USERELATIONSHIP('Sales'[ShipDate]" in line for line in ship.evidence)
    assert usable[("sales", "orderdate", "dates", "date")].is_active
    orphaned = usable[("sales", "productkey", "products", "productkey")]
    assert not orphaned.rely_on_referential_integrity
    assert any(line.startswith("Warning:") for line in orphaned.evidence)
    # No value columns or row IDs, no fact-to-fact joins, and two facts sharing
    # dimensions are not a "cycle".
    involved = {
        column
        for item in result.relationships
        for column in (item.from_column_id, item.to_column_id)
    }
    assert not involved & {"quantity", "month", "saleslineid", "returnid"}
    assert any("share Products" in note for note in result.recommendations)


@pytest.mark.parametrize("dimension_keys", [["A", "a"], ["A", "A "]])
def test_text_one_side_uniqueness_uses_import_comparison_semantics(
    tmp_path, dimension_keys
) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "Customers": pd.DataFrame({"customer_id": dimension_keys}),
            "Orders": pd.DataFrame({"customer_id": ["A", "A", "A"]}),
        },
    )
    relationship = (
        RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest).relationships[0]
    )
    assert relationship.blockers
    assert any("duplicate row" in item for item in relationship.blockers)
    assert not relationship.rely_on_referential_integrity


def test_text_integrity_ignores_case_and_trailing_spaces_but_preserves_leading_spaces(
    tmp_path,
) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "Customers": pd.DataFrame({"customer_id": ["A", "B"]}),
            "Orders": pd.DataFrame({"customer_id": ["a", "A ", "b"]}),
        },
    )
    relationship = (
        RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest).relationships[0]
    )
    assert not relationship.blockers
    assert relationship.rely_on_referential_integrity
    leading = _manifest(
        tmp_path / "leading",
        {
            "Customers": pd.DataFrame({"customer_id": ["A", "B"]}),
            "Orders": pd.DataFrame({"customer_id": [" A", " A", " A"]}),
        },
    )
    checked = RelationshipGovernanceEngine(tmp_path / "spill").propose(leading).relationships[0]
    assert "No non-null key values match across the full snapshots" in checked.blockers


def test_directed_three_table_filter_cycle_is_blocked(tmp_path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "Alpha": pd.DataFrame({"ID": [1, 2], "Parent": [1, 1]}),
            "Beta": pd.DataFrame({"ID": [1, 2], "Parent": [1, 1]}),
            "Gamma": pd.DataFrame({"ID": [1, 2], "Parent": [1, 1]}),
        },
    )
    links = [
        RelationshipCandidate(
            id=f"rel_{many}_{one}",
            from_table_id=many,
            from_column_id="parent",
            to_table_id=one,
            to_column_id="id",
            cardinality="many_to_one",
            confidence=1,
            evidence=["Explicit selected key"],
            review_status="approved",
        )
        for many, one in [("alpha", "beta"), ("beta", "gamma"), ("gamma", "alpha")]
    ]
    relationships = (
        RelationshipGovernanceEngine(tmp_path / "spill").govern(manifest, links).relationships
    )
    assert sum(bool(item.blockers) for item in relationships) == 1
    assert any("cycle" in reason for item in relationships for reason in item.blockers)


def test_datetime_integrity_retains_the_time_component(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "Dates": pd.DataFrame({"TimestampKey": pd.to_datetime(["2024-01-01", "2024-01-02"])}),
            "Orders": pd.DataFrame(
                {
                    "TimestampKey": pd.to_datetime(
                        [
                            "2024-01-01 00:00",
                            "2024-01-01 00:00",
                            "2024-01-01 12:00",
                            "2024-01-02 00:00",
                        ]
                    )
                }
            ),
        },
    )
    selected = RelationshipCandidate(
        id="rel_exact_timestamp",
        from_table_id="orders",
        from_column_id="timestampkey",
        to_table_id="dates",
        to_column_id="timestampkey",
        cardinality="many_to_one",
        confidence=1,
        evidence=["Explicit timestamp key"],
    )
    relationship = (
        RelationshipGovernanceEngine(tmp_path / "spill")
        .govern(manifest, [selected])
        .relationships[0]
    )
    assert relationship.cardinality == "many_to_one"
    assert not relationship.rely_on_referential_integrity
    assert any("3 matched and 1 orphan row(s)" in line for line in relationship.evidence)
