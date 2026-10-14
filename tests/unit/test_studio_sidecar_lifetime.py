from __future__ import annotations

import argparse
import io
import os
import socket
import sys
import threading
from types import SimpleNamespace

import pytest
import uvicorn

from ai_powerbi_analyst.studio import __main__ as entrypoint
from ai_powerbi_analyst.studio import parent_lifetime
from ai_powerbi_analyst.studio.state_owner import own_state_root


@pytest.mark.parametrize("enabled", [False, True])
def test_stdin_lifetime_cli_is_opt_in(tmp_path, monkeypatch, enabled):
    arguments = ["studio", "--token", "test-token", "--state-root", str(tmp_path)]
    if enabled:
        arguments.append("--parent-stdin-lifetime")
    monkeypatch.setattr(sys, "argv", arguments)
    captured = []
    monkeypatch.setattr(entrypoint, "_serve", lambda args, *rest: captured.append(args))
    entrypoint.main()
    assert len(captured) == 1
    assert captured[0].parent_stdin_lifetime is enabled


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_stdin_watcher_is_independent_of_existing_frozen_parent_watch(
    tmp_path, monkeypatch, frozen, enabled
):
    calls = []

    class Server:
        should_exit = False

        def __init__(self, config):
            pass

        def run(self):
            calls.append(("run", self.should_exit))

    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(entrypoint, "StudioService", lambda *args: object())
    monkeypatch.setattr(entrypoint, "create_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(entrypoint.uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(entrypoint.uvicorn, "Server", Server)
    monkeypatch.setattr(
        parent_lifetime, "watch_parent_exit", lambda stop: calls.append(("parent", stop))
    )
    entrypoint._serve(_arguments(tmp_path, enabled), "test-token", False, tmp_path)
    assert [name for name, _ in calls] == (["parent", "run"] if frozen else ["run"])
    assert calls[-1] == ("run", enabled)
    if frozen:
        # Both lifetime channels use graceful shutdown; the frozen watch remains active.
        calls[0][1]()


def _arguments(state_root, enabled=True):
    return argparse.Namespace(
        repository=state_root,
        host="127.0.0.1",
        port=0,
        emit_token=False,
        dev_browser_origin=None,
        parent_stdin_lifetime=enabled,
    )


@pytest.mark.parametrize("channel", ["missing", "closed", "eof", "during-startup"])
def test_real_server_stdin_shutdown_runs_lifespan_and_releases_listener_and_state(
    tmp_path, monkeypatch, channel
):
    calls, servers, watchers, errors = [], [], [], []
    started, finished = threading.Event(), threading.Event()
    pipe_writer = None
    if channel == "missing":
        stream = None
    elif channel == "closed":
        stream = io.BytesIO()
        stream.close()
    elif channel == "eof":
        stream = io.BytesIO()
    else:
        reader_fd, writer_fd = os.pipe()
        stream = os.fdopen(reader_fd, "rb", buffering=0)
        pipe_writer = os.fdopen(writer_fd, "wb", buffering=0)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stream))
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    class Editing:
        def start_recovery(self):
            calls.append("start")
            started.set()
            if pipe_writer is not None:
                pipe_writer.close()

        def close(self):
            calls.append("editing-close")

    service = SimpleNamespace(
        editing=Editing(), model=SimpleNamespace(unload=lambda: calls.append("model-unload"))
    )
    monkeypatch.setattr(entrypoint, "StudioService", lambda *args: service)
    real_server, real_config = uvicorn.Server, uvicorn.Config
    real_watch = parent_lifetime.watch_stdin_eof

    def capture_server(config):
        server = real_server(config)
        servers.append(server)
        return server

    def capture_config(*args, **kwargs):
        return real_config(*args, **kwargs, http="h11", ws="none", loop="asyncio", lifespan="on")

    def capture_watch(stop):
        watcher = real_watch(stop)
        if watcher is not None:
            watchers.append(watcher)
        return watcher

    monkeypatch.setattr(entrypoint.uvicorn, "Server", capture_server)
    monkeypatch.setattr(entrypoint.uvicorn, "Config", capture_config)
    monkeypatch.setattr(parent_lifetime, "watch_stdin_eof", capture_watch)

    def run():
        try:
            with own_state_root(tmp_path):
                entrypoint._serve(_arguments(tmp_path), "0123456789abcdef", False, tmp_path)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert started.wait(timeout=5), "the real ASGI lifespan did not start"
        assert finished.wait(timeout=5), "stdin loss did not stop the API"
    finally:
        if pipe_writer is not None:
            pipe_writer.close()
        for server in servers:
            server.should_exit = True
        worker.join(timeout=5)
        for watcher in watchers:
            watcher.join(timeout=2)
            assert not watcher.is_alive()
        if stream is not None:
            stream.close()
    assert not worker.is_alive()
    assert errors == []
    assert calls == ["start", "editing-close", "model-unload"]
    assert len(servers) == 1
    assert servers[0].started
    assert servers[0].should_exit
    assert all(not listener.is_serving() for listener in servers[0].servers)
    with own_state_root(tmp_path):
        pass


def test_real_server_bind_failure_still_tears_down_owned_resources(tmp_path, monkeypatch):
    calls = []
    service = SimpleNamespace(
        editing=SimpleNamespace(
            start_recovery=lambda: calls.append("start"),
            close=lambda: calls.append("editing-close"),
        ),
        model=SimpleNamespace(unload=lambda: calls.append("model-unload")),
    )
    monkeypatch.setattr(entrypoint, "StudioService", lambda *args: service)
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    real_config = uvicorn.Config
    monkeypatch.setattr(
        entrypoint.uvicorn,
        "Config",
        lambda *args, **kwargs: real_config(
            *args, **kwargs, http="h11", ws="none", loop="asyncio", lifespan="on"
        ),
    )
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        arguments = _arguments(tmp_path)
        arguments.port = blocker.getsockname()[1]
        with pytest.raises(SystemExit) as error, own_state_root(tmp_path):
            entrypoint._serve(arguments, "0123456789abcdef", False, tmp_path)
        assert error.value.code == 3
    assert calls == ["start", "editing-close", "model-unload"]
    with own_state_root(tmp_path):
        pass
