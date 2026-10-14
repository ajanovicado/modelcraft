from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_powerbi_analyst.contracts import SemanticCatalog
from ai_powerbi_analyst.orchestration.conversation import ConversationState
from ai_powerbi_analyst.orchestration.explainer import (
    GroundedExplanationError,
    GroundedPowerBIExplainer,
)

CATALOG_PATH = Path(__file__).parents[2] / "fixtures" / "data" / "semantic_catalog.yaml"


def _catalog() -> SemanticCatalog:
    return SemanticCatalog.model_validate(yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")))


def _write_page(
    report_path: Path,
    *,
    page_id: str = "executive-pulse",
    page_title: str = "Executive Pulse",
    visual: dict[str, object] | None = None,
) -> None:
    page_path = report_path / "definition" / "pages" / page_id
    page_path.mkdir(parents=True, exist_ok=True)
    (page_path / "page.json").write_text(
        json.dumps({"name": page_id, "displayName": page_title}),
        encoding="utf-8",
    )
    if visual is not None:
        visual_path = page_path / "visuals" / "growth-card"
        visual_path.mkdir(parents=True)
        (visual_path / "visual.json").write_text(json.dumps(visual), encoding="utf-8")


def _write_visual(
    report_path: Path,
    *,
    page_id: str,
    visual_id: str,
    visual: dict[str, object],
) -> None:
    visual_path = report_path / "definition" / "pages" / page_id / "visuals" / visual_id
    visual_path.mkdir(parents=True, exist_ok=True)
    (visual_path / "visual.json").write_text(json.dumps(visual), encoding="utf-8")


def _growth_visual(*, property_name: str = "Sales Growth") -> dict[str, object]:
    return {
        "name": "growth-card",
        "visual": {
            "visualType": "cardVisual",
            "visualContainerObjects": {
                "title": [
                    {"properties": {"text": {"expr": {"Literal": {"Value": "'Sales Growth'"}}}}}
                ]
            },
            "query": {
                "queryState": {
                    "Data": {
                        "projections": [
                            {
                                "field": {
                                    "Measure": {
                                        "Expression": {"SourceRef": {"Entity": "Measures"}},
                                        "Property": property_name,
                                    }
                                }
                            }
                        ]
                    }
                }
            },
        },
    }


def _write_measures(semantic_model_path: Path, text: str) -> None:
    table_path = semantic_model_path / "definition" / "tables"
    table_path.mkdir(parents=True, exist_ok=True)
    (table_path / "Measures.tmdl").write_text(text, encoding="utf-8")


def _explainer(tmp_path: Path) -> GroundedPowerBIExplainer:
    report_path = tmp_path / "ClientWow.Report"
    semantic_model_path = tmp_path / "ClientWow.SemanticModel"
    _write_measures(
        semantic_model_path,
        """table Measures
\tmeasure 'Net Sales' = SUM('Fact Sales'[Net Sales])
\t\tformatString: $#,0

\tmeasure 'Sales Growth' =
\t\tVAR PriorSales =
\t\t\tCALCULATE([Net Sales], DATEADD('Dim Date'[Month Start], -1, YEAR))
\t\tRETURN
\t\t\tDIVIDE([Net Sales] - PriorSales, PriorSales)
\t\tformatString: 0.0%
""",
    )
    _write_page(report_path, visual=_growth_visual())
    return GroundedPowerBIExplainer(
        _catalog(),
        report_path=report_path,
        semantic_model_path=semantic_model_path,
    )


def test_sales_growth_explanation_uses_exact_tmdl_dax_and_active_visual(
    tmp_path: Path,
) -> None:
    explanation = _explainer(tmp_path).explain(
        "Explain Sales Growth",
        ConversationState(project_id="client-wow"),
    )

    assert "Metric: Sales Growth (sales_growth)" in explanation
    assert "Exact stored DAX:" in explanation
    assert "    VAR PriorSales =" in explanation
    assert "CALCULATE([Net Sales], DATEADD('Dim Date'[Month Start], -1, YEAR))" in explanation
    assert "DIVIDE([Net Sales] - PriorSales, PriorSales)" in explanation
    assert "Plain-language explanation:" in explanation
    assert "Change in Net Sales versus the governed comparison period." in explanation
    assert 'Page "Executive Pulse" — visual "Sales Growth" (cardVisual)' in explanation
    assert "SUM(net_sales)" not in explanation


def test_explain_metric_resolves_an_approved_alias(tmp_path: Path) -> None:
    explanation = _explainer(tmp_path).explain_metric("revenue")

    assert "Metric: Net Sales (net_sales)" in explanation
    assert "Exact stored DAX:\n\n    SUM('Fact Sales'[Net Sales])" in explanation
    assert "not currently used by any active visual" in explanation


def test_margin_drivers_page_intent_wins_over_margin_metric_alias(tmp_path: Path) -> None:
    report_path = tmp_path / "ClientWow.Report"
    semantic_model_path = tmp_path / "ClientWow.SemanticModel"
    _write_measures(
        semantic_model_path,
        "table Measures\n\tmeasure 'Gross Margin %' = DIVIDE([Gross Profit], [Net Sales])\n",
    )
    _write_page(report_path, page_id="margin-drivers", page_title="Margin Drivers")
    _write_visual(
        report_path,
        page_id="margin-drivers",
        visual_id="driver-chart",
        visual={
            "name": "driver-chart",
            "visual": {
                "visualType": "clusteredBarChart",
                "visualContainerObjects": {
                    "title": [
                        {
                            "properties": {
                                "text": {
                                    "expr": {
                                        "Literal": {
                                            "Value": "'Gross Profit change by governed driver'"
                                        }
                                    }
                                }
                            }
                        }
                    ]
                },
                "query": {
                    "queryState": {
                        "Y": {
                            "projections": [
                                {
                                    "field": {
                                        "Measure": {
                                            "Expression": {
                                                "SourceRef": {"Entity": "Driver Contributions"}
                                            },
                                            "Property": "Driver Contribution — All",
                                        }
                                    }
                                }
                            ]
                        }
                    }
                },
            },
        },
    )
    _write_visual(
        report_path,
        page_id="margin-drivers",
        visual_id="evidence-slot",
        visual={
            "name": "evidence-slot",
            "visual": {
                "visualType": "textbox",
                "visualContainerObjects": {
                    "title": [
                        {
                            "properties": {
                                "text": {
                                    "expr": {"Literal": {"Value": "'Conversational evidence'"}}
                                }
                            }
                        }
                    ]
                },
            },
        },
    )
    explainer = GroundedPowerBIExplainer(
        _catalog(),
        report_path=report_path,
        semantic_model_path=semantic_model_path,
    )

    explanation = explainer.explain(
        "Walk me through the Margin Drivers visuals",
        ConversationState(project_id="client-wow"),
    )

    assert "Page: Margin Drivers" in explanation
    assert "governed contribution bridge" in explanation
    assert 'visual "Gross Profit change by governed driver" (clusteredBarChart)' in explanation
    assert "Driver Contribution — All" in explanation
    assert 'visual "Conversational evidence" (textbox)' in explanation


def test_executive_pulse_explanation_lists_active_approved_bindings(tmp_path: Path) -> None:
    explanation = _explainer(tmp_path).explain(
        "What is the purpose of the Executive Pulse page?",
        ConversationState(project_id="client-wow"),
    )

    assert "Page: Executive Pulse" in explanation
    assert "approved executive overview" in explanation
    assert 'visual "Sales Growth" (cardVisual)' in explanation
    assert "'Measures'[Sales Growth] (measure)" in explanation


def test_scenario_vs_forecast_explanation_uses_exact_stored_provenance(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "ClientWow.Report"
    semantic_model_path = tmp_path / "ClientWow.SemanticModel"
    _write_measures(semantic_model_path, "table Measures\n")
    table_path = semantic_model_path / "definition" / "tables"
    (table_path / "Scenario Grid.tmdl").write_text(
        """table 'Scenario Grid'
\tmeasure 'Approved Scenario Assumption' =
\t\t\"+3% price · elasticity -0.8 · mechanical scenario, not a prediction\"
""",
        encoding="utf-8",
    )
    (table_path / "Forecast.tmdl").write_text(
        """table Forecast
\tmeasure 'Forecast Provenance' =
\t\t\"Approved \" & SELECTEDVALUE('Forecast'[Method], \"frozen forecast\")
""",
        encoding="utf-8",
    )
    _write_page(report_path)
    explainer = GroundedPowerBIExplainer(
        _catalog(),
        report_path=report_path,
        semantic_model_path=semantic_model_path,
    )

    explanation = explainer.explain(
        "Why is the approved scenario not a forecast?",
        ConversationState(project_id="client-wow"),
    )

    assert "mechanical sensitivity" in explanation
    assert "not a prediction" in explanation
    assert "Approved Scenario Assumption exact stored DAX:" in explanation
    assert "Forecast Provenance exact stored DAX:" in explanation
    assert "SELECTEDVALUE('Forecast'[Method]" in explanation


def test_outlook_uncertainty_explanation_lists_exact_bounds_and_line_bindings(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "ClientWow.Report"
    semantic_model_path = tmp_path / "ClientWow.SemanticModel"
    _write_measures(semantic_model_path, "table Measures\n")
    table_path = semantic_model_path / "definition" / "tables"
    bounds = (
        "Forecast Net Sales Lower 80",
        "Forecast Net Sales Upper 80",
        "Forecast Net Sales Lower 95",
        "Forecast Net Sales Upper 95",
    )
    (table_path / "Forecast.tmdl").write_text(
        "table Forecast\n"
        + "\n".join(f"\tmeasure '{name}' = SUM('Forecast'[{name}])" for name in bounds)
        + "\n",
        encoding="utf-8",
    )
    _write_page(report_path, page_id="outlook", page_title="Outlook & Decisions")
    projections = [
        {
            "field": {
                "Measure": {
                    "Expression": {"SourceRef": {"Entity": "Forecast"}},
                    "Property": name,
                }
            }
        }
        for name in bounds
    ]
    _write_visual(
        report_path,
        page_id="outlook",
        visual_id="forecast-chart",
        visual={
            "name": "forecast-chart",
            "visual": {
                "visualType": "lineChart",
                "visualContainerObjects": {
                    "title": [
                        {
                            "properties": {
                                "text": {
                                    "expr": {
                                        "Literal": {
                                            "Value": (
                                                "'Approved six-month outlook · 80% and 95% "
                                                "uncertainty'"
                                            )
                                        }
                                    }
                                }
                            }
                        }
                    ]
                },
                "query": {"queryState": {"Y": {"projections": projections}}},
            },
        },
    )
    explainer = GroundedPowerBIExplainer(
        _catalog(),
        report_path=report_path,
        semantic_model_path=semantic_model_path,
    )

    explanation = explainer.explain(
        "Explain the uncertainty bands on the Outlook page.",
        ConversationState(project_id="client-wow"),
    )

    assert all(name in explanation for name in bounds)
    assert "exact stored DAX" in explanation
    assert "lineChart" in explanation
    assert "stored lower/upper series" in explanation
    assert "not a native shaded-band claim" in explanation


def test_explanation_rejects_multiple_named_metrics(tmp_path: Path) -> None:
    with pytest.raises(GroundedExplanationError, match="exactly one approved metric"):
        _explainer(tmp_path).explain(
            "Explain Sales Growth and Net Sales",
            ConversationState(project_id="client-wow"),
        )


def test_explanation_fails_closed_when_measure_definition_is_missing(tmp_path: Path) -> None:
    report_path = tmp_path / "ClientWow.Report"
    semantic_model_path = tmp_path / "ClientWow.SemanticModel"
    _write_measures(semantic_model_path, "table Measures\n")
    _write_page(report_path)
    explainer = GroundedPowerBIExplainer(
        _catalog(),
        report_path=report_path,
        semantic_model_path=semantic_model_path,
    )

    with pytest.raises(GroundedExplanationError, match="exactly one stored definition"):
        explainer.explain_metric("Net Sales")


def test_explanation_fails_closed_on_duplicate_measure_definitions(tmp_path: Path) -> None:
    report_path = tmp_path / "ClientWow.Report"
    semantic_model_path = tmp_path / "ClientWow.SemanticModel"
    _write_measures(
        semantic_model_path,
        "table Measures\n\tmeasure 'Net Sales' = SUM('Fact Sales'[Net Sales])\n",
    )
    duplicate_path = semantic_model_path / "definition" / "tables" / "Duplicate.tmdl"
    duplicate_path.write_text(
        "table Measures\n\tmeasure 'Net Sales' = SUM('Fact Sales'[Net Sales])\n",
        encoding="utf-8",
    )
    _write_page(report_path)
    explainer = GroundedPowerBIExplainer(
        _catalog(),
        report_path=report_path,
        semantic_model_path=semantic_model_path,
    )

    with pytest.raises(GroundedExplanationError, match="exactly one stored definition"):
        explainer.explain_metric("Net Sales")


def test_usage_requires_an_exact_structured_measure_binding(tmp_path: Path) -> None:
    report_path = tmp_path / "ClientWow.Report"
    semantic_model_path = tmp_path / "ClientWow.SemanticModel"
    _write_measures(
        semantic_model_path,
        "table Measures\n\tmeasure 'Sales Growth' = DIVIDE([Net Sales], [Net Sales])\n",
    )
    visual = _growth_visual(property_name="Different Measure")
    visual["queryRef"] = "Measures.Sales Growth"
    _write_page(report_path, visual=visual)
    explainer = GroundedPowerBIExplainer(
        _catalog(),
        report_path=report_path,
        semantic_model_path=semantic_model_path,
    )

    explanation = explainer.explain_metric("Sales Growth")

    assert "not currently used by any active visual" in explanation
    assert 'Page "Executive Pulse"' not in explanation


def test_explanation_fails_closed_on_malformed_active_pbir(tmp_path: Path) -> None:
    explainer = _explainer(tmp_path)
    broken = (
        tmp_path
        / "ClientWow.Report"
        / "definition"
        / "pages"
        / "executive-pulse"
        / "visuals"
        / "growth-card"
        / "visual.json"
    )
    broken.write_text("not-json", encoding="utf-8")

    with pytest.raises(GroundedExplanationError, match="valid JSON object"):
        explainer.explain_metric("Sales Growth")
