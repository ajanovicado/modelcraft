from __future__ import annotations

from ai_powerbi_analyst.preflight import EnvironmentReport, ToolProbe, evaluate_readiness


def test_readiness_blocks_real_powerbi_writes_off_windows() -> None:
    report = EnvironmentReport(
        operating_system="Darwin",
        python_version="3.12.10",
        total_memory_gb=16.0,
        available_memory_gb=8.0,
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

    readiness = evaluate_readiness(report)

    assert readiness.can_run_demo
    assert not readiness.can_write_real_powerbi
    assert "Windows" in " ".join(readiness.blockers)


def test_readiness_blocks_writes_when_desktop_is_open() -> None:
    report = EnvironmentReport(
        operating_system="Windows",
        python_version="3.12.10",
        total_memory_gb=16.0,
        available_memory_gb=7.0,
        desktop_running=True,
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

    readiness = evaluate_readiness(report)

    assert readiness.can_run_demo
    assert not readiness.can_write_real_powerbi
    assert "Close Power BI Desktop" in readiness.blockers


def test_readiness_allows_windows_with_tools_and_memory() -> None:
    report = EnvironmentReport(
        operating_system="Windows",
        python_version="3.12.10",
        total_memory_gb=16.0,
        available_memory_gb=7.0,
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

    readiness = evaluate_readiness(report)

    assert readiness.can_run_demo
    assert readiness.can_write_real_powerbi
    assert not readiness.blockers


def test_low_memory_is_reported_but_never_blocks() -> None:
    report = EnvironmentReport(
        operating_system="Windows",
        python_version="3.12.10",
        total_memory_gb=4.0,
        available_memory_gb=0.25,
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

    readiness = evaluate_readiness(report)

    assert readiness.can_run_demo
    assert readiness.can_write_real_powerbi
    assert not readiness.blockers


def test_missing_ollama_blocks_demo_but_not_static_inspection() -> None:
    report = EnvironmentReport(
        operating_system="Windows",
        python_version="3.12.10",
        total_memory_gb=8.0,
        available_memory_gb=1.5,
        desktop_running=False,
        tools={
            "pbi": ToolProbe(name="pbi", available=True, version="3.11.1"),
            "powerbi-report-author": ToolProbe(
                name="powerbi-report-author", available=True, version="0.1.4"
            ),
            "ollama": ToolProbe(name="ollama", available=False),
            "node": ToolProbe(name="node", available=True, version="24.0.0"),
            "powerbi-desktop": ToolProbe(
                name="powerbi-desktop", available=True, version="2.146.705.0"
            ),
        },
    )

    readiness = evaluate_readiness(report)

    assert not readiness.can_run_demo
    assert readiness.can_inspect_project
    assert "Ollama is not available" in readiness.blockers


def test_wrong_pinned_writer_version_blocks_real_mode() -> None:
    report = EnvironmentReport(
        operating_system="Windows",
        python_version="3.12.10",
        total_memory_gb=16.0,
        available_memory_gb=7.0,
        desktop_running=False,
        tools={
            "pbi": ToolProbe(name="pbi", available=True, version="pbi-cli 3.12.0"),
            "powerbi-report-author": ToolProbe(
                name="powerbi-report-author", available=True, version="0.1.4"
            ),
            "ollama": ToolProbe(name="ollama", available=True, version="0.12.0"),
            "node": ToolProbe(name="node", available=True, version="24.0.0"),
            "powerbi-desktop": ToolProbe(
                name="powerbi-desktop", available=True, version="2.146.705.0"
            ),
        },
    )

    readiness = evaluate_readiness(report)

    assert readiness.can_run_demo
    assert not readiness.can_write_real_powerbi
    assert "pbi-cli version 3.11.1 is required" in readiness.blockers


def test_pbi_distribution_version_overrides_stale_embedded_banner() -> None:
    report = EnvironmentReport(
        operating_system="Windows",
        python_version="3.12.10",
        total_memory_gb=16.0,
        available_memory_gb=7.0,
        desktop_running=False,
        tools={
            "pbi": ToolProbe(
                name="pbi",
                available=True,
                version="pbi-cli, version 3.10.10",
                distribution_version="3.11.1",
            ),
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

    readiness = evaluate_readiness(report)

    assert readiness.can_write_real_powerbi
    assert not readiness.blockers


def test_uv_tool_version_lookup_disables_forced_color(monkeypatch) -> None:
    import subprocess

    from ai_powerbi_analyst import preflight

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="pbi-cli-tool v3.11.1\n- pbi\n")

    monkeypatch.setattr(preflight.shutil, "which", lambda _: "uv")
    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    assert preflight._uv_tool_distribution_version("pbi-cli-tool") == "3.11.1"
    assert calls == [["uv", "tool", "list", "--color", "never"]]
