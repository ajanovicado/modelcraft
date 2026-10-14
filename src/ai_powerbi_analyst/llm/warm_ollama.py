"""Reuse one guarded model session; release it after two idle minutes.

The owner thread holds the process lock and, when Windows grants it, the sleep
inhibitor for the entire session. The watchdog stays alive between requests.
Each request still writes to its own evidence directory.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_powerbi_analyst.paths import runtime_guard_root
from ai_powerbi_analyst.settings import ModelSettings

from .managed_ollama import ManagedOllamaModelRuntime
from .runtime import LocalModelError

if TYPE_CHECKING:
    from .managed_ollama_backend import OllamaServer


@dataclass
class _Request:
    target: Path
    granted: queue.Queue[_Lease | BaseException] = field(
        default_factory=lambda: queue.Queue(maxsize=1)
    )
    finished: threading.Event = field(default_factory=threading.Event)


class _Lease:
    def __init__(self, server: OllamaServer, request: _Request, owner: _Owner) -> None:
        self.server, self.request, self.owner = server, request, owner

    def complete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if self.request.finished.is_set() or self.owner.stop.is_set():
            raise LocalModelError("Model request has ended")
        return self.server.complete(*args, **kwargs)

    def stop(self) -> None:
        # The inherited chat iterator closes even after normal completion.
        # Only an unfinished lease means cancellation of live generation.
        if not self.request.finished.is_set():
            self.owner.stop.set()


class _Owner:
    def __init__(self, runtime: WarmOllamaModelRuntime, path: Path, cfg: dict[str, Any]) -> None:
        self.runtime, self.path, self.cfg = runtime, path, cfg
        self.key = json.dumps(cfg, sort_keys=True)
        self.requests: queue.Queue[_Request] = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.failure: BaseException | None = None
        self.thread = threading.Thread(target=self.run, name="guarded-qwen-owner", daemon=True)

    def run(self) -> None:
        from powerbi_rag_runtime.guards import exclusive_lock, keep_awake

        server: OllamaServer | None = None
        pending = None
        session = self.runtime.evidence_root / ("session-" + uuid.uuid4().hex)
        guard_root = runtime_guard_root(self.path)
        try:
            pending = self.requests.get(timeout=5)
            session.mkdir(parents=True, exist_ok=False)
            guard = self.runtime._guard(guard_root, self.cfg)
            lock = exclusive_lock(guard_root / ".active-training.lock")
            with lock, contextlib.ExitStack() as stack:
                # Keeping the PC awake is a courtesy: a refused power API must not stop Qwen.
                with contextlib.suppress(OSError):
                    stack.enter_context(keep_awake())
                self.runtime._save(session / "preflight.json", guard.check(startup=True))
                try:

                    def check() -> dict[str, object]:
                        if self.stop.is_set():
                            raise LocalModelError("Model session unloaded")
                        return guard.check()

                    server = self.runtime._server(self.cfg, check, session).start()
                    self.runtime._active = server
                    idle_since = time.monotonic()
                    while not self.stop.is_set():
                        server.raise_failure()
                        if server.stopped.is_set():
                            raise LocalModelError("Guarded model session stopped")
                        if pending is None:
                            if time.monotonic() - idle_since >= self.runtime.idle_seconds:
                                break
                            try:
                                pending = self.requests.get(timeout=0.25)
                            except queue.Empty:
                                continue
                        server.check()
                        process = server.process
                        assert process is not None
                        self.runtime._save(
                            pending.target / "session.json",
                            {"session": str(session), "pid": process.pid},
                        )
                        pending.granted.put(_Lease(server, pending, self))
                        while not pending.finished.wait(0.1):
                            server.raise_failure()
                            if self.stop.is_set() or server.stopped.is_set():
                                break
                        pending = None
                        idle_since = time.monotonic()
                finally:
                    # Stop the process tree before releasing the global lock.
                    if server is not None:
                        server.stop()
                    if self.runtime._active is server:
                        self.runtime._active = None
        except BaseException as exc:  # noqa: BLE001 - deliver thread failures to the caller
            self.failure = exc
            if session.exists():
                self.runtime._save(
                    session / ("failure-" + uuid.uuid4().hex + ".json"),
                    {"error": str(exc), "time": time.time()},
                )
        finally:
            self.stop.set()
            if pending is not None and pending.granted.empty():
                pending.granted.put(self.failure or LocalModelError("Model session closed"))
            while True:
                try:
                    waiting = self.requests.get_nowait()
                except queue.Empty:
                    break
                waiting.granted.put(self.failure or LocalModelError("Model session closed"))


class WarmOllamaModelRuntime(ManagedOllamaModelRuntime):
    """Amortize startup and keep the current request after reference examples."""

    idle_seconds = 120.0

    def __init__(self, settings: ModelSettings, evidence_root: Path | None = None) -> None:
        super().__init__(settings, evidence_root)
        self._owner: _Owner | None = None

    def _messages(
        self,
        system_prompt: str,
        user_prompt: str,
        references: str,
        history: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        from .artifact_policy import domain_for

        messages = super()._messages(system_prompt, user_prompt, references, history)
        # The unrestricted change regressed a fresh M task. Preserve the
        # original ordering outside the two domains with measured benefits.
        if references and domain_for(user_prompt) in {"dax", "model"}:
            messages[-1]["content"] = (
                references.lstrip("\n") + "\n\nCURRENT REQUEST - answer this only:\n" + user_prompt
            )
        return messages

    @contextlib.contextmanager
    def _session(self) -> Iterator[tuple[_Lease, Path, Path]]:
        if not self._mutex.acquire(blocking=False):
            raise LocalModelError("Local model is busy; only one guarded request is allowed")
        request = None
        try:
            path, cfg = self._configuration()
            target = self.evidence_root / (
                datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
            )
            target.mkdir(parents=True, exist_ok=False)
            owner = self._owner
            if owner and (
                owner.stop.is_set()
                or not owner.thread.is_alive()
                or owner.key != json.dumps(cfg, sort_keys=True)
            ):
                self.unload()
                owner = None
            if owner is None:
                owner = _Owner(self, path, cfg)
                self._owner = owner
                owner.thread.start()
            request = _Request(target)
            owner.requests.put_nowait(request)
            # Model load has its independent 120-second watchdog. Hashing and
            # process startup may add time; waiting here does not extend it.
            deadline = time.monotonic() + 150
            while True:
                try:
                    lease = request.granted.get(timeout=0.25)
                    break
                except queue.Empty:
                    if not owner.thread.is_alive() or time.monotonic() >= deadline:
                        raise LocalModelError(
                            str(owner.failure or "Model startup failed")
                        ) from None
            if isinstance(lease, BaseException):
                raise LocalModelError(str(lease)) from lease
            yield lease, target, path.parent.parent
        except BaseException:
            if self._owner:
                self._owner.stop.set()
            raise
        finally:
            if request:
                request.finished.set()
            self._mutex.release()

    def unload(self) -> None:
        owner = self._owner
        if owner:
            owner.stop.set()
            owner.thread.join(timeout=30)
            if owner.thread.is_alive():
                raise LocalModelError("Owned model worker did not shut down")
            if self._owner is owner:
                self._owner = None
