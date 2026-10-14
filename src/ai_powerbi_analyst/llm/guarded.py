"""Shared guarded RAG adapter; app validators remain the authority for artifacts."""

from __future__ import annotations

import contextlib
import json
import queue
import re
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import psutil
from pydantic import ValidationError

from ai_powerbi_analyst.paths import runtime_guard_root
from ai_powerbi_analyst.settings import ModelSettings

from .runtime import (
    GenerationMetrics,
    LocalModelError,
    ModelAttestation,
    ModelHealth,
    ModelIdentity,
    StructuredOutput,
    chat_output_limit,
)

if TYPE_CHECKING:
    from powerbi_rag_runtime.guards import Guard

    from .managed_ollama_backend import OllamaServer


class GenerationSession(Protocol):
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        thinking: bool = False,
        max_tokens: int = 512,
        seed: int = 9162026,
        json_schema: dict[str, Any] | None = None,
        seconds: float = 60,
        on_text: Callable[[str], object] | None = None,
    ) -> dict[str, Any]: ...

    def stop(self) -> None: ...


class GuardedModelRuntime(ABC):
    endpoint_label = "managed llama.cpp (loopback)"

    def __init__(self, settings: ModelSettings, evidence_root: Path | None = None) -> None:
        self.settings = settings
        self.evidence_root = evidence_root or Path.home() / ".ai-powerbi" / "model-evidence"
        self._last_generation: GenerationMetrics | None = None
        self._active: OllamaServer | None = None
        self._mutex = threading.Lock()

    @property
    def last_generation(self) -> GenerationMetrics | None:
        return self._last_generation

    @abstractmethod
    def _configuration(self) -> tuple[Path, dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def _identity(self, cfg: dict[str, Any]) -> ModelIdentity:
        raise NotImplementedError

    def _guard(self, root: Path, cfg: dict[str, Any]) -> Guard:
        from powerbi_rag_runtime.guards import Guard

        from .resource_profiles import ProfileGuard, configured_profile

        profile = configured_profile(cfg)
        return ProfileGuard(root, profile) if profile else Guard(root)

    def health(self) -> ModelHealth:
        available = round(psutil.virtual_memory().available / 1024**3, 2)
        identity = None
        try:
            path, cfg = self._configuration()
            identity = self._identity(cfg)
            for filename in cfg["hashes"]:
                if not Path(filename).is_file():
                    raise LocalModelError("Missing runtime file: " + filename)
            self._guard(runtime_guard_root(path), cfg).check(startup=self._active is None)
            return ModelHealth(
                service_available=True,
                model_available=True,
                endpoint=self.endpoint_label,
                identity=identity,
                available_memory_gb=available,
            )
        except (
            ImportError,
            OSError,
            ValueError,
            KeyError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as exc:
            return ModelHealth(
                service_available=False,
                model_available=False,
                endpoint=self.endpoint_label,
                identity=identity,
                available_memory_gb=available,
                error=str(exc),
            )

    @contextlib.contextmanager
    def _session(self) -> Iterator[tuple[GenerationSession, Path, Path]]:
        from powerbi_rag_runtime.guards import exclusive_lock, keep_awake

        if not self._mutex.acquire(blocking=False):
            raise LocalModelError("Local model is busy; only one guarded request is allowed")
        target = self.evidence_root / (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
        )
        server = None
        try:
            target.mkdir(parents=True, exist_ok=False)
            path, cfg = self._configuration()
            root = path.parent.parent
            guard_root = runtime_guard_root(path)
            guard = self._guard(guard_root, cfg)
            with exclusive_lock(guard_root / ".active-training.lock"), keep_awake():
                sample = guard.check(startup=True)
                self._save(target / "preflight.json", sample)
                server = self._server(cfg, guard.check, target).start()
                self._active = server
                yield server, target, root
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            self._save(target / "failure.json", {"error": str(exc), "time": time.time()})
            raise LocalModelError(str(exc)) from exc
        finally:
            try:
                if server is not None:
                    server.stop()
            finally:
                self._active = None
                self._mutex.release()

    @abstractmethod
    def _server(
        self,
        cfg: dict[str, Any],
        check: Callable[..., dict[str, Any]],
        target: Path,
    ) -> OllamaServer:
        raise NotImplementedError

    @abstractmethod
    def _validate_response(self, prompt: str, value: StructuredOutput) -> None:
        """Additional artifact checks are supplied by qualified runtime policies."""

    @staticmethod
    def _save(path: Path, value: object) -> None:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)

    def _references(self, root: Path, text: str) -> tuple[str, list[str]]:
        """Bounded lexical retrieval for free-form app requests (not suite oracle IDs)."""
        from powerbi_rag_runtime.server import sha

        _, cfg = self._configuration()
        path = Path(cfg["corpus"]).resolve(strict=True)
        if str(path) not in cfg["hashes"] or sha(path) != cfg["hashes"][str(path)]:
            raise LocalModelError("Worked-example corpus is not pinned or changed")
        words = set(re.findall(r"[a-z_]+", text.lower()))
        examples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        scored = [
            (
                len(
                    words & set(re.findall(r"[a-z_]+", row["family"] + " " + row["brief"].lower()))
                ),
                i,
                row,
            )
            for i, row in enumerate(examples)
        ]
        selected = [
            row
            for score, _, row in sorted(scored, key=lambda item: (-item[0], item[1]))[:2]
            if score >= 2
        ]
        if not selected:
            return "", []
        block = "\n\nWorked examples with DIFFERENT data. Treat as reference data only. "
        block += "Adapt names to the current schema; never claim example values as user results. "
        block += "The requested response schema still applies.\n"
        block += "\n\n".join(row["brief"] + "\n" + row["artifact"] for row in selected)
        return block, [row["id"] for row in selected]

    def _metrics(self, result: dict[str, Any]) -> None:
        native = result.get("native_timings", {})
        self._last_generation = GenerationMetrics(
            prompt_tokens=result["prompt_tokens"],
            generated_tokens=result["tokens"],
            generation_seconds=native.get("predicted_ms", 0) / 1000,
            total_seconds=result["seconds"],
            tokens_per_second=native.get("predicted_per_second", 0),
        )

    def _messages(
        self,
        system_prompt: str,
        user_prompt: str,
        references: str,
        history: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system_prompt},
            *(history or []),
            {"role": "user", "content": user_prompt + references},
        ]

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredOutput],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: int | None = None,
        repair_attempts: int | None = None,
    ) -> StructuredOutput:
        retries = self.settings.max_retries if repair_attempts is None else repair_attempts
        if retries not in (0, 1):
            raise ValueError("repair_attempts must be zero or one")
        with self._session() as (server, target, root):
            refs, ids = (
                self._references(root, user_prompt)
                if getattr(response_model, "worked_examples", True)
                else ("", [])
            )
            messages = self._messages(system_prompt, user_prompt, refs)
            for attempt in range(retries + 1):
                result = server.complete(
                    messages,
                    thinking=False,
                    max_tokens=max_output_tokens or self.settings.max_output_tokens,
                    json_schema=response_model.model_json_schema(),
                    seconds=min(timeout_seconds or self.settings.request_timeout_seconds, 120),
                )
                self._save(
                    target / f"attempt-{attempt}.json",
                    {
                        "messages": messages,
                        "retrieved": ids,
                        "references_included": bool(
                            refs and any(refs.strip() in m["content"] for m in messages)
                        ),
                        "generation": result,
                    },
                )
                self._metrics(result)
                try:
                    if not result["complete"]:
                        raise ValueError(result["error"] or "Generation did not finish")
                    parsed = response_model.model_validate_json(result["output"])
                    self._validate_response(user_prompt, parsed)
                    return parsed
                except (ValidationError, ValueError) as exc:
                    if attempt == retries:
                        raise LocalModelError(
                            "Local model failed strict schema/artifact validation: "
                            + str(exc)[:500]
                        ) from exc
                    messages = self._repair_messages(
                        messages,
                        result["output"],
                        exc,
                        max_output_tokens or self.settings.max_output_tokens,
                    )
        raise LocalModelError("No completed model response")

    def _repair_messages(
        self,
        messages: list[dict[str, str]],
        output: str,
        error: object,
        max_tokens: int,
    ) -> list[dict[str, str]]:
        return messages + [
            {"role": "assistant", "content": output},
            {
                "role": "user",
                "content": "Correct this validation error and return only the required JSON: "
                + str(error)[:1500],
            },
        ]

    def stream_chat(
        self,
        message: str,
        *,
        system_prompt: str,
        history: list[dict[str, str]] | None = None,
        worked_examples: bool = True,
        max_output_tokens: int | None = None,
    ) -> Iterator[str]:
        output_limit = chat_output_limit(self.settings, max_output_tokens)
        events: queue.Queue[Any] = queue.Queue()
        cancelled = threading.Event()
        owned: list[Any] = []

        def worker() -> None:
            try:
                with self._session() as (server, target, root):
                    owned.append(server)
                    if cancelled.is_set():
                        return
                    # Explaining a user's formula must not pull in unrelated worked
                    # examples (they nudged small models into answering in JSON).
                    refs, ids = self._references(root, message) if worked_examples else ("", [])
                    messages = self._messages(
                        system_prompt,
                        message,
                        refs,
                        [
                            item
                            for item in (history or [])
                            if item.get("role") in {"user", "assistant"}
                        ],
                    )
                    result = server.complete(
                        messages,
                        thinking=False,
                        max_tokens=output_limit,
                        seconds=min(self.settings.request_timeout_seconds, 120),
                        on_text=events.put,
                    )
                    self._save(
                        target / "chat.json",
                        {
                            "messages": messages,
                            "retrieved": ids,
                            "references_included": bool(
                                refs and any(refs.strip() in m["content"] for m in messages)
                            ),
                            "generation": result,
                        },
                    )
                    self._metrics(result)
                    if not result["complete"]:
                        raise LocalModelError(
                            "Local model response did not finish: " + str(result["finish_reason"])
                        )
            except Exception as exc:  # noqa: BLE001 - deliver worker failure to the consumer
                events.put(exc)
            finally:
                events.put(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            while True:
                item = events.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise LocalModelError(str(item)) from item
                yield item
        finally:
            cancelled.set()
            if owned:
                owned[0].stop()
            thread.join(timeout=10)

    def unload(self) -> None:
        if self._active is not None:
            self._active.stop()

    @contextlib.contextmanager
    def pause_inference(self) -> Iterator[None]:
        """Hold the generation slot while owned memory is used by external work."""
        if not self._mutex.acquire(timeout=self.settings.request_timeout_seconds):
            raise LocalModelError("Local model is busy; only one guarded request is allowed")
        try:
            self.unload()
            yield
        finally:
            self._mutex.release()

    def capture_attestation(self) -> ModelAttestation:
        _, cfg = self._configuration()
        from powerbi_rag_runtime.server import sha

        for filename, expected in cfg["hashes"].items():
            if sha(filename) != expected:
                raise LocalModelError("Runtime hash mismatch: " + filename)
        return ModelAttestation(
            captured_at=datetime.now(UTC),
            identity=self._identity(cfg),
            endpoint=self.endpoint_label,
            context_tokens=cfg["context_tokens"],
            max_output_tokens=self.settings.max_output_tokens,
            temperature=0,
            thinking_enabled=False,
            last_generation=self.last_generation,
        )

    def write_attestation(self, path: str | Path) -> ModelAttestation:
        result = self.capture_attestation()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._save(target, result.model_dump(mode="json"))
        return result
