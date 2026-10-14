"""Install the one pinned local model into the per-user store on first launch.

Installers cannot carry Qwen 4B (a single 3.4 GB file; MSI and NSIS stop at
2 GB), so the packaged profile points its model at %LOCALAPPDATA%. Sources, in
order: the file already in place, a byte-identical copy in the local Ollama
store, a file the user chose, or a download of exactly the pinned blob. Every
source is accepted only when its SHA-256 matches the profile's pin; the guarded
runtime verifies the same pin again before each model start.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ai_powerbi_analyst.contracts import ClosedModel

_CHUNK = 4 * 1024 * 1024
# Room kept free after the model lands, so the disk the user works on never fills.
_DISK_MARGIN = 256 * 1024 * 1024
_FileSignature = tuple[int, int, int, int, int]


class ModelInstallRequest(ClosedModel):
    source: Literal["auto", "download", "file"] = "auto"
    path: str | None = Field(default=None, max_length=1000)


@dataclass(frozen=True)
class ModelTarget:
    path: Path
    sha256: str
    size: int | None
    url: str | None
    label: str


def model_target(runtime: Any) -> ModelTarget | None:
    """The pinned model file of the runtime's profile, or None for other runtimes."""
    describe = getattr(runtime, "model_target", None)
    return describe() if callable(describe) else None


def ollama_blob(sha256: str) -> Path:
    store = os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models"
    return Path(store) / "blobs" / f"sha256-{sha256}"


class ModelInstallError(RuntimeError):
    pass


class ModelInstaller:
    """One background install at a time; status() is cheap enough to poll."""

    def __init__(self, target: ModelTarget | None):
        self.target = target
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"state": "idle"}
        self._verified_signature: _FileSignature | None = None
        self._damaged_signature: _FileSignature | None = None
        self._attempted_signature: _FileSignature | None = None

    # -- status ---------------------------------------------------------------

    def installed(self) -> bool:
        with self._lock:
            signature = self._signature()
            return signature is not None and signature == self._verified_signature

    def _signature(self) -> _FileSignature | None:
        if self.target is None:
            return None
        return self._path_signature(self.target.path)

    @staticmethod
    def _path_signature(path: Path) -> _FileSignature | None:
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def local_copy(self) -> Path | None:
        if self.target is None:
            return None
        candidate = ollama_blob(self.target.sha256)
        if candidate.resolve() == self.target.path.resolve():
            return None
        if candidate.is_file() and (
            self.target.size is None or candidate.stat().st_size == self.target.size
        ):
            return candidate
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            signature = self._signature()
            running = self._thread is not None and self._thread.is_alive()
            # Verify an existing file once in the background, then cache only its
            # exact file identity/metadata. A changed file must be checked again.
            # Cancellation/failure does not silently restart the same check.
            if (
                signature is not None
                and not running
                and signature
                not in (
                    self._verified_signature,
                    self._damaged_signature,
                    self._attempted_signature,
                )
            ):
                self._launch_locked("verify", None, signature)
                running = True
            installed = (
                not running and signature is not None and signature == self._verified_signature
            )
            state = dict(self._state)
        target = self.target
        if not running and installed:
            state["state"] = "installed"
        elif not running and state["state"] in {"idle", "installed"}:
            state["state"] = "missing" if target else "unavailable"
        copy = self.local_copy() if target and not installed else None
        return {
            **state,
            "installed": installed,
            "running": running,
            "model": target.label if target else None,
            "bytes_total": state.get("bytes_total") or (target.size if target else None),
            "download_available": bool(target and target.url),
            "local_copy": str(copy) if copy else None,
            "target": str(target.path) if target else None,
        }

    # -- control --------------------------------------------------------------

    def start(self, source: str = "auto", path: str | None = None) -> dict[str, Any]:
        if self.target is None:
            raise ModelInstallError("This build has no installable local model profile.")
        if source == "file" and not path:
            raise ModelInstallError("Choose the Qwen 4B model file to install.")
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._launch_locked(source, path, self._signature())
        # status takes the same lock; repeated starts must return outside it.
        return self.status()

    def _launch_locked(
        self, source: str, path: str | None, signature: _FileSignature | None
    ) -> None:
        self._cancel.clear()
        self._attempted_signature = signature
        self._state = {
            "state": "verifying" if source == "verify" else "starting",
            "source": "existing" if source == "verify" else source,
            "bytes_done": 0,
        }
        self._thread = threading.Thread(
            target=self._run, args=(source, path), name="model-install", daemon=True
        )
        self._thread.start()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            self._cancel.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=10)
        return self.status()

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.status()

    # -- work -----------------------------------------------------------------

    def _update(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def _run(self, source: str, path: str | None) -> None:
        target = self.target
        assert target is not None
        try:
            if source == "verify":
                self._verify_in_place()
            elif not self.installed():
                signature = self._signature()
                with self._lock:
                    damaged = signature is not None and signature == self._damaged_signature
                if signature is not None and not damaged:
                    # Explicit Start also authorizes a verified replacement.
                    with contextlib.suppress(_DamagedModel):
                        self._verify_in_place()
                if not self.installed():
                    copy = self.local_copy() if source == "auto" else None
                    if source == "file":
                        self._copy(Path(path or ""), "file")
                    elif copy is not None:
                        try:
                            self._copy(copy, "ollama")
                        except _FingerprintMismatch:
                            if not target.url:
                                raise
                            self._download()
                    elif target.url:
                        self._download()
                    else:
                        raise ModelInstallError(
                            "No download address is pinned for this model; choose the model file."
                        )
            self._update(state="installed", finished_at=time.time(), error=None)
        except _Cancelled:
            self._update(state="cancelled", error=None)
        except _DamagedModel as error:
            self._update(state="damaged", error=str(error))
        except Exception as error:  # noqa: BLE001 - any failure is reported to the user
            self._update(state="failed", error=str(error)[:500])

    def _check_disk(self, needed: int) -> None:
        folder = self.target.path.parent  # type: ignore[union-attr]
        folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(folder).free
        if free < needed + _DISK_MARGIN:
            raise ModelInstallError(
                f"Not enough free disk space: Qwen 4B needs {needed / 1e9:.1f} GB more "
                f"on {folder.anchor or folder}, and {free / 1e9:.1f} GB is free."
            )

    def _finish(self, partial: Path, digest: str) -> None:
        target = self.target
        assert target is not None
        self._raise_if_cancelled()
        signature = self._path_signature(partial)
        # Check the completed bytes on disk, as well as the streamed bytes. This
        # also makes a final check interruptible without removing the old file.
        if digest == target.sha256:
            self._update(state="verifying", bytes_done=0)
            digest = self._hash_file(partial)
            self._raise_if_cancelled()
            if signature is None or signature != self._path_signature(partial):
                raise ModelInstallError(
                    "The downloaded model file changed during its check. Retry."
                )
        if digest != target.sha256:
            partial.unlink(missing_ok=True)
            raise _FingerprintMismatch(
                "The file does not match the pinned Qwen 4B fingerprint, so it was not installed."
            )
        self._raise_if_cancelled()
        # The existing file is retained until the replacement has passed its pin.
        os.replace(partial, target.path)
        promoted = self._signature()
        # Rename can change ctime, but the checked identity/size/mtime must stay
        # the same. Never cache a concurrently changed file as verified.
        if signature is None or promoted is None or signature[:4] != promoted[:4]:
            raise ModelInstallError(
                "The installed model file changed during setup. Check it again."
            )
        with self._lock:
            self._verified_signature = promoted
            self._damaged_signature = None
            self._attempted_signature = promoted

    def _verify_in_place(self) -> None:
        target = self.target
        assert target is not None
        signature = self._signature()
        self._update(state="verifying", source="existing", bytes_total=target.size, bytes_done=0)
        wrong_size = signature is not None and target.size not in (None, signature[2])
        digest = None if wrong_size else self._hash_file(target.path)
        self._raise_if_cancelled()
        if signature is None or signature != self._signature():
            raise ModelInstallError("The model file changed while it was being checked. Try again.")
        with self._lock:
            self._verified_signature = signature if digest == target.sha256 else None
            self._damaged_signature = signature if digest != target.sha256 else None
        if digest != target.sha256:
            raise _DamagedModel(
                "The local Qwen 4B file is damaged (fingerprint mismatch). "
                "Repair it to install a checked replacement; no manual deletion is needed."
            )

    def _hash_file(self, path: Path, limit: int | None = None) -> str:
        hasher = hashlib.sha256()
        done = 0
        with path.open("rb") as stream:
            while limit is None or done < limit:
                chunk = stream.read(_CHUNK if limit is None else min(_CHUNK, limit - done))
                if not chunk:
                    break
                self._raise_if_cancelled()
                hasher.update(chunk)
                done += len(chunk)
                self._update(bytes_done=done)
        return hasher.hexdigest()

    def _copy(self, source: Path, kind: str) -> None:
        target = self.target
        assert target is not None
        if not source.is_file():
            raise ModelInstallError(f"Model file not found: {source}")
        size = source.stat().st_size
        if target.size is not None and size != target.size:
            raise ModelInstallError(
                f"That file is {size:,} bytes; the pinned Qwen 4B file is {target.size:,} bytes."
            )
        self._check_disk(size)
        partial = target.path.with_name(target.path.name + ".partial")
        self._update(state="copying", source=kind, bytes_total=size, bytes_done=0)
        hasher = hashlib.sha256()
        done = 0
        with source.open("rb") as reader, partial.open("wb") as writer:
            while chunk := reader.read(_CHUNK):
                self._raise_if_cancelled()
                hasher.update(chunk)
                writer.write(chunk)
                done += len(chunk)
                self._update(bytes_done=done)
        self._finish(partial, hasher.hexdigest())

    def _download(self) -> None:
        target = self.target
        assert target is not None and target.url
        partial = target.path.with_name(target.path.name + ".partial")
        target.path.parent.mkdir(parents=True, exist_ok=True)
        done = partial.stat().st_size if partial.exists() else 0
        if target.size is not None and done > target.size:
            partial.unlink()
            done = 0
        self._check_disk((target.size or 0) - done)
        self._update(state="downloading", source="download", bytes_total=target.size)
        hasher = hashlib.sha256()
        if done:
            # Resume: the running hash must cover the bytes already on disk.
            self._update(state="verifying", bytes_done=0)
            hasher = hashlib.sha256()
            with partial.open("rb") as stream:
                while chunk := stream.read(_CHUNK):
                    self._raise_if_cancelled()
                    hasher.update(chunk)
            # Cancellation can leave a complete, unpromoted download. Check it
            # locally instead of requesting an invalid range at end-of-file.
            if done == target.size:
                self._update(state="verifying", bytes_done=done)
                self._finish(partial, hasher.hexdigest())
                return
        headers = {"User-Agent": "AI-Analyst-Studio/1.0"}
        if done:
            headers["Range"] = f"bytes={done}-"
        request = urllib.request.Request(target.url, headers=headers)  # noqa: S310 - pinned https URL
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            if done and response.status != 206:
                # The server ignored the range: start over rather than corrupt the file.
                done, hasher = 0, hashlib.sha256()
                mode = "wb"
            else:
                mode = "ab" if done else "wb"
            length = response.headers.get("Content-Length")
            total = target.size or (done + int(length) if length else None)
            self._update(state="downloading", bytes_total=total, bytes_done=done)
            fresh = 0  # bytes fetched in this attempt, for an honest speed after a resume
            with partial.open(mode) as writer:
                while chunk := response.read(_CHUNK):
                    self._raise_if_cancelled()
                    hasher.update(chunk)
                    writer.write(chunk)
                    done += len(chunk)
                    fresh += len(chunk)
                    elapsed = max(time.monotonic() - started, 1e-6)
                    self._update(bytes_done=done, bytes_per_second=round(fresh / elapsed))
        if target.size is not None and done != target.size:
            raise ModelInstallError(
                f"Download ended early ({done:,} of {target.size:,} bytes); press Install "
                "again to resume."
            )
        self._update(state="verifying")
        self._finish(partial, hasher.hexdigest())

    def _raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled


class _Cancelled(Exception):
    pass


class _DamagedModel(ModelInstallError):
    pass


class _FingerprintMismatch(ModelInstallError):
    pass
