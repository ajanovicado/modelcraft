"""Request-bound artifact checks and deterministic relationship rendering.

These checks establish selected structural/request constraints, not complete
DAX semantics or data-profile facts. Unspecified facts are never fabricated.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from ai_powerbi_analyst.contracts import ClosedModel

from .artifact_checks import problems as existing_problems

_IDENT = r"[^\W\d]\w*"
_TABLE = rf"(?:'(?:[^']|'')+'|{_IDENT})"
_COLUMN = r"\[(?:[^\]]|\]\])+\]"
_REF = re.compile(rf"(?P<table>{_TABLE})\s*(?P<column>{_COLUMN})")
_TOKEN = re.compile(
    rf"\s+|//[^\n]*|--[^\n]*|/\*.*?\*/|"
    rf'"(?:[^\"]|\"\")*"|{_TABLE}\s*{_COLUMN}|{_COLUMN}|'
    rf"'(?:[^']|'')+'|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|"
    rf"{_IDENT}|&&|\|\||<>|<=|>=|==|[(){{}},+*/^&=<>!-]",
    re.S,
)


def references(text: str) -> set[tuple[str, str]]:
    return {
        (
            (
                m["table"][1:-1].replace("''", "'") if m["table"].startswith("'") else m["table"]
            ).casefold(),
            m["column"][1:-1].replace("]]", "]").casefold(),
        )
        for m in _REF.finditer(text)
    }


def requested_measure_name(prompt: str) -> str | None:
    quoted = re.search(r'\bmeasure\s+(?:named|called)\s+"([^"]+)"', prompt, re.I)
    if quoted:
        return quoted[1]
    plain = re.search(
        rf"\bmeasure\s+(?:named|called)\s+({_IDENT}(?: +{_IDENT})*?)"
        r"(?=\s+(?:as|summing|using|that|which|to)\b|[.\n]|$)",
        prompt,
        re.I,
    )
    return plain[1].strip() if plain else None


def declared_columns(prompt: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for table, fields in re.findall(
        rf"\b({_IDENT})\s+has\s+(?:columns\s+)?([^.!?\n]+)", prompt, re.I
    ):
        parts = re.split(r",\s*|\s+and\s+", fields)
        if all(re.fullmatch(_IDENT, p.strip()) for p in parts):
            found[table.casefold()] = {p.strip().casefold() for p in parts}
    return found


def dax_problems(prompt: str, artifact: str) -> list[str]:
    errors = existing_problems(prompt, artifact)
    match = re.fullmatch(r"\s*([^=\n]+?)\s*=\s*(.+?)\s*", artifact, re.S)
    if not match:
        return errors or ["Return exactly one complete Name = expression measure."]
    expected = requested_measure_name(prompt)
    if expected and match[1].strip() != expected:
        errors.append("Use the requested measure name: " + expected)
    expression = match[2]
    schema = declared_columns(prompt)
    supplied_refs = references(prompt)
    # Consume every character: punctuation/prose outside the language cannot be
    # silently ignored. Strings, quoted identifiers and comments remain opaque.
    tokens: list[str] = []
    offset = 0
    while offset < len(expression):
        token_match = _TOKEN.match(expression, offset)
        if token_match is None:
            errors.append("Unexpected DAX text; return the expression without explanation.")
            break
        value = token_match[0]
        if not value.isspace() and not value.startswith(("//", "--", "/*")):
            tokens.append(value)
        offset = token_match.end()
    for value in tokens:
        if not _REF.fullmatch(value):
            continue
        for table, column in references(value):
            if table in schema and column not in schema[table]:
                errors.append(f"Unknown supplied column: {table}[{column}]")
            if schema and table not in schema and (table, column) not in supplied_refs:
                errors.append(f"Table is absent from the supplied schema: {table}")
    variables = {tokens[i + 1].casefold() for i, t in enumerate(tokens[:-1]) if t.upper() == "VAR"}
    table_names = {table for table, _ in supplied_refs} | set(schema)
    keywords = {
        "var",
        "return",
        "true",
        "false",
        "not",
        "in",
        "asc",
        "desc",
        "dense",
        "skip",
        "day",
        "month",
        "quarter",
        "year",
        "week",
        "both",
        "none",
        "oneway",
    }
    stack: list[str] = []
    for i, token in enumerate(tokens):
        if token in {"(", "{"}:
            stack.append(token)
        elif token in {")", "}"}:
            if not stack or stack.pop() != {")": "(", "}": "{"}[token]:
                errors.append("Unbalanced DAX delimiters.")
        elif re.fullmatch(_IDENT, token):
            function = i + 1 < len(tokens) and tokens[i + 1] == "("
            if not function and token.casefold() not in keywords | variables | table_names:
                errors.append("Unexpected identifier or explanation in DAX: " + token)
    if stack:
        errors.append("Unbalanced DAX delimiters.")
    alternate = re.search(r"\balternate result\s+(-?\d+(?:\.\d+)?)\b", prompt, re.I)
    if alternate and re.search(r"\bDIVIDE\b", prompt, re.I):
        # A requested zero fallback is semantically different from DIVIDE's
        # omitted/BLANK fallback. Parse top-level call arguments, not substrings.
        for i, token in enumerate(tokens[:-1]):
            if token.upper() != "DIVIDE" or tokens[i + 1] != "(":
                continue
            args: list[str] = []
            part: list[str] = []
            depth = 1
            for value in tokens[i + 2 :]:
                if value == "(":
                    depth += 1
                elif value == ")":
                    depth -= 1
                    if depth == 0:
                        args.append("".join(part))
                        break
                if value == "," and depth == 1:
                    args.append("".join(part))
                    part = []
                else:
                    part.append(value)
            if len(args) != 3 or args[2] != alternate[1]:
                errors.append("DIVIDE must include the requested alternate result " + alternate[1])
    return list(dict.fromkeys(errors))


class RelationshipAnswer(ClosedModel):
    from_ref: str = Field(min_length=1, max_length=256)
    to_ref: str = Field(min_length=1, max_length=256)
    cardinality: Literal["1:1", "1:*", "*:1", "*:*"]
    is_active: bool
    filter_direction: Literal["from_to", "to_from", "both"]

    @model_validator(mode="after")
    def valid_references(self) -> RelationshipAnswer:
        if not _REF.fullmatch(self.from_ref) or not _REF.fullmatch(self.to_ref):
            raise ValueError("Each endpoint must be Table[Column] or 'Table name'[Column].")
        return self

    @property
    def from_table(self) -> str:
        match = _REF.fullmatch(self.from_ref)
        assert match is not None
        return match["table"]

    @property
    def from_column(self) -> str:
        match = _REF.fullmatch(self.from_ref)
        assert match is not None
        return match["column"][1:-1]

    @property
    def to_table(self) -> str:
        match = _REF.fullmatch(self.to_ref)
        assert match is not None
        return match["table"]

    @property
    def to_column(self) -> str:
        match = _REF.fullmatch(self.to_ref)
        assert match is not None
        return match["column"][1:-1]

    @property
    def from_cardinality(self) -> str:
        return "one" if self.cardinality[0] == "1" else "many"

    @property
    def to_cardinality(self) -> str:
        return "one" if self.cardinality[-1] == "1" else "many"

    def endpoints(self) -> set[tuple[str, str]]:
        return references(self.from_ref) | references(self.to_ref)

    def render(self) -> str:
        direction = "Both (bidirectional)"
        if self.filter_direction != "both":
            source, target = (
                (self.from_table, self.to_table)
                if self.filter_direction == "from_to"
                else (self.to_table, self.from_table)
            )
            direction = f"Single, from {source} to {target}"
        return (
            f"Relationship: {self.from_ref} to {self.to_ref}. "
            f"Cardinality: {self.cardinality}. "
            f"Active status: {'Yes' if self.is_active else 'No (inactive)'}. "
            f"Filter direction: {direction}."
        )


def relationship_problems(prompt: str, answer: RelationshipAnswer) -> list[str]:
    errors = []
    supplied = references(prompt)
    if len(supplied) == 2 and answer.endpoints() != supplied:
        errors.append("Use exactly the two relationship columns supplied in the request.")
    if len(answer.endpoints()) != 2:
        errors.append("A relationship needs two distinct endpoints.")
    pair = (answer.from_cardinality, answer.to_cardinality)
    if re.search(r"\bone[- ]to[- ]one\b|\b1\s*:\s*1\b", prompt, re.I) and pair != ("one", "one"):
        errors.append("The requested relationship is one-to-one.")
    if pair == ("one", "one") and answer.filter_direction != "both":
        errors.append("Power BI one-to-one relationships require Both filter direction.")
    status = re.search(
        r"\b(inactive|active)\s+(?:(?:Power BI|one[- ]to[- ]one)\s+){0,2}relationship\b",
        prompt,
        re.I,
    )
    if status and status[1].lower() == "inactive" and answer.is_active:
        errors.append("The request requires an inactive relationship.")
    elif status and status[1].lower() == "active" and not answer.is_active:
        errors.append("The request requires an active relationship.")
    for table, column, cardinality in [
        (answer.from_table, answer.from_column, answer.from_cardinality),
        (answer.to_table, answer.to_column, answer.to_cardinality),
    ]:
        ref = re.escape(table) + r"\s*\[" + re.escape(column) + r"\]"
        if re.search(ref + r"\s+is\s+unique\b", prompt, re.I) and cardinality != "one":
            errors.append(f"{table}[{column}] is declared unique and must be the one side.")
        if re.search(ref + r"\s+repeats\b", prompt, re.I) and cardinality != "many":
            errors.append(f"{table}[{column}] is declared repeating and must be the many side.")
    if re.search(r"\bsingle(?:[- ]direction)?\b", prompt, re.I):
        expected = {("one", "many"): "from_to", ("many", "one"): "to_from"}.get(pair)
        if expected and answer.filter_direction != expected:
            errors.append("Use single filtering from the one side to the many side.")
    return errors
