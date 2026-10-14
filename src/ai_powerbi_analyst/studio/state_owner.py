"""Keep one engine process responsible for a local Studio state folder."""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

    def _lock_descriptor(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)

    def _unlock_descriptor(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


_CONTENTION_ERRORS = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}


class StateRootOwnedError(RuntimeError):
    """Another engine already owns this state folder."""


@contextmanager
def own_state_root(state_root: str | Path) -> Iterator[Path]:
    """Hold a native, nonblocking OS lock until the engine has shut down.

    The marker is deliberately retained: its existence is not ownership, and
    keeping its identity avoids splitting competing processes across files.
    The OS also releases the lock after an abrupt process exit.
    """
    root = Path(state_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    # os.open creates a non-inheritable descriptor: child model processes must
    # not retain engine ownership after the engine exits. Never truncate or
    # unlink the file, including after an unsuccessful acquisition.
    descriptor = os.open(root / "studio-engine.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            _lock_descriptor(descriptor)
        except OSError as error:
            if error.errno not in _CONTENTION_ERRORS:
                raise
            raise StateRootOwnedError(
                f"Another Modelcraft engine is already using the state folder '{root}'. "
                "Close the existing app or engine before starting another with this state folder."
            ) from error
        try:
            yield root
        finally:
            _unlock_descriptor(descriptor)
    finally:
        os.close(descriptor)
