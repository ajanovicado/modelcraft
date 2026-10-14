"""Keep advertised forecast settings synchronized with the validated native contract."""

from __future__ import annotations

import json

import pytest

from ai_powerbi_analyst.contracts import VisualForecast
from ai_powerbi_analyst.studio.report_capabilities import (
    model_visual_capabilities,
    report_capabilities,
    report_chat_guidance,
    visual_types_by_recipe,
)


def test_forecast_metadata_matches_schema_defaults_and_confidence_choices() -> None:
    capability = report_capabilities()["editing"]["visual_forecast"]
    properties = VisualForecast.model_json_schema()["properties"]
    assert capability["defaults"] == VisualForecast().model_dump()
    assert capability["settings"]["confidence"]["enum"] == properties["confidence"]["enum"]
    assert capability["settings"]["confidence"]["unit"] == "percent"


@pytest.mark.parametrize("field", ["forecast_length", "ignore_last", "seasonality"])
def test_forecast_catalog_limits_match_validated_settings(field: str) -> None:
    advertised = report_capabilities()["editing"]["visual_forecast"]["settings"][field]
    schema = VisualForecast.model_json_schema()["properties"][field]
    if "anyOf" in schema:
        assert advertised["nullable"] is True
        assert {"type": "null"} in schema["anyOf"]
        schema = next(option for option in schema["anyOf"] if option["type"] == "integer")
    assert advertised["minimum"] == schema["minimum"]
    assert advertised["maximum"] == schema["maximum"]
    assert advertised["unit"] == "points"


def test_forecast_is_native_editing_on_one_bound_line_chart_not_a_chart_type() -> None:
    catalog = report_capabilities()
    capability = catalog["editing"]["visual_forecast"]
    assert capability["scope"] == "one_visual"
    assert capability["recipe"] == "trend"
    assert capability["visual_type"] == "lineChart"
    assert capability["axis"] == "date_column"
    assert capability["measures"] == 1
    assert capability["preview"] == "historical_only"
    assert capability["powerbi"] == "native_forecast"
    roles = catalog["families"][capability["recipe"]]["roles"]
    assert roles["Category"] == {"kind": capability["axis"], "min": 1, "max": 1}
    assert roles["Y"] == {"kind": "measure", "min": 1, "max": capability["measures"]}
    assert capability["visual_type"] in visual_types_by_recipe()[capability["recipe"]]
    assert not any("forecast" in visual["id"].casefold() for visual in catalog["visuals"])


def test_forecast_guidance_preserves_native_calculation_and_review_policy() -> None:
    guidance = report_chat_guidance()
    assert "Forecast is a setting, not a visual_type" in guidance
    assert "one trend lineChart, date axis, one measure" in guidance
    assert "Points 1-60 (default 6)" in guidance
    assert "confidence 75/80/85/90/95/99% (95)" in guidance
    assert "ignore last 0-500 (0)" in guidance
    assert "seasonality auto or 1-120 points" in guidance
    assert "Review, approve and build for Power BI to calculate it" in guidance
    assert "Studio previews historical values only; never invent future values" in guidance
    assert len(guidance) < 1200


def test_model_metadata_is_bounded_and_does_not_mutate_the_forecast_registry() -> None:
    context = model_visual_capabilities()
    capability = report_capabilities()["editing"]["visual_forecast"]
    assert context["editing"]["visual_forecast"] == capability
    assert len(json.dumps(context)) < 3000
    context["editing"]["visual_forecast"]["defaults"]["forecast_length"] = 9999
    next_defaults = model_visual_capabilities()["editing"]["visual_forecast"]["defaults"]
    assert next_defaults["forecast_length"] == 6
