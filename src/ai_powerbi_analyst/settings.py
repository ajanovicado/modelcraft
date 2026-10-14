"""Strict settings for the locked client-demo profile."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import Field, field_validator, model_validator

from ai_powerbi_analyst.contracts import ClosedModel


def _default_interval_levels() -> list[Literal[80, 95]]:
    return [80, 95]


class ModelSettings(ClosedModel):
    provider: Literal["ollama"] = "ollama"
    runtime_config_path: str | None = None
    name: str = "qwen3.5:4b-q4_K_M"
    candidates: list[str] = Field(
        default_factory=lambda: ["qwen3.5:4b-q4_K_M"],
        min_length=1,
    )
    selected_digest: str | None = (
        "sha256:2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd"
    )
    endpoint: str = "http://127.0.0.1:11434"
    temperature: float = Field(default=0, ge=0, le=1)
    max_output_tokens: int = Field(default=512, ge=64, le=1024)
    chat_output_tokens: int = Field(default=384, ge=64, le=512)
    context_tokens: int = Field(default=4096, ge=2048, le=8192)
    request_timeout_seconds: int = Field(default=60, ge=1, le=90)
    stream_timeout_seconds: int = Field(default=300, ge=60, le=600)
    max_retries: int = Field(default=1, ge=0, le=1)
    thinking_enabled: bool = False

    @field_validator("endpoint")
    @classmethod
    def ollama_endpoint_is_loopback(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("offline profile requires a loopback Ollama endpoint")
        return value.rstrip("/")


class AnalyticsSettings(ClosedModel):
    read_only: bool = True
    external_access: bool = False
    extension_autoload: bool = False
    extension_install: bool = False
    threads: int = Field(default=2, ge=1, le=2)
    memory_limit: str = Field(default="1GB", pattern=r"^1GB$")
    timeout_seconds: int = Field(default=15, ge=1, le=18)
    max_rows: int = Field(default=200, ge=1, le=200)


class PowerBISettings(ClosedModel):
    mode: Literal["auto", "mock", "real"] = "auto"
    writer_version: str = "3.11.1"
    microsoft_validator_version: str = "0.1.4"
    desktop_bridge_version: str = "0.1.2"
    require_desktop_closed: bool = True
    validation_warning_policy: Literal["fail"] = "fail"
    style_preset: Literal["executive_light"] = "executive_light"
    evidence_target_page: Literal["margin_drivers"] = "margin_drivers"
    evidence_slot_key: Literal["conversational_evidence"] = "conversational_evidence"


class ForecastSettings(ClosedModel):
    horizon_months: int = Field(default=6, ge=1, le=12)
    rolling_origin_folds: int = Field(default=3, ge=3, le=6)
    fold_step_months: int = Field(default=3, ge=1, le=6)
    interval_levels: list[Literal[80, 95]] = Field(default_factory=_default_interval_levels)
    revenue_wape_ceiling: float = Field(default=0.12, gt=0, le=0.25)
    gross_profit_wape_ceiling: float = Field(default=0.15, gt=0, le=0.25)


class ScenarioSettings(ClosedModel):
    minimum_price_change_pct: int = Field(default=-3, ge=-10, le=0)
    maximum_price_change_pct: int = Field(default=5, ge=0, le=10)
    step_pct: int = Field(default=1, ge=1, le=5)
    approved_price_change_pct: int = 3
    price_elasticity: float = Field(default=-0.8, ge=-3, le=0)

    @model_validator(mode="after")
    def approved_value_is_on_grid(self) -> ScenarioSettings:
        in_range = (
            self.minimum_price_change_pct
            <= self.approved_price_change_pct
            <= self.maximum_price_change_pct
        )
        if not in_range:
            raise ValueError("approved scenario must be inside the configured range")
        offset = self.approved_price_change_pct - self.minimum_price_change_pct
        if offset % self.step_pct:
            raise ValueError("approved scenario must land on the configured grid")
        return self


class AppSettings(ClosedModel):
    profile: Literal["client_demo"] = "client_demo"
    offline_only: Literal[True] = True
    model: ModelSettings = Field(default_factory=ModelSettings)
    analytics: AnalyticsSettings = Field(default_factory=AnalyticsSettings)
    powerbi: PowerBISettings = Field(default_factory=PowerBISettings)
    forecast: ForecastSettings = Field(default_factory=ForecastSettings)
    scenario: ScenarioSettings = Field(default_factory=ScenarioSettings)


def load_settings(path: Path) -> AppSettings:
    """Load and validate one YAML configuration without implicit overrides."""

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("settings file must contain a YAML mapping")
    return AppSettings.model_validate(payload)
