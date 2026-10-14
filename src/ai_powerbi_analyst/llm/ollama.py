"""Strict structured-output adapter for a loopback Ollama service."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

from ai_powerbi_analyst.settings import ModelSettings

StructuredModel = TypeVar("StructuredModel", bound=BaseModel)


class InvalidModelResponse(ValueError):
    """Ollama returned content that does not satisfy the closed contract."""


class OllamaClientProtocol(Protocol):
    def chat(self, **kwargs: Any) -> Any: ...


# Compatibility export: the application has one canonical model settings contract.
OllamaSettings = ModelSettings


class OllamaStructuredClient:
    def __init__(
        self,
        settings: ModelSettings | None = None,
        client: OllamaClientProtocol | None = None,
    ) -> None:
        self.settings = settings or ModelSettings()
        if client is None:
            from ollama import Client

            self._client = cast(
                OllamaClientProtocol,
                Client(
                    host=self.settings.endpoint,
                    timeout=self.settings.request_timeout_seconds,
                ),
            )
        else:
            self._client = client

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredModel],
    ) -> StructuredModel:
        last_error: Exception | None = None
        for _attempt in range(self.settings.max_retries + 1):
            try:
                response = self._client.chat(
                    model=self.settings.name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    format=response_model.model_json_schema(),
                    options={
                        "temperature": self.settings.temperature,
                        "num_ctx": self.settings.context_tokens,
                        "num_predict": self.settings.max_output_tokens,
                    },
                    keep_alive="5m",
                    stream=False,
                )
                content = response.message.content
                if not isinstance(content, str):
                    raise TypeError("model response content is not text")
                return response_model.model_validate_json(content)
            except (AttributeError, TypeError, ValueError, ValidationError) as exc:
                last_error = exc
        raise InvalidModelResponse("model output failed closed-schema validation") from last_error
