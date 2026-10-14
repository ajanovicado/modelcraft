from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from ai_powerbi_analyst.data.build import build_demo_data, open_hardened_read_only


def test_build_demo_data_creates_parquet_and_internal_store(tmp_path: Path) -> None:
    artifacts = build_demo_data(tmp_path)

    assert artifacts.sales_parquet.exists()
    assert artifacts.forecast_parquet.exists()
    assert artifacts.scenario_parquet.exists()
    assert artifacts.driver_parquet.exists()
    assert artifacts.forecast_selection_parquet.exists()
    assert artifacts.database.exists()
    assert len(artifacts.data_hash) == 64
    assert len(artifacts.catalog_hash) == 64

    with open_hardened_read_only(artifacts.database) as connection:
        assert connection.execute("SELECT count(*) FROM sales").fetchone()[0] > 10_000
        assert connection.execute("SELECT count(*) FROM forecast").fetchone()[0] == 12
        assert connection.execute("SELECT count(*) FROM scenario_grid").fetchone()[0] == 9
        assert connection.execute("SELECT count(*) FROM forecast_selection").fetchone()[0] == 2
        external_access = connection.execute(
            "SELECT current_setting('enable_external_access')"
        ).fetchone()[0]
        assert external_access is False
        configuration_locked = connection.execute(
            "SELECT current_setting('lock_configuration')"
        ).fetchone()[0]
        assert configuration_locked is True
        with pytest.raises(duckdb.Error):
            connection.execute("SET threads = 1")


def test_runtime_database_connection_is_read_only(tmp_path: Path) -> None:
    artifacts = build_demo_data(tmp_path)

    with open_hardened_read_only(artifacts.database) as connection, pytest.raises(duckdb.Error):
        connection.execute("CREATE TABLE forbidden(value INTEGER)")


def test_manifest_hash_binds_every_runtime_artifact(tmp_path: Path) -> None:
    artifacts = build_demo_data(tmp_path)
    manifest = json.loads(artifacts.manifest.read_text(encoding="utf-8"))

    assert manifest["runtime"]["python"]
    assert manifest["runtime"]["statsforecast"]
    assert "numerical ties" in manifest["forecast_selection_rule"]
    assert all("candidate_set_complete" in row for row in manifest["forecast_approval"])
    for descriptor in manifest["files"].values():
        artifact_path = Path(descriptor["path"])
        if not artifact_path.is_absolute():
            artifact_path = artifacts.root / artifact_path
        assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == descriptor["sha256"]
