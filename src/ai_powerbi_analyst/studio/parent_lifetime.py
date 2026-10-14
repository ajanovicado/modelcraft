"""Let the packaged child shut down when its native host or launcher exits."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from typing import BinaryIO

import psutil


def watch_parent_exit(stop_server: Callable[[], None]) -> threading.Thread | None:
    """Monitor the original parent identity, including exit before startup."""
    try:
        parent = psutil.Process(os.getppid())
    except psutil.NoSuchProcess:
        stop_server()
        return None

    def wait() -> None:
        try:
            parent.wait()
        finally:
            stop_server()

    thread = threading.Thread(target=wait, name="studio-parent-lifetime", daemon=True)
    thread.start()
    return thread


def watch_stdin_eof(
    stop_server: Callable[[], None], stream: BinaryIO | None = None
) -> threading.Thread | None:
    """Watch the opt-in native parent's pipe without owning other processes."""
    if stream is None:
        stream = getattr(sys.stdin, "buffer", None)
    if stream is None or stream.closed:
        stop_server()
        return None

    def wait() -> None:
        try:
            while stream.read(4096):
                pass
        except (OSError, ValueError):
            # A broken or closed channel means the native host is unavailable.
            pass
        finally:
            stop_server()

    thread = threading.Thread(target=wait, name="studio-stdin-lifetime", daemon=True)
    thread.start()
    return thread
