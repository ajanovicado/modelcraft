import pytest

from ai_powerbi_analyst.llm.artifact_policy import (
    dax_structure_problems,
    domain_for,
    problems,
    references,
    structured_problems,
)


def test_retrieval_scopes_language_and_abstains():
    block, ids = references("Write a DAX measure summing visible Units preserving filters")
    assert ids == ["sum-visible"] and "Table.NestedJoin" not in block
    assert references("hello there") == ("", [])
    assert domain_for("Write SUM of Cost through the existing inactive relationship") == "dax"
    assert references(
        "DuckDB Crew(Id,Name,LeadId); return leader Name AS Leader; no Markdown fencing"
    )[1] == ["sql-self"]


@pytest.mark.parametrize(
    "expression",
    [
        "CALCULATE(SUM(T[X]) FILTERED BY T[Y]=1)",
        "SUMX(T)",
        "DIVIDE(SUM(T[X]))",
        "USERELATIONSHIP(T[A],U[B],V[C])",
    ],
)
def test_rejects_observed_dax_errors(expression):
    assert dax_structure_problems(expression)


def test_accepts_equivalent_dax_and_quoted_text():
    assert problems("Write a DAX measure in current filter context", "Total = SUMX(T,T[X])") == []
    assert not dax_structure_problems('IF(T[X]=1,"FILTERED BY", "DIVIDE(a)")')
    assert problems("Write a measure in current context", "Total = 42")
    assert problems("Write DAX", '{"description":"SUM(T[X])"}')


def test_sql_rejects_copied_schema_and_writes():
    prompt = "DuckDB Records(Id INTEGER, Value INTEGER). Return all records."
    assert not problems(prompt, "SELECT Id, Value FROM Records")
    assert problems(prompt, "SELECT * FROM Bookings")
    assert problems(prompt, "DELETE FROM Records")


def test_m_parser_and_known_function_arity():
    prompt = "Write Power Query M"
    good = 'let T=#table(type table [A=number],{{1}}) in Table.Sort(T,{{"A",Order.Ascending}})'
    assert not problems(prompt, good)
    assert problems(prompt, "```m\n" + good + "\n```")
    assert problems(prompt, "let T=#table(type table [A=number],{1}) in T")
    assert problems(
        prompt, 'let T=#table(type table [A=number],{{1}}) in Table.Sort(T,{"A"},Order.Ascending)'
    )
    # M let bindings are lazy: unused bad arithmetic does not invalidate the result.
    assert not problems(prompt, "let T=#table(type table [A=number],{{1}}), Unused=T[A]*T[A] in T")


def test_explicit_fields_only():
    assert not structured_problems("Write DAX", {"description": "SUMX(T)"})
    assert structured_problems("Write DAX", {"measures": [{"expression": "SUMX(T)"}]})
