"""Concrete adapters for the governed analytics and safe Power BI subsystems."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from ai_powerbi_analyst.contracts import (
    AnalysisOperation,
    AnalysisPlan,
    EvidenceAnswer,
    ReportChangePlan,
    ReportPagePlan,
    RouteIntent,
    SemanticCatalog,
    VisualRecipe,
)
from ai_powerbi_analyst.powerbi.composer import ComposerProgress, SafePowerBIComposer
from ai_powerbi_analyst.powerbi.transaction import TransactionReceipt, hash_tree
from ai_powerbi_analyst.powerbi.validation import validate_report

from .conversation import ConversationState
from .service import AnalysisResult, ProgressReporter, ReportPlanningError


class AnalysisPlanner(Protocol):
    def plan(self, message: str) -> AnalysisPlan: ...


class GovernedAnalytics(Protocol):
    def answer(self, plan: AnalysisPlan) -> EvidenceAnswer: ...


class EvidencePlacementUnsupported(ValueError):
    """Grounded evidence cannot be represented by an approved report binding."""


class AnalyticsServiceAdapter:
    """Keep the exact approved plan next to its evidence for later placement."""

    def __init__(self, planner: AnalysisPlanner, analytics: GovernedAnalytics) -> None:
        self.planner = planner
        self.analytics = analytics

    def answer(self, message: str, _state: ConversationState) -> AnalysisResult:
        plan = self.planner.plan(message)
        return AnalysisResult(plan=plan, evidence=self.analytics.answer(plan))


class ThreePageReportPlanner:
    """Produce the single approved three-page report recipe from live state."""

    def __init__(self, catalog: SemanticCatalog, *, report_hash: Callable[[], str]) -> None:
        self.catalog = catalog
        self.report_hash = report_hash

    def plan(self, message: str, state: ConversationState) -> ReportChangePlan:
        if state.last_route is RouteIntent.BUILD:
            self._validate_build_scope(message)
        pages = [
            ReportPagePlan(
                key="executive_pulse",
                title="Executive Pulse",
                visual_recipes=[
                    VisualRecipe.KPI_ROW,
                    VisualRecipe.MONTHLY_TREND,
                    VisualRecipe.CATEGORY_BAR,
                    VisualRecipe.SLICER_ROW,
                ],
                metric_ids=self._available(
                    "net_sales", "gross_profit", "gross_margin_pct", "sales_growth"
                ),
                dimension_ids=self._dimensions("month", "region", "category"),
            ),
            ReportPagePlan(
                key="margin_drivers",
                title="Margin Drivers",
                visual_recipes=[
                    VisualRecipe.KPI_ROW,
                    VisualRecipe.CONTRIBUTION_BRIDGE,
                    VisualRecipe.DETAIL_MATRIX,
                    VisualRecipe.EVIDENCE_SLOT,
                ],
                metric_ids=self._available(
                    "gross_profit", "gross_margin_pct", "average_selling_price", "unit_cost"
                ),
                dimension_ids=self._dimensions("category", "region"),
            ),
            ReportPagePlan(
                key="outlook_decisions",
                title="Outlook & Decisions",
                visual_recipes=[
                    VisualRecipe.FORECAST_LINE,
                    VisualRecipe.SCENARIO_SUMMARY,
                    VisualRecipe.KPI_ROW,
                ],
                metric_ids=self._available("net_sales", "gross_profit", "gross_margin_pct"),
                dimension_ids=self._dimensions("month"),
            ),
        ]
        if state.last_route is RouteIntent.MODIFY:
            pages = self._bounded_modification_pages(message, pages)
        return ReportChangePlan(
            base_report_hash=self.report_hash(),
            catalog_version=self.catalog.version,
            pages=pages,
            assumptions=[
                "Uses the latest complete quarter in the prepared local data.",
                "Forecast values come from the approved frozen six-month model.",
                "The price scenario uses the governed elasticity assumption.",
            ],
        )

    @staticmethod
    def _validate_build_scope(message: str) -> None:
        lowered = " ".join(message.casefold().split())
        page_count = re.search(
            r"\b(?P<count>one|two|three|four|five|six|seven|eight|nine|ten|\d+)"
            r"\s*[- ]?pages?\b",
            lowered,
        )
        if page_count and page_count.group("count") not in {"three", "3"}:
            raise ReportPlanningError(
                "This prepared build supports exactly the approved three-page report: "
                "Executive Pulse, Margin Drivers, and Outlook & Decisions."
            )
        if re.search(
            r"\b(?:without|omit|exclude|remove|no)\b.{0,30}"
            r"\b(?:forecast|outlook|scenario|drivers?|page|slicers?|filters?|"
            r"trend|bridge|matrix|kpis?)\b",
            lowered,
        ):
            raise ReportPlanningError(
                "The first release cannot remove prepared pages or governed forecast, "
                "driver, or scenario content."
            )
        unsupported_domain = re.search(
            r"\b(?:inventory|workforce|human resources|hr|marketing|web analytics|"
            r"supply chain|manufacturing|healthcare|finance dashboard|"
            r"financial dashboard|finance report)\b",
            lowered,
        )
        if unsupported_domain:
            raise ReportPlanningError(
                f"{unsupported_domain.group(0).title()} is outside this prepared Sales & "
                "Margin workspace."
            )
        unsupported_visual = re.search(
            r"\b(?:pie|donut|scatter|bubble|map|gauge|treemap|funnel|radar)\s+"
            r"(?:chart|visual)\b|\bcustom\s+visual\b",
            lowered,
        )
        if unsupported_visual:
            raise ReportPlanningError(
                f"{unsupported_visual.group(0).title()} is outside the approved fixed "
                "visual recipes."
            )
        unsupported_presentation = re.search(
            r"\b(?:dark|mobile|portrait|landscape|custom)\s+(?:theme|layout|view)\b",
            lowered,
        )
        if unsupported_presentation:
            raise ReportPlanningError(
                "The first release supports only the approved executive-light layout and theme."
            )

    @staticmethod
    def _bounded_modification_pages(
        message: str,
        pages: list[ReportPagePlan],
    ) -> list[ReportPagePlan]:
        lowered = " ".join(message.casefold().split())
        recipes: dict[str, tuple[str, ...]] = {
            "executive_pulse": ("executive pulse", "kpi", "trend"),
            "margin_drivers": ("margin drivers", "bridge", "detail matrix"),
            "outlook_decisions": ("outlook", "forecast", "scenario"),
        }
        selected: list[str] = []
        for key, terms in recipes.items():
            if terms[0] in lowered and any(term in lowered for term in terms[1:]):
                selected.append(key)
        if ("executive-light" in lowered or "executive light" in lowered) and re.search(
            r"\b(?:three|3|all)\b.{0,35}\bpages?\b", lowered
        ):
            selected = list(recipes)
        if not selected:
            raise ReportPlanningError(
                "Supported report changes are: refresh Executive Pulse with the approved KPI "
                "and trend recipe; refresh Margin Drivers with the governed bridge and detail "
                "matrix; replace Outlook with the approved forecast and scenario recipe; or "
                "apply executive-light to all three supported pages."
            )
        return [page for page in pages if page.key in selected]

    def _available(self, *identifiers: str) -> list[str]:
        available = {metric.id for metric in self.catalog.metrics}
        missing = [identifier for identifier in identifiers if identifier not in available]
        if missing:
            raise ValueError(
                "the semantic catalog is missing required report metrics: " + ", ".join(missing)
            )
        return list(identifiers)

    def _dimensions(self, *identifiers: str) -> list[str]:
        available = {dimension.id for dimension in self.catalog.dimensions}
        missing = [identifier for identifier in identifiers if identifier not in available]
        if missing:
            raise ValueError(
                "the semantic catalog is missing required report dimensions: " + ", ".join(missing)
            )
        return list(identifiers)


class SafeComposerAdapter:
    """Map orchestration calls to transactions and persist the last undo receipt."""

    def __init__(
        self,
        composer: SafePowerBIComposer,
        catalog: SemanticCatalog,
        *,
        catalog_hash: str,
        data_hash: str,
        state_root: str | Path,
        desktop_opener: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.composer = composer
        self.catalog = catalog
        if not re.fullmatch(r"[0-9a-f]{64}", catalog_hash):
            raise ValueError("catalog_hash must be a lowercase SHA-256 digest")
        if not re.fullmatch(r"[0-9a-f]{64}", data_hash):
            raise ValueError("data_hash must be a lowercase SHA-256 digest")
        self.catalog_hash = catalog_hash
        self.data_hash = data_hash
        self.state_root = Path(state_root)
        self.receipt_path = self.state_root / "latest-transaction.json"
        self.desktop_opener = desktop_opener
        self._recover_latest_receipt()

    @property
    def undo_available(self) -> bool:
        try:
            self._load_receipt()
        except (FileNotFoundError, KeyError, OSError, ValueError):
            return False
        return True

    def report_plan_installed(self, plan: ReportChangePlan) -> bool:
        """Prove that every deterministic plan object exists in the live PBIR."""

        report_path = self.composer.transactions.report_path
        if plan.base_report_hash != hash_tree(report_path):
            return False
        if not validate_report(report_path, catalog=self.catalog).passed:
            return False
        try:
            blueprint = self.composer.compiler.compile(plan, self.catalog)
        except (KeyError, ValueError):
            return False
        pages_root = report_path / "definition" / "pages"
        for page in blueprint.pages:
            page_root = pages_root / page.page_id
            if not (page_root / "page.json").is_file():
                return False
            for visual in page.visuals:
                if not (page_root / "visuals" / visual.visual_id / "visual.json").is_file():
                    return False
        return True

    def evidence_slot_ready(self, plan: ReportChangePlan) -> bool:
        if not self.report_plan_installed(plan):
            return False
        blueprint = self.composer.compiler.compile(plan, self.catalog)
        return any(
            visual.is_evidence_slot
            and (
                self.composer.transactions.report_path
                / "definition"
                / "pages"
                / page.page_id
                / "visuals"
                / visual.visual_id
                / "visual.json"
            ).is_file()
            for page in blueprint.pages
            for visual in page.visuals
        )

    def apply_report(self, plan: ReportChangePlan, report: ProgressReporter) -> Mapping[str, Any]:
        result = self.composer.apply(plan, self.catalog, progress=self._progress(report))
        self._save_reversible_receipt(result.transaction)
        return self._result(result)

    def add_evidence(
        self,
        evidence: EvidenceAnswer,
        report: ProgressReporter,
        *,
        report_plan: ReportChangePlan | None,
        analysis_plan: AnalysisPlan | None,
    ) -> Mapping[str, Any]:
        if report_plan is None or analysis_plan is None:
            raise EvidencePlacementUnsupported(
                "evidence placement requires its reviewed report and analytical plans"
            )
        visual_plan = evidence.report_visual_plan
        if not evidence.add_to_report_supported or visual_plan is None:
            raise EvidencePlacementUnsupported(
                "this answer has no explicit prepared report visual plan"
            )
        if (
            analysis_plan.operation is not AnalysisOperation.DRIVER_BRIDGE
            or analysis_plan.metric_ids != ["gross_profit"]
            or analysis_plan.dimension_ids
            or analysis_plan.filters != {"region": "West"}
            or analysis_plan.time_window.preset != "latest_quarter"
            or analysis_plan.comparison != "prior_year"
        ):
            raise EvidencePlacementUnsupported(
                "only the canonical frozen West driver bridge is approved for placement"
            )
        if (
            evidence.filters != analysis_plan.filters
            or evidence.catalog_version != self.catalog.version
            or evidence.catalog_hash != self.catalog_hash
            or evidence.data_hash != self.data_hash
            or evidence.metric_ids != ["net_sales", "gross_profit", "gross_margin_pct"]
            or evidence.target_page_key != "margin_drivers"
            or evidence.chart.kind != "contribution_bar"
            or evidence.chart.x_field != "contribution_value"
            or evidence.chart.y_field != "driver"
            or evidence.chart.color_field is not None
            or not evidence.rows
            or any(
                row.get("current_period") != "2025 Q4"
                or row.get("comparison_period") != "2024 Q4"
                or "driver" not in row
                or "contribution_value" not in row
                for row in evidence.rows
            )
        ):
            raise EvidencePlacementUnsupported(
                "the evidence does not match the frozen West driver bridge"
            )
        if (
            visual_plan.binding_id != "west_driver_bridge"
            or visual_plan.metric_object_id != "driver_contribution_west"
            or visual_plan.dimension_object_id != "driver"
            or visual_plan.chart_kind != "bar"
            or visual_plan.target_page_key != "margin_drivers"
        ):
            raise EvidencePlacementUnsupported("the prepared evidence binding is not approved")
        target_page = next(
            (page for page in report_plan.pages if page.key == visual_plan.target_page_key),
            None,
        )
        if (
            report_plan.catalog_version != self.catalog.version
            or target_page is None
            or VisualRecipe.EVIDENCE_SLOT not in target_page.visual_recipes
        ):
            raise EvidencePlacementUnsupported(
                "the reviewed report has no compatible reserved evidence slot"
            )
        for object_id, expected_kind in (
            (visual_plan.dimension_object_id, "column"),
            (visual_plan.metric_object_id, "measure"),
        ):
            try:
                report_object = self.catalog.resolve_report_object(object_id)
            except KeyError as error:
                raise EvidencePlacementUnsupported(str(error).strip("'")) from error
            if report_object.source != "driver" or report_object.kind != expected_kind:
                raise EvidencePlacementUnsupported(
                    f"prepared report object {object_id} has an incompatible binding"
                )
        metric_id = visual_plan.metric_object_id
        dimension_id = visual_plan.dimension_object_id
        chart_kind = visual_plan.chart_kind
        live_report_hash = hash_tree(self.composer.transactions.report_path)
        if report_plan.base_report_hash != live_report_hash:
            raise EvidencePlacementUnsupported(
                "the report changed after this evidence placement was reviewed"
            )
        result = self.composer.apply_evidence(
            report_plan,
            self.catalog,
            metric_id=metric_id,
            dimension_id=dimension_id,
            chart_kind=chart_kind,
            progress=self._progress(report),
        )
        self._save_reversible_receipt(result.transaction)
        return self._result(result)

    def undo(self, report: ProgressReporter) -> Mapping[str, Any]:
        receipt = self._load_receipt()
        result = self.composer.undo(receipt, progress=self._progress(report))
        self.receipt_path.unlink(missing_ok=True)
        return {
            "restored": True,
            "transaction_id": result.transaction_id,
            "report_hash": result.restored_hash,
            "undo_available": self.undo_available,
        }

    def open_power_bi(self) -> Mapping[str, Any]:
        if self.desktop_opener is None:
            return {"opened": False, "reason": "Power BI Desktop launcher is not configured."}
        return self.desktop_opener()

    @staticmethod
    def _progress(report: ProgressReporter) -> Callable[[ComposerProgress], None]:
        def relay(progress: ComposerProgress) -> None:
            report(progress.stage, progress.message, progress.percent / 100)

        return relay

    def _result(self, result: Any) -> Mapping[str, Any]:
        return {
            "validated": bool(result.validation.passed),
            "undo_available": self.undo_available,
            "changed": result.transaction.before_hash != result.transaction.after_hash,
            "changed_paths": list(result.transaction.changed_paths),
            "transaction_id": result.transaction.transaction_id,
            "report_hash": result.transaction.after_hash,
            "backend": result.backend.backend,
            "blueprint_hash": result.blueprint.fingerprint,
        }

    def _save_reversible_receipt(self, receipt: TransactionReceipt) -> None:
        # Idempotent re-application produces an attested no-op transaction with
        # no backup. Preserve the most recent meaningful receipt so a harmless
        # reapply cannot consume the user's only Undo target.
        if receipt.before_hash == receipt.after_hash or receipt.backup_path is None:
            return
        self._save_receipt(receipt)

    def _save_receipt(self, receipt: TransactionReceipt) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "transaction_id": receipt.transaction_id,
            "project_root": str(receipt.project_root),
            "report_relative_path": receipt.report_relative_path.as_posix(),
            "before_hash": receipt.before_hash,
            "after_hash": receipt.after_hash,
            "changed_paths": list(receipt.changed_paths),
            "backup_path": str(receipt.backup_path) if receipt.backup_path else None,
            "validation_sources": list(receipt.validation_sources),
        }
        temporary = self.state_root / f".{self.receipt_path.name}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.receipt_path)

    def _load_receipt(self) -> TransactionReceipt:
        if not self.receipt_path.is_file():
            self._recover_latest_receipt()
        if not self.receipt_path.is_file():
            raise FileNotFoundError("there is no reversible report transaction")
        value = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("transaction receipt must be a JSON object")
        receipt = TransactionReceipt(
            transaction_id=str(value["transaction_id"]),
            project_root=Path(str(value["project_root"])),
            report_relative_path=Path(str(value["report_relative_path"])),
            before_hash=str(value["before_hash"]),
            after_hash=str(value["after_hash"]),
            changed_paths=tuple(str(item) for item in value["changed_paths"]),
            backup_path=Path(str(value["backup_path"])) if value["backup_path"] else None,
            validation_sources=tuple(str(item) for item in value["validation_sources"]),
        )
        if receipt.before_hash == receipt.after_hash or receipt.backup_path is None:
            raise ValueError("transaction receipt is not reversible")
        return receipt

    def _recover_latest_receipt(self) -> None:
        if self.receipt_path.is_file():
            return
        journal_root = self.composer.transactions.state_root / "journal"
        if not journal_root.is_dir():
            return
        consumed_transaction_ids: set[str] = set()
        for undo_journal in journal_root.glob("*.undo.json"):
            try:
                undo_value = json.loads(undo_journal.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(undo_value, dict)
                and undo_value.get("kind") == "undo"
                and undo_value.get("status") == "undone"
                and isinstance(undo_value.get("transaction_id"), str)
            ):
                consumed_transaction_ids.add(str(undo_value["transaction_id"]))
        live_hash = hash_tree(self.composer.transactions.report_path)
        candidates = sorted(
            (path for path in journal_root.glob("*.json") if not path.name.endswith(".undo.json")),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for candidate in candidates:
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
                if (
                    not isinstance(value, dict)
                    or value.get("status") != "committed"
                    or value.get("transaction_id") in consumed_transaction_ids
                    or value.get("after_hash") != live_hash
                    or value.get("before_hash") == value.get("after_hash")
                    or value.get("backup_path") is None
                ):
                    continue
                backup = value.get("backup_path")
                if backup is not None and not Path(str(backup)).is_dir():
                    continue
                payload = {
                    key: value[key]
                    for key in (
                        "transaction_id",
                        "project_root",
                        "report_relative_path",
                        "before_hash",
                        "after_hash",
                        "changed_paths",
                        "backup_path",
                        "validation_sources",
                    )
                }
            except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            self.state_root.mkdir(parents=True, exist_ok=True)
            temporary = self.state_root / f".{self.receipt_path.name}.{uuid.uuid4().hex}.tmp"
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.receipt_path)
            return
