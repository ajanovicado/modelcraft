"""Application-facing facade for validated local analysis."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path

from ai_powerbi_analyst.analytics.compiler import compile_analysis
from ai_powerbi_analyst.analytics.evidence import render_evidence_answer
from ai_powerbi_analyst.analytics.executor import AnalyticsExecutor
from ai_powerbi_analyst.contracts import (
    AnalysisOperation,
    AnalysisPlan,
    EvidenceAnswer,
    ProjectBundle,
    SemanticCatalog,
)
from ai_powerbi_analyst.data.catalog import catalog_sha256, load_semantic_catalog

_PREPARED_SOURCE_KEYS = frozenset(
    {
        "sales_parquet",
        "driver_parquet",
        "forecast_parquet",
        "forecast_backtest_parquet",
        "forecast_selection_parquet",
        "scenario_parquet",
    }
)


class AnalyticsConfigurationError(RuntimeError):
    """Project provenance is inconsistent, so analysis must fail closed."""


class AnalyticsNoEvidenceError(RuntimeError):
    """A valid governed query has no rows in the prepared data window."""


class AnalyticsService:
    """Compile, execute, and render a governed analysis request."""

    def __init__(
        self,
        database_path: str | Path,
        catalog: SemanticCatalog,
        data_hash: str,
        catalog_hash: str,
        data_through: date,
        *,
        timeout_seconds: float = 10.0,
        integrity_guard: Callable[[], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self.data_hash = data_hash
        self.catalog_hash = catalog_hash
        self.data_through = data_through
        self.executor = AnalyticsExecutor(database_path, timeout_seconds=timeout_seconds)
        self.integrity_guard = integrity_guard

    @classmethod
    def from_project_bundle(
        cls,
        bundle: ProjectBundle,
        *,
        timeout_seconds: float = 10.0,
    ) -> AnalyticsService:
        catalog = load_semantic_catalog(bundle.catalog_path)
        if catalog.version != bundle.catalog_version:
            raise AnalyticsConfigurationError("catalog version does not match the project bundle")
        manifest_path = bundle.duckdb_path.with_name("manifest.json")
        if not manifest_path.is_file():
            raise AnalyticsConfigurationError("analytical manifest is missing")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("data_hash") != bundle.data_hash:
            raise AnalyticsConfigurationError(
                "analytical data hash does not match the project bundle"
            )
        actual_catalog_hash = catalog_sha256(bundle.catalog_path)
        if payload.get("catalog_hash") != actual_catalog_hash:
            raise AnalyticsConfigurationError("catalog hash does not match the analytical manifest")
        bundle_catalog_hash = getattr(bundle, "catalog_hash", None)
        if bundle_catalog_hash is not None and bundle_catalog_hash != actual_catalog_hash:
            raise AnalyticsConfigurationError("catalog hash does not match the project bundle")

        file_inventory = payload.get("files")
        if not isinstance(file_inventory, dict):
            raise AnalyticsConfigurationError("analytical manifest file inventory is invalid")

        database_hash = _file_sha256(bundle.duckdb_path)
        database_record = file_inventory.get("duckdb")
        if not isinstance(database_record, dict):
            raise AnalyticsConfigurationError("DuckDB manifest entry is invalid")
        recorded_database = database_record.get("sha256")
        if recorded_database != database_hash:
            raise AnalyticsConfigurationError("DuckDB hash does not match the analytical manifest")
        bundle_database_hash = getattr(bundle, "duckdb_hash", None)
        if bundle_database_hash is not None and bundle_database_hash != database_hash:
            raise AnalyticsConfigurationError("DuckDB hash does not match the project bundle")
        governed_sources = _load_governed_source_inventory(
            bundle,
            file_inventory=file_inventory,
            source_root=bundle.duckdb_path.parent,
        )
        manifest_hash = _file_sha256(manifest_path)

        def assert_runtime_integrity() -> None:
            governed_files = (
                ("analytical manifest", manifest_path, manifest_hash),
                ("DuckDB", bundle.duckdb_path, database_hash),
                ("catalog", bundle.catalog_path, actual_catalog_hash),
                *governed_sources,
            )
            for label, path, expected_hash in governed_files:
                if path.is_symlink() or not path.is_file():
                    raise AnalyticsConfigurationError(
                        f"{label} disappeared after project initialization"
                    )
                if _file_sha256(path) != expected_hash:
                    raise AnalyticsConfigurationError(
                        f"{label} changed after project initialization"
                    )

        return cls(
            bundle.duckdb_path,
            catalog,
            bundle.data_hash,
            actual_catalog_hash,
            bundle.data_through,
            timeout_seconds=timeout_seconds,
            integrity_guard=assert_runtime_integrity,
        )

    def answer(self, plan: AnalysisPlan) -> EvidenceAnswer:
        self._assert_runtime_integrity()
        try:
            query = compile_analysis(plan, self.catalog, data_through=self.data_through)
            rows = self.executor.execute(query)
            if not _contains_complete_evidence(plan, rows):
                raise AnalyticsNoEvidenceError(
                    "No prepared data exists for that period and filter combination. "
                    f"This workspace contains data through {self.data_through.isoformat()}."
                )
            return render_evidence_answer(
                plan,
                query,
                rows,
                self.catalog,
                self.data_hash,
                self.catalog_hash,
            )
        finally:
            # A governed file can be atomically replaced while the worker or
            # renderer is running. Never return numbers carrying frozen
            # provenance unless every bundled source identity still matches.
            self._assert_runtime_integrity()

    def _assert_runtime_integrity(self) -> None:
        if self.integrity_guard is not None:
            self.integrity_guard()


def _file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _load_governed_source_inventory(
    bundle: ProjectBundle,
    *,
    file_inventory: Mapping[str, object],
    source_root: Path,
) -> tuple[tuple[str, Path, str], ...]:
    """Resolve the bundle-authoritative prepared source inventory.

    Older local/mock bundles predate ``source_hashes`` and retain a narrow sales
    compatibility path. A release bundle that supplies the field must bind the
    complete six-sidecar set; the analytical manifest is then only the path map,
    never the hash authority.
    """

    if bundle.source_hashes:
        if set(bundle.source_hashes) != _PREPARED_SOURCE_KEYS:
            raise AnalyticsConfigurationError(
                "project bundle must bind all six prepared Parquet sources"
            )
        expected_hashes = dict(bundle.source_hashes)
        if (
            bundle.source_data_hash is not None
            and bundle.source_data_hash != expected_hashes["sales_parquet"]
        ):
            raise AnalyticsConfigurationError(
                "primary source-data hash disagrees with the project bundle inventory"
            )
    else:
        sales_record = file_inventory.get("sales_parquet")
        if not isinstance(sales_record, dict):
            raise AnalyticsConfigurationError("sales_parquet manifest entry is invalid")
        manifest_sales_hash = sales_record.get("sha256")
        if not isinstance(manifest_sales_hash, str):
            raise AnalyticsConfigurationError("sales_parquet manifest entry is invalid")
        expected_hashes = {
            "sales_parquet": bundle.source_data_hash or manifest_sales_hash,
        }

    resolved_root = source_root.resolve(strict=True)
    governed: list[tuple[str, Path, str]] = []
    for key in sorted(expected_hashes):
        record = file_inventory.get(key)
        if not isinstance(record, dict):
            raise AnalyticsConfigurationError(f"{key} manifest entry is invalid")
        relative = record.get("path")
        recorded_hash = record.get("sha256")
        expected_hash = expected_hashes[key]
        if not isinstance(relative, str) or not isinstance(recorded_hash, str):
            raise AnalyticsConfigurationError(f"{key} manifest entry is invalid")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise AnalyticsConfigurationError(f"{key} path escapes the analytical root")
        if recorded_hash != expected_hash:
            raise AnalyticsConfigurationError(
                f"{key} hash disagrees with the project bundle inventory"
            )
        source_path = resolved_root / relative_path
        try:
            resolved_source = source_path.resolve(strict=True)
        except OSError as error:
            raise AnalyticsConfigurationError(f"prepared source is missing: {key}") from error
        if (
            not resolved_source.is_relative_to(resolved_root)
            or source_path.is_symlink()
            or not source_path.is_file()
        ):
            raise AnalyticsConfigurationError(f"{key} path is not a governed local file")
        if _file_sha256(source_path) != expected_hash:
            raise AnalyticsConfigurationError(
                f"{key} hash does not match the project bundle inventory"
            )
        governed.append((key, source_path, expected_hash))
    return tuple(governed)


def _contains_complete_evidence(
    plan: AnalysisPlan,
    rows: Sequence[Mapping[str, object | None]],
) -> bool:
    """Reject both empty result sets and aggregate rows whose facts are all NULL."""

    if not rows:
        return False

    operation = plan.operation
    required_by_operation: dict[AnalysisOperation, tuple[str, ...]] = {
        AnalysisOperation.KPI: ("value",),
        AnalysisOperation.COMPARISON: ("current_value", "comparison_value"),
        AnalysisOperation.TREND: ("period", "value"),
        AnalysisOperation.RANKING: ("rank_key", "value"),
        AnalysisOperation.CONTRIBUTION: ("contributor", "contribution"),
        AnalysisOperation.DRIVER_BRIDGE: ("driver", "contribution_value"),
        AnalysisOperation.FORECAST: (
            "period",
            "metric_id",
            "point_forecast",
            "lower_80",
            "upper_80",
        ),
        AnalysisOperation.SCENARIO: (
            "price_change_pct",
            "baseline_units",
            "scenario_units",
            "net_sales",
            "gross_profit",
        ),
    }
    required = required_by_operation[operation]
    if any(any(row.get(key) is None for key in required) for row in rows):
        return False

    if operation is AnalysisOperation.DRIVER_BRIDGE:
        summary = (
            "current_net_sales",
            "comparison_net_sales",
            "current_gross_margin_pct",
            "comparison_gross_margin_pct",
        )
        if any(rows[0].get(key) is None for key in summary):
            return False
    elif operation is AnalysisOperation.FORECAST:
        counts = {
            metric_id: sum(row.get("metric_id") == metric_id for row in rows)
            for metric_id in plan.metric_ids
        }
        if any(count < 3 for count in counts.values()):
            return False
    return True
