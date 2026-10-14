import pytest
from pydantic import ValidationError

from ai_powerbi_analyst.llm.artifact_contracts import (
    RelationshipAnswer,
    dax_problems,
    references,
    relationship_problems,
    requested_measure_name,
)

PROMPT = (
    'Write a DAX measure named "Net Units" summing StockLedger[Units]. '
    "StockLedger has columns ItemId, Units and Region. Preserve current filter context."
)


@pytest.mark.parametrize(
    "artifact",
    [
        "Net Units = SUM(StockLedger[Units])",
        "Net Units = SUMX(StockLedger, StockLedger[Units])",
        "Net Units = VAR n = SUM(StockLedger[Units]) RETURN n",
        'Net Units = CALCULATE(SUM(StockLedger[Units]), StockLedger[Region] = "East")',
        "Net Units = SUM(StockLedger[Units]) // source column",
        "Net Units = SUM(StockLedger[Units]) // ExampleSales[Amount] is a comment",
        'Net Units = IF(StockLedger[Region] = "ExampleSales[Amount]", 1, 0)',
    ],
)
def test_valid_dax_forms_are_not_rejected(artifact):
    assert not dax_problems(PROMPT, artifact)


@pytest.mark.parametrize(
    "artifact,reason",
    [
        ("Other Name = SUM(StockLedger[Units])", "measure name"),
        ("Net Units = SUM(ExampleSales[Amount])", "absent"),
        ("Net Units = SUM(StockLedger[Missing])", "Unknown"),
        (
            "Net Units = SUM(StockLedger[Units])\nHere is another example: total sales.",
            "Unexpected",
        ),
        ("Net Units = SUM(StockLedger[Units]))", "Unbalanced"),
        ("Net Units = SUM(StockLedger[Units]) /* unfinished", "Unexpected"),
    ],
)
def test_wrong_or_contaminated_answers_fail(artifact, reason):
    assert any(reason in p for p in dax_problems(PROMPT, artifact))


def test_quoted_names_strings_and_partial_schema():
    prompt = "Write a DAX measure named \"Total Ounces\" summing 'Daily Cargo'[Ounces]."
    assert requested_measure_name(prompt) == "Total Ounces"
    assert not dax_problems(prompt, "Total Ounces = SUM('Daily Cargo'[Ounces])")
    assert references("'O''Brien'[Net]]Units]") == {("o'brien", "net]units")}
    assert (
        requested_measure_name("Write a DAX measure named ProfitRate as SUM(A[B]).") == "ProfitRate"
    )
    assert requested_measure_name("Explain DAX measures") is None


@pytest.mark.parametrize("function", ["CALCULATE", "SUM", "SUMX"])
def test_using_function_is_instruction_not_part_of_measure_name(function):
    prompt = f"Write a DAX measure named NetWeight using {function} to sum Loads[Weight]."
    assert requested_measure_name(prompt) == "NetWeight"
    assert not dax_problems(prompt, "NetWeight = SUM(Loads[Weight])")
    assert requested_measure_name('Write a DAX measure named "Revenue Using Cash".') == (
        "Revenue Using Cash"
    )


def relationship(**updates):
    values = dict(
        from_ref="Devices[DeviceId]",
        to_ref="Registrations[DeviceId]",
        cardinality="1:1",
        is_active=True,
        filter_direction="both",
    )
    return RelationshipAnswer(**(values | updates))


def test_relationship_contract_and_rendering_cover_required_fields():
    prompt = (
        "Give the active one-to-one relationship between "
        "Devices[DeviceId] and Registrations[DeviceId]."
    )
    answer = relationship()
    assert not relationship_problems(prompt, answer)
    assert "Active status: Yes" in answer.render()
    assert "Cardinality: 1:1" in answer.render()
    assert "Both" in answer.render()
    for update in [
        dict(from_ref="Examples[ExampleId]"),
        dict(cardinality="1:*"),
        dict(is_active=False),
        dict(filter_direction="from_to"),
    ]:
        assert relationship_problems(prompt, relationship(**update))


def test_missing_relationship_properties_and_prose_cannot_become_valid_plan():
    with pytest.raises(ValidationError):
        RelationshipAnswer(artifact="Relationship ... drag line to center.")
    with pytest.raises(ValidationError):
        relationship(from_ref="Devices[DeviceId]. Then open Settings")
    values = relationship().model_dump()
    del values["is_active"]
    with pytest.raises(ValidationError):
        RelationshipAnswer(**values)


def test_inactive_and_reversed_endpoint_order_are_supported():
    prompt = (
        "Give the inactive Power BI relationship between Parents[Pid] and Children[Pid]. "
        "Parents[Pid] is unique; Children[Pid] repeats. Use single filter direction."
    )
    answer = relationship(
        from_ref="Children[Pid]",
        to_ref="Parents[Pid]",
        cardinality="*:1",
        is_active=False,
        filter_direction="to_from",
    )
    assert not relationship_problems(prompt, answer)
    assert relationship_problems(prompt, answer.model_copy(update={"filter_direction": "from_to"}))
    assert not relationship_problems(
        "Describe the relationship. State active status.", relationship(is_active=False)
    )
    assert relationship_problems(
        "Give the inactive one-to-one Power BI relationship between "
        "Devices[DeviceId] and Registrations[DeviceId].",
        relationship(is_active=True),
    )
