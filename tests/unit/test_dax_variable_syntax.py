"""Regress genuine Qwen VAR syntax errors without narrowing native DAX support."""

import pytest

from ai_powerbi_analyst.powerbi.dax_validation import dax_argument_errors, dax_variable_errors


@pytest.mark.parametrize(
    "expression",
    [
        "VAR QualifyingSales := CALCULATE(SUM('FactSales'[Sales]), 'FactSales'[Sales] >= 220) "
        "RETURN QualifyingSales",
        "var denominator == SUM('FactSales'[Sales]) return denominator",
        "VAR Outer = (VAR Inner /* native expression */ := SUMX('FactSales', [Sales]) "
        "RETURN Inner) RETURN Outer",
    ],
)
def test_invalid_variable_assignment_is_rejected_by_existing_approval_and_compile_gate(expression):
    assert dax_variable_errors(expression)
    assert any("requires '='" in error for error in dax_argument_errors(expression))


@pytest.mark.parametrize(
    "expression",
    [
        "VAR Sales = SUM('FactSales'[Sales]) RETURN Sales",
        "Qualified Margin := VAR Sales = SUM('FactSales'[Sales]) RETURN Sales",
        "VAR Rows = FILTER('FactSales', 'FactSales'[Sales] >= 220) "
        "VAR Sales = SUMX(Rows, 'FactSales'[Sales]) RETURN Sales",
        "VAR Outer = (VAR Inner = 1 RETURN Inner) RETURN Outer",
        "VAR.P('FactSales'[Sales]) + VAR.S('FactSales'[Sales])",
        'VAR Message = "VAR Fake := SUM(1)" RETURN Message',
        "SUM('VAR Wrong := ('[VAR Fake := SUM(1)])",
        "/* VAR Fake := SUM(1) */ VAR Real = SUM('FactSales'[Sales]) RETURN Real",
        "// VAR Fake := SUM(1)\nVAR Real = 1 -- VAR Another := SUM(1)\nRETURN Real",
        "VAR /* declaration comment */ Sales = SUM('FactSales'[Sales]) RETURN Sales",
    ],
)
def test_valid_variables_native_iterators_headers_literals_and_comments_remain_available(
    expression,
):
    assert not dax_variable_errors(expression)
    assert not dax_argument_errors(expression)


def test_invalid_qwen_variable_syntax_cannot_reach_approved_semantic_build():
    from types import SimpleNamespace

    from ai_powerbi_analyst.contracts import ProposedDaxMeasure
    from ai_powerbi_analyst.powerbi.semantic_model import (
        SemanticProjectError,
        validate_advanced_dax,
    )

    measure = ProposedDaxMeasure(
        id="qualified_margin",
        name="Qualified Margin",
        table_id="facts",
        expression="VAR QualifyingSales := SUM('FactSales'[Sales]) RETURN QualifyingSales",
        dependencies=[],
        format_string="0.00%",
        assumptions=["Current context"],
        approved=True,
    )
    manifest = SimpleNamespace(tables=[SimpleNamespace(id="facts")])
    with pytest.raises(SemanticProjectError, match="VAR QualifyingSales requires '='"):
        validate_advanced_dax(measure, manifest, set())
