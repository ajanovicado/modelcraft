"""Native forecast settings for a selected draft line chart, without model inference.

Power BI computes the future values. Studio retains only the typed settings and the
historical query receipt; it never presents invented forecast points as preview data.
"""

from __future__ import annotations

import re

from ai_powerbi_analyst.contracts import VisualForecast

from .contracts import DashboardDraft, DashboardPageProposal
from .conversation import is_discussion_request

_FORECAST = re.compile(r"\b(?:forecast(?:ing)?|forcast(?:ing)?)\b", re.I)
_CONTEXT = "\nSelected workspace context"
_LATEST = "Latest user instruction (takes precedence):\n"


class ForecastRequestError(ValueError):
    pass


def forecast_instruction(prompt: str) -> str:
    """Earlier requests and attached labels cannot turn a discussion into an edit."""
    return prompt.rsplit(_LATEST, 1)[-1].split(_CONTEXT, 1)[0].strip()


def forecast_command(prompt: str) -> bool:
    message = forecast_instruction(prompt)
    return bool(
        _FORECAST.search(message)
        and not is_discussion_request(message)
        and re.match(
            r"^(?:(?:please|ok|okay|now)[, ]+)*(?:(?:can|could|would) you\s+)?"
            r"(?:add|enable|apply|set|update|change|remove|disable|turn)\s+"
            r"(?:(?:on|off|a|the|native|power\s+bi)\s+)*"
            r"(?:\d+[ -](?:points?|periods?)[ -])?"
            r"(?:forecast(?:ing)?|forcast(?:ing)?)\b",
            message,
            re.I,
        )
        and not re.search(
            r"\b(?:measure|formula|table|column|dax)\b|\b(?:forecast(?:ing)?|forcast(?:ing)?)\s+page\b",
            message,
            re.I,
        )
    )


def forecast_pages(draft: DashboardDraft, prompt: str) -> list[DashboardPageProposal]:
    message = forecast_instruction(prompt)
    if re.search(
        r"\b(?:and|also|then)\s+(?:add|create|remove|delete|change|replace|move)\b",
        message,
        re.I,
    ):
        raise ForecastRequestError(
            "Apply the forecast to one chart at a time, then request other changes."
        )
    candidates = [(page, visual) for page in draft.pages for visual in page.visuals]
    named = [
        (page, visual)
        for page, visual in candidates
        if any(
            re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", message, re.I)
            for label in (visual.id, visual.title)
        )
    ]
    if named:
        longest = max(len(visual.title) for _, visual in named)
        named = [(p, v) for p, v in named if len(v.title) == longest]
    named_pages = [
        page.id
        for page in draft.pages
        if re.search(r"(?<!\w)" + re.escape(page.title) + r"(?!\w)", message, re.I)
    ]
    if len(named_pages) == 1:
        named = [(p, v) for p, v in named if p.id == named_pages[0]]
    context = prompt.rsplit(_CONTEXT, 1)[-1] if _CONTEXT in prompt else ""
    page_ids = re.findall(r"^Page: .+ \[([a-z][a-z0-9_]*)\]$", context, re.M)
    visual_ids = re.findall(r"^Visual: .+ \[([a-z][a-z0-9_]*)\]$", context, re.M)
    if len(named) > 1 and len(page_ids) == 1:
        on_page = [(p, v) for p, v in named if p.id == page_ids[0]]
        named = on_page or named
    if not named and not named_pages and len(page_ids) == len(visual_ids) == 1:
        named = [(p, v) for p, v in candidates if p.id == page_ids[0] and v.id == visual_ids[0]]
    if len(named) != 1:
        raise ForecastRequestError(
            "Select one line chart, then ask to add forecasting. You can also name its full title."
        )
    page, visual = named[0]
    if visual.recipe != "trend" or visual.visual_type != "lineChart":
        raise ForecastRequestError(
            "Forecasting requires a line chart with a date axis. "
            "Select the time-series line chart first."
        )
    remove = bool(
        re.search(r"\b(?:remove|disable|turn\s+off)\b|\bforecast(?:ing)?\s+off\b", message, re.I)
    )
    settings = None if remove else _settings(message, visual.forecast)
    if visual.forecast == settings:
        raise ForecastRequestError(
            "That chart already has these forecast settings."
            if settings
            else "That chart has no forecast to remove."
        )
    return [
        item.model_copy(
            update={
                "visuals": [
                    v.model_copy(update={"forecast": settings})
                    if item.id == page.id and v.id == visual.id
                    else v
                    for v in item.visuals
                ]
            }
        )
        for item in draft.pages
    ]


def _settings(message: str, current: VisualForecast | None) -> VisualForecast:
    # The native transform uses points. Never silently treat '6 months' as six points
    # on a chart that Power BI may evaluate at daily grain.
    if re.search(r"\b\d+\s*(?:days?|weeks?|months?|quarters?|years?)\b", message, re.I):
        raise ForecastRequestError(
            "Set forecast length in points in the Visual pane; "
            "calendar-unit forecast lengths are not supported yet."
        )
    values = current.model_dump() if current else VisualForecast().model_dump()
    length = re.search(
        r"\b(?:forecast(?:ing)?|forcast(?:ing)?)(?:\s+(?:the\s+)?next|\s+for)?"
        r"\s+(\d+)\s*(?:points?|periods?)\b|\b(?:length|horizon)\s+(?:to\s+)?(\d+)\b",
        message,
        re.I,
    )
    if length:
        values["forecast_length"] = int(length[1] or length[2])
    else:
        prefix_length = re.search(
            r"\b(\d+)[ -](?:points?|periods?)[ -](?:forecast(?:ing)?|forcast(?:ing)?)\b",
            message,
            re.I,
        )
        if prefix_length:
            values["forecast_length"] = int(prefix_length[1])
    confidence = re.search(r"\b(\d+)\s*%|\bconfidence\s+(?:of\s+|to\s+)?(\d+)\b", message, re.I)
    if confidence:
        values["confidence"] = int(confidence[1] or confidence[2])
    ignore = re.search(r"\bignore\s+(?:the\s+)?last\s+(\d+)\b", message, re.I)
    if ignore:
        values["ignore_last"] = int(ignore[1])
    seasonality = re.search(r"\bseasonality\s+(?:of\s+|to\s+)?(auto|\d+)\b", message, re.I)
    if seasonality:
        values["seasonality"] = None if seasonality[1].casefold() == "auto" else int(seasonality[1])
    try:
        return VisualForecast.model_validate(values)
    except ValueError as error:
        raise ForecastRequestError(
            "Use 1–60 forecast points and a confidence level of 75, 80, 85, 90, 95 or 99%."
        ) from error


def forecast_reply(before: DashboardDraft, after: DashboardDraft) -> str:
    old = {v.id: v for p in before.pages for v in p.visuals}
    changed = next(v for p in after.pages for v in p.visuals if v.forecast != old[v.id].forecast)
    if changed.forecast:
        settings = changed.forecast
        return (
            f"Forecast enabled on “{changed.title}”: {settings.forecast_length} points at "
            f"{settings.confidence}% confidence. The Studio chart shows historical values; "
            "Power BI calculates the forecast after you approve and build. "
            "The draft is ready for review. No Power BI project files have changed."
        )
    return (
        f"Forecast removed from “{changed.title}” in the draft. Review before building. "
        "No Power BI project files have changed."
    )
