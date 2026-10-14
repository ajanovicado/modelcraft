from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import duckdb
import pytest

from ai_powerbi_analyst.analytics import AnalyticsService
from ai_powerbi_analyst.analytics.service import (
    AnalyticsConfigurationError,
    AnalyticsNoEvidenceError,
)
from ai_powerbi_analyst.contracts import AnalysisPlan, ProjectBundle, TimeWindow
from ai_powerbi_analyst.data.build import build_demo_data

CATALOG_PATH = Path(__file__).parents[2] / "fixtures" / "data" / "semantic_catalog.yaml"
SOURCE_KEYS = (
    "sales_parquet",
    "driver_parquet",
    "forecast_parquet",
    "forecast_backtest_parquet",
    "forecast_selection_parquet",
    "scenario_parquet",
)


def _bundle_with_source_inventory(tmp_path: Path) -> tuple[ProjectBundle, dict[str, Path]]:
    artifacts = build_demo_data(tmp_path)
    manifest = json.loads(artifacts.manifest.read_text(encoding="utf-8"))
    files = manifest["files"]
    source_hashes = {key: files[key]["sha256"] for key in SOURCE_KEYS}
    source_paths = {key: tmp_path / files[key]["path"] for key in SOURCE_KEYS}
    bundle = ProjectBundle(
        id="sales-margin-demo",
        name="Sales & Margin",
        pbip_path=tmp_path / "Sales.pbip",
        report_path=tmp_path / "Sales.Report",
        semantic_model_path=tmp_path / "Sales.SemanticModel",
        duckdb_path=artifacts.database,
        catalog_path=CATALOG_PATH,
        data_hash=artifacts.data_hash,
        source_data_hash=source_hashes["sales_parquet"],
        source_hashes=source_hashes,
        catalog_version="1.0.0",
        data_through=date(2025, 12, 31),
    )
    return bundle, source_paths


def test_public_analyst_facade_returns_provenanced_evidence(tmp_path: Path) -> None:
    artifacts = build_demo_data(tmp_path)
    bundle = ProjectBundle(
        id="sales-margin-demo",
        name="Sales & Margin",
        pbip_path=tmp_path / "Sales.pbip",
        report_path=tmp_path / "Sales.Report",
        semantic_model_path=tmp_path / "Sales.SemanticModel",
        duckdb_path=artifacts.database,
        catalog_path=CATALOG_PATH,
        data_hash=artifacts.data_hash,
        catalog_version="1.0.0",
        data_through=date(2025, 12, 31),
    )
    analyst = AnalyticsService.from_project_bundle(bundle)

    answer = analyst.answer(
        AnalysisPlan(
            operation="ranking",
            metric_ids=["net_sales"],
            dimension_ids=["region"],
            ranking_limit=4,
        )
    )

    assert answer.data_hash == artifacts.data_hash
    assert answer.catalog_hash == artifacts.catalog_hash
    assert answer.catalog_version == "1.0.0"
    assert len(answer.rows) == 4
    assert answer.add_to_report_supported is False
    assert answer.report_visual_plan is None


def test_public_analyst_fails_closed_when_catalog_bytes_change(tmp_path: Path) -> None:
    catalog = tmp_path / "semantic_catalog.yaml"
    catalog.write_bytes(CATALOG_PATH.read_bytes())
    artifacts = build_demo_data(tmp_path, catalog_path=catalog)
    bundle = ProjectBundle(
        id="sales-margin-demo",
        name="Sales & Margin",
        pbip_path=tmp_path / "Sales.pbip",
        report_path=tmp_path / "Sales.Report",
        semantic_model_path=tmp_path / "Sales.SemanticModel",
        duckdb_path=artifacts.database,
        catalog_path=catalog,
        data_hash=artifacts.data_hash,
        catalog_version="1.0.0",
        data_through=date(2025, 12, 31),
    )
    catalog.write_text(catalog.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(AnalyticsConfigurationError, match="catalog hash"):
        AnalyticsService.from_project_bundle(bundle)


def test_aggregate_null_row_is_reported_as_no_evidence(tmp_path: Path) -> None:
    artifacts = build_demo_data(tmp_path)
    bundle = ProjectBundle(
        id="sales-margin-demo",
        name="Sales & Margin",
        pbip_path=tmp_path / "Sales.pbip",
        report_path=tmp_path / "Sales.Report",
        semantic_model_path=tmp_path / "Sales.SemanticModel",
        duckdb_path=artifacts.database,
        catalog_path=CATALOG_PATH,
        data_hash=artifacts.data_hash,
        catalog_version="1.0.0",
        data_through=date(2025, 12, 31),
    )
    analyst = AnalyticsService.from_project_bundle(bundle)

    with pytest.raises(AnalyticsNoEvidenceError, match="No prepared data"):
        analyst.answer(
            AnalysisPlan(
                operation="kpi",
                metric_ids=["net_sales"],
                time_window=TimeWindow(
                    start=date(2010, 1, 1),
                    end=date(2010, 12, 31),
                ),
            )
        )


def test_public_analyst_fails_closed_when_valid_database_is_replaced_after_startup(
    tmp_path: Path,
) -> None:
    artifacts = build_demo_data(tmp_path)
    bundle = ProjectBundle(
        id="sales-margin-demo",
        name="Sales & Margin",
        pbip_path=tmp_path / "Sales.pbip",
        report_path=tmp_path / "Sales.Report",
        semantic_model_path=tmp_path / "Sales.SemanticModel",
        duckdb_path=artifacts.database,
        catalog_path=CATALOG_PATH,
        data_hash=artifacts.data_hash,
        catalog_version="1.0.0",
        data_through=date(2025, 12, 31),
    )
    analyst = AnalyticsService.from_project_bundle(bundle)
    replacement = tmp_path / "replacement.duckdb"
    shutil.copyfile(artifacts.database, replacement)
    with duckdb.connect(str(replacement)) as connection:
        connection.execute("CREATE TABLE post_startup_tamper(marker INTEGER)")
    replacement.replace(artifacts.database)

    with pytest.raises(AnalyticsConfigurationError, match="DuckDB changed"):
        analyst.answer(
            AnalysisPlan(
                operation="kpi",
                metric_ids=["net_sales"],
            )
        )


@pytest.mark.parametrize("source_key", SOURCE_KEYS)
def test_public_analyst_fails_closed_when_any_governed_sidecar_drifts(
    tmp_path: Path,
    source_key: str,
) -> None:
    bundle, source_paths = _bundle_with_source_inventory(tmp_path)
    analyst = AnalyticsService.from_project_bundle(bundle)
    with source_paths[source_key].open("ab") as handle:
        handle.write(b"post-startup-tamper")

    with pytest.raises(AnalyticsConfigurationError, match=f"{source_key} changed"):
        analyst.answer(
            AnalysisPlan(
                operation="kpi",
                metric_ids=["net_sales"],
            )
        )


def test_public_analyst_rechecks_all_sidecars_after_query_execution(
    tmp_path: Path,
) -> None:
    bundle, source_paths = _bundle_with_source_inventory(tmp_path)
    analyst = AnalyticsService.from_project_bundle(bundle)
    delegate = analyst.executor

    class MutatingExecutor:
        def execute(self, query: object) -> object:
            rows = delegate.execute(query)  # type: ignore[arg-type]
            with source_paths["forecast_parquet"].open("ab") as handle:
                handle.write(b"changed-during-answer")
            return rows

    analyst.executor = MutatingExecutor()  # type: ignore[assignment]

    with pytest.raises(AnalyticsConfigurationError, match="forecast_parquet changed"):
        analyst.answer(
            AnalysisPlan(
                operation="kpi",
                metric_ids=["net_sales"],
            )
        )
