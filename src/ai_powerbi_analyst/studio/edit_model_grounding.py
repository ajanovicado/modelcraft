"""Request-grounded role predicates and group targets, independent of the planner.

Only a complete single-value categorical request is compiled here. Other RLS formulas
keep the existing drafting path and must pass ``validate_role_expression`` as well as
the planner's ordinary checks. No generated formula is repaired with text substitution.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from . import dax_syntax as dax
from .edit_context import EditContext, VisualEntry
from .edit_targets import find_types, resolve_visuals
from .edit_text import tokens
from .edit_types import Choice, Clause, ParseKind

_ROLE_OPS = {"create_role", "update_role"}
_GROUP_ONLY = {"ungroup_visuals", "set_group"}
_RELATIONSHIP_OPS = {"create_relationship", "update_relationship", "delete_relationship"}


def _relationship_values(text: str) -> dict[str, list[str | bool]]:
    """Relationship settings said in words, separate from table/column identity.

    Security direction and ordinary filtering share enum values but are distinct settings.
    Keeping their clauses separate prevents 'security both ways' changing cross-filtering.
    """
    values: dict[str, list[str | bool]] = {}

    def add(key: str, value: str | bool) -> None:
        if value not in values.setdefault(key, []):
            values[key].append(value)

    for match in re.finditer(r"\b(many|one)[\s_-]*to[\s_-]*(many|one)\b", text, re.I):
        add("cardinality", f"{match[1].lower()}_to_{match[2].lower()}")
    for match in re.finditer(
        r"\b(inactive|deactivat(?:e|ed)|activat(?:e|ed)|active)\b", text, re.I
    ):
        value = match[1].lower() not in {"inactive", "deactivate", "deactivated"}
        if re.search(r"\b(?:not|non|no\s+longer)\s*$", text[: match.start()], re.I):
            value = not value
        add("active", value)
    for clause in re.split(r"[,;]|\b(?:and|but)\b", text, flags=re.I):
        if re.search(r"\breferential\s+integrity\b", clause, re.I):
            negative = re.search(
                r"\b(?:no|not|without|disable|stop|do\s+not|don'?t)\b", clause, re.I
            )
            add("assume_referential_integrity", not bool(negative))
        setting = (
            "security_filter"
            if re.search(r"\b(?:security|rls)\b", clause, re.I)
            else "cross_filter"
        )
        for match in re.finditer(
            r"\b(both\s+(?:ways|directions)|bidirectional|single(?:\s+direction)?|one[\s-]+way)\b",
            clause,
            re.I,
        ):
            add(
                setting,
                "both" if match[1].lower().startswith(("both", "bidirectional")) else "single",
            )
    return values


def relationship_setting(name: str, text: str) -> str | bool | None:
    """One explicit setting, or None when absent/contradictory (grounding asks on conflict)."""
    values = _relationship_values(text).get(name, [])
    return values[0] if len(values) == 1 else None


def ground_relationship(
    context: EditContext,
    text: str,
    card_or_op: Any,
    filled: Mapping[str, Any],
) -> dict[str, Any] | Clause:
    """Keep explicit join operands/settings authoritative over the model's numbered choices."""
    if _op(card_or_op) not in _RELATIONSHIP_OPS:
        return dict(filled)
    values = dict(filled)
    qualified: list[tuple[int, str, str]] = []
    settings_text = text
    for table, columns in context.tables.items():
        owner = rf"(?:'{re.escape(table.replace(chr(39), chr(39) * 2))}'|{re.escape(table)})"
        for column in columns:
            pattern = rf"(?<!\w){owner}\s*\[{re.escape(column.replace(']', ']]'))}\]"
            qualified += [(m.start(), table, column) for m in re.finditer(pattern, text, re.I)]
            settings_text = re.sub(pattern, " ", settings_text, flags=re.I)
    setting_values = (
        {} if _op(card_or_op) == "delete_relationship" else _relationship_values(settings_text)
    )
    for key, requested in setting_values.items():
        if len(requested) > 1:
            return _ask(
                text,
                f"Which {key.replace('_', ' ')} should this relationship use? Specify one setting.",
            )
        values[key] = requested[0]
    ends = list(dict.fromkeys((table, column) for _, table, column in sorted(qualified)))
    if len(ends) > 2:
        return _ask(
            text, "Which two table columns should this relationship join? Specify one pair."
        )
    if len(ends) == 2:
        # from/to order (or a cardinality between the two references) is explicit. 'between'
        # with no cardinality only identifies an unordered pair and keeps the selected order.
        explicit_order = (
            bool(re.search(r"\bfrom\b", text[: min(q[0] for q in qualified)], re.I))
            or "cardinality" in values
        )
        chosen = [values.get(side) for side in ("from", "to")]
        chosen_ends = [(v.get("table"), v.get("name")) for v in chosen if isinstance(v, Mapping)]
        if explicit_order or set(chosen_ends) != set(ends):
            for side, (table, column) in zip(("from", "to"), ends, strict=True):
                values[side] = {"table": table, "name": column}
                values.pop(f"{side}_table", None)
    direction = re.search(
        r"\bfilter(?:s|ing)?(?:\s+(?:only|in|a|single|one|way|direction)){0,5}\s+from\s+"
        r"(.+?)\s+to\s+(.+?)(?:[,.;]|$)",
        text,
        re.I,
    )
    if direction and values.get("cross_filter") != "both":
        source, target = values.get("from"), values.get("to")
        if isinstance(source, Mapping) and isinstance(target, Mapping):
            filter_sources = [t for t in context.tables if _name_in(direction[1], t)]
            filter_targets = [t for t in context.tables if _name_in(direction[2], t)]
            existing = next(
                (
                    r
                    for r in context.relationships
                    if r.joins(
                        (str(source.get("table")), str(source.get("name"))),
                        (str(target.get("table")), str(target.get("name"))),
                    )
                ),
                None,
            )
            cardinality = str(values.get("cardinality") or "")
            many = {
                "many_to_one": (True, False),
                "one_to_many": (False, True),
                "one_to_one": (False, False),
                "many_to_many": (True, True),
            }.get(cardinality)
            if many is None and _op(card_or_op) == "create_relationship":
                many = (True, False)
            if many is None and existing is not None and existing.cardinality_known:
                many = (existing.from_many, existing.to_many)
                if existing.from_table.casefold() != str(source.get("table")).casefold():
                    many = many[::-1]
            if many == (True, True):
                if len(filter_sources) != 1 or len(filter_targets) != 1:
                    return _ask(
                        text,
                        "Which table should filter which in this many-to-many "
                        "relationship? Specify both table names.",
                    )
                if _op(card_or_op) == "create_relationship":
                    expected = (target.get("table"), source.get("table"))
                elif existing is not None and context.relationships_known:
                    expected = (existing.to_table, existing.from_table)
                else:
                    return _ask(
                        text,
                        "The stored many-to-many relationship direction is unknown. "
                        "Read the current model or configure its direction in Power BI first.",
                    )
                if (filter_sources[0], filter_targets[0]) != expected:
                    return _ask(
                        text,
                        "That single direction is opposite to the stored "
                        "many-to-many relationship. Configure its direction in Power BI, "
                        "or explicitly request both directions if intended.",
                    )
            if len(filter_sources) == len(filter_targets) == 1 and many in {
                (True, False),
                (False, True),
            }:
                one_table = target.get("table") if many[0] else source.get("table")
                if filter_sources[0] != one_table:
                    return _ask(
                        text,
                        "Single-direction filtering flows from the one side to the many side. "
                        "Do you want both directions, or one-to-many filtering?",
                    )
    return values


_SEES = re.compile(
    r"\b(?:can\s+(?:only\s+)?(?:see|view|access)|see(?:s)?(?:\s+only)?|"
    r"view(?:s)?\s+only|(?:limited|restricted)\s+to|access\s+to|"
    r"filter(?:s|ed)?\s+(?:to|on|by))\s+",
    re.I,
)
_COMPLEX = re.compile(
    r"\b(?:and|or|except|unless|without|excluding|not|where|when|during|for|"
    r"within|between|above|below|over|under|greater|less|at\s+least|at\s+most|"
    r"biggest|smallest|largest|highest|lowest)\b|[<>=]",
    re.I,
)
_COUNTS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "both": 2,
    "pair": 2,
    "couple": 2,
}


def _op(card_or_op: Any) -> str:
    return card_or_op if isinstance(card_or_op, str) else str(card_or_op.op)


def _ask(text: str, question: str, choices: tuple[Choice, ...] = ()) -> Clause:
    return Clause(text, ParseKind.CLARIFY, question=question, choices=choices)


def _name_in(text: str, name: str) -> bool:
    return bool(name and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I))


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in re.finditer(r""""(?:[^"]|"")*"|'(?:[^']|'')*'""", text)]


def _outside_quotes(match: re.Match[str], spans: list[tuple[int, int]]) -> bool:
    return not any(start <= match.start() < end for start, end in spans)


def _named_entries(text: str, entries: list[VisualEntry]) -> list[VisualEntry]:
    """A shorter caption inside a longer caption is not another requested visual."""
    spans = [
        (v, m.span())
        for v in entries
        if v.caption
        for m in re.finditer(
            r"(?<!\w)" + re.escape(v.caption) + r"(?!\w)",
            text,
            re.I,
        )
    ]
    found = {
        v.id
        for v, (start, end) in spans
        if not any(
            outer_start <= start and end <= outer_end and outer_end - outer_start > end - start
            for _, (outer_start, outer_end) in spans
        )
    }
    return [v for v in entries if v.id in found]


def validate_role_expression(
    expression: str,
    context: EditContext,
    *,
    table: str | None = None,
) -> list[str]:
    """Check names with lexical VAR scope; single quotes identify tables, never text.

    This is a naming/syntax check, not a claim of boolean type or native execution.
    Known tables, declared earlier variables, columns and measures remain available
    to complex expressions, including relationship-aware predicates.
    """
    if dax.is_query(expression):
        return ["A role filter needs a row predicate, not an EVALUATE query."]
    try:
        node = dax.parse_expression(expression)
    except (dax.DaxParseError, ValueError) as error:
        return [f"The role filter is not valid DAX: {error}"]
    tables = {
        name.casefold(): {c.casefold() for c in cols} for name, cols in context.tables.items()
    }
    measures = {m.name.casefold() for m in context.measures}
    problems: list[str] = []
    if table is not None and table.casefold() not in tables:
        problems.append(f"There is no table '{table}' for the role filter.")

    def walk(item: dax.Node, variables: frozenset[str]) -> None:
        if isinstance(item, dax.Name):
            key = item.name.casefold()
            if key and key not in tables and (item.quoted or key not in variables):
                problems.append(
                    f"The role filter cannot resolve identifier '{item.name}'. "
                    'Text values need double quotes, for example "Online".'
                )
        elif isinstance(item, dax.ColRef):
            column = item.column.casefold()
            if item.table:
                owner = tables.get(item.table.casefold())
                if owner is None or (column not in owner and column not in measures):
                    problems.append(f"There is no field {item.table}[{item.column}].")
            elif column not in measures:
                owners = [name for name, cols in tables.items() if column in cols]
                if table and table.casefold() in tables:
                    if column not in tables[table.casefold()]:
                        problems.append(f"{table} has no column [{item.column}].")
                elif len(owners) != 1:
                    problems.append(f"Resolve the table for column [{item.column}].")
        elif isinstance(item, dax.VarBlock):
            scope = variables
            for name, value in item.variables:
                walk(value, scope)
                scope = scope | {name.casefold()}
            walk(item.body, scope)
        elif isinstance(item, dax.Call):
            for arg in item.args:
                walk(arg, variables)
        elif isinstance(item, dax.Binary):
            walk(item.left, variables)
            walk(item.right, variables)
        elif isinstance(item, dax.Unary):
            walk(item.operand, variables)
        elif isinstance(item, dax.TableCtor):
            for row in item.rows:
                for value in row:
                    walk(value, variables)

    walk(node, frozenset())
    return list(dict.fromkeys(problems))


def _field_forms(name: str) -> list[str]:
    words = list(tokens(name))
    forms = [words]
    if len(words) > 1 and words[-1] == "name":
        forms.append(words[:-1])
    patterns = []
    for form in forms:
        # CamelCase fields also match ordinary noun phrases: StoreType -> stores of type.
        parts = [re.escape(word) + (r"s?" if not word.endswith("s") else "") for word in form]
        patterns.append(r"(?<!\w)" + r"\s+(?:of\s+)?".join(parts) + r"(?!\w)")
    patterns.append(r"(?<!\w)" + re.escape(name) + r"(?!\w)")
    return patterns


def _literal(text: str) -> str | None:
    value = re.sub(r"^(?:(?:the|only)\s+)+", "", text.strip(), flags=re.I)
    value = re.sub(r"^(?:is|equals?|equal\s+to|must\s+be|of\s+type)\s+", "", value, flags=re.I)
    value = value.strip().rstrip(".").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        quote = value[0]
        inner = value[1:-1]
        # A quoted literal cannot contain another unmatched quote or a trailing clause.
        if quote in inner.replace(quote + quote, ""):
            return None
        return inner.replace(quote + quote, quote)
    if (
        not value
        or _COMPLEX.search(value)
        or re.search(r"[\[\]()`;\n]", value)
        or re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value)
    ):
        return None
    return value


def _typed_role_body(text: str) -> str | None:
    """Recognize a labelled row predicate, including an unfenced equality body.

    The planner's generic assignment extractor cannot read a row equality whose RHS
    is just a literal. Its entire typed body still takes precedence over prose here.
    """
    fenced = re.search(r"`([^`]+)`", text)
    if fenced and re.search(r"[\[(]|\bVAR\b", fenced.group(1), re.I):
        return fenced.group(1).strip()
    labels = re.finditer(
        r"\b(?:filter|predicate|formula|dax)\b\s*(?:(?:to|as|is)\b\s*|[:=]\s*)?",
        text,
        re.I,
    )
    for label in labels:
        body = text[label.end() :].strip()
        if re.match(r"(?:VAR\b|[A-Za-z_][\w]*\s*\(|(?:'(?:[^']|'')+'|[\w]+)?\s*\[)", body, re.I):
            return body
    return None


def _role_literal_text(tail: str, mention: re.Match[str], table: str | None) -> str:
    """Remove only field/owner evidence outside quoted categorical values.

    A bare owner is metadata when it directly adjoins the field; a prepositional
    owner may appear elsewhere. Work on the original spans so quoted owner words
    remain literal data even when the same table is explicitly named outside them.
    """
    removed = [mention.span()]
    if table is not None:
        quoted = _quoted_spans(tail)
        owner = r"(?<!\w)(?:the\s+)?(?:table\s+)?" + re.escape(table) + r"(?:\s+table)?(?!\w)"
        for match in re.finditer(r"\b(?:in|on|from)\s+" + owner, tail, re.I):
            if _outside_quotes(match, quoted):
                removed.append(match.span())
        for match in re.finditer(owner, tail, re.I):
            adjacent = (
                match.end() <= mention.start() and not tail[match.end() : mention.start()].strip()
            ) or (
                match.start() >= mention.end() and not tail[mention.end() : match.start()].strip()
            )
            if adjacent and _outside_quotes(match, quoted):
                removed.append(match.span())
    value = list(tail)
    for start, end in removed:
        value[start:end] = " " * (end - start)
    return "".join(value)


def ground_role_filter(
    context: EditContext,
    text: str,
    card_or_op: Any,
    filled: Mapping[str, Any],
    *,
    typed: str | None = None,
) -> dict[str, Any] | Clause:
    """Ground one literal equality before drafting; preserve and check typed DAX exactly.

    An unchanged mapping means the request needs the existing checked fallback. A Clause
    is terminal clarification. Call again after drafting to validate any present filter.
    ``typed`` must be the user's extracted body, never a generated draft.
    """
    result = dict(filled)
    if _op(card_or_op) not in _ROLE_OPS:
        return result
    if typed is None:
        typed = _typed_role_body(text)
    if typed is not None:
        problems = validate_role_expression(typed, context, table=result.get("table"))
        if problems:
            return _ask(text, "The role filter you typed needs correction: " + problems[0])
        result["filter"] = typed
        return result
    quoted_spans = _quoted_spans(text)
    scope = next((m for m in _SEES.finditer(text) if _outside_quotes(m, quoted_spans)), None)
    if scope is not None:
        tail = text[scope.end() :].strip()
        quoted_values = _quoted_spans(tail)
        prose = list(tail)
        for start, end in quoted_values:
            prose[start:end] = " " * (end - start)
        complex_scope = _COMPLEX.search("".join(prose)) is not None
        matches: list[tuple[str, str, re.Match[str]]] = []
        for table, columns in context.tables.items():
            for column in columns:
                found = [
                    m
                    for form in _field_forms(column)
                    for m in re.finditer(form, tail, re.I)
                    if _outside_quotes(m, quoted_values)
                ]
                if found:
                    matches.append((table, column, max(found, key=lambda m: m.end() - m.start())))
        named_tables = [
            table
            for table in context.tables
            if any(
                _outside_quotes(m, quoted_values)
                for m in re.finditer(r"(?<!\w)" + re.escape(table) + r"(?!\w)", tail, re.I)
            )
        ]
        if named_tables:
            matches = [item for item in matches if item[0] in named_tables]
        if len(matches) > 1 and not complex_scope:
            return _ask(text, "Which table and column should identify the role's allowed value?")
        if len(matches) == 1 and not complex_scope:
            table, column, mention = matches[0]
            remainder = _role_literal_text(tail, mention, table if table in named_tables else None)
            remainder = re.sub(r"^\s*(?:on|in)\s+", "", remainder, flags=re.I)
            literal = _literal(remainder)
            dtype = context.column_types.get(f"{table}.{column}", "").casefold()
            if literal is not None and dtype in {"", "string", "text", "object"}:
                owner = "'" + table.replace("'", "''") + "'"
                field = column.replace("]", "]]")
                value = '"' + literal.replace('"', '""') + '"'
                result.update(table=table, filter=f"{owner}[{field}] = {value}")
                return result
        elif not matches and not complex_scope:
            return _ask(text, "Which column identifies the values this role should see?")
    expression = result.get("filter")
    if isinstance(expression, str) and expression.strip():
        problems = validate_role_expression(expression, context, table=result.get("table"))
        if problems:
            return _ask(text, "The role filter needs correction: " + problems[0])
    return result


def _group_choices(groups: list[VisualEntry]) -> tuple[Choice, ...]:
    return tuple(Choice(v.label, fill={"group": v.id}) for v in groups[:6])


def ground_group_targets(
    context: EditContext,
    text: str,
    card_or_op: Any,
    filled: Mapping[str, Any],
) -> dict[str, Any] | Clause:
    """A group-only slot cannot name a leaf; a requested subset is never chosen by the AI.

    Can run before model selection with an empty mapping to clarify missing targets.
    Existing non-group operations return unchanged mappings.
    """
    result = dict(filled)
    operation = _op(card_or_op)
    if operation not in _GROUP_ONLY | {"group_visuals"}:
        return result
    if operation in _GROUP_ONLY:
        local = text.split(" to ", 1)[0] if operation == "set_group" else text
        groups = [
            v
            for v in context.visuals
            if v.is_group
            and (
                context.active_page is None
                or v.page_id == context.active_page
                or _name_in(local, v.page_name)
            )
        ]
        mentioned = _named_entries(local, context.visuals)
        named = [v for v in groups if v in mentioned or _name_in(local, v.id)]
        leaves = [v for v in mentioned if not v.is_group]
        selected = context.visual(context.selection) if context.selection else None
        if (
            not named
            and not leaves
            and selected is not None
            and selected.is_group
            and selected in groups
            and re.search(r"\b(?:this|that|selected|it|group)\b", local, re.I)
        ):
            named = [selected]
        if (
            not named
            and not leaves
            and len(groups) == 1
            and re.search(r"\b(?:the|this|that)\s+group\b", local, re.I)
        ):
            named = groups
        if len(named) != 1:
            return _ask(
                text,
                "Which existing visual group should I change? Select or name a group; "
                "a card or chart is not a group.",
                _group_choices(named or groups),
            )
        result["group"] = named[0].id
        return result

    resolution = resolve_visuals(text, context, include_hidden=True)
    quantity_text = text
    for entry in context.visuals:
        if entry.caption:
            quantity_text = re.sub(re.escape(entry.caption), " ", quantity_text, flags=re.I)
    quantity = re.search(
        r"\b(two|three|four|five|six|both|pair|couple|[2-9])\s+(?:of\s+)?(?:the\s+)?"
        r"(?:cards?|charts?|visuals?|groups?)\b",
        quantity_text,
        re.I,
    )
    count = None
    if quantity is not None:
        count_word = quantity.group(1).casefold()
        count = _COUNTS[count_word] if count_word in _COUNTS else int(count_word)
    # Explicitly named groups may be combined with leaves or other groups.
    named = _named_entries(text, context.visuals)
    if len(named) >= 2:
        entries = named
    elif resolution.ok and resolution.explicit and resolution.how != "selection":
        entries = resolution.entries
    else:
        entries = []
    cues = find_types(text)
    types = {kind for cue in cues if isinstance(cue.groups, frozenset | set) for kind in cue.groups}
    candidates = [v for v in resolution.pool if not types or v.visual_type in types]
    if (
        len(named) < 2
        and len(candidates) > 2
        and re.search(
            r"\b(?:some|several|a\s+few)\s+(?:of\s+)?(?:the\s+)?"
            r"(?:cards?|charts?|visuals?|groups?)\b",
            quantity_text,
            re.I,
        )
    ):
        return _ask(text, "Which two or more visuals should I group? Name the visuals.")
    scoped = resolution.how in {"position", "hue"} or len(named) >= 2
    if (
        count
        and len(entries) == count
        and re.search(
            r"\bthe\s+(?:two|three|four|five|six|[2-9])\s+(?:cards?|charts?|visuals?)\b",
            quantity_text,
            re.I,
        )
    ):
        rows: dict[float, list[VisualEntry]] = {}
        for entry in candidates:
            rows.setdefault(round(entry.y, 1), []).append(entry)
        fitting = [row for row in rows.values() if len(row) == count]
        scoped = scoped or (
            len(fitting) == 1 and {v.id for v in fitting[0]} == {v.id for v in entries}
        )
    if count and (len(entries) != count or (len(candidates) > count and not scoped)):
        return _ask(text, f"Which {count} visuals should I group? Name them or select them.")
    if len(entries) < 2 or (resolution.ambiguous and len(named) < 2):
        return _ask(text, "Which two or more visuals should I group? Name the visuals.")
    if len({v.page_id for v in entries}) != 1:
        return _ask(
            text, "A visual group is on one page. Which visuals on the same page should I group?"
        )
    result["visuals"] = list(dict.fromkeys(v.id for v in entries))
    return result


__all__ = ["ground_group_targets", "ground_role_filter", "validate_role_expression"]
