from ai_powerbi_analyst.llm.artifact_checks import declared_sql_tables, problems


def test_aggregate_calls_are_not_schema_declarations():
    prompt = "Use SQL. The table is named WorkLog. Return SUM(Minutes) per Team."
    assert declared_sql_tables(prompt) == {"worklog"}
    assert not problems(prompt, "SELECT Team,SUM(Minutes) FROM WorkLog GROUP BY Team")
    assert problems(prompt, "SELECT * FROM Bookings")
    assert declared_sql_tables("Use SQL to return SUM(Minutes)") == set()


def test_typed_schemas_are_checked_case_insensitively():
    prompt = "DuckDB Events(Id INTEGER, Cost DOUBLE). Return SUM(Cost)."
    assert declared_sql_tables(prompt) == {"events"}
    assert not problems(prompt, "SELECT SUM(Cost) FROM events")
    assert problems(prompt, "SELECT * FROM SUM")
    assert problems(prompt, "DELETE FROM Events")


def test_m_bundle_keeps_syntax_and_arity_checks():
    assert not problems("Power Query M", "let T=#table(type table [A=number],{{1}}) in T")
    assert problems(
        "Power Query M",
        'let T=#table(type table [A=number],{{1}}) in Table.Sort(T,{"A"},Order.Ascending)',
    )
