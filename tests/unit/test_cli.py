from __future__ import annotations

import pytest

from ai_powerbi_analyst import cli
from ai_powerbi_analyst.cli import build_parser
from ai_powerbi_analyst.preflight import Readiness


def test_run_command_uses_recommended_safe_defaults() -> None:
    args = build_parser().parse_args(["run"])

    assert args.command == "run"
    assert args.powerbi_mode == "auto"
    assert args.port == 8501
    assert not args.scripted


def test_powerbi_mode_is_closed_choice() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--powerbi-mode", "anything"])


def test_prepare_demo_default_is_generated_fixture_area() -> None:
    args = build_parser().parse_args(["prepare-demo"])

    assert args.output.as_posix() == "fixtures/generated/data"


def test_model_attestation_default_is_runtime_evidence() -> None:
    args = build_parser().parse_args(["attest-model"])

    assert args.output.as_posix() == ".runtime/model-attestation.json"


def test_run_command_selects_fixture_backed_mock_ui_when_real_gate_is_closed(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, *, check, env):  # type: ignore[no-untyped-def]
        captured["argv"] = argv
        captured["check"] = check
        captured["env"] = env
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(cli, "collect_environment", lambda: object())
    monkeypatch.setattr(
        cli,
        "evaluate_readiness",
        lambda _environment: Readiness(
            can_run_demo=True,
            can_write_real_powerbi=False,
            blockers=["Real Power BI writes require Windows"],
        ),
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli._run_ui(mode="auto", port=8501, no_browser=True, scripted=True)

    assert result == 0
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["AI_POWERBI_UI_MODE"] == "mock"
    assert environment["AI_POWERBI_SCRIPTED"] == "1"


def test_run_ui_treats_parent_keyboard_interrupt_as_clean_shell_interrupt(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "collect_environment", lambda: object())
    monkeypatch.setattr(
        cli,
        "evaluate_readiness",
        lambda _environment: Readiness(
            can_run_demo=True,
            can_write_real_powerbi=False,
            blockers=["Real Power BI writes require Windows"],
        ),
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    result = cli.main(["run", "--scripted", "--no-browser"])

    assert result == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_run_ui_preserves_streamlit_child_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(cli, "collect_environment", lambda: object())
    monkeypatch.setattr(
        cli,
        "evaluate_readiness",
        lambda _environment: Readiness(
            can_run_demo=True,
            can_write_real_powerbi=False,
            blockers=["Real Power BI writes require Windows"],
        ),
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 23})(),
    )

    result = cli._run_ui(mode="auto", port=8501, no_browser=True, scripted=True)

    assert result == 23


def test_auto_mode_falls_back_to_mock_when_full_real_workspace_gate_fails(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "collect_environment", lambda: object())
    monkeypatch.setattr(
        cli,
        "evaluate_readiness",
        lambda _environment: Readiness(
            can_run_demo=True,
            can_write_real_powerbi=True,
            blockers=[],
        ),
    )
    monkeypatch.setattr(
        cli,
        "_complete_real_workspace_preflight",
        lambda _environment: (False, "Phase-0 attestation verification failed"),
    )

    def fake_run(argv, *, check, env):  # type: ignore[no-untyped-def]
        captured["env"] = env
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli._run_ui(mode="auto", port=8501, no_browser=True, scripted=True)

    assert result == 0
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["AI_POWERBI_UI_MODE"] == "mock"


def test_explicit_real_mode_fails_before_streamlit_when_full_gate_fails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "collect_environment", lambda: object())
    monkeypatch.setattr(
        cli,
        "evaluate_readiness",
        lambda _environment: Readiness(
            can_run_demo=True,
            can_write_real_powerbi=True,
            blockers=[],
        ),
    )
    monkeypatch.setattr(
        cli,
        "_complete_real_workspace_preflight",
        lambda _environment: (False, "Phase-0 attestation verification failed"),
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Streamlit must not start")),
    )

    with pytest.raises(RuntimeError, match="Phase-0"):
        cli._run_ui(mode="real", port=8501, no_browser=True, scripted=True)
