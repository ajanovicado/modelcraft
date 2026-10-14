"""Keep requested constructs in advanced proposals through the bounded repair lane."""

import json
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.powerbi.dax_validation import (
    dax_requested_construct_errors,
    requested_dax_constructs,
)
from ai_powerbi_analyst.studio.contracts import AdvancedDaxProposal, MeasureProposal
from ai_powerbi_analyst.studio.planner import StudioPlanningError, StudioPlanningService


@pytest.mark.parametrize(
    ("prompt", "expression", "missing"),
    [
        (
            "Use VAR and SUMX to total Sales.",
            "VAR Sales = SUM('FactSales'[Sales]) RETURN Sales",
            "SUMX",
        ),
        ("Use VAR and SUMX.", "SUMX('FactSales', 'FactSales'[Sales])", "VAR"),
        ("Use VAR and SUMX.", "/* VAR x = SUMX(...) */ SUM('FactSales'[Sales])", "SUMX, VAR"),
        ("Use VAR and SUMX.", '"VAR x = SUMX("', "SUMX, VAR"),
    ],
)
def test_explicit_constructs_must_exist_outside_comments_and_literals(prompt, expression, missing):
    errors = dax_requested_construct_errors(prompt, expression)
    assert errors and missing in errors[0]


@pytest.mark.parametrize(
    "prompt",
    ["Sum Sales.", "Do not use SUMX.", "Never use VAR.", "What is SUMX?"],
)
def test_requests_without_a_positive_construct_instruction_are_unrestricted(prompt):
    assert not dax_requested_construct_errors(prompt, "SUM('FactSales'[Sales])")
    assert requested_dax_constructs(prompt) == []


@pytest.mark.parametrize(
    ("prompt", "required"),
    [
        ("Use VAR and SUMX to total Sales.", ["SUMX", "VAR"]),
        ("Create a measure using both sumx & var.", ["SUMX", "VAR"]),
        ("Create a measure with SUMX, not VAR.", ["SUMX"]),
        ("Never use SUMX; use VAR for readability.", ["VAR"]),
    ],
)
def test_planning_contract_matches_positive_requirements_enforced_by_validation(prompt, required):
    assert requested_dax_constructs(prompt) == required
    errors = dax_requested_construct_errors(prompt, "SUM('FactSales'[Sales])")
    assert ", ".join(required) in errors[0]


class ProposalModel:
    def __init__(self, expressions):
        self.expressions = iter(expressions)
        self.requests = []
        self.systems = []

    def health(self):
        return SimpleNamespace(model_available=True)

    def generate(self, _system, request, _response_type, **_options):
        self.requests.append(json.loads(request))
        self.systems.append(_system)
        return AdvancedDaxProposal(
            id="qualified_margin",
            name="Qualified Margin",
            table_id="facts",
            expression=next(self.expressions),
            dependencies=[],
            format_string="0.00%",
            assumptions=["Current filter context"],
        )


def planner_and_draft(monkeypatch, expressions):
    model = ProposalModel(expressions)
    planner = StudioPlanningService(model, None)
    monkeypatch.setattr(planner, "_references", lambda *_args: "")
    draft = SimpleNamespace(measure_proposal=MeasureProposal(), workspace_id="workspace-test")
    catalog = {
        "tables": [
            {
                "id": "facts",
                "name": "FactSales",
                "row_count": 97,
                "fields": [{"column_id": "sales", "name": "Sales", "type": "number"}],
            }
        ]
    }
    return planner, draft, catalog, model


def test_missing_sumx_gets_one_real_proposal_repair_without_formula_rewriting(monkeypatch):
    first = "VAR Sales = SUM('FactSales'[Sales]) RETURN Sales"
    second = "VAR Sales = SUMX('FactSales', 'FactSales'[Sales]) RETURN Sales"
    planner, draft, catalog, model = planner_and_draft(monkeypatch, [first, second])
    proposal = planner._advanced_dax_revision(draft, "Use VAR and SUMX to total Sales.", catalog)
    assert proposal.measures[0].expression == second
    assert len(model.requests) == 2
    assert model.requests[0]["required_dax_constructs"] == ["SUMX", "VAR"]
    assert model.requests[1]["required_dax_constructs"] == ["SUMX", "VAR"]
    assert "Missing explicitly requested DAX constructs: SUMX" in model.requests[1]["repair"]
    assert "do not repeat it unchanged" in model.requests[1]["repair"]
    assert "preserving the requested fields, filters" in model.requests[1]["repair"]
    assert model.requests[1]["previous_unapproved_proposal"]["expression"] == first
    assert not proposal.measures[0].approved


def test_invalid_var_and_missing_sumx_are_diagnosed_and_fail_after_bounded_repair(monkeypatch):
    invalid = "VAR Sales := SUM('FactSales'[Sales]) RETURN Sales"
    decoy = "VAR Sales = SUM('FactSales'[Sales]) /* SUMX(...) */ RETURN Sales"
    planner, draft, catalog, model = planner_and_draft(monkeypatch, [invalid, decoy])
    with pytest.raises(
        StudioPlanningError, match="Missing explicitly requested DAX constructs: SUMX"
    ):
        planner._advanced_dax_revision(draft, "Use VAR and SUMX to total Sales.", catalog)
    assert len(model.requests) == 2
    assert "requires '='" in model.requests[1]["repair"]
    assert "Missing explicitly requested DAX constructs" in model.requests[1]["repair"]


def test_advanced_prompt_gives_standard_var_iterator_and_blank_syntax(monkeypatch):
    valid = "VAR Sales = SUMX('FactSales', 'FactSales'[Sales]) RETURN Sales"
    planner, draft, catalog, model = planner_and_draft(monkeypatch, [valid])
    proposal = planner._advanced_dax_revision(draft, "Use VAR and SUMX to total Sales.", catalog)
    assert proposal.measures[0].expression == valid
    assert "VAR Name = expression" in model.systems[0]
    assert "SUMX('Table', 'Table'[Column])" in model.systems[0]
    assert "BLANK(), never 0" in model.systems[0]


def test_advanced_format_guidance_distinguishes_ratios_from_money_and_honors_request(monkeypatch):
    valid = "VAR Sales = SUMX('FactSales', 'FactSales'[Sales]) RETURN DIVIDE(Sales, Sales)"
    planner, draft, catalog, model = planner_and_draft(monkeypatch, [valid])
    proposal = planner._advanced_dax_revision(
        draft, "Use VAR and SUMX for a sales ratio. Use format 0.00%.", catalog
    )
    system = model.systems[0]
    assert "Honor an explicitly requested number format" in system
    assert "0.00% for ratios or percentage\nmargins" in system
    assert "#,0 for counts, #,0.00 for other numbers" in system
    assert "$#,0.00 only for monetary amounts" in system
    assert "format a ratio as money" in system
    assert proposal.measures[0].expression == valid
    assert proposal.measures[0].format_string == "0.00%"


def test_advanced_normal_generation_teaches_filtered_iterator_arguments(monkeypatch):
    valid = (
        "VAR Sales = SUMX(FILTER('FactSales', 'FactSales'[Sales] > 0), "
        "'FactSales'[Sales]) RETURN Sales"
    )
    planner, draft, catalog, model = planner_and_draft(monkeypatch, [valid])
    prompt = "Use VAR and SUMX to total positive Sales."
    proposal = planner._advanced_dax_revision(draft, prompt, catalog)
    assert proposal.measures[0].expression == valid
    assert len(model.requests) == 1
    assert model.requests[0]["request"] == prompt
    assert "repair" not in model.requests[0]
    assert "SUMX(FILTER('Table', condition), row_expression)" in model.systems[0]
    assert "CALCULATE(SUMX('Table', row_expression), filter)" in model.systems[0]
    assert "FILTER belongs inside a function\nargument, never as a postfix" in model.systems[0]


def test_required_constructs_are_authoritative_over_sum_based_reference_example(monkeypatch):
    valid = "VAR Sales = SUMX('FactSales', 'FactSales'[Sales]) RETURN Sales"
    planner, draft, catalog, model = planner_and_draft(monkeypatch, [valid])
    reference = "Ratio example: DIVIDE(SUM('Fact'[Profit]), SUM('Fact'[Sales]))."
    monkeypatch.setattr(planner, "_references", lambda *_args: reference)
    proposal = planner._advanced_dax_revision(draft, "Use VAR and SUMX to total Sales.", catalog)
    assert proposal.measures[0].expression == valid
    assert list(model.requests[0]).index("required_dax_constructs") < list(model.requests[0]).index(
        "reference_material"
    )
    assert model.requests[0]["reference_material"] == reference
    assert "SUM is not a\nsubstitute for requested SUMX" in model.systems[0]
    assert (
        "never waive the output schema, allowed catalog, or formula validation" in model.systems[0]
    )
