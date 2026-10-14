"""Characterization of the report edit operations: what each one writes, byte for byte.

``report_edits`` was split into family modules (report_edit_layout,
report_edit_pages, report_edit_measures, report_edit_validation) without changing behavior.
This test is what proves it: every scenario below runs a batch of operations on one of the
imported-report fixtures and compares, with a golden file recorded from the code before the
split, the summaries, the model-layer operations, the validation problems, and every report
part the batch changed (as a hash of its compact JSON, plus the text of the small ones).

A scenario is a name, a fixture, a batch of operations written as dicts (the way chat and the
HTTP API send them) and, for the validation scenarios, a mutation applied after the batch.

When a change to an operation is deliberate (a bug fix, a new default), do NOT re-record the
baseline. Record the scenarios you changed in your own override file, which several families
can do at the same time without touching each other's:

    .venv\\Scripts\\python.exe tests\\unit\\test_report_edits_characterization.py
        --record-family <family> --reason "<why the output changed>" [<scenario> ...]

With no scenario names it records every scenario whose output now differs from the golden
(the baseline plus every override). Read ``golden/overrides/<family>.json`` before committing
it. Two override files may not change the same scenario (a test says so): settle it by hand.
Only ``--record`` (the whole baseline) rewrites ``golden/report_edits_baseline.json`` and
deletes the overrides, once a round of family changes is finished.

Only scenarios whose names appear in the golden are compared, so a family adds scenarios for
its new operations in its own test file and leaves this one alone.

The golden pins what the code says today, not what it should say. Known wording debts, recorded
as they were (a later cleanup will need one more ``--record``):

- ``report_edit_pages`` writes "button eff2d7b0" (a raw short id) for buttons that navigate to a
  deleted page (``page_delete``, ``page_delete_with_cleanup``).
"""

from __future__ import annotations

import ast
import copy
import hashlib
import itertools
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from pydantic import ValidationError

from ai_powerbi_analyst.studio import report_edits
from ai_powerbi_analyst.studio.pbir_document import DocumentError, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage, decode_json

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden" / "report_edits_baseline.json"
OVERRIDES = HERE / "golden" / "overrides"
FIXTURES = HERE.parents[1] / "fixtures" / "imported"

# Parts up to this size are stored as text next to their hash, so a failing scenario can be
# read from the golden; bigger ones (a duplicated page) are compared by hash alone.
TEXT_LIMIT = 2500

PAGE1 = "ReportSection8454c812b60b8bb83b99"
DETAILS = "ReportSection3f1c0d5e7a9b2c4d6e8f"
DRILL = "ReportSectiond2a4c6e8f0b1a3c5e7d9"
TOOLTIP = "ReportSectionb7e9f1a3c5d7e9f1a3b5"

TABLE = "e82e02f93055d6b0b544"  # tableEx
TOP_SLICER = "953a59a3cad98b0b5a39"  # slicer with a container title
ADVANCED = "29ba010b20b2162d1536"  # advancedSlicerVisual
CARD_BARE = "fb4c5aa34e0b084b62b9"  # cardVisual, no container title
CARD = "2202e311b1493069e4eb"  # cardVisual with a container title
COLUMN = "b404a461ede7604a2774"  # columnChart with per-category fills, a sort, no legend
COLUMN_2 = "da32cdbb0090e885649b"
STORE_SLICER = "5b0dbccd61c6c4adb415"
CHANNEL_SLICER = "54be5cf0783e66471540"
DUMMY_CARD = "8a8ba163374a72762030"
BUTTON = "eff2d7b04706cc92f1db"  # multipage only
GROUP = "40c896eda14bfa8f6a97"  # multipage only: Header block, holds a group and a card
INNER_GROUP = "7799291338ca3548d7d5"
IMAGE = "81ab76274a5b4e9e62da"

Mutation = Callable[[PbirDocument], None]


@dataclass(frozen=True)
class Scenario:
    fixture: str
    ops: list[dict[str, Any]]
    mutate: Mutation | None = None


SCENARIOS: dict[str, Scenario] = {}


def scenario(
    name: str,
    ops: list[dict[str, Any]],
    fixture: str = "structure-twin",
    mutate: Mutation | None = None,
) -> None:
    assert name not in SCENARIOS
    SCENARIOS[name] = Scenario(fixture, ops, mutate)


MULTI = "multipage"


# --- the model the operations check against ---------------------------------------------------


def make_model() -> report_edits.ModelInfo:
    tables = [
        ("DimEmployee", "EmployeeKey", "int64"),
        ("DimEmployee", "Salary", "float64"),
        ("DimEmployee", "Hired", "datetime64[ns]"),
        ("DimStore", "StoreName", "object"),
        ("DimStore", "StoreKey", "int64"),
        ("DimChannel", "ChannelName", "object"),
        ("Clustered Employees", "Clustered Employees", "object"),
        ("Top-Bottom-N", "Top-Bottom-N", "object"),
        ("Actuals-vs-PctChange", "Top-Bottom-Field", "object"),
    ]
    measures = [
        "Rank",
        "Employees Avg MTD Sales",
        "Employees Avg MTD % Change",
        "Table MTD Sales",
        "TopN/BottomN/ MTD Sales",
        "dummy",
        "Unused Ratio",
    ]
    return report_edits.ModelInfo.from_report(
        [{"TableName": t, "ColumnName": c, "PandasDataType": d} for t, c, d in tables],
        [{"Name": m, "TableName": "# Measures"} for m in measures],
    )


# --- running a scenario ----------------------------------------------------------------------


@contextmanager
def sequential_names() -> Iterator[None]:
    """PbirDocument.new_name draws from ``secrets``; a counter makes new ids repeatable."""
    counter = itertools.count(1)

    def new_name(self: PbirDocument, used: set[str]) -> str:
        while True:
            candidate = f"{next(counter):020x}"
            if candidate not in used:
                return candidate

    with mock.patch.object(PbirDocument, "new_name", new_name):
        yield


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _part_record(package: PbixPackage, name: str) -> dict[str, Any]:
    data = package.read(name)
    text = _dump(decode_json(data)) if name.endswith(".json") else None
    payload = text.encode("utf-8") if text is not None else data
    record: dict[str, Any] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
    if text is not None and len(text) <= TEXT_LIMIT:
        record["text"] = text
    return record


def run_scenario(item: Scenario) -> dict[str, Any]:
    package = PbixPackage(FIXTURES / f"{item.fixture}.pbix")
    before = set(package.names())
    document = PbirDocument(package)
    model = make_model()
    with sequential_names():
        try:
            batch = report_edits.EditBatch.model_validate({"operations": item.ops})
        except ValidationError as error:
            return {
                "invalid": [[e["type"], [str(part) for part in e["loc"]]] for e in error.errors()]
            }
        try:
            outcome = report_edits.apply_operations(document, batch.operations, model)
        except report_edits.NeedsConfirmation as error:
            return {"error": str(error), "asks": {"kind": error.kind, "step": error.step}}
        except report_edits.NeedsChoice as error:
            return {
                "error": str(error),
                "asks": {
                    "step": error.step,
                    "options": [{"label": o.label, "fill": o.fill} for o in error.options],
                },
            }
        except DocumentError as error:
            return {"error": str(error)}
        if item.mutate is not None:
            item.mutate(document)
        document.commit()
    after = set(package.names())
    changed = sorted(n for n in after if package.is_modified(n))
    return {
        "summaries": outcome.summaries,
        "model_operations": [op.model_dump(mode="json") for op in outcome.model_operations],
        "warnings": outcome.warnings,
        "report_changed": outcome.report_changed,
        "measures": dict(sorted(model.measures.items())),
        "problems": report_edits.validate_document(document),
        "parts": {name: _part_record(package, name) for name in changed},
        "removed": sorted(before - after),
    }


# --- scenarios: placement and structure of visuals --------------------------------------------

scenario("position_move", [{"op": "set_position", "visual": CARD, "x": 100, "y": 50}])
scenario("position_resize", [{"op": "set_position", "visual": CARD, "width": 500, "height": 120}])
scenario(
    "position_clamped_to_the_page",
    [{"op": "set_position", "visual": CARD, "x": 5000, "y": 5000, "width": 9000}],
)
scenario("position_nothing", [{"op": "set_position", "visual": CARD}])
scenario("position_on_a_group", [{"op": "set_position", "visual": GROUP, "x": 10}], fixture=MULTI)
scenario("position_unknown_visual", [{"op": "set_position", "visual": "nope", "x": 10}])
scenario("position_out_of_range", [{"op": "set_position", "visual": CARD, "x": -5}])

for direction in ("front", "back", "forward", "backward"):
    scenario(f"arrange_{direction}", [{"op": "arrange", "visual": COLUMN, "to": direction}])
scenario("arrange_unknown_visual", [{"op": "arrange", "visual": "nope", "to": "front"}])
scenario("arrange_the_bottom_visual_back", [{"op": "arrange", "visual": TABLE, "to": "back"}])
scenario(
    "arrange_the_top_visual_forward",
    [{"op": "arrange", "visual": BUTTON, "to": "forward"}],
    fixture=MULTI,
)
scenario(
    "arrange_two_in_a_row",
    [
        {"op": "arrange", "visual": CARD, "to": "front"},
        {"op": "arrange", "visual": COLUMN, "to": "back"},
    ],
)

scenario("hide_visual", [{"op": "set_visual_hidden", "visual": CARD, "hidden": True}])
scenario("show_visual", [{"op": "set_visual_hidden", "visual": CARD, "hidden": False}])
scenario("hide_unknown_visual", [{"op": "set_visual_hidden", "visual": "nope", "hidden": True}])
scenario(
    "hide_a_group", [{"op": "set_visual_hidden", "visual": GROUP, "hidden": True}], fixture=MULTI
)

scenario("delete_visual", [{"op": "delete_visual", "visual": COLUMN}])
scenario(
    "delete_a_group_and_its_visuals", [{"op": "delete_visual", "visual": GROUP}], fixture=MULTI
)
scenario("delete_an_inner_group", [{"op": "delete_visual", "visual": INNER_GROUP}], fixture=MULTI)
scenario("delete_unknown_visual", [{"op": "delete_visual", "visual": "nope"}])
scenario(
    "delete_two_visuals",
    [{"op": "delete_visual", "visual": COLUMN}, {"op": "delete_visual", "visual": COLUMN_2}],
)

scenario("duplicate_on_the_same_page", [{"op": "duplicate_visual", "visual": COLUMN}])
scenario("duplicate_at_a_spot", [{"op": "duplicate_visual", "visual": CARD, "x": 10, "y": 20}])
scenario(
    "duplicate_onto_another_page",
    [{"op": "duplicate_visual", "visual": COLUMN, "page": "Details"}],
    fixture=MULTI,
)
scenario(
    "duplicate_a_grouped_visual",
    [{"op": "duplicate_visual", "visual": "04ba32659627f6e8c7d7"}],
    fixture=MULTI,
)
scenario(
    "duplicate_onto_a_small_page",
    [{"op": "duplicate_visual", "visual": TABLE, "page": TOOLTIP}],
    fixture=MULTI,
)
scenario("duplicate_a_group", [{"op": "duplicate_visual", "visual": GROUP}], fixture=MULTI)
scenario(
    "duplicate_to_an_unknown_page",
    [{"op": "duplicate_visual", "visual": CARD, "page": "Nowhere"}],
    fixture=MULTI,
)
scenario(
    "duplicate_twice",
    [{"op": "duplicate_visual", "visual": CARD}, {"op": "duplicate_visual", "visual": CARD}],
)

scenario(
    "add_column_chart",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "clusteredColumnChart",
            "fields": [
                {"table": "DimChannel", "name": "ChannelName"},
                {"table": "# Measures", "name": "Rank"},
            ],
            "title": "Rank by channel",
        }
    ],
)
scenario(
    "add_card",
    [
        {
            "op": "add_visual",
            "page": "1",
            "visual_type": "card",
            "fields": [{"table": "# Measures", "name": "Table MTD Sales"}],
            "x": 5000,
            "y": 5000,
            "width": 300,
            "height": 100,
        }
    ],
)
scenario(
    "add_aggregated_column",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "columnChart",
            "fields": [
                {"table": "DimStore", "name": "StoreName"},
                {"table": "DimEmployee", "name": "Salary", "aggregation": "Average"},
            ],
        }
    ],
)
scenario(
    "add_numeric_column_with_no_aggregation_to_a_pie",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "pieChart",
            "fields": [
                {"table": "dimstore", "name": "storename"},
                {"table": "DimEmployee", "name": "salary"},
            ],
        }
    ],
)
scenario(
    "add_names_in_the_wrong_case",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "tableEx",
            "fields": [
                {"table": "dimstore", "name": "storename"},
                {"table": "DIMEMPLOYEE", "name": "salary"},
            ],
        }
    ],
)
scenario(
    "add_table",
    [
        {
            "op": "add_visual",
            "page": DETAILS,
            "visual_type": "tableEx",
            "fields": [
                {"table": "DimEmployee", "name": "EmployeeKey"},
                {"table": "DimStore", "name": "StoreName"},
                {"table": "# Measures", "name": "Rank"},
            ],
            "width": 5000,
        }
    ],
    fixture=MULTI,
)
scenario(
    "add_slicer",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "slicer",
            "fields": [{"table": "DimChannel", "name": "ChannelName"}],
        }
    ],
)
scenario(
    "add_scatter",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "scatterChart",
            "fields": [
                {"table": "DimStore", "name": "StoreName"},
                {"table": "# Measures", "name": "Rank"},
                {"table": "# Measures", "name": "Table MTD Sales"},
            ],
        }
    ],
)
scenario(
    "add_scatter_of_two_measures",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "scatterChart",
            "fields": [
                {"table": "# Measures", "name": "Table MTD Sales"},
                {"table": "# Measures", "name": "Rank"},
            ],
        }
    ],
)
scenario(
    "add_a_type_studio_cannot_add",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "map",
            "fields": [{"table": "DimStore", "name": "StoreName"}],
        }
    ],
)
scenario(
    "add_an_unknown_type",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "hologram",
            "fields": [{"table": "DimStore", "name": "StoreName"}],
        }
    ],
)
scenario(
    "add_with_no_room_for_a_field",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "card",
            "fields": [
                {"table": "# Measures", "name": "Rank"},
                {"table": "# Measures", "name": "dummy"},
            ],
        }
    ],
)
scenario(
    "add_missing_a_required_role",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "clusteredColumnChart",
            "fields": [{"table": "DimStore", "name": "StoreName"}],
        }
    ],
)
scenario(
    "add_an_unknown_field",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "card",
            "fields": [{"table": "DimStore", "name": "Nope"}],
        }
    ],
)
scenario(
    "add_from_an_unknown_table",
    [
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "card",
            "fields": [{"table": "Nope", "name": "Rank"}],
        }
    ],
)
scenario(
    "add_to_an_unknown_page",
    [
        {
            "op": "add_visual",
            "page": "Nowhere",
            "visual_type": "card",
            "fields": [{"table": "# Measures", "name": "Rank"}],
        }
    ],
)
scenario(
    "add_without_fields", [{"op": "add_visual", "page": PAGE1, "visual_type": "card", "fields": []}]
)

scenario(
    "type_column_to_line",
    [{"op": "change_visual_type", "visual": COLUMN, "visual_type": "lineChart"}],
)
scenario(
    "type_column_to_pie",
    [{"op": "change_visual_type", "visual": COLUMN_2, "visual_type": "pieChart"}],
)
scenario(
    "type_card_to_table", [{"op": "change_visual_type", "visual": CARD, "visual_type": "tableEx"}]
)
scenario(
    "type_table_to_card", [{"op": "change_visual_type", "visual": TABLE, "visual_type": "card"}]
)
scenario(
    "type_slicer_to_table",
    [{"op": "change_visual_type", "visual": CHANNEL_SLICER, "visual_type": "tableEx"}],
)
scenario(
    "type_to_one_studio_cannot_make",
    [{"op": "change_visual_type", "visual": COLUMN, "visual_type": "map"}],
)
scenario(
    "type_of_a_group",
    [{"op": "change_visual_type", "visual": GROUP, "visual_type": "lineChart"}],
    fixture=MULTI,
)

scenario(
    "fields_replace",
    [
        {
            "op": "set_fields",
            "visual": COLUMN,
            "role": "Y",
            "fields": [{"table": "# Measures", "name": "Rank"}],
        }
    ],
)
scenario(
    "fields_add",
    [
        {
            "op": "set_fields",
            "visual": COLUMN,
            "role": "y",
            "mode": "add",
            "fields": [{"table": "# Measures", "name": "Table MTD Sales"}],
        }
    ],
)
scenario(
    "fields_remove",
    [
        {
            "op": "set_fields",
            "visual": COLUMN,
            "role": "Category",
            "mode": "remove",
            "fields": [{"table": "Clustered Employees", "name": "clustered employees"}],
        }
    ],
)
scenario(
    "fields_remove_a_measure_with_a_sort",
    [
        {
            "op": "set_fields",
            "visual": COLUMN,
            "role": "Y",
            "mode": "remove",
            "fields": [{"table": "# Measures", "name": "TopN/BottomN/ MTD Sales"}],
        }
    ],
)
scenario(
    "fields_replace_a_sorted_measure",
    [
        {
            "op": "set_fields",
            "visual": COLUMN,
            "role": "Y",
            "fields": [
                {"table": "# Measures", "name": "Rank"},
                {"table": "# Measures", "name": "dummy"},
            ],
        }
    ],
)
scenario(
    "fields_add_to_a_table",
    [
        {
            "op": "set_fields",
            "visual": TABLE,
            "role": "Values",
            "mode": "add",
            "fields": [
                {"table": "DimStore", "name": "StoreName"},
                {"table": "DimEmployee", "name": "Salary"},
            ],
        }
    ],
)
scenario(
    "fields_add_an_aggregated_column",
    [
        {
            "op": "set_fields",
            "visual": COLUMN,
            "role": "Y",
            "mode": "replace",
            "fields": [{"table": "DimEmployee", "name": "Salary", "aggregation": "Max"}],
        }
    ],
)
scenario(
    "fields_more_than_the_role_takes",
    [
        {
            "op": "set_fields",
            "visual": COLUMN,
            "role": "Category",
            "mode": "add",
            "fields": [{"table": "DimStore", "name": "StoreName"}],
        }
    ],
)
scenario(
    "fields_unknown_role",
    [
        {
            "op": "set_fields",
            "visual": COLUMN,
            "role": "Sparkle",
            "fields": [{"table": "# Measures", "name": "Rank"}],
        }
    ],
)
scenario(
    "fields_remove_what_is_not_there",
    [
        {
            "op": "set_fields",
            "visual": COLUMN,
            "role": "Y",
            "mode": "remove",
            "fields": [{"table": "# Measures", "name": "Rank"}],
        }
    ],
)
scenario(
    "fields_replace_with_nothing",
    [{"op": "set_fields", "visual": COLUMN, "role": "Y", "fields": []}],
)
scenario(
    "fields_on_an_unlisted_type",
    [
        {
            "op": "set_fields",
            "visual": BUTTON,
            "role": "Values",
            "fields": [{"table": "# Measures", "name": "Rank"}],
        }
    ],
    fixture=MULTI,
)

scenario(
    "sort_by_a_measure",
    [
        {
            "op": "set_sort",
            "visual": COLUMN,
            "field": {"table": "# Measures", "name": "TopN/BottomN/ MTD Sales"},
            "direction": "ascending",
        }
    ],
)
scenario(
    "sort_by_a_column_default_direction",
    [
        {
            "op": "set_sort",
            "visual": COLUMN,
            "field": {"table": "Clustered Employees", "name": "Clustered Employees"},
        }
    ],
)
scenario(
    "sort_by_a_field_not_in_the_visual",
    [{"op": "set_sort", "visual": COLUMN, "field": {"table": "# Measures", "name": "Rank"}}],
)
scenario(
    "sort_by_an_unknown_field",
    [{"op": "set_sort", "visual": COLUMN, "field": {"table": "# Measures", "name": "Ghost"}}],
)

# --- scenarios: pages --------------------------------------------------------------------------

scenario(
    "page_rename", [{"op": "rename_page", "page": "Details", "name": "Overview"}], fixture=MULTI
)
scenario("page_rename_by_id", [{"op": "rename_page", "page": PAGE1, "name": "Start"}])
scenario(
    "page_rename_to_a_taken_name",
    [{"op": "rename_page", "page": "Details", "name": "STORE DRILLTHROUGH"}],
    fixture=MULTI,
)
scenario("page_rename_unknown", [{"op": "rename_page", "page": "Nowhere", "name": "X"}])

scenario("page_add_at_the_end", [{"op": "add_page", "name": "Summary"}])
scenario(
    "page_add_after_one",
    [{"op": "add_page", "name": "Summary", "after": "Details", "width": 1920, "height": 1080}],
    fixture=MULTI,
)
scenario(
    "page_add_after_the_first",
    [{"op": "add_page", "name": "Second", "after": PAGE1}],
    fixture=MULTI,
)
scenario("page_add_with_a_taken_name", [{"op": "add_page", "name": "details"}], fixture=MULTI)
scenario("page_add_after_an_unknown_page", [{"op": "add_page", "name": "X", "after": "Nowhere"}])
scenario(
    "page_add_two", [{"op": "add_page", "name": "A"}, {"op": "add_page", "name": "B", "after": "A"}]
)

scenario("page_duplicate", [{"op": "duplicate_page", "page": "Details"}], fixture=MULTI)
scenario(
    "page_duplicate_named",
    [{"op": "duplicate_page", "page": DRILL, "name": "Drill again"}],
    fixture=MULTI,
)
scenario("page_duplicate_a_big_page", [{"op": "duplicate_page", "page": PAGE1}])
scenario("page_duplicate_unknown", [{"op": "duplicate_page", "page": "Nowhere"}])

scenario("page_delete", [{"op": "delete_page", "page": "Details"}], fixture=MULTI)
# A button and a bookmark point at Details, so the plain delete asks; this is the answer.
scenario(
    "page_delete_with_cleanup",
    [{"op": "delete_page", "page": "Details", "cleanup_references": True}],
    fixture=MULTI,
)
scenario(
    "page_delete_a_tooltip_with_cleanup",
    [{"op": "delete_page", "page": "Sales tooltip", "cleanup_references": True}],
    fixture=MULTI,
)
scenario("page_delete_a_tooltip", [{"op": "delete_page", "page": "Sales tooltip"}], fixture=MULTI)
scenario("page_delete_the_last_one", [{"op": "delete_page", "page": PAGE1}])
scenario(
    "page_delete_both_of_two_pages",
    [
        {"op": "add_page", "name": "B"},
        {"op": "delete_page", "page": PAGE1},
        {"op": "delete_page", "page": "B"},
    ],
)
scenario("page_delete_unknown", [{"op": "delete_page", "page": "Nowhere"}], fixture=MULTI)

scenario("page_move_unknown", [{"op": "move_page", "page": "Nowhere", "index": 0}], fixture=MULTI)
scenario(
    "page_size_unknown", [{"op": "set_page_size", "page": "Nowhere", "width": 800, "height": 600}]
)
scenario("page_hide_unknown", [{"op": "set_page_hidden", "page": "Nowhere", "hidden": True}])
scenario(
    "page_move_to_the_front", [{"op": "move_page", "page": "Details", "index": 0}], fixture=MULTI
)
scenario("page_move_to_the_end", [{"op": "move_page", "page": PAGE1, "index": 99}], fixture=MULTI)
scenario(
    "page_move_to_the_middle", [{"op": "move_page", "page": TOOLTIP, "index": 2}], fixture=MULTI
)

scenario("page_size", [{"op": "set_page_size", "page": PAGE1, "width": 1600, "height": 900}])
scenario(
    "page_size_with_display",
    [
        {
            "op": "set_page_size",
            "page": "Details",
            "width": 800,
            "height": 600,
            "display": "ActualSize",
        }
    ],
    fixture=MULTI,
)
scenario(
    "page_size_too_small", [{"op": "set_page_size", "page": PAGE1, "width": 50, "height": 900}]
)


scenario("page_hide", [{"op": "set_page_hidden", "page": "Details", "hidden": True}], fixture=MULTI)
scenario("page_show", [{"op": "set_page_hidden", "page": TOOLTIP, "hidden": False}], fixture=MULTI)


# --- scenarios: measures (model layer, applied later through Desktop) -----------------------------

scenario(
    "measure_create",
    [
        {
            "op": "create_measure",
            "table": "# measures",
            "name": "Avg Discount",
            "expression": "AVERAGE(Sales[Discount])",
            "format_string": "0.0%",
            "folder": "Discounts",
            "description": "Average discount",
        }
    ],
)
scenario(
    "measure_create_that_exists",
    [{"op": "create_measure", "table": "# Measures", "name": "rank", "expression": "1"}],
)
scenario(
    "measure_create_in_an_unknown_table",
    [{"op": "create_measure", "table": "Nowhere", "name": "X", "expression": "1"}],
)
scenario(
    "measure_create_in_a_data_table",
    [
        {
            "op": "create_measure",
            "table": "DimStore",
            "name": "Stores",
            "expression": "COUNTROWS(DimStore)",
        }
    ],
)
scenario(
    "measure_update",
    [{"op": "update_measure", "name": "Unused Ratio", "expression": "2", "format_string": "0"}],
)
scenario(
    "measure_update_the_folder",
    [{"op": "update_measure", "name": "dummy", "folder": "Misc", "description": "d"}],
)
scenario("measure_update_nothing", [{"op": "update_measure", "name": "Rank"}])
scenario("measure_update_unknown", [{"op": "update_measure", "name": "Ghost", "expression": "1"}])
scenario(
    "measure_rename_and_follow_the_visuals",
    [{"op": "rename_measure", "name": "Rank", "new_name": "Position"}],
)
scenario(
    "measure_rename_used_in_charts",
    [{"op": "rename_measure", "name": "TopN/BottomN/ MTD Sales", "new_name": "Best MTD Sales"}],
    fixture=MULTI,
)
scenario(
    "measure_rename_unused", [{"op": "rename_measure", "name": "Unused Ratio", "new_name": "Ratio"}]
)
scenario(
    "measure_rename_to_an_existing_name",
    [{"op": "rename_measure", "name": "Rank", "new_name": "DUMMY"}],
)
scenario(
    "measure_rename_unknown", [{"op": "rename_measure", "name": "Ghost", "new_name": "Ghost 2"}]
)
scenario("measure_delete_unused", [{"op": "delete_measure", "name": "Unused Ratio"}])
scenario("measure_delete_one_that_a_visual_uses", [{"op": "delete_measure", "name": "Rank"}])
scenario(
    "measure_delete_one_used_by_many", [{"op": "delete_measure", "name": "dummy"}], fixture=MULTI
)
scenario("measure_delete_unknown", [{"op": "delete_measure", "name": "Ghost"}])
scenario(
    "measure_create_then_delete",
    [
        {"op": "create_measure", "table": "# Measures", "name": "Temp", "expression": "1"},
        {"op": "delete_measure", "name": "Temp"},
    ],
)

# --- scenarios: batches and validation of the batch itself ----------------------------------------

scenario(
    "batch_of_model_operations_only",
    [
        {"op": "create_measure", "table": "# Measures", "name": "Margin", "expression": "1"},
        {"op": "delete_measure", "name": "Unused Ratio"},
    ],
)
scenario(
    "batch_of_a_rename_only",
    [{"op": "rename_measure", "name": "Unused Ratio", "new_name": "Ratio"}],
)
scenario(
    "batch_of_a_page_and_its_visuals",
    [
        {"op": "add_page", "name": "Fresh"},
        {
            "op": "add_visual",
            "page": "Fresh",
            "visual_type": "card",
            "fields": [{"table": "# Measures", "name": "Rank"}],
        },
        {"op": "duplicate_visual", "visual": COLUMN, "page": "Fresh"},
        {"op": "move_page", "page": "Fresh", "index": 0},
        {"op": "rename_page", "page": "Fresh", "name": "Fresher"},
    ],
)
scenario("batch_with_an_unknown_op", [{"op": "explode", "visual": COLUMN}])
scenario("batch_with_no_op", [{"visual": COLUMN}])
scenario(
    "batch_with_an_unknown_field", [{"op": "arrange", "visual": COLUMN, "to": "front", "speed": 3}]
)
scenario("batch_with_a_bad_value", [{"op": "arrange", "visual": COLUMN, "to": "sideways"}])
scenario("batch_with_a_missing_value", [{"op": "set_visual_hidden", "visual": COLUMN}])
scenario("batch_of_nothing", [])
scenario("batch_of_too_many", [{"op": "arrange", "visual": COLUMN, "to": "front"}] * 51)
scenario(
    "batch_of_the_most_allowed", [{"op": "set_visual_hidden", "visual": CARD, "hidden": True}] * 50
)


# --- scenarios: validate_document on documents that are wrong ----------------------------

_FIRST_VISUAL_OP = {"op": "set_visual_hidden", "visual": CARD, "hidden": False}


def _first_page(document: PbirDocument) -> Any:
    return next(iter(document.pages.values()))


def _two_visuals_share_a_name(document: PbirDocument) -> None:
    pages = list(document.pages.values())
    pages[1].visuals[COLUMN] = copy.deepcopy(pages[0].visuals[COLUMN])


def _a_visual_is_named_wrongly(document: PbirDocument) -> None:
    _first_page(document).visuals[CARD]["name"] = "someone-else"


def _a_visual_is_off_the_page(document: PbirDocument) -> None:
    _first_page(document).visuals[CARD]["position"]["x"] = -4


def _a_format_entry_points_at_a_missing_field(document: PbirDocument) -> None:
    visual = _first_page(document).visuals[COLUMN]["visual"]
    visual["objects"]["labels"].append({"properties": {}, "selector": {"metadata": "Ghost.Field"}})
    visual["visualContainerObjects"]["title"].append(
        {"properties": {}, "selector": {"metadata": "Ghost.Other"}}
    )
    visual["objects"]["labels"].append(
        {"properties": {}, "selector": {"metadata": "Sum(Ghost.Fn)"}}
    )


def _the_page_list_is_wrong(document: PbirDocument) -> None:
    document.pages_meta["pageOrder"] = ["ReportSectionGhost"]


scenario("validate_a_clean_report", [_FIRST_VISUAL_OP])
scenario("validate_a_clean_multipage_report", [_FIRST_VISUAL_OP], fixture=MULTI)
scenario(
    "validate_shared_names", [_FIRST_VISUAL_OP], fixture=MULTI, mutate=_two_visuals_share_a_name
)
scenario("validate_misnamed_visual", [_FIRST_VISUAL_OP], mutate=_a_visual_is_named_wrongly)
scenario("validate_off_the_page", [_FIRST_VISUAL_OP], mutate=_a_visual_is_off_the_page)
scenario(
    "validate_dangling_format", [_FIRST_VISUAL_OP], mutate=_a_format_entry_points_at_a_missing_field
)
scenario("validate_page_list", [_FIRST_VISUAL_OP], fixture=MULTI, mutate=_the_page_list_is_wrong)


# --- the tests ----------------------------------------------------------------------------

# The retained operations of the first version of the edit layer; each has scenarios above.
# Operations added later are not scenarios here: their tests live with their family, and
# EXEMPT_OPERATIONS names the file that pins each (a test checks the file exists and uses it).
KNOWN_OPERATIONS = [
    "set_position",
    "arrange",
    "set_visual_hidden",
    "delete_visual",
    "duplicate_visual",
    "add_visual",
    "change_visual_type",
    "set_fields",
    "set_sort",
    "rename_page",
    "add_page",
    "duplicate_page",
    "delete_page",
    "move_page",
    "set_page_size",
    "set_page_hidden",
    "create_measure",
    "update_measure",
    "rename_measure",
    "delete_measure",
]

EXEMPT_OPERATIONS = {
    "align_visuals": "test_report_edit_align.py",
    "distribute_visuals": "test_report_edit_align.py",
    "swap_visuals": "test_report_edit_align.py",
    "place_visual": "test_report_edit_align.py",
    "add_textbox": "test_report_edit_align.py",
    "add_shape": "test_report_edit_align.py",
    "set_page_filter": "test_report_edit_filters.py",
    "set_visual_filter": "test_report_edit_filters.py",
    "set_report_filter": "test_report_edit_filters.py",
    "clear_filter": "test_report_edit_filters.py",
    # Round 3 (2026-09-29): four families built after the baseline was recorded. Their scenarios
    # live in their own test files (the baseline is never re-recorded for a new operation), and
    # test_round3_integration.py applies every card example of theirs through this facade.
    "set_conditional_format": "test_report_edit_conditional.py",
    "clear_conditional_format": "test_report_edit_conditional.py",
    "set_number_format": "test_report_edit_conditional.py",
    "set_table_layout": "test_report_edit_conditional.py",
    "move_column": "test_report_edit_conditional.py",
    "add_bookmark": "test_report_edit_navigation.py",
    "update_bookmark": "test_report_edit_navigation.py",
    "rename_bookmark": "test_report_edit_navigation.py",
    "move_bookmark": "test_report_edit_navigation.py",
    "group_bookmarks": "test_report_edit_navigation.py",
    "ungroup_bookmarks": "test_report_edit_navigation.py",
    "delete_bookmark": "test_report_edit_navigation.py",
    "set_bookmark_visual_hidden": "test_report_edit_navigation.py",
    "add_button": "test_report_edit_navigation.py",
    "set_button_action": "test_report_edit_navigation.py",
    "add_navigator": "test_report_edit_navigation.py",
    "set_navigator": "test_report_edit_navigation.py",
    "set_drillthrough": "test_report_edit_navigation.py",
    "set_tooltip_page": "test_report_edit_navigation.py",
    "clear_page_type": "test_report_edit_navigation.py",
    "set_visual_tooltip": "test_report_edit_navigation.py",
    "set_interaction": "test_report_edit_navigation.py",
    "reset_interactions": "test_report_edit_navigation.py",
    "sync_slicer": "test_report_edit_navigation.py",
    "unsync_slicer": "test_report_edit_navigation.py",
    "set_drill_filter": "test_report_edit_navigation.py",
    "drill_visual": "test_report_edit_navigation.py",
    "set_analytics_line": "test_report_edit_extras.py",
    "remove_analytics_line": "test_report_edit_extras.py",
    "place_on_mobile": "test_report_edit_extras.py",
    "remove_from_mobile": "test_report_edit_extras.py",
    "arrange_mobile_layout": "test_report_edit_extras.py",
    "group_visuals": "test_report_edit_extras.py",
    "ungroup_visuals": "test_report_edit_extras.py",
    "set_group": "test_report_edit_extras.py",
    "set_visual_calculation": "test_report_edit_extras.py",
    "remove_visual_calculation": "test_report_edit_extras.py",
    "set_small_multiples": "test_report_edit_extras.py",
    "set_alt_text": "test_report_edit_extras.py",
    "set_tab_order": "test_report_edit_extras.py",
    "set_keep_layer_order": "test_report_edit_extras.py",
    "set_page_view": "test_report_edit_extras.py",
    "set_field_display_name": "test_report_edit_extras.py",
    "create_calculated_column": "test_report_edit_model.py",
    "set_column_properties": "test_report_edit_model.py",
    "rename_column": "test_report_edit_model.py",
    "delete_column": "test_report_edit_model.py",
    "create_calculated_table": "test_report_edit_model.py",
    "set_table_properties": "test_report_edit_model.py",
    "rename_table": "test_report_edit_model.py",
    "delete_table": "test_report_edit_model.py",
    "mark_date_table": "test_report_edit_model.py",
    "create_relationship": "test_report_edit_model.py",
    "update_relationship": "test_report_edit_model.py",
    "delete_relationship": "test_report_edit_model.py",
    "create_hierarchy": "test_report_edit_model.py",
    "update_hierarchy": "test_report_edit_model.py",
    "delete_hierarchy": "test_report_edit_model.py",
    "create_field_parameter": "test_report_edit_model.py",
    "update_field_parameter": "test_report_edit_model.py",
    "create_calculation_group": "test_report_edit_model.py",
    "set_calculation_item": "test_report_edit_model.py",
    "delete_calculation_item": "test_report_edit_model.py",
    "update_calculation_group": "test_report_edit_model.py",
    "create_role": "test_report_edit_model.py",
    "update_role": "test_report_edit_model.py",
    "delete_role": "test_report_edit_model.py",
    "set_table_query": "test_report_edit_model.py",
    "set_named_expression": "test_report_edit_model.py",
    "delete_named_expression": "test_report_edit_model.py",
    "set_query_parameter": "test_report_edit_model.py",
    "refresh_data": "test_report_edit_model.py",
    "set_model_options": "test_report_edit_model.py",
}


def defined_names(source: str) -> list[str]:
    """The top-level names a module's source defines: functions, classes and assignments.

    Read from the source of the module before the split, because that is what other modules
    and tests import from ``report_edits`` (the private helpers included)."""
    found: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
    return sorted(found)


def record() -> dict[str, Any]:
    source = Path(str(report_edits.__file__)).read_text(encoding="utf-8")
    # The names the module offered before the split are read from the source it had then; that
    # source is gone, so a re-recording keeps the names the last recording listed. Only new
    # names may join the list, never leave it.
    kept: list[str] = []
    if GOLDEN.exists():
        kept = json.loads(GOLDEN.read_text(encoding="utf-8"))["names"]
    return {
        "version": 1,
        "operations": KNOWN_OPERATIONS,
        # ROOT and DocumentError are imported there and re-exported (they are in __all__).
        "names": sorted({*kept, *defined_names(source), "ROOT", "DocumentError"}),
        "scenarios": {name: run_scenario(item) for name, item in sorted(SCENARIOS.items())},
    }


def _override_files() -> list[Path]:
    return sorted(OVERRIDES.glob("*.json")) if OVERRIDES.is_dir() else []


def _golden() -> dict[str, Any]:
    """The baseline with every family's deliberate changes applied over it."""
    loaded: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for path in _override_files():
        changes: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["scenarios"]
        loaded["scenarios"].update(changes)
    return loaded


def test_no_scenario_is_overridden_by_two_families() -> None:
    owner: dict[str, str] = {}
    clashes = []
    for path in _override_files():
        for name in json.loads(path.read_text(encoding="utf-8"))["scenarios"]:
            if name in owner:
                clashes.append(f"{name}: {owner[name]} and {path.stem}")
            owner[name] = path.stem
    assert clashes == [], "two families changed the same scenario; settle it by hand"


def test_every_override_names_a_scenario_and_a_reason() -> None:
    for path in _override_files():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert str(loaded.get("reason", "")).strip(), f"{path.name} needs a reason"
        assert set(loaded["scenarios"]) <= set(SCENARIOS), f"{path.name} names an unknown scenario"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_matches_the_recording(name: str) -> None:
    recorded = _golden()["scenarios"].get(name)
    assert recorded is not None, f"{name} is not in the golden; re-record it"
    assert run_scenario(SCENARIOS[name]) == recorded


def test_the_golden_and_the_scenarios_list_the_same_names() -> None:
    assert sorted(_golden()["scenarios"]) == sorted(SCENARIOS)


def test_every_known_operation_has_a_scenario_that_succeeds() -> None:
    succeeded: set[str] = set()
    for name, item in SCENARIOS.items():
        recorded = _golden()["scenarios"][name]
        if "summaries" in recorded:
            succeeded.update(str(op["op"]) for op in item.ops)
    assert sorted(set(KNOWN_OPERATIONS) - succeeded) == []


def test_every_known_operation_has_a_scenario_that_fails() -> None:
    failed: set[str] = set()
    for name, item in SCENARIOS.items():
        recorded = _golden()["scenarios"][name]
        if "error" in recorded and len(item.ops) == 1:
            failed.update(str(op["op"]) for op in item.ops)
    assert sorted(set(KNOWN_OPERATIONS) - failed) == []


def test_every_operation_is_pinned_here_or_in_the_test_file_named_for_it() -> None:
    every = {str(cls.model_fields["op"].default) for cls in report_edits.REPORT_OPERATIONS}
    assert set(KNOWN_OPERATIONS).isdisjoint(EXEMPT_OPERATIONS)
    assert every == set(KNOWN_OPERATIONS) | set(EXEMPT_OPERATIONS)
    for name, file in EXEMPT_OPERATIONS.items():
        text = (HERE / file).read_text(encoding="utf-8")
        assert f'"{name}"' in text, f"{file} does not exercise {name}"


def test_a_question_is_recorded_with_its_answers() -> None:
    """A batch that stops to ask is pinned by what it asks, not only by the message."""
    delete = _golden()["scenarios"]["page_delete"]
    assert delete["asks"]["kind"] == "remove_page_references"


def test_every_name_the_module_offered_before_the_split_is_still_importable() -> None:
    for name in _golden()["names"]:
        assert hasattr(report_edits, name), name


def test_the_golden_is_small_enough_to_review() -> None:
    assert GOLDEN.stat().st_size < 400_000


def _record_family(argv: list[str]) -> None:
    family = argv[argv.index("--record-family") + 1]
    if "--reason" not in argv:
        sys.exit('pass --reason "why the output changed"')
    reason = argv[argv.index("--reason") + 1]
    named = [
        a
        for a in argv[argv.index("--record-family") + 2 :]
        if not a.startswith("--") and a != reason
    ]
    current = _golden()["scenarios"]
    unknown = [n for n in named if n not in SCENARIOS]
    if unknown:
        sys.exit("unknown scenario: " + ", ".join(unknown))
    chosen = named or [n for n in sorted(SCENARIOS) if run_scenario(SCENARIOS[n]) != current[n]]
    OVERRIDES.mkdir(parents=True, exist_ok=True)
    target = OVERRIDES / f"{family}.json"
    kept: dict[str, Any] = {}
    if target.exists():
        kept = json.loads(target.read_text(encoding="utf-8"))["scenarios"]
    kept.update({n: run_scenario(SCENARIOS[n]) for n in chosen})
    payload = {"family": family, "reason": reason, "scenarios": dict(sorted(kept.items()))}
    text = json.dumps(payload, indent=1, ensure_ascii=False)
    target.write_text(text + "\n", encoding="utf-8")
    print(f"{family}: {len(chosen)} scenario(s) recorded ({', '.join(chosen) or 'none'})")


if __name__ == "__main__":
    if "--record-family" in sys.argv:
        _record_family(sys.argv)
    elif "--record" in sys.argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        recorded = record()
        text = json.dumps(recorded, indent=1, ensure_ascii=False)
        GOLDEN.write_text(text + "\n", encoding="utf-8")
        for stale in _override_files():
            stale.unlink()
        print(f"{len(recorded['scenarios'])} scenarios, {GOLDEN.stat().st_size} bytes")
    else:
        sys.exit("pass --record (whole baseline) or --record-family <family> --reason <why>")
