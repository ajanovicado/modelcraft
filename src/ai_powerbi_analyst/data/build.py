"""Build and open the internal analytical mirror."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from ai_powerbi_analyst.data.catalog import catalog_sha256, load_semantic_catalog
from ai_powerbi_analyst.data.fixture import (
    DATA_THROUGH,
    build_driver_bridge,
    build_forecast,
    build_forecast_backtest,
    build_price_scenarios,
    generate_sales_fixture,
    select_forecast_models,
)


@dataclass(frozen=True, slots=True)
class DataArtifacts:
    """Paths and provenance for one prepared data build."""

    root: Path
    sales_parquet: Path
    driver_parquet: Path
    forecast_parquet: Path
    forecast_backtest_parquet: Path
    forecast_selection_parquet: Path
    scenario_parquet: Path
    database: Path
    catalog_path: Path
    manifest: Path
    data_hash: str
    catalog_hash: str


def _frame_hash(frame: pd.DataFrame) -> str:
    normalized = frame.copy()
    for column in normalized.select_dtypes(include=["datetime64[ns]", "datetimetz"]).columns:
        normalized[column] = normalized[column].dt.strftime("%Y-%m-%dT%H:%M:%S")
    digest = hashlib.sha256()
    digest.update("|".join(normalized.columns).encode())
    row_hashes = pd.util.hash_pandas_object(normalized, index=False).to_numpy(
        dtype="uint64",
        copy=False,
    )
    digest.update(row_hashes.tobytes())
    return digest.hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_database(
    database: Path,
    *,
    sales_parquet: Path,
    driver_parquet: Path,
    forecast_parquet: Path,
    forecast_backtest_parquet: Path,
    forecast_selection_parquet: Path,
    scenario_parquet: Path,
) -> None:
    database.unlink(missing_ok=True)
    connection = duckdb.connect(str(database))
    try:
        sources = {
            "sales": sales_parquet,
            "driver_contributions": driver_parquet,
            "forecast": forecast_parquet,
            "forecast_backtest": forecast_backtest_parquet,
            "forecast_selection": forecast_selection_parquet,
            "scenario_grid": scenario_parquet,
        }
        for table, source in sources.items():
            connection.execute(
                f"CREATE TABLE {table} AS SELECT * FROM read_parquet(?)",  # noqa: S608
                [str(source)],
            )
        connection.execute("CREATE INDEX sales_date_idx ON sales(transaction_date)")
        connection.execute("CREATE INDEX sales_month_idx ON sales(month_start)")
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


def build_demo_data(
    output_dir: str | Path,
    *,
    prefer_statsforecast: bool = False,
    catalog_path: str | Path | None = None,
) -> DataArtifacts:
    """Create all deterministic Parquet artifacts and the internal DuckDB store."""

    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if catalog_path is None:
        local_catalog = root / "semantic_catalog.yaml"
        repository_catalog = (
            Path(__file__).resolve().parents[3] / "fixtures/data/semantic_catalog.yaml"
        )
        catalog = local_catalog if local_catalog.is_file() else repository_catalog
    else:
        catalog = Path(catalog_path).resolve()
    load_semantic_catalog(catalog)

    sales = generate_sales_fixture()
    drivers = build_driver_bridge(sales)
    forecast_backtest = build_forecast_backtest(
        sales,
        include_statsforecast=prefer_statsforecast,
    )
    forecast_selection = select_forecast_models(forecast_backtest)
    forecast = build_forecast(
        sales,
        prefer_statsforecast=prefer_statsforecast,
        selection=forecast_selection,
    )
    scenarios = build_price_scenarios(sales, forecast)

    artifacts = DataArtifacts(
        root=root,
        sales_parquet=root / "sales.parquet",
        driver_parquet=root / "driver_contributions.parquet",
        forecast_parquet=root / "forecast.parquet",
        forecast_backtest_parquet=root / "forecast_backtest.parquet",
        forecast_selection_parquet=root / "forecast_selection.parquet",
        scenario_parquet=root / "scenario_grid.parquet",
        database=root / "analytics.duckdb",
        catalog_path=catalog,
        manifest=root / "manifest.json",
        data_hash=_frame_hash(sales),
        catalog_hash=catalog_sha256(catalog),
    )
    sales.to_parquet(artifacts.sales_parquet, index=False)
    drivers.to_parquet(artifacts.driver_parquet, index=False)
    forecast.to_parquet(artifacts.forecast_parquet, index=False)
    forecast_backtest.to_parquet(artifacts.forecast_backtest_parquet, index=False)
    forecast_selection.to_parquet(artifacts.forecast_selection_parquet, index=False)
    scenarios.to_parquet(artifacts.scenario_parquet, index=False)
    _write_database(
        artifacts.database,
        sales_parquet=artifacts.sales_parquet,
        driver_parquet=artifacts.driver_parquet,
        forecast_parquet=artifacts.forecast_parquet,
        forecast_backtest_parquet=artifacts.forecast_backtest_parquet,
        forecast_selection_parquet=artifacts.forecast_selection_parquet,
        scenario_parquet=artifacts.scenario_parquet,
    )

    artifact_files = {
        "sales_parquet": artifacts.sales_parquet,
        "driver_parquet": artifacts.driver_parquet,
        "forecast_parquet": artifacts.forecast_parquet,
        "forecast_backtest_parquet": artifacts.forecast_backtest_parquet,
        "forecast_selection_parquet": artifacts.forecast_selection_parquet,
        "scenario_parquet": artifacts.scenario_parquet,
        "duckdb": artifacts.database,
        "catalog": artifacts.catalog_path,
    }

    def display_path(path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    manifest_payload = {
        "version": "1.0.0",
        "data_hash": artifacts.data_hash,
        "catalog_hash": artifacts.catalog_hash,
        "data_through": DATA_THROUGH.isoformat(),
        "row_count": len(sales),
        "forecast_version": "1.0.0",
        "forecast_selection_rule": (
            "Lowest three-fold mean WAPE among candidates meeting the threshold and "
            "beating seasonal naive in at least two folds; numerical ties within 1e-9 "
            "prefer AutoETS, AutoARIMA, seasonal drift, then seasonal naive."
        ),
        "forecast_approval": forecast_selection.to_dict(orient="records"),
        "scenario_version": "1.0.0",
        "scenario_baseline": (
            "Approved first-three-month Net Sales and Gross Profit forecasts; volume is "
            "derived at latest-quarter net ASP and unit cost reconciles forecast Gross Profit."
        ),
        "runtime": {
            "python": platform.python_version(),
            "duckdb": importlib.metadata.version("duckdb"),
            "numpy": importlib.metadata.version("numpy"),
            "pandas": importlib.metadata.version("pandas"),
            "statsforecast": importlib.metadata.version("statsforecast"),
            "full_forecast_candidate_set_requested": prefer_statsforecast,
        },
        "files": {
            name: {
                "path": display_path(path),
                "sha256": _file_hash(path),
                "bytes": path.stat().st_size,
            }
            for name, path in artifact_files.items()
        },
    }
    artifacts.manifest.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifacts


def prepare_demo(root: str | Path) -> DataArtifacts:
    """Public one-call preparation entry point used by the CLI and demo reset."""

    return build_demo_data(root, prefer_statsforecast=True)


@contextmanager
def open_hardened_read_only(database: str | Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open the runtime store read-only with external I/O and installs disabled."""

    connection = duckdb.connect(
        str(Path(database).resolve()),
        read_only=True,
        config={
            "enable_external_access": "false",
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
            "threads": "2",
            "memory_limit": "1GB",
            "lock_configuration": "true",
        },
    )
    try:
        yield connection
    finally:
        connection.close()


def main() -> None:
    """Generate fixture artifacts from the command line."""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="fixtures/data")
    parser.add_argument("--statsforecast", action="store_true")
    arguments = parser.parse_args()
    artifacts = build_demo_data(arguments.output, prefer_statsforecast=arguments.statsforecast)
    print(json.dumps({"root": str(artifacts.root), "data_hash": artifacts.data_hash}, indent=2))


if __name__ == "__main__":
    main()
