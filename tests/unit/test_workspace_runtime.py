"""Fixture assembly is hermetic; real services retain native/model discovery."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from ai_powerbi_analyst.llm.runtime import LocalModelError, StructuredModelRuntime
from ai_powerbi_analyst.workspace import runtime
from ai_powerbi_analyst.workspace.planner import default_visual_catalog

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_mock_services_never_discover_native_catalog_or_create_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Fixture services attempted native/model discovery")

    monkeypatch.setattr(runtime, "create_model_runtime", forbidden)
    monkeypatch.setattr(runtime.VisualCatalogService, "load", forbidden)
    services = runtime.build_user_workspace_services(PROJECT_ROOT, tmp_path, mode="mock")

    assert services.planner.visual_catalog == default_visual_catalog()
    assert not services.model.health().model_available
    assert not services.model.health().service_available
    with pytest.raises(LocalModelError, match="disabled in fixture mode"):
        services.model.stream_chat("Do not run a model", system_prompt="Fixture only")
    services.model.unload()
    assert not (tmp_path / "visual-catalog.json").exists()


def test_default_real_services_preserve_model_and_catalog_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = cast(StructuredModelRuntime, object())
    catalog = default_visual_catalog()[:1]
    calls: list[str] = []

    def create_model(*_args: object, **_kwargs: object) -> StructuredModelRuntime:
        calls.append("model")
        return model

    def load_catalog(_self: runtime.VisualCatalogService) -> object:
        calls.append("catalog")
        return catalog

    monkeypatch.setattr(runtime, "create_model_runtime", create_model)
    monkeypatch.setattr(runtime.VisualCatalogService, "load", load_catalog)
    services = runtime.build_user_workspace_services(PROJECT_ROOT, tmp_path)

    assert calls == ["model", "catalog"]
    assert services.model is model
    assert services.planner.visual_catalog == catalog
