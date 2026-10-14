"""Catalog-grounded explanations for the prepared local solution."""

from __future__ import annotations

from ai_powerbi_analyst.contracts import MetricDefinition, SemanticCatalog
from ai_powerbi_analyst.orchestration.conversation import ConversationState


class GroundedExplainer:
    """Explain governed objects without inventing formulas or report metadata."""

    _PAGE_EXPLANATIONS = {
        "executive pulse": (
            "Executive Pulse is the prepared overview page: governed KPI cards, the monthly "
            "trend, regional performance, and approved slicers."
        ),
        "margin drivers": (
            "Margin Drivers is the prepared diagnostic page: the governed contribution "
            "bridge, ranked detail, and one replaceable conversational-evidence slot."
        ),
        "outlook": (
            "Outlook & Decisions uses the frozen six-month forecast and the governed price "
            "scenario. Forecast uncertainty and mechanical scenario assumptions stay separate."
        ),
    }

    def __init__(self, catalog: SemanticCatalog) -> None:
        self.catalog = catalog

    def explain(self, message: str, state: ConversationState) -> str:
        normalized = " ".join(message.split()).casefold()
        metric = self._mentioned_metric(normalized)
        if metric is not None:
            dependencies = [
                self.catalog.resolve_metric(identifier).name for identifier in metric.dependencies
            ]
            dependency_text = (
                f" Its governed dependencies are {', '.join(dependencies)}." if dependencies else ""
            )
            return (
                f"{metric.name}: {metric.description} "
                f"The prepared Power BI binding is {metric.powerbi_ref}. "
                f"The read-only analytical mirror uses {metric.sql_expression}."
                f"{dependency_text} This explanation comes from semantic catalog "
                f"{self.catalog.version}; no formula was generated."
            )

        for phrase, explanation in self._PAGE_EXPLANATIONS.items():
            if phrase in normalized:
                return explanation

        if "scenario" in normalized or "elasticity" in normalized:
            return (
                "The approved price scenario is a precomputed mechanical sensitivity table. "
                "It applies the frozen price-change grid and elasticity assumption; it is not "
                "a forecast or causal prediction."
            )
        if "forecast" in normalized:
            return (
                "The outlook uses the frozen, backtested six-month forecast stored with the "
                "prepared fixture. The model does not fit or revise a forecast during the demo."
            )
        if state.last_evidence is not None:
            evidence = state.last_evidence
            return (
                f"The latest answer is grounded in data hash {evidence.data_hash[:12]}… and "
                f"query-plan hash {evidence.query_plan_hash[:12]}… for "
                f"{evidence.comparison_period}."
            )
        return (
            "This workspace explains only governed metrics, the three prepared report pages, "
            "the approved forecast, and the approved price scenario."
        )

    def _mentioned_metric(self, normalized_message: str) -> MetricDefinition | None:
        candidates: list[tuple[str, MetricDefinition]] = []
        for metric in self.catalog.metrics:
            for label in (metric.id, metric.name, *metric.aliases):
                normalized_label = label.replace("_", " ").casefold()
                candidates.append((normalized_label, metric))
        for label, metric in sorted(candidates, key=lambda item: len(item[0]), reverse=True):
            if label in normalized_message:
                return metric
        return None
