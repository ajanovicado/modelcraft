from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.orchestration.desktop import PowerBIDesktopLauncher


def test_desktop_launcher_unloads_model_before_opening_pbip(tmp_path) -> None:
    pbip = tmp_path / "Demo.pbip"
    pbip.write_text("{}")
    desktop = tmp_path / "PBIDesktop.exe"
    desktop.write_bytes(b"desktop")
    requests: list[tuple[str, dict[str, object]]] = []
    commands: list[tuple[str, ...]] = []
    opened: list[Path] = []
    ps_calls = 0

    def post(url: str, payload: dict[str, object]) -> None:
        requests.append((url, payload))

    def run(argv: tuple[str, ...]):  # type: ignore[no-untyped-def]
        nonlocal ps_calls
        commands.append(argv)
        if argv == ("ollama", "ps"):
            ps_calls += 1
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "NAME ID SIZE PROCESSOR UNTIL\nqwen3.5:4b abc 3GB CPU 4m"
                    if ps_calls == 1
                    else "NAME ID SIZE PROCESSOR UNTIL\n"
                ),
            )
        return SimpleNamespace(returncode=0, stdout="")

    launcher = PowerBIDesktopLauncher(
        pbip,
        model="qwen3.5:4b",
        platform_name="Windows",
        http_post=post,
        run_command=run,
        open_path=opened.append,
        desktop_executable=desktop,
        desktop_running_check=lambda: False,
        desktop_identity_check=lambda executable: executable == desktop,
        sleep=lambda _seconds: None,
    )

    result = launcher()

    assert result["opened"] is True
    assert requests[0][1] == {"model": "qwen3.5:4b", "keep_alive": 0}
    assert ("ollama", "stop", "qwen3.5:4b") in commands
    assert opened == [pbip.resolve()]


def test_desktop_launcher_refuses_to_open_with_any_other_resident_model(
    tmp_path,
) -> None:
    pbip = tmp_path / "Demo.pbip"
    pbip.write_text("{}")
    desktop = tmp_path / "PBIDesktop.exe"
    desktop.write_bytes(b"desktop")
    opened: list[Path] = []

    launcher = PowerBIDesktopLauncher(
        pbip,
        model="qwen3.5:4b",
        platform_name="Windows",
        http_post=lambda *_args: None,
        run_command=lambda _argv: SimpleNamespace(
            returncode=0,
            stdout="NAME ID SIZE PROCESSOR UNTIL\nother-model:7b abc 5GB CPU 4m\n",
        ),
        desktop_executable=desktop,
        open_path=opened.append,
        desktop_running_check=lambda: False,
        sleep=lambda _seconds: None,
    )

    result = launcher()

    assert result["opened"] is False
    assert "other-model:7b" in str(result["reason"])
    assert opened == []


def test_desktop_launcher_fails_closed_off_windows(tmp_path) -> None:
    pbip = tmp_path / "Demo.pbip"
    pbip.write_text("{}")
    launcher = PowerBIDesktopLauncher(pbip, model="qwen3.5:4b", platform_name="Darwin")

    result = launcher()

    assert result["opened"] is False
    assert "Windows" in str(result["reason"])


def test_desktop_launcher_holds_shared_lock_through_process_confirmation(tmp_path) -> None:
    pbip = tmp_path / "Demo.pbip"
    pbip.write_text("{}")
    desktop = tmp_path / "PBIDesktop.exe"
    desktop.write_bytes(b"desktop")
    lock_path = tmp_path / "operation.lock"
    events: list[str] = []
    identity_states = iter((False, True))

    def precheck() -> None:
        events.append("checked")

    def opened(_path: Path) -> None:
        events.append("opened")

    launcher = PowerBIDesktopLauncher(
        pbip,
        model="qwen3.5:4b",
        platform_name="Windows",
        http_post=lambda *_args: events.append("unloaded"),
        run_command=lambda _argv: SimpleNamespace(returncode=0, stdout=""),
        desktop_executable=desktop,
        open_path=opened,
        pre_open_check=precheck,
        operation_lock_path=lock_path,
        desktop_running_check=lambda: False,
        desktop_identity_check=lambda _executable: next(identity_states),
        sleep=lambda _seconds: None,
    )

    result = launcher()

    assert result["opened"] is True
    assert events == ["checked", "unloaded", "opened"]
    assert not (tmp_path / ".reveal-in-progress.json").exists()


def test_desktop_timeout_leaves_durable_marker_that_blocks_writes(tmp_path) -> None:
    pbip = tmp_path / "Demo.pbip"
    pbip.write_text("{}")
    desktop = tmp_path / "PBIDesktop.exe"
    desktop.write_bytes(b"desktop")
    lock_path = tmp_path / "operation.lock"
    launcher = PowerBIDesktopLauncher(
        pbip,
        model="qwen3.5:4b",
        platform_name="Windows",
        http_post=lambda *_args: None,
        run_command=lambda _argv: SimpleNamespace(returncode=0, stdout=""),
        desktop_executable=desktop,
        open_path=lambda _path: None,
        operation_lock_path=lock_path,
        desktop_running_check=lambda: False,
        desktop_identity_check=lambda _executable: False,
        confirmation_timeout_seconds=0,
        sleep=lambda _seconds: None,
    )

    result = launcher()

    assert result["opened"] is False
    assert "writes remain blocked" in str(result["reason"])
    assert (tmp_path / ".reveal-in-progress.json").is_file()


def test_desktop_launcher_uses_exact_attested_executables(tmp_path) -> None:
    pbip = tmp_path / "Demo.pbip"
    pbip.write_text("{}")
    desktop = tmp_path / "PBIDesktop.exe"
    desktop.write_bytes(b"desktop-release")
    ollama = tmp_path / "ollama.exe"
    ollama.write_bytes(b"ollama-release")
    commands: list[tuple[str, ...]] = []
    launches: list[tuple[Path, Path]] = []

    launcher = PowerBIDesktopLauncher(
        pbip,
        model="qwen3.5:4b",
        platform_name="Windows",
        ollama_executable=ollama,
        ollama_executable_hash=sha256_file(ollama),
        desktop_executable=desktop,
        desktop_executable_hash=sha256_file(desktop),
        http_post=lambda *_args: None,
        run_command=lambda argv: (
            commands.append(argv)
            or SimpleNamespace(returncode=0, stdout="NAME ID SIZE PROCESSOR UNTIL\n")
        ),
        launch_desktop=lambda executable, project: launches.append((executable, project)),
        desktop_running_check=lambda: False,
        desktop_identity_check=lambda executable: executable == desktop,
        sleep=lambda _seconds: None,
    )

    result = launcher()

    assert result["opened"] is True
    assert commands == [(str(ollama), "ps"), (str(ollama), "ps")]
    assert launches == [(desktop, pbip)]
    assert result["desktop_executable"] == str(desktop)


def test_desktop_launcher_rejects_attested_executable_drift(tmp_path) -> None:
    pbip = tmp_path / "Demo.pbip"
    pbip.write_text("{}")
    desktop = tmp_path / "PBIDesktop.exe"
    desktop.write_bytes(b"tampered")
    opened: list[Path] = []

    result = PowerBIDesktopLauncher(
        pbip,
        model="qwen3.5:4b",
        platform_name="Windows",
        desktop_executable=desktop,
        desktop_executable_hash="0" * 64,
        http_post=lambda *_args: None,
        run_command=lambda _argv: SimpleNamespace(returncode=0, stdout=""),
        open_path=opened.append,
        desktop_running_check=lambda: False,
        desktop_identity_check=lambda _executable: True,
        sleep=lambda _seconds: None,
    )()

    assert result["opened"] is False
    assert "identity changed" in str(result["reason"])
    assert opened == []
