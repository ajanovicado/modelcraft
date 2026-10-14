"""Compile common business questions into the closed AnalysisPlan contract."""

from __future__ import annotations

import calendar
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Literal

from ai_powerbi_analyst.contracts import (
    AnalysisOperation,
    AnalysisPlan,
    SemanticCatalog,
    TimeWindow,
)


class AnalysisPlanningError(ValueError):
    """A request cannot be represented without changing its stated meaning."""


_YEAR_PATTERN = r"(?:19|20)\d{2}"
_MONTH_NUMBERS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_MONTH_PATTERN = "(?:" + "|".join(sorted(_MONTH_NUMBERS, key=len, reverse=True)) + ")"
_QUARTER_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}


class RuleFirstAnalysisPlanner:
    """A narrow deterministic planner for the approved analysis families."""

    def __init__(
        self,
        catalog: SemanticCatalog,
        *,
        dimension_values: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self.catalog = catalog
        configured_values = dimension_values or {
            "region": ("West", "East", "Central", "South"),
            "segment": ("Enterprise", "Mid-Market", "SMB"),
            "channel": ("Direct", "Partner", "Online"),
            "category": ("Devices", "Infrastructure", "Software", "Services"),
            "product": (
                "Pulse Mini",
                "Pulse Pro",
                "Atlas Core",
                "Atlas Edge",
                "Cloud Start",
                "Cloud Scale",
                "Care Plus",
                "Care Premier",
            ),
        }
        governed_dimensions = {dimension.id for dimension in catalog.dimensions}
        self.dimension_values = {
            key: values for key, values in configured_values.items() if key in governed_dimensions
        }

    def plan(self, message: str) -> AnalysisPlan:
        clean = " ".join(message.split())
        lowered = clean.casefold()
        self._reject_unsupported_metric_terms(lowered)
        self._reject_unsupported_dimension_terms(lowered)
        self._reject_unsupported_filter_operators(lowered)
        operation = self._operation(lowered)
        explicit_metric_ids = self._matched_metrics(lowered)
        if (
            operation is AnalysisOperation.COMPARISON
            and "net_sales" in explicit_metric_ids
            and re.search(
                r"\b(?:net\s+sales|revenue|sales)\s+(?:growth|grow|grew|grown)\b",
                lowered,
            )
        ):
            try:
                self.catalog.resolve_metric("sales_growth")
            except KeyError:
                pass
            else:
                explicit_metric_ids = [
                    "sales_growth" if metric_id == "net_sales" else metric_id
                    for metric_id in explicit_metric_ids
                ]
        if not explicit_metric_ids and operation not in {
            AnalysisOperation.DRIVER_BRIDGE,
            AnalysisOperation.FORECAST,
            AnalysisOperation.SCENARIO,
        }:
            raise AnalysisPlanningError(
                "Name at least one approved metric: Net Sales, Units, Gross Profit, "
                "Gross Margin %, Average Selling Price, Discount Rate, Unit Cost, or "
                "Sales Growth."
            )
        metric_ids = explicit_metric_ids or self._default_metrics(operation)
        dimension_ids = self._dimensions(lowered)
        filters = self._filters(clean)
        time_window = self._time_window(lowered)
        self._reject_mixed_filter_values(clean, filters)
        self._reject_unresolved_filter_intent(clean, filters)
        self._reject_unresolved_semantic_conjunctions(
            lowered,
            operation=operation,
            has_approved_metric=bool(explicit_metric_ids),
        )
        self._reject_unresolved_grouping_terms(lowered, operation=operation)
        comparison = self._comparison(lowered)

        ranking_match = re.search(r"\b(?:top|bottom)\s+(\d+)\b", lowered)
        word_limits = {"five": 5, "ten": 10}
        word_match = re.search(r"\b(?:top|bottom)\s+(five|ten)\b", lowered)
        if ranking_match:
            ranking_limit = int(ranking_match.group(1))
            if not 1 <= ranking_limit <= 25:
                raise AnalysisPlanningError(
                    "Top/bottom ranking supports a limit from 1 through 25."
                )
        elif word_match:
            ranking_limit = word_limits[word_match.group(1)]
        else:
            ranking_limit = 10
        price_change = (
            self._price_change(lowered) if operation is AnalysisOperation.SCENARIO else None
        )
        if re.search(r"\b(?:top|highest|most)\b", lowered) and re.search(
            r"\b(?:bottom|lowest|least)\b", lowered
        ):
            raise AnalysisPlanningError(
                "Ask for either the highest or the lowest ranking in one question, not both."
            )
        sort_direction: Literal["asc", "desc"] = (
            "asc" if re.search(r"\b(?:bottom|lowest|least)\b", lowered) else "desc"
        )

        # Normalize to the exact capability sets enforced again by the SQL compiler.
        if operation is AnalysisOperation.DRIVER_BRIDGE:
            if "why" in lowered:
                focus = lowered.rsplit("why", maxsplit=1)[-1]
            else:
                focus = re.split(r"\b(?:into|by)\b", lowered, maxsplit=1)[0]
            focused_metric_ids = self._matched_metrics(focus)
            unsupported_driver_metrics = set(focused_metric_ids) - {
                "gross_profit",
                "gross_margin_pct",
            }
            if unsupported_driver_metrics:
                names = ", ".join(sorted(unsupported_driver_metrics))
                raise AnalysisPlanningError(
                    "The governed driver bridge decomposes Gross Profit only; "
                    f"unsupported requested metric(s): {names}."
                )
            unsupported_driver_dimensions = [
                dimension_id
                for dimension_id in dimension_ids
                if not (
                    dimension_id == "region"
                    and "region" in filters
                    and not self._is_explicit_grouping(lowered, dimension_id)
                )
                and not (
                    dimension_id == "month"
                    and not self._is_explicit_grouping(lowered, dimension_id)
                )
            ]
            if unsupported_driver_dimensions:
                self._raise_unsupported_grouping("driver bridge", unsupported_driver_dimensions)
            if time_window.preset != "latest_quarter":
                raise AnalysisPlanningError(
                    "The governed driver bridge supports only the latest complete quarter."
                )
            metric_ids = ["gross_profit"]
            dimension_ids = []
            unsupported_filters = set(filters) - {"region"}
            if unsupported_filters:
                raise AnalysisPlanningError(
                    "The governed driver bridge supports only an optional Region filter."
                )
            if comparison != "prior_year":
                raise AnalysisPlanningError(
                    "The frozen driver bridge compares the latest complete quarter with "
                    "the same quarter last year; prior-quarter driver analysis is not prepared."
                )
        elif operation is AnalysisOperation.FORECAST:
            unsupported_forecast_dimensions = [
                dimension_id for dimension_id in dimension_ids if dimension_id != "month"
            ]
            if unsupported_forecast_dimensions:
                self._raise_unsupported_grouping("forecast", unsupported_forecast_dimensions)
            if time_window.preset != "latest_quarter":
                raise AnalysisPlanningError(
                    "The approved six-month forecast does not support a requested date or period."
                )
            unsupported_metrics = set(explicit_metric_ids) - {
                "net_sales",
                "gross_profit",
            }
            if unsupported_metrics:
                names = ", ".join(sorted(unsupported_metrics))
                raise AnalysisPlanningError(
                    "The approved forecast supports only Net Sales and Gross Profit; "
                    f"unsupported requested metric(s): {names}."
                )
            metric_ids = explicit_metric_ids or ["net_sales", "gross_profit"]
            dimension_ids = []
            if filters:
                raise AnalysisPlanningError("The approved forecast does not support filters.")
        elif operation is AnalysisOperation.SCENARIO:
            if dimension_ids:
                self._raise_unsupported_grouping("price scenario", dimension_ids)
            if time_window.preset != "latest_quarter":
                raise AnalysisPlanningError(
                    "The approved next-quarter scenario does not support a requested "
                    "date or period."
                )
            metric_ids = ["net_sales", "units", "gross_profit", "gross_margin_pct"]
            dimension_ids = []
            if filters:
                raise AnalysisPlanningError("The approved price scenario does not support filters.")
        elif operation in {AnalysisOperation.KPI, AnalysisOperation.COMPARISON}:
            # Temporal words such as "latest month" and "prior period" describe the
            # time window/comparison; they are not an implicit request to group rows.
            # Likewise, a governed label/value pair such as "Region West" names a
            # filter, not a grouping request. Explicit "by/across/per" language still
            # wins and is rejected below for scalar KPI/comparison evidence.
            dimension_ids = [
                value
                for value in dimension_ids
                if value != "month"
                and not (value in filters and not self._is_explicit_grouping(lowered, value))
            ]
            if dimension_ids:
                raise AnalysisPlanningError(
                    "This question includes a grouping dimension; ask for a top/bottom ranking, "
                    "trend, or contribution analysis so its meaning is explicit."
                )
            if operation is AnalysisOperation.COMPARISON and len(metric_ids) != 1:
                raise AnalysisPlanningError(
                    "Comparison evidence supports exactly one metric so unlike units are "
                    "never combined on one chart axis."
                )
        elif operation is AnalysisOperation.TREND:
            if re.search(r"\b(?:weekly|daily|by\s+week|by\s+day)\b", lowered):
                raise AnalysisPlanningError("The prepared trend supports monthly time grain only.")
            dimension_ids = [value for value in dimension_ids if value != "month"]
            if len(metric_ids) != 1:
                raise AnalysisPlanningError(
                    "Trend analysis supports exactly one metric so unlike units are never "
                    "combined on one evidence axis."
                )
            if len(dimension_ids) > 1:
                raise AnalysisPlanningError(
                    "Trend analysis supports at most one grouping dimension."
                )
            expected_rows = self._trend_cardinality(
                metric_ids,
                dimension_ids,
                time_window,
            )
            if expected_rows > 200:
                raise AnalysisPlanningError(
                    "That trend would exceed the 200-row governed evidence limit. "
                    "Request fewer metrics, a shorter period, or no additional grouping."
                )
        elif operation is AnalysisOperation.RANKING:
            if len(metric_ids) != 1 or len(dimension_ids) != 1:
                raise AnalysisPlanningError(
                    "Top/bottom ranking requires exactly one approved metric and one dimension."
                )
        elif operation is AnalysisOperation.CONTRIBUTION:
            additive_metrics = {"net_sales", "units", "gross_profit"}
            if not set(metric_ids).issubset(additive_metrics):
                raise AnalysisPlanningError(
                    "Contribution analysis supports additive Net Sales, Units, or Gross Profit."
                )
            if len(metric_ids) != 1 or len(dimension_ids) != 1:
                raise AnalysisPlanningError(
                    "Contribution analysis requires exactly one additive metric and one dimension."
                )

        return AnalysisPlan(
            operation=operation,
            metric_ids=metric_ids,
            dimension_ids=dimension_ids,
            time_window=time_window,
            comparison=comparison,
            filters=filters,
            ranking_limit=ranking_limit,
            sort_direction=sort_direction,
            scenario_price_change_pct=price_change,
        )

    @staticmethod
    def _reject_unsupported_metric_terms(message: str) -> None:
        unsupported = re.search(
            r"\b(?:orders?|order\s+count|customer\s+count|ebitda?|arr|"
            r"annual\s+recurring\s+revenue|customer\s+acquisition\s+cost|cac|"
            r"cash\s+flow|bookings?|customer\s+satisfaction|taxes?|returns?|"
            r"net\s+(?:profit|margin)|operating\s+(?:profit|margin)|inventory|churn)\b",
            message,
        )
        if unsupported:
            raise AnalysisPlanningError(
                f"{unsupported.group(0).title()} is not an approved metric in this workspace."
            )

    @staticmethod
    def _reject_unsupported_filter_operators(message: str) -> None:
        if re.search(r"\b(?:excluding|except|without|not\s+in)\b", message):
            raise AnalysisPlanningError(
                "Exclusion and negated filters are not supported by the prepared equality-only "
                "filter contract."
            )

    @staticmethod
    def _reject_unsupported_dimension_terms(message: str) -> None:
        unsupported = re.search(
            r"\b(?:city|cities|brands?|skus?|markets?|countries|country|states?|"
            r"salespersons?|salespeople)\b",
            message,
        )
        if (
            unsupported
            and unsupported.group(0) == "state"
            and re.search(
                r"\bstate\s+(?:the|an?|our|its|elasticity|assumption|result|finding)",
                message,
            )
        ):
            unsupported = None
        if unsupported:
            raise AnalysisPlanningError(
                f"{unsupported.group(0).title()} is not an approved grouping dimension in "
                "this workspace."
            )

    def _approved_semantic_phrases(self) -> set[str]:
        phrases: set[str] = set()
        for metric in self.catalog.metrics:
            for label in (metric.id, metric.name, *metric.aliases):
                normalized = " ".join(
                    label.casefold().replace("_", " ").replace("%", " percent ").split()
                )
                phrases.add(normalized)
                if normalized.endswith(" percent"):
                    phrases.add(normalized.removesuffix(" percent"))
        phrases.update(
            " ".join(label.casefold().replace("_", " ").split())
            for dimension in self.catalog.dimensions
            for label in (dimension.id, dimension.name, *dimension.aliases)
        )
        phrases.update(
            value.casefold() for values in self.dimension_values.values() for value in values
        )
        return phrases

    @staticmethod
    def _candidate_core(value: str) -> str:
        candidate = re.sub(r"^(?:the|a|an)\s+", "", value.strip().casefold())
        candidate = re.split(
            r"\s+(?:last|latest|most\s+recent|prior|previous|current|next|"
            r"this|over|during|under|with|where|for|in|by|from|between)\b",
            candidate,
            maxsplit=1,
        )[0]
        candidate = re.sub(
            r"\s+(?:ascending|descending|change|contribution|values?|amounts?|"
            r"trend|forecast|outlook)$",
            "",
            candidate,
        )
        return " ".join(re.findall(r"[a-z0-9%+-]+", candidate))

    def _reject_unresolved_semantic_conjunctions(
        self,
        message: str,
        *,
        operation: AnalysisOperation,
        has_approved_metric: bool,
    ) -> None:
        """Never answer only the recognized half of an explicit semantic list."""

        if not has_approved_metric:
            return
        approved = self._approved_semantic_phrases()
        continuation = re.compile(
            r"^(?:identify|state|explain|show|tell|include|report|describe|list|"
            r"rank|calculate|compare|summarize|add|what|why|how)\b"
        )
        governed_driver_terms = {
            "price",
            "volume",
            "mix",
            "discount",
            "cost",
            "price volume mix discount cost",
        }
        governed_assumption_terms = {
            "elasticity",
            "elasticity assumption",
            "uncertainty",
            "uncertainty range",
            "confidence interval",
        }
        temporal_terms = re.compile(
            r"^(?:same|prior|previous|current|next|last|latest|this)\s+"
            r"(?:month|quarter|period|year)$"
        )
        for match in re.finditer(r"\b(?:and|or)\s+([^,.?]+)", message):
            raw_candidate = match.group(1).strip()
            if continuation.match(raw_candidate):
                continue
            candidate = self._candidate_core(raw_candidate)
            if not candidate:
                continue
            if (
                candidate in approved
                or temporal_terms.fullmatch(candidate)
                or candidate in governed_assumption_terms
                or (
                    operation is AnalysisOperation.DRIVER_BRIDGE
                    and candidate in governed_driver_terms
                )
            ):
                continue
            raise AnalysisPlanningError(
                f"{candidate.title()} does not resolve to an approved metric, dimension, "
                "filter value, or governed analytical term; the request was not "
                "partially applied."
            )

    def _reject_unresolved_grouping_terms(
        self,
        message: str,
        *,
        operation: AnalysisOperation,
    ) -> None:
        approved = self._approved_semantic_phrases()
        driver_terms = {"price", "volume", "mix", "discount", "cost", "driver"}
        for match in re.finditer(
            r"\bby\s+(.+?)(?=\s+(?:for|in|over|during|where|with|under|last|"
            r"latest|prior|previous|this|next|from|between)\b|[.?]|$)",
            message,
        ):
            body = match.group(1)
            for raw_part in re.split(r"\s+(?:and|or)\s+|,", body):
                candidate = self._candidate_core(raw_part)
                if not candidate or re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", candidate):
                    continue
                if candidate in approved:
                    continue
                if operation is AnalysisOperation.DRIVER_BRIDGE and candidate in driver_terms:
                    continue
                raise AnalysisPlanningError(
                    f"{candidate.title()} is not an approved metric or grouping dimension; "
                    "the request was not partially applied."
                )

    def _reject_mixed_filter_values(
        self,
        message: str,
        filters: Mapping[str, str | int | float | bool],
    ) -> None:
        """Reject a conjunctive filter clause when any requested value is unknown."""

        if not filters:
            return
        approved_values = {
            value.casefold() for values in self.dimension_values.values() for value in values
        }
        selected_values = {str(value).casefold() for value in filters.values()}
        dimension_labels = {
            label.casefold()
            for dimension in self.catalog.dimensions
            for label in (dimension.id, dimension.name, *dimension.aliases)
        }
        suffixes = dimension_labels | {"customer", "customers", "business", "businesses"}
        clause_pattern = re.compile(
            r"\b(?:in|for)\s+(?:the\s+)?(?P<body>[^.?]*?)"
            r"(?=\s+(?:last|latest|most\s+recent|prior|previous|"
            r"over\s+(?:the\s+)?last|year\s+to\s+date|ytd)\b|[.?]|$)",
            re.IGNORECASE,
        )
        for match in clause_pattern.finditer(message):
            body = " ".join(match.group("body").split()).casefold()
            body = re.split(
                r"\s+and\s+(?:identify|state|explain|show|tell|include|report|"
                r"describe|list|rank|calculate|compare|summarize)\b",
                body,
                maxsplit=1,
            )[0]
            if not re.search(r"\b(?:and|or)\b|,", body):
                continue
            if not any(
                re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", body)
                for value in selected_values
            ):
                continue
            parts = re.split(r"\s+(?:and|or)\s+|,", body)
            for part in parts:
                words = [
                    word
                    for word in re.findall(r"[a-z0-9-]+", part)
                    if word not in {"the", "a", "an"}
                ]
                while words and words[-1] in suffixes:
                    words.pop()
                candidate = " ".join(words)
                if candidate not in approved_values:
                    raise AnalysisPlanningError(
                        "Every requested filter value must resolve to the prepared "
                        "dimension allowlists; mixed known and unknown filters are not "
                        "partially applied."
                    )

    def _reject_unresolved_filter_intent(
        self,
        message: str,
        filters: Mapping[str, str | int | float | bool],
    ) -> None:
        for dimension in self.catalog.dimensions:
            if dimension.id == "month" or dimension.id in filters:
                continue
            labels = (dimension.id, dimension.name, *dimension.aliases)
            label_pattern = "|".join(
                re.escape(label) for label in sorted(labels, key=len, reverse=True)
            )
            if re.search(
                rf"\b(?:for|where|filtered?\s+(?:to|by))\s+(?:the\s+)?"
                rf"(?:{label_pattern})\s+[^,.?]+",
                message,
                re.IGNORECASE,
            ):
                raise AnalysisPlanningError(
                    f"The requested {dimension.name} filter is not in the prepared allowlist."
                )
        if not filters.get("segment") and re.search(
            r"\b(?:for|where|filtered?\s+(?:to|by))\s+[^,.?]+\s+customers?\b",
            message,
            re.IGNORECASE,
        ):
            raise AnalysisPlanningError(
                "Customer-level filters are not supported; use an approved customer Segment."
            )
        # A short business entity between a filter preposition and a period or
        # sentence boundary is likely a filter value, even when lower-case.
        # Failing closed here prevents unknown locations such as "boston" or
        # "northeast" from becoming an accidental all-data answer.
        implicit_value = re.search(
            r"\b(?:in|for)\s+(?:the\s+)?"
            r"(?!the\b)"
            r"(?P<value>[A-Za-z][A-Za-z-]*(?:\s+[A-Za-z][A-Za-z-]*)?)"
            r"(?=\s+(?:last|latest|most\s+recent|prior|previous)\b|[,.?]|$)",
            message,
            re.IGNORECASE,
        )
        recognized_values = {str(value).casefold() for value in filters.values()}
        recognized_labeled_values: set[str] = set()
        for dimension_id, value in filters.items():
            dimension = self.catalog.resolve_dimension(dimension_id)
            for label in (dimension.id, dimension.name, *dimension.aliases):
                normalized_label = " ".join(label.casefold().replace("_", " ").split())
                normalized_value = str(value).casefold()
                recognized_labeled_values.add(f"{normalized_label} {normalized_value}")
                recognized_labeled_values.add(f"{normalized_label} is {normalized_value}")
        if implicit_value:
            candidate = implicit_value.group("value").casefold()
            normalized_candidate = re.sub(r"\s+(?:customers?|businesses?)$", "", candidate)
            approved_terms = (
                recognized_values
                | recognized_labeled_values
                | {
                    item.casefold()
                    for metric in self.catalog.metrics
                    for item in (metric.id, metric.name, *metric.aliases)
                }
            )
            temporal = re.fullmatch(
                r"(?:last|latest|prior|previous|current|next)\s+"
                r"(?:month|quarter|period|year)|most\s+recent",
                candidate,
            )
            if candidate in approved_terms or normalized_candidate in approved_terms or temporal:
                return
            raise AnalysisPlanningError(
                f"The requested filter value {implicit_value.group('value')!r} is not in "
                "the prepared dimension allowlists."
            )

    @staticmethod
    def _operation(message: str) -> AnalysisOperation:
        if re.search(
            r"\b(?:scenarios?|what[ -]?if|elasticity)\b|"
            r"\b(?:approved|governed|mechanical|under|at)\b.{0,35}"
            r"\bprice\s+(?:increase|decrease|change)\b|"
            r"\b\d{1,2}\s*(?:%|percent)\s+price\s+(?:increase|decrease|change)\b",
            message,
        ):
            return AnalysisOperation.SCENARIO
        if re.search(
            r"\b(?:forecasts?|outlook|projections?|next quarter|next month)\b",
            message,
        ):
            return AnalysisOperation.FORECAST
        if re.search(
            r"\b(?:why|drivers?|drove|decompos\w*|margin\s+(?:fall|decline|pressure)|"
            r"governed\s+margin\s+bridge)\b",
            message,
        ):
            return AnalysisOperation.DRIVER_BRIDGE
        if re.search(r"\b(?:contribut|share of|breakdown)\w*\b", message):
            return AnalysisOperation.CONTRIBUTION
        if re.search(
            r"\b(?:top|bottom|rank|lowest|highest)\b|"
            r"\bmost\s+(?:net\s+sales|revenue|sales|units|volume|gross\s+profit|"
            r"profit|gross\s+margin|margin|discount|unit\s+cost|"
            r"average\s+selling\s+price)\b",
            message,
        ):
            return AnalysisOperation.RANKING
        if re.search(
            r"\b(?:trend|over time|monthly|weekly|daily|plot|by\s+month|moved\s+over)\b",
            message,
        ):
            return AnalysisOperation.TREND
        if re.search(
            r"\b(?:compare|versus|\bvs\b|change|growth|grow|grew|grown|fell|"
            r"increase|decrease|"
            r"prior[ -](?:period|year))\b",
            message,
        ):
            return AnalysisOperation.COMPARISON
        return AnalysisOperation.KPI

    def _matched_metrics(self, message: str) -> list[str]:
        candidates: list[tuple[str, str]] = []
        for metric in self.catalog.metrics:
            for name in (metric.id, metric.name, *metric.aliases):
                candidates.append((name.casefold(), metric.id))
        matches: list[tuple[int, int, str]] = []
        for alias, metric_id in candidates:
            expression = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
            matches.extend(
                (match.start(), match.end(), metric_id)
                for match in re.finditer(expression, message)
            )
        selected: list[tuple[int, int, str]] = []
        for start, end, metric_id in sorted(
            matches,
            key=lambda item: (-(item[1] - item[0]), item[0], item[2]),
        ):
            if any(
                start < chosen_end and end > chosen_start
                for chosen_start, chosen_end, _ in selected
            ):
                continue
            selected.append((start, end, metric_id))
        found: list[str] = []
        for _start, _end, metric_id in sorted(selected):
            if metric_id not in found:
                found.append(metric_id)
        if len(found) > 4:
            raise AnalysisPlanningError("A question may request at most four approved metrics.")
        return found

    def _trend_cardinality(
        self,
        metric_ids: Sequence[str],
        dimension_ids: Sequence[str],
        time_window: TimeWindow,
    ) -> int:
        if time_window.start is not None and time_window.end is not None:
            periods = (
                (time_window.end.year - time_window.start.year) * 12
                + time_window.end.month
                - time_window.start.month
                + 1
            )
        else:
            periods = {
                "latest_month": 1,
                "latest_quarter": 3,
                "last_12_months": 12,
                "year_to_date": 12,
                "all_time": 48,
            }.get(time_window.preset or "", 48)
        groups = 1
        if dimension_ids:
            values = self.dimension_values.get(dimension_ids[0])
            groups = len(values) if values is not None else 201
        return len(metric_ids) * periods * groups

    def _default_metrics(self, operation: AnalysisOperation) -> list[str]:
        preferred = (
            "gross_margin_pct" if operation is AnalysisOperation.DRIVER_BRIDGE else "net_sales"
        )
        try:
            self.catalog.resolve_metric(preferred)
            return [preferred]
        except KeyError:
            return [self.catalog.metrics[0].id]

    def _dimensions(self, message: str) -> list[str]:
        found: list[str] = []
        for dimension in self.catalog.dimensions:
            for name in (dimension.id, dimension.name, *dimension.aliases):
                normalized_name = name.casefold()
                if normalized_name.endswith("y"):
                    singular = re.escape(normalized_name)
                    plural = re.escape(normalized_name[:-1] + "ies")
                    spelling = rf"(?:{singular}|{plural})"
                elif normalized_name.endswith("s"):
                    spelling = re.escape(normalized_name)
                else:
                    spelling = rf"{re.escape(normalized_name)}s?"
                expression = rf"(?<![a-z0-9]){spelling}(?![a-z0-9])"
                if re.search(expression, message) and dimension.id not in found:
                    found.append(dimension.id)
            if (
                dimension.id == "region"
                and re.search(r"(?<![a-z0-9])regional(?![a-z0-9])", message)
                and dimension.id not in found
            ):
                found.append(dimension.id)
        if len(found) > 3:
            raise AnalysisPlanningError(
                "A question may reference at most three approved dimensions."
            )
        return found

    def _is_explicit_grouping(self, message: str, dimension_id: str) -> bool:
        dimension = self.catalog.resolve_dimension(dimension_id)
        labels = (dimension.id, dimension.name, *dimension.aliases)
        if dimension_id == "region":
            labels = (*labels, "regional")
        spellings = "|".join(
            re.escape(label.casefold()) + r"s?" for label in sorted(labels, key=len, reverse=True)
        )
        return bool(
            re.search(
                rf"\b(?:by|across|per|for\s+each)\s+(?:the\s+)?(?:{spellings})\b|"
                rf"\b(?:{spellings})\s+(?:breakdown|split|grouping)\b|"
                rf"\b(?:breakdown|split|grouping)\s+(?:by|across|for)\s+"
                rf"(?:the\s+)?(?:{spellings})\b",
                message,
            )
        )

    def _raise_unsupported_grouping(
        self,
        operation_name: str,
        dimension_ids: Sequence[str],
    ) -> None:
        names = ", ".join(
            self.catalog.resolve_dimension(identifier).name for identifier in dimension_ids
        )
        raise AnalysisPlanningError(
            f"The approved {operation_name} does not support grouping by {names}."
        )

    def _filters(self, message: str) -> dict[str, str | int | float | bool]:
        filters: dict[str, str | int | float | bool] = {}
        for dimension_id, values in self.dimension_values.items():
            matched: list[str] = []
            for value in values:
                if re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", message, re.I):
                    matched.append(value)
            if len(matched) > 1:
                raise AnalysisPlanningError(
                    f"One question may select only one {dimension_id.title()} value; "
                    f"found {', '.join(matched)}."
                )
            if matched:
                filters[dimension_id] = matched[0]
        return filters

    @classmethod
    def _time_window(cls, message: str) -> TimeWindow:
        explicit = cls._explicit_time_window(message)
        if explicit is not None:
            if re.search(
                r"\b(?:latest|last|most\s+recent)\s+(?:month|quarter)\b|"
                r"\b(?:year\s+to\s+date|ytd|all\s+time)\b|"
                r"\b(?:last\s+(?:12|twelve)\s+months|past\s+year|"
                r"over\s+(?:the\s+)?last\s+year)\b",
                message,
            ):
                raise cls._unsupported_time_expression()
            return explicit

        if (
            "last 12 months" in message
            or "last twelve months" in message
            or "past year" in message
            or "over the last year" in message
            or "over last year" in message
        ):
            return TimeWindow(preset="last_12_months")
        if re.search(r"\bsame\s+(?:quarter|period)\s+last\s+year\b", message):
            return TimeWindow(preset="latest_quarter")

        cls._reject_unsupported_time_expression(cls._mask_supported_prior_year_comparison(message))
        if (
            "latest month" in message
            or "last month" in message
            or "most recent month" in message
            or re.search(r"\bmonth[ -]over[ -]month\b|\bmom\b", message)
            or re.search(r"\b(?:prior|previous)\s+month\b", message)
        ):
            return TimeWindow(preset="latest_month")
        if "year to date" in message or "ytd" in message:
            return TimeWindow(preset="year_to_date")
        if "all time" in message:
            return TimeWindow(preset="all_time")
        return TimeWindow(preset="latest_quarter")

    @classmethod
    def _explicit_time_window(cls, message: str) -> TimeWindow | None:
        """Parse one unambiguous calendar window and reject competing periods."""

        unsupported_before_masking = (
            r"\b\d{1,4}/\d{1,2}/\d{1,4}\b|"
            r"\b\d{1,2}[-.]\d{1,2}[-.]\d{4}\b|"
            r"\bfy\s*\d{2,4}\b|\bfiscal\b|"
            r"\bq(?:0|[5-9])\s*(?:of\s+)?(?:19|20)\d{2}\b|"
            r"\b(?:quarter\s+(?:one|two|three|four|[1-4])|"
            r"(?:one|two|three|four|[1-4])\s+quarter)\b|"
            r"\bqtr\s*[1-4]\b|\bh[12]\b|\bweek\s+\d{1,2}\b|"
            r"\b(?:last|past|previous|prior|next)\s+"
            r"(?!(?:12\s+months|twelve\s+months)\b)"
            r"\d+\s+(?:days?|weeks?|months?|quarters?|years?)\b|"
            rf"\b(?:spring|summer|fall|autumn|winter)\s+{_YEAR_PATTERN}\b|"
            r"\b(?:this|current)\s+(?:day|week|month|quarter|year)\b|"
            r"\b(?:today|yesterday|tomorrow)\b"
        )
        if re.search(unsupported_before_masking, message):
            raise cls._unsupported_time_expression()

        masked = list(message)
        for supported_relative in re.finditer(
            r"\b(?:last\s+(?:12|twelve)\s+months|past\s+year|"
            r"over\s+(?:the\s+)?last\s+year|"
            r"same\s+(?:quarter|period)\s+last\s+year|"
            r"(?:vs|versus|compared\s+to|to)\s+(?:the\s+)?"
            r"(?:last|prior|previous)\s+year)\b",
            message,
        ):
            masked[supported_relative.start() : supported_relative.end()] = " " * (
                supported_relative.end() - supported_relative.start()
            )
        windows: list[tuple[date, date]] = []

        def consume(match: re.Match[str], start: date, end: date) -> None:
            if start > end:
                raise cls._unsupported_time_expression()
            windows.append((start, end))
            masked[match.start() : match.end()] = " " * (match.end() - match.start())

        iso = rf"(?P<start>{_YEAR_PATTERN}-\d{{2}}-\d{{2}})"
        iso += rf"\s+(?:to|through)\s+(?P<end>{_YEAR_PATTERN}-\d{{2}}-\d{{2}})"
        between_iso = rf"between\s+(?P<start>{_YEAR_PATTERN}-\d{{2}}-\d{{2}})"
        between_iso += rf"\s+and\s+(?P<end>{_YEAR_PATTERN}-\d{{2}}-\d{{2}})"
        for expression in (rf"\bfrom\s+{iso}\b", rf"\b{between_iso}\b"):
            for match in re.finditer(expression, "".join(masked)):
                try:
                    start = date.fromisoformat(match.group("start"))
                    end = date.fromisoformat(match.group("end"))
                except ValueError as error:
                    raise cls._unsupported_time_expression() from error
                consume(match, start, end)

        named_date_pattern = re.compile(
            rf"\b(?P<month>{_MONTH_PATTERN})\s+"
            rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*|\s+)"
            rf"(?P<year>{_YEAR_PATTERN})\b"
        )
        for match in named_date_pattern.finditer("".join(masked)):
            try:
                value = date(
                    int(match.group("year")),
                    _MONTH_NUMBERS[match.group("month")],
                    int(match.group("day")),
                )
            except ValueError as error:
                raise cls._unsupported_time_expression() from error
            consume(match, value, value)

        for match in re.finditer(
            rf"\b(?P<value>{_YEAR_PATTERN}-\d{{2}}-\d{{2}})\b",
            "".join(masked),
        ):
            try:
                value = date.fromisoformat(match.group("value"))
            except ValueError as error:
                raise cls._unsupported_time_expression() from error
            consume(match, value, value)

        quarter_patterns = (
            re.compile(rf"\bq(?P<quarter>[1-4])\s*(?:of\s+)?(?P<year>{_YEAR_PATTERN})\b"),
            re.compile(rf"\b(?P<year>{_YEAR_PATTERN})\s+q(?P<quarter>[1-4])\b"),
        )
        for pattern in quarter_patterns:
            for match in pattern.finditer("".join(masked)):
                quarter = int(match.group("quarter"))
                year = int(match.group("year"))
                start_month = (quarter - 1) * 3 + 1
                end_month = start_month + 2
                consume(
                    match,
                    date(year, start_month, 1),
                    date(year, end_month, calendar.monthrange(year, end_month)[1]),
                )

        word_quarter_pattern = re.compile(
            rf"\b(?P<quarter>first|second|third|fourth)\s+quarter\s+"
            rf"(?:of\s+)?(?P<year>{_YEAR_PATTERN})\b"
        )
        for match in word_quarter_pattern.finditer("".join(masked)):
            quarter = _QUARTER_WORDS[match.group("quarter")]
            year = int(match.group("year"))
            start_month = (quarter - 1) * 3 + 1
            end_month = start_month + 2
            consume(
                match,
                date(year, start_month, 1),
                date(year, end_month, calendar.monthrange(year, end_month)[1]),
            )

        month_pattern = re.compile(
            rf"\b(?P<month>{_MONTH_PATTERN})(?:\s+of)?[\s,]+"
            rf"(?P<year>{_YEAR_PATTERN})\b"
        )
        for match in month_pattern.finditer("".join(masked)):
            month = _MONTH_NUMBERS[match.group("month")]
            year = int(match.group("year"))
            consume(
                match,
                date(year, month, 1),
                date(year, month, calendar.monthrange(year, month)[1]),
            )

        for match in re.finditer(rf"\b(?P<year>{_YEAR_PATTERN})\b", "".join(masked)):
            year = int(match.group("year"))
            consume(match, date(year, 1, 1), date(year, 12, 31))

        cls._reject_unsupported_time_expression("".join(masked))
        if len(windows) > 1:
            raise cls._unsupported_time_expression()
        if not windows:
            return None
        return TimeWindow(start=windows[0][0], end=windows[0][1])

    @staticmethod
    def _mask_supported_prior_year_comparison(message: str) -> str:
        return re.sub(
            r"\b(?:vs|versus|compared\s+to|to)\s+(?:the\s+)?"
            r"(?:last|prior|previous)\s+year\b",
            " ",
            message,
        )

    @classmethod
    def _reject_unsupported_time_expression(cls, message: str) -> None:
        unsupported = (
            r"\b\d{1,4}/\d{1,2}/\d{1,4}\b|"
            rf"\b{_YEAR_PATTERN}-\d{{1,2}}(?:\b|-(?!\d{{2}}\b))|"
            r"\bfy\s*\d{2,4}\b|\bfiscal\b|"
            r"\bq(?:0|[5-9]|[1-4](?!\s*(?:of\s+)?(?:19|20)\d{2}))\b|"
            r"\b(?:this|current)\s+(?:day|week|month|quarter|year)\b|"
            r"\b(?:today|yesterday|tomorrow)\b|"
            r"\b(?:before|after|since|until|through)\b|"
            r"\b(?:first|second)\s+half\b|\bhalf\s+of\b|"
            r"\b(?:last|previous|prior)\s+year\b|"
            r"\b(?:one|two|three|\d+)\s+years?\s+ago\b|"
            rf"\b{_MONTH_PATTERN}\b|"
            r"\b(?:first|second|third|fourth)\s+quarter\b"
        )
        if re.search(unsupported, message):
            raise cls._unsupported_time_expression()

    @staticmethod
    def _unsupported_time_expression() -> AnalysisPlanningError:
        return AnalysisPlanningError(
            "The requested date or period is ambiguous or outside the supported calendar "
            "forms. Use YYYY, Month YYYY, Q1–Q4 YYYY, YYYY-MM-DD, or an ISO date range."
        )

    @staticmethod
    def _price_change(message: str) -> int:
        if re.search(r"\d+\.\d+\s*(?:%|\bpercent\b)", message):
            raise AnalysisPlanningError(
                "The approved price scenario accepts whole percentage points only."
            )
        if re.search(r"\b(?:basis\s+points?|bps)\b", message):
            raise AnalysisPlanningError(
                "The approved price scenario uses whole percentage points, not basis points."
            )
        if re.search(
            r"\b(?:plus|minus)?\s*(?:zero|one|two|three|four|five|six|seven|eight|"
            r"nine|ten)\s+percent\b",
            message,
        ):
            raise AnalysisPlanningError("Spell the governed price change with digits, such as +3%.")
        matches = list(
            re.finditer(
                r"(?<![\d.])([+-]?\d{1,2})(?![\d.])\s*(?:%|\bpercent\b)",
                message,
            )
        )
        if len(matches) > 1:
            raise AnalysisPlanningError(
                "Ask for exactly one approved price scenario value at a time."
            )
        if not matches:
            if re.search(r"\b(?:approved|default|governed)\b", message):
                return 3
            raise AnalysisPlanningError(
                "Specify an approved whole-point price change from -3% through +5%, or ask "
                "for the approved default scenario."
            )
        match = matches[0]
        raw_value = match.group(1)
        value = int(raw_value)
        local_prefix = message[max(0, match.start() - 12) : match.start()]
        local_suffix = message[match.end() : min(len(message), match.end() + 32)]
        has_decrease = bool(
            re.search(r"\bminus\s*$", local_prefix)
            or re.search(r"\bprice\s+decrease\b", local_suffix)
        )
        has_increase = bool(
            re.search(r"\bplus\s*$", local_prefix)
            or re.search(r"\bprice\s+increase\b", local_suffix)
        )
        if not raw_value.startswith(("+", "-")) and has_decrease and has_increase:
            raise AnalysisPlanningError("The requested price direction is ambiguous.")
        if not raw_value.startswith(("+", "-")) and has_decrease and value > 0:
            value = -value
        if not -3 <= value <= 5:
            raise AnalysisPlanningError(
                "The approved price scenario supports -3% through +5% in one-point steps."
            )
        return value

    @staticmethod
    def _comparison(message: str) -> Literal["prior_period", "prior_year"]:
        if re.search(
            r"\b(?:prior period|previous period|prior month|previous month|"
            r"prior quarter|previous quarter|"
            r"quarter[ -]over[ -]quarter|qoq|month[ -]over[ -]month|mom)\b",
            message,
        ):
            return "prior_period"
        return "prior_year"
