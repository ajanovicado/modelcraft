"""Resolve report editing commands against saved IDs before invoking generation."""

import re

from .contracts import ConversationContext, DashboardDraft
from .conversation import is_discussion_request


def filter_command(message: str) -> bool:
    return bool(
        not is_discussion_request(message)
        and re.search(
            r"\b(?:add|set|apply|change|edit|update|show|open|remove|clear|filter)\b", message, re.I
        )
        and re.search(r"\b(?:filter|filters|filer|slicer|slice)\b", message, re.I)
        and re.search(r"\b(?:date|chart|visual|slicer|slice)\b", message, re.I)
        and not re.search(r"\b(?:lod|measure|formula|calculation)\b", message, re.I)
    )


def report_filter_action(
    message: str, context: ConversationContext | None, draft: DashboardDraft | None
) -> tuple[str, dict[str, str] | None]:
    if draft is None:
        return "Create a report draft first, then choose a chart to filter.", None
    if not re.search(r"\bdate\b", message, re.I):
        return (
            "I can open a date filter for one chart. Other slicers and visual interactions "
            "aren't available yet. Select a chart and ask for a date filter.",
            None,
        )
    if re.search(
        r"\b(?:all|every|entire|whole)\b|\b(?:page|report)[ -]?(?:wide|level)\b", message, re.I
    ):
        return (
            "Date filters currently apply to one chart at a time. Page-wide slicers and "
            "interaction controls aren't available yet.",
            None,
        )
    candidates = [(p, v) for p in draft.pages for v in p.visuals]
    named = [(p, v) for p, v in candidates if v.title.casefold() in message.casefold()]
    # "Total Sales" is also a prefix of "Total Sales over time".
    if named:
        longest = max(len(v.title) for _, v in named)
        named = [(p, v) for p, v in named if len(v.title) == longest]
    if len(named) > 1 and context and context.page_id:
        on_page = [(p, v) for p, v in named if p.id == context.page_id]
        named = on_page or named
    # An explicit, unambiguous title takes precedence over an attached selection.
    selected = named[0] if len(named) == 1 else None
    if not named and context and context.visual_id:
        selected = next(
            (
                (p, v)
                for p, v in candidates
                if p.id == context.page_id and v.id == context.visual_id
            ),
            None,
        )
    action = {"kind": "visual_date_filter"}
    if selected:
        page, visual = selected
        action.update(page_id=page.id, visual_id=visual.id)
        text = (
            f"Choose the date field and range for {visual.title} in Date filter, then apply it. "
            "This filters only that visual; its measure stays unchanged."
        )
    else:
        if context and context.page_id:
            action["page_id"] = context.page_id
        text = (
            "Choose the chart, date field and range in Date filter, then apply it. "
            "This filters only the chosen visual; its measure stays unchanged."
        )
    return text, action
