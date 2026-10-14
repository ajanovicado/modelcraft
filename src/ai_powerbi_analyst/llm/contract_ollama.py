"""Request-bound Qwen artifacts; frozen baseline runtime remains available."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any, ClassVar

from ai_powerbi_analyst.contracts import ClosedModel

from .artifact_checks import declared_sql_tables
from .artifact_contracts import (
    RelationshipAnswer,
    dax_problems,
    references,
    relationship_problems,
    requested_measure_name,
)
from .artifact_policy import domain_for
from .runtime import StructuredOutput, chat_output_limit
from .sql_contracts import sql_binding_problems
from .warm_ollama import WarmOllamaModelRuntime

_CHAT_HISTORY: ContextVar[list[dict[str, str]] | None] = ContextVar(
    "artifact_chat_history", default=None
)


def _single_relationship_request(prompt: str) -> bool:
    # Rich explanations and multi-relationship plans need their original response.
    return (
        domain_for(prompt) == "model"
        and len(references(prompt)) == 2
        and bool(re.search(r"\b(?:give|create|return|provide)\b.*\brelationship\b", prompt, re.I))
        and not re.search(r"\b(?:explain|why|how|steps|compare|relationships)\b", prompt, re.I)
    )


class ArtifactAnswer(ClosedModel):
    artifact: str


class _ArtifactAnswerWithoutExamples(ArtifactAnswer):
    # Honours stream_chat(worked_examples=False) on the buffered artifact path too.
    worked_examples: ClassVar[bool] = False


class ContractOllamaModelRuntime(WarmOllamaModelRuntime):
    def _relationship_response_model(self, prompt: str) -> type[RelationshipAnswer]:
        return RelationshipAnswer

    def _messages(
        self,
        system_prompt: str,
        user_prompt: str,
        refs: str,
        history: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        return super()._messages(
            system_prompt,
            user_prompt,
            refs,
            history if history is not None else _CHAT_HISTORY.get(),
        )

    def _validate_response(self, prompt: str, value: StructuredOutput) -> None:
        domain = domain_for(prompt)
        super()._validate_response(prompt, value)
        artifact = getattr(value, "artifact", None)
        if isinstance(value, RelationshipAnswer):
            errors = relationship_problems(prompt, value)
        elif isinstance(artifact, str) and domain == "dax":
            errors = dax_problems(prompt, artifact)
        elif isinstance(artifact, str) and domain == "sql":
            errors = sql_binding_problems(prompt, artifact)
        else:
            errors = []
        if errors:
            raise ValueError("; ".join(errors))

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredOutput],
        **kwargs: Any,
    ) -> StructuredOutput:
        single_artifact = set(response_model.model_fields) == {"artifact"}
        if single_artifact and _single_relationship_request(user_prompt):
            value = super().generate(
                system_prompt + "\nFor this relationship, the JSON schema replaces the artifact "
                "string: return its five fields. from_ref and to_ref must each be a complete "
                "Table[Column] reference, never a table name alone. "
                "from_to filters from the first to the second, "
                "to_from does the reverse. One-to-one uses both. Include is_active explicitly. "
                "No UI instructions or additional relationships.",
                user_prompt,
                self._relationship_response_model(user_prompt),
                **kwargs,
            )
            if not isinstance(value, RelationshipAnswer):
                raise ValueError("Relationship generation returned an unexpected response type")
            return response_model.model_validate({"artifact": value.render()})
        return super().generate(system_prompt, user_prompt, response_model, **kwargs)

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
        # Only self-contained artifact requests use buffered validation.
        # Discussion and context-dependent followups retain ordinary streaming.
        explicit = re.match(
            r"\s*(?:please\s+)?(?:write|create|generate|give|return|provide)\b", message, re.I
        ) or re.search(r"\bReturn only\b.*\bSQL\s+SELECT\b", message, re.I)
        domain = domain_for(message)
        self_contained = explicit and (
            (domain == "dax" and bool(references(message)) and requested_measure_name(message))
            or _single_relationship_request(message)
            or (domain == "m" and "#table" in message)
            or (domain == "sql" and bool(declared_sql_tables(message)))
        )
        if self_contained:
            token = _CHAT_HISTORY.set(history)
            try:
                answer = self.generate(
                    system_prompt,
                    message,
                    ArtifactAnswer if worked_examples else _ArtifactAnswerWithoutExamples,
                    max_output_tokens=output_limit,
                )
            finally:
                _CHAT_HISTORY.reset(token)
            yield answer.artifact
        else:
            yield from super().stream_chat(
                message,
                system_prompt=system_prompt,
                history=history,
                worked_examples=worked_examples,
                max_output_tokens=output_limit,
            )
