"""Small immutable conversation context used across Streamlit reruns."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from ai_powerbi_analyst.contracts import AnalysisPlan, EvidenceAnswer, RouteIntent, Scalar


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ConversationState:
    project_id: str
    active_page_key: str | None = None
    active_filters: dict[str, Scalar] = field(default_factory=dict)
    last_evidence: EvidenceAnswer | None = None
    last_analysis_plan: AnalysisPlan | None = None
    last_route: RouteIntent | None = None
    report_hash: str | None = None
    turns: tuple[ConversationTurn, ...] = ()
    max_turns: int = 20

    def record_message(self, role: Literal["user", "assistant"], content: str) -> ConversationState:
        clean = " ".join(content.split())
        if not clean:
            return self
        turns = (*self.turns, ConversationTurn(role=role, content=clean))[-self.max_turns :]
        return replace(self, turns=turns)

    def record_route(self, route: RouteIntent) -> ConversationState:
        return replace(self, last_route=route)

    def record_report_hash(self, report_hash: str) -> ConversationState:
        return replace(self, report_hash=report_hash)

    def clear_evidence(self) -> ConversationState:
        """Invalidate the deictic evidence referent while retaining turn history."""

        return replace(
            self,
            last_evidence=None,
            last_analysis_plan=None,
            active_filters={},
        )

    def record_evidence(
        self,
        evidence: EvidenceAnswer,
        *,
        analysis_plan: AnalysisPlan | None = None,
        user_message: str | None = None,
    ) -> ConversationState:
        state = self
        if user_message:
            state = state.record_message("user", user_message)
        state = state.record_message("assistant", evidence.conclusion)
        return replace(
            state,
            active_page_key=evidence.target_page_key or state.active_page_key,
            active_filters=dict(evidence.filters),
            last_evidence=evidence,
            last_analysis_plan=analysis_plan or state.last_analysis_plan,
            last_route=RouteIntent.ANALYZE,
        )
