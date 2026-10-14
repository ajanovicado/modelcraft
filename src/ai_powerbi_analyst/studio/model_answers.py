"""A walkthrough of an imported report's data model, read from the model itself.

DEV-10 (2026-09-27 battery) asked how the tables relate, how sales reach an employee,
why three tables have no relationships and why DateAutoTemplate sits next to Date. The
model left out FactSales -> DimChannel, called the employee link ambiguous and guessed
DateAutoTemplate "simplifies date selection". Every line below is read from the model:
the relationships, the chains they form, which measures and visuals read a table that
has none, which table another copies, and the (Blank) member unmatched keys create.
"""

from __future__ import annotations

import re

from .chat_context import SYNONYMS, name_words, sensitive_column
from .dax_engine import Model, _related_path
from .report_import import ImportedReport

_WALKTHROUGH = re.compile(
    r"\b(?:data\s+model|walk\s+(?:me\s+)?through\s+(?:the\s+)?(?:data\s+)?model|"
    # H13: "How are the tables in this report connected to each other?"
    r"how\s+(?:are|do|does|is)\s+(?:the\s+|these\s+|all\s+(?:the\s+)?|my\s+|our\s+)?"
    r"(?:tables|data)\b[^.?!\n]{0,40}?\b(?:relate|related|connect|connected|joined|join|link|"
    r"linked|fit\s+together)|relationships?\s+between\s+(?:the\s+)?tables|"
    r"(?:what|which)\s+(?:are\s+the\s+)?relationships\b|(?:show|list)\s+(?:me\s+)?(?:all\s+)?"
    r"(?:the\s+)?relationships\b|which\s+tables\s+(?:are\s+)?(?:related|connected|linked|joined)|"
    r"explain\s+(?:the\s+)?(?:data\s+)?model|model\s+structure|star\s+schema|"
    r"(?:not|aren'?t|isn'?t)\s+(?:related|connected|linked)\s+to|"
    # H14: "Why does the Top-Bottom-N table have no relationships?"
    r"\b(?:has|have|with)\s+no\s+(?:relationships?|joins?|links?)\b|\bdisconnected\s+tables?\b|"
    r"\b(?:not|aren'?t|isn'?t)\s+(?:related|connected|linked|joined)\b)",
    re.I,
)
# "Walk me through the data model ... and why is Top-Bottom-N not related" wants it all.
_WHOLE_MODEL = re.compile(
    r"walk\s+(?:me\s+)?through|data\s+model|(?:explain|describe)\s+(?:the\s+)?(?:data\s+)?"
    r"model|model\s+structure|star\s+schema|how\s+(?:are|do)\s+(?:the|all|these)\s+tables",
    re.I,
)
# A question about one table's relationships, not the whole model.
_FOCUS = re.compile(
    r"\b(?:has|have|with)\s+no\s+(?:relationships?|joins?|links?)\b|\bdisconnected\b|"
    r"\b(?:not|aren'?t|isn'?t)\s+(?:related|connected|linked|joined)\b|\bwhy\b",
    re.I,
)


def _column_refs(expression: str, table: str) -> bool:
    quoted = re.escape(table.replace("'", "''"))
    return re.search(rf"(?:'{quoted}'|\b{re.escape(table)})\s*\[", expression, re.I) is not None


def _values_text(model: Model, table: str) -> str:
    data = model.tables[table]
    parts = []
    for column in list(data.columns.values())[:3]:
        values = [
            v for v in dict.fromkeys(column.value(i) for i in range(data.rows)) if v is not None
        ]
        if not values:
            continue
        integers = [v for v in values if isinstance(v, int)]
        if len(integers) == len(values) and values == list(range(min(integers), max(integers) + 1)):
            parts.append(f"{column.name} {min(values)}–{max(values)}")
        elif len(values) <= 6:
            ordered = sorted(values, key=lambda v: (isinstance(v, str), v))
            parts.append(f"{column.name}: " + ", ".join(str(v) for v in ordered))
    return "; ".join(parts)


def _copy_of(model: Model, table: str) -> str | None:
    mine = model.tables[table]
    names = {c.casefold() for c in mine.columns}
    for other in model.tables.values():
        if (
            other is mine
            or other.rows != mine.rows
            or {c.casefold() for c in other.columns} != names
        ):
            continue
        if all(
            mine.columns[c].value(0) == other.columns[c].value(0)
            and mine.columns[c].value(mine.rows - 1) == other.columns[c].value(other.rows - 1)
            for c in mine.columns
        ):
            return other.name
    return None


def model_walkthrough(message: str, report: ImportedReport, model: Model) -> str | None:
    """How the report's tables relate, from its own model; None for other questions."""
    if not _WALKTHROUGH.search(message):
        return None
    lines: list[str] = ["**Relationships** (from the report's model):"]
    for rel in model.relationships:
        many, one = model.tables[rel.many], model.tables[rel.one]
        state = "" if rel.active else " (inactive: used only through USERELATIONSHIP)"
        lines.append(
            f"- {many.name}[{many.columns[rel.many_column].name}] → "
            f"{one.name}[{one.columns[rel.one_column].name}]: many-to-one, "
            f"{'both directions' if rel.both else 'single direction'}{state}"
        )
    related = {r.many for r in model.relationships} | {r.one for r in model.relationships}
    facts = sorted(
        {r.many for r in model.relationships if r.active}
        - {r.one for r in model.relationships if r.active}
    )
    chains = []
    for fact in facts:
        for table in sorted(model.closure(fact) - {fact}):
            path = _related_path(model, fact, table)
            if path and len(path) > 1:
                names = [model.tables[fact].name] + [model.tables[r.one].name for r in path]
                link = model.tables[path[-1].many].columns[path[-1].many_column].name
                one = model.tables[table]
                used = model.tables[path[-1].many]
                keys = {used.columns[path[-1].many_column].value(i) for i in range(used.rows)} - {
                    None
                }
                linked = sum(
                    1 for i in range(one.rows) if one.columns[path[-1].one_column].value(i) in keys
                )
                chains.append(
                    f"- {' → '.join(names)}: rows of {names[0]} reach {one.name} only through "
                    f"{used.name}[{link}] ({linked:,} of the {one.rows:,} {one.name} rows are "
                    f"linked from {used.name})."
                )
    if chains:
        lines += ["", "**Longer paths**:", *chains]
    blanks = []
    for rel in model.relationships:
        unmatched = model.unmatched(rel)
        if rel.active and unmatched.any():
            many, one = model.tables[rel.many], model.tables[rel.one]
            column = many.columns[rel.many_column]
            examples = sorted(
                {column.value(int(i)) for i in unmatched.nonzero()[0]} - {None}, key=str
            )[:3]
            shown = ", ".join(str(e) for e in examples) or "blank"
            count = int(unmatched.sum())
            key = f"{one.name}[{one.columns[rel.one_column].name}]"
            blanks.append(
                f"- {many.name} has {count:,} row{'s' if count != 1 else ''} whose "
                f"{column.name} is missing from {one.name} ({shown}), so Power BI adds a "
                f"(Blank) {one.name} member; measures with NOT ISBLANK({key}) leave it out."
            )
    if blanks:
        lines += ["", "**Unmatched keys**:", *blanks]
    measures = {str(m["Name"]): str(m.get("Expression", "")) for m in report.measures}
    lonely = [t for t in model.tables if t not in related]
    named = [
        key
        for key, table in model.tables.items()
        if re.search(r"(?<![\w-])'?" + re.escape(table.name) + r"'?(?![\w-])", message, re.I)
    ]
    focus = (
        named[0]
        if len(named) == 1 and _FOCUS.search(message) and not _WHOLE_MODEL.search(message)
        else None
    )
    if focus is not None:
        # H14 asked about one table: its own relationships, or why it has none.
        name = model.tables[focus].name
        own = [line for line in lines[1:] if re.search(rf"\b{re.escape(name)}\[", line)]
        if focus not in lonely:
            return "\n".join(
                [f"**{name}** is related to other tables:", *own]
                + (
                    [
                        "",
                        "Tables filter each other only through these relationships; two tables "
                        "with no path between them do not filter each other.",
                    ]
                )
            )
        lines = []
        lonely = [focus]
    elif lonely:
        lines += ["", "**Tables without relationships** (they filter nothing by themselves):"]
    for table in lonely:
        name = model.tables[table].name
        readers = [m for m, text in measures.items() if _column_refs(text, name)]
        visuals = sorted(
            {
                visual.title
                for page in report.pages
                for visual in page.visuals
                if any(
                    str(b.get("table", "")).casefold() == name.casefold() for b in visual.bindings
                )
            }
        )
        values = _values_text(model, table)
        text = f"- {name} ({model.tables[table].rows:,} rows{'; ' + values if values else ''})"
        copy = _copy_of(model, table)
        if copy:
            text += f": the same columns and rows as {copy}, a copy of it"
        if readers or visuals:
            uses = []
            defaults: set[str] = set()
            if readers:
                shown = ", ".join(f"[{m}]" for m in readers[:3])
                more = f" and {len(readers) - 3} more" if len(readers) > 3 else ""
                selected = any("SELECTEDVALUE" in measures[m].upper() for m in readers)
                defaults = {
                    match.group(1)
                    for m in readers
                    for match in re.finditer(
                        rf"SELECTEDVALUE\s*\(\s*'?{re.escape(name)}'?\s*\[[^\]]+\]\s*,\s*"
                        r"([^()]+?)\s*\)",
                        measures[m],
                        re.I,
                    )
                }
                uses.append(
                    f"measures read its selection{' with SELECTEDVALUE' if selected else ''}: "
                    f"{shown}{more}"
                )
            if len(defaults) == 1:
                uses.append(f"default {next(iter(defaults))} when not exactly one is selected")
            if visuals:
                uses.append("visuals: " + ", ".join(visuals[:3]))
            text += " — a parameter table: " + "; ".join(uses) + "."
        else:
            text += ". No measure or visual uses it."
        lines.append(text)
    if focus is not None:
        table = model.tables[focus].name
        reason = (
            "A slicer on it sets a value that measures read, so it needs no relationship: "
            "joined to another table, the slicer would filter that table's rows instead."
            if " — a parameter table" in lines[-1]
            else "Nothing reads it, so it can be removed in Power BI Desktop if no one needs it."
        )
        return "\n".join(
            [
                f"**{table}** has no relationships: no other table joins it, so it filters "
                "nothing by itself.",
                lines[-1],
                "",
                reason,
            ]
        )
    lines += [
        "",
        "_Read from the imported model. The report is read-only here: change relationships "
        "in Power BI Desktop._",
    ]
    return "\n".join(lines)


# Words for the same kind of field: "region" is also answered by Country or Territory.
_FAMILIES = {
    "region": {"region", "country", "territory", "state", "city", "geography", "location"},
    "country": {"region", "country", "territory", "state", "city", "geography", "location"},
    "city": {"region", "country", "territory", "state", "city", "geography", "location"},
    "customer": {"customer", "client", "buyer", "account", "consumer"},
    "client": {"customer", "client", "buyer", "account", "consumer"},
    "supplier": {"supplier", "vendor"},
    "vendor": {"supplier", "vendor"},
    "category": {"category", "subcategory", "class", "group"},
    "brand": {"brand", "manufacturer"},
}
_SLOT = re.compile(
    r"\b(?:by|per|each|which|biggest|largest|top|best|worst|smallest|main|leading)\s+"
    r"(?:\d+\s+)?(?:the\s+|our\s+|my\s+)?([a-z]+)",
    re.I,
)
_KEY_WORDS = frozenset({"key", "id", "code"})


def missing_fields_answer(message: str, report: ImportedReport, model: Model) -> str | None:
    """ "Sales by region, and who is our biggest customer?" when the model has neither.

    BRE-01 went to the model (56-131 s). It said so, but never that DimStore[GeographyKey]
    is a key with no geography table behind it, the only trace of a location. This
    answers only when every field the question groups or ranks by is absent; one that
    exists makes the question partly answerable, which is the model's job.
    """
    nouns: list[str] = []
    for match in _SLOT.finditer(message):
        words = name_words(match[1])
        if words and words[0] not in nouns:
            nouns.append(words[0])
    if not nouns or any(noun not in _FAMILIES for noun in nouns):
        return None
    joined = {(r.many, r.many_column) for r in model.relationships}
    facts = sorted(
        {r.many for r in model.relationships if r.active}
        - {r.one for r in model.relationships if r.active}
    )
    # "Sales by region and channel" is partly answerable: the model says what it can.
    asked = set(name_words(message))
    asked |= {s for w in asked for s in SYNONYMS.get(w, ())}
    for key, table in model.tables.items():
        if key in facts:
            continue
        own = set(name_words(table.name)) - {"dim", "fact", "tbl", "table", "d", "f"}
        if own & asked:
            return None
    lines: list[str] = []
    for noun in nouns:
        family = _FAMILIES[noun] | set(SYNONYMS.get(noun, ()))
        hints = []
        for key, table in model.tables.items():
            if family & set(name_words(table.name)):
                return None  # a Customers table: the field exists under its own name
            for column_key, column in table.columns.items():
                column_words = set(name_words(column.name))
                if not family & column_words:
                    continue
                if not column_words & _KEY_WORDS or (key, column_key) in joined:
                    return None  # a Country column, or a key with its table: it exists
                hints.append(f"{table.name}[{column.name}]")
        if any(family & set(name_words(m.name)) for m in model.measures.values()):
            return None
        if hints:
            lines.append(
                f"- **{noun}**: {', '.join(hints)} is a key with no table joined to it, so it "
                f"cannot be turned into {noun} names."
            )
        else:
            lines.append(f"- **{noun}**: no table or column holds it.")
    for fact in facts:
        keys = [
            c.name
            for c in model.tables[fact].columns.values()
            if set(name_words(c.name)) & _KEY_WORDS
        ]
        if keys:
            lines.append(f"- {model.tables[fact].name} records each row by {', '.join(keys)}.")
    dimensions = []
    for key in sorted({r.one for r in model.relationships if r.active}):
        table = model.tables[key]
        labels = [
            c.name
            for c in table.columns.values()
            if c.kind == "string"
            and not set(name_words(c.name)) & _KEY_WORDS
            and not sensitive_column(table.name, c.name)
        ]
        if key in {t for t, _ in model.date_keys}:
            labels = [c.name for c in table.columns.values() if c.name in {"Year", "Year Month"}]
        labels.sort(key=lambda n: not ({"name", "type"} & set(name_words(n))))
        if labels:
            dimensions.append(f"{table.name} ({', '.join(labels[:3])})")
    subject = " or ".join(f"**{noun}**" for noun in nouns)
    text = [
        f"This report's model has no {subject} field, so "
        f"{'it cannot' if len(nouns) == 1 else 'none of them can'} be computed from it:",
        *lines,
    ]
    if dimensions:
        text += ["", "Breakdowns that exist: " + "; ".join(dimensions) + "."]
    return "\n".join(text)
