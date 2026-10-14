from __future__ import annotations

from ai_powerbi_analyst.contracts import (
    ChartSpec,
    EvidenceAnswer,
    EvidenceVisualPlan,
    RouteIntent,
)
from ai_powerbi_analyst.orchestration.conversation import ConversationState


def _answer() -> EvidenceAnswer:
    return EvidenceAnswer(
        conclusion="Input costs contributed most to the West margin decline.",
        facts=["Input costs contributed -2.3 percentage points."],
        metric_ids=["gross_margin_pct"],
        comparison_period="Q1 2025 vs Q1 2024",
        filters={"region": "West"},
        data_hash="a" * 64,
        catalog_hash="c" * 64,
        catalog_version="1.0.0",
        query_plan_hash="b" * 64,
        chart=ChartSpec(
            kind="contribution_bar",
            title="Margin variance by driver (pp)",
            x_field="contribution_pp",
            y_field="driver",
        ),
        rows=[{"driver": "Input costs", "contribution_pp": -2.3}],
        add_to_report_supported=True,
        target_page_key="margin_drivers",
        report_visual_plan=EvidenceVisualPlan(
            binding_id="west_driver_bridge",
            metric_object_id="driver_contribution_west",
            dimension_object_id="driver",
        ),
    )


def test_recording_evidence_preserves_follow_up_context() -> None:
    state = ConversationState(project_id="sales-margin-demo")

    updated = state.record_evidence(_answer(), user_message="Why did margin fall?")

    assert updated.last_evidence is not None
    assert updated.last_evidence.filters == {"region": "West"}
    assert updated.active_filters == {"region": "West"}
    assert updated.last_route is RouteIntent.ANALYZE
    assert updated.turns[-1].role == "assistant"


def test_conversation_state_is_immutable_and_caps_history() -> None:
    state = ConversationState(project_id="sales-margin-demo")
    original = state

    for index in range(30):
        state = state.record_message("user", f"message {index}")

    assert original.turns == ()
    assert len(state.turns) == state.max_turns
    assert state.turns[0].content == "message 10"
