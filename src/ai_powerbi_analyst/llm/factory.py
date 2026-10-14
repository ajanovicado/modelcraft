"""Select the configured local inference backend without a silent fallback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ai_powerbi_analyst.settings import ModelSettings

from .runtime import OllamaModelRuntime

if TYPE_CHECKING:
    from .adaptive import AdaptiveQwenRuntime
    from .managed_ollama import ManagedOllamaModelRuntime


def create_model_runtime(
    settings: ModelSettings, evidence_root: Path | None = None
) -> OllamaModelRuntime | ManagedOllamaModelRuntime | AdaptiveQwenRuntime:
    if settings.runtime_config_path:
        from ai_powerbi_analyst.paths import resolve_runtime_config

        from .managed_ollama import ManagedOllamaModelRuntime

        try:
            configuration = json.loads(
                resolve_runtime_config(settings.runtime_config_path).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            # Preserve fail-closed health diagnostics for missing/bad configs.
            # No model can start until its runtime validates the config.
            configuration = {}
        if isinstance(configuration, dict) and configuration.get("adaptive_profiles") == 1:
            from .adaptive import AdaptiveQwenRuntime

            return AdaptiveQwenRuntime(settings, evidence_root=evidence_root)
        if isinstance(configuration, dict) and configuration.get("warm_session") is True:
            if configuration.get("validated_artifacts") is True:
                from .contract_ollama import ContractOllamaModelRuntime

                return ContractOllamaModelRuntime(settings, evidence_root=evidence_root)
            from .warm_ollama import WarmOllamaModelRuntime

            return WarmOllamaModelRuntime(settings, evidence_root=evidence_root)
        return ManagedOllamaModelRuntime(settings, evidence_root=evidence_root)
    return OllamaModelRuntime(settings)
