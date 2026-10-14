from __future__ import annotations

import json
from pathlib import Path

from ai_powerbi_analyst.preflight import EnvironmentReport, ToolProbe
from ai_powerbi_analyst.ui.launch_checks import (
    evaluate_launch_gate,
    installed_ollama_inventory,
)

PROJECT_ROOT = Path(__file__).parents[2]


def _environment(
    *, operating_system: str = "Windows", total: float = 16, available: float = 8
) -> EnvironmentReport:
    return EnvironmentReport(
        operating_system=operating_system,
        python_version="3.12.10",
        total_memory_gb=total,
        available_memory_gb=available,
        desktop_running=False,
        tools={
            "pbi": ToolProbe(name="pbi", available=True, version="3.11.1"),
            "powerbi-report-author": ToolProbe(
                name="powerbi-report-author", available=True, version="0.1.4"
            ),
            "powerbi-desktop-bridge": ToolProbe(
                name="powerbi-desktop", available=True, version="0.1.2"
            ),
            "ollama": ToolProbe(name="ollama", available=True, version="0.12.0"),
            "node": ToolProbe(name="node", available=True, version="24.0.0"),
            "powerbi-desktop": ToolProbe(
                name="powerbi-desktop", available=True, version="2.146.705.0"
            ),
        },
    )


def test_real_launch_requires_exact_local_model_and_windows_tooling() -> None:
    gate = evaluate_launch_gate(
        _environment(),
        required_model="qwen3.5:4b",
        installed_models={"qwen3.5:4b"},
        real_mode=True,
        required_desktop_version="2.146.705.0",
    )

    assert gate.ready
    assert gate.blockers == ()


def test_low_memory_never_blocks_the_real_launch() -> None:
    gate = evaluate_launch_gate(
        _environment(total=4, available=0.25),
        required_model="qwen3.5:4b",
        installed_models={"qwen3.5:4b"},
        real_mode=True,
        required_desktop_version="2.146.705.0",
    )

    assert gate.ready
    assert gate.blockers == ()


def test_real_launch_fails_closed_when_required_model_is_missing() -> None:
    gate = evaluate_launch_gate(
        _environment(),
        required_model="qwen3.5:4b",
        installed_models={"phi4-mini:latest"},
        real_mode=True,
        required_desktop_version="2.146.705.0",
    )

    assert not gate.ready
    assert "qwen3.5:4b" in gate.blockers[0]


def test_real_launch_enforces_frozen_model_digest_when_configured() -> None:
    gate = evaluate_launch_gate(
        _environment(),
        required_model="qwen3.5:4b",
        installed_models={"qwen3.5:4b"},
        installed_digests={"qwen3.5:4b": "wrong"},
        selected_digest="sha256:approved",
        real_mode=True,
        required_desktop_version="2.146.705.0",
    )

    assert not gate.ready
    assert "digest mismatch" in " ".join(gate.blockers)


def test_ollama_inventory_uses_full_loopback_api_digest_not_short_cli_id(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    full_digest = "sha256:" + "a" * 64

    class Response:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        @staticmethod
        def read(_limit: int) -> bytes:
            return json.dumps(
                {
                    "models": [
                        {
                            "name": "qwen3.5:4b",
                            "digest": full_digest,
                            "short_id": "a1b2c3d4e5f6",
                        }
                    ]
                }
            ).encode()

    monkeypatch.setattr(
        "ai_powerbi_analyst.ui.launch_checks.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )

    assert installed_ollama_inventory() == {"qwen3.5:4b": full_digest}


def test_ollama_inventory_never_promotes_a_short_display_id_to_a_digest(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    payload = json.dumps({"models": [{"name": "qwen3.5:4b", "digest": "a1b2c3d4e5f6"}]}).encode()

    class Response:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        @staticmethod
        def read(_limit: int) -> bytes:
            return payload

    monkeypatch.setattr(
        "ai_powerbi_analyst.ui.launch_checks.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )

    assert installed_ollama_inventory() == {"qwen3.5:4b": ""}


def test_real_launch_rejects_desktop_version_drift() -> None:
    gate = evaluate_launch_gate(
        _environment(),
        required_model="qwen3.5:4b",
        installed_models={"qwen3.5:4b"},
        real_mode=True,
        required_desktop_version="2.147.0.0",
    )

    assert not gate.ready
    assert "Power BI Desktop version 2.147.0.0 is required" in gate.blockers


def test_mock_mode_can_render_without_powerbi_writer() -> None:
    gate = evaluate_launch_gate(
        _environment(operating_system="Darwin"),
        required_model="qwen3.5:4b",
        installed_models=set(),
        real_mode=False,
    )

    assert gate.ready


def test_windows_scripts_are_strict_local_and_never_pull_models() -> None:
    launcher = (PROJECT_ROOT / "scripts" / "launch-demo.ps1").read_text(encoding="utf-8")
    reset = (PROJECT_ROOT / "scripts" / "reset-demo.ps1").read_text(encoding="utf-8")

    assert "Set-StrictMode" in launcher
    assert "AI_POWERBI_UI_MODE" in launcher
    assert "127.0.0.1" in launcher
    assert "ollama pull" not in launcher.casefold()
    assert "ollama serve" not in launcher.casefold()
    assert '@("serve")' in launcher
    assert "api/generate" in launcher
    assert "keep_alive" in launcher
    assert "--require-attested" in launcher
    assert "ui.production_runtime --preflight" in launcher
    assert "Complete application preflight failed before browser launch" in launcher
    assert "$AppReady" in launcher
    assert "--browser.gatherUsageStats" in launcher
    assert '"false"' in launcher
    assert "runtime.preparation reset" in reset
    assert "Remove-Item -Recurse" not in reset
