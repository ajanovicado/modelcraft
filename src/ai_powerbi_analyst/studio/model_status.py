"""Model metadata and safe, actionable messages for known runtime failures."""

from __future__ import annotations

import re


def is_model_identity_request(message: str) -> bool:
    """Match standalone identity questions, never data-model or mixed requests."""
    text = " ".join(message.casefold().split()).strip(" ?.!")
    return bool(
        re.fullmatch(
            r"(?:what|which) (?:ai |language )?(?:model|llm) "
            r"(?:are you(?: (?:using|running))?|do you use|is this)|"
            r"what(?: is|'s) your (?:ai |language )?(?:model|llm)",
            text,
        )
    )


def model_failure_message(detail: str) -> str:
    """Translate only recognized runtime diagnostics; keep internal details private.

    Free RAM, disk, power and paging never pause the local Qwen 4B model, so no
    message blames them; anything unrecognized points to Settings diagnostics.
    """
    if "context bound exceeded" in detail.lower() or "context budget" in detail.lower():
        return (
            "This request is too large for the local model. "
            "Select one table or shorten the request and retry."
        )
    if detail == "Local model is busy; only one guarded request is allowed":
        return "Local AI is already handling a request. Wait for it to finish and retry."
    return (
        "Local AI could not complete this response. Check model diagnostics in Settings and retry."
    )
