"""Serve the single pinned local Qwen 4B profile per request, with no cloud fallback."""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from ai_powerbi_analyst.settings import ModelSettings

from .hardware import Candidate, Hardware, Selection, select_profile
from .profile_ollama import ProfileOllamaRuntime
from .resource_profiles import PROFILES, configured_profile
from .runtime import (
    GenerationMetrics,
    LocalModelError,
    ModelAttestation,
    ModelHealth,
    StructuredOutput,
)

if TYPE_CHECKING:
    from .model_install import ModelTarget


class ProfileEntry(TypedDict):
    path: Path
    config: dict[str, Any]
    sha256: str


GIB = 1024**3


class AdaptiveQwenRuntime:
    # studio/service.py keys its compact CPU prompts on this flag. Qwen 4B is the
    # only model, so health() reports no automatic selection between models.
    automatic_model_selection = True
    # Longest bounded planning call (the 75 s narrative) plus margin.
    busy_wait_seconds: float = 90.0

    def __init__(self, settings: ModelSettings, evidence_root: Path | None = None) -> None:
        self.base_settings = settings
        self.evidence_root = evidence_root or Path.home() / ".ai-powerbi/model-evidence"
        self._lock = threading.Lock()
        self._current: ProfileOllamaRuntime | None = None
        self._key: str | None = None
        from ai_powerbi_analyst.paths import resolve_runtime_config

        if settings.runtime_config_path is None:
            raise ValueError("Adaptive runtime requires a pinned profile catalog")
        self._catalog_path = resolve_runtime_config(settings.runtime_config_path).resolve(
            strict=True
        )
        self._entries = self._read_catalog()

    def _read_catalog(self) -> dict[str, ProfileEntry]:
        catalog = json.loads(self._catalog_path.read_text(encoding="utf-8"))
        if catalog.get("adaptive_profiles") != 1:
            raise ValueError("Unknown adaptive profile catalog")
        entries: dict[str, ProfileEntry] = {}
        for key, entry in catalog["profiles"].items():
            if key not in PROFILES or entry.get("qualified") is not True:
                continue
            path = (self._catalog_path.parent / entry["config"]).resolve(strict=True)
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise LocalModelError("Adaptive profile configuration hash mismatch")
            from ai_powerbi_analyst.paths import normalize_profile_config

            cfg = normalize_profile_config(json.loads(path.read_text(encoding="utf-8")), path)
            profile = configured_profile(cfg)
            if profile is None or profile.key != key:
                raise LocalModelError("Adaptive profile identity mismatch")
            entries[key] = {"path": path, "config": cfg, "sha256": entry["sha256"]}
        # A stale payload listing only retired profiles must not take the whole studio
        # service down: health() and each request report the model as not installed.
        return entries

    def _selection(self) -> Selection:
        # Installed files decide, never free or total RAM: Qwen 4B always runs.
        candidates = []
        for key, profile in PROFILES.items():
            entry = self._entries.get(key)
            installed = bool(entry and all(Path(p).is_file() for p in entry["config"]["hashes"]))
            candidates.append(Candidate(key, profile.tag, installed, entry is not None))
        return select_profile(candidates)

    def _pick(self) -> ProfileOllamaRuntime:
        # Reuse a live warm session; its owner releases it on idle, Stop or cancellation.
        if self._current and self._current._active is not None:
            if self._key is None:
                raise LocalModelError("Warm model profile is unavailable")
            self._verify_entry(self._entries[self._key])
            return self._current
        selection = self._selection()
        if selection.candidate is None:
            raise LocalModelError(selection.reason)
        key = selection.candidate.key
        entry, profile = self._entries[key], PROFILES[key]
        self._verify_entry(entry)
        if self._current and self._key == key:
            return self._current
        if self._current:
            self._current.unload()
        settings = self.base_settings.model_copy(
            update={
                "name": profile.tag,
                "selected_digest": "sha256:" + profile.digest,
                "runtime_config_path": str(entry["path"]),
            }
        )
        self._current = ProfileOllamaRuntime(settings, self.evidence_root / key)
        if entry["config"].get("backend") == "managed-llama-text":
            self._current.endpoint_label = "managed text-only Qwen (loopback; CPU; guarded)"
        self._current.idle_seconds = profile.idle_seconds
        self._key = key
        return self._current

    def prompt_bytes_per_token(self) -> float:
        """Conservative UTF-8 bytes per budget unit for the backend that serves next.

        The managed Ollama backend bounds raw bytes (1.0). The text-only backend
        counts real tokenizer tokens, where English/JSON/DAX runs well above three
        bytes per token, so byte-budgeting it would discard useful references.
        Selection mirrors _pick without loading or switching a model.
        """
        if self._current and self._current._active is not None:
            key = self._key
        else:
            candidate = self._selection().candidate
            key = candidate.key if candidate else None
        entry = self._entries.get(key) if key else None
        if entry and entry["config"].get("backend") == "managed-llama-text":
            return 3.0
        return 1.0

    def model_target(self) -> ModelTarget | None:
        """The pinned model file that first-launch install fills (llm/model_install.py)."""
        from .model_install import ModelTarget

        entry = self._entries.get("standard")
        if entry is None:
            return None
        cfg = entry["config"]
        download = cfg.get("model_download") or {}
        return ModelTarget(
            path=Path(cfg["model"]),
            sha256=cfg["hashes"][cfg["model"]],
            size=download.get("bytes"),
            url=download.get("url"),
            label=PROFILES["standard"].label,
        )

    @staticmethod
    def _verify_entry(entry: ProfileEntry) -> None:
        if hashlib.sha256(entry["path"].read_bytes()).hexdigest() != entry["sha256"]:
            raise LocalModelError("Adaptive profile configuration hash mismatch")

    @property
    def settings(self) -> ModelSettings:
        return self._current.settings if self._current else self.base_settings

    @property
    def last_generation(self) -> GenerationMetrics | None:
        return self._current.last_generation if self._current else None

    def health(self) -> ModelHealth:
        # Free and total RAM are reported truthfully but never gate the model, so
        # health carries no startup memory requirement and no selection between models.
        locked = self._lock.acquire(blocking=False)
        try:
            model = self._pick() if locked else self._current
            if model is None:
                raise LocalModelError("Local model is starting")
            health = model.health()
            return health.model_copy(
                update={
                    "memory_profile": PROFILES[self._key or "standard"].label,
                    "total_memory_gb": round(Hardware.detect().total_bytes / GIB, 2),
                }
            )
        except (OSError, ValueError, RuntimeError) as error:
            hardware = Hardware.detect()
            return ModelHealth(
                service_available=False,
                model_available=False,
                endpoint="adaptive guarded Qwen (local CPU)",
                available_memory_gb=round(hardware.available_bytes / GIB, 2),
                error=str(error),
                memory_profile=PROFILES[self._key or "standard"].label,
                total_memory_gb=round(hardware.total_bytes / GIB, 2),
            )
        finally:
            if locked:
                self._lock.release()

    def _record_selection(self, model: ProfileOllamaRuntime) -> None:
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        hardware = Hardware.detect()
        model._save(
            self.evidence_root / ("selection-" + uuid.uuid4().hex + ".json"),
            {
                "profile": self._key,
                "model": model.settings.name,
                "total_bytes": hardware.total_bytes,
                "available_bytes": hardware.available_bytes,
                "warm": model._active is not None,
            },
        )

    def _acquire(self) -> None:
        """Wait briefly for the single model slot instead of failing a user request.

        A chat sent while the draft narrative is generating used to fail at once.
        Requests still run one at a time; the wait is bounded (0 = no waiting).
        """
        wait = self.busy_wait_seconds
        acquired = self._lock.acquire(timeout=wait) if wait > 0 else self._lock.acquire(False)
        if not acquired:
            raise LocalModelError("Local model is busy; only one guarded request is allowed")

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredOutput],
        **kwargs: Any,
    ) -> StructuredOutput:
        self._acquire()
        try:
            model = self._pick()
            self._record_selection(model)
            return model.generate(system_prompt, user_prompt, response_model, **kwargs)
        finally:
            self._lock.release()

    def stream_chat(
        self,
        message: str,
        *,
        system_prompt: str,
        history: list[dict[str, str]] | None = None,
        worked_examples: bool = True,
        max_output_tokens: int | None = None,
    ) -> Iterator[str]:
        self._acquire()
        stream = None
        try:
            model = self._pick()
            self._record_selection(model)
            stream = model.stream_chat(
                message,
                system_prompt=system_prompt,
                history=history,
                worked_examples=worked_examples,
                max_output_tokens=max_output_tokens,
            )
            yield from stream
        finally:
            if stream is not None:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()
            self._lock.release()

    def unload(self) -> None:
        if self._current:
            self._current.unload()

    @contextlib.contextmanager
    def pause_inference(self) -> Iterator[None]:
        """Finish active inference, then release owned memory for external work."""
        self._acquire()
        try:
            self.unload()
            yield
        finally:
            self._lock.release()

    def capture_attestation(self) -> ModelAttestation:
        with self._lock:
            return self._pick().capture_attestation()

    def write_attestation(self, path: str | Path) -> ModelAttestation:
        with self._lock:
            return self._pick().write_attestation(path)
