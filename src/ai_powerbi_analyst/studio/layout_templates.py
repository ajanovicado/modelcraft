"""Reviewed dashboard page structures and themes that Qwen may select but never place.

Templates live in ``layout_templates.json``. Each slot names a recipe, a
catalog visual type, a ranked measure/category index and a grid rectangle.
Binding is deterministic: the planner supplies ranked reviewed fields and this
module turns a template into ordinary ``DashboardPageProposal`` objects, which
then pass through the same validation and layout normalization as any page.
Themes come from the hash-pinned registry in ``powerbi/report_themes``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from itertools import product
from typing import Literal, cast, get_args

from pydantic import Field

from ai_powerbi_analyst.contracts import ClosedModel, ReportThemeId
from ai_powerbi_analyst.powerbi.theme import studio_theme_registry

from .contracts import (
    DashboardPageProposal,
    DashboardVisualProposal,
    LayoutTemplateId,
)

LayoutSource = Literal["local_model", "prompt_rule", "default"]

CLASSIC_TEMPLATE: LayoutTemplateId = "classic_overview"


class LayoutSlot(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    recipe: Literal["kpi", "trend", "category", "detail"]
    visual_type: str
    measure: int | None = Field(default=None, ge=0, le=7)
    category: int | None = Field(default=None, ge=0, le=7)
    x: int = Field(ge=0, le=11)
    y: int = Field(ge=0, le=11)
    width: int = Field(ge=1, le=12)
    height: int = Field(ge=1, le=12)


class LayoutPage(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    title: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=300)
    slots: list[LayoutSlot] = Field(min_length=1, max_length=12)


class LayoutRequirements(ClosedModel):
    date: bool = False
    category_pairs: int = Field(default=0, ge=0, le=12)


class LayoutTemplate(ClosedModel):
    id: LayoutTemplateId
    label: str = Field(min_length=1, max_length=60)
    use_when: str = Field(min_length=1, max_length=240)
    prompt_terms: list[str] = Field(default_factory=list, max_length=20)
    derived_from: str = Field(min_length=1, max_length=200)
    requires: LayoutRequirements
    pages: list[LayoutPage] = Field(default_factory=list, max_length=3)


class LayoutCatalog(ClosedModel):
    version: Literal["1.0"]
    qualification: str
    provenance: dict[str, str]
    default: LayoutTemplateId
    templates: list[LayoutTemplate] = Field(min_length=1)


@dataclass(frozen=True)
class LayoutField:
    column_id: str
    name: str
    # A related dimension's table (Dates, Customers); None is the primary table.
    table_id: str | None = None


@dataclass(frozen=True)
class LayoutMeasure:
    id: str
    name: str
    table_id: str


@dataclass(frozen=True)
class LayoutInputs:
    """Ranked reviewed fields from one primary table; produced by the planner."""

    table_id: str
    table_name: str
    kpi_measures: tuple[LayoutMeasure, ...]
    chart_measures: tuple[LayoutMeasure, ...]
    categories: tuple[LayoutField, ...]
    date_field: LayoutField | None
    detail_column_ids: tuple[str, ...]


@lru_cache(maxsize=1)
def layout_catalog() -> LayoutCatalog:
    text = (
        files("ai_powerbi_analyst.studio")
        .joinpath("layout_templates.json")
        .read_text(encoding="utf-8")
    )
    catalog = LayoutCatalog.model_validate(json.loads(text))
    ids = [template.id for template in catalog.templates]
    if sorted(ids) != sorted(get_args(LayoutTemplateId)):
        raise ValueError("layout_templates.json must define exactly the LayoutTemplateId values")
    return catalog


def _template(template_id: LayoutTemplateId) -> LayoutTemplate:
    return next(item for item in layout_catalog().templates if item.id == template_id)


def eligible_templates(inputs: LayoutInputs) -> list[LayoutTemplate]:
    """Templates whose every slot can bind to distinct reviewed fields."""
    pairs = len(inputs.chart_measures) * len(inputs.categories)
    eligible = []
    for template in layout_catalog().templates:
        if template.id == CLASSIC_TEMPLATE:
            eligible.append(template)
            continue
        if not inputs.kpi_measures or not inputs.detail_column_ids:
            continue
        if template.requires.date and inputs.date_field is None:
            continue
        if pairs < template.requires.category_pairs:
            continue
        eligible.append(template)
    return eligible


def model_layout_options(inputs: LayoutInputs) -> list[str]:
    """Self-describing template IDs only: the narrative prompt budgets its bytes tightly."""
    return [template.id for template in eligible_templates(inputs)]


def choose_template(
    prompt: str,
    inputs: LayoutInputs,
    model_choice: LayoutTemplateId | None,
) -> tuple[LayoutTemplateId, LayoutSource]:
    """Explicit request terms outrank the model, as for themes: someone who asks for
    "an overview, trends and detail page" has already chosen the structure."""
    eligible = {template.id for template in eligible_templates(inputs)}
    for template in layout_catalog().templates:
        if template.id in eligible and _mentions(prompt, template.prompt_terms):
            return template.id, "prompt_rule"
    if model_choice is not None and model_choice in eligible:
        return model_choice, "local_model"
    return layout_catalog().default, "default"


def model_theme_options() -> list[str]:
    """Self-describing theme IDs only, for the same prompt budget reason."""
    return list(studio_theme_registry())


def choose_theme(
    prompt: str, model_choice: ReportThemeId | None
) -> tuple[ReportThemeId, LayoutSource]:
    """An explicit prompt term outranks the model; otherwise keep Power BI's default."""
    for key, entry in studio_theme_registry().items():
        terms = entry.get("prompt_terms")
        if isinstance(terms, list) and _mentions(prompt, [str(term) for term in terms]):
            return cast(ReportThemeId, key), "prompt_rule"
    if model_choice is not None:
        return model_choice, "local_model"
    return "default", "default"


def _mentions(prompt: str, terms: list[str]) -> bool:
    normalized = " ".join(re.findall(r"[a-z0-9]+", prompt.casefold()))
    return any(re.search(rf"\b{re.escape(term)}\b", normalized) for term in terms)


def bind_template(
    template_id: LayoutTemplateId, inputs: LayoutInputs
) -> list[DashboardPageProposal]:
    """Bind a non-classic template; the caller validates and normalizes the result."""
    template = _template(template_id)
    if template_id == CLASSIC_TEMPLATE or template not in eligible_templates(inputs):
        raise ValueError(f"Layout template {template_id} cannot bind to this selection")
    all_pairs = [
        (measure_index, category_index)
        for category_index, measure_index in product(
            range(len(inputs.categories)), range(len(inputs.chart_measures))
        )
    ]
    used_pairs: set[tuple[int, int]] = set()
    pages: list[DashboardPageProposal] = []
    for page in template.pages:
        visuals: list[DashboardVisualProposal] = []
        for slot in page.slots:
            visual = _bind_slot(page.id, slot, inputs, all_pairs, used_pairs)
            if visual is not None:
                visuals.append(visual)
        if visuals:
            pages.append(
                DashboardPageProposal(
                    id=page.id, title=page.title, purpose=page.purpose, visuals=visuals
                )
            )
    return pages


def _bind_slot(
    page_id: str,
    slot: LayoutSlot,
    inputs: LayoutInputs,
    all_pairs: list[tuple[int, int]],
    used_pairs: set[tuple[int, int]],
) -> DashboardVisualProposal | None:
    visual: dict[str, object] = {
        "id": f"{page_id}_{slot.id}",
        "recipe": slot.recipe,
        "visual_type": slot.visual_type,
        "x": slot.x,
        "y": slot.y,
        "width": slot.width,
        "height": slot.height,
    }
    if slot.recipe == "kpi":
        index = slot.measure or 0
        if index >= len(inputs.kpi_measures):
            # A missing KPI leaves a gap rather than repeating a card.
            return None
        measure = inputs.kpi_measures[index]
        visual |= {"title": measure.name, "table_id": measure.table_id, "measure_id": measure.id}
    elif slot.recipe == "trend":
        if inputs.date_field is None:
            return None
        measure = inputs.chart_measures[min(slot.measure or 0, len(inputs.chart_measures) - 1)]
        visual |= {
            "title": f"{measure.name} over time",
            "table_id": inputs.date_field.table_id or inputs.table_id,
            "measure_id": measure.id,
            "category_column_id": inputs.date_field.column_id,
        }
    elif slot.recipe == "category":
        preferred = (slot.measure or 0, slot.category or 0)
        pair = (
            preferred
            if preferred in all_pairs and preferred not in used_pairs
            else next((item for item in all_pairs if item not in used_pairs), None)
        )
        if pair is None:
            return None
        used_pairs.add(pair)
        measure = inputs.chart_measures[pair[0]]
        category = inputs.categories[pair[1]]
        visual |= {
            "title": f"{measure.name} by {category.name}",
            "table_id": category.table_id or inputs.table_id,
            "measure_id": measure.id,
            "category_column_id": category.column_id,
        }
    else:
        visual |= {
            "title": f"{inputs.table_name} detail",
            "table_id": inputs.table_id,
            "detail_column_ids": list(inputs.detail_column_ids),
        }
    # Titles derive from reviewed field names; clamp to the proposal contract.
    visual["title"] = str(visual["title"])[:120]
    return DashboardVisualProposal.model_validate(visual)
