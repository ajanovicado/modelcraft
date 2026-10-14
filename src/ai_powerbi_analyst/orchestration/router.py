"""Fail-closed, rule-first intent routing.

The common and safety-sensitive routes stay deterministic. A local structured
model is used only when the message remains genuinely ambiguous.
"""

from __future__ import annotations

import re
from typing import Protocol

from ai_powerbi_analyst.contracts import RouteDecision, RouteIntent


class ModelRouter(Protocol):
    def route(self, message: str) -> RouteDecision: ...


class RuleFirstRouter:
    """Classify supported requests while keeping unsafe actions fail closed."""

    _UNSUPPORTED = re.compile(
        r"(?:\b(?:dax|power\s*query|m\s+code|relationships?|rls|row.level.security|"
        r"drop\s+table|raw\s+sql)\b|"
        r"\bdelete\s+(?:the\s+)?(?:semantic\s+)?(?:model|dataset|table)\b|"
        r"\bpublish\b.{0,45}\bpower\s*bi\s+service\b|"
        r"\b(?:prove|claim)\b.{0,45}\bcaused?\b|"
        r"(?:^|\s)(?:/[a-z]+/|[a-z]:\\)[^\s]+)",
        re.IGNORECASE,
    )
    _ADD_EVIDENCE = re.compile(
        r"\b(?:add|put|place|send)\b.{0,45}\b(?:evidence|insight|finding|"
        r"this\s+(?:chart|analysis|result))\b|\badd\s+to\s+(?:the\s+)?(?:report|page)\b",
        re.IGNORECASE,
    )
    _BUILD = re.compile(
        r"\b(?:build|create|generate|make|prepare|lay\s+out)\b.{0,90}"
        r"\b(?:dashboard|report|pages?|executive|leadership|client\s+demo|"
        r"overview|margin\s+drivers)\b",
        re.IGNORECASE,
    )
    _MODIFY = re.compile(
        r"\b(?:modify|update|change|rename|replace|move|resize|remove|refresh|apply)\b.{0,80}"
        r"\b(?:visuals?|charts?|pages?|dashboard|report|title|filter|slicer|layout|"
        r"preset|recipe|matrix)\b",
        re.IGNORECASE,
    )
    _SCENARIO = re.compile(
        r"\b(?:scenarios?|what[ -]?if|elasticity)\b|"
        r"\b(?:approved|governed|mechanical|under|at)\b.{0,35}\bprice\s+"
        r"(?:increase|decrease|change)\b",
        re.IGNORECASE,
    )
    _FORECAST = re.compile(
        r"\b(?:forecasts?|outlook|projections?|next\s+(?:month|quarter|year)|predict)\b",
        re.IGNORECASE,
    )
    _EXPLAIN = re.compile(
        r"\b(?:explain|definition|defined|calculated|formula|walk\s+me\s+through|"
        r"what\s+(?:exactly\s+)?does|what\s+is\s+the\s+purpose|what\s+filters\s+are\s+supported|"
        r"why\s+is\s+the\s+approved\s+scenario)\b",
        re.IGNORECASE,
    )
    _AMBIGUOUS = re.compile(
        r"^(?:how\s+is\s+.+\s+doing|show\s+me\s+performance\s+by\s+\w+|"
        r"compare\s+sales|change\s+the\s+chart|what\s+changed\s+recently|"
        r"add\s+it\s+to\s+the\s+report)[?.!]*$",
        re.IGNORECASE,
    )
    _ANALYZE = re.compile(
        r"\b(?:why|compare|trend|top|bottom|rank|driver|contribut|performance|"
        r"revenue|sales|margin|profit|orders|units|cost|discount|selling\s+price)\b",
        re.IGNORECASE,
    )

    def __init__(self, model_fallback: ModelRouter | None = None) -> None:
        self._model_fallback = model_fallback

    def route(self, message: str) -> RouteDecision:
        normalized = " ".join(message.split())
        if not normalized:
            return self._clarify("Tell me what you want to build, analyze, or explain.")

        deterministic: tuple[tuple[re.Pattern[str], RouteIntent, str], ...] = (
            (
                self._UNSUPPORTED,
                RouteIntent.UNSUPPORTED,
                "The request changes an unsupported layer.",
            ),
            (
                self._ADD_EVIDENCE,
                RouteIntent.ADD_EVIDENCE,
                "The request adds the last grounded evidence to the report.",
            ),
            (self._BUILD, RouteIntent.BUILD, "The request creates a supported report recipe."),
            (
                self._AMBIGUOUS,
                RouteIntent.CLARIFY,
                "The request does not identify enough governed objects or context.",
            ),
            (self._MODIFY, RouteIntent.MODIFY, "The request changes a supported report object."),
            (
                self._EXPLAIN,
                RouteIntent.EXPLAIN,
                "The request asks about report meaning or lineage.",
            ),
            (self._SCENARIO, RouteIntent.SCENARIO, "The request uses the governed price scenario."),
            (self._FORECAST, RouteIntent.FORECAST, "The request asks for the approved forecast."),
            (self._ANALYZE, RouteIntent.ANALYZE, "The request asks a numerical business question."),
        )
        for pattern, intent, reason in deterministic:
            if pattern.search(normalized):
                confidence = 1.0 if intent is RouteIntent.UNSUPPORTED else 0.97
                if intent is RouteIntent.CLARIFY:
                    return RouteDecision(
                        intent=intent,
                        confidence=confidence,
                        missing_information=[
                            "Name the metric, comparison or page, and the period you want."
                        ],
                        reason=reason,
                    )
                return RouteDecision(intent=intent, confidence=confidence, reason=reason)

        if self._model_fallback is None:
            return self._clarify("Specify whether you want to build, analyze, or explain.")

        try:
            decision = self._model_fallback.route(normalized)
        except Exception:  # noqa: BLE001 - model failures clarify, never execute
            return self._clarify("I could not safely determine the requested action.")

        if decision.confidence < 0.65:
            return self._clarify("The request is ambiguous; choose build, analyze, or explain.")
        return decision

    @staticmethod
    def _clarify(message: str) -> RouteDecision:
        return RouteDecision(
            intent=RouteIntent.CLARIFY,
            confidence=1.0,
            missing_information=[message],
            reason="More information is required before taking action.",
        )
