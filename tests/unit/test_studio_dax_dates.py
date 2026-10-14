from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import (
    CreateWorkspaceRequest,
    DaxEvaluateRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


@pytest.fixture
def dated(tmp_path: Path):
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    source = tmp_path / "dated_orders.csv"
    source.write_text(
        "Order Date,Sales,Discount,Qty,Customer,Region\n"
        "2024-01-01T00:00:00,100,10,2,A,East\n"
        "2024-01-31T23:59:59,200,20,0,A,East\n"
        "2024-02-01T00:00:00,60,5,3,B,West\n"
        "2024-04-02T14:30:00,90,8,0,C,East\n"
        "2025-01-05T08:00:00,400,40,4,D,East\n"
        ",50,2,1,E,East\n",
        encoding="utf-8",
    )
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Dates QA",
            selected_folder=str(tmp_path),
            idempotency_key="dates-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="dates-import",
            expected_base_hash=workspace.base_hash,
        )
    )
    table = snapshot.tables[0].table
    columns = {column.name: column.id for column in table.columns}
    assert table.columns[0].data_type == "datetime"
    return service, workspace.id, table.name, columns


def evaluate(dated, expression, grain="month", **kwargs):
    service, wid, table, columns = dated
    return service.evaluate_dax(
        DaxEvaluateRequest(
            workspace_id=wid,
            expression=expression.replace("TABLE", table),
            preview={"column_id": columns["Order Date"], "grain": grain},
            **kwargs,
        )
    )


def values(result):
    meta = result.date_preview
    assert meta
    return {str(row[meta.column])[:10]: row[meta.value_column] for row in result.rows}


def test_date_preview_leaves_scalar_unchanged_and_keeps_calendar_years(dated):
    service, wid, table, columns = dated
    expr = f"SUM('{table}'[Sales]) - SUM('{table}'[Discount])"
    before = service.workspace(wid).base_hash
    overall = service.evaluate_dax(DaxEvaluateRequest(workspace_id=wid, expression=expr))
    assert overall.rows == [{"Sales - Discount": 815}]
    assert overall.date_fields[0].column_id == columns["Order Date"]
    months = evaluate(dated, expr)
    assert values(months) == {
        "2024-01-01": 270,
        "2024-02-01": 55,
        "2024-04-01": 82,
        "2025-01-01": 360,
        "None": 48,
    }
    assert values(evaluate(dated, expr, "quarter")) == {
        "2024-01-01": 325,
        "2024-04-01": 82,
        "2025-01-01": 360,
        "None": 48,
    }
    assert values(evaluate(dated, expr, "year")) == {
        "2024-01-01": 407,
        "2025-01-01": 360,
        "None": 48,
    }
    assert values(evaluate(dated, expr, "day"))["2024-01-31"] == 180
    assert (
        service.evaluate_dax(DaxEvaluateRequest(workspace_id=wid, expression=expr)).rows
        == overall.rows
    )
    assert service.workspace(wid).base_hash == before


def test_dates_recalculate_nonadditive_measures_and_preserve_blanks(dated):
    average = values(evaluate(dated, "AVERAGE('TABLE'[Sales])", "quarter"))
    assert average["2024-01-01"] == 120  # (100 + 200 + 60) / 3, not mean of monthly means.
    ratio = values(evaluate(dated, "DIVIDE(SUM('TABLE'[Sales]), SUM('TABLE'[Qty]))"))
    assert ratio["2024-01-01"] == 150
    assert ratio["2024-04-01"] is None
    assert values(evaluate(dated, "DISTINCTCOUNT('TABLE'[Customer])"))["2024-01-01"] == 1


def test_period_inspection_retains_filters_and_half_open_timestamp_bounds(dated):
    expression = "CALCULATE(SUM('TABLE'[Sales]), 'TABLE'[Region] = \"East\")"
    assert "2024-02-01" not in values(evaluate(dated, expression))
    details = evaluate(dated, expression, detail_period="2024-01-01")
    assert [row["Sales"] for row in details.rows] == [100, 200]
    assert all(row["Region"] == "East" for row in details.rows)
    assert evaluate(dated, expression, detail_period="2024-02-01").rows == []
    assert [
        row["Sales"] for row in evaluate(dated, expression, "year", detail_period="2025-01-01").rows
    ] == [400]
    with pytest.raises(ValueError, match="start of a displayed"):
        evaluate(dated, expression, detail_period="2024-01-15")


def test_preview_rejects_invalid_context_and_keeps_limits(dated):
    service, wid, table, columns = dated
    token = "test-dax-dates-token-long-enough"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "workspace_id": wid,
        "expression": f"SUM('{table}'[Sales])",
        "preview": {"column_id": columns["Order Date"], "grain": "month"},
    }
    with TestClient(create_app(service, token)) as client:
        path = f"/v1/workspaces/{wid}/dax/evaluate"
        assert client.post(path, json=payload).status_code == 401
        assert client.post(path, headers=headers, json=payload).status_code == 200
        for update in [
            {"top_n": 101},
            {"preview": {"column_id": columns["Sales"], "grain": "month"}},
            {"preview": {"column_id": "unrelated_date", "grain": "month"}},
            {"preview": {"column_id": columns["Order Date"], "grain": "month'); DROP TABLE x--"}},
            {"expression": f"EVALUATE '{table}'"},
            {
                "expression": (
                    f"CALCULATE(SUM('{table}'[Sales]), '{table}'[Order Date] >= \"2024-01-01\")"
                )
            },
            {"preview": None, "detail_period": "2024-01-01"},
            {
                "detail_period": "9999-12-31",
                "preview": {"column_id": columns["Order Date"], "grain": "day"},
            },
        ]:
            response = client.post(path, headers=headers, json={**payload, **update})
            assert response.status_code == 422, response.text
    limited = evaluate(dated, "COUNTROWS('TABLE')", "day", top_n=2)
    assert limited.row_count == 2 and limited.truncated
    assert list(values(limited)) == ["2024-01-01", "2024-01-31"]
    rows = evaluate(dated, "COUNTROWS('TABLE')", detail_period="2024-01-01", top_n=1)
    assert rows.row_count == 1 and rows.truncated
