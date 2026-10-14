import socket
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from .windows_job import MemoryJob

def sha(path: str | Path) -> str: ...

class GuardedServer:
    config: dict[str, Any]
    check: Callable[..., dict[str, Any]]
    evidence: Path
    process: subprocess.Popen[bytes] | None
    stopped: threading.Event
    failure: BaseException | None
    deadline: float | None
    log: TextIO | None
    job: MemoryJob | None
    response_socket: socket.socket | None
    timed_out: bool
    endpoint: str
    watcher: threading.Thread
    def __init__(
        self, config: dict[str, Any], check: Callable[..., dict[str, Any]], evidence: str | Path
    ) -> None: ...
    def _watch(self) -> None: ...
    def request(
        self, path: str, data: dict[str, Any] | None = ..., timeout: float = ...
    ) -> dict[str, Any]: ...
    def raise_failure(self) -> None: ...
    def stop(self) -> None: ...
