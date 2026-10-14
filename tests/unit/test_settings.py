from __future__ import annotations

from pathlib import Path

from ai_powerbi_analyst.settings import AppSettings, ModelSettings, load_settings

PROJECT_ROOT = Path(__file__).parents[2]


def test_recommended_defaults_are_locked_and_safe() -> None:
    settings = load_settings(PROJECT_ROOT / "config" / "defaults.yaml")

    assert settings.profile == "client_demo"
    assert settings.offline_only
    assert settings.model.provider == "ollama"
    assert settings.model.runtime_config_path == "runtime/catalog.json"
    assert settings.model.name == "qwen3.5:4b-q4_K_M"
    assert settings.model.selected_digest == (
        "sha256:2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd"
    )
    # Qwen 4B is the only local model, and no memory setting gates it.
    assert settings.model.candidates == ["qwen3.5:4b-q4_K_M"]
    assert "minimum_memory_headroom_gb" not in settings.model.model_dump()
    assert settings.model.temperature == 0
    assert settings.model.max_output_tokens <= 1024
    assert settings.model.chat_output_tokens == 512
    assert settings.model.stream_timeout_seconds == 300
    assert settings.analytics.read_only
    assert not settings.analytics.external_access
    assert settings.analytics.threads == 2
    assert settings.analytics.memory_limit == "1GB"
    assert settings.analytics.max_rows == 200
    assert settings.powerbi.require_desktop_closed
    assert settings.powerbi.writer_version == "3.11.1"
    assert settings.scenario.approved_price_change_pct == 3
    assert settings.scenario.price_elasticity == -0.8


def test_model_defaults_name_only_qwen_4b() -> None:
    model = ModelSettings()

    assert model.name == "qwen3.5:4b-q4_K_M"
    assert model.candidates == ["qwen3.5:4b-q4_K_M"]


def test_settings_contract_rejects_unknown_keys() -> None:
    payload = {
        "profile": "client_demo",
        "offline_only": True,
        "model": {
            "name": "qwen3.5:4b",
            "endpoint": "http://127.0.0.1:11434",
            "temperature": 0,
            "max_output_tokens": 512,
            "context_tokens": 4096,
        },
        "analytics": {},
        "powerbi": {},
        "forecast": {},
        "scenario": {},
        "surprise": "not accepted",
    }

    try:
        AppSettings.model_validate(payload)
    except ValueError as error:
        assert "surprise" in str(error)
    else:
        raise AssertionError("unknown settings key was accepted")
