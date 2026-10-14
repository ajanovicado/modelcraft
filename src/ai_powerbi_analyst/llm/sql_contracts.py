"""Bind SQL against explicitly supplied, empty DuckDB tables without running it."""

from __future__ import annotations

import re
import threading

import duckdb

_COLUMN = re.compile(
    r"\s*([A-Za-z_]\w*)\s+"
    r"(INTEGER|INT|BIGINT|SMALLINT|VARCHAR|TEXT|DOUBLE|FLOAT|REAL|BOOLEAN|DATE|TIMESTAMP|NUMBER)\s*",
    re.I,
)


def sql_binding_problems(prompt: str, artifact: str) -> list[str]:
    schemas: dict[str, list[tuple[str, str]]] = {}
    for name, fields in re.findall(r"\b([A-Za-z_]\w*)\s*\(([^()]*)\)", prompt):
        column_matches = [_COLUMN.fullmatch(item) for item in fields.split(",")]
        if column_matches and all(column_matches):
            schemas[name] = [
                (m[1], "DOUBLE" if m[2].upper() == "NUMBER" else m[2])
                for m in column_matches
                if m is not None
            ]
    if not schemas:
        return []  # Unrecognized schemas cannot justify guessing column names.
    conn = duckdb.connect(
        config={
            "enable_external_access": "false",
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
            "threads": 1,
            "memory_limit": "256MB",
        }
    )
    timer = threading.Timer(3, conn.interrupt)
    try:
        timer.start()
        statements = conn.extract_statements(artifact)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            return ["Return exactly one read-only SQL SELECT."]
        declared = {name.casefold() for name in schemas}
        if any(name.casefold() not in declared for name in conn.get_table_names(artifact)):
            return []  # Existing table checks handle mismatches; partial schemas abstain.
        for name, columns in schemas.items():
            fields = ", ".join(f'"{column}" {kind}' for column, kind in columns)
            conn.execute(f'CREATE TABLE "{name}" ({fields})')
        # EXPLAIN binds aliases, columns and aggregates. No user rows are loaded
        # or queried, and this is not evidence of semantic correctness.
        conn.execute("EXPLAIN " + artifact)
        return []
    except duckdb.Error as exc:
        return ["SQL does not bind to the supplied schema: " + str(exc)[:500]]
    finally:
        timer.cancel()
        timer.join()
        conn.close()
