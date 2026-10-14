from __future__ import annotations

from pathlib import Path

import pytest

from ai_powerbi_analyst.data.onboarding import DataOnboardingService, UploadedSource
from ai_powerbi_analyst.powerbi.calendar_table import (
    CalendarTableError,
    plan_calendar_table,
)


def test_no_date_profiles_do_not_create_a_calendar(tmp_path: Path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Region,Sales\nEast,10\nWest,20\n")]
    )

    assert plan_calendar_table(manifest) is None


def test_calendar_uses_complete_stable_year_bounds_and_valid_local_m(tmp_path: Path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="orders.csv",
                content=(
                    b"Order Date,Ship Date,Sales\n"
                    b"2024-07-15,2024-07-17,10\n"
                    b"2025-02-03,2025-02-04,20\n"
                ),
            ),
            UploadedSource(
                name="targets.csv",
                content=b"Effective Date,Target\n2023-11-20,100\n2024-01-10,120\n",
            ),
        ]
    )

    plan = plan_calendar_table(manifest)

    assert plan is not None
    assert plan.table_name == "Calendar"
    assert plan.table_id == "calendar"
    assert plan.start_date.isoformat() == "2023-01-01"
    assert plan.end_date.isoformat() == "2025-12-31"
    assert plan.model_reference_tmdl == "ref table Calendar"
    assert len(plan.source_bindings) == 2
    assert len(plan.relationships) == 2
    assert (
        next(
            binding for binding in plan.source_bindings if binding.table_id == manifest.tables[0].id
        ).column_id
        == "order_date"
    )
    assert "table Calendar" in plan.tmdl
    assert "\tdataCategory: Time" in plan.tmdl
    for column in ("Date", "Year", "'Month Number'", "Month", "Quarter", "'Year Month'"):
        assert f"\tcolumn {column}" in plan.tmdl
    assert "StartDate = #date(2023, 1, 1)" in plan.tmdl
    assert "EndDate = #date(2025, 12, 31)" in plan.tmdl
    assert "List.Dates(" in plan.tmdl
    assert "File.Contents" not in plan.tmdl
    assert 'Date.ToText([Date], "yyyy-MM", "en-US")' in plan.tmdl


def test_relationships_are_safe_active_many_to_one_specs(tmp_path: Path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="events.csv",
                content=b"Occurred At,Value\n2024-01-02 00:00:00,1\n2024-01-03 00:00:00,2\n",
            )
        ]
    )

    plan = plan_calendar_table(manifest)

    assert plan is not None
    relationship = plan.relationships[0]
    assert relationship.from_table_id == manifest.tables[0].id
    assert relationship.from_column_id == "occurred_at"
    assert relationship.to_table_id == plan.table_id
    assert relationship.to_column_id == "date"
    assert relationship.cardinality == "many_to_one"
    assert relationship.cross_filter_direction == "single"
    assert relationship.is_active
    assert not relationship.rely_on_referential_integrity
    assert relationship.review_status == "pending"
    assert not relationship.blockers
    assert any("date part" in item for item in relationship.evidence)
    assert plan.relationship_specs[0].join_on_date_behavior == "datePartOnly"
    assert plan.relationship_specs[0].tmdl_property == "joinOnDateBehavior: datePartOnly"


def test_explicit_review_order_selects_one_date_field_per_source_table(tmp_path: Path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="tickets.csv",
                content=(
                    b"Opened Date,Closed Date,Ticket\n"
                    b"2024-01-01,2024-02-10,A\n"
                    b"2024-01-02,2024-03-20,B\n"
                ),
            )
        ]
    )
    table = manifest.tables[0]

    plan = plan_calendar_table(
        manifest,
        reviewed_date_fields={table.id: ["closed_date", "opened_date"]},
    )

    assert plan is not None
    assert plan.source_bindings[0].column_id == "closed_date"
    assert len(plan.relationships) == 1


def test_calendar_name_avoids_case_insensitive_name_and_id_collisions(tmp_path: Path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(name="Calendar.csv", content=b"Date\n2024-01-01\n"),
            UploadedSource(name="Calendar 2.csv", content=b"Date\n2024-01-02\n"),
            UploadedSource(name="Facts.csv", content=b"Date\n2024-01-03\n"),
        ]
    )

    plan = plan_calendar_table(manifest)

    assert plan is not None
    assert plan.table_name == "Calendar 3"
    assert plan.table_id == "calendar_3"
    assert plan.model_reference_tmdl == "ref table 'Calendar 3'"
    assert plan.tmdl.startswith("table 'Calendar 3'\n")
    assert {item.to_table_id for item in plan.relationships} == {"calendar_3"}


def test_invalid_reviewed_fields_and_bounds_fail_closed(tmp_path: Path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="events.csv", content=b"Date,Value\n2024-01-01,1\n")]
    )
    table = manifest.tables[0]

    with pytest.raises(CalendarTableError, match="does not exist"):
        plan_calendar_table(manifest, reviewed_date_fields={table.id: ["missing_date"]})
    with pytest.raises(CalendarTableError, match="unknown table"):
        plan_calendar_table(manifest, reviewed_date_fields={"missing_table": ["date"]})

    date_column = table.columns[0].model_copy(
        update={"minimum": "2025-01-01T00:00:00", "maximum": "2024-01-01T00:00:00"}
    )
    invalid_table = table.model_copy(update={"columns": [date_column, *table.columns[1:]]})
    invalid_manifest = manifest.model_copy(update={"tables": [invalid_table]})
    with pytest.raises(CalendarTableError, match="inverted bounds"):
        plan_calendar_table(invalid_manifest)


def test_calendar_row_limit_blocks_pathological_profile_ranges(tmp_path: Path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="events.csv", content=b"Date\n1900-01-01\n2024-01-01\n")]
    )

    with pytest.raises(CalendarTableError, match="Calendar range would contain"):
        plan_calendar_table(manifest, max_calendar_days=365)
