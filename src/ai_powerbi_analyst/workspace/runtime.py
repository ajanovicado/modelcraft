"""Assemble user-data, local-model, planning, and PBIP-generation services."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ai_powerbi_analyst.data.onboarding import DataOnboardingService
from ai_powerbi_analyst.llm.factory import create_model_runtime
from ai_powerbi_analyst.llm.runtime import (
    GenerationMetrics,
    LocalModelError,
    ModelAttestation,
    ModelHealth,
    StructuredModelRuntime,
    StructuredOutput,
)
from ai_powerbi_analyst.powerbi.semantic_model import SemanticProjectBuilder
from ai_powerbi_analyst.powerbi.visual_catalog import VisualCatalogService
from ai_powerbi_analyst.settings import AppSettings, load_settings
from ai_powerbi_analyst.workspace.planner import DashboardPlanningService, default_visual_catalog


class _FixtureModelRuntime:
    """Explicitly disabled local inference for the prepared fixture workspace."""

    last_generation: GenerationMetrics | None = None
    _message = "Local AI is disabled in fixture mode. Use a real workspace to run the local model."

    def health(self) -> ModelHealth:
        return ModelHealth(
            service_available=False,
            model_available=False,
            endpoint="fixture mode (no model connection)",
            available_memory_gb=0,
            error=self._message,
        )

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredOutput],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: int | None = None,
        repair_attempts: int | None = None,
    ) -> StructuredOutput:
        raise LocalModelError(self._message)

    def stream_chat(
        self,
        message: str,
        *,
        system_prompt: str,
        history: list[dict[str, str]] | None = None,
        worked_examples: bool = True,
        max_output_tokens: int | None = None,
    ) -> Iterator[str]:
        raise LocalModelError(self._message)

    def unload(self) -> None:
        pass

    def write_attestation(self, path: Path) -> ModelAttestation:
        raise LocalModelError(self._message)


@dataclass(frozen=True, slots=True)
class UserWorkspaceServices:
    settings: AppSettings
    onboarding: DataOnboardingService
    model: StructuredModelRuntime
    planner: DashboardPlanningService
    projects: SemanticProjectBuilder


def build_user_workspace_services(
    project_root: str | Path,
    state_root: str | Path,
    *,
    mode: Literal["real", "mock"] = "real",
) -> UserWorkspaceServices:
    if mode not in {"real", "mock"}:
        raise ValueError("Workspace service mode must be 'real' or 'mock'")
    repository = Path(project_root).expanduser().resolve(strict=True)
    runtime = Path(state_root).expanduser().resolve(strict=False)
    settings = load_settings(repository / "config" / "defaults.yaml")
    if mode == "mock":
        model: StructuredModelRuntime = _FixtureModelRuntime()
        visual_catalog = default_visual_catalog()
    else:
        model = create_model_runtime(settings.model, runtime / "model-evidence")
        shared_catalog = repository / ".runtime" / "visual-catalog.json"
        catalog_cache = (
            shared_catalog if shared_catalog.is_file() else runtime / "visual-catalog.json"
        )
        visual_catalog = VisualCatalogService(catalog_cache).load()
    return UserWorkspaceServices(
        settings=settings,
        onboarding=DataOnboardingService(runtime / "user-data"),
        model=model,
        planner=DashboardPlanningService(model, visual_catalog),
        projects=SemanticProjectBuilder(
            runtime / "user-projects",
            repository / "fixtures" / "powerbi" / "client-wow-template",
        ),
    )
