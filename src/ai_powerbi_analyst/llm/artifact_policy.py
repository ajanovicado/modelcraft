"""Language-scoped examples and conservative artifact checks, not native execution."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parent
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "column",
        "columns",
        "complete",
        "contains",
        "current",
        "data",
        "every",
        "for",
        "from",
        "has",
        "in",
        "integer",
        "is",
        "it",
        "its",
        "named",
        "number",
        "of",
        "on",
        "only",
        "or",
        "original",
        "output",
        "per",
        "preserve",
        "preserving",
        "requested",
        "return",
        "row",
        "rows",
        "table",
        "tables",
        "text",
        "the",
        "then",
        "these",
        "this",
        "to",
        "using",
        "value",
        "values",
        "with",
        "write",
    ]
)


def domain_for(text: str) -> str | None:
    value = text.lower()
    if re.search(r"power\s*query|\bm\s+(?:code|program)|#table|table\.nestedjoin", value):
        return "m"
    if re.search(r"\b(?:duckdb|sql)\b", value):
        return "sql"
    if re.search(
        r"\b(?:dax|measure|sumx|userelationship)\b|current (?:filter )?context|"
        r"\bsum\b.*inactive relationship",
        value,
    ):
        return "dax"
    if re.search(r"primary key|foreign key|cardinality|relationship|dimension|bridge model", value):
        return "model"
    return None


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z_]+", text.lower())) - _STOP


def references(prompt: str) -> tuple[str, list[str]]:
    """One relevant pattern; abstain for an unclear language. No task-family IDs."""
    domain = domain_for(prompt)
    if domain is None:
        return "", []
    examples = json.loads((ROOT / "artifact_examples.json").read_text(encoding="utf-8"))
    terms = _terms(prompt)
    scored = []
    for i, row in enumerate(examples):
        if row["domain"] != domain:
            continue
        score = 3 * len(terms & _terms(row.get("keywords", "")))
        score += len(terms & _terms(row["brief"]))
        scored.append((score, -i, row))
    best = max(scored, key=lambda item: (item[0], item[1])) if scored else None
    names = {
        "dax": "DAX measure",
        "m": "Power Query M program",
        "sql": "SQL SELECT",
        "model": "Power BI model design",
    }
    header = "\n\nRequested artifact: " + names[domain] + ". "
    header += (
        "Put the actual finished artifact in the artifact string, not the question, "
        "a description, nested JSON metadata or a worked example copied verbatim. "
        "For other response schemas, fill their required fields as instructed.\n"
    )
    if best is None or best[0] <= 0:
        return header, []
    example = best[2]
    return (
        header
        + "\nReference with DIFFERENT names and data; adapt it to the actual request:\n"
        + example["brief"]
        + "\n"
        + example["artifact"],
        [example["id"]],
    )


def dax_structure_problems(expression: str) -> list[str]:
    """Known syntax/arity errors only; do not claim to parse all DAX."""
    tokens = r'/\*.*?\*/|//[^\n]*|--[^\n]*|"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'|\[(?:[^\]]|\]\])*\]'
    masked = re.sub(tokens, lambda m: " " * len(m.group()), expression, flags=re.S)
    errors = []
    if re.search(r"\bFILTERED\s+BY\b", masked, re.I):
        errors.append("FILTERED BY is not DAX syntax; CALCULATE takes comma-separated arguments.")
    arities = {
        "SUMX": (2, 2),
        "AVERAGEX": (2, 2),
        "DIVIDE": (2, 3),
        "RELATED": (1, 1),
        "USERELATIONSHIP": (2, 2),
    }
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z_0-9]*)\s*\(", masked):
        name = match[1].upper()
        if name not in arities:
            continue
        depth = 1
        braces = 0
        commas = 0
        end = match.end()
        while end < len(masked) and depth:
            ch = masked[end]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "{":
                braces += 1
            elif ch == "}":
                braces -= 1
            elif ch == "," and depth == 1 and braces == 0:
                commas += 1
            end += 1
        if depth:
            errors.append(name + " has an unclosed argument list.")
            continue
        count = commas + 1 if expression[match.end() : end - 1].strip() else 0
        low, high = arities[name]
        if not low <= count <= high:
            errors.append(
                f"{name} requires {low}" + (f" to {high}" if low != high else "") + " arguments."
            )
    return errors


def _schema(prompt: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for name, fields in re.findall(r"\b([A-Za-z_]\w*)\s*\(([^()]*)\)", prompt):
        parts = fields.split(",")
        if parts and all(
            re.fullmatch(
                r"\s*[A-Za-z_]\w*(?:\s+(?:integer|number|text|date|varchar|int))?\s*", p, re.I
            )
            for p in parts
        ):
            found[name] = {p.strip().split()[0] for p in parts}
    return found


def problems(prompt: str, artifact: str) -> list[str]:
    domain = domain_for(prompt)
    if not artifact.strip():
        return ["The artifact is empty."]
    if domain == "dax":
        match = re.fullmatch(r"\s*([^=\n]+)\s*=\s*(.+)\s*", artifact, re.S)
        if not match or artifact.lstrip().startswith(("{", "[")):
            return [
                "Return a complete DAX measure: Measure Name = expression. "
                "No metadata or request prose."
            ]
        from ai_powerbi_analyst.powerbi.dax_validation import (
            dax_argument_errors,
            unsupported_dax_functions,
        )

        expression = match[2]
        errors = dax_structure_problems(expression) + dax_argument_errors(expression)
        if unsupported_dax_functions(expression):
            errors.append("Use supported DAX functions, not SQL or invented function names.")
        if re.search(r"current (?:filter )?context", prompt, re.I) and "[" not in expression:
            errors.append(
                "This measure must reference the supplied data and respond to filter context; "
                "do not hardcode arithmetic."
            )
        return errors
    if domain == "m":
        node = shutil.which("node")
        if not node:
            return ["Power Query syntax could not be verified: Node runtime unavailable."]
        result = subprocess.run(  # noqa: S603 - fixed parser; untrusted M is passed on stdin
            [node, str(ROOT / "m_validation.cjs")],
            input=artifact,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=8,
        )
        if result.returncode:
            return ["Power Query parser unavailable; artifact remains unverified."]
        return list(json.loads(result.stdout)["problems"])
    if domain == "sql":
        conn = duckdb.connect(config={"enable_external_access": "false", "threads": 1})
        try:
            statements = conn.extract_statements(artifact)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                return ["Return one SQL SELECT, not the question, prose or a write statement."]
            supplied = _schema(prompt)
            names = conn.get_table_names(artifact)
            if supplied and any(name not in supplied for name in names):
                return [
                    "Use only table names in the supplied schema; do not copy example table names."
                ]
            return []
        except duckdb.Error as error:
            return ["SQL cannot be parsed: " + str(error)[:500]]
        finally:
            conn.close()
    if domain == "model":
        schema = _schema(prompt)
        bad = [
            f"{table}[{column}]"
            for table, column in re.findall(r"\b(\w+)\[([^\]]+)\]", artifact)
            if schema and (table not in schema or column not in schema[table])
        ]
        return ["Nonexistent model references: " + ", ".join(bad)] if bad else []
    return []


def structured_problems(prompt: str, value: dict[str, Any]) -> list[str]:
    """Apply artifact checks only to explicit artifact/formula fields."""
    errors: list[str] = []
    if isinstance(value.get("artifact"), str):
        errors += problems(prompt, value["artifact"])
    if isinstance(value.get("expression"), str):
        errors += dax_structure_problems(value["expression"])
    for item in value.values():
        if isinstance(item, dict):
            errors += structured_problems(prompt, item)
        elif isinstance(item, list):
            for child in item:
                if isinstance(child, dict):
                    errors += structured_problems(prompt, child)
    return list(dict.fromkeys(errors))
