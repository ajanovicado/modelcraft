from __future__ import annotations

import errno
import os
import subprocess
import sys
import time

import pytest

from ai_powerbi_analyst.studio import __main__ as entrypoint
from ai_powerbi_analyst.studio import state_owner
from ai_powerbi_analyst.studio.state_owner import StateRootOwnedError, own_state_root


def test_state_root_rejects_another_owner_and_allows_independent_roots(tmp_path):
    root = tmp_path / "shared"
    with own_state_root(root) as resolved:
        assert resolved == root.resolve()
        with (
            pytest.raises(StateRootOwnedError, match="already using the state folder"),
            own_state_root(root / ".." / "shared"),
        ):
            pytest.fail("a second owner must not enter")
        with own_state_root(tmp_path / "independent"):
            pass
    with own_state_root(root):
        pass


def test_state_root_releases_after_startup_or_server_failure(tmp_path):
    with pytest.raises(ValueError, match="startup failed"), own_state_root(tmp_path):
        raise ValueError("startup failed")
    with own_state_root(tmp_path):
        pass
    # A persistent marker is not a stale ownership claim.
    assert (tmp_path / "studio-engine.lock").is_file()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_failed_acquisition_closes_descriptor_and_preserves_non_contention_errors(
    tmp_path, monkeypatch, error_number
):
    descriptors = []

    def fail(descriptor):
        assert not os.get_inheritable(descriptor)
        descriptors.append(descriptor)
        raise OSError(error_number, "native lock failed")

    monkeypatch.setattr(state_owner, "_lock_descriptor", fail)
    expected = StateRootOwnedError if error_number == errno.EACCES else OSError
    with pytest.raises(expected), own_state_root(tmp_path):
        pytest.fail("a failed lock must never admit an owner")
    assert len(descriptors) == 1
    with pytest.raises(OSError) as error:
        os.fstat(descriptors[0])
    assert error.value.errno == errno.EBADF


@pytest.mark.parametrize("exit_mode", ["normal", "abrupt", "killed"])
def test_process_exit_releases_native_state_ownership(tmp_path, exit_mode):
    root = tmp_path / "state"
    ready = tmp_path / "ready"
    finish = tmp_path / "finish"
    code = """
import os
import sys
import time
from pathlib import Path
from ai_powerbi_analyst.studio.state_owner import own_state_root

with own_state_root(sys.argv[1]):
    Path(sys.argv[2]).write_text("ready", encoding="utf-8")
    while not Path(sys.argv[3]).exists():
        time.sleep(0.02)
    if sys.argv[4] == "abrupt":
        os._exit(0)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(root), str(ready), str(finish), exit_mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child engine did not acquire ownership"
        with pytest.raises(StateRootOwnedError), own_state_root(root):
            pytest.fail("parent must not share a live child's state root")
        if exit_mode == "killed":
            process.kill()
        else:
            finish.write_text("finish", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
        if exit_mode != "killed":
            assert process.returncode == 0, (stdout, stderr)
        with own_state_root(root):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_entrypoint_rejects_second_owner_before_service_initialization(
    tmp_path, monkeypatch, capsys
):
    token = "private-test-token"
    monkeypatch.setattr(sys, "argv", ["studio", "--token", token, "--state-root", str(tmp_path)])
    monkeypatch.setattr(
        entrypoint, "StudioService", lambda *args: pytest.fail("must not open state or model")
    )
    with own_state_root(tmp_path), pytest.raises(SystemExit) as error:
        entrypoint.main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "Close the existing app or engine" in output.err
    assert token not in output.err


@pytest.mark.parametrize("fail_server", [False, True])
def test_entrypoint_holds_ownership_through_server_shutdown(tmp_path, monkeypatch, fail_server):
    monkeypatch.setattr(
        sys, "argv", ["studio", "--token", "test-token", "--state-root", str(tmp_path)]
    )
    initialized = []

    def initialize_service(repository, state_root):
        assert state_root == tmp_path.resolve()
        with pytest.raises(StateRootOwnedError), own_state_root(state_root):
            pytest.fail("ownership must precede service initialization")
        initialized.append(state_root)
        return object()

    class Server:
        def __init__(self, config):
            pass

        def run(self):
            with pytest.raises(StateRootOwnedError), own_state_root(tmp_path):
                pytest.fail("ownership must cover the entire server lifetime")
            if fail_server:
                raise ValueError("server failed")

    monkeypatch.setattr(entrypoint, "StudioService", initialize_service)
    monkeypatch.setattr(entrypoint, "create_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(entrypoint.uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(entrypoint.uvicorn, "Server", Server)
    if fail_server:
        with pytest.raises(ValueError, match="server failed"):
            entrypoint.main()
    else:
        entrypoint.main()
    assert initialized == [tmp_path.resolve()]
    with own_state_root(tmp_path):
        pass
