"""Business help must describe preserved definitions and refuse ambiguous recommendations."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from test_model_health import column, measure, rel, table
from test_relationship_engine import _manifest

from ai_powerbi_analyst.studio import business_help as help
from ai_powerbi_analyst.studio import model_health, number_explain
from ai_powerbi_analyst.studio.edit_context import EditContext
from ai_powerbi_analyst.studio.relationship_engine import (
    RelationshipGovernanceEngine,
    RelationshipGovernanceResult,
)
from ai_powerbi_analyst.studio.report_edit_model import CreateRelationship
from ai_powerbi_analyst.studio.report_import import ImportedPage, ImportedReport, ImportedVisual


@pytest.fixture
def report() -> ImportedReport:
    measures = [
        measure("Total Sales", "SUM(Sales[Amount])"),
        measure("Total Cost", "SUM(Sales[Cost])"),
        measure("Margin", "DIVIDE([Total Sales] - [Total Cost], [Total Sales])"),
    ]
    facts = {
        "version": 1,
        "tables": [
            table("Sales"),
            table("Customer"),
            table("Calendar", isDateTable=True, dateColumn="Date"),
            table("# Measures"),
        ],
        "columns": [
            column("Sales", "Amount", 10),
            column("Sales", "Cost", 10),
            column("Sales", "CustomerKey", 6),
            column("Sales", "OrderDate", 9),
            column("Customer", "CustomerKey", 6),
            column("Customer", "Region", 2),
            column("Calendar", "Date", 9),
        ],
        "relationships": [
            rel("Sales", "CustomerKey", "Customer", "CustomerKey"),
            rel("Sales", "OrderDate", "Calendar", "Date"),
        ],
        "measures": measures,
        # Private partition data must never enter any returned wording or proposal.
        "partitions": [{"table": "Sales", "expression": "let PRIVATE_M_SENTINEL in Secret"}],
    }
    sales = {"kind": "measure", "table": "# Measures", "name": "Total Sales", "role": "Y"}
    region = {"kind": "column", "table": "Customer", "name": "Region", "role": "Category"}
    return ImportedReport(
        id="report-business",
        workspace_id="workspace-business",
        name="Business report",
        source_path="unread.pbix",
        archived_path="unread.pbix",
        source_hash="a" * 64,
        base_hash="a" * 64,
        format="pbir",
        model=facts,
        tables=[{"TableName": c["table"], "ColumnName": c["name"]} for c in facts["columns"]],
        measures=[
            {"TableName": m["table"], "Name": m["name"], "Expression": m["expression"]}
            for m in measures
        ],
        pages=[
            ImportedPage(
                id="overview",
                title="Overview",
                visuals=[
                    ImportedVisual(
                        id="card",
                        title="Total Sales card",
                        visual_type="cardVisual",
                        bindings=[sales],
                        x=0,
                        y=0,
                    ),
                    ImportedVisual(
                        id="region",
                        title="Sales by Region",
                        visual_type="clusteredBarChart",
                        bindings=[region, sales],
                        x=0,
                        y=200,
                        definition={"filterConfig": {"filters": [{"name": "saved-region"}]}},
                    ),
                ],
            )
        ],
    )


@pytest.mark.parametrize(
    "message",
    [
        "Walk me through this page",
        "Describe the Overview page",
        "Explain this page",
    ],
)
def test_page_walkthrough_uses_preserved_visuals_not_current_number_claims(report, message):
    context = EditContext.from_report(report, active_page="overview", selection="card")
    before = report.model_dump_json()
    answer = help.walkthrough(message, report, context=context)
    assert answer is not None and "2 saved visual(s)" in answer
    assert "Total Sales card is a card" in answer and "Sales by Region is a bar chart" in answer
    assert "Customer[Region]" in answer and "the sum of Sales[Amount]" in answer
    assert "saved definitions" in answer and "not a calculation" in answer
    assert "PRIVATE_M_SENTINEL" not in answer and "SUM(" not in answer
    assert report.model_dump_json() == before


@pytest.mark.parametrize(
    "message",
    [
        "Walk me through the data model and how the Sales by Region chart's tables are related",
        "Describe the data model relationships, including the Sales by Region chart",
        "Explain the data model and why the Sales by Region visual uses Customer[Region]",
    ],
)
def test_model_walkthrough_subject_stays_in_the_existing_rich_model_answer_lane(report, message):
    context = EditContext.from_report(report, active_page="overview", selection="region")
    assert help.walkthrough(message, report, context=context, measure="Margin") is None
    assert help.recommend_visual(message, report, context=context) is None
    assert help.model_advice(message, report) is None


@pytest.mark.parametrize(
    "message",
    [
        "Explain how the Total Sales and Total Cost measures work",
        "Describe Total Sales and Total Cost calculations",
        "Walk me through '# Measures'[Total Sales] and '# Measures'[Total Cost] formulas",
        "Explain the Total Sales measure and the Total Cost measure",
        "Describe '# Measures'[Total Sales] and Total Cost",
        "Walk me through Total Cost and '# Measures'[Total Sales]",
    ],
)
def test_multiple_specific_measures_preserve_the_whole_question_for_the_existing_answer_lane(
    report,
    monkeypatch,
    message,
):
    def forbidden(*_args, **_kwargs):
        pytest.fail(
            "The single-measure helper must not calculate or truncate a multi-measure question",
        )

    monkeypatch.setattr(number_explain, "answer", forbidden)
    attached = number_explain.AskContext(report, object(), visual_id="card")
    assert (
        help.walkthrough(
            message,
            report,
            visual_id="card",
            measure="Margin",
            explain_context=attached,
        )
        is None
    )


@pytest.mark.parametrize(
    "message",
    [
        "Explain this chart",
        "What does this visual show?",
        "Walk me through Sales by Region",
    ],
)
def test_chart_walkthrough_resolves_selected_and_named_visuals(report, message):
    answer = help.walkthrough(message, report, visual_id="region", page_id="overview")
    assert answer is not None and "Sales by Region is a bar chart" in answer
    assert "Category: Customer[Region]" in answer and "Y: # Measures[Total Sales]" in answer
    assert "1 visual filter definition" in answer and "saved definition" in answer


def test_walkthrough_asks_on_duplicate_entities_and_rejects_stale_selection(report):
    report.pages[0].visuals.append(report.pages[0].visuals[1].model_copy(update={"id": "copy"}))
    assert "Which visual" in help.walkthrough("Explain Sales by Region chart", report)
    assert "Which visual" in help.walkthrough("Explain this chart", report, visual_id="stale")
    assert "attached page" in help.walkthrough(
        "Explain this chart", report, visual_id="card", page_id="foreign"
    )


def test_measure_walkthrough_describes_dependencies_and_unknown_formula_honestly(report):
    answer = help.walkthrough("Explain the Margin measure", report)
    assert answer is not None and "# Measures[Margin]" in answer
    assert "Uses [Total Sales]" in answer and "Uses [Total Cost]" in answer
    assert "filters of each visual" in answer and "DIVIDE(" not in answer
    report.measures[0]["Expression"] = "CUSTOM_UNKNOWN_CALL(Sales[Amount])"
    unknown = help.walkthrough("Explain this calculation", report, measure="Total Sales")
    assert "cannot give a reliable" in unknown and "CUSTOM_UNKNOWN_CALL" not in unknown


def test_walkthrough_reuses_only_the_callers_number_context(report, monkeypatch):
    context = number_explain.AskContext(report, object(), page_id="overview", visual_id="card")
    seen = []
    monkeypatch.setattr(
        number_explain, "answer", lambda message, ctx: seen.append(ctx) or "Checked 42"
    )
    assert (
        help.walkthrough("Why is this number 42?", report, explain_context=context) == "Checked 42"
    )
    assert seen == [context]
    other = report.model_copy(update={"id": "foreign"})
    foreign = replace(context, report=other)
    answer = help.walkthrough("Why is this number 42?", report, explain_context=foreign)
    assert "different report" in answer and seen == [context]
    old_revision = replace(context, report=report.model_copy(update={"revision_hash": "b" * 64}))
    stale = help.walkthrough("Why is this number 42?", report, explain_context=old_revision)
    assert "revision" in stale and seen == [context]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Explain the Total Sales measure", "# Measures[Total Sales]:"),
        ("Describe '# Measures'[Total Sales] calculation", "# Measures[Total Sales]:"),
        ("Walk me through the Total Sales formula", "# Measures[Total Sales]:"),
        ("Explain the Total Value page", "Page Total Value contains 2 saved visual(s)."),
        ("Describe Total Value page", "Page Total Value contains 2 saved visual(s)."),
        ("Walk me through the Total Value page", "Page Total Value contains 2 saved visual(s)."),
        ("Explain the Total Sales card", "Total Sales card is a card."),
        ("Describe the Total Sales card", "Total Sales card is a card."),
        ("Walk me through the Total Sales card", "Total Sales card is a card."),
    ],
)
def test_explicit_definition_target_cannot_be_hijacked_by_an_attached_number_context(
    report,
    monkeypatch,
    message,
    expected,
):
    report.pages[0] = report.pages[0].model_copy(update={"title": "Total Value"})
    foreign = number_explain.AskContext(
        report.model_copy(update={"id": "other-report"}),
        object(),
        visual_id="region",
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("A definition walkthrough must not calculate the attached number")

    monkeypatch.setattr(number_explain, "answer", forbidden)
    answer = help.walkthrough(
        message,
        report,
        page_id="overview",
        visual_id="region",
        measure="Margin",
        explain_context=foreign,
    )
    assert answer.startswith(expected)


@pytest.mark.parametrize(
    ("message", "visual_type", "group"),
    [
        (
            "How should I visualize the monthly Total Sales trend?",
            "lineChart",
            ("Calendar", "Date"),
        ),
        ("Show Total Sales over time", "lineChart", ("Calendar", "Date")),
        (
            "What chart should compare Total Sales by Region?",
            "clusteredBarChart",
            ("Customer", "Region"),
        ),
        ("Visualize Total Sales across Region", "clusteredBarChart", ("Customer", "Region")),
        ("Show the Total Sales contribution by Region", "donutChart", ("Customer", "Region")),
        ("What visual should show Total Sales as one number?", "cardVisual", None),
    ],
)
def test_business_question_yields_closed_chart_type_and_canonical_fields(
    report,
    message,
    visual_type,
    group,
):
    before = report.model_dump_json()
    found = help.recommend_visual(message, report)
    assert found is not None and found.kind == "recommend", found
    assert found.visual_type == visual_type and "report has not changed" in found.text
    assert found.roles[-1].field() == {"table": "# Measures", "name": "Total Sales"}
    assert found.roles[-1].role == ("Data" if visual_type == "cardVisual" else "Y")
    if group:
        assert found.roles[0].role == "Category"
        assert (found.roles[0].table, found.roles[0].name) == group
    assert report.model_dump_json() == before


def test_visual_recommendation_does_not_guess_metric_category_date_or_relationship(report):
    assert (
        help.recommend_visual("What chart compares performance by Region?", report).kind
        == "clarify"
    )
    assert (
        help.recommend_visual("Show Total Sales by the missing Market column", report).kind
        == "clarify"
    )
    report.model["columns"].append(column("Other", "Region", 2))
    ambiguous = help.recommend_visual("Show Total Sales by Region", report)
    assert ambiguous.kind == "clarify" and not ambiguous.roles and ambiguous.visual_type is None
    report.model["relationships"][0]["active"] = False
    disconnected = help.recommend_visual("Show Total Sales by Customer[Region]", report)
    assert disconnected.kind == "clarify" and "active filter path" in disconnected.text
    report.model["tables"][2]["isDateTable"] = False
    report.model["columns"].append(column("Sales", "ShipDate", 9))
    assert help.recommend_visual("Show the monthly Total Sales trend", report).kind == "clarify"


def test_duplicate_metric_and_nonadditive_share_require_a_choice(report):
    report.measures.append(
        {"TableName": "Other", "Name": "Total Sales", "Expression": "SUM(Sales[Cost])"}
    )
    assert help.recommend_visual("Show Total Sales as one number", report).kind == "clarify"
    qualified = help.recommend_visual("Show '# Measures'[Total Sales] as one number", report)
    assert qualified.kind == "recommend" and qualified.roles[0].table == "# Measures"
    ratio = help.recommend_visual("Show the Margin share by Customer[Region]", report)
    assert ratio.kind == "clarify" and "additive" in ratio.text and not ratio.roles


def test_explicit_aggregation_and_selected_metric_are_grounded(report):
    average = help.recommend_visual("Show the average Sales[Amount] by Customer[Region]", report)
    assert average.kind == "recommend"
    assert average.roles[-1].field() == {
        "table": "Sales",
        "name": "Amount",
        "aggregation": "Average",
    }
    context = EditContext.from_report(report, selection="card")
    selected = help.recommend_visual("Show a monthly trend of this metric", report, context=context)
    assert selected.kind == "recommend" and selected.roles[-1].name == "Total Sales"


def test_foreign_qualified_fields_and_explicit_missing_metrics_do_not_use_selection(report):
    context = EditContext.from_report(report, selection="card")
    for message in (
        "Show Other[Total Sales] as one number",
        "Show Total Sales by Other[Region]",
        "Show the Revenue trend over time",
    ):
        answer = help.recommend_visual(message, report, context=context)
        assert answer.kind == "clarify" and not answer.roles and answer.visual_type is None


@pytest.mark.parametrize(
    "message",
    [
        "What chart shows Total Sales and Total Cost as one number?",
        "Show the monthly Total Sales trend by Region",
        "Visualize Total Sales by Region and CustomerKey",
    ],
)
def test_multiple_metrics_or_groupings_are_not_silently_discarded(report, message):
    found = help.recommend_visual(message, report)
    assert found.kind == "clarify" and not found.roles and found.visual_type is None


@pytest.mark.parametrize(
    "message",
    [
        "What visual should show '# Measures'[Total Sales] and Total Cost as one number?",
        "Recommend a headline card for Total Cost and '# Measures'[Total Sales]",
        "Which chart compares '# Measures'[Total Sales] with Total Cost by Customer[Region]?",
    ],
)
def test_qualified_metric_does_not_discard_a_separate_bare_metric(report, message):
    found = help.recommend_visual(message, report)
    assert found.kind == "clarify" and not found.roles and found.visual_type is None
    assert "# Measures[Total Sales]" in found.text and "# Measures[Total Cost]" in found.text


@pytest.mark.parametrize(
    "message",
    [
        "Which chart shows monthly Total Sales on 'Calendar'[Date] by Region?",
        "Recommend a Total Sales trend by Region using Calendar[Date]",
        "Visualize Total Sales over time on Calendar[Date], with separate Region trends",
    ],
)
def test_qualified_date_does_not_discard_a_separate_bare_grouping(report, message):
    found = help.recommend_visual(message, report)
    assert found.kind == "clarify" and not found.roles and found.visual_type is None
    assert "separate trends by a category" in found.text


@pytest.mark.parametrize(
    "message",
    [
        "Explain Other[Margin] measure",
        "Describe the calculation 'Other Measures'[Margin]",
        "Walk me through the Foreign[Margin] formula",
    ],
)
def test_foreign_qualified_measure_does_not_fall_back_to_a_bare_or_attached_name(report, message):
    answer = help.walkthrough(message, report, measure="Margin")
    assert "not in this report" in answer and "Which measure" in answer
    assert "# Measures[Margin]:" not in answer


@pytest.mark.parametrize(
    "message",
    [
        "Explain the Total Sales measure",
        "Describe '# Measures'[Total Sales] calculation",
        "Walk me through the Total Sales formula",
    ],
)
def test_explicit_measure_subject_overrides_another_attached_measure(report, message):
    answer = help.walkthrough(message, report, measure="Margin")
    assert answer.startswith("# Measures[Total Sales]:")
    assert "the sum of Sales[Amount]" in answer and "# Measures[Margin]:" not in answer


@pytest.mark.parametrize(
    "message",
    [
        "Explain this measure",
        "Describe the selected calculation",
        "Walk me through that formula",
    ],
)
def test_deictic_measure_subject_keeps_the_attached_measure(report, message):
    answer = help.walkthrough(message, report, measure="# Measures[Margin]")
    assert answer.startswith("# Measures[Margin]:")
    assert "Uses [Total Sales]" in answer and "Uses [Total Cost]" in answer


@pytest.mark.parametrize(
    "message",
    [
        "Walk me through the Overview page",
        "Explain this page",
        "Describe the selected page",
    ],
)
def test_page_subject_does_not_become_a_measure_walkthrough_due_to_attachment(report, message):
    answer = help.walkthrough(message, report, page_id="overview", measure="Margin")
    assert answer.startswith("Page Overview contains 2 saved visual(s).")
    assert "Total Sales card is a card" in answer and "Sales by Region is a bar chart" in answer


@pytest.mark.parametrize(
    "message",
    [
        "Walk me through Sales by Region",
        "Explain this chart",
        "Describe the Sales by Region visual",
    ],
)
def test_visual_subject_does_not_become_a_measure_walkthrough_due_to_attachment(report, message):
    answer = help.walkthrough(
        message,
        report,
        page_id="overview",
        visual_id="region",
        measure="Margin",
    )
    assert answer.startswith("Sales by Region is a bar chart.")
    assert "Category: Customer[Region]" in answer and "Y: # Measures[Total Sales]" in answer


@pytest.mark.parametrize(
    "message",
    [
        "Explain the Margin measure",
        "Describe the Margin formula",
        "Walk me through Margin calculation",
    ],
)
def test_ambiguous_explicit_measure_does_not_use_an_attached_disambiguation(report, message):
    report.measures.append({"TableName": "Other", "Name": "Margin", "Expression": "42"})
    answer = help.walkthrough(message, report, measure="# Measures[Margin]")
    assert "Which measure" in answer and "# Measures[Margin]:" not in answer
    qualified = help.walkthrough(
        "Explain Other[Margin] measure",
        report,
        measure="# Measures[Margin]",
    )
    assert qualified.startswith("Other[Margin]:")


@pytest.mark.parametrize(
    "message",
    [
        "Explain Other[Margin]",
        "Describe 'Foreign Measures'[Margin]",
        "Walk me through Missing[Margin]",
    ],
)
def test_qualified_unknown_measure_asks_without_a_subject_noun_or_attachment(report, message):
    answer = help.walkthrough(message, report)
    assert "Which measure" in answer and "not in this report" in answer
    assert "# Measures[Margin]:" not in answer


@pytest.mark.parametrize(
    "message",
    [
        "Make this chart green",
        "Write DAX for a ratio",
        "What were sales last month?",
    ],
)
def test_other_requests_stay_in_their_existing_route(report, message):
    assert help.walkthrough(message, report) is None
    assert help.recommend_visual(message, report) is None
    assert help.model_advice(message, report) is None


def test_model_advice_explains_star_and_date_design_without_fake_operations(report):
    answer = help.model_advice("Help me build a star schema and a date table", report)
    assert answer is not None and "Shared dimensions" in answer.text
    assert "Marked date tables in this model: Calendar" in answer.text
    assert "Full-snapshot governance is needed" in answer.text
    assert "report has not changed" in answer.text and "PRIVATE_M_SENTINEL" not in answer.text
    assert all(not p.operations and not p.verified for p in answer.proposals)
    assert next(p for p in answer.proposals if p.kind == "date_table").requires_desktop


def test_full_governance_candidates_are_reviewable_proposals_and_blocked_keys_stay_blocked(
    report,
    tmp_path: Path,
):
    manifest = _manifest(
        tmp_path,
        {
            "Sales": pd.DataFrame({"CustomerKey": [1, 1, 2], "Amount": [10, 20, 30]}),
            "Customer": pd.DataFrame({"CustomerKey": [1, 2], "Region": ["North", "South"]}),
        },
    )
    governance = RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest)
    report.model["relationships"] = []
    before = report.model_dump_json()
    answer = help.model_advice(
        "How should I connect Customer and Sales?", report, manifest=manifest, governance=governance
    )
    joins = [p for p in answer.proposals if p.kind == "relationship"]
    assert joins and joins[0].verified and joins[0].requires_desktop
    op = CreateRelationship.model_validate(joins[0].operations[0])
    assert (op.from_table, op.from_column, op.to_table, op.to_column) == (
        "Sales",
        "CustomerKey",
        "Customer",
        "CustomerKey",
    )
    assert op.cardinality == "many_to_one" and op.cross_filter == "single"
    assert not op.assume_referential_integrity and report.model_dump_json() == before
    blocked = replace(
        governance,
        relationships=tuple(
            c.model_copy(
                update={
                    "blockers": ["Duplicate keys on dimension"],
                }
            )
            for c in governance.relationships
        ),
    )
    declined = help.model_advice(
        "Suggest relationships", report, manifest=manifest, governance=blocked
    )
    assert all(not p.operations and not p.verified for p in declined.proposals)
    assert "Duplicate keys" in declined.text


def test_governance_never_recreates_existing_joins_or_uses_foreign_report_fields(report, tmp_path):
    manifest = _manifest(
        tmp_path,
        {
            "Sales": pd.DataFrame({"CustomerKey": [1, 1, 2]}),
            "Customer": pd.DataFrame({"CustomerKey": [1, 2]}),
        },
    )
    result = RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest)
    answer = help.model_advice(
        "Suggest relationships", report, manifest=manifest, governance=result
    )
    assert not [p for p in answer.proposals if p.kind == "relationship"]
    report.model["relationships"] = []
    report.model["columns"] = [c for c in report.model["columns"] if c["table"] != "Customer"]
    report = report.model_copy(
        update={
            "tables": [c for c in report.tables if c["TableName"] != "Customer"],
        }
    )
    foreign = help.model_advice(
        "Suggest relationships", report, manifest=manifest, governance=result
    )
    join = next(p for p in foreign.proposals if p.kind == "relationship")
    assert "not present" in join.blockers[0] and not join.verified and not join.operations
    no_manifest = help.model_advice("Suggest relationships", report, governance=result)
    assert "Full-snapshot governance is needed" in no_manifest.text


def test_outside_contract_date_health_fix_is_guidance_without_an_executable_op(report):
    report.model["tables"][2]["isDateTable"] = False
    report.model["measures"].append(measure("Sales YTD", "TOTALYTD([Total Sales], Calendar[Date])"))
    health = model_health.check_model(report.model, usage=None)
    date_findings = [f for f in health.findings if f.rule == "date_table_not_marked"]
    assert date_findings and date_findings[0].fix_outside_supported
    answer = help.model_advice("Advise on a date table", report, health=health)
    fix = next(p for p in answer.proposals if p.kind == "health_fix")
    assert fix.requires_desktop and fix.blockers and not fix.operations
    assert not fix.verified


def test_unknown_model_and_empty_governance_do_not_claim_validation(report):
    report = report.model_copy(update={"model": None})
    answer = help.model_advice(
        "Give model design advice", report, governance=RelationshipGovernanceResult((), ())
    )
    assert "could not read the full model" in answer.text
    assert all(not p.verified and not p.operations for p in answer.proposals)


@pytest.mark.parametrize(
    ("active", "expected"),
    [
        (True, "recommend"),
        (1, "recommend"),
        (False, "clarify"),
        (0, "clarify"),
        ("1", "clarify"),
        ("true", "clarify"),
        (1.0, "clarify"),
        (None, "clarify"),
    ],
)
def test_legacy_flat_relationships_require_a_known_boolean_or_integer_active_flag(
    report,
    active,
    expected,
):
    legacy = report.model_copy(
        update={
            "model": None,
            "relationships": [
                {
                    "FromTableName": "Sales",
                    "FromColumnName": "CustomerKey",
                    "ToTableName": "Customer",
                    "ToColumnName": "CustomerKey",
                    "Cardinality": "M:1",
                    "IsActive": active,
                }
            ],
        }
    )
    answer = help.recommend_visual(
        "Recommend a comparison chart for Total Sales by Customer[Region]",
        legacy,
    )
    assert answer.kind == expected
    if expected == "recommend":
        assert answer.visual_type == "clusteredBarChart"
        assert answer.roles[0].field() == {"table": "Customer", "name": "Region"}
    else:
        assert not answer.roles and "active filter path" in answer.text


@pytest.mark.parametrize(
    "message",
    [
        "Give me advice about joining the tables",
        "Suggest how connecting the tables should work",
        "Give advice on linking the fact and dimension tables",
    ],
)
def test_model_advice_accepts_business_joining_connecting_and_linking_words(report, message):
    answer = help.model_advice(message, report)
    assert answer is not None and "Relationship names alone" in answer.text
    assert "report has not changed" in answer.text
    assert all(not p.verified and not p.operations for p in answer.proposals)


@pytest.mark.parametrize(
    "message",
    [
        "Explain how the store slicer works",
        "Describe how a waterfall chart works",
        "Explain how slicer selections affect the report",
    ],
)
def test_unanchored_visual_how_to_questions_remain_with_ordinary_chat(report, message):
    assert help.walkthrough(message, report) is None
    assert "select" in help.walkthrough("Explain this chart", report).casefold()
