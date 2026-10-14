"""UI-level configuration that does not depend on a concrete backend."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ai_powerbi_analyst.contracts import AnalysisPlan, EvidenceAnswer, ReportChangePlan
from ai_powerbi_analyst.orchestration.conversation import ConversationState
from ai_powerbi_analyst.orchestration.service import ApplicationOrchestrator


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    project_id: str
    project_name: str
    data_through: date
    approved_metrics: tuple[str, ...]
    compatibility_label: str = "Not validated"
    compatibility_detail: str = "Compatibility has not been established"
    can_open_power_bi: bool = False
    forecast_version: str = "1.0.0"
    approved_price_change_pct: int = 3
    price_elasticity: float = -0.8


@dataclass(frozen=True, slots=True)
class AppWorkspace:
    config: WorkspaceConfig
    orchestrator: ApplicationOrchestrator
    initial_prompt: str | None = None
    initial_plan: ReportChangePlan | None = None
    initial_active_plan: ReportChangePlan | None = None
    initial_evidence: EvidenceAnswer | None = None
    initial_analysis_plan: AnalysisPlan | None = None
    initial_conversation: ConversationState | None = None
    initial_validated: bool = False
    initial_undo_available: bool = False
