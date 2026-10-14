"""The local engine's model cache is keyed by the model's fingerprint: formatting edits and
undo of them reuse a model, and any measure, schema, relationship or snapshot change builds
a new one."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from ai_powerbi_analyst.studio import report_engine as engine
from ai_powerbi_analyst.studio.dax_engine import EngineTable, Model
from ai_powerbi_analyst.studio.report_import import (
    ImportedPage,
    ImportedReport,
    model_fingerprint,
)

MEASURE = {"Name": "Sales", "TableName": "M", "Expression": "SUM(F[Amount])"}


class Builds:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.rows = 1
        monkeypatch.setattr(engine, "_build", self.build)

    def build(self, report: ImportedReport, snapshot: Any) -> Model:
        self.calls.append(report.measures[0]["Expression"] if report.measures else "")
        return Model([EngineTable("F", {}, self.rows)], [], [])


@pytest.fixture(autouse=True)
def empty_cache() -> Iterator[None]:
    engine._models.clear()
    yield
    engine._models.clear()


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> Builds:
    return Builds(monkeypatch)


SNAPSHOT: Any = SimpleNamespace(id="snap-1")


def make_report(**more: Any) -> ImportedReport:
    fields: dict[str, Any] = {
        "id": "report-1",
        "workspace_id": "ws",
        "name": "R",
        "source_path": "s.pbix",
        "archived_path": "o.pbix",
        "source_hash": "a" * 64,
        "base_hash": "a" * 64,
        "format": "PBIR",
        "pages": [ImportedPage(id="p", title="One")],
        "measures": [MEASURE],
        "tables": [{"TableName": "F", "ColumnName": "Amount"}],
        "snapshot_id": "snap-1",
        "relationships_version": 2,
    }
    fields.update(more)
    return ImportedReport(**fields)


def with_measure(report: ImportedReport, expression: str, **more: Any) -> ImportedReport:
    return report.model_copy(update={"measures": [{**MEASURE, "Expression": expression}], **more})


def test_a_report_layer_revision_reuses_the_model(builds: Builds) -> None:
    head = make_report()
    first = engine.report_model(head, SNAPSHOT)
    formatted = head.model_copy(
        update={
            "revision_path": "revisions/r1.pbix",
            "revision_hash": "b" * 64,
            "pages": [ImportedPage(id="p", title="Renamed page")],
        }
    )
    assert engine.report_model(formatted, SNAPSHOT) is first
    assert builds.calls == ["SUM(F[Amount])"]


def test_a_measure_change_builds_a_new_model_and_undo_returns_to_the_cached_one(
    builds: Builds,
) -> None:
    before = make_report()
    after = with_measure(
        before, "SUM(F[Amount]) * 2", revision_hash="c" * 64, revision_path="r.pbix"
    )
    original = engine.report_model(before, SNAPSHOT)
    changed = engine.report_model(after, SNAPSHOT)
    assert changed is not original and len(builds.calls) == 2
    # Undo, redo, undo: the two models alternate without another build.
    for report, expected in ((before, original), (after, changed), (before, original)):
        assert engine.report_model(report, SNAPSHOT) is expected
    assert len(builds.calls) == 2


def test_the_old_key_would_have_served_a_stale_model_after_a_measure_edit(builds: Builds) -> None:
    before = make_report()
    after = with_measure(before, "COUNTROWS(F)")
    assert (before.source_hash, before.base_hash) == (after.source_hash, after.base_hash)
    assert model_fingerprint(before) != model_fingerprint(after)
    engine.report_model(before, SNAPSHOT)
    engine.report_model(after, SNAPSHOT)
    assert builds.calls == ["SUM(F[Amount])", "COUNTROWS(F)"]


@pytest.mark.parametrize(
    "change",
    [
        {"tables": [{"TableName": "F", "ColumnName": "Cost"}]},
        {"relationships": [{"FromTableName": "F", "ToTableName": "D"}]},
        {"snapshot_id": "snap-2"},
        {"measures": [{**MEASURE, "FormatString": "0.00"}]},
    ],
)
def test_schema_relationship_snapshot_and_format_changes_build_a_new_model(
    builds: Builds, change: dict[str, Any]
) -> None:
    engine.report_model(make_report(), SNAPSHOT)
    engine.report_model(make_report(**change), SNAPSHOT)
    assert len(builds.calls) == 2


def test_the_snapshot_and_the_report_are_part_of_the_key(builds: Builds) -> None:
    report = make_report()
    engine.report_model(report, SNAPSHOT)
    engine.report_model(report, SimpleNamespace(id="snap-2"))  # type: ignore[arg-type]
    engine.report_model(make_report(id="report-2"), SNAPSHOT)
    assert len(builds.calls) == 3


def test_the_cache_keeps_at_least_four_models_and_drops_the_least_recently_used(
    builds: Builds,
) -> None:
    reports = [with_measure(make_report(), f"SUM(F[Amount]) + {n}") for n in range(5)]
    for report in reports[:4]:
        engine.report_model(report, SNAPSHOT)
    assert len(builds.calls) == 4
    engine.report_model(reports[0], SNAPSHOT)  # touched: no longer the oldest
    assert len(builds.calls) == 4
    engine.report_model(reports[4], SNAPSHOT)  # evicts reports[1]
    assert len(builds.calls) == 5 and len(engine._models) == engine.MAX_CACHED_MODELS >= 4
    engine.report_model(reports[0], SNAPSHOT)
    assert len(builds.calls) == 5
    engine.report_model(reports[1], SNAPSHOT)
    assert len(builds.calls) == 6


def test_big_models_are_not_all_kept(builds: Builds, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "MAX_CACHED_ROWS", 100)
    builds.rows = 60
    first, second = (with_measure(make_report(), f"SUM(F[Amount]) + {n}") for n in range(2))
    engine.report_model(first, SNAPSHOT)
    engine.report_model(second, SNAPSHOT)
    # 120 rows exceed the budget: the older one is dropped, the newest always stays.
    assert len(engine._models) == 1
    engine.report_model(second, SNAPSHOT)
    assert len(builds.calls) == 2
    builds.rows = 500
    engine.report_model(first, SNAPSHOT)
    assert len(engine._models) == 1 and len(builds.calls) == 3
