import pytest

from ai_powerbi_analyst.llm.sql_contracts import sql_binding_problems

PROMPT = "DuckDB Guests(GuestId INTEGER). Visits(GuestId INTEGER). Return guests without a visit."


def test_correct_joins_and_ctes_bind_without_rows():
    assert not sql_binding_problems(
        PROMPT,
        "SELECT g.GuestId FROM Guests g LEFT JOIN Visits v ON g.GuestId=v.GuestId "
        "WHERE v.GuestId IS NULL ORDER BY g.GuestId",
    )
    assert not sql_binding_problems(
        PROMPT, "WITH absent AS (SELECT GuestId FROM Guests) SELECT * FROM absent"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x.GuestId FROM Guests g ORDER BY x.GuestId",
        "SELECT Missing FROM Guests",
        "SELECT GuestId FROM Guests g JOIN Visits v ON g.GuestId=v.GuestId",
        "SELECT GuestId, COUNT(*) FROM Guests",
        "DELETE FROM Guests",
        "SELECT * FROM Guests; SELECT * FROM Visits",
    ],
)
def test_invalid_alias_columns_aggregates_and_writes_fail(sql):
    assert sql_binding_problems(PROMPT, sql)


def test_no_schema_abstains_and_no_external_files_can_be_read():
    assert not sql_binding_problems("Use the supplied table.", "SELECT A FROM T")
    assert sql_binding_problems(
        PROMPT, "SELECT * FROM read_csv_auto('C:/not-an-authorized-file.csv')"
    )
