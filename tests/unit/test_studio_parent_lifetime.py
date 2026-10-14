import io
import os
import sys
import threading
from types import SimpleNamespace

import psutil
import pytest

from ai_powerbi_analyst.studio.parent_lifetime import watch_parent_exit, watch_stdin_eof


def test_parent_exit_requests_server_shutdown(monkeypatch):
    exited, stopped = threading.Event(), threading.Event()

    class Parent:
        def wait(self):
            assert exited.wait(timeout=2)

    monkeypatch.setattr(psutil, "Process", lambda pid: Parent())
    watcher = watch_parent_exit(stopped.set)
    assert not stopped.is_set()
    exited.set()
    watcher.join(timeout=2)
    assert stopped.is_set()
    assert not watcher.is_alive()


def test_parent_missing_at_startup_requests_shutdown(monkeypatch):
    stopped = threading.Event()

    def missing(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", missing)
    assert watch_parent_exit(stopped.set) is None
    assert stopped.is_set()


def test_stdin_pipe_stays_live_after_input_and_stops_once_on_eof():
    reader_fd, writer_fd = os.pipe()
    consumed, stopped = threading.Event(), threading.Event()
    calls = []
    with (
        os.fdopen(reader_fd, "rb", buffering=0) as reader,
        os.fdopen(writer_fd, "wb", buffering=0) as writer,
    ):

        class Stream:
            closed = False

            def read(self, size):
                chunk = reader.read(size)
                if chunk:
                    consumed.set()
                return chunk

        def stop():
            calls.append("stop")
            stopped.set()

        watcher = watch_stdin_eof(stop, Stream())
        try:
            assert watcher.daemon
            writer.write(b"benign input\x00\xff")
            assert consumed.wait(timeout=2)
            assert not stopped.is_set()
        finally:
            writer.close()
            watcher.join(timeout=2)
        assert not watcher.is_alive()
        assert stopped.is_set()
        assert calls == ["stop"]
        assert not reader.closed, "the watcher does not own the stdin handle"


def test_stdin_eof_before_watcher_start_requests_shutdown():
    reader_fd, writer_fd = os.pipe()
    os.close(writer_fd)
    stopped = threading.Event()
    with os.fdopen(reader_fd, "rb") as reader:
        watcher = watch_stdin_eof(stopped.set, reader)
        watcher.join(timeout=2)
        assert not watcher.is_alive()
        assert stopped.is_set()


@pytest.mark.parametrize("stdin", [None, SimpleNamespace(), SimpleNamespace(buffer=None)])
def test_missing_native_stdin_requests_shutdown_immediately(monkeypatch, stdin):
    calls = []
    monkeypatch.setattr(sys, "stdin", stdin)
    assert watch_stdin_eof(lambda: calls.append("stop")) is None
    assert calls == ["stop"]


def test_closed_native_stdin_requests_shutdown_immediately():
    calls = []
    stream = io.BytesIO()
    stream.close()
    assert watch_stdin_eof(lambda: calls.append("stop"), stream) is None
    assert calls == ["stop"]


@pytest.mark.parametrize("error", [OSError("broken pipe"), ValueError("closed stream")])
def test_native_stdin_read_failure_requests_shutdown(error):
    calls = []

    class BrokenStream:
        closed = False

        def read(self, size):
            raise error

    watcher = watch_stdin_eof(lambda: calls.append("stop"), BrokenStream())
    watcher.join(timeout=2)
    assert not watcher.is_alive()
    assert calls == ["stop"]
