"""Guarded Ollama adapter with the policy measured in the confirmation comparison."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .guarded import GuardedModelRuntime
from .runtime import LocalModelError, ModelIdentity, StructuredOutput

if TYPE_CHECKING:
    from .managed_ollama_backend import OllamaServer


class ManagedOllamaModelRuntime(GuardedModelRuntime):
    endpoint_label = "managed Ollama (loopback; CPU; guarded)"

    def _configuration(self) -> tuple[Path, dict[str, Any]]:
        if not self.settings.runtime_config_path:
            raise LocalModelError("Guarded Ollama requires runtime_config_path")
        from ai_powerbi_analyst.paths import normalize_profile_config, resolve_runtime_config

        path = resolve_runtime_config(self.settings.runtime_config_path).resolve(strict=True)
        cfg = normalize_profile_config(json.loads(path.read_text(encoding="utf-8")), path)
        from .resource_profiles import configured_profile

        configured_profile(cfg)
        if self.settings.selected_digest != "sha256:" + cfg["package_sha256"]:
            raise LocalModelError("Configured package digest differs from the pinned runtime")
        if self.settings.name != cfg["tag"]:
            raise LocalModelError("Configured model name differs from the pinned runtime")
        if self.settings.context_tokens != 4096 or cfg["context_tokens"] != 4096:
            raise LocalModelError("Qualified guarded profile requires 4096 context tokens")
        if self.settings.thinking_enabled or self.settings.temperature != 0:
            raise LocalModelError(
                "Qualified guarded profile requires greedy inference without thinking"
            )
        return path, cfg

    def _identity(self, cfg: dict[str, Any]) -> ModelIdentity:
        return ModelIdentity(
            name=cfg["tag"],
            digest="sha256:" + cfg["package_sha256"],
            size_bytes=Path(cfg["model"]).stat().st_size,
            parameter_size=cfg["parameter_size"],
            quantization=cfg.get("quantization", "Q4_K_M"),
            license=cfg["license"],
            source_url=cfg["source_url"],
        )

    def _server(
        self, cfg: dict[str, Any], check: Callable[[], dict[str, object]], target: Path
    ) -> OllamaServer:
        if cfg.get("backend") == "managed-llama-text":
            from .text_only_backend import TextOnlyServer

            return TextOnlyServer(cfg, check, target)
        from .managed_ollama_backend import OllamaServer

        return OllamaServer(cfg, check, target)

    def _validate_response(self, prompt: str, value: StructuredOutput) -> None:
        from .artifact_checks import structured_problems

        errors = structured_problems(prompt, value.model_dump())
        if errors:
            raise ValueError("; ".join(errors))
