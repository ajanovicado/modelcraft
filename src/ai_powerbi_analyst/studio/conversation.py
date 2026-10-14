"""Bounded conversation context and conservative, deterministic action routing."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime


def is_discussion_request(message: str) -> bool:
    return bool(
        re.search(
            r"\b(?:don't|do not|never|explain|why|how|what|can you do|can you support)\b",
            message,
            re.I,
        )
    )


_DEVELOPER_TOPIC = re.compile(
    r"(?<!\w)(?:python|javascript|typescript|react|fastapi|flask|html|css|sql|bash|"
    r"powershell|java|swift|kotlin|php|ruby|c#|c\+\+|csharp|"
    r"c(?=\s+(?:language|program|code|script|function|class|pointer|memory))|"
    r"rust|golang|go(?=\s+(?:language|program|code|"
    r"script|function|routine))|r(?=\s+(?:script|function|code|program|vector|package))|"
    r"code\s+(?:snippet|example)|unit\s+tests?|apis?|graphql|programming|software\s+design|"
    r"git|docker|kubernetes|ci/cd|ci\s+pipelines?|databases?|indexing|testing|debugging|"
    r"algorithms?|data\s+structures?|http|oauth|jwt|authentication|authorization|"
    r"dependency\s+injection|asynchronous\s+programming)"
    r"(?!\w)",
    re.I,
)
_BI_TOPIC = re.compile(r"\b(?:dax|power\s+(?:bi|query)|powerbi|pbix|pbip)\b", re.I)
_WORKSPACE_TOPIC = re.compile(
    r"\b(?:this|my|our|current|selected|imported)\s+(?:(?:power\s+bi|imported)\s+)?"
    r"(?:workspace|dataset|data|report|dashboard|model|schema|table|measure|visual|chart|"
    r"field|column|relationship|page)\b",
    re.I,
)
_REPORT_TARGET = re.compile(
    r"\b(?:report|dashboard|visual|chart|graph|measure|slicer|workspace|dataset)\b",
    re.I,
)
_REPORT_ACTION = re.compile(
    r"^(?:(?:please|ok|okay)[, ]+)*(?:(?:can|could|would) you |i want you to )?"
    r"(?:build|create|add|make|generate|update|change|rename|remove|delete|move)\s+"
    r"(?:(?:a|an|the|this|my|our|new)\s+)*(?:report|dashboard|visual|chart|graph|"
    r"measure|slicer|model|page)\b",
    re.I,
)
_DEVELOPER_FOLLOWUP = re.compile(
    r"\b(?:examples?|type\s+hints?|annotations?|trade[ -]?offs?|explain\s+(?:it|that|this)|"
    r"(?:simplify|expand|improve|rewrite|debug|test)\s+(?:it|that|this)|"
    r"(?:make|convert)\s+it|continue)\b",
    re.I,
)


def needs_workspace_context(message: str) -> bool:
    """Explicit BI or workspace questions retain the supplied model evidence."""
    return bool(_BI_TOPIC.search(message) or _WORKSPACE_TOPIC.search(message))


def is_developer_request(message: str, history: Sequence[Mapping[str, str]] = ()) -> bool:
    """Technical concepts and code stay discussion; follow-ups use only recent user intent."""
    if re.search(r"\b(?:python|r)\s+visual\b|\b(?:dax|sql)\s+measure\b", message, re.I):
        return False
    artifact = bool(
        re.search(
            r"\b(?:scripts?|functions?|class(?:es)?|components?|endpoints?|servers?|queries|query|"
            r"code|snippets?|programs?|websites?|apps?|routes?|handlers?|unit\s+tests?)\b",
            message,
            re.I,
        )
    )
    if artifact and re.search(r"\b(?:dax|power\s+query)\s+(?:query|script|code)\b", message, re.I):
        return True
    if _DEVELOPER_TOPIC.search(message):
        if _REPORT_ACTION.search(message.strip()):
            return False
        if artifact:
            return True
        # Conceptual BI questions keep their normal report grounding. A language
        # mentioned as the source of a dashboard is not a request to write code.
        return not needs_workspace_context(message) and not _REPORT_TARGET.search(message)
    if needs_workspace_context(message) or _REPORT_TARGET.search(message):
        return False
    if not _DEVELOPER_FOLLOWUP.search(message):
        return False
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        earlier = item.get("content", "")
        if needs_workspace_context(earlier):
            return False
        if is_developer_request(earlier):
            return True
        if _REPORT_TARGET.search(earlier):
            return False
        if not _DEVELOPER_FOLLOWUP.search(earlier):
            return False
    return False


def developer_history(
    history: Sequence[Mapping[str, str]], *, max_characters: int = 1600
) -> list[dict[str, str]]:
    """Keep a bounded technical discussion, prioritizing user intent over assistant verbosity."""
    start = len(history)
    for index in range(len(history) - 1, -1, -1):
        item = history[index]
        if item.get("role") != "user":
            continue
        message = item.get("content", "")
        if needs_workspace_context(message):
            break
        if not is_developer_request(message) and (
            _REPORT_TARGET.search(message) or not _DEVELOPER_FOLLOWUP.search(message)
        ):
            break
        start = index
    result = [{"role": item["role"], "content": item["content"]} for item in history[start:]]
    if sum(len(item["content"]) for item in result) <= max_characters:
        return result
    users = [index for index, item in enumerate(result) if item["role"] == "user"]
    if not users:
        return []
    topic = next(
        (index for index in reversed(users) if is_developer_request(result[index]["content"])),
        users[-1],
    )
    # A terse follow-up needs its earlier explicit topic as well as the newest
    # user instruction. Long assistant replies cannot spend that reserved space.
    selected_users = sorted({topic, users[-1]})
    selected = {
        index
        for user in selected_users
        for index in range(user, next((later for later in users if later > user), len(result)))
    }
    user_limit = max_characters // len(selected_users)
    for index in selected_users:
        result[index]["content"] = result[index]["content"][:user_limit]
    remaining = max_characters - sum(len(result[index]["content"]) for index in selected_users)
    for index in sorted(selected, reverse=True):
        if result[index]["role"] != "assistant":
            continue
        content = result[index]["content"]
        if len(content) > remaining:
            marker = " [excerpt]"
            content = (
                content[: remaining - len(marker)] + marker
                if remaining > len(marker)
                else content[:remaining]
            )
        result[index]["content"] = content
        remaining -= len(content)
    return [result[index] for index in sorted(selected) if result[index]["content"]]


# Chat revisions can only add. An edit command ("remove the detail page") reached the chat
# model, which answered with unrelated admin steps; it gets this reply without a model call.
DRAFT_EDIT_REPLY = (
    "Chat can add pages, visuals, measures and relationships to the draft, but it cannot "
    "change, remove, rename or move what is already there. Use the designer for that. "
    "The report has not changed."
)


def is_draft_edit_command(message: str) -> bool:
    """An imperative edit of a report element; data-prep asks ("remove duplicates") stay chat."""
    text = message.strip().casefold()
    return (
        not is_discussion_request(text)
        and not is_developer_request(text)
        and bool(
            re.match(
                r"^(?:(?:please|ok|okay)[, ]+)*(?:(?:can|could|would) you |i want you to )?"
                r"(?:remove|delete|rename|replace|move|swap|hide|get rid of)\b",
                text,
            )
        )
        and bool(
            re.search(
                r"\b(?:page|tab|visual|chart|graph|card|kpi|slicer|title|measure|trend|"
                r"scorecard|map|matrix|donut|pie|gauge|funnel|waterfall)s?\b",
                text,
            )
        )
        and not re.search(
            r"\b(?:duplicate|row|null|blank|column|query|queries|step|error|space)s?\b", text
        )
    )


def is_build_request(message: str) -> bool:
    text = message.strip().casefold()
    if is_discussion_request(text) or is_developer_request(text):
        return False
    if re.match(
        r"^(?:on|in)\s+.{1,100}?\s+(?:page|tab)\s*[, :]\s*(?:add|insert|place|put)\b", text
    ):
        return True
    if re.match(
        r"^(?:(?:please|yes|okay|ok)[, ]+)*(?:(?:can|could|would) you |i want you to )?"
        r"(?:join|relate|connect)\b",
        text,
    ):
        # Connection setup and social requests do not become model revisions.
        return bool(
            re.search(r"\b(?:to|and|with|between|tables?)\b|\[[^\]]+\]", text)
        ) and not bool(
            re.search(r"\b(?:database|server|account|website|api|internet|wi[ -]?fi)\b", text)
        )
    return bool(
        re.match(
            r"^(?:(?:please|yes|okay|ok)[, ]+)*(?:(?:can|could|would) you |i want you to )?"
            r"(?:build|create|crate|add|make|generate|revise|update|apply|implement)\b",
            text,
        )
    )


def bounded_history(messages: list[dict[str, object]], *, since: str = "") -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    budget = 5000
    for item in reversed(messages):
        role = str(item.get("role", ""))
        created = str(item.get("created_at", ""))
        if role not in {"user", "assistant"}:
            continue
        if since and created:
            # SQLite timestamps have second precision; do not discard later turns
            # in the same second as a new snapshot.
            timestamp = datetime.fromisoformat(created).replace(tzinfo=UTC)
            if timestamp < datetime.fromisoformat(since).replace(tzinfo=UTC, microsecond=0):
                continue
        content = str(item.get("content", ""))[:1600]
        if len(content) > budget or len(result) >= 8:
            break
        result.append({"role": role, "content": content})
        budget -= len(content)
    result.reverse()
    # A size/turn/snapshot cutoff must not promote an orphan assistant reply
    # into the first turn of the next prompt.
    while result and result[0]["role"] == "assistant":
        result.pop(0)
    return result


def revision_prompt(message: str, history: list[dict[str, str]]) -> str:
    """Resolve follow-ups from USER instructions, never promote assistant pseudo-DAX."""
    users: list[str] = []
    for item in reversed(history):
        if item["role"] == "assistant" and item["content"].startswith("Draft updated:"):
            break
        if item["role"] == "user":
            users.append(item["content"])
        if len(users) == 3:
            break
    refers_back = bool(
        re.search(r"\b(?:it|this|that|these|those|around|lod|level of detail)\b", message, re.I)
    )
    pending = bool(
        history
        and history[-1]["role"] == "assistant"
        and history[-1]["content"].startswith("Before I create the LOD")
    )
    if not refers_back and not pending:
        return message[:2000]
    return (
        "Earlier user requirements:\n"
        + "\n".join(reversed(users))[-1200:]
        + "\nLatest user instruction (takes precedence):\n"
        + message[:650]
    )[:2000]


def awaiting_lod_details(history: list[dict[str, str]]) -> bool:
    return bool(
        history
        and history[-1]["role"] == "assistant"
        and history[-1]["content"].startswith("Before I create the LOD")
    )
