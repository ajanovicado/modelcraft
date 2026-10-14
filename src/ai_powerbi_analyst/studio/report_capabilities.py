"""One versioned, conservative capability contract for Studio, its UI and Qwen.

The installed Power BI catalog describes what the native engine knows. This
registry describes what Studio can currently author. Discovery never enables
an unimplemented visual or weakens validation.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    text = (
        files("ai_powerbi_analyst.studio")
        .joinpath("report_visuals.json")
        .read_text(encoding="utf-8")
    )
    return dict(json.loads(text))


def report_capabilities() -> dict[str, Any]:
    # Do not let an API caller or prompt adapter mutate the cached contract.
    return dict(json.loads(json.dumps(_catalog())))


def visual_types_by_recipe() -> dict[str, frozenset[str]]:
    catalog = _catalog()
    return {
        recipe: frozenset(v["id"] for v in catalog["visuals"] if v["recipe"] == recipe)
        for recipe in catalog["families"]
    }


def minimum_visual_sizes() -> dict[str, tuple[int, int]]:
    return {
        key: (value["minimum_size"][0], value["minimum_size"][1])
        for key, value in _catalog()["families"].items()
    }


def model_visual_capabilities() -> dict[str, Any]:
    """Small metadata-only context: no source rows, credentials or tool authority."""
    catalog = report_capabilities()
    return {
        "version": catalog["version"],
        "types_by_recipe": {key: sorted(value) for key, value in visual_types_by_recipe().items()},
        "roles_by_recipe": {key: value["roles"] for key, value in catalog["families"].items()},
        "limits": catalog["limits"],
        "limitations": catalog["limitations"],
        "editing": {"visual_forecast": catalog["editing"]["visual_forecast"]},
        "write_policy": (
            "Propose typed drafts using reviewed catalog IDs. Only the existing validation "
            "and approval workflow may apply or build them."
        ),
    }


def report_chat_guidance() -> str:
    catalog = _catalog()
    labels = ", ".join(v["label"] for v in catalog["visuals"])
    forecast = catalog["editing"]["visual_forecast"]
    defaults = forecast["defaults"]
    settings = forecast["settings"]
    confidence = "/".join(str(value) for value in settings["confidence"]["enum"])
    return (
        "\nStudio report authoring capabilities (not all Power BI capabilities): "
        + labels
        + ". "
        + " ".join(catalog["limitations"])
        + " Explain unsupported requests honestly; "
        "do not silently substitute a table for a requested matrix."
        + " Forecast is a setting, not a visual_type: one "
        f"{forecast['recipe']} {forecast['visual_type']}, date axis, one measure. "
        f"Points {settings['forecast_length']['minimum']}-{settings['forecast_length']['maximum']} "
        f"(default {defaults['forecast_length']}); "
        f"confidence {confidence}% ({defaults['confidence']}); "
        f"ignore last {settings['ignore_last']['minimum']}-{settings['ignore_last']['maximum']} "
        f"({defaults['ignore_last']}); seasonality auto or "
        f"{settings['seasonality']['minimum']}-{settings['seasonality']['maximum']} points. "
        "Review, approve and build for Power BI to calculate it. "
        "Studio previews historical values only; never invent future values."
    )
