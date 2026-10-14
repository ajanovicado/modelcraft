"""Health, streaming chat, and strict structured generation for local Ollama."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

import psutil
from pydantic import BaseModel, ValidationError

from ai_powerbi_analyst.contracts import ClosedModel
from ai_powerbi_analyst.preflight import probe_tool
from ai_powerbi_analyst.settings import ModelSettings

StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


class ModelIdentity(ClosedModel):
    name: str
    digest: str | None = None
    size_bytes: int | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    installed_at: datetime | None = None
    license: str = "Apache-2.0"
    source_url: str = "https://huggingface.co/Qwen/Qwen3.5-4B"


class GenerationMetrics(ClosedModel):
    prompt_tokens: int = 0
    generated_tokens: int = 0
    generation_seconds: float = 0
    total_seconds: float = 0
    tokens_per_second: float = 0


class ModelAttestation(ClosedModel):
    captured_at: datetime
    identity: ModelIdentity
    ollama_version: str | None = None
    endpoint: str
    context_tokens: int
    max_output_tokens: int
    temperature: float
    thinking_enabled: bool = False
    last_generation: GenerationMetrics | None = None


class ModelHealth(ClosedModel):
    service_available: bool
    model_available: bool
    endpoint: str
    identity: ModelIdentity | None = None
    available_memory_gb: float
    error: str | None = None
    memory_profile: str | None = None
    startup_memory_gb: float | None = None
    total_memory_gb: float | None = None
    automatic_model_selection: bool = False


class StructuredModelRuntime(Protocol):
    def health(self) -> ModelHealth: ...

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredOutput],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: int | None = None,
        repair_attempts: int | None = None,
    ) -> StructuredOutput: ...

    def stream_chat(
        self,
        message: str,
        *,
        system_prompt: str,
        history: list[dict[str, str]] | None = None,
        worked_examples: bool = True,
        max_output_tokens: int | None = None,
    ) -> Iterator[str]: ...

    def unload(self) -> None: ...


@runtime_checkable
class PausableModelRuntime(Protocol):
    """Optional owned-runtime capability; shared legacy runtimes do not opt in."""

    def pause_inference(self) -> AbstractContextManager[None]: ...


class LocalModelError(RuntimeError):
    """The loopback model service was unavailable or returned invalid output."""


def chat_output_limit(settings: ModelSettings, requested: int | None) -> int:
    """A request may reserve more room for code without mutating shared model settings."""
    output = settings.chat_output_tokens if requested is None else requested
    if type(output) is not int or not 64 <= output <= 1024:
        raise ValueError("Chat output budget must be an integer between 64 and 1024 tokens")
    return output


class OllamaModelRuntime:
    """Small HTTP client that never leaves the configured loopback endpoint."""

    def __init__(self, settings: ModelSettings | None = None) -> None:
        self.settings = settings or ModelSettings()
        self._last_generation: GenerationMetrics | None = None

    @property
    def last_generation(self) -> GenerationMetrics | None:
        return self._last_generation

    def health(self) -> ModelHealth:
        available_gb = round(psutil.virtual_memory().available / 1024**3, 2)
        try:
            payload = self._request_json("GET", "/api/tags")
            models = payload.get("models", []) if isinstance(payload, dict) else []
            for value in models if isinstance(models, list) else []:
                if not isinstance(value, dict):
                    continue
                name = str(value.get("name") or value.get("model") or "")
                if name.casefold() != self.settings.name.casefold():
                    continue
                raw_details = value.get("details")
                details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
                identity = ModelIdentity(
                    name=name,
                    digest=(
                        _normalized_digest(str(value["digest"])) if value.get("digest") else None
                    ),
                    size_bytes=int(value["size"]) if isinstance(value.get("size"), int) else None,
                    parameter_size=(
                        str(details.get("parameter_size"))
                        if details.get("parameter_size")
                        else None
                    ),
                    quantization=(
                        str(details.get("quantization_level"))
                        if details.get("quantization_level")
                        else None
                    ),
                    installed_at=_parsed_timestamp(value.get("modified_at")),
                    license=_model_provenance(name)[0],
                    source_url=_model_provenance(name)[1],
                )
                expected_digest = self.settings.selected_digest
                if expected_digest is not None and identity.digest != _normalized_digest(
                    expected_digest
                ):
                    return ModelHealth(
                        service_available=True,
                        model_available=False,
                        endpoint=self.settings.endpoint,
                        identity=identity,
                        available_memory_gb=available_gb,
                        error=(
                            f"Model digest mismatch for {self.settings.name}; "
                            f"expected {_normalized_digest(expected_digest)}"
                        ),
                    )
                return ModelHealth(
                    service_available=True,
                    model_available=True,
                    endpoint=self.settings.endpoint,
                    identity=identity,
                    available_memory_gb=available_gb,
                )
            return ModelHealth(
                service_available=True,
                model_available=False,
                endpoint=self.settings.endpoint,
                available_memory_gb=available_gb,
                error=f"Model {self.settings.name} is not installed",
            )
        except (LocalModelError, OSError, ValueError) as error:
            return ModelHealth(
                service_available=False,
                model_available=False,
                endpoint=self.settings.endpoint,
                available_memory_gb=available_gb,
                error=str(error),
            )

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
        retry_count = self.settings.max_retries if repair_attempts is None else repair_attempts
        if retry_count not in {0, 1}:
            raise ValueError("repair_attempts must be zero or one")
        failure: Exception | None = None
        repair = ""
        for attempt in range(retry_count + 1):
            started = time.perf_counter()
            payload = {
                "model": self.settings.name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt + repair},
                ],
                "format": response_model.model_json_schema(),
                "stream": False,
                "think": self.settings.thinking_enabled,
                "keep_alive": "5m",
                "options": {
                    "temperature": self.settings.temperature,
                    "num_ctx": self.settings.context_tokens,
                    "num_predict": max_output_tokens or self.settings.max_output_tokens,
                },
            }
            try:
                response = self._request_json(
                    "POST",
                    "/api/chat",
                    payload,
                    timeout_seconds=timeout_seconds,
                )
                self._last_generation = _generation_metrics(
                    response,
                    total_seconds=max(0.0, time.perf_counter() - started),
                )
                message = response.get("message") if isinstance(response, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str):
                    raise LocalModelError("Ollama response did not contain text")
                return response_model.model_validate_json(content)
            except LocalModelError:
                raise
            except (ValidationError, ValueError, TypeError) as error:
                failure = error
                if attempt < retry_count:
                    repair = (
                        "\nThe prior response failed strict validation. Return only one JSON "
                        "object matching the supplied schema; do not add fields or prose."
                    )
        raise LocalModelError("Local model output failed strict schema validation") from failure

    def stream_chat(
        self,
        message: str,
        *,
        system_prompt: str,
        history: list[dict[str, str]] | None = None,
        worked_examples: bool = True,  # this legacy runtime never adds worked examples
        max_output_tokens: int | None = None,
    ) -> Iterator[str]:
        payload = {
            "model": self.settings.name,
            "messages": [
                {"role": "system", "content": system_prompt},
                *[item for item in (history or []) if item.get("role") in {"user", "assistant"}],
                {"role": "user", "content": message},
            ],
            "stream": True,
            "think": self.settings.thinking_enabled,
            "keep_alive": "5m",
            "options": {
                "temperature": 0.2,
                "num_ctx": self.settings.context_tokens,
                "num_predict": chat_output_limit(self.settings, max_output_tokens),
            },
        }
        request = self._request("POST", "/api/chat", payload)
        started = time.perf_counter()
        completed = False
        self._last_generation = None
        try:
            with urllib.request.urlopen(  # noqa: S310 - endpoint is validated loopback
                request,
                timeout=self.settings.stream_timeout_seconds,
            ) as response:
                for line in response:
                    if not line.strip():
                        continue
                    value = json.loads(line.decode("utf-8"))
                    if not isinstance(value, dict):
                        continue
                    if value.get("error"):
                        raise LocalModelError(str(value["error"]))
                    part = value.get("message")
                    content = part.get("content") if isinstance(part, dict) else None
                    if isinstance(content, str) and content:
                        yield content
                    if value.get("done") is True:
                        completed = True
                        self._last_generation = _generation_metrics(
                            value,
                            total_seconds=max(0.0, time.perf_counter() - started),
                        )
                        break
                if not completed:
                    raise LocalModelError(
                        "Ollama stream ended before completion; retry the response"
                    )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise LocalModelError(f"Ollama streaming request failed: {error}") from error

    def unload(self) -> None:
        self._request_json(
            "POST",
            "/api/generate",
            {"model": self.settings.name, "keep_alive": 0},
        )

    def capture_attestation(self) -> ModelAttestation:
        health = self.health()
        if not health.model_available or health.identity is None:
            raise LocalModelError(health.error or "The pinned local model is unavailable")
        ollama = probe_tool("ollama")
        return ModelAttestation(
            captured_at=datetime.now(UTC),
            identity=health.identity,
            ollama_version=ollama.version,
            endpoint=self.settings.endpoint,
            context_tokens=self.settings.context_tokens,
            max_output_tokens=self.settings.max_output_tokens,
            temperature=self.settings.temperature,
            thinking_enabled=self.settings.thinking_enabled,
            last_generation=self.last_generation,
        )

    def write_attestation(self, path: str | Path) -> ModelAttestation:
        attestation = self.capture_attestation()
        target = Path(path).expanduser().resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            attestation.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return attestation

    def wait_until_ready(self, timeout_seconds: float = 30.0) -> ModelHealth:
        deadline = time.monotonic() + timeout_seconds
        latest = self.health()
        while not latest.service_available and time.monotonic() < deadline:
            time.sleep(0.25)
            latest = self.health()
        return latest

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        request = self._request(method, path, payload)
        try:
            with urllib.request.urlopen(  # noqa: S310 - endpoint is validated loopback
                request,
                timeout=timeout_seconds or self.settings.request_timeout_seconds,
            ) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise LocalModelError(f"Ollama request failed: {error}") from error
        if not isinstance(value, dict):
            raise LocalModelError("Ollama returned a non-object response")
        if value.get("error"):
            raise LocalModelError(str(value["error"]))
        return value

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> urllib.request.Request:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        return urllib.request.Request(  # noqa: S310 - endpoint is validated loopback
            f"{self.settings.endpoint}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )


def _normalized_digest(value: str) -> str:
    normalized = value.strip().casefold()
    return normalized if normalized.startswith("sha256:") else f"sha256:{normalized}"


def _model_provenance(name: str) -> tuple[str, str]:
    if name.casefold().startswith("phi4-mini"):
        return "MIT", "https://huggingface.co/microsoft/Phi-4-mini-instruct"
    return "Apache-2.0", "https://huggingface.co/Qwen/Qwen3.5-4B"


def _parsed_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _generation_metrics(
    response: dict[str, Any],
    *,
    total_seconds: float,
) -> GenerationMetrics:
    prompt_tokens = response.get("prompt_eval_count")
    generated_tokens = response.get("eval_count")
    duration = response.get("eval_duration")
    generation_seconds = (
        float(duration) / 1_000_000_000 if isinstance(duration, (int, float)) else 0.0
    )
    token_count = int(generated_tokens) if isinstance(generated_tokens, int) else 0
    return GenerationMetrics(
        prompt_tokens=int(prompt_tokens) if isinstance(prompt_tokens, int) else 0,
        generated_tokens=token_count,
        generation_seconds=round(generation_seconds, 3),
        total_seconds=round(total_seconds, 3),
        tokens_per_second=(
            round(token_count / generation_seconds, 2) if generation_seconds > 0 else 0.0
        ),
    )
