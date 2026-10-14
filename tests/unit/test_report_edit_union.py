"""The shape of the split ``report_edits``: one union, family tuples, handlers, no cycles.

The behavior of every operation is pinned by test_report_edits_characterization.py; this file
checks the wiring a family relies on when it adds an operation: the union that validates a
batch is the union of the family unions, every operation has exactly one handler in its own
family, the family modules import in one direction only, and ``parse_operations`` accepts what
``EditBatch`` accepts and explains what it refuses.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import TypeAdapter

from ai_powerbi_analyst.studio import (
    report_edit_conditional,
    report_edit_context,
    report_edit_extras,
    report_edit_filters,
    report_edit_layout,
    report_edit_measures,
    report_edit_model,
    report_edit_navigation,
    report_edit_ops,
    report_edit_pages,
    report_edit_validation,
    report_edits,
)
from ai_powerbi_analyst.studio.pbir_document import DocumentError

STUDIO = Path(report_edits.__file__).parent

FAMILIES: dict[str, tuple[tuple[type, ...], dict[str, Any]]] = {
    "layout": (report_edit_layout.LAYOUT_OPERATIONS, report_edit_layout.LAYOUT_HANDLERS),
    "pages": (report_edit_pages.PAGE_OPERATIONS, report_edit_pages.PAGE_HANDLERS),
    "measures": (report_edit_measures.MEASURE_OPERATIONS, report_edit_measures.MEASURE_HANDLERS),
    "filters": (report_edit_filters.FILTER_OPERATIONS, report_edit_filters.FILTER_HANDLERS),
    "conditional": (
        report_edit_conditional.CONDITIONAL_OPERATIONS,
        report_edit_conditional.CONDITIONAL_HANDLERS,
    ),
    "navigation": (
        report_edit_navigation.NAVIGATION_OPERATIONS,
        report_edit_navigation.NAVIGATION_HANDLERS,
    ),
    "extras": (report_edit_extras.EXTRAS_OPERATIONS, report_edit_extras.EXTRAS_HANDLERS),
    "model": (report_edit_model.MODEL_FAMILY_OPERATIONS, report_edit_model.MODEL_HANDLERS),
}


def op_name(cls: type) -> str:
    return str(cls.model_fields["op"].default)  # type: ignore[attr-defined]


def union_members() -> tuple[type, ...]:
    """The classes the ``ReportOperation`` annotation validates a batch step into."""
    union, _discriminator = get_args(report_edits.ReportOperation)
    members: tuple[type, ...] = get_args(union)
    return members


# --- the union, the tuples and the handlers agree -----------------------------------------------


def test_the_union_holds_the_hundred_and_three_operations_in_family_order() -> None:
    assert [op_name(cls) for cls in union_members()] == [
        *("set_position", "arrange", "set_visual_hidden", "delete_visual", "duplicate_visual"),
        *("add_visual", "change_visual_type", "set_fields", "set_sort"),
        *("align_visuals", "distribute_visuals", "swap_visuals", "place_visual"),
        *("add_textbox", "add_shape"),
        *("rename_page", "add_page", "duplicate_page", "delete_page", "move_page"),
        *("set_page_size", "set_page_hidden"),
        *("create_measure", "update_measure", "rename_measure", "delete_measure"),
        *("set_page_filter", "set_visual_filter", "set_report_filter", "clear_filter"),
        # conditional
        *("set_conditional_format", "clear_conditional_format", "set_number_format"),
        *("set_table_layout", "move_column"),
        # navigation
        *("add_bookmark", "update_bookmark", "rename_bookmark", "move_bookmark"),
        *("group_bookmarks", "ungroup_bookmarks", "delete_bookmark", "set_bookmark_visual_hidden"),
        *("add_button", "set_button_action", "add_navigator"),
        *("set_navigator", "set_drillthrough", "set_tooltip_page", "clear_page_type"),
        *("set_visual_tooltip", "set_interaction", "reset_interactions", "sync_slicer"),
        *("unsync_slicer", "set_drill_filter", "drill_visual"),
        # extras
        *("set_analytics_line", "remove_analytics_line", "place_on_mobile", "remove_from_mobile"),
        *("arrange_mobile_layout", "group_visuals", "ungroup_visuals", "set_group"),
        *("set_visual_calculation", "remove_visual_calculation", "set_small_multiples"),
        *("set_alt_text", "set_tab_order"),
        *("set_keep_layer_order", "set_page_view", "set_field_display_name"),
        # model
        *("create_calculated_column", "set_column_properties", "rename_column", "delete_column"),
        *("create_calculated_table", "set_table_properties", "rename_table", "delete_table"),
        *("mark_date_table", "create_relationship", "update_relationship"),
        *("delete_relationship", "create_hierarchy", "update_hierarchy", "delete_hierarchy"),
        *("create_field_parameter", "update_field_parameter", "create_calculation_group"),
        *("set_calculation_item", "delete_calculation_item", "update_calculation_group"),
        *("create_role", "update_role", "delete_role", "set_table_query"),
        *("set_named_expression", "delete_named_expression", "set_query_parameter"),
        *("refresh_data", "set_model_options"),
    ]


def test_the_union_is_the_family_tuples_together() -> None:
    families = [cls for classes, _ in FAMILIES.values() for cls in classes]
    assert list(report_edits.REPORT_OPERATIONS) == families
    assert list(union_members()) == families
    assert len(families) == 103


def test_every_operation_has_one_handler_in_its_own_family() -> None:
    for name, (classes, handlers) in FAMILIES.items():
        assert sorted(handlers) == sorted(op_name(cls) for cls in classes), name


def test_the_facade_dispatches_to_exactly_the_operations_of_the_union() -> None:
    assert sorted(report_edits._HANDLERS) == sorted(op_name(cls) for cls in union_members())


def test_no_two_operations_share_an_op_name() -> None:
    names = [op_name(cls) for cls in report_edits.REPORT_OPERATIONS]
    assert len(names) == len(set(names))


def test_model_operations_are_the_measure_and_model_family_operations() -> None:
    """The editor sends every operation of these classes to Power BI Desktop."""
    measures, family = report_edit_measures, report_edit_model
    expected = [*measures.MEASURE_OPERATIONS, *family.MODEL_FAMILY_OPERATIONS]
    assert list(report_edits.MODEL_OPERATIONS) == expected
    assert [op_name(c) for c in report_edits.MODEL_OPERATIONS][:4] == [
        "create_measure",
        "update_measure",
        "rename_measure",
        "delete_measure",
    ]
    assert len(report_edits.MODEL_OPERATIONS) == 34


def test_only_renames_have_a_report_half() -> None:
    assert sorted(report_edits.REPORT_HALF_HANDLERS) == [
        "rename_column",
        "rename_measure",
        "rename_table",
    ]
    for name, handler in report_edits.REPORT_HALF_HANDLERS.items():
        assert (
            handler
            is {
                **report_edit_measures.MEASURE_REPORT_HANDLERS,
                **report_edit_model.MODEL_REPORT_HANDLERS,
            }[name]
        )


def test_a_class_is_one_object_whichever_module_it_is_imported_from() -> None:
    assert report_edits.SetPosition is report_edit_ops.SetPosition
    assert report_edits.ModelInfo is report_edit_context.ModelInfo
    assert report_edits.validate_document is report_edit_validation.validate_document
    assert report_edits.ADDABLE_TYPES is report_edit_layout.ADDABLE_TYPES


def test_operation_classes_takes_a_union_or_a_single_class() -> None:
    assert report_edit_ops.operation_classes(report_edit_ops.SetPosition) == (
        report_edit_ops.SetPosition,
    )
    both = report_edit_ops.SetPosition | report_edit_ops.Arrange
    assert report_edit_ops.operation_classes(both) == (
        report_edit_ops.SetPosition,
        report_edit_ops.Arrange,
    )


def test_the_annotation_validates_a_step_into_its_class_by_op() -> None:
    step = {"op": "move_page", "page": "Sales", "index": 2}
    parsed: Any = TypeAdapter(report_edits.ReportOperation).validate_python(step)
    assert isinstance(parsed, report_edit_ops.MovePage)
    batch = report_edits.EditBatch.model_validate({"operations": [step]})
    assert batch.operations == [parsed]


# --- the modules import in one direction ----------------------------------------------------------


def imported_modules(module: str) -> set[str]:
    """The sibling modules a studio module imports, from ``from .x import`` statements."""
    tree = ast.parse((STUDIO / f"{module}.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 1:
            found.update(alias.name for alias in node.names)
    return found


def test_ops_imports_no_edit_module() -> None:
    assert not {m for m in imported_modules("report_edit_ops") if m.startswith("report_edit")}


def test_context_imports_only_ops() -> None:
    edit = {m for m in imported_modules("report_edit_context") if m.startswith("report_edit")}
    assert edit == {"report_edit_ops"}


# Beyond ops and context, a module may import the helper module of its own family and nothing
# else: layout builds on align (six of its operations) and geometry (page-pixel boxes shared
# by both). The round-3
# families use geometry to resolve and describe visuals the way the layout family does.
SHARED = {"report_edit_ops", "report_edit_context"}
FAMILY_MODULES: dict[str, set[str]] = {
    "align": {"report_edit_geometry"},
    "geometry": set(),
    "layout": {"report_edit_align", "report_edit_geometry"},
    "pages": set(),
    "measures": set(),
    "filters": set(),
    "validation": set(),
    "conditional": {"report_edit_geometry"},
    "navigation": {"report_edit_geometry"},
    "extras": {"report_edit_geometry"},
    "model": set(),
}


@pytest.mark.parametrize("module", sorted(FAMILY_MODULES))
def test_a_family_imports_only_ops_and_context(module: str) -> None:
    name = f"report_edit_{module}"
    edit = {m for m in imported_modules(name) if m.startswith("report_edit")}
    assert edit <= SHARED | FAMILY_MODULES[module], edit


def test_the_family_modules_form_no_cycle() -> None:
    """Follow every ``report_edit_*`` import from every module: none leads back to itself."""
    graph = {
        f"report_edit_{module}": {
            m for m in imported_modules(f"report_edit_{module}") if m.startswith("report_edit")
        }
        for module in [*FAMILY_MODULES, "ops", "context"]
    }

    def reaches(start: str, seen: frozenset[str] = frozenset()) -> set[str]:
        found: set[str] = set()
        for dependency in graph.get(start, set()) - seen:
            found |= {dependency} | reaches(dependency, seen | {dependency})
        return found

    for name in graph:
        assert name not in reaches(name), name


# --- parse_operations ----------------------------------------------------------------------


def test_parse_operations_gives_what_edit_batch_gives() -> None:
    steps: list[dict[str, Any]] = [
        {"op": "set_position", "visual": "v1", "x": 100},
        {"op": "add_page", "name": "Summary"},
        {"op": "delete_measure", "name": "Margin"},
    ]
    assert (
        report_edits.parse_operations(steps)
        == report_edits.EditBatch.model_validate({"operations": steps}).operations
    )


def test_parse_operations_returns_typed_operations() -> None:
    parsed = report_edits.parse_operations([{"op": "arrange", "visual": "v", "to": "back"}])
    assert len(parsed) == 1
    assert isinstance(parsed[0], report_edit_ops.Arrange)
    assert parsed[0].to == "back"


# The shortest valid step of each operation added after the first thirty.
NEW_STEPS: dict[str, dict[str, Any]] = {
    "align_visuals": {"op": "align_visuals", "visuals": ["a"], "edge": "left", "to": "page"},
    "distribute_visuals": {"op": "distribute_visuals", "visuals": ["a", "b"], "axis": "vertical"},
    "swap_visuals": {"op": "swap_visuals", "a": "a", "b": "b"},
    "place_visual": {"op": "place_visual", "visual": "a", "relative_to": "b", "side": "below"},
    "add_textbox": {"op": "add_textbox", "page": "p", "text": "Hello"},
    "add_shape": {"op": "add_shape", "page": "p"},
    "set_page_filter": {
        "op": "set_page_filter",
        "page": "p",
        "filter": {"kind": "categorical", "field": {"name": "Year"}, "values": [2019]},
    },
    "set_visual_filter": {
        "op": "set_visual_filter",
        "visual": "v",
        "filter": {"kind": "categorical", "field": {"name": "Year"}, "values": [2019]},
    },
    "set_report_filter": {
        "op": "set_report_filter",
        "filter": {"kind": "categorical", "field": {"name": "Year"}, "values": [2019]},
    },
    "clear_filter": {"op": "clear_filter", "scope": "report"},
    # one of each round-3 family (test_round3_integration.py parses every card example)
    "move_column": {"op": "move_column", "visual": "v", "field": "Sales", "to_index": 0},
    "rename_bookmark": {"op": "rename_bookmark", "bookmark": "A", "name": "B"},
    "set_alt_text": {"op": "set_alt_text", "visual": "v", "text": "Sales by month"},
    "rename_column": {"op": "rename_column", "table": "T", "name": "a", "new_name": "b"},
}


def test_every_operation_added_later_is_accepted_and_typed_by_parse_operations() -> None:
    known = {op_name(cls) for cls in union_members()}
    assert set(NEW_STEPS) <= known
    for name, step in NEW_STEPS.items():
        (parsed,) = report_edits.parse_operations([step])
        assert parsed.op == name
        assert type(parsed) in union_members()
    # a step of the new families that is wrong is explained like any other
    with pytest.raises(DocumentError) as raised:
        report_edits.parse_operations([{"op": "align_visuals", "visuals": ["a"], "edge": "middle"}])
    assert str(raised.value).startswith("Step 1 (align_visuals): 'edge' is not valid")
    with pytest.raises(DocumentError) as raised:
        report_edits.parse_operations([{"op": "rename_column", "table": "T", "name": "a"}])
    assert str(raised.value) == "Step 1 (rename_column) needs 'new_name'"


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([], "Say what to change: the batch has no steps"),
        (
            [{"op": "arrange", "visual": "v", "to": "front"}] * 51,
            "A batch holds at most 50 steps; this one has 51",
        ),
        (["set_position"], "Step 1 is not an operation: expected a set of settings"),
        ([{"visual": "v"}], "Step 1 does not say what to do (it has no 'op')"),
        (
            [{"op": "set_positon", "visual": "v"}],
            "Step 1: 'set_positon' is not an operation Studio knows; did you mean 'set_position'?",
        ),
        (
            [{"op": "explode", "visual": "v"}],
            "Step 1: 'explode' is not an operation Studio knows",
        ),
        (
            [{"op": "set_position", "visual": 3}],
            "Step 1 (set_position): 'visual' is not valid: input should be a valid string",
        ),
        ([{"op": "arrange", "visual": "v"}], "Step 1 (arrange) needs 'to'"),
        (
            [{"op": "arrange", "visual": "v", "to": "front", "speed": 3}],
            "Step 1 (arrange) has no setting called 'speed'",
        ),
        (
            [{"op": "arrange", "visual": "v", "to": "sideways"}],
            "Step 1 (arrange): 'to' is not valid: input should be 'front', 'back', 'forward' or "
            "'backward'",
        ),
        (
            [{"op": "add_shape", "page": "p", "fill": "red"}],
            "Step 1 (add_shape): 'fill' is not in the expected format (colors look like #1E90FF)",
        ),
        (
            [{"op": "add_shape", "page": "p", "fill": {"theme": 99}}],
            "Step 1 (add_shape): 'fill' (theme) is not valid: input should be less than or "
            "equal to 41",
        ),
        (
            [{"op": "add_visual", "page": "p", "visual_type": "card", "fields": []}],
            "Step 1 (add_visual): 'fields' is not valid: list should have at least 1 item after "
            "validation, not 0",
        ),
        (
            [{"op": "add_visual", "page": "p", "visual_type": "card", "fields": [{"table": "T"}]}],
            "Step 1 (add_visual) needs 'fields' (item 1, name)",
        ),
        (
            [{"op": "set_position", "visual": "v", "x": 10}, {"op": "set_position"}],
            "Step 2 (set_position) needs 'visual'",
        ),
    ],
)
def test_parse_operations_says_which_step_is_wrong_and_why(steps: list[Any], message: str) -> None:
    with pytest.raises(DocumentError) as raised:
        report_edits.parse_operations(steps)
    assert str(raised.value) == message
