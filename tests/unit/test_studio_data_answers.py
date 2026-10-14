"""Chat answers exact data questions from evaluated DAX instead of the model's guesses."""

from __future__ import annotations

from threading import Event

import pytest
from test_studio_dax_relationships import _approve, superstore  # noqa: F401 - fixture

from ai_powerbi_analyst.studio.contracts import AddDaxMeasureRequest, ChatRequest
from ai_powerbi_analyst.studio.data_answers import format_value, plan_data_answer


@pytest.fixture
def ready(superstore):  # noqa: F811 - pytest fixture injection
    service, workspace_id = superstore
    _approve(service, workspace_id, include={"returns", "people"}, key="answers-approve")
    draft = service.draft(service.workspace(workspace_id).active_draft_id)
    service.add_dax_measure(
        workspace_id,
        AddDaxMeasureRequest(
            workspace_id=workspace_id,
            name="Return Rate",
            expression=(
                "DIVIDE(CALCULATE(DISTINCTCOUNT('Orders'[Order ID]), "
                "'Returns'[Returned] = TRUE()), DISTINCTCOUNT('Orders'[Order ID]))"
            ),
            idempotency_key="answers-save-rate",
            expected_base_hash=draft.base_hash,
        ),
    )
    return service, workspace_id


def _ask(service, workspace_id: str, message: str) -> str:
    events = list(
        service.chat_events(ChatRequest(workspace_id=workspace_id, message=message), Event())
    )
    return "".join(event["data"]["text"] for event in events if event["event"] == "token")


def test_ranking_question_is_computed_with_the_related_manager(ready) -> None:
    service, workspace_id = ready
    # The planner has no model; a model call here would raise instead of answering.
    answer = _ask(
        service, workspace_id, "Which region has the highest return rate, and who manages it?"
    )
    assert answer.startswith("**Central · Cy** has the highest Return Rate: **100.0%**.")
    assert "- West · Bob: 50.0%" in answer
    assert "East" not in answer  # no returns: BLANK rate, omitted like SUMMARIZECOLUMNS
    assert "SUMMARIZECOLUMNS('People'[Region], 'People'[Person]" in answer
    messages = service.messages(workspace_id)
    assert [m["role"] for m in messages[-2:]] == ["user", "assistant"]
    assert any(e["type"] == "chat.data_answer" for e in service.events(workspace_id))
    # The report view attaches its page to every message; that must not block answers.
    from ai_powerbi_analyst.studio.contracts import ConversationContext

    page = service.draft(service.workspace(workspace_id).active_draft_id).pages[0]
    events = list(
        service.chat_events(
            ChatRequest(
                workspace_id=workspace_id,
                message="Which region has the lowest sales?",
                context=ConversationContext(page_id=page.id),
            ),
            Event(),
        )
    )
    assert "has the lowest Total Sales" in events[-1]["data"]["text"]


def test_named_values_filter_and_lowest_ranks_ascending(ready) -> None:
    service, workspace_id = ready
    assert _ask(service, workspace_id, "What are total sales in the West?").startswith(
        "Total Sales for West is **400**."
    )
    lowest = _ask(service, workspace_id, "Which region had the lowest sales?")
    assert lowest.startswith("**Central · Cy** has the lowest Total Sales: **80**.")
    listed = _ask(service, workspace_id, "Show total sales by region")
    assert listed.startswith("Total Sales by Region and Person:\n- West · Bob: 400")


@pytest.mark.parametrize(
    "question",
    [
        "Why is West so strong?",
        "What is the average sales per order?",
        "Which region has the highest profit margin?",
        "What were sales in 2017?",
        "Which region has the most sales excluding West?",
        "How should I design a returns page?",
        "Build a page with sales by region",
        "Tell me about the data",
    ],
)
def test_other_questions_stay_with_the_model(ready, question: str) -> None:
    service, workspace_id = ready
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    draft = service.draft(service.workspace(workspace_id).active_draft_id)
    assert plan_data_answer(question, snapshot, draft) is None


def test_small_model_context_keeps_every_table_field_and_join(ready) -> None:
    """At 1,000 characters People vanished and the model denied its join to Orders."""
    service, workspace_id = ready
    workspace = service.workspace(workspace_id)
    context = service._chat_context(workspace, "who manages the west?", max_characters=1400)
    assert '"table":"People"' in context and "Person, Region" in context
    assert '"table":"Returns"' in context and "Returned (true/false)" in context
    assert "Orders[Region] *-1 People[Region]" in context
    assert "field_id" not in context and "memory_policy" not in context
    assert "context_notice" not in context
    # The large-budget context keeps full records and field IDs.
    assert "field_id" in service._chat_context(workspace, "who manages the west?")


def test_values_follow_the_measure_format() -> None:
    assert format_value(0.0590936, "0.0%") == "5.9%"
    assert format_value(2297200.8603, "#,#0.00") == "2,297,200.86"
    assert format_value(37873, "#,#0") == "37,873"
    assert format_value(None, "#,#0") == "(Blank)"
