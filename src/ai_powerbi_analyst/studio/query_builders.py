"""Exact DAX for data questions chat's planner has no template for.

Live on the Agents Performance report (2026-09-27), H09 ("how many stores does each
store type have, and how much did each type sell"), H10 ("how many stores closed because
of relocation") and BRE-07 ("how many stores are closed, why, and what did they sell")
went to the 4B model: it has no numbers, and it claimed FactSales could not reach
CloseReason. A query the model wrote itself took up to 142 s on the CPU and filtered
Status = CloseReason. In the DAX Lab, H17 ("average sales amount per transaction by
year") divided by quantity and DEV-07 (year-over-year % by year) returned nothing.

Each builder reads the question against the report's own tables, values and measures,
and writes one query for Studio's DAX engine. It declines (None) when a word is not read,
a value or column is ambiguous, or the question needs more than it does. A wrong but
confident number is worse than no computed answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .chat_context import SYNONYMS, name_words, sensitive_column
from .data_answers import (
    _COMMON_VALUES,
    _CONNECTIVES,
    _HIGH,
    _LOW,
    _QUESTION_WORDS,
    _TABLE_PREFIXES,
    _measure_core,
    _reference,
    _unmapped_words,
    _without_injection,
)
from .dax_engine import EngineColumn, EngineTable, Model
from .report_engine import measure_fact_table


@dataclass(frozen=True)
class BuiltQuery:
    dax: str
    notes: tuple[str, ...] = ()
    # ("stores", "store") when the rows are a list of them: the answer counts them.
    listed: tuple[str, str] = ("", "")


def build_query(message: str, model: Model) -> BuiltQuery | None:
    """The first builder that reads the whole question, or None."""
    for builder in (entity_count_query, year_over_year_query, ratio_query):
        built = builder(message, model)
        if built is not None:
            return built
    return None


# --- shared -----------------------------------------------------------------------------

# Words a question may use around what the builders read.
_CHATTER = frozenset(
    name_words(
        "anymore longer because due reason there exist exists existing altogether currently "
        "still now ones one different distinct unique separate respectively overall "
        "write dax query formula measure shows show return returns calculate calculates "
        "compute computes give get want need please thanks thank okay"
    )
)
# "over all the years in the data": no period, all of it.
_ALL_DATA = re.compile(
    r"\b(?:(?:over|across|in|for|of)\s+)?all\s+(?:the\s+)?(?:years?|time|dates?|periods?)"
    r"(?:\s+(?:in|of)\s+the\s+data(?:set)?)?\b|\ball[\s-]time\b|\bin\s+the\s+data(?:set)?\b"
    r"|\bever\b",
    re.I,
)
_DISCUSSION = re.compile(
    r"\b(?:why|explain\w*|should|would|could|recommend\w*|suggest\w*|design|visual|chart|"
    r"dashboard|forecast\w*|predict\w*|if)\b",
    re.I,
)
_SELLING = re.compile(r"\b(?:sell|sells|sold|selling|sales|revenue|turnover)\b", re.I)
# Periods a builder does not read. The question words the planner knows include "month"
# and "year", so "how many stores closed this year" or "sales per transaction this
# month" would otherwise get all-time figures.
_TIME = re.compile(
    r"\b(?:months?|quarters?|weeks?|days?|years?|today|yesterday|tonight|mtd|qtd|ytd|pm|py|"
    r"daily|weekly|monthly|quarterly|yearly|annual\w*|recent\w*|latest|since|before|after|"
    r"during|until|till|ago|q[1-4]|january|february|march|april|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
    re.I,
)


def _string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _measure_ref(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _label(text: str) -> str:
    return text.replace('"', "").strip()


def _visible_values(column: EngineColumn, limit: int = 100) -> list[str] | None:
    """Distinct non-blank text values of a column, or None when there are too many."""
    values: dict[str, None] = {}
    for value in column.values:
        if isinstance(value, str) and value.strip():
            values.setdefault(value, None)
            if len(values) > limit:
                return None
    return list(values)


def _words(text: str) -> set[str]:
    words = name_words(text)
    return set(words) | {synonym for word in words for synonym in SYNONYMS.get(word, ())}


def _table_words(table: EngineTable) -> set[str]:
    return {w for w in name_words(table.name) if w not in _TABLE_PREFIXES}


def _filters_fact(model: Model, dimension: str, fact: str) -> bool:
    """The dimension's filters reach the fact table (many-to-one steps from the fact)."""
    seen, stack = {fact}, [fact]
    while stack:
        current = stack.pop()
        if current == dimension:
            return True
        for rel in model.upstream.get(current, []):
            if rel.active and rel.one not in seen:
                seen.add(rel.one)
                stack.append(rel.one)
    return False


def _plain_measures(model: Model) -> dict[str, str]:
    """{measure name: fact table key} for measures that aggregate one table."""
    found: dict[str, str] = {}
    for measure in model.measures.values():
        if measure.name.startswith("_"):
            continue
        table = measure_fact_table(model, measure.name)
        if table:
            found[measure.name] = table.casefold()
    return found


def _named_measure(text: str, model: Model, *, exclude: set[str] | None = None) -> str | None:
    """The plain measure whose distinctive words the text names; ties decline."""
    asked = _words(text)
    typed = {w.casefold() for w in re.findall(r"\b[A-Za-z]{2,6}\b", text)}
    scored: list[tuple[int, str]] = []
    for name in _plain_measures(model):
        if exclude and name in exclude:
            continue
        words, acronyms = _measure_core(name)
        if words and all(w in asked for w in words) and acronyms <= typed:
            scored.append((len(words) + len(acronyms), name))
    if not scored:
        return None
    best = max(score for score, _ in scored)
    top = [name for score, name in scored if score == best]
    return top[0] if len(top) == 1 else None


def _base_measure(model: Model) -> str | None:
    """The plain measure most others build on ([Total Sales]), if it is clear."""
    plain = _plain_measures(model)
    counts = {
        name: sum(
            1
            for other in model.measures.values()
            if re.search(re.escape(_measure_ref(name)), other.text, re.I)
        )
        for name in plain
    }
    ranked = sorted(counts.items(), key=lambda item: -item[1])
    if not ranked or ranked[0][1] < 2 or (len(ranked) > 1 and ranked[1][1] == ranked[0][1]):
        return None
    return ranked[0][0]


def _date_table(model: Model) -> str | None:
    """The table the facts' date keys join (the report's calendar), if there is one."""
    tables = {table for table, _ in model.date_keys}
    return next(iter(tables)) if len(tables) == 1 else None


def _date_group(model: Model, word: str) -> tuple[list[str], str] | None:
    """(group references, ORDER BY reference) for "by year/quarter/month"."""
    table_key = _date_table(model)
    if table_key is None:
        return None
    table = model.tables[table_key]
    wanted = {
        "year": ("Year", "Year"),
        "quarter": ("Year Quarter", "Year Quarter Number"),
        "month": ("Year Month", "Year Month Number"),
    }.get(word)
    if wanted is None:
        return None
    shown, order = (table.columns.get(n.casefold()) for n in wanted)
    if shown is None:
        return None
    refs = [_reference(table.name, shown.name)]
    if order is not None and order is not shown:
        refs.insert(0, _reference(table.name, order.name))
    return refs, refs[0]


def _dimension_group(model: Model, fact: str, words: set[str]) -> str | None:
    """The one column of a dimension the fact reaches that the words name."""
    found: set[str] = set()
    named: set[str] = set()  # "by store": the table's name column
    for key, table in model.tables.items():
        if key == fact or key == _date_table(model) or not _filters_fact(model, key, fact):
            continue
        own = _table_words(table)
        for column in table.columns.values():
            if column.kind != "string" or sensitive_column(table.name, column.name):
                continue
            core = set(name_words(column.name)) - _CONNECTIVES
            distinctive = core - own
            if distinctive and distinctive <= words:
                found.add(_reference(table.name, column.name))
            elif not distinctive and own <= words and "name" in name_words(column.name):
                named.add(_reference(table.name, column.name))
    chosen = found or named
    return next(iter(chosen)) if len(chosen) == 1 else None


# --- counts of a dimension's rows --------------------------------------------------------

_COUNT = re.compile(r"\b(?:how\s+many|number\s+of|count\s+(?:of|the))\b", re.I)
# "Which stores are not active anymore?": the rows themselves.
_LIST = re.compile(r"^\s*(?:which|list|name|show(?:\s+me)?)\b", re.I)
# "…, and which ones?" after a count lists them too.
_WHICH_ONES = re.compile(
    r"\b(?:which\s+ones|which\s+of\s+them|(?:list|name|show)\s+(?:me\s+)?them)\b", re.I
)
_RANKING = re.compile(rf"\b(?:{_HIGH}|{_LOW}|strongest)\b", re.I)
_OFF_WORDS = re.compile(
    r"\b(?:closed|close|shut(?:\s+down)?|inactive|discontinued|deactivated|disabled|"
    r"(?:not|no\s+longer)\s+(?:active|open|operating|running)(?:\s+anymore)?)\b",
    re.I,
)
_ON_WORDS = re.compile(r"\b(?:open|active|operating|running|enabled)\b", re.I)
_OFF_VALUES = frozenset(
    {"off", "inactive", "closed", "close", "no", "n", "false", "0", "disabled", "discontinued"}
)
_ON_VALUES = frozenset({"on", "active", "open", "yes", "y", "true", "1", "enabled"})
_STATE_WORDS = frozenset(
    name_words(
        "closed close shut down inactive discontinued deactivated disabled not no longer "
        "active open operating running enabled anymore"
    )
)
_LEAD_WORDS = frozenset({"the", "of", "our", "my", "all", "different", "distinct", "unique"})
_GROUP = re.compile(
    r"\b(?:each|every|per|by|what\s+(?:were|are|was|is)(?:\s+the)?|which)\s+"
    r"((?:[a-z]+\s+){0,2}[a-z]+)",
    re.I,
)


def _clause(message: str) -> tuple[str, str] | None:
    """(the sentence asking for a count or a list and any question after it, the mode)."""
    text, _ = _without_injection(message)
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+|:\s+", text) if p.strip()]
    for index, part in enumerate(parts):
        mode = "count" if _COUNT.search(part) else "list" if _LIST.search(part) else ""
        if mode:
            return " ".join([part, *(p for p in parts[index + 1 :] if p.endswith("?"))]), mode
    return None


def _status(table: EngineTable, off: bool) -> tuple[EngineColumn, str, str] | None:
    """(column, value, other value) of the table's one on/off column for this state."""
    found = []
    for column in table.columns.values():
        if column.kind != "string":
            continue
        values = _visible_values(column, limit=2)
        if not values or len(values) != 2:
            continue
        folded = {v.casefold(): v for v in values}
        side = _OFF_VALUES if off else _ON_VALUES
        other_side = _ON_VALUES if off else _OFF_VALUES
        picked = [v for k, v in folded.items() if k in side]
        others = [v for k, v in folded.items() if k in other_side]
        if len(picked) == 1 and len(others) == 1:
            found.append((column, picked[0], others[0]))
    return found[0] if len(found) == 1 else None


def _name_columns(table: EngineTable) -> list[EngineColumn]:
    """The columns that name a row: StoreName, or FirstName and LastName."""
    named = [
        c
        for c in table.columns.values()
        if c.kind == "string"
        and "name" in name_words(c.name)
        and not sensitive_column(table.name, c.name)
    ]
    if len(named) == 1:
        return named
    words = {c.name: set(name_words(c.name)) for c in named}
    people = [c for c in named if words[c.name] & {"first", "last"}]
    return people if len(people) == 2 else []


def entity_count_query(message: str, model: Model) -> BuiltQuery | None:
    """ "How many stores are closed, why, and what did they sell?" as one query; "which
    stores closed?" lists them."""
    found = _clause(message)
    if found is None:
        return None
    clause, mode = found
    if len(clause) > 400 or _DISCUSSION.search(clause) or _RANKING.search(clause):
        return None
    clause = _ALL_DATA.sub(" ", clause)
    if re.search(r"\d", clause) or _TIME.search(clause):
        return None  # a period, threshold or name the builder does not read
    lead = (_COUNT if mode == "count" else _LIST).search(clause)
    after = clause[lead.end() :]  # type: ignore[union-attr]
    # The counted rows: the first noun after "how many" that names a dimension table;
    # values ("Reseller") and states ("closed") may come before it.
    facts = set(_plain_measures(model).values())
    entity_key = None
    said = ""
    for token in re.findall(r"[A-Za-z]+", after)[:4]:
        words = _words(token)
        matches = [
            key
            for key, table in model.tables.items()
            if key not in facts
            and not _is_calendar(model, key)
            and _table_words(table)
            and _table_words(table) <= words
        ]
        if len(matches) > 1:
            return None
        if matches:
            entity_key, said = matches[0], token
            break
        if not (words <= _STATE_WORDS | _LEAD_WORDS or token[:1].isupper()):
            return None
    if entity_key is None:
        return None
    entity = model.tables[entity_key]
    entity_words = _table_words(entity)
    listing = mode == "list" or bool(_WHICH_ONES.search(clause))
    known = set(_QUESTION_WORDS) | _CHATTER | _words(said) | entity_words
    notes: list[str] = []
    filters: list[tuple[str, str]] = []  # (reference, DAX literal)

    # A state ("closed", "not active anymore"): the table's one On/Off column. Column
    # names ("close reason") are not states.
    state_text = clause
    for column in entity.columns.values():
        spaced = r"\s*".join(re.escape(w) for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+", column.name))
        if spaced:
            state_text = re.sub(rf"\b{spaced}\b", " ", state_text, flags=re.I)
    status_column = None
    off = _OFF_WORDS.search(state_text)
    on = _ON_WORDS.search(_OFF_WORDS.sub(" ", state_text))
    if off and on:
        return None
    if off or on:
        state = _status(entity, off=bool(off))
        if state is None:
            return None
        status_column, value, other = state
        reference = _reference(entity.name, status_column.name)
        filters.append((reference, _string(value)))
        word = (off or on)[0].strip()  # type: ignore[index]
        notes.append(
            f"“{word}” is read as {reference} = {_string(value)} (the other value is "
            f"{_string(other)})."
        )
        known |= _STATE_WORDS

    # Named values of the entity's own columns ("because of relocation").
    places: dict[str, list[EngineColumn]] = {}
    for column in entity.columns.values():
        if column is status_column or sensitive_column(entity.name, column.name):
            continue
        for value in _visible_values(column) or []:
            key = value.casefold()
            if len(value) < 3 or key in _COMMON_VALUES or set(name_words(value)) <= entity_words:
                continue
            if re.search(r"(?<![\w-])" + re.escape(value) + r"s?(?![\w-])", clause, re.I):
                places.setdefault(value, []).append(column)
    for value, columns in places.items():
        if len(columns) != 1:
            return None
        filters.append((_reference(entity.name, columns[0].name), _string(value)))
        # "a close reason of relocation" names the column too.
        known |= set(name_words(value)) | set(name_words(columns[0].name))

    # A breakdown: "each store type", "what were the reasons".
    groups: set[str] = set()
    group_column = None
    for match in _GROUP.finditer(clause):
        phrase = _words(match[1])
        for column in entity.columns.values():
            if column.kind not in {"string", "boolean"} or sensitive_column(
                entity.name, column.name
            ):
                continue
            distinctive = set(name_words(column.name)) - _CONNECTIVES - entity_words
            if distinctive & phrase and not distinctive <= _STATE_WORDS:
                groups.add(column.name)
                group_column = column
    if len(groups) > 1 or (group_column is not None and listing):
        return None
    if group_column is not None:
        known |= set(name_words(group_column.name))
        if any(ref == _reference(entity.name, group_column.name) for ref, _ in filters):
            return None

    # What they sold: the report's base measure, or a measure the clause names.
    measure = _named_measure(clause, model)
    if measure is None and _SELLING.search(clause):
        measure = _base_measure(model)
        if measure is None:
            return None
    if measure is not None:
        fact = _plain_measures(model).get(measure)
        if fact is None or not _filters_fact(model, entity_key, fact):
            return None
        known |= set(name_words(measure)) | {"sell", "sold", "selling", "sale", "revenue"}
    if _unmapped_words(clause, known):
        return None

    count_label = _label(said[:1].upper() + said[1:])
    table_ref = "'" + entity.name.replace("'", "''") + "'"
    condition = ", ".join(f"{ref} = {value}" for ref, value in filters)
    listed = ("", "")
    if listing:
        names = _name_columns(entity)
        if not names or not filters:
            return None  # every store, or rows with no name, is no list to show
        refs = [_reference(entity.name, c.name) for c in names]
        rows = (
            f"VALUES({refs[0]})" if len(refs) == 1 else f"SUMMARIZE({table_ref}, {', '.join(refs)})"
        )
        table = f"CALCULATETABLE({rows}, {condition})"
        if measure is not None:
            table = f'ADDCOLUMNS({table}, "{_label(measure)}", {_measure_ref(measure)})'
        dax = f"EVALUATE {table} ORDER BY {', '.join(refs)}"
        listed = (said.casefold(), name_words(said)[0])
    elif group_column is not None:
        parts = [_reference(entity.name, group_column.name)]
        parts += [f"FILTER(ALL({ref}), {ref} = {value})" for ref, value in filters]
        parts += [f'"{count_label}"', f"COUNTROWS({table_ref})"]
        if measure is not None:
            parts += [f'"{_label(measure)}"', _measure_ref(measure)]
        dax = f"EVALUATE SUMMARIZECOLUMNS({', '.join(parts)}) ORDER BY [{count_label}] DESC"
    else:

        def filtered(expression: str) -> str:
            return f"CALCULATE({expression}, {condition})" if condition else expression

        parts = [f'"{count_label}"', filtered(f"COUNTROWS({table_ref})")]
        if measure is not None:
            parts += [f'"{_label(measure)}"', filtered(_measure_ref(measure))]
        dax = f"EVALUATE ROW({', '.join(parts)})"
    if not set(name_words(said)) <= entity_words:
        # "agents" are DimEmployee rows: every employee, not only those who sold.
        notes.append(f"{count_label} are counted as rows of {entity.name}.")
    if measure is not None:
        notes.append(f"{measure} covers all dates in the data.")
    return BuiltQuery(dax, tuple(notes), listed)


def _is_calendar(model: Model, key: str) -> bool:
    """The report's calendar, or another table of dates (DateAutoTemplate)."""
    if key == _date_table(model):
        return True
    names = {w for c in model.tables[key].columns.values() for w in name_words(c.name)[:1]}
    return {"date", "year", "month"} <= names


# --- year over year ----------------------------------------------------------------------

_YOY = re.compile(
    r"\b(?:year[\s-]*over[\s-]*year|yoy|y/y|(?:vs\.?|versus|compared\s+(?:to|with))\s+"
    r"(?:the\s+)?(?:previous|prior|last)\s+year)\b",
    re.I,
)
_REF = re.compile(r"'((?:[^']|'')+)'\s*\[([^\]]+)\]|\b([A-Za-z_]\w*)\[([^\]]+)\]")


def year_over_year_query(message: str, model: Model) -> BuiltQuery | None:
    """Sales and its year-over-year % by calendar year, from the report's own YOY %
    measure when it has one (DEV-07)."""
    text, _ = _without_injection(message)
    text = re.sub(r",?\s*\bso (?:that )?(?:i|we) (?:can|could)\b.*$", "", text, flags=re.I | re.S)
    if not _YOY.search(text) or _DISCUSSION.search(text) or len(text) > 400:
        return None
    date_key = _date_table(model)
    if date_key is None:
        return None
    calendar = model.tables[date_key]
    year = calendar.columns.get("year")
    dates = next((calendar.columns[c].name for t, c in model.date_keys if t == date_key), None)
    if year is None or dates is None:
        return None
    # A column reference other than the calendar year is a different breakdown.
    for match in _REF.finditer(text):
        table, column = (match[1] or match[3]), (match[2] or match[4])
        if table.replace("''", "'").casefold() != calendar.name.casefold() or (
            column.casefold() != "year"
        ):
            return None
    plain = _REF.sub(" ", text)
    if re.search(r"\b(?:by|per|each|for\s+each)\s+(?!(?:calendar\s+)?years?\b)[a-z]+", plain, re.I):
        return None
    if re.search(r"\d", _YEAR_WORDS.sub(" ", plain)):
        return None
    # Year over year is by calendar year; "this month" or "in Q4" asks for another grain.
    unread = re.sub(
        r"\b(?:by|per|each|for\s+each|every)\s+(?:calendar\s+)?years?\b",
        " ",
        _YOY.sub(" ", _YEAR_WORDS.sub(" ", plain)),
        flags=re.I,
    )
    if _TIME.search(unread):
        return None
    base = _named_measure(_YOY.sub(" ", plain), model) or (
        _base_measure(model) if _SELLING.search(plain) else None
    )
    if base is None:
        return None
    known = (
        set(_QUESTION_WORDS)
        | _CHATTER
        | set(name_words(base))
        | set(name_words("year over yoy percent percentage change growth calendar its by"))
    )
    if _unmapped_words(_YOY.sub(" ", plain), known):
        return None
    year_ref = _reference(calendar.name, year.name)
    yoy = next(
        (
            m.name
            for m in model.measures.values()
            if re.fullmatch(r"yoy\s*%\s*" + re.escape(base), m.name, re.I)
        ),
        None,
    )
    label = _label(base)
    if yoy is not None:
        dax = (
            f'EVALUATE SUMMARIZECOLUMNS({year_ref}, "{label}", {_measure_ref(base)}, '
            f'"{_label(yoy)}", {_measure_ref(yoy)}) ORDER BY {year_ref}'
        )
        return BuiltQuery(
            dax,
            (
                f"Uses the report's own [{yoy}] measure; the first year has no previous "
                "year, so its change is blank.",
            ),
        )
    previous = (
        f"CALCULATE({_measure_ref(base)}, SAMEPERIODLASTYEAR({_reference(calendar.name, dates)}))"
    )
    dax = (
        f'EVALUATE ADDCOLUMNS(FILTER(ADDCOLUMNS(VALUES({year_ref}), "This Year", '
        f'{_measure_ref(base)}, "Previous Year", {previous}), NOT ISBLANK([This Year])), '
        f'"YoY %", DIVIDE([This Year] - [Previous Year], [Previous Year])) ORDER BY {year_ref}'
    )
    return BuiltQuery(dax, ("The first year has no previous year, so its change is blank.",))


_YEAR_WORDS = re.compile(r"\b(?:19|20)\d{2}\b")


# --- ratios: "per transaction", "returned units divided by sold units" ------------------

_PER = re.compile(r"\bper\s+([a-z]+)\b", re.I)
_DIVIDED = re.compile(r"\bdivided\s+by\b", re.I)
_BY = re.compile(r"\bby\s+([a-z]+)(?:\s+([a-z]+))?", re.I)
_SELLING_WORDS = frozenset({"sold", "sale", "sell", "selling"})


def _stem(text: str) -> str:
    """ "returned units" -> "return units": measure names use the plain verb."""
    return re.sub(r"\b([a-z]{4,}?)ed\b", r"\1", text, flags=re.I)


def _divisor(text: str, plain: dict[str, str]) -> str | None:
    """The one plain measure the words name as what is divided by ("transaction",
    "sold units"); "per channel" names none, so it is a breakdown, not a ratio."""
    asked = _words(_stem(text))
    found = []
    for name in plain:
        words, acronyms = _measure_core(name)
        core = words - _SELLING_WORDS or words  # "per unit" is [Quantity Sold]
        if words and not acronyms and core <= asked:
            found.append((len(words & asked), name))
    if not found:
        return None
    best = max(score for score, _ in found)
    top = [name for score, name in found if score == best]
    return top[0] if len(top) == 1 else None


def _breakdown(model: Model, fact: str, by: re.Match[str]) -> tuple[list[str], str, str] | None:
    """(group references, ORDER BY, the words read) for "by year" / "by store type"."""
    phrases = [f"{by[1]} {by[2]}", by[1]] if by[2] else [by[1]]
    for phrase in phrases:
        words = name_words(phrase)
        date_group = _date_group(model, words[-1]) if len(words) == 1 else None
        if date_group is not None:
            return date_group[0], date_group[1], f"by {phrase}"
        column = _dimension_group(model, fact, _words(phrase))
        if column is not None:
            return [column], "", f"by {phrase}"
    return None


def ratio_query(message: str, model: Model) -> BuiltQuery | None:
    """ "Average sales amount per transaction by year" or "return rate by channel:
    returned units divided by sold units": DIVIDE of two report measures (H17's model
    formula divided by quantity sold; H18's dropped the channel breakdown)."""
    text, _ = _without_injection(message)
    if _DISCUSSION.search(text) or len(text) > 300:
        return None
    plain = _plain_measures(model)
    pers = _PER.findall(text)
    divided = _DIVIDED.search(text)
    if len(pers) + (1 if divided else 0) != 1:
        return None
    bys = [
        m
        for m in _BY.finditer(text)
        if not text[: m.start()].rstrip().casefold().endswith("divided")
    ]
    if len(bys) > 1:
        return None  # two breakdowns
    without_by = text if not bys else text[: bys[0].start()] + " " + text[bys[0].end() :]
    if divided:
        segment = next(p for p in re.split(r"[:;,]", without_by) if _DIVIDED.search(p))
        top_text, bottom_text = _DIVIDED.split(segment, maxsplit=1)
        divisor = _divisor(bottom_text, plain)
    else:
        divisor = _divisor(pers[0], plain)
        top_text = _PER.sub(" ", without_by)
    if divisor is None:
        return None
    numerator = _named_measure(_YEAR_WORDS.sub(" ", _stem(top_text)), model, exclude={divisor})
    if numerator is None or plain[numerator] != plain[divisor]:
        return None  # two fact tables: not one ratio per row
    groups: list[str] = []
    order = ""
    rest = text
    if bys:
        found = _breakdown(model, plain[divisor], bys[0])
        if found is None:
            return None
        groups, order, read = found
        rest = text.replace(read, " ", 1)
    year = _YEAR_WORDS.findall(rest)
    if len(year) > 1:
        return None
    words_left = _YEAR_WORDS.sub(" ", _PER.sub(" ", _DIVIDED.sub(" ", rest)))
    if _TIME.search(words_left) or re.search(r"\d", words_left):
        return None  # "this month" or "Q4" would get an all-time ratio
    known = (
        set(_QUESTION_WORDS)
        | _CHATTER
        | set(name_words(numerator))
        | set(name_words(divisor))
        | _words(pers[0] if pers else "")
        | set(name_words("average avg mean amount value ratio rate percent percentage in for"))
    )
    # "units" names [Quantity Sold] through its synonym.
    known |= {w for w in name_words(words_left) if set(SYNONYMS.get(w, ())) & known}
    if _unmapped_words(_stem(words_left), known):
        return None
    # "Return rate % by channel: returned units divided by sold units" names its result.
    head = ""
    if divided and ":" in rest:
        named = _YEAR_WORDS.sub(" ", rest.split(":")[0])
        head = " ".join(re.sub(r"\b(?:for|in)\s*$", "", named.strip()).split())
    if divided:
        default = f"{numerator} / {divisor}"
    else:
        default = f"{numerator} per {name_words(pers[0])[0].capitalize()}"
    label = _label(head if 0 < len(head) <= 40 else default)
    value = f"DIVIDE({_measure_ref(numerator)}, {_measure_ref(divisor)})"
    notes = [f"{label} is [{numerator}] divided by [{divisor}]."]
    if year:
        date_key = _date_table(model)
        calendar = model.tables[date_key] if date_key else None
        year_column = calendar.columns.get("year") if calendar else None
        if calendar is None or year_column is None:
            return None
        year_ref = _reference(calendar.name, year_column.name)
        value = f"CALCULATE({value}, {year_ref} = {year[0]})"
    if groups:
        dax = (
            f'EVALUATE SUMMARIZECOLUMNS({", ".join(groups)}, "{label}", {value}) '
            f"ORDER BY {order or f'[{label}] DESC'}"
        )
    else:
        dax = f'EVALUATE ROW("{label}", {value})'
    return BuiltQuery(dax, tuple(notes))


__all__ = [
    "BuiltQuery",
    "build_query",
    "entity_count_query",
    "ratio_query",
    "year_over_year_query",
]
