"""Production artifact checks; frozen benchmark policy remains reproducible."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any

import duckdb

from . import artifact_policy as frozen


def declared_sql_tables(prompt: str) -> set[str]:
    """Recognize explicit declarations; function calls are not table schemas.

    Unknown prompt formats abstain from table-name checking rather than inventing
    a schema. SQL parsing and the application's data-bound execution still apply.
    """
    tables = set()
    column = (
        r"\s*[A-Za-z_]\w*\s+(?:INTEGER|INT|BIGINT|SMALLINT|VARCHAR|TEXT|DOUBLE|FLOAT|"
        r"REAL|BOOLEAN|DATE|TIMESTAMP|NUMBER)\s*"
    )
    for name, fields in re.findall(r"\b([A-Za-z_]\w*)\s*\(([^()]*)\)", prompt):
        if all(re.fullmatch(column, item, re.I) for item in fields.split(",")):
            tables.add(name.casefold())
    tables.update(
        name.casefold()
        for name in re.findall(r'\btable\s+(?:is\s+)?named\s+["`]?([A-Za-z_]\w*)', prompt, re.I)
    )
    return tables


def problems(prompt: str, artifact: str) -> list[str]:
    domain = frozen.domain_for(prompt)
    if domain == "sql":
        conn = duckdb.connect(config={"enable_external_access": "false", "threads": 1})
        try:
            statements = conn.extract_statements(artifact)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                return ["Return one SQL SELECT, not prose or a write statement."]
            supplied = declared_sql_tables(prompt)
            if supplied and any(
                name.casefold() not in supplied for name in conn.get_table_names(artifact)
            ):
                return [
                    "Use only table names in the supplied schema; do not copy example table names."
                ]
            return []
        except duckdb.Error as exc:
            return ["SQL cannot be parsed: " + str(exc)[:500]]
        finally:
            conn.close()
    if domain == "m":
        node = shutil.which("node")
        if not node:
            return ["Power Query syntax could not be verified: Node runtime unavailable."]
        result = subprocess.run(  # noqa: S603 - local parser, artifact passed only through stdin
            [node, str(frozen.ROOT / "m_validation.cjs")],
            input=artifact,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode:
            return ["Power Query parser unavailable; artifact remains unverified."]
        return list(json.loads(result.stdout)["problems"])
    return frozen.problems(prompt, artifact)


def structured_problems(prompt: str, value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if isinstance(value.get("artifact"), str):
        errors += problems(prompt, value["artifact"])
    if isinstance(value.get("expression"), str):
        errors += frozen.dax_structure_problems(value["expression"])
    for item in value.values():
        if isinstance(item, dict):
            errors += structured_problems(prompt, item)
        elif isinstance(item, list):
            for child in item:
                if isinstance(child, dict):
                    errors += structured_problems(prompt, child)
    return list(dict.fromkeys(errors))
