"""One fresh guarded visual-selection task in an isolated, append-only workspace."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    CreateWorkspaceRequest,
    DraftRequest,
    IngestRequest,
    ReviseDraftRequest,
    UpdateDashboardRequest,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningService, compile_powerbi_plan
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    root = (
        repo
        / "evidence"
        / (
            "report-visual-selection-"
            + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-")
            + uuid.uuid4().hex[:6]
        )
    )
    source = root / "source"
    source.mkdir(parents=True, exist_ok=False)
    data = source / "orders.csv"
    data.write_text(
        "Order ID,Order Date,Region,Sales,Profit\n"
        "N-1,2026-01-01,Aster,137,19\nN-2,2026-01-02,Brook,211,31\n"
        "N-3,2026-01-03,Cedar,89,7\nN-4,2026-02-01,Aster,314,47\n"
        "N-5,2026-02-02,Brook,57,9\nN-6,2026-02-03,Cedar,198,28\n",
        encoding="utf-8",
    )

    def save(name: str, value: object) -> None:
        with (root / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, default=str)

    service = StudioService(repo, root / "state")
    started = time.monotonic()
    result: dict[str, object] = {"native_desktop_render_tested": False}
    try:
        workspace = service.create_workspace(
            CreateWorkspaceRequest(
                name="Isolated visual selection evaluation",
                selected_folder=str(source),
                expected_base_hash=EMPTY_HASH,
                idempotency_key="eval-create",
            )
        )
        snapshot = service.ingest(
            IngestRequest(
                workspace_id=workspace.id,
                item_paths=[str(data)],
                expected_base_hash=workspace.base_hash,
                idempotency_key="eval-ingest",
            )
        )
        # Preparation is deterministic; only the requested visual uses inference.
        service.planner = StudioPlanningService(None, service.aggregates)
        draft = service.create_draft(
            DraftRequest(
                workspace_id=workspace.id,
                snapshot_id=snapshot.id,
                prompt="Build a sales dashboard",
                idempotency_key="eval-draft",
                expected_base_hash=service.workspace(workspace.id).base_hash,
            )
        )
        first = draft.pages[0].visuals[0].model_copy(update={"x": 0, "y": 0, "height": 2})
        page = draft.pages[0].model_copy(update={"title": "Overview", "visuals": [first]})
        draft = service.update_dashboard(
            draft.id,
            UpdateDashboardRequest(
                workspace_id=workspace.id,
                pages=[page],
                expected_base_hash=draft.base_hash,
                idempotency_key="prepare-page",
            ),
        )
        service.planner.model = service.model
        health = service.model.health()
        save("health.json", health.model_dump(mode="json"))
        if not health.model_available:
            raise RuntimeError(health.error or "Resource guard did not permit evaluation")
        request = ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Add a donut chart of Total Sales grouped by Region to Overview.",
            expected_base_hash=draft.base_hash,
            idempotency_key="fresh-visual-selection",
        )
        save("request.json", request.model_dump(mode="json"))
        inference_started = time.monotonic()
        revised = service.revise_draft(draft.id, request)
        result["request_seconds"] = round(time.monotonic() - inference_started, 3)
        save("revised-draft.json", revised.model_dump(mode="json"))
        visual = revised.pages[0].visuals[-1]
        preview = next(p for p in revised.previews if p.visual_id == visual.id)
        values = {r["group_1"]: r["value"] for r in preview.rows}
        result["visual_type"] = visual.visual_type
        result["values"] = values
        assert visual.visual_type == "donutChart"
        assert values == {"Aster": 451, "Brook": 268, "Cedar": 287}
        assert not revised.semantic_model.approved
        approved = service.approve_draft(
            revised.id,
            ApproveDraftRequest(
                workspace_id=workspace.id,
                approved_relationship_ids=[],
                expected_base_hash=revised.base_hash,
                idempotency_key="approve-isolated-plan",
            ),
        )
        plan = compile_powerbi_plan(approved, service.ingestion.load_manifest(snapshot))
        save("native-plan.json", plan.model_dump(mode="json"))
        assert plan.pages[0].visuals[-1].visual_type == "donutChart"
        result["passed"] = True
    except Exception as error:  # noqa: BLE001 - preserve a failed evaluation as evidence
        result.update(passed=False, error=f"{type(error).__name__}: {error}")
    finally:
        service.model.unload()
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        save("result.json", result)
        print(json.dumps({"evidence": str(root), **result}))
    if not result.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
