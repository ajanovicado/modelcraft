from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest

from ai_powerbi_analyst.llm.runtime import LocalModelError, ModelHealth
from ai_powerbi_analyst.studio.contracts import (
    CreateWorkspaceRequest,
    DatasetNarrativePlan,
    DraftRequest,
    IngestRequest,
    LayoutTemplateId,
)
from ai_powerbi_analyst.studio.layout_templates import (
    LayoutField,
    LayoutInputs,
    LayoutMeasure,
    bind_template,
    choose_template,
    eligible_templates,
    layout_catalog,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningService, compile_powerbi_plan
from ai_powerbi_analyst.studio.report_capabilities import (
    minimum_visual_sizes,
    visual_types_by_recipe,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def _inputs(*, date: bool = True, categories: int = 2, measures: int = 3) -> LayoutInputs:
    measure_items = tuple(
        LayoutMeasure(id=f"sum_orders_m{index}", name=f"Measure {index}", table_id="orders")
        for index in range(measures)
    )
    return LayoutInputs(
        table_id="orders",
        table_name="Orders",
        kpi_measures=measure_items,
        chart_measures=measure_items,
        categories=tuple(
            LayoutField(column_id=f"category_{index}", name=f"Category {index}")
            for index in range(categories)
        ),
        date_field=LayoutField(column_id="order_date", name="Order Date") if date else None,
        detail_column_ids=("order_date", "category_0", "m0"),
    )


def test_catalog_ids_match_the_typed_contract() -> None:
    ids = {template.id for template in layout_catalog().templates}
    assert ids == set(get_args(LayoutTemplateId))
    assert layout_catalog().default == "classic_overview"
    assert layout_catalog().provenance["license"] == "MIT"


@pytest.mark.parametrize(
    "template", [t for t in layout_catalog().templates if t.pages], ids=lambda t: t.id
)
def test_template_slots_respect_studio_visual_catalog(template: Any) -> None:
    minimums = minimum_visual_sizes()
    allowed = visual_types_by_recipe()
    page_ids = [page.id for page in template.pages]
    assert len(page_ids) == len(set(page_ids))
    category_slots = 0
    has_trend = False
    for page in template.pages:
        occupied: set[tuple[int, int]] = set()
        for slot in page.slots:
            assert slot.visual_type in allowed[slot.recipe], slot.id
            minimum_width, minimum_height = minimums[slot.recipe]
            assert slot.width >= minimum_width and slot.height >= minimum_height, slot.id
            assert slot.x + slot.width <= 12 and slot.y + slot.height <= 12, slot.id
            cells = {
                (x, y)
                for x in range(slot.x, slot.x + slot.width)
                for y in range(slot.y, slot.y + slot.height)
            }
            assert not cells & occupied, f"{page.id}.{slot.id} overlaps"
            occupied |= cells
            category_slots += slot.recipe == "category"
            has_trend = has_trend or slot.recipe == "trend"
    # Requirements must guarantee every category slot binds a distinct pair.
    assert template.requires.category_pairs >= category_slots
    assert template.requires.date == has_trend


def test_eligibility_excludes_templates_the_data_cannot_fill() -> None:
    no_date = {t.id for t in eligible_templates(_inputs(date=False))}
    assert "kpi_rail_trend" not in no_date
    assert "executive_scorecard" not in no_date
    assert {"classic_overview", "ranked_performance", "comparison_grid"} <= no_date

    one_pair = {t.id for t in eligible_templates(_inputs(categories=1, measures=1))}
    assert one_pair == {"classic_overview"}


def test_choice_prefers_prompt_rule_then_eligible_model_pick_then_default() -> None:
    inputs = _inputs()
    # Named structure beats the model's pick.
    assert choose_template(
        "Create a sales and profit dashboard with an overview, trends and detail page.",
        inputs,
        "executive_scorecard",
    ) == ("classic_overview", "prompt_rule")
    assert choose_template("anything", inputs, "driver_analysis") == (
        "driver_analysis",
        "local_model",
    )
    assert choose_template("Show the top 10 and bottom 10 agents", inputs, None) == (
        "ranked_performance",
        "prompt_rule",
    )
    assert choose_template("Build a sales dashboard", inputs, None) == (
        "classic_overview",
        "default",
    )
    # An ineligible model pick is ignored rather than trusted.
    no_date = _inputs(date=False)
    assert choose_template("Monthly trend", no_date, "kpi_rail_trend") == (
        "classic_overview",
        "default",
    )


@pytest.mark.parametrize("template_id", [t.id for t in layout_catalog().templates if t.pages])
def test_every_template_binds_all_slots_to_distinct_reviewed_fields(
    template_id: LayoutTemplateId,
) -> None:
    template = next(t for t in layout_catalog().templates if t.id == template_id)
    pages = bind_template(template_id, _inputs())
    assert [page.id for page in pages] == [page.id for page in template.pages]
    for page, source in zip(pages, template.pages, strict=True):
        assert len(page.visuals) == len(source.slots)
    pairs = [
        (visual.measure_id, visual.category_column_id)
        for page in pages
        for visual in page.visuals
        if visual.recipe == "category"
    ]
    assert len(pairs) == len(set(pairs))


def test_binding_drops_only_missing_kpis() -> None:
    pages = bind_template("comparison_grid", _inputs(measures=2))
    recipes = [visual.recipe for visual in pages[0].visuals]
    assert recipes.count("kpi") == 2
    assert recipes.count("category") == 4


class _TemplateModel:
    def __init__(self, choice: LayoutTemplateId | None) -> None:
        self.choice = choice
        self.payloads: list[dict[str, Any]] = []

    def health(self) -> ModelHealth:
        return ModelHealth(
            service_available=True,
            model_available=True,
            endpoint="http://127.0.0.1:11434",
            available_memory_gb=8,
        )

    def generate(
        self, system_prompt: str, user_prompt: str, response_model: type[Any], **_: Any
    ) -> Any:
        assert response_model is DatasetNarrativePlan
        self.payloads.append(json.loads(user_prompt))
        return DatasetNarrativePlan(
            summary="Reviewed local orders.", layout_template_id=self.choice
        )


def _draft(tmp_path: Path, model: _TemplateModel, prompt: str) -> Any:
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    service.planner = StudioPlanningService(model, service.aggregates)  # type: ignore[arg-type]
    source_root = tmp_path / "selected"
    source_root.mkdir()
    source = source_root / "orders.csv"
    source.write_text(
        "Order Date,Region,Segment,Sales,Profit\n"
        "2025-01-01,East,Consumer,100.50,20.25\n"
        "2025-02-01,West,Corporate,250.00,40.00\n"
        "2025-03-01,East,Corporate,75.00,5.00\n",
        encoding="utf-8",
    )
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Layout templates",
            selected_folder=str(source_root),
            idempotency_key="create-layout-template-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="ingest-layout-template-source",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    return service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt=prompt,
            idempotency_key="analyze-layout-template-source",
            expected_base_hash=workspace.base_hash,
        )
    )


@pytest.mark.parametrize("template_id", [t.id for t in layout_catalog().templates if t.pages])
def test_model_selected_template_produces_a_validated_previewed_draft(
    tmp_path: Path, template_id: LayoutTemplateId
) -> None:
    model = _TemplateModel(template_id)
    draft = _draft(tmp_path, model, "Build a sales dashboard")

    offered = model.payloads[0]["layout_templates"]
    assert template_id in offered and "classic_overview" in offered
    # The CPU model's prompt budget is tight, so design options stay bare IDs.
    design_options = [*offered, *model.payloads[0]["report_themes"]]
    assert all(isinstance(item, str) for item in design_options)
    assert len(json.dumps(design_options, separators=(",", ":"))) < 300
    assert draft.planning_provenance.layout_template == template_id
    assert draft.planning_provenance.layout_template_source == "local_model"
    assert draft.planning_provenance.pages == "deterministic_compiler"
    template = next(t for t in layout_catalog().templates if t.id == template_id)
    assert [page.id for page in draft.pages] == [page.id for page in template.pages]
    visual_ids = {visual.id for page in draft.pages for visual in page.visuals}
    assert {preview.visual_id for preview in draft.previews} == visual_ids


def test_without_a_model_choice_the_classic_layout_is_unchanged(tmp_path: Path) -> None:
    draft = _draft(tmp_path, _TemplateModel(None), "Build a sales dashboard")

    assert draft.planning_provenance.layout_template == "classic_overview"
    assert draft.planning_provenance.layout_template_source == "default"
    assert [page.id for page in draft.pages] == ["overview", "trends_drivers", "detail"]


class _ThemeModel(_TemplateModel):
    def generate(
        self, system_prompt: str, user_prompt: str, response_model: type[Any], **_: Any
    ) -> Any:
        self.payloads.append(json.loads(user_prompt))
        return DatasetNarrativePlan(summary="Reviewed local orders.", report_theme_id="calm_teal")


def test_model_theme_choice_flows_from_draft_into_the_build_plan(tmp_path: Path) -> None:
    model = _ThemeModel(None)
    draft = _draft(tmp_path, model, "Build a sales dashboard")

    offered = set(model.payloads[0]["report_themes"])
    assert offered == {
        "default",
        "executive_light",
        "calm_teal",
        "finance_neutral",
        "accessible_contrast",
    }
    assert draft.planning_provenance.report_theme == "calm_teal"
    assert draft.planning_provenance.report_theme_source == "local_model"

    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    manifest = service.ingestion.load_manifest(service.snapshot(draft.snapshot_id))
    approved = draft.model_copy(
        update={
            "status": "approved",
            "semantic_model": draft.semantic_model.model_copy(update={"approved": True}),
        }
    )
    assert compile_powerbi_plan(approved, manifest).report_theme == "calm_teal"


class _TightContextModel(_TemplateModel):
    """Mimics a backend rejecting an oversize prompt before generation."""

    def generate(
        self, system_prompt: str, user_prompt: str, response_model: type[Any], **_: Any
    ) -> Any:
        payload = json.loads(user_prompt)
        self.payloads.append(payload)
        if "layout_templates" in payload:
            raise LocalModelError("Conservative context bound exceeded; no silent truncation")
        # The retry must be the original narrative-only prompt, byte for byte.
        assert list(payload) == ["request", "profile"]
        assert "layout_template_id" not in system_prompt
        return DatasetNarrativePlan(
            summary="Reviewed local orders.",
            layout_template_id="comparison_grid",
            report_theme_id="calm_teal",
        )


def test_design_options_never_cost_the_narrative_its_context_window(tmp_path: Path) -> None:
    model = _TightContextModel(None)
    draft = _draft(tmp_path, model, "Build a sales dashboard")

    assert len(model.payloads) == 2
    assert "report_themes" not in model.payloads[1]
    assert draft.planning_provenance.narrative == "local_model"
    # Choices returned without offered options are discarded, not trusted.
    assert draft.planning_provenance.layout_template == "classic_overview"
    assert draft.planning_provenance.report_theme == "default"


def test_narrative_prompt_is_lean_and_skips_worked_examples(tmp_path: Path) -> None:
    model = _TemplateModel(None)
    _draft(tmp_path, model, "Build an executive overview")

    # Knowledge references and DAX/M worked examples cost CPU prefill without
    # improving a profile narrative; they stay with chat, DAX and revisions.
    assert "reference_material" not in model.payloads[0]
    assert DatasetNarrativePlan.worked_examples is False
    assert len(json.dumps(model.payloads[0], separators=(",", ":"))) < 2000


def test_generation_schema_forces_the_model_to_decide_design_first() -> None:
    schema = DatasetNarrativePlan.model_json_schema()
    # Constrained decoding skips optional keys; requiring them (null allowed) makes
    # the model state a choice, and property order makes it decide before prose.
    assert {"layout_template_id", "report_theme_id"} <= set(schema["required"])
    assert list(schema["properties"])[1:3] == ["layout_template_id", "report_theme_id"]
    assert DatasetNarrativePlan(summary="Python callers keep defaults.").layout_template_id is None
