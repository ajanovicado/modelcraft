"""Measure operations and the model facts behind them (report_edit_measures, ModelInfo).

The rename and delete tests use the shapes of the real Agents report, carried by the structure
twin: the table's Top/Bottom-N filter reaches ``Rank Filtering Dynamics`` through the ``From``
alias ``#`` (not through ``Entity``), and no visual shows ``Rank Filtering Employyees MTD``, yet
deleting it breaks that filter, because ``Rank Filtering Dynamics`` is defined from it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import report_edits as edits
from ai_powerbi_analyst.studio.pbir_document import DocumentError, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import HandlerResult, ModelInfo

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
PAGE = "ReportSection8454c812b60b8bb83b99"
TABLE = "e82e02f93055d6b0b544"  # tableEx with the Top/Bottom-N filter on a measure
TABLE_PART = f"Report/definition/pages/{PAGE}/visuals/{TABLE}/visual.json"
COLUMN = "b404a461ede7604a2774"
HOME = "# Measures"

DYNAMICS = "Rank Filtering Dynamics"
EMPLOYEES_MTD = "Rank Filtering Employyees MTD"
EMPLOYEES_PCT = "Rank Filtering Employyees MTD%"


def load(name: str = "structure-twin") -> PbirDocument:
    return PbirDocument(PbixPackage(FIXTURES / f"{name}.pbix"))


def _walk(node: Any, measures: set[str], columns: dict[str, set[str]]) -> None:
    if isinstance(node, dict):
        for kind, target in (("Measure", None), ("Column", "column")):
            body = node.get(kind)
            entity = (
                body.get("Expression", {}).get("SourceRef", {}).get("Entity")
                if isinstance(body, dict)
                else None
            )
            if isinstance(entity, str):
                if target is None:
                    measures.add(str(body["Property"]))
                else:
                    columns.setdefault(entity, set()).add(str(body["Property"]))
        for value in node.values():
            _walk(value, measures, columns)
    elif isinstance(node, list):
        for value in node:
            _walk(value, measures, columns)


def twin_model(expressions: dict[str, str] | None = None, **extra: Any) -> ModelInfo:
    """A model holding every field the twin's visuals use, with DAX for the given measures."""
    measures: set[str] = {DYNAMICS, EMPLOYEES_MTD, EMPLOYEES_PCT, "Unused Ratio"}
    columns: dict[str, set[str]] = {}
    for _, _, visual in load().all_visuals():
        _walk(visual, measures, columns)
    measures.discard("")
    dax = expressions or {}
    return ModelInfo.from_report(
        [
            {"TableName": t, "ColumnName": c, "PandasDataType": "object"}
            for t, cols in columns.items()
            for c in sorted(cols)
        ],
        [
            {"Name": m, "TableName": HOME, **({"Expression": dax[m]} if m in dax else {})}
            for m in sorted(measures)
        ],
        **extra,
    )


AGENTS_DAX = {
    DYNAMICS: (
        f"IF ( SELECTEDVALUE ( 'Top-Bottom-N'[Top-Bottom-N] ) = \"Top\", [{EMPLOYEES_MTD}], "
        f"[{EMPLOYEES_PCT}] )"
    ),
    EMPLOYEES_MTD: "RANKX ( ALL ( 'Clustered Employees' ), [Table MTD Sales] )",
    EMPLOYEES_PCT: "RANKX ( ALL ( 'Clustered Employees' ), [vs PM MOM%] )",
}


def run(document: PbirDocument, model: ModelInfo, *ops: dict[str, Any]) -> edits.EditOutcome:
    return edits.apply_operations(document, edits.parse_operations(list(ops)), model)


def refuses(document: PbirDocument, model: ModelInfo, *ops: dict[str, Any]) -> str:
    with pytest.raises(DocumentError) as raised:
        run(document, model, *ops)
    return str(raised.value)


def visual_json(document: PbirDocument, visual: str = TABLE) -> str:
    return json.dumps(document.visual(visual)[1])


# --- ModelInfo ------------------------------------------------------------------------------


def make_info() -> ModelInfo:
    return ModelInfo.from_report(
        [
            {"TableName": "Sales", "ColumnName": "Amount", "PandasDataType": "float64"},
            {"TableName": "Sales", "ColumnName": "Year", "PandasDataType": "int64"},
            {"TableName": "Sales", "ColumnName": "Region", "PandasDataType": "object"},
            {"TableName": "Store", "ColumnName": "Region", "PandasDataType": "object"},
            {"TableName": "Store", "ColumnName": "Opened", "PandasDataType": "datetime64[ns]"},
        ],
        [
            {"Name": "Total", "TableName": HOME, "Expression": "SUM ( Sales[Amount] )"},
            {"Name": "Amount", "TableName": HOME, "Expression": "1"},
        ],
        {
            "columns": [
                {"table": "Sales", "name": "Amount", "dataType": 10, "summarizeBy": "none"},
                {"table": "Sales", "name": "Year", "dataType": 6, "summarizeBy": "sum"},
                {"table": "Store", "name": "Opened", "dataType": 9, "hidden": True},
                {"table": "Sales", "name": "Region", "dataType": None, "summarizeBy": None},
            ],
            "measures": [{"table": HOME, "name": "Total", "dataType": 8, "hidden": True}],
        },
        calculated={
            "Pick": "{ (\"Sales\", NAMEOF ( 'Sales'[Amount] ), 0) }",
            "Calendar": "CALENDAR ( DATE ( 2020, 1, 1 ), DATE ( 2020, 12, 31 ) )",
            "Sales[Double]": "Sales[Amount] * 2",
        },
    )


def test_data_type_prefers_the_model_id_then_the_pandas_dtype_then_none() -> None:
    info = make_info()
    assert info.data_type("Sales", "Amount") == 10  # the model says fixed decimal, pandas float
    assert info.data_type("sales", "YEAR") == 6
    assert info.data_type("Store", "Opened") == 9
    assert info.data_type("Sales", "Region") == "object"  # the model gave no type: pandas'
    assert info.data_type(HOME, "Total") == 8
    assert info.data_type(HOME, "Amount") is None  # a measure with no type
    assert info.data_type("Nowhere", "Nothing") is None


def test_without_a_types_payload_the_pandas_dtypes_answer() -> None:
    plain = ModelInfo.from_report(
        [{"TableName": "T", "ColumnName": "C", "PandasDataType": "int64"}], []
    )
    assert plain.data_type("T", "C") == "int64"
    assert plain.summarize_by("T", "C") is None
    assert plain.is_hidden("T", "C") is False
    assert plain.field_parameter_tables() == set()


def test_summarize_by_and_hidden_come_from_the_metadata() -> None:
    info = make_info()
    assert info.summarize_by("Sales", "Amount") == "none"
    assert info.summarize_by("Sales", "Year") == "sum"
    assert info.summarize_by("Sales", "Region") is None
    assert info.is_hidden("Store", "Opened") is True
    assert info.is_hidden(HOME, "Total") is True
    assert info.is_hidden("Sales", "Year") is False


def test_a_model_type_id_decides_whether_a_column_is_numeric() -> None:
    info = make_info()
    assert info.is_numeric("Sales", "Amount") is True
    assert info.is_numeric("Sales", "Region") is False
    assert info.is_numeric("Store", "Opened") is False


def test_field_parameter_tables_are_the_calculated_tables_that_use_nameof() -> None:
    assert make_info().field_parameter_tables() == {"Pick"}


def test_resolve_without_a_table_finds_the_one_column_or_measure() -> None:
    info = make_info()
    ref = edits.FilterFieldRef
    assert info.resolve(ref(name="year")) == ("column", "Sales", "Year")
    assert info.resolve(ref(name="opened")) == ("column", "Store", "Opened")
    assert info.resolve(ref(name="TOTAL")) == ("measure", HOME, "Total")


def test_resolve_without_a_table_names_the_candidates_when_it_is_ambiguous() -> None:
    info = make_info()
    with pytest.raises(DocumentError) as raised:
        info.resolve(edits.FilterFieldRef(name="Region"))
    assert str(raised.value) == (
        "'Region' could mean the column Sales[Region], the column Store[Region]; "
        "say which table it is in"
    )
    with pytest.raises(DocumentError) as raised:
        info.resolve(edits.FilterFieldRef(name="Amount"))
    assert str(raised.value) == (
        "'Amount' could mean the measure [Amount], the column Sales[Amount]; "
        "say which table it is in"
    )


def test_resolve_without_a_table_says_when_nothing_matches() -> None:
    with pytest.raises(DocumentError, match="The model has no column or measure named 'Ghost'"):
        make_info().resolve(edits.FilterFieldRef(name="Ghost"))


def test_resolve_with_a_table_is_what_it_was() -> None:
    info = make_info()
    assert info.resolve(edits.FieldRef(table="store", name="region")) == (
        "column",
        "Store",
        "Region",
    )
    assert info.resolve(edits.FieldRef(table=HOME, name="amount")) == ("measure", HOME, "Amount")
    with pytest.raises(DocumentError, match="The model has no table 'Nope'"):
        info.resolve(edits.FieldRef(table="Nope", name="x"))
    with pytest.raises(DocumentError, match="Store has no column or measure named 'Zip'"):
        info.resolve(edits.FieldRef(table="Store", name="Zip"))


def test_a_model_built_by_hand_still_works() -> None:
    by_hand = ModelInfo(columns={"T": {"C": "object"}}, measures={"M": HOME})
    assert by_hand.resolve(edits.FieldRef(table="T", name="C")) == ("column", "T", "C")
    assert by_hand.data_type("T", "C") == "object"
    assert by_hand.measure_expressions == {}


def test_with_measures_keeps_the_types_and_swaps_the_measures() -> None:
    info = make_info()
    other = info.with_measures({"Total": HOME})
    assert other.measures == {"Total": HOME}
    assert other.data_type("Sales", "Amount") == 10
    assert other.measure_expressions == {"Total": "SUM ( Sales[Amount] )"}
    assert info.measures == {"Total": HOME, "Amount": HOME}  # the original is untouched


# --- create ---------------------------------------------------------------------------------


def create(**changes: Any) -> dict[str, Any]:
    return {"op": "create_measure", "table": HOME, "name": "Margin", "expression": "1", **changes}


def test_create_reports_the_summary_and_updates_the_model() -> None:
    model = twin_model()
    outcome = run(load(), model, create(expression="SUM ( 'Top-Bottom-N'[Top-Bottom-N] )"))
    assert outcome.summaries == ["Created measure [Margin] in # Measures."]
    assert [op.op for op in outcome.model_operations] == ["create_measure"]
    assert model.measure_table("margin") == HOME
    assert model.measure_expressions["Margin"] == "SUM ( 'Top-Bottom-N'[Top-Bottom-N] )"
    assert outcome.report_changed is False


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("rank", "A measure named 'rank' already exists"),
        (" Margin", "The measure name cannot start or end with a space"),
        ("Margin ", "The measure name cannot start or end with a space"),
        ("Mar\ngin", "The measure name cannot contain line breaks or other control characters"),
        ("Mar\x00gin", "The measure name cannot contain line breaks or other control characters"),
    ],
)
def test_create_checks_the_name(name: str, message: str) -> None:
    assert refuses(load(), twin_model(), create(name=name)) == message


def test_a_measure_name_is_at_most_128_characters_and_the_operation_says_so() -> None:
    with pytest.raises(DocumentError) as raised:
        edits.parse_operations([create(name="x" * 129)])
    assert str(raised.value) == (
        "Step 1 (create_measure): 'name' is not valid: string should have at most 128 characters"
    )
    assert run(load(), twin_model(), create(name="x" * 128)).summaries


def test_a_formula_is_at_most_20000_characters_and_the_operation_says_so() -> None:
    with pytest.raises(DocumentError) as raised:
        edits.parse_operations([create(expression="1" * 20001)])
    assert str(raised.value) == (
        "Step 1 (create_measure): 'expression' is not valid: string should have at most 20000 "
        "characters"
    )
    with pytest.raises(DocumentError, match="'table' is not valid"):
        edits.parse_operations([create(table="t" * 201)])


def test_create_needs_a_table_that_exists() -> None:
    assert refuses(load(), twin_model(), create(table="Nowhere")) == (
        "The model has no table 'Nowhere'"
    )


def test_create_refuses_a_name_a_column_of_that_table_already_has() -> None:
    message = refuses(load(), twin_model(), create(table="DimStore", name="storename"))
    assert message == "The table DimStore already has a column called 'storename'"


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("   ", "The measure needs a formula"),
        ("SUM ( x", "The formula has a parenthesis that is never closed"),
        ("SUM ( x ) )", "The formula closes a parenthesis that was never opened"),
        ('"open', "The formula has a text that is never closed"),
        ("'Table", "The formula has a table name that is never closed"),
        ("[Rank", "The formula has a bracket that is never closed"),
        ("1 /* never", "The formula has a comment that is never closed"),
        ("1\x07", "The formula cannot contain control characters"),
        (
            "Margin = SUM ( x )",
            "Give just the formula, without the measure's name and equals sign before it",
        ),
        (
            "[Margin] = 1",
            "Give just the formula, without the measure's name and equals sign before it",
        ),
    ],
)
def test_create_checks_the_formula_without_running_it(expression: str, message: str) -> None:
    assert refuses(load(), twin_model(), create(expression=expression)) == message


def test_the_formula_check_leaves_valid_dax_alone() -> None:
    # Quotes, brackets and comments that hold parentheses are not counted.
    dax = (
        'VAR t = "a ( b" -- a ( comment\n'
        "VAR u = 'Top-Bottom-N'[Top ]] ) Bottom]]] /* ( */ RETURN\n"
        'IF ( t = """", 1, 2 ) // )\n'
    )
    assert run(load(), twin_model(), create(expression=dax)).summaries


def test_the_local_engine_is_advisory_so_unsupported_dax_is_not_refused() -> None:
    dax = "CALCULATE ( [Rank], DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -3, MONTH ) )"
    assert run(load(), twin_model(), create(expression=dax)).summaries == [
        "Created measure [Margin] in # Measures."
    ]


def test_create_checks_format_folder_and_description() -> None:
    assert refuses(load(), twin_model(), create(format_string='0.0" pts')) == (
        "The format string has a quote that is never closed"
    )
    assert refuses(load(), twin_model(), create(format_string="0\n")) == (
        "The format string cannot contain control characters"
    )
    assert refuses(load(), twin_model(), create(folder="A\tB")) == (
        "The display folder cannot contain control characters"
    )
    ok = create(format_string='#,0.0" pts"', folder="KPI\\Sales", description="Line 1\nLine 2")
    assert run(load(), twin_model(), ok).summaries


def test_create_warns_about_a_qualified_reference_the_model_does_not_have() -> None:
    outcome = run(load(), twin_model(), create(expression="SUM ( DimStore[Ghost] ) + [Rank]"))
    assert outcome.summaries == ["Created measure [Margin] in # Measures."]
    assert outcome.warnings == [
        "The formula uses DimStore[Ghost], which Studio cannot find in the model. Power BI will "
        "check it, and the change is not kept if Power BI rejects it."
    ]
    quiet = run(load(), twin_model(), create(expression="SUM ( DimStore[StoreName] )"))
    assert quiet.warnings == []


# --- update ---------------------------------------------------------------------------------


def test_update_lists_what_changed() -> None:
    model = twin_model()
    outcome = run(
        load(),
        model,
        {"op": "update_measure", "name": "unused ratio", "expression": "2"},
        {"op": "update_measure", "name": "Rank", "format_string": "0", "folder": "KPI"},
    )
    assert outcome.summaries == [
        "Updated measure [unused ratio] (expression).",
        "Updated measure [Rank] (format string, folder).",
    ]
    assert [op.model_dump(mode="json")["name"] for op in outcome.model_operations] == [
        "unused ratio",
        "Rank",
    ]
    assert model.measure_expressions["Unused Ratio"] == "2"


def test_update_measure_dumps_hidden_only_when_it_is_set() -> None:
    plain = edits.UpdateMeasure(name="Rank", expression="1")
    assert "hidden" not in plain.model_dump(mode="json")
    assert edits.UpdateMeasure(name="Rank", hidden=False).model_dump(mode="json")["hidden"] is False
    old = {"op": "update_measure", "name": "Rank", "expression": "1"}
    assert edits.UpdateMeasure.model_validate(old) == plain


def test_update_needs_something_to_change_and_a_measure_that_exists() -> None:
    assert refuses(load(), twin_model(), {"op": "update_measure", "name": "Rank"}) == (
        "Say what to change in the measure"
    )
    assert refuses(
        load(), twin_model(), {"op": "update_measure", "name": "Ghost", "hidden": True}
    ) == ("There is no measure named 'Ghost'")


def test_update_checks_the_new_formula() -> None:
    message = refuses(
        load(), twin_model(), {"op": "update_measure", "name": "Rank", "expression": "SUM ( x"}
    )
    assert message == "The formula has a parenthesis that is never closed"


def test_update_refuses_a_measure_that_uses_itself() -> None:
    model = twin_model(AGENTS_DAX)
    for text in ("[Rank] + 1", "'# Measures'[rank] + 1"):
        message = refuses(
            load(), model, {"op": "update_measure", "name": "Rank", "expression": text}
        )
        assert message == "[Rank] cannot use itself in its own formula"
    # The same name in another table's column is not the measure.
    assert run(
        load(), model, {"op": "update_measure", "name": "Rank", "expression": "DimStore[Rank] + 1"}
    ).summaries


def test_update_refuses_a_formula_that_would_go_round_in_a_circle() -> None:
    model = twin_model(AGENTS_DAX)
    message = refuses(
        load(),
        model,
        {"op": "update_measure", "name": EMPLOYEES_MTD, "expression": f"[{DYNAMICS}] + 1"},
    )
    assert message == (
        f"[{EMPLOYEES_MTD}] cannot use [{DYNAMICS}]: [{DYNAMICS}] already depends on "
        f"[{EMPLOYEES_MTD}], so the two would refer to each other"
    )


def test_update_of_a_later_step_sees_the_expression_an_earlier_one_wrote() -> None:
    model = twin_model(AGENTS_DAX)
    message = refuses(
        load(),
        model,
        {"op": "update_measure", "name": EMPLOYEES_PCT, "expression": f"[{EMPLOYEES_MTD}] * 2"},
        {"op": "update_measure", "name": EMPLOYEES_MTD, "expression": f"[{EMPLOYEES_PCT}] * 2"},
    )
    assert message.startswith(f"Step 2: [{EMPLOYEES_MTD}] cannot use [{EMPLOYEES_PCT}]")


# --- rename ---------------------------------------------------------------------------------


def test_rename_follows_a_filter_that_reaches_the_measure_through_a_from_alias() -> None:
    document = load()
    before = visual_json(document)
    assert before.count(DYNAMICS) == 2  # the filter's field and its Where
    outcome = run(
        document,
        twin_model(),
        {"op": "rename_measure", "name": DYNAMICS, "new_name": "Rank Filter"},
    )
    after = visual_json(document)
    assert DYNAMICS not in after
    assert after.count('"Property": "Rank Filter"') == 2
    filter_ = document.visual(TABLE)[1]["filterConfig"]["filters"][0]
    where = filter_["filter"]["Where"][0]["Condition"]["Comparison"]["Left"]["Measure"]
    assert where["Expression"] == {"SourceRef": {"Source": "#"}}  # still through the alias
    assert where["Property"] == "Rank Filter"
    assert filter_["name"] == "Filterff831044ab8c98051120"
    assert outcome.summaries == [
        f"Renamed measure [{DYNAMICS}] to [Rank Filter] and updated the visuals that use it."
    ]
    assert [op.op for op in outcome.model_operations] == ["rename_measure"]
    assert document.commit()  # the touched visual is written


def test_rename_keeps_queryref_and_selector_metadata_as_desktop_does() -> None:
    document = load()
    run(document, twin_model(), {"op": "rename_measure", "name": "Rank", "new_name": "Position"})
    visual = document.visual(TABLE)[1]
    projections = visual["visual"]["query"]["queryState"]["Values"]["projections"]
    renamed = next(
        p for p in projections if p["field"].get("Measure", {}).get("Property") == "Position"
    )
    assert renamed["queryRef"] == "# Measures.Rank"  # Desktop keeps it across a rename
    assert renamed["nativeQueryRef"] == "Position"
    text = json.dumps(visual)
    assert '"metadata": "# Measures.Rank"' in json.dumps(visual, indent=None).replace('":"', '": "')
    assert '"Property":"Rank"' not in text.replace(" ", "")


def test_rename_rewrites_the_dax_of_dependent_measures_as_extra_update_operations() -> None:
    document = load()
    model = twin_model(AGENTS_DAX)
    outcome = run(
        document,
        model,
        {"op": "rename_measure", "name": EMPLOYEES_MTD, "new_name": "Rank by Sales"},
    )
    assert [op.op for op in outcome.model_operations] == ["rename_measure", "update_measure"]
    update = outcome.model_operations[1]
    assert update.name == DYNAMICS
    assert update.expression == (
        "IF ( SELECTEDVALUE ( 'Top-Bottom-N'[Top-Bottom-N] ) = \"Top\", [Rank by Sales], "
        f"[{EMPLOYEES_PCT}] )"  # the sibling with a longer name is a different measure
    )
    assert outcome.summaries == [
        f"Renamed measure [{EMPLOYEES_MTD}] to [Rank by Sales] and updated the visuals that use "
        f"it, and the formulas of [{DYNAMICS}]."
    ]
    assert outcome.warnings == []
    # The model reflects both changes, so a later step sees them.
    assert model.measure_expressions[DYNAMICS] == update.expression
    assert model.measure_table("rank by sales") == HOME
    assert model.measure_table(EMPLOYEES_MTD) is None
    assert model.measure_expressions["Rank by Sales"] == AGENTS_DAX[EMPLOYEES_MTD]


def test_a_rename_followed_by_another_rename_chains_through_the_rewritten_dax() -> None:
    model = twin_model(AGENTS_DAX)
    outcome = run(
        load(),
        model,
        {"op": "rename_measure", "name": EMPLOYEES_MTD, "new_name": "A"},
        {"op": "rename_measure", "name": "A", "new_name": "B"},
    )
    assert [op.op for op in outcome.model_operations] == [
        "rename_measure",
        "update_measure",
        "rename_measure",
        "update_measure",
    ]
    assert outcome.model_operations[3].expression.count("[B]") == 1
    assert "[A]" not in model.measure_expressions[DYNAMICS]


def test_rename_names_what_it_cannot_rewrite() -> None:
    model = twin_model(
        AGENTS_DAX,
        calculated={
            "DimStore[Score]": f"[{EMPLOYEES_MTD}] + 1",
            "Pick": f'{{ ("Rank", NAMEOF ( [{EMPLOYEES_MTD}] ), 0) }}',
            "Unrelated": "CALENDAR ( DATE ( 2020, 1, 1 ), DATE ( 2020, 12, 31 ) )",
        },
    )
    outcome = run(
        load(), model, {"op": "rename_measure", "name": EMPLOYEES_MTD, "new_name": "Rank by Sales"}
    )
    assert outcome.warnings == [
        "the calculated column 'DimStore[Score]' still uses [Rank Filtering Employyees MTD]: "
        "also update it to [Rank by Sales] in Power BI",
        "the field parameter 'Pick' still uses [Rank Filtering Employyees MTD]: "
        "also update it to [Rank by Sales] in Power BI",
    ]
    # Only the measure formula becomes an operation: the rest cannot be rewritten from here.
    assert [op.op for op in outcome.model_operations] == ["rename_measure", "update_measure"]


def test_rename_checks_the_names() -> None:
    model = twin_model()
    rename = {"op": "rename_measure", "name": "Rank"}
    assert refuses(load(), model, {**rename, "new_name": "dummy"}) == (
        "A measure named 'dummy' already exists"
    )
    assert refuses(load(), model, {**rename, "new_name": "Rank"}) == (
        "The measure is already called 'Rank'"
    )
    assert refuses(load(), model, {**rename, "new_name": "Rank "}) == (
        "The measure name cannot start or end with a space"
    )
    assert refuses(load(), model, {"op": "rename_measure", "name": "Ghost", "new_name": "x"}) == (
        "There is no measure named 'Ghost'"
    )


def test_rename_refuses_a_name_a_column_of_the_home_table_has() -> None:
    model = ModelInfo(columns={HOME: {"Stamp": "object"}}, measures={"Rank": HOME})
    message = refuses(load(), model, {"op": "rename_measure", "name": "Rank", "new_name": "stamp"})
    assert message == "The table # Measures already has a column called 'stamp'"


def test_only_the_letter_case_of_a_measure_can_change_onto_its_own_name() -> None:
    document = load()
    model = twin_model()
    outcome = run(document, model, {"op": "rename_measure", "name": "Rank", "new_name": "RANK"})
    assert outcome.summaries == [
        "Renamed measure [Rank] to [RANK] and updated the visuals that use it."
    ]
    assert model.measure_name("rank") == "RANK"
    assert '"Property":"RANK"' in visual_json(document).replace(" ", "")


def test_rename_moves_the_types_with_the_measure() -> None:
    model = make_info()
    model = model.with_measures({"Total": HOME})
    document = load()
    run(document, model, {"op": "rename_measure", "name": "Total", "new_name": "Grand"})
    assert model.data_type(HOME, "Grand") == 8
    assert model.is_hidden(HOME, "Grand") is True
    assert model.data_type(HOME, "Total") is None


def test_the_report_layer_part_of_a_rename_runs_on_the_saved_model() -> None:
    document = load()
    saved = twin_model()  # what Desktop saved: the measure is already called Position
    saved.measures["Position"] = saved.measures.pop("Rank")
    ops = edits.parse_operations(
        [
            {"op": "create_measure", "table": HOME, "name": "Margin", "expression": "1"},
            {"op": "rename_measure", "name": "Rank", "new_name": "Position"},
            {"op": "update_measure", "name": "Position", "format_string": "0"},
            {"op": "delete_measure", "name": "Unused Ratio"},
        ]
    )
    outcome = edits.apply_report_operations(document, ops, saved)
    assert outcome.model_operations == []
    assert outcome.summaries == [
        "Renamed measure [Rank] to [Position] and updated the visuals that use it."
    ]
    assert outcome.report_changed is True
    assert '"Property":"Position"' in visual_json(document).replace(" ", "")


def test_the_report_layer_part_without_a_rename_changes_nothing_in_the_report() -> None:
    document = load()
    outcome = edits.apply_report_operations(
        document,
        edits.parse_operations([create()]),
        twin_model(),
    )
    assert outcome.summaries == []
    assert outcome.report_changed is False
    assert document.commit() == []


# --- delete ---------------------------------------------------------------------------------


def test_delete_of_an_unused_measure_updates_the_model() -> None:
    model = twin_model()
    outcome = run(load(), model, {"op": "delete_measure", "name": "unused ratio"})
    assert outcome.summaries == ["Deleted measure [unused ratio]."]
    assert [op.op for op in outcome.model_operations] == ["delete_measure"]
    assert model.measure_table("Unused Ratio") is None


def test_delete_lists_the_visuals_that_use_the_measure() -> None:
    message = refuses(load(), twin_model(), {"op": "delete_measure", "name": "Rank"})
    assert message == "[Rank] is used by 1 visual: table 'Agent ID'. Remove or change those first."


def test_delete_is_refused_for_the_measure_the_filter_measure_is_defined_from() -> None:
    """No visual shows it, and the report never names it, but the table filters on a measure
    that IF()s between it and its sibling."""
    document = load()
    message = refuses(
        document, twin_model(AGENTS_DAX), {"op": "delete_measure", "name": EMPLOYEES_MTD}
    )
    assert message == (
        f"[{EMPLOYEES_MTD}] is used by the measure '{DYNAMICS}'; needed indirectly by 1 visual: "
        "table 'Agent ID'. Remove or change those first."
    )
    assert document.commit() == []


def test_delete_without_the_model_dax_only_sees_what_the_report_uses() -> None:
    # A model that was not given the measures' DAX cannot know the dependency.
    assert run(load(), twin_model(), {"op": "delete_measure", "name": EMPLOYEES_MTD}).summaries


def test_delete_is_refused_for_a_measure_a_filter_uses() -> None:
    message = refuses(load(), twin_model(), {"op": "delete_measure", "name": DYNAMICS})
    assert message == (
        f"[{DYNAMICS}] is used by 1 visual: table 'Agent ID'; used in 1 filter. "
        "Remove or change those first."
    )


def test_delete_counts_a_report_filter_and_a_measure_that_a_calculated_column_uses() -> None:
    model = twin_model(AGENTS_DAX, calculated={"DimStore[Score]": "[Unused Ratio] * 2"})
    message = refuses(load(), model, {"op": "delete_measure", "name": "unused ratio"})
    assert message == (
        "[unused ratio] is used by the calculated column 'DimStore[Score]'. "
        "Remove or change those first."
    )
    filtered = load()
    run(
        filtered,
        twin_model(),
        {
            "op": "set_report_filter",
            "filter": {
                "kind": "advanced",
                "field": {"name": "Unused Ratio"},
                "conditions": [{"operator": "greater_than", "value": 1}],
            },
        },
    )
    message = refuses(filtered, twin_model(), {"op": "delete_measure", "name": "Unused Ratio"})
    assert message == "[Unused Ratio] is used in 1 filter. Remove or change those first."


def test_a_create_then_a_delete_in_one_batch_goes_through() -> None:
    model = twin_model()
    outcome = run(
        load(),
        model,
        create(name="Temp"),
        {"op": "delete_measure", "name": "temp"},
    )
    assert [op.op for op in outcome.model_operations] == ["create_measure", "delete_measure"]
    assert model.measure_table("Temp") is None


# --- apply_operations -----------------------------------------------------------------------


def test_handler_results_are_collected_into_the_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_powerbi_analyst.studio.report_edit_context import Protected, VisibleIn

    def handler(document: PbirDocument, op: Any, model: ModelInfo) -> HandlerResult:
        return HandlerResult(
            "did part",
            warnings=["kept the rest"],
            protected=[Protected("v", "a conditional color")],
            replaced=["TopN blue"],
            visible_in=[VisibleIn("set_x", "powerbi_only")],
            pending_model_operations=["a suggestion"],
            partial=True,
            lineage={"new": "old"},
        )

    monkeypatch.setitem(edits._HANDLERS, "set_visual_hidden", handler)
    outcome = run(
        load(),
        twin_model(),
        {"op": "set_visual_hidden", "visual": COLUMN, "hidden": True},
        {"op": "create_measure", "table": HOME, "name": "Margin", "expression": "1"},
    )
    assert outcome.summaries == ["did part", "Created measure [Margin] in # Measures."]
    assert outcome.warnings == ["kept the rest"]
    assert [p.what for p in outcome.protected] == ["a conditional color"]
    assert outcome.replaced == ["TopN blue"]
    assert [v.where for v in outcome.visible_in] == ["powerbi_only"]
    assert outcome.pending_model_operations == ["a suggestion"]
    assert outcome.partial is True
    assert outcome.lineage == {"new": "old"}
    assert [op.op for op in outcome.model_operations] == ["create_measure"]
    assert outcome.report_changed is True


def test_an_outcome_is_not_partial_unless_a_handler_says_so() -> None:
    outcome = run(load(), twin_model(), create())
    assert outcome.partial is False
    assert outcome.lineage == {}


def test_a_failure_in_a_batch_names_the_step() -> None:
    message = refuses(
        load(),
        twin_model(),
        create(),
        {"op": "delete_measure", "name": "Rank"},
    )
    assert message.startswith("Step 2: [Rank] is used by")


# --- review fixes ---------------------------------------------------------------------------


def _same_name_model() -> ModelInfo:
    """Column Sales[Amount] beside a measure [Amount] that depends on the measure [Total]."""
    return ModelInfo.from_report(
        [{"TableName": "Sales", "ColumnName": "Amount", "PandasDataType": "float64"}],
        [
            {"Name": "Total", "TableName": HOME, "Expression": "SUM ( Sales[Amount] )"},
            {"Name": "Amount", "TableName": HOME, "Expression": "[Total] * 2"},
        ],
    )


def test_a_column_named_like_a_dependent_measure_is_not_a_circular_reference() -> None:
    model = _same_name_model()
    update = {"op": "update_measure", "name": "Total", "expression": "SUM ( Sales[Amount] ) + 1"}
    outcome = run(load(), model, update)
    assert outcome.summaries == ["Updated measure [Total] (expression)."]
    assert model.measure_expressions["Total"] == "SUM ( Sales[Amount] ) + 1"


@pytest.mark.parametrize("reference", ["[Amount]", "'# Measures'[Amount]"])
def test_a_reference_to_the_dependent_measure_itself_is_still_circular(reference: str) -> None:
    message = refuses(
        load(),
        _same_name_model(),
        {"op": "update_measure", "name": "Total", "expression": f"{reference} + 1"},
    )
    assert message == (
        "[Total] cannot use [Amount]: [Amount] already depends on [Total], so the two would "
        "refer to each other"
    )


def test_a_rename_whose_dependent_formula_would_be_too_long_is_refused_whole() -> None:
    long_formula = f"[{EMPLOYEES_MTD}]" + " + 0" * 4980  # 19,951 characters
    model = twin_model({DYNAMICS: long_formula})
    document = load()
    message = refuses(
        document,
        model,
        {"op": "rename_measure", "name": EMPLOYEES_MTD, "new_name": "Total Revenue Long Name" * 4},
    )
    assert message == (
        f"The formula of [{DYNAMICS}] uses [{EMPLOYEES_MTD}] and is too long for Studio to "
        f"rewrite (20000 characters at most). Rename [{EMPLOYEES_MTD}] in Power BI instead, "
        "which lets you change that formula too."
    )
    # Nothing was changed on the way to the refusal.
    assert model.measure_table(EMPLOYEES_MTD) == HOME
    assert model.measure_expressions[DYNAMICS] == long_formula
    assert document.commit() == []


def test_a_rename_whose_source_formula_is_already_too_long_is_refused_the_same_way() -> None:
    model = twin_model({DYNAMICS: f"[{EMPLOYEES_MTD}]" + " + 0" * 6250})  # 25,031 characters
    message = refuses(
        load(), model, {"op": "rename_measure", "name": EMPLOYEES_MTD, "new_name": "Shorter"}
    )
    assert message.startswith(f"The formula of [{DYNAMICS}] uses [{EMPLOYEES_MTD}] and is too long")


@pytest.mark.parametrize("hidden", [True, False])
def test_hiding_a_measure_is_refused_because_the_applier_cannot_do_it(hidden: bool) -> None:
    model = twin_model()
    message = refuses(load(), model, {"op": "update_measure", "name": "Rank", "hidden": hidden})
    assert message == (
        "Studio cannot hide or show a measure yet. In Power BI Desktop, right-click [Rank] in "
        f"the Data pane and choose {'Hide' if hidden else 'Unhide'}"
    )


def test_a_measure_only_table_that_lacks_the_field_is_not_reported_as_missing() -> None:
    info = make_info()
    for name in ("Nope", "Region"):
        with pytest.raises(DocumentError) as raised:
            info.resolve(edits.FieldRef(table=HOME, name=name))
        assert str(raised.value) == f"{HOME} has no column or measure named {name!r}"
    with pytest.raises(DocumentError) as raised:
        info.resolve(edits.FieldRef(table="Ghost", name="Nope"))
    assert str(raised.value) == "The model has no table 'Ghost'"
