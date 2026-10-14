"""Imported joins retain explicit user settings and validate effective native metadata.

Only the committed synthetic report and fake TOM are used; no Desktop, row sources or user state.
"""

from dataclasses import replace
from typing import Any

import pytest
from editing_support import TWIN
from fake_tom import Model, Server, agents_state, snapshot_of

from ai_powerbi_analyst.studio import desktop_model
from ai_powerbi_analyst.studio import report_edit_model as rem
from ai_powerbi_analyst.studio.edit_context import EditContext
from ai_powerbi_analyst.studio.edit_model_grounding import ground_relationship
from ai_powerbi_analyst.studio.edit_planner import PROMPT_BUDGET, EditPlanner, build_prompt
from ai_powerbi_analyst.studio.edit_types import Clause, ParseKind
from ai_powerbi_analyst.studio.pbir_document import DocumentError, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_import import ImportedReport

ENDS = dict(
    from_table="FactSales", from_column="StoreKey", to_table="DimStore", to_column="StoreKey"
)
PROMOTION = dict(
    from_table="FactSales",
    from_column="PromotionKey",
    to_table="DimPromotion",
    to_column="PromotionKey",
)


def check(op: Any, model: Any) -> Any:
    return rem.check_model_operation(PbirDocument(PbixPackage(TWIN)), op, model)


def test_reversed_update_cardinality_has_same_orientation_in_preflight_tom_and_saved_diff():
    op = rem.UpdateRelationship(
        from_table="DimStore",
        from_column="StoreKey",
        to_table="FactSales",
        to_column="StoreKey",
        cardinality="many_to_one",
        cross_filter="both",
    )
    metadata = rem.example_model()
    check(op, metadata)
    relation = rem.model_structure(metadata).relationship(
        ("FactSales", "StoreKey"), ("DimStore", "StoreKey")
    )
    assert relation is not None and relation.cardinality_known
    assert (relation.from_many, relation.to_many) == (False, True)
    native = Model.from_state(agents_state())
    plan = desktop_model.plan_changes([op], snapshot_of(native))
    assert plan.changes[0]["props"] == {
        "fromCardinality": "One",
        "toCardinality": "Many",
        "crossFilteringBehavior": "BothDirections",
    }
    desktop_model.apply_model_operations([op], server="localhost:1", runner=Server(native))
    assert rem.saved_model_problems([op], snapshot_of(native)) == ([], [])


def test_reversed_update_after_column_and_table_rename_still_maps_native_ends():
    ops = [
        rem.RenameTable(name="FactSales", new_name="Sales"),
        rem.RenameColumn(table="Sales", name="StoreKey", new_name="StoreID"),
        rem.UpdateRelationship(
            from_table="DimStore",
            from_column="StoreKey",
            to_table="Sales",
            to_column="StoreID",
            cardinality="many_to_one",
            cross_filter="both",
        ),
    ]
    native = Model.from_state(agents_state())
    desktop_model.apply_model_operations(ops, server="localhost:1", runner=Server(native))
    assert rem.saved_model_problems(ops, snapshot_of(native)) == ([], [])


def test_reversed_cardinality_with_single_filters_is_refused_before_native_mutation():
    native = Model.from_state(agents_state())
    before = snapshot_of(native)
    op = rem.UpdateRelationship(
        from_table="DimStore",
        from_column="StoreKey",
        to_table="FactSales",
        to_column="StoreKey",
        cardinality="many_to_one",
    )
    with pytest.raises(desktop_model.ModelChangeFailed, match="Configure it in Power BI"):
        desktop_model.plan_changes([op], before)
    assert snapshot_of(native) == before


def test_reversed_endpoint_identification_can_change_activity_without_cardinality_change():
    native = Model.from_state(agents_state())
    op = rem.UpdateRelationship(
        from_table="DimStore",
        from_column="StoreKey",
        to_table="FactSales",
        to_column="StoreKey",
        active=False,
    )
    plan = desktop_model.plan_changes([op], snapshot_of(native))
    assert plan.changes[0]["props"] == {"isActive": False}
    desktop_model.apply_model_operations([op], server="localhost:1", runner=Server(native))
    assert rem.saved_model_problems([op], snapshot_of(native)) == ([], [])


def test_existing_one_to_one_requires_both_even_when_request_omits_cardinality():
    metadata = rem.example_model()
    structure = rem.model_structure(metadata)
    original = structure.relationship(("FactSales", "StoreKey"), ("DimStore", "StoreKey"))
    structure.relationships[structure.relationships.index(original)] = replace(
        original, from_many=False, to_many=False, cardinality_known=True, both=True
    )
    op = rem.UpdateRelationship(**ENDS, cross_filter="single")
    with pytest.raises(DocumentError, match="one-to-one"):
        check(op, metadata)
    native = Model.from_state(agents_state())
    relation = next(r for r in native.Relationships if r.ToTable.Name == "DimStore")
    relation.FromCardinality = relation.ToCardinality = "One"
    relation.CrossFilteringBehavior = "BothDirections"
    before = snapshot_of(native)
    with pytest.raises(desktop_model.ModelChangeFailed, match="one-to-one"):
        desktop_model.plan_changes([op], before)
    assert snapshot_of(native) == before


def test_changing_cross_filter_does_not_silently_weaken_existing_security_filter():
    metadata = rem.example_model()
    structure = rem.model_structure(metadata)
    relation = structure.relationship(("FactSales", "StoreKey"), ("DimStore", "StoreKey"))
    structure.relationships[structure.relationships.index(relation)] = replace(
        relation, both=True, security_both=True
    )
    with pytest.raises(DocumentError, match="explicitly change the security filter"):
        check(rem.UpdateRelationship(**ENDS, cross_filter="single"), metadata)
    result = check(
        rem.UpdateRelationship(**ENDS, cross_filter="single", security_filter="single"), metadata
    )
    assert "security filter in one direction" in result.summary


@pytest.mark.parametrize("cardinality", ["many_to_one", "many_to_many"])
def test_enabling_assume_RI_does_not_infer_key_integrity_from_DQ_modes(cardinality):
    metadata = rem.example_model()
    for table in ("factsales", "dimpromotion"):
        rem.model_structure(metadata).partitions[table] = [{"mode": "directquery"}]
    with pytest.raises(DocumentError, match="source group|many-to-many"):
        check(
            rem.CreateRelationship(
                **PROMOTION,
                cardinality=cardinality,
                assume_referential_integrity=True,
                active=False,
            ),
            metadata,
        )


def test_existing_assume_RI_can_be_preserved_or_disabled_but_not_kept_on_many_to_many():
    metadata = rem.example_model()
    structure = rem.model_structure(metadata)
    relation = structure.relationship(("FactSales", "StoreKey"), ("DimStore", "StoreKey"))
    structure.relationships[structure.relationships.index(relation)] = replace(relation, rely=True)
    assert check(rem.UpdateRelationship(**ENDS, active=False), metadata)
    with pytest.raises(DocumentError, match="many-to-many"):
        check(rem.UpdateRelationship(**ENDS, cardinality="many_to_many"), metadata)
    assert check(
        rem.UpdateRelationship(
            **ENDS, cardinality="many_to_many", assume_referential_integrity=False
        ),
        metadata,
    )


def test_native_batch_checks_relationships_created_earlier_in_the_same_plan():
    native = Model.from_state(agents_state())
    first = rem.CreateRelationship(
        from_table="FactSales",
        from_column="StoreKey",
        to_table="DimEmployee",
        to_column="EmployeeKey",
    )
    second = rem.CreateRelationship(
        from_table="FactSales",
        from_column="SalesKey",
        to_table="DimEmployee",
        to_column="EmployeeKey",
    )
    with pytest.raises(desktop_model.ModelChangeFailed, match="unambiguous active path"):
        desktop_model.plan_changes([first, second], snapshot_of(native))


def context() -> EditContext:
    return EditContext(
        pages=[],
        visuals=[],
        measures=[],
        tables={"Orders": ["CustomerID"], "Customers": ["CustomerID"]},
    )


def card(op):
    return next(c for c in rem.TOOL_CARDS if c.op == op)


class Stub:
    def __init__(self, slots):
        self.slots = slots
        self.planner = None

    def generate(self, _system, _user, response_model, **_options):
        prompt = self.planner.prompt
        slots = {
            side: next(
                i
                for i, field in enumerate(prompt.lists.fields, 1)
                if field[1:] == (table, "CustomerID")
            )
            for side, table in (("from", "Orders"), ("to", "Customers"))
        }
        slots.update(self.slots)
        return response_model.model_validate(
            {"kind": "plan", "steps": [{"tool": 1, "slots": slots}]}
        )


def planned(text, slots, op="create_relationship"):
    stub = Stub(slots)
    planner = EditPlanner(context(), stub, cards=(card(op),))
    stub.planner = planner
    return planner.plan([text])


@pytest.mark.parametrize(
    "text,op",
    [
        (
            "Create an inactive relationship from Orders[CustomerID] to Customers[CustomerID]",
            "create_relationship",
        ),
        (
            "Deactivate the relationship from Orders[CustomerID] to Customers[CustomerID]",
            "update_relationship",
        ),
    ],
)
def test_explicit_inactive_is_preserved_or_rejects_opposite_model_activity(text, op):
    good = planned(text, {"active": False}, op)
    assert len(good.steps) == 1 and good.steps[0].operation["active"] is False
    assert not good.steps[0].notes
    bad = planned(text, {"active": True}, op)
    assert not bad.steps
    assert "active" in repr(bad)


def test_one_to_many_keeps_explicit_endpoints_against_reversed_model_choices():
    text = "Create a one-to-many relationship from Customers[CustomerID] to Orders[CustomerID]"
    outcome = planned(text, {"cardinality": "one_to_many"})
    assert len(outcome.steps) == 1
    op = rem.CreateRelationship.model_validate(outcome.steps[0].operation)
    assert (op.from_table, op.to_table, op.cardinality) == ("Orders", "Customers", "many_to_one")
    assert not planned(text, {"cardinality": "many_to_one"}).steps


def test_relationship_cards_schema_and_builders_expose_every_supported_setting():
    for op in ("create_relationship", "update_relationship"):
        assert {
            "cardinality",
            "cross_filter",
            "active",
            "assume_referential_integrity",
            "security_filter",
        } <= {s.name for s in card(op).slots}
    outcome = planned(
        "Update the relationship from Orders[CustomerID] to Customers[CustomerID], "
        "security both ways",
        {"security_filter": "both"},
        "update_relationship",
    )
    assert outcome.steps[0].operation["security_filter"] == "both"
    assert "cross_filter" not in outcome.steps[0].operation


def test_contradictory_activity_asks_without_using_operation_default():
    result = ground_relationship(
        context(), "create an active and inactive relationship", card("create_relationship"), {}
    )
    assert isinstance(result, Clause) and result.kind == ParseKind.CLARIFY


def test_column_name_inactive_is_an_operand_and_does_not_disable_a_relationship():
    ctx = EditContext(
        pages=[], visuals=[], measures=[], tables={"A": ["Inactive"], "B": ["Inactive"]}
    )
    result = ground_relationship(
        ctx,
        "create a relationship from A[Inactive] to B[Inactive]",
        card("create_relationship"),
        {},
    )
    assert isinstance(result, dict) and "active" not in result


def test_explicit_single_filtering_against_cardinality_asks_instead_of_reversing_mode():
    filled = {
        "from": {"table": "Orders", "name": "CustomerID"},
        "to": {"table": "Customers", "name": "CustomerID"},
        "cardinality": "many_to_one",
    }
    result = ground_relationship(
        context(),
        "create a many-to-one relationship from Orders[CustomerID] "
        "to Customers[CustomerID], filter single from Orders to Customers",
        card("create_relationship"),
        filled,
    )
    assert isinstance(result, Clause) and "one side to the many side" in result.question
    result = ground_relationship(
        context(),
        "create a many-to-one relationship from Orders[CustomerID] "
        "to Customers[CustomerID], filter single from Customers to Orders",
        card("create_relationship"),
        filled,
    )
    assert isinstance(result, dict) and result["cross_filter"] == "single"


@pytest.mark.parametrize("op", ["create_relationship", "update_relationship"])
def test_many_to_many_explicit_single_direction_matches_native_to_from_or_clarifies(op):
    ctx = context()
    ctx.relationships = (
        rem.RelationshipFact(
            "Orders",
            "CustomerID",
            "Customers",
            "CustomerID",
            from_many=True,
            to_many=True,
            cardinality_known=True,
        ),
    )
    ctx.relationships_known = True
    filled = {
        "from": {"table": "Orders", "name": "CustomerID"},
        "to": {"table": "Customers", "name": "CustomerID"},
        "cardinality": "many_to_many",
    }
    compatible = ground_relationship(
        ctx, "many-to-many relationship, filter single from Customers to Orders", card(op), filled
    )
    assert isinstance(compatible, dict) and compatible["cross_filter"] == "single"
    opposite = ground_relationship(
        ctx, "many-to-many relationship, filter single from Orders to Customers", card(op), filled
    )
    assert isinstance(opposite, Clause) and "opposite" in opposite.question


def test_many_to_many_update_uses_stored_orientation_even_with_reversed_endpoint_identity():
    ctx = context()
    ctx.relationships = (
        rem.RelationshipFact(
            "Orders",
            "CustomerID",
            "Customers",
            "CustomerID",
            from_many=True,
            to_many=True,
            cardinality_known=True,
        ),
    )
    ctx.relationships_known = True
    filled = {
        "from": {"table": "Customers", "name": "CustomerID"},
        "to": {"table": "Orders", "name": "CustomerID"},
    }
    result = ground_relationship(
        ctx,
        "update the relationship, filter single from Customers to Orders",
        card("update_relationship"),
        filled,
    )
    assert isinstance(result, dict) and result["cross_filter"] == "single"
    result = ground_relationship(
        ctx,
        "update the relationship, filter single from Orders to Customers",
        card("update_relationship"),
        filled,
    )
    assert isinstance(result, Clause) and result.kind == ParseKind.CLARIFY


def test_many_to_many_requested_single_direction_with_unknown_stored_orientation_clarifies():
    filled = {
        "from": {"table": "Orders", "name": "CustomerID"},
        "to": {"table": "Customers", "name": "CustomerID"},
        "cardinality": "many_to_many",
    }
    result = ground_relationship(
        context(),
        "update many-to-many relationship, filter single from Customers to Orders",
        card("update_relationship"),
        filled,
    )
    assert isinstance(result, Clause) and "direction is unknown" in result.question


def test_model_context_is_current_bounded_metadata_with_no_remote_query_or_raw_values():
    report = ImportedReport(
        id="synthetic",
        workspace_id="synthetic",
        name="Synthetic",
        source_path="fixture.pbix",
        archived_path="fixture.pbix",
        source_hash="a" * 64,
        base_hash="a" * 64,
        format="pbir",
        pages=[],
        tables=[
            {"TableName": "Orders", "ColumnName": "CustomerID"},
            {"TableName": "Customers", "ColumnName": "CustomerID"},
        ],
        model={
            "tables": [{"name": "Orders"}, {"name": "Customers"}],
            "partitions": [
                {"table": t, "mode": "directquery", "expression": "SECRET SOURCE QUERY"}
                for t in ("Orders", "Customers")
            ],
            "relationships": [
                {
                    "fromTable": "Orders",
                    "fromColumn": "CustomerID",
                    "toTable": "Customers",
                    "toColumn": "CustomerID",
                    "cardinality": "M:1",
                    "active": False,
                    "crossFilter": "both",
                    "securityFilter": "single",
                    "relyOnReferentialIntegrity": False,
                }
            ],
            "model": {
                "defaultMode": "Import",
                "connectionString": "SECRET PASSWORD",
                "sample": "PRIVATE ROW",
            },
        },
    )
    current = EditContext.from_report(report, head_revision="r-current")
    assert current.model_revision == "r-current" and current.relationships_known
    assert current.storage_modes == {"Orders": "direct_query", "Customers": "direct_query"}
    assert current.model_settings == {"defaultMode": "Import"}
    prompt = build_prompt(
        "deactivate relationship from Orders[CustomerID] to Customers[CustomerID]",
        current,
        [card("update_relationship")],
    )
    assert prompt.size <= PROMPT_BUDGET
    assert (
        "r-current" in prompt.user and "inactive" in prompt.user and "direct_query" in prompt.user
    )
    assert "source groups" in prompt.user.lower()
    assert all(secret not in prompt.user for secret in ("SECRET", "PRIVATE ROW"))
