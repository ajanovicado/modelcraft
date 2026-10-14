"""Reject actual malformed Qwen expression boundaries, preserving native DAX syntax."""

from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.contracts import ProposedDaxMeasure
from ai_powerbi_analyst.powerbi.dax_validation import dax_argument_errors
from ai_powerbi_analyst.powerbi.semantic_model import SemanticProjectError, validate_advanced_dax
from ai_powerbi_analyst.studio.contracts import AdvancedDaxProposal
from ai_powerbi_analyst.studio.planner import StudioPlanningError, _validate_advanced_dax_proposal

QWEN_POSTFIX_FILTER = (
    "VAR QualifyingSales = SUMX('FactSales', 'FactSales'[Sales]) "
    "FILTER('FactSales', 'FactSales'[Sales] >= 220)\n"
    "VAR QualifyingProfit = SUMX('FactSales', 'FactSales'[Profit]) "
    "FILTER('FactSales', 'FactSales'[Sales] >= 220)\n"
    "VAR Result = DIVIDE(QualifyingProfit, QualifyingSales)\nRETURN Result"
)


@pytest.mark.parametrize(
    "expression",
    [
        QWEN_POSTFIX_FILTER,
        "SUM('FactSales'[Sales]) SUM('FactSales'[Profit])",
        "VAR Sales = SUMX('FactSales', 'FactSales'[Sales]) /* whitespace */ "
        "FILTER('FactSales', 'FactSales'[Sales] > 0) RETURN Sales",
        "VAR Sales = SUMX('FactSales', 'FactSales'[Sales]) // postfix comment\n"
        "filter('FactSales', 'FactSales'[Sales] > 0) RETURN Sales",
    ],
)
def test_function_calls_need_a_boundary_after_a_closed_expression(expression):
    assert any(
        "cannot follow a closed expression" in error for error in dax_argument_errors(expression)
    )


@pytest.mark.parametrize(
    "expression",
    [
        "SUMX(FILTER('FactSales', 'FactSales'[Sales] >= 220), 'FactSales'[Profit])",
        "CALCULATE(SUM('FactSales'[Profit]), FILTER('FactSales', 'FactSales'[Sales] > 0))",
        "SUM('FactSales'[Sales]) + SUM('FactSales'[Profit])",
        "VAR Sales = SUMX('FactSales', 'FactSales'[Sales]) "
        "VAR Profit = SUMX('FactSales', 'FactSales'[Profit]) RETURN DIVIDE(Profit, Sales)",
        "VAR Outer = (VAR Inner = SUM('FactSales'[Sales]) RETURN (Inner)) RETURN (Outer)",
        "SUM('FactSales'[Sales]) IN (VALUES('FactSales'[Profit]))",
        'VAR Text = ") FILTER(" RETURN Text',
        "SUM('Table) FILTER('[Sales])",
        "SUM('FactSales'[Sales) FILTER(])",
        "SUM('FactSales'[Sales]) /* ) FILTER(...) */ + SUM('FactSales'[Profit])",
        "SUM('FactSales'[Sales]) // ) FILTER(...)\n + SUM('FactSales'[Profit])",
        "SUMX(WINDOW(1, ABS, -1, REL, ALL('FactSales'), ORDERBY('FactSales'[Sales])), "
        "'FactSales'[Profit])",
    ],
)
def test_native_iterators_operators_literals_comments_and_nested_vars_are_preserved(expression):
    assert not dax_argument_errors(expression)


def test_actual_postfix_filter_is_rejected_at_proposal_and_approved_compile_boundaries():
    proposal = AdvancedDaxProposal(
        id="qualified_margin",
        name="Qualified Margin",
        table_id="facts",
        expression=QWEN_POSTFIX_FILTER,
        dependencies=[],
        format_string="0.00%",
        assumptions=["Current context"],
    )
    catalog = {
        "tables": [
            {
                "id": "facts",
                "name": "FactSales",
                "fields": [
                    {"name": "Sales"},
                    {"name": "Profit"},
                ],
            }
        ]
    }
    with pytest.raises(StudioPlanningError, match="DAX call FILTER cannot follow"):
        _validate_advanced_dax_proposal(proposal, catalog, {proposal.id: proposal})
    hypothetical = ProposedDaxMeasure.model_validate({**proposal.model_dump(), "approved": True})
    manifest = SimpleNamespace(tables=[SimpleNamespace(id="facts")])
    with pytest.raises(SemanticProjectError, match="DAX call FILTER cannot follow"):
        validate_advanced_dax(hypothetical, manifest, set())
