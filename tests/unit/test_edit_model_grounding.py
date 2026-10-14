"""Role literals and group identities are grounded before a model can substitute them."""

from dataclasses import replace

import pandas as pd
import pytest

from ai_powerbi_analyst.studio import dax_syntax as dax
from ai_powerbi_analyst.studio import report_engine
from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest
from ai_powerbi_analyst.studio.dax_engine import Model
from ai_powerbi_analyst.studio.edit_context import EditContext, PageEntry, VisualEntry
from ai_powerbi_analyst.studio.edit_model_grounding import (
    ground_group_targets,
    ground_role_filter,
    validate_role_expression,
)
from ai_powerbi_analyst.studio.edit_types import Clause, ParseKind


@pytest.fixture
def context():
    def visual(key, caption, kind="card", *, page="p1", group=None):
        return VisualEntry(key, page, page, kind, caption, (), 0, 0, 100, 100, group=group)

    return EditContext(
        pages=[PageEntry("p1", "Agents", 0, 1000, 800), PageEntry("p2", "Other", 1, 1000, 800)],
        visuals=[
            visual("sales", "Total MTD Sales"),
            visual("cost", "Total Cost"),
            visual("discount", "Discount"),
            visual("profit", "Profit"),
            visual("chart", "Sales by agent", "columnChart"),
            visual("kpis", "KPI cards", "visualGroup"),
            visual("filters", "Filters", "group"),
            visual("other", "Other cards", "visualGroup", page="p2"),
        ],
        measures=[],
        tables={"DimChannel": ["ChannelName", "Amount"], "DimStore": ["Region"]},
        active_page="p1",
        column_types={"DimChannel.ChannelName": "text", "DimStore.Region": "text"},
    )


@pytest.mark.parametrize(
    "goal,operation",
    [
        ("create a security role Online only that only sees the Online channel", "create_role"),
        ("add a role Online Team that can only see the Online channel", "create_role"),
        ("let the Auditors role see only the Online channel", "update_role"),
        ("restrict the Auditors role: restricted to the Online channel", "update_role"),
        ("create a role Web with access to the Online channel", "create_role"),
        ("update the Auditors role to view only Online channels", "update_role"),
    ],
)
def test_natural_role_goal_builds_canonical_text_predicate(context, goal, operation):
    filled = {"name": "Auditors", "table": "DimStore", "filter": "TRUE()"}
    result = ground_role_filter(context, goal, operation, filled)
    assert result == {
        "name": "Auditors",
        "table": "DimChannel",
        "filter": "'DimChannel'[ChannelName] = \"Online\"",
    }
    assert filled["filter"] == "TRUE()"  # no mutation, even with adversarial model choices
    node = dax.parse_expression(result["filter"])
    assert isinstance(node, dax.Binary) and node.op == "="
    assert node.left == dax.ColRef("DimChannel", "ChannelName")
    assert node.right == dax.Str("Online")
    assert validate_role_expression(result["filter"], context, table="DimChannel") == []


@pytest.mark.parametrize(
    "goal",
    [
        "create a role West Analysts that only sees the East region",
        "add a role East Region named East; restricted to the West region",
        "change Analysts to see only the North America region",
    ],
)
def test_role_name_is_not_an_operand_and_literal_retains_full_identity(context, goal):
    result = ground_role_filter(context, goal, "create_role", {"name": "East"})
    assert isinstance(result, dict)
    node = dax.parse_expression(result["filter"])
    expected = "North America" if "North America" in goal else ("West" if ";" in goal else "East")
    assert node.right == dax.Str(expected)
    assert result["table"] == "DimStore"


@pytest.mark.parametrize("value", ["North America", 'North "Sales"', "O'Brien"])
def test_table_column_and_literal_escaping_round_trip_without_formula_rewrite(context, value):
    context.tables = {"Owner's Regions": ["Region"]}
    context.column_types = {"Owner's Regions.Region": "text"}
    quoted = '"' + value.replace('"', '""') + '"'
    result = ground_role_filter(
        context,
        f"create a role Team that sees only {quoted} region",
        "create_role",
        {"name": "Team"},
    )
    assert isinstance(result, dict)
    node = dax.parse_expression(result["filter"])
    assert node.left == dax.ColRef("Owner's Regions", "Region")
    assert node.right == dax.Str(value)


def test_grounded_role_predicate_restricts_actual_engine_rows_and_amount(context, monkeypatch):
    result = ground_role_filter(
        context,
        "create a role Team that only sees the Online channel",
        "create_role",
        {"name": "Team"},
    )
    model = Model.from_frames(
        {
            "DimChannel": pd.DataFrame(
                {
                    "ChannelName": ["Online", "Store", "Online", "Partner"],
                    "Amount": [2, 30, 5, 40],
                }
            )
        }
    )
    monkeypatch.setattr(report_engine, "report_model", lambda *_: model)
    receipt = report_engine.evaluate(
        None,
        None,
        DaxEvaluateRequest(
            workspace_id="role-fixture",
            expression=f"SUMX(FILTER('DimChannel', {result['filter']}), 'DimChannel'[Amount])",
        ),
    )
    assert receipt.rows == [{"Value": 7}]
    count = report_engine.evaluate(
        None,
        None,
        DaxEvaluateRequest(
            workspace_id="role-fixture",
            expression=f"COUNTROWS(FILTER('DimChannel', {result['filter']}))",
        ),
    )
    assert count.rows == [{"Value": 2}]


@pytest.mark.parametrize(
    "body",
    [
        "DimChannel[ChannelName] = 'Online'",
        "DimChannel[ChannelName] = Online",
        "VAR unused = TRUE() RETURN DimChannel[ChannelName] = MissingValue",
        'VAR first = laterValue VAR laterValue = "Online" RETURN DimChannel[ChannelName] = first',
    ],
)
def test_unknown_identifiers_cannot_pass_role_validation(context, body):
    problems = validate_role_expression(body, context, table="DimChannel")
    assert problems and any("identifier" in problem for problem in problems)


@pytest.mark.parametrize(
    "body",
    [
        'VAR channel = "Online" RETURN DimChannel[ChannelName] = channel',
        'VAR channel = "Online" VAR allowed = DimChannel[ChannelName] = channel RETURN allowed',
        'CONTAINS(VALUES(DimChannel[ChannelName]), DimChannel[ChannelName], "Online")',
    ],
)
def test_complex_valid_predicates_and_lexical_variables_remain_available(context, body):
    assert validate_role_expression(body, context, table="DimChannel") == []
    result = ground_role_filter(
        context,
        f"update the role Team using `{body}`",
        "update_role",
        {"name": "Team", "table": "DimChannel"},
        typed=body,
    )
    assert result["filter"] == body


def test_nested_variables_do_not_leak_into_outer_scope(context):
    body = 'IF((VAR private = "Online" RETURN TRUE()), DimChannel[ChannelName] = private, FALSE())'
    assert any("private" in p for p in validate_role_expression(body, context))


@pytest.mark.parametrize(
    "body",
    [
        'DimChannel[ChannelName] = "Online"',
        'VAR c = "Online" RETURN DimChannel[ChannelName] = c',
        'IF(DimChannel[ChannelName] = "Online", TRUE(), FALSE())',
    ],
)
def test_explicit_typed_body_is_preserved_exactly(context, body):
    result = ground_role_filter(
        context,
        "create the Team role using the formula I typed",
        "create_role",
        {"name": "Team", "filter": "FALSE()"},
        typed=body,
    )
    assert result["filter"] == body


@pytest.mark.parametrize(
    "goal",
    [
        "create a role Team that only sees Online and Store channels",
        "create a role Team that only sees Online channels except closed stores",
        "create a role Team that only sees sales over 1000",
    ],
)
def test_complex_business_predicates_are_left_for_checked_fallback(context, goal):
    filled = {"name": "Team"}
    assert ground_role_filter(context, goal, "create_role", filled) == filled


def test_missing_or_ambiguous_column_never_uses_model_chosen_owner(context):
    context.tables["OtherChannels"] = ["ChannelName"]
    result = ground_role_filter(
        context,
        "add a role Team that only sees the Online channel",
        "create_role",
        {"name": "Team", "table": "DimChannel"},
    )
    assert isinstance(result, Clause) and result.kind is ParseKind.CLARIFY
    context.tables = {"DimStore": ["Region"]}
    result = ground_role_filter(
        context,
        "add a role Team that only sees the Online channel",
        "create_role",
        {"name": "Team"},
    )
    assert isinstance(result, Clause) and "column" in result.question


def test_explicit_owner_resolves_duplicated_column_and_numeric_field_is_not_text(context):
    context.tables["OtherChannels"] = ["ChannelName"]
    result = ground_role_filter(
        context,
        "add a role Team that sees only Online ChannelName in DimChannel",
        "create_role",
        {"name": "Team"},
    )
    assert isinstance(result, dict) and result["table"] == "DimChannel"
    assert dax.parse_expression(result["filter"]).right == dax.Str("Online")
    context.tables = {"Date": ["Year"]}
    context.column_types = {"Date.Year": "int64"}
    assert ground_role_filter(
        context, "create a role Team that sees only year 2024", "create_role", {"name": "Team"}
    ) == {"name": "Team"}


@pytest.mark.parametrize(
    "goal",
    [
        "ungroup Total MTD Sales",
        "break up the Total MTD Sales group",
        "ungroup the selected card",
    ],
)
def test_leaf_is_never_accepted_as_group(context, goal):
    context.selection = "sales"
    result = ground_group_targets(context, goal, "ungroup_visuals", {"group": "sales"})
    assert isinstance(result, Clause) and result.kind is ParseKind.CLARIFY
    assert result.ops == () and "not a group" in result.question


@pytest.mark.parametrize(
    "goal",
    [
        "group two of the cards together",
        "group any two cards",
        "put a pair of cards into a group",
        "group two cards",
        "group 2 cards together",
    ],
)
def test_arbitrary_subset_needs_actual_card_identities(context, goal):
    result = ground_group_targets(context, goal, "group_visuals", {"visuals": ["sales", "cost"]})
    assert isinstance(result, Clause) and result.kind is ParseKind.CLARIFY
    assert result.ops == () and "Which" in result.question


@pytest.mark.parametrize(
    "goal", ["ungroup KPI cards", "break up the KPI cards group", "ungroup that group"]
)
def test_existing_group_is_resolved_from_context_not_adversarial_leaf(context, goal):
    context.selection = "kpis"
    assert ground_group_targets(context, goal, "ungroup_visuals", {"group": "sales"}) == {
        "group": "kpis"
    }


@pytest.mark.parametrize(
    "goal",
    [
        "group Total MTD Sales and Total Cost",
        "group all the cards",
        "group both KPI cards and Filters",
    ],
)
def test_explicit_or_complete_grouping_keeps_retained_positive_operation(context, goal):
    result = ground_group_targets(context, goal, "group_visuals", {"visuals": ["chart"]})
    assert isinstance(result, dict)
    wanted = (
        ["sales", "cost"]
        if "Total Cost" in goal
        else (["kpis", "filters"] if "Filters" in goal else ["sales", "cost", "discount", "profit"])
    )
    assert result["visuals"] == wanted


def test_two_cards_when_exactly_two_exist_is_not_ambiguous(context):
    context.visuals = [v for v in context.visuals if v.id in {"sales", "cost", "chart"}]
    result = ground_group_targets(context, "group two of the cards", "group_visuals", {})
    assert result == {"visuals": ["sales", "cost"]}


def test_group_membership_does_not_make_a_leaf_the_group(context):
    context.visuals = [replace(v, group="kpis") if v.id == "sales" else v for v in context.visuals]
    result = ground_group_targets(
        context, "ungroup Total MTD Sales", "ungroup_visuals", {"group": "sales"}
    )
    assert isinstance(result, Clause) and result.ops == ()


def test_mixed_page_grouping_clarifies_and_group_only_selection_is_respected(context):
    result = ground_group_targets(context, "group KPI cards and Other cards", "group_visuals", {})
    assert isinstance(result, Clause) and "one page" in result.question
    context.selection = "filters"
    result = ground_group_targets(
        context,
        "rename this group to Filter controls",
        "set_group",
        {"group": "chart", "name": "Filter controls"},
    )
    assert result == {"group": "filters", "name": "Filter controls"}


def test_unrelated_cards_and_role_metadata_edits_are_unchanged(context):
    assert ground_role_filter(
        context,
        "rename Team role to Auditors",
        "update_role",
        {"name": "Team", "new_name": "Auditors"},
    ) == {
        "name": "Team",
        "new_name": "Auditors",
    }
    assert ground_group_targets(context, "move the chart", "set_position", {"visual": "chart"}) == {
        "visual": "chart"
    }


@pytest.mark.parametrize("literal", ["ChannelName", "channel", "Region", "DimChannel"])
def test_quoted_literal_cannot_become_field_or_table_evidence(context, literal):
    result = ground_role_filter(
        context,
        f'create role Team that sees only "{literal}" channel',
        "create_role",
        {"name": "Team"},
    )
    assert isinstance(result, dict)
    assert result["table"] == "DimChannel"
    assert dax.parse_expression(result["filter"]).right == dax.Str(literal)


@pytest.mark.parametrize("literal", ["Salesforce", "Sales East", "Presales"])
def test_owner_name_is_not_removed_from_literal_substring(context, literal):
    context.tables = {"Sales": ["Department"]}
    result = ground_role_filter(
        context,
        f'create role CRM that can only see the "{literal}" department',
        "create_role",
        {"name": "CRM"},
    )
    assert isinstance(result, dict)
    assert dax.parse_expression(result["filter"]).right == dax.Str(literal)


def test_unquoted_salesforce_value_is_not_shortened_to_force(context):
    context.tables = {"Sales": ["Department"]}
    result = ground_role_filter(
        context,
        "create role CRM that can only see the Salesforce department",
        "create_role",
        {"name": "CRM"},
    )
    assert dax.parse_expression(result["filter"]).right == dax.Str("Salesforce")


@pytest.mark.parametrize("caption", ["Top 2 cards", "Two cards", "Sales 3 cards"])
def test_numbers_in_explicit_visual_captions_are_not_group_quantity(context, caption):
    context.visuals = [
        replace(v, caption=caption) if v.id == "sales" else v for v in context.visuals
    ]
    result = ground_group_targets(
        context,
        f"group {caption} and Total Cost",
        "group_visuals",
        {"visuals": ["chart"]},
    )
    assert result == {"visuals": ["sales", "cost"]}


@pytest.mark.parametrize(
    "goal",
    [
        "group two of the cards Total MTD Sales and Total Cost",
        "group any two cards: Total MTD Sales and Total Cost",
        "group a pair of cards named Total MTD Sales and Total Cost",
    ],
)
def test_explicit_pair_does_not_clarify_merely_because_other_cards_exist(context, goal):
    assert ground_group_targets(context, goal, "group_visuals", {}) == {
        "visuals": ["sales", "cost"],
    }


def test_an_explicit_leaf_name_overrides_selected_unrelated_group(context):
    context.selection = "kpis"
    result = ground_group_targets(
        context,
        "ungroup the Total MTD Sales group",
        "ungroup_visuals",
        {"group": "kpis"},
    )
    assert isinstance(result, Clause) and result.ops == ()


def test_typed_unknown_literal_is_not_repaired_by_natural_words(context):
    typed = "DimChannel[ChannelName] = 'Online'"
    result = ground_role_filter(
        context,
        "create role Team that only sees the Online channel",
        "create_role",
        {"name": "Team"},
        typed=typed,
    )
    assert isinstance(result, Clause) and result.ops == () and "double quotes" in result.question


@pytest.mark.parametrize(
    "goal",
    [
        "group Total MTD Sales",
        "group Total MTD Sales and Total Cost",
        "ungroup Total MTD Sales",
    ],
)
def test_short_caption_inside_long_caption_is_not_a_second_visual(context, goal):
    context.visuals.append(replace(context.visuals[0], id="short", caption="Sales"))
    operation = "ungroup_visuals" if goal.startswith("ungroup") else "group_visuals"
    result = ground_group_targets(context, goal, operation, {})
    if "Total Cost" in goal:
        assert result == {"visuals": ["sales", "cost"]}
    else:
        assert isinstance(result, Clause) and result.ops == ()


def test_definite_count_keeps_existing_unique_matching_row_resolution(context):
    context.visuals.append(replace(context.visuals[0], id="selector", caption="Top choice", y=500))
    result = ground_group_targets(context, "group the four cards together", "group_visuals", {})
    assert result == {"visuals": ["sales", "cost", "discount", "profit"]}


@pytest.mark.parametrize(
    "goal",
    [
        "group two of the cards",
        "group any two cards",
        "put a pair of cards into a group",
    ],
)
def test_arbitrary_subset_does_not_use_a_convenient_existing_row(context, goal):
    context.visuals = [
        replace(v, y=500) if v.id in {"discount", "profit"} else v for v in context.visuals
    ]
    result = ground_group_targets(context, goal, "group_visuals", {})
    assert isinstance(result, Clause) and result.ops == ()


@pytest.mark.parametrize("label", ["filter", "predicate:", "DAX is"])
def test_unfenced_typed_row_equality_is_preserved_and_precedes_prose(context, label):
    body = 'DimChannel[ChannelName] = "Store"'
    result = ground_role_filter(
        context,
        f"update Team role that can see Online channel using {label} {body}",
        "update_role",
        {"name": "Team", "table": "DimChannel"},
    )
    assert result["filter"] == body


@pytest.mark.parametrize("label", ["filter", "predicate:", "DAX is"])
def test_unfenced_typed_identifier_is_rejected_and_never_rewritten_as_literal(context, label):
    body = "DimChannel[ChannelName] = 'Online'"
    result = ground_role_filter(
        context,
        f"add Team role that only sees Online channel using {label} {body}",
        "create_role",
        {"name": "Team", "table": "DimChannel"},
    )
    assert isinstance(result, Clause) and result.ops == ()
    assert "double quotes" in result.question


@pytest.mark.parametrize(
    "goal",
    [
        "create a role Team that sees Online channel and East region",
        "update Team role restricted to Online channel except West region",
        "let Team role see only Online channels where the region is East",
    ],
)
def test_complex_multi_field_rule_preserves_validated_fallback(context, goal):
    filled = {
        "name": "Team",
        "table": "DimChannel",
        "filter": 'DimChannel[ChannelName] = "Online" && RELATED(DimStore[Region]) = "East"',
    }
    assert ground_role_filter(context, goal, "create_role", filled) == filled
    assert validate_role_expression(filled["filter"], context, table="DimChannel") == []


@pytest.mark.parametrize(
    "goal",
    [
        "group a couple of the cards",
        "group some cards together",
        "group a few of the cards",
        "group several cards together",
    ],
)
def test_unidentified_subset_never_becomes_all_cards(context, goal):
    result = ground_group_targets(context, goal, "group_visuals", {"visuals": ["sales", "cost"]})
    assert isinstance(result, Clause) and result.kind is ParseKind.CLARIFY and result.ops == ()


@pytest.mark.parametrize(
    "predicate",
    [
        "Online ChannelName in DimChannel",
        "Online ChannelName in the DimChannel table",
        "Online ChannelName in DimChannel table",
        "Online ChannelName on the DimChannel table",
        "Online ChannelName from the DimChannel table",
        "Online ChannelName in the table DimChannel",
        "DimChannel ChannelName Online",
        "the DimChannel table ChannelName Online",
        "Online DimChannel ChannelName",
        "ChannelName DimChannel Online",
        "Online ChannelName DimChannel",
        "DimChannel ChannelName equals Online",
    ],
)
def test_explicit_owner_words_do_not_leak_into_the_role_literal(context, predicate):
    context.tables["OtherChannels"] = ["ChannelName"]
    result = ground_role_filter(
        context,
        f"create a role Team that sees only {predicate}",
        "create_role",
        {"name": "Team", "table": "OtherChannels", "filter": "TRUE()"},
    )
    assert result == {
        "name": "Team",
        "table": "DimChannel",
        "filter": "'DimChannel'[ChannelName] = \"Online\"",
    }
    node = dax.parse_expression(result["filter"])
    assert node.left == dax.ColRef("DimChannel", "ChannelName")
    assert node.right == dax.Str("Online")


@pytest.mark.parametrize(
    "literal",
    [
        "Online in the DimChannel table",
        "Online on DimChannel",
        "Online from the table DimChannel",
        "DimChannel Online",
        "DimChannel ChannelName Online",
        'Online in the "DimChannel" table',
        "Online in the OtherChannels table",
    ],
)
@pytest.mark.parametrize("quote", ['"', "'"])
def test_owner_cleanup_preserves_all_words_inside_quoted_literals(context, literal, quote):
    context.tables["OtherChannels"] = ["ChannelName"]
    quoted = quote + literal.replace(quote, quote + quote) + quote
    result = ground_role_filter(
        context,
        f"create a role Team that sees only {quoted} ChannelName in the DimChannel table",
        "create_role",
        {"name": "Team"},
    )
    assert isinstance(result, dict) and result["table"] == "DimChannel"
    node = dax.parse_expression(result["filter"])
    assert node.left == dax.ColRef("DimChannel", "ChannelName")
    assert node.right == dax.Str(literal)


@pytest.mark.parametrize(
    "predicate",
    [
        "Online ChannelName in the DimChannel table and the OtherChannels table",
        "DimChannel OtherChannels ChannelName Online",
        '"Online in the DimChannel table" ChannelName',
        "Online ChannelName in the DimStore table",
    ],
)
def test_owner_cleanup_does_not_bypass_explicit_table_ambiguity(context, predicate):
    context.tables["OtherChannels"] = ["ChannelName"]
    result = ground_role_filter(
        context,
        f"create a role Team that sees only {predicate}",
        "create_role",
        {"name": "Team", "table": "DimChannel"},
    )
    if " and " in predicate:
        # A complex request retains the checked fallback rather than compiling a partial rule.
        assert result == {"name": "Team", "table": "DimChannel"}
    else:
        assert isinstance(result, Clause) and result.kind is ParseKind.CLARIFY
        assert result.ops == ()


@pytest.mark.parametrize(
    "body",
    [
        "'DimChannel'[ChannelName] = \"Online in the DimChannel table\"",
        'VAR c = "DimChannel ChannelName Online" RETURN DimChannel[ChannelName] = c',
    ],
)
def test_owner_cleanup_never_changes_a_typed_role_predicate(context, body):
    result = ground_role_filter(
        context,
        f"create Team role that sees only Online ChannelName in the DimChannel table "
        f"using predicate: {body}",
        "create_role",
        {"name": "Team", "table": "DimChannel", "filter": "TRUE()"},
    )
    assert result == {"name": "Team", "table": "DimChannel", "filter": body}
