"""Answer data questions in chat from evaluated DAX, not from model arithmetic.

Measured 2026-09-24: asked "Which region has the highest return rate, and who manages
it?", the 4B model said return rate needs Profit and that People has no link to Orders,
although its context listed the verified Orders[Region] -> People[Region] join. Chat is
told never to calculate numbers, so without a tool it can only refuse or guess.

Questions that name one existing measure, at most two grouping fields and exact listed
values are answered here: the app writes the DAX, the DAX lab engine evaluates it over
the reviewed snapshot through approved relationships, and the reply shows the numbers
and the query. Everything else (explanations, design, time filters, unknown metrics)
stays with the model.

Imported reports (``plan_imported_answer``) use the author's own measures and joins and
also answer time questions: "this month" is the latest month with data, as the
report's MTD measures show it, never today's month. They decline whenever the metric,
the entity or a named value is ambiguous: a wrong confident number is worse than none.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from .chat_context import (
    SYNONYMS,
    FactDates,
    ModelShape,
    calendar_table,
    fact_dates,
    folded,
    measure_references,
    model_shape,
    name_words,
    period_acronyms,
    person_table,
    sensitive_column,
    usable_relationships,
)
from .contracts import AdvancedDaxProposal, DashboardDraft, DatasetSnapshot, RelationshipProposal
from .dax_format import format_number

_HIGH = r"highest|most|top|largest|biggest|best|greatest|maximum|max|leading"
_LOW = r"lowest|least|fewest|smallest|worst|bottom|minimum|min|weakest"
_ASKS = re.compile(
    r"^\s*(?:which|what|who|how much|how many|show|list|rank|give me|tell me|top)\b|\?\s*$",
    re.I,
)
# Explanations, design help and semantics the planner does not own stay with the model.
_DISCUSSION = re.compile(
    r"\b(?:why|explain|how (?:do|does|did|should|can|could|would|is|are|to)|should|would|"
    r"could|suggest|recommend|design|dashboard|visual|chart|page|formula|dax|measure|"
    r"relationship|join|model|calculate|build|create|add|margin|ratio|growth|change|trend|"
    r"over time|forecast|predict|median|share|percent|percentage|difference|versus|vs|"
    r"compare|compared|correlat\w*|per|each|excluding|except|without|not|"
    r"year|month|quarter|week|day|ytd|mtd|qtd|yoy|since|before|after|between)\b"
    r"|\b(?:19|20)\d{2}\b",
    re.I,
)
_GENERIC_MEASURE_WORDS = {"total", "sum", "count", "distinct", "number", "of", "the", "id", "key"}
_GENERIC_COLUMN_WORDS = {"name", "id", "key", "code"}


@dataclass(frozen=True)
class DataAnswerPlan:
    expression: str
    measure_name: str
    format_string: str
    group_labels: tuple[str, ...]
    direction: Literal["desc", "asc"] | None
    filters: tuple[str, ...]


def _words(text: str) -> list[str]:
    words = []
    for word in re.findall(r"[a-z0-9]+", text.casefold()):
        if word.endswith("ies") and len(word) > 4:
            word = word[:-3] + "y"
        elif word.endswith("s") and len(word) > 3 and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return words


def _quoted(table: str) -> str:
    return "'" + table.replace("'", "''") + "'"


def _reference(table: str, column: str) -> str:
    return f"{_quoted(table)}[{column}]"


def _measure_words(measure: Any) -> set[str]:
    words = set(_words(measure.name)) - _GENERIC_MEASURE_WORDS
    if getattr(measure, "operation", None) == "average":
        words.discard("average")
    return words


def plan_data_answer(
    question: str, snapshot: DatasetSnapshot, draft: DashboardDraft
) -> DataAnswerPlan | None:
    """The DAX that answers ``question`` exactly, or None to leave it to the model."""
    if not _ASKS.search(question) or _DISCUSSION.search(question):
        return None
    asked = set(_words(question))
    tables = {item.table.id: item.table for item in snapshot.tables}
    columns = {(t.id, c.id): c for t in tables.values() for c in t.columns}

    # One existing measure whose distinctive words the question uses, longest first.
    scored = []
    for measure in draft.measure_proposal.measures:
        words = _measure_words(measure)
        if words and words <= asked and measure.table_id in tables:
            scored.append((len(words), measure))
    if not scored:
        return None
    scored.sort(key=lambda item: -item[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    measure = scored[0][1]
    # "Average sales" is not [Total Sales]; only an average measure answers "average".
    if (
        re.search(r"\b(?:average|avg|mean)\b", question, re.I)
        and getattr(measure, "operation", None) != "average"
    ):
        return None
    fact = tables[measure.table_id]
    consumed = _measure_words(measure)

    relationships = [
        item
        for item in draft.semantic_model.relationships
        if item.review_status == "approved"
        and item.is_active
        and not item.blockers
        and item.from_table_id == fact.id
    ]
    reachable = {fact.id} | {item.to_table_id for item in relationships}

    # Named values ("West", "Technology") filter; one value per column, text columns only.
    names = {t.name.casefold() for t in tables.values()} | {
        c.name.casefold() for c in columns.values()
    }
    filters: list[tuple[str, str, str, str]] = []  # table_id, column_id, table, value
    # The fact's own column wins when a dimension repeats it (Orders[Region], People[Region]).
    for table in sorted(tables.values(), key=lambda item: item.id != fact.id):
        if table.id not in reachable:
            continue
        for column in table.columns:
            values = _listed_values(column)
            named = [
                value
                for value in values
                if len(value) > 1
                and value.casefold() not in names
                and re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", question, re.I)
            ]
            if len(named) > 1:
                return None
            if named and all(named[0] != value for _, _, _, value in filters):
                filters.append((table.id, column.id, table.name, named[0]))
    filtered = {(table_id, column_id) for table_id, column_id, _, _ in filters}

    # Grouping fields the question names, excluding those that only restate the metric
    # or carry a named filter value. Name columns beat ID columns for the same entity.
    candidates = []
    for table in tables.values():
        if table.id not in reachable:
            continue
        for column in table.columns:
            if column.data_type.value not in {"string", "boolean"}:
                continue
            tokens = _words(column.name)
            core = [token for token in tokens if token not in _GENERIC_COLUMN_WORDS] or tokens
            if not set(core) <= asked or set(core) <= consumed:
                continue
            if (table.id, column.id) in filtered:
                continue
            rank = (
                len(core),
                table.id == fact.id,
                "name" in tokens,
                not {"id", "key", "code"} & set(tokens),
            )
            candidates.append((rank, table, column, frozenset(core)))
    candidates.sort(key=lambda item: item[0], reverse=True)
    groups: list[tuple[Any, Any]] = []
    covered: set[str] = set()
    for _, table, column, candidate_core in candidates:
        if candidate_core <= covered:
            continue
        groups.append((table, column))
        covered |= candidate_core
    if len(groups) > 2 or len({table.id for table, _ in groups}) > 1:
        return None

    high = re.search(rf"\b(?:{_HIGH})\b", question, re.I)
    low = re.search(rf"\b(?:{_LOW})\b", question, re.I)
    if high and low:
        return None
    direction: Literal["desc", "asc"] | None = "desc" if high else "asc" if low else None
    if not groups and direction:
        return None  # "the highest sales" with nothing to rank is not a single number

    # A small related dimension shows its descriptive fields: West · Anna Andreadi.
    if len(groups) == 1 and groups[0][0].id == fact.id:
        table, column = groups[0]
        dimension = next(
            (
                item
                for item in relationships
                if item.from_column_id == column.id and item.rely_on_referential_integrity
            ),
            None,
        )
        if dimension is not None:
            target = tables[dimension.to_table_id]
            extra = [
                c
                for c in target.columns
                if c.id != dimension.to_column_id
                and c.data_type.value == "string"
                and c.distinct_count == target.row_count
            ]
            if target.row_count <= 100 and extra:
                key = next(c for c in target.columns if c.id == dimension.to_column_id)
                if (target.id, key.id) not in filtered and (target.id, extra[0].id) not in filtered:
                    groups = [(target, key), (target, extra[0])]
    if any((table.id, column.id) in filtered for table, column in groups):
        return None

    value = "[" + measure.name.replace("]", "]]") + "]"
    if filters:
        value = (
            f"CALCULATE({value}, "
            + ", ".join(
                f'{_reference(table, columns[(table_id, column_id)].name)} = "{named}"'
                for table_id, column_id, table, named in filters
            )
            + ")"
        )
    filter_text = tuple(named for _, _, _, named in filters)
    format_string = getattr(measure, "format_string", "") or ""
    if not groups:
        return DataAnswerPlan(value, measure.name, format_string, (), None, filter_text)
    label = measure.name.replace('"', "")
    group_refs = ", ".join(_reference(table.name, column.name) for table, column in groups)
    expression = (
        f'EVALUATE SUMMARIZECOLUMNS({group_refs}, "{label}", {value}) '
        f"ORDER BY [{label}] {'ASC' if direction == 'asc' else 'DESC'}"
    )
    return DataAnswerPlan(
        expression,
        label,
        format_string,
        tuple(column.name for _, column in groups),
        direction,
        filter_text,
    )


def format_value(value: object, format_string: str) -> str:
    if value is None:
        return "(Blank)"
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    # Rounded half away from zero, as Power BI shows it (2,889,921.545 -> .55).
    decimals = re.search(r"\.(0+)", format_string)
    places = "." + decimals.group(1) if decimals else ""
    if "%" in format_string:
        return format_number(value, f"#,##0{places}%")
    if decimals:
        return format_number(value, f"#,##0{places}")
    if format_string:
        return format_number(value, "#,##0")
    if isinstance(value, float) and not value.is_integer():
        return format_number(value, "#,##0.00")
    return f"{value:,}"


def compose_answer(plan: DataAnswerPlan, rows: list[dict[str, object]]) -> str:
    """A short answer with every number taken from the evaluated rows."""
    scope = f" for {' and '.join(plan.filters)}" if plan.filters else ""
    evidence = f"\n\nCalculated from your imported data with:\n```dax\n{plan.expression}\n```"
    if not plan.group_labels:
        value = next(iter(rows[0].values())) if rows else None
        return f"{plan.measure_name}{scope} is **{format_value(value, plan.format_string)}**." + (
            evidence
        )
    if not rows:
        return f"No rows have a {plan.measure_name} value{scope}." + evidence

    def label(row: dict[str, object]) -> str:
        return " · ".join(
            "(Blank)" if row.get(name) is None else str(row.get(name)) for name in plan.group_labels
        )

    def shown(row: dict[str, object]) -> str:
        return format_value(row.get(plan.measure_name), plan.format_string)

    listed = rows[:8]
    ranking = "\n".join(f"- {label(row)}: {shown(row)}" for row in listed)
    more = f"\n- …and {len(rows) - len(listed)} more" if len(rows) > len(listed) else ""
    by = " and ".join(plan.group_labels)
    if plan.direction:
        first = rows[0]
        tied = [row for row in rows if row.get(plan.measure_name) == first.get(plan.measure_name)]
        word = "highest" if plan.direction == "desc" else "lowest"
        if len(tied) > 1:
            lead = f"{', '.join(f'**{label(row)}**' for row in tied)} tie for the {word}"
        else:
            lead = f"**{label(first)}** has the {word}"
        return (
            f"{lead} {plan.measure_name}{scope}: **{shown(first)}**.\n\n"
            f"{plan.measure_name} by {by}:\n{ranking}{more}{evidence}"
        )
    return f"{plan.measure_name}{scope} by {by}:\n{ranking}{more}{evidence}"


def _listed_values(column: Any) -> list[str]:
    from .dax_assistant import _listed_values as listed

    return listed(column)


# --- Imported reports ------------------------------------------------------------------

# Injected instructions are ignored, never obeyed: the sentence carrying them is dropped
# and the question that remains is answered from the data (BRE-05 asked for every
# number as 1,000,000 before asking for total sales).
_INJECTION = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass)\b[^.?!\n]*\b(?:instructions?|rules?|"
    r"prompts?|guidelines?|polic(?:y|ies))\b"
    r"|\byou are now\b|\bact as\b|\bpretend\b"
    r"|\b(?:system|hidden|developer) (?:prompt|message|instructions?)\b"
    r"|\b(?:admin|debug|developer|god|dan)\s+mode\b|\bjailbreak"
    r"|\b(?:answer|reply|respond|report|say|return|output)\b[^.?!\n]*\b(?:every|all|each|any) "
    r"(?:numbers?|figures?|values?|answers?|results?)\b"
    r"|\bevery (?:number|figure|value|answer)\b",
    re.I,
)
# "…so I can check whether older agents sell more" is the purpose, not the question.
_PURPOSE = re.compile(
    r",?\s*\b(?:so (?:that )?(?:i|we) (?:can|could)|in order to|because (?:i|we))\b.*$", re.I | re.S
)
# "Could you show me the top agent?" is the question "show me the top agent?": the polite
# opener is dropped before the discussion words are looked for, since "could" and "would" there
# ask for an answer, not a discussion ("what would sales be if ..." still is one). A request verb
# right after it reads as "show" ("could you name the top agent" -> "show the top agent").
_POLITE_OPENER = re.compile(
    r"^\s*(?:please\s+)?(?:(?:could|would|can|will)\s+you\s+(?:please\s+)?|please\s+)", re.I
)
_REQUEST_VERB = re.compile(r"^(?:name|find|identify|tell\s+me)\b", re.I)


def _plain_question(text: str) -> str:
    """The question inside a polite request; other text unchanged."""
    stripped = _POLITE_OPENER.sub("", text, count=1)
    if stripped == text:
        return text
    return _REQUEST_VERB.sub("show", stripped, count=1)


_IMPORTED_ASKS = re.compile(
    r"\?|^\s*(?:which|what|who|whom|how|hw|show|list|rank|give|tell|top|bottom|compare|for|in|"
    r"during)\b",
    re.I,
)
_IMPORTED_DISCUSSION = re.compile(
    r"\b(?:why|explain\w*|means?|meaning|define|definition|formula|dax|measures?|calculat\w*|"
    r"how (?:do|does|did|should|can|could|would|to)|should|would|could|suggest\w*|recommend\w*|"
    r"design|dashboard|visuals?|charts?|pages?|cards?|slicers?|build|create|add|"
    r"relationships?|joins?|model|median|shares?|percent\w*|ratio|margins?|excluding|except|"
    r"without|not|never|no|none|average|avg|mean|if|whether|forecast\w*|predict\w*|trend\w*)\b",
    re.I,
)
_UNSUPPORTED_TIME = re.compile(
    r"\b(?:quarters?|qtd|weeks?|weekly|days?|daily|today|yesterday|tomorrow|since|before|after|"
    r"between|rolling|trailing|monthly|yearly|annual(?:ly)?|cumulative|running|over time|"
    r"(?:by|per|each|every) (?:month|year)|(?:last|past|next) \d+|morning|tonight)\b",
    re.I,
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# Whole month names or their short forms: "market 2009" is no March.
_MONTH_WORD = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_MONTH_YEAR = re.compile(rf"\b({_MONTH_WORD})\.?\s+((?:19|20)\d{{2}})\b", re.I)
_MONTH_NAME = (
    r"(?:january|february|march|april|june|july|august|september|october|november|december|"
    r"(?-i:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\b\.?)"
)
# Periods the planner cannot filter exactly. Each used to return a number for another
# period: a day became its year or month, "in December" all data, and "the first half
# of 2009" grouped by DimEmployee[FirstName].
_UNSUPPORTED_PERIOD = re.compile(
    # 2009-12-15, 2009/12, 12/15/2009, 15.12.09, 12/2009 and '09: days, months, short years.
    r"\b\d{4}[-/.]\d{1,2}\b|\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b|\b\d{1,2}[-/.](?:19|20)\d{2}\b"
    r"|['’]\d{2}\b|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_NAME}"
    rf"|{_MONTH_NAME}\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
    r"|\b(?:halfs?|halves|h[12]|fiscal|fy\s?\d*|semesters?|seasons?|seasonal|holidays?|"
    r"weekends?|weekdays?|q[1-4]|spring|summer|autumn|winter)\b"
    r"|\bfall\s+(?:of\s+)?(?:19|20)\d{2}\b",
    re.I,
)
_ANY_MONTH = re.compile(rf"\b{_MONTH_NAME}", re.I)
# Period wording _periods reads; any time wording left once it is removed was not
# understood ("the last three months", "a year ago", "the end of 2009", "by date") and
# used to leave the answer over all data or a whole year.
_READ_PERIODS = re.compile(
    rf"\b(?:the\s+)?(?:month\s+of\s+)?{_MONTH_WORD}\.?\s+(?:19|20)\d{{2}}\b"
    r"|\b(?:the\s+)?(?:calendar\s+)?(?:year\s+(?:of\s+)?)?(?:19|20)\d{2}\b"
    r"|\b(?:so\s+far\s+)?(?:this|current)\s+(?:month|year)(?:\s+so\s+far)?\b"
    r"|\b(?:last|previous|prior)\s+(?:month|year)\b"
    r"|\b(?:month|year)[\s-]to[\s-]date\b|\b[mpy]td\b|\bp[my]\b",
    re.I,
)
_UNREAD_TIME = re.compile(
    r"\b(?:months?|years?|yrs?|ago|recent(?:ly)?|lately|latest|earliest|newest|oldest|same|"
    r"through|thru|until|till|so\s+far|to\s+date|as\s+of|end|start|beginning|middle|mid|early|"
    r"late|rest|remainder|period|periods|times?)\b"
    r"|\b(?:by|per|each|every|across)\s+(?:the\s+)?(?:date|dates|day|days)\b",
    re.I,
)
# "2009 vs last year" may mean 2008 or the year before the one named: never guessed.
_RELATIVE_BACK = re.compile(r"\b(?:last|previous|prior)\s+(?:month|year)\b", re.I)
_RELATIVE_WORDS = re.compile(
    r"\b(this|current|last|previous|prior)\s+(month|year)\b|\b(month|year)[\s-]to[\s-]date\b",
    re.I,
)
_NAMED_PERIOD = re.compile(r"\b(?:19|20)\d{2}\b")
# Words that name no column on their own: "first half" is not DimEmployee[FirstName].
_ORDINAL_WORDS = frozenset({"first", "second", "third", "half", "last", "next", "other"})
# Question wording the planner reads itself; any other word must map to the model.
_QUESTION_WORDS = frozenset(
    name_words(
        "what which who whom whose how when where were was are have has had does did doing "
        "done much many show list rank ranked ranking give tell please could would can will "
        "also just only about above below under over across into onto from with within during "
        "this that these those their there they them then than both either some any all "
        "overall total totals combined together altogether whole entire compare compared "
        "comparison versus against highest lowest most least best worst weakest strongest "
        "biggest largest smallest greatest maximum minimum leading lead leads top bottom fewest "
        "more less grew grow grown growing growth increase increased decrease decreased "
        "decline declined drop dropped fell fall change changed rise rose gain gained lost "
        "performance performer performing perform performed sell sold selling sale amount "
        "value number figure result data target goal quota threshold hit reach reached beat "
        "made make exceed exceeded exceeding month year today current previous prior last next "
        "period thousand million one two three four five six seven eight nine ten twelve "
        "me my our us we you your it its see get got know find let give each every per "
        "the and for but nor yet use sum max min vs far"
    )
    + ["january", "february", "march", "april", "june", "july", "august", "september"]
    + ["october", "november", "december", "sept", *_MONTHS]
)
# Growth verdicts: "declined the most" and "grew least" name the lowest change.
_DECLINE = re.compile(
    r"\b(?:declin\w*|decreas\w*|dropp?(?:ed|s)?|fell|fall\w*|shr[aiu]nk\w*|lost|los(?:e|s|ing)\w*)\b",
    re.I,
)
_GROWTH = re.compile(
    r"\b(?:gr[eo]w\w*|growth|increas\w*|chang\w*|improv\w*|ris(?:e|es|en|ing)|rose|gain\w*)\b|"
    + _DECLINE.pattern,
    re.I,
)
_LEAST = re.compile(r"\b(?:least|lowest|smallest|slowest|fewest)\b", re.I)
_WORST = re.compile(r"\b(?:worst|weakest|poorest)\b", re.I)
# Measure-name acronyms that are a period _periods applies to the base measure.
_FOLDED_ACRONYMS = {"mtd": "this month", "ytd": "this year", "py": "last year", "pm": "last month"}
_THRESHOLD = re.compile(
    r"\b(?P<op>more than|over|above|at least|exceed(?:ed|ing|s)?|greater than|hit|hits|"
    r"reach(?:ed|es)?|beat|made|make|below|under|less than|fewer than|at most)\s+"
    r"(?:the\s+|a\s+)?(?:[$€£]\s?)?(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>k|m|thousand|million)?\b",
    re.I,
)
_THRESHOLD_OPS = {
    "more than": "gt",
    "over": "gt",
    "above": "gt",
    "greater than": "gt",
    "below": "lt",
    "under": "lt",
    "less than": "lt",
    "fewer than": "lt",
    "at most": "lte",
}
_OP_TEXT = {"gt": "more than", "gte": "at least", "lt": "less than", "lte": "at most"}
_SLOT = re.compile(
    r"\b(?:by|per|each|every|which|top\s+\d+|bottom\s+\d+|best|worst|weakest|biggest|largest|"
    r"smallest|highest|lowest|strongest)\s+(?:the\s+|our\s+|my\s+|of\s+)?([a-z]+)",
    re.I,
)
_FILLER = frozenset(
    {"one", "of", "is", "are", "wa", "was", "were", "ha", "has", "have", "had", "did", "do"}
    | {"doe", "does", "the", "a", "an", "total", "amount", "number", "value", "performance"}
    | {"performer", "all", "combined", "overall", "in", "for", "and", "sale", "sold", "it"}
)
_TIME_WORDS = frozenset(
    {"year", "month", "quarter", "day", "week", "date", "this", "thi", "last", "previous"}
    | {"prior", "current", "mtd", "ytd", "time", "period", "of", "to"}
)
# A measure named only by question wording ([Rank], [vs Previous Month]) is not a second
# metric the question asks for; [Total Sales] is.
_WORDING = (_QUESTION_WORDS | _TIME_WORDS) - {"sale", "sell", "sold", "selling", "amount", "value"}
_TABLE_PREFIXES = frozenset({"dim", "fact", "tbl", "table", "lkp", "lookup", "d", "f"})
# Column-name words that name nothing on their own.
_CONNECTIVES = frozenset(
    {"name", "id", "key", "code", "with", "of", "the", "and", "for", "per", "to", "in", "by"}
    | {"on", "at", "a", "an", "or", "is", "from"}
)
_COMMON_VALUES = frozenset(
    {"off", "on", "yes", "no", "all", "not", "none", "other", "others", "new", "old", "top"}
    | {"bottom", "total", "and", "the", "true", "false", "unknown", "store"}
)


@dataclass(frozen=True)
class Period:
    label: str
    start: date
    end: date  # inclusive


@dataclass(frozen=True)
class ImportedQuery:
    expression: str  # what the lab evaluates (ISO date strings)
    display: str  # the same query as Power BI DAX (DATE literals)
    period: Period | None


@dataclass(frozen=True)
class ImportedAnswerPlan:
    measure: str
    queries: tuple[ImportedQuery, ...]
    entity: str = ""
    label_columns: tuple[str, ...] = ()
    key_column: str | None = None
    join_labels: str = " · "
    direction: Literal["desc", "asc"] | None = None
    limit: int | None = None
    picks: tuple[str, ...] = ()
    threshold: tuple[str, float] | None = None
    scope: str = ""
    all_data: str = ""
    growth: bool = False
    # "declined the most" / "grew least": the verdict names the lowest change.
    growth_low: bool = False
    leader: ImportedQuery | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    # "[Total Sales] = SUM( FactSales[SalesAmount] )" when the formula is short.
    definition: str = ""

    @property
    def evaluations(self) -> tuple[ImportedQuery, ...]:
        return (*self.queries, *((self.leader,) if self.leader else ()))


def _one_edit(word: str, target: str) -> bool:
    """A typo of ``target``: one edit ("onlin", "chanel"), two from 8 letters ("resseler").

    Optimal string alignment distance, so a swap of neighbours counts once.
    """
    allowed = 2 if min(len(word), len(target)) >= 8 else 1
    if abs(len(word) - len(target)) > allowed:
        return False
    previous = list(range(len(target) + 1))
    before: list[int] = []
    for i, a in enumerate(word, start=1):
        current = [i]
        for j, b in enumerate(target, start=1):
            cost = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b))
            if i > 1 and j > 1 and a == target[j - 2] and word[i - 2] == b:
                cost = min(cost, before[j - 2] + 1)
            current.append(cost)
        before, previous = previous, current
    return previous[-1] <= allowed


def _all_values(column: Any) -> list[str]:
    """Every value of a small text column when the profile saw all of them."""
    if column.data_type.value != "string" or not column.distinct_count_exact:
        return []
    values = [str(v) for v in column.sample_values if isinstance(v, str) and v.strip()]
    if not 1 <= column.distinct_count <= 8 or len(values) < column.distinct_count:
        return []
    return values if all(len(value) <= 32 for value in values) else []


def _measure_core(name: str) -> tuple[set[str], set[str]]:
    """(distinctive words, period acronyms) of a measure name: MTD Total Sales -> sale, mtd."""
    acronyms = {word.casefold() for word in re.findall(r"\b[A-Z]{2,6}\b", name)}
    words = {w for w in name_words(name) if w not in _GENERIC_MEASURE_WORDS} - acronyms
    return words, acronyms


def _matches(word: str, asked: set[str]) -> bool:
    return word in asked or any(synonym in asked for synonym in SYNONYMS.get(word, ()))


def _without_injection(question: str) -> tuple[str, bool]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?:;])\s+|\n+", question) if p.strip()]
    kept = [part for part in parts if not _INJECTION.search(part)]
    return " ".join(kept), len(kept) != len(parts)


def _month_end(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _unmapped_words(text: str, known: set[str] | frozenset[str]) -> list[str]:
    """Content words the plan does not use: capitalised names anywhere but a sentence's
    start ("Nuria", "Seattle", "MAT") and other words of three or more letters ("ana").
    A typo of a known word ("whcih", "chanel") or a word cut short ("sel", "mor") is
    known."""
    unmapped: list[str] = []
    for sentence in re.split(r"(?<=[.!?:;])\s+|\n+", text):
        tokens = re.findall(r"\b[A-Za-z][A-Za-z'’-]*", sentence)
        for index, token in enumerate(tokens):
            named = index > 0 and token[0].isupper()
            for word in name_words(token):
                if word in known or len(word) < (2 if named else 3):
                    continue
                if len(word) >= 5 and any(
                    len(item) >= 4 and _one_edit(word, item) for item in known
                ):
                    continue
                if (
                    not named
                    and len(word) <= 4
                    and any(len(item) == len(word) + 1 and item.startswith(word) for item in known)
                ):
                    continue
                unmapped.append(word)
    return unmapped


def _relative_phrases(text: str) -> set[str]:
    """ "this month", "last year" … as the question says them ("current", "MTD" folded)."""
    return {
        f"this {match[3].casefold()}"
        if match[3]
        else f"{'this' if match[1].casefold() in {'this', 'current'} else 'last'} "
        f"{match[2].casefold()}"
        for match in _RELATIVE_WORDS.finditer(text)
    }


def _unsupported_period(text: str) -> bool:
    """A day, a month without its year, a half, a fiscal period or time wording _periods
    does not read ("the last three months", "a year ago"): never filtered exactly."""
    return bool(
        _UNSUPPORTED_PERIOD.search(text)
        or _ANY_MONTH.search(_MONTH_YEAR.sub(" ", text))
        or _UNREAD_TIME.search(_READ_PERIODS.sub(" ", text))
    )


def _periods(text: str, dates: FactDates) -> list[Period] | None:
    """Named periods in time order, or None when one lies outside the data."""
    last, first = dates.last, dates.first
    found: list[Period] = []
    for match in _MONTH_YEAR.finditer(text):
        start = date(int(match[2]), _MONTHS.index(match[1][:3].casefold()) + 1, 1)
        found.append(Period(f"{start:%B %Y}", start, _month_end(start)))
    rest = _MONTH_YEAR.sub(" ", text)
    for year in dict.fromkeys(int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", rest)):
        found.append(Period(str(year), date(year, 1, 1), date(year, 12, 31)))
    this_month = date(last.year, last.month, 1)
    previous = (this_month - timedelta(days=1)).replace(day=1)
    if re.search(r"\b(?:this|current) month\b|\bmtd\b|\bmonth[\s-]to[\s-]date\b", text, re.I):
        found.append(Period(f"{this_month:%B %Y}", this_month, last))
    if re.search(r"\b(?:last|previous|prior) month\b", text, re.I):
        found.append(Period(f"{previous:%B %Y}", previous, _month_end(previous)))
    if re.search(r"\b(?:this|current) year\b|\bytd\b|\byear[\s-]to[\s-]date\b", text, re.I):
        found.append(Period(str(last.year), date(last.year, 1, 1), last))
    if re.search(r"\b(?:last|previous|prior) year\b", text, re.I):
        found.append(
            Period(str(last.year - 1), date(last.year - 1, 1, 1), date(last.year - 1, 12, 31))
        )
    unique = list({(p.start, p.end): p for p in found}.values())
    if any(p.end < first or p.start > last for p in unique):
        return None
    return sorted(unique, key=lambda p: p.start)


def _relative_note(
    text: str, last: date, implied: str | None, typed: set[str] | frozenset[str] = frozenset()
) -> str:
    """ "“This month” is December 2009 (the latest month with data) …" for relative words.

    A typed acronym is named as read: "“PY” is read as last year, 2008"."""
    this_month = date(last.year, last.month, 1)
    previous = this_month - timedelta(days=1)
    said = []

    def read(acronym: str, words: str, value: str) -> None:
        said.append(
            f"“{acronym.upper()}” is read as {words}, {value}"
            if acronym in typed
            else f"“{words}” is {value}"
        )

    if (
        "mtd" in typed
        or implied == "mtd"
        or re.search(r"\b(?:this|current) month\b|\bmtd\b|\bmonth[\s-]to[\s-]date\b", text, re.I)
    ):
        read("mtd", "this month", f"{last:%B %Y} (the latest month with data)")
    if "pm" in typed or re.search(r"\b(?:last|previous|prior) month\b", text, re.I):
        read("pm", "last month", f"{previous:%B %Y}")
    if "ytd" in typed or re.search(
        r"\b(?:this|current) year\b|\bytd\b|\byear[\s-]to[\s-]date\b", text, re.I
    ):
        read("ytd", "this year", str(last.year))
    if "py" in typed or re.search(r"\b(?:last|previous|prior) year\b", text, re.I):
        read("py", "last year", str(last.year - 1))
    if not said:
        return ""
    joined = re.sub(r"^(\W*)(\w)", lambda m: m[1] + m[2].upper(), " and ".join(said))
    return (
        f"{joined}: the data ends on {last:%d %b %Y}, so relative dates count from there, "
        "not from today."
    )


def _data_dates(shape: ModelShape, table_id: str) -> FactDates | None:
    """The dates periods filter on, or None when they would be about people.

    In an HR-shaped model the measure's table is the employee table: its BirthDate made
    "this month" the latest birthday and filtered hours by it. A personal date column is
    never used, and a person table's other dates (HireDate) only when they join a
    calendar table, as a report's transaction date does.
    """
    dates = fact_dates(shape, table_id)
    if dates is None or sensitive_column(dates.table, dates.column):
        return None
    if person_table(dates.table) and not any(
        item.from_table_id == dates.table_id
        and item.from_column_id == dates.column_id
        and item.to_table_id in shape.tables
        and calendar_table(shape.tables[item.to_table_id])
        for item in usable_relationships(shape.relationships)
    ):
        return None
    return dates


def _reachable(start: str, relationships: list[RelationshipProposal]) -> dict[str, int]:
    """Tables a filter or group can reach from the measure's table, by hop count."""
    usable = usable_relationships(relationships)
    hops = {start: 0}
    queue = [start]
    while queue:
        current = queue.pop(0)
        for item in usable:
            if item.from_table_id == current and item.to_table_id not in hops:
                hops[item.to_table_id] = hops[current] + 1
                queue.append(item.to_table_id)
    return hops


def _date_filters(fact: str, column: str, period: Period) -> tuple[str, str]:
    """(lab, Power BI) filters for a period; the end bound is exclusive for datetimes."""
    after = period.end + timedelta(days=1)
    reference = _reference(fact, column)
    lab = f'{reference} >= "{period.start.isoformat()}", {reference} < "{after.isoformat()}"'
    shown = (
        f"{reference} >= DATE({period.start.year}, {period.start.month}, {period.start.day}), "
        f"{reference} < DATE({after.year}, {after.month}, {after.day})"
    )
    return lab, shown


def plan_imported_answer(
    question: str,
    snapshot: DatasetSnapshot,
    measures: list[AdvancedDaxProposal],
    relationships: list[RelationshipProposal],
    measure_table: Callable[[AdvancedDaxProposal], str | None] | None = None,
) -> ImportedAnswerPlan | None:
    """The DAX that answers an imported report's data question exactly, or None.

    ``measure_table`` names the table a measure aggregates when the lab subset cannot
    run it but Studio's DAX engine can ([Profit] = SUMX(FactSales, ...)).
    """
    from .aggregates import AggregatePlanError
    from .dax import describe_dax

    text, injected = _without_injection(question)
    purpose = _PURPOSE.search(text)
    text = _plain_question(_PURPOSE.sub("", text).strip())
    # H12: "How did Reseller sales change from 2008 (or between 2008 and 2009)?" compares
    # two named periods; any other "how did" asks for an explanation.
    named = rf"(?:{_MONTH_WORD}\.?\s+)?(?:19|20)\d{{2}}"
    compared = bool(_GROWTH.search(text)) and len(re.findall(rf"\b{named}\b", text, re.I)) == 2
    if compared:
        text = re.sub(
            rf"\bbetween\s+({named})\s+and\s+({named})\b", r"from \1 to \2", text, flags=re.I
        )
    if (
        not text
        or len(text) > 400
        or not (_IMPORTED_ASKS.search(text) or re.search(r"\b(?:vs\.?|versus)\s", text, re.I))
        or _IMPORTED_DISCUSSION.search(
            re.sub(r"^\s*how\s+(?:did|does|has|have)\b", "what", text, flags=re.I)
            if compared
            else text
        )
        or _UNSUPPORTED_TIME.search(text)
        or _unsupported_period(text)
    ):
        return None
    tables = {item.table.id: item.table for item in snapshot.tables}
    shape = model_shape(snapshot, relationships)
    raw_words = name_words(text)
    asked = set(raw_words) | {s for w in raw_words for s in SYNONYMS.get(w, ())}
    acronyms = period_acronyms(text) | {w for w in raw_words if w in {"mtd", "ytd"}}

    evaluable_cache: dict[str, str | None] = {}

    def evaluable(measure: AdvancedDaxProposal) -> str | None:
        """The measure's aggregate table when the lab can evaluate it."""
        if measure.id not in evaluable_cache:
            try:
                evaluable_cache[measure.id] = describe_dax(
                    snapshot, measures, "[" + measure.name.replace("]", "]]") + "]", relationships
                )[0]
            except (ValueError, AggregatePlanError):
                evaluable_cache[measure.id] = measure_table(measure) if measure_table else None
        return evaluable_cache[measure.id]

    # The measure: named by its distinctive words. A period variant folds into its base
    # only when the period is applied below: "PY Total Sales" is last year's Total
    # Sales; YOY, MAT or PMTD have no such reading and used to answer the plain total.
    notes: list[str] = []
    scored: list[tuple[int, frozenset[str], AdvancedDaxProposal]] = []
    for measure in measures:
        words, own = _measure_core(measure.name)
        if words and all(_matches(w, asked) for w in words) and own <= acronyms | asked:
            scored.append((len(words), frozenset(own), measure))
    chosen: AdvancedDaxProposal | None = None
    implied: str | None = None
    typed: set[str] = set()
    if scored:
        best = max(score for score, _, _ in scored)
        top = [(own, m) for score, own, m in scored if score == best]
        typed = {a for own, _ in top for a in own if a in raw_words}
        if typed - _FOLDED_ACRONYMS.keys():
            exact = [m for own, m in top if own == typed and evaluable(m)]
            if len(exact) != 1:
                return None
            chosen = exact[0]
            typed = set()
        else:
            usable = [(own, m) for own, m in top if evaluable(m)]
            plain = [m for own, m in usable if not own]
            picked = plain if plain else [m for _, m in usable]
            if len(picked) != 1:
                return None
            chosen = picked[0]
    # "PY"/"PM" typed as a word: the same period as "last year"/"last month". With a
    # period of its own ("PY Total Sales in 2009", "MTD sales last month") the acronym
    # shifts that period instead, which is not read here.
    if typed and (
        _NAMED_PERIOD.search(text) or _relative_phrases(text) - {_FOLDED_ACRONYMS[a] for a in typed}
    ):
        return None
    period_text = " ".join([text, *(_FOLDED_ACRONYMS[a] for a in sorted(typed))])
    # "2008 compared to the previous year" is 2007, not the year before the data ends.
    if _NAMED_PERIOD.search(period_text) and _RELATIVE_BACK.search(period_text):
        return None
    threshold_match = _THRESHOLD.search(text)
    threshold: tuple[str, float] | None = None
    if threshold_match:
        number = float(threshold_match["num"].replace(",", ""))
        unit = (threshold_match["unit"] or "").casefold()
        number *= {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get(unit, 1)
        op = _THRESHOLD_OPS.get(threshold_match["op"].casefold(), "gte")
        threshold = (op, number)
    by_name = {folded(m.name): m for m in measures}
    base = _base_measure(measures, evaluable)
    if chosen is None and threshold is not None:
        # "hit the 100K target": the report's own measure compares [MTD Total Sales]
        # with 100000, so that is the metric and the test; say so in the answer.
        literal = str(int(threshold[1])) if threshold[1].is_integer() else str(threshold[1])
        found = {
            (match[1], match[2], holder.name)
            for holder in measures
            for match in re.finditer(
                r"\[([^\[\]]+)\]\s*(>=|<=|>|<)\s*" + re.escape(literal) + r"(?![\d.])",
                holder.expression,
            )
        }
        if len({(item, op) for item, op, _ in found}) == 1:
            compared_measure, operator, holder = next(iter(found))
            target = by_name.get(folded(compared_measure))
            if target is not None and not evaluable(target):
                words, own = _measure_core(target.name)
                target = next(
                    (
                        m
                        for m in measures
                        if not _measure_core(m.name)[1]
                        and _measure_core(m.name)[0] == words
                        and evaluable(m)
                    ),
                    None,
                )
                implied = next(iter(own & {"mtd", "ytd"}), None) if len(own) == 1 else None
                if implied is None and own:
                    target = None
            if target is not None:
                chosen = target
                threshold = (
                    {">=": "gte", ">": "gt", "<=": "lte", "<": "lt"}[operator],
                    threshold[1],
                )
                notes.append(
                    f"The report's [{holder}] tests [{compared_measure}] {operator} {literal}; "
                    "this uses the same test."
                )
    ranking = bool(re.search(rf"\b(?:{_HIGH}|{_LOW}|strongest)\b", text, re.I))
    comparing = bool(
        re.search(
            r"\b(?:vs\.?|versus|compared?|compaired|grew|grow|growth|increase[ds]?|"
            r"decrease[ds]?|declined?|much)\b",
            text,
            re.I,
        )
    )
    if chosen is None:
        # No measure named: rank or compare by the report's base measure, and say so.
        if base is None or not (ranking or comparing):
            return None
        metric_words = {w for m in measures for w in _measure_core(m.name)[0]}
        fallback_entities = {
            w for t in tables.values() for w in name_words(t.name) if w not in _TABLE_PREFIXES
        }
        unexplained = (
            (asked & metric_words) - fallback_entities - _TIME_WORDS - _measure_core(base.name)[0]
        )
        if unexplained:
            return None
        chosen = base
        notes.append(
            f"No measure was named, so this uses [{base.name}], the measure the report's "
            "other measures build on; name another measure for a different figure."
        )
    # A second metric the words left over still name ("total sales and quantity sold"
    # answered only [Quantity Sold]) is not this one number or list.
    mine = _measure_core(chosen.name)[0]
    selling = {"sale", "sell", "sold", "selling"}
    rest = {
        w
        for w in raw_words
        if w not in mine
        and not set(SYNONYMS.get(w, ())) & mine
        # "How many units did we sell": the verb belongs to [Quantity Sold]; the noun
        # "sales" still names [Total Sales].
        and not (w in selling - {"sale"} and mine & selling)
    }
    rest |= {s for w in rest for s in SYNONYMS.get(w, ())}
    for other in measures:
        words = _measure_core(other.name)[0]
        if (
            words
            and not words <= mine
            and not words <= _WORDING
            and all(_matches(w, rest) for w in words)
        ):
            return None
    table_id = evaluable(chosen)
    if table_id is None or table_id not in tables:
        return None
    fact = tables[table_id]
    reach = _reachable(table_id, relationships)

    # Periods, from the measure table's own date column: robust without a date table.
    dates = _data_dates(shape, table_id)
    periods: list[Period] = []
    implied_used = False
    if dates is not None:
        parsed_periods = _periods(period_text, dates)
        if parsed_periods is None:
            return None
        periods = parsed_periods
        if implied == "mtd" and not periods:
            this_month = date(dates.last.year, dates.last.month, 1)
            periods = [Period(f"{this_month:%B %Y}", this_month, dates.last)]
            implied_used = True
    elif implied or re.search(r"\b(?:month|year|mtd|ytd|(?:19|20)\d{2})\b", period_text, re.I):
        return None  # the report's test is month-to-date; without dates it is not this one
    if len(periods) > 2:
        return None
    if dates is not None:
        # "in November 2009" is no relative date: only the implied MTD says "this month".
        note = _relative_note(text, dates.last, implied if implied_used else None, typed)
        if note:
            notes.insert(0, note)
    if len(periods) == 2 and dates is not None:
        earlier, later = periods
        if earlier.label[:1].isalpha() != later.label[:1].isalpha():
            return None  # "December 2009 vs 2008": a month against a year
        natural = (
            _month_end(later.start) if later.label[:1].isalpha() else date(later.start.year, 12, 31)
        )
        if later.end > dates.last:
            later = Period(later.label, later.start, dates.last)
        if later.end < natural:
            # The same calendar days, as DATEADD shifts them: to 10 Dec in both years (a
            # day count stopped leap-year 2008 on 9 Dec).
            first = (
                earlier.start
                if later.label[:1].isalpha()
                else date(earlier.start.year, later.end.month, 1)
            )
            cut = first.replace(day=min(later.end.day, _month_end(first).day))
            earlier = Period(earlier.label, earlier.start, min(earlier.end, cut))
            notes.append(
                f"{later.label} has data only until {later.end:%d %b %Y}, so {earlier.label} "
                f"is compared over the same days (to {earlier.end:%d %b}), as the report's "
                "previous-period measures do."
            )
        periods = [earlier, later]
    elif len(periods) == 1 and dates is not None and periods[0].end > dates.last:
        periods = [Period(periods[0].label, periods[0].start, dates.last)]
        notes.append(f"{periods[0].label} has data only until {dates.last:%d %b %Y}.")

    # Entities: a column the question names, or a table ("agents" -> DimEmployee rows).
    entity_words: dict[str, set[str]] = {}
    for other_id in reach:
        if other_id == table_id:
            continue
        words = {w for w in name_words(tables[other_id].name) if w not in _TABLE_PREFIXES}
        if words and not words <= _TIME_WORDS:
            entity_words[other_id] = words
    # H09: "How many stores does each store type have" counts DimStore rows, which no
    # measure does; answering [Total Sales] by store type dropped the count. The query
    # builders read it. "How many agents sold more than 100K" is a threshold count here.
    counted = re.search(
        r"\b(?:how\s+many|number\s+of|count\s+of)\s+(?:the\s+|our\s+)?([a-z]+)", text, re.I
    )
    if counted and threshold is None:
        noun = set(name_words(counted[1]))
        noun |= {s for w in noun for s in SYNONYMS.get(w, ())}
        if not noun & _measure_core(chosen.name)[0] and any(
            words <= noun for words in entity_words.values()
        ):
            return None
    vocabulary = set().union(*entity_words.values()) if entity_words else set()
    columns_core: dict[tuple[str, str], set[str]] = {}
    for other_id in reach:
        for column in tables[other_id].columns:
            tokens = name_words(column.name)
            # DateWithTransactions made "compared with December 2008" name a column (H19).
            core = {t for t in tokens if t not in _CONNECTIVES} or set(tokens)
            columns_core[(other_id, column.id)] = core
            vocabulary |= core
    for word in list(asked):
        if len(word) >= 5 and word not in vocabulary:
            asked |= {known for known in vocabulary if len(known) >= 5 and _one_edit(word, known)}

    sensitive_named = [
        column.name
        for other_id in reach
        for column in tables[other_id].columns
        if sensitive_column(tables[other_id].name, column.name)
        and columns_core[(other_id, column.id)] <= asked
    ]

    # Named values: exact, or one typo away for single words ("onlin" -> Online).
    tokens = [w.casefold() for w in re.findall(r"[A-Za-z][\w-]*", text)]
    values: dict[str, list[tuple[str, str]]] = {}
    for other_id in reach:
        table = tables[other_id]
        for column in table.columns:
            if sensitive_column(table.name, column.name):
                continue
            for value in _all_values(column):
                key = value.casefold()
                if len(value) < 3 or key in _COMMON_VALUES or key in vocabulary:
                    continue
                value_match = re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", text, re.I)
                typo = (
                    " " not in value
                    and len(value) >= 5
                    and any(
                        len(token) >= 4 and _one_edit(token.removesuffix("s"), key)
                        for token in tokens
                    )
                )
                if value_match or typo:
                    values.setdefault(value, []).append((other_id, column.id))
    by_column: dict[tuple[str, str], list[str]] = {}
    for value, places in values.items():
        if len(places) > 1:
            # Online is a channel and a store type: the question says which, or the value
            # names a row of a lookup table (DimChannel[ChannelName], one row per channel)
            # rather than a category of another table's rows (H12: "Reseller sales").
            said_which = [
                place
                for place in places
                if (columns_core[place] | entity_words.get(place[0], set())) & asked
            ]
            if len(said_which) == 1:
                places = said_which
            else:
                keys = [p for p in places if _column(tables, p[0], p[1]).candidate_key]
                if said_which or len(keys) != 1:
                    return None
                others = [
                    _reference(tables[t].name, _column(tables, t, c).name)
                    for t, c in places
                    if (t, c) != keys[0]
                ]
                chosen_ref = _reference(tables[keys[0][0]].name, _column(tables, *keys[0]).name)
                notes.append(
                    f"“{value}” is also a value of {', '.join(others)}; this uses {chosen_ref}."
                )
                places = keys
        by_column.setdefault(places[0], []).append(value)
    picks_at = [place for place, named in by_column.items() if len(named) > 1]
    if len(picks_at) > 1:
        return None
    filters = [(place, named[0]) for place, named in by_column.items() if len(named) == 1]
    consumed = {place for place in by_column} | {(place[0], "") for place in by_column}

    combined = re.search(
        r"\ball\s+(?:the\s+|of\s+the\s+)?(\w+)\s+(?:combined|together|in total|overall|altogether)"
        r"|\b(?:combined|altogether)\b",
        text,
        re.I,
    )
    groups: list[tuple[str, str]] = []
    entity = ""
    label_columns: list[str] = []
    key_column: str | None = None
    join_labels = " · "

    def said(words: set[str], fallback: str) -> str:
        # Answer in the user's words: "agents", "channel" and "store type", not
        # DimEmployee, ChannelName or a bare "store" for StoreType.
        spoken: dict[str, str] = {}  # model word -> the user's first word for it
        for w in raw_words:
            for target in ({w} | set(SYNONYMS.get(w, ()))) & words:
                spoken.setdefault(target, w)
        if spoken:
            return " ".join(dict.fromkeys(spoken.values()))
        return next((w for w in asked if w in words and len(w) > 2), fallback)

    if picks_at:
        place = picks_at[0]
        groups = [place]
        column = _column(tables, place[0], place[1])
        label_columns = [column.name]
        entity = said(columns_core[place], column.name)
    elif not combined:
        metric = _measure_core(chosen.name)[0]
        named_columns = []
        for (t, c), core in columns_core.items():
            column = _column(tables, t, c)
            if (
                t == table_id
                or not core <= asked
                or core <= _TIME_WORDS
                or (core <= _ORDINAL_WORDS and not set(name_words(column.name)) <= asked)
                or core <= metric
                or (t, c) in consumed
                or column.data_type.value not in {"string", "boolean"}
                or sensitive_column(tables[t].name, column.name)
            ):
                continue
            words = set(name_words(column.name))
            # StoreName beats StoreKey for "store": more words, then names, then non-keys.
            rank = (len(core), "name" in words, not {"id", "key", "code"} & words)
            named_columns.append((rank, t, c))
        named_tables = [
            t for t, words in entity_words.items() if words <= asked and (t, "") not in consumed
        ]
        if len({t for _, t, _ in named_columns} | set(named_tables)) > 1:
            return None
        if named_columns:
            _, t, c = max(named_columns)
            groups = [(t, c)]
            label_columns = [_column(tables, t, c).name]
            entity = said(columns_core[(t, c)], label_columns[0])
        elif named_tables:
            t = named_tables[0]
            table = tables[t]
            labels = [
                c
                for c in table.columns
                if c.data_type.value == "string"
                and "name" in name_words(c.name)
                and not sensitive_column(table.name, c.name)
            ][:2] or [
                c
                for c in table.columns
                if c.data_type.value == "string"
                and c.candidate_key
                and not sensitive_column(table.name, c.name)
            ][:1]
            if not labels:
                return None
            relationship_key = next(
                (
                    item.to_column_id
                    for item in usable_relationships(relationships)
                    if item.to_table_id == t
                ),
                None,
            )
            unique = len(labels) == 1 and labels[0].candidate_key
            groups = ([(t, relationship_key)] if relationship_key and not unique else []) + [
                (t, c.id) for c in labels
            ]
            key_column = (
                _column(tables, t, relationship_key).name
                if relationship_key and not unique
                else None
            )
            label_columns = [c.name for c in labels]
            words = {w for c in labels for w in name_words(c.name)}
            join_labels = " " if {"first", "last"} <= words else " · "
            entity = said(entity_words[t], sorted(entity_words[t])[0])
    # Every "by X" / "which X" / "top 5 X" must be what the answer groups by: "by region"
    # (no such field) or "by product" (only a key) is declined, never silently dropped.
    grouped = {w for t, c in groups for w in columns_core[(t, c)] | entity_words.get(t, set())}
    known = (
        grouped
        | _measure_core(chosen.name)[0]
        | _FILLER
        | _TIME_WORDS
        | {w for value in values for w in name_words(value)}
    )
    for match in _SLOT.finditer(text):
        slot_words = name_words(match[1])
        word = slot_words[0] if slot_words else ""
        if not word or word.isdigit() or word in known or set(SYNONYMS.get(word, ())) & known:
            continue
        if len(word) < 5 or not any(_one_edit(word, item) for item in grouped if len(item) >= 5):
            return None
    # A model field the question names but the answer would not use ("sales and returns",
    # "sales by product key") means the question is not this one number or list.
    used = (
        known
        | {
            w
            for place in by_column
            for w in columns_core[place] | entity_words.get(place[0], set())
        }
        # Personal fields are left out with a note, whichever of their words were used.
        | {
            w
            for (t, c), core in columns_core.items()
            if sensitive_column(tables[t].name, _column(tables, t, c).name)
            for w in core
        }
        | (set(name_words(combined[1])) if combined and combined[1] else set())
        | {"key", "name", "id", "code", "total", "count", "number", "amount", "value"}
    )
    # "units" asked for [Quantity Sold]: a word whose synonym is used is used too.
    used |= {w for w in asked if set(SYNONYMS.get(w, ())) & used}
    if (asked & vocabulary) - used:
        return None
    # Words that map to nothing were dropped before: "total sales for Nuria Gonzalez" and
    # "sales in Seattle" got the grand total, "store managers" a list by store.
    # Personal wording ("base pay", "salary") is declined with a note, never answered.
    personal_words = [w for w in dict.fromkeys(raw_words) if sensitive_column("", w)]
    if _unmapped_words(
        text,
        used
        | _measure_core(chosen.name)[1]
        | acronyms
        | typed
        | _QUESTION_WORDS
        | set(personal_words),
    ):
        return None
    # Pay or birth dates per person (or per store, often one person) never reach chat
    # history; a total across everyone is an aggregate.
    personal = _reads_personal(chosen, measures, tables)
    if personal and (groups or filters):
        return None
    if len(groups) > 3:
        return None
    high = re.search(rf"\b(?:{_HIGH}|strongest)\b", text, re.I)
    low = re.search(rf"\b(?:{_LOW})\b", text, re.I)
    if high and low:
        return None
    direction: Literal["desc", "asc"] | None = "desc" if high else "asc" if low else None
    limit_match = re.search(
        r"\b(?:top|best|bottom|worst|first)\s+(\d{1,2})\b|\b(\d{1,2})\s+(?:best|top|biggest|"
        r"largest|highest|worst|weakest|lowest|bottom|strongest)\b",
        text,
        re.I,
    )
    if limit_match is None and direction:
        # H05: "Which 3 stores sold the most in 2009?" ranks as "the top 3 stores".
        limit_match = re.search(
            r"\b(?:which|what|the|name|list|show(?:\s+me)?|give\s+me)\s+(?:the\s+)?(\d{1,2})"
            r"(?=\s+[a-z])",
            text,
            re.I,
        )
    limit = int(next(group for group in limit_match.groups() if group)) if limit_match else None
    if limit is not None and not 1 <= limit <= 25:
        return None
    # A number the plan does not read ("Store 001", "No.1") names something it dropped.
    unread = text
    for match in sorted((m for m in (threshold_match, limit_match) if m), key=lambda m: -m.start()):
        unread = unread[: match.start()] + " " + unread[match.end() :]
    if re.search(r"\d", _READ_PERIODS.sub(" ", unread)):
        return None
    if (direction or threshold or limit) and not groups:
        return None  # "the highest sales" with nothing to rank is not one number
    if threshold is not None and not groups:
        return None
    if any(place in groups for place, _ in filters):
        return None

    # The query: CALCULATE filters for named values and the period, grouped if asked.
    label = chosen.name.replace('"', "")
    value = "[" + chosen.name.replace("]", "]]") + "]"
    value_filters = [
        f'{_reference(tables[t].name, _column(tables, t, c).name)} = "{named}"'
        for (t, c), named in filters
    ]
    scope = f" for {' and '.join(named for _, named in filters)}" if filters else ""

    def query(period: Period | None) -> ImportedQuery:
        lab = list(value_filters)
        shown = list(value_filters)
        if period is not None and dates is not None:
            filtered_lab, filtered_shown = _date_filters(fact.name, dates.column, period)
            lab.append(filtered_lab)
            shown.append(filtered_shown)
        lab_value = f"CALCULATE({value}, {', '.join(lab)})" if lab else value
        shown_value = f"CALCULATE({value}, {', '.join(shown)})" if shown else value
        if not groups:
            return ImportedQuery(lab_value, shown_value, period)
        refs = ", ".join(_reference(tables[t].name, _column(tables, t, c).name) for t, c in groups)
        order = "ASC" if direction == "asc" else "DESC"
        template = (
            'EVALUATE SUMMARIZECOLUMNS({refs}, "{label}", {value}) ORDER BY [{label}] {order}'
        )
        return ImportedQuery(
            template.format(refs=refs, label=label, value=lab_value, order=order),
            template.format(refs=refs, label=label, value=shown_value, order=order),
            period,
        )

    queries = tuple(query(p) for p in periods) if periods else (query(None),)
    leader = None
    if (
        dates is not None
        and groups
        and not periods
        and any(note.startswith("No measure was named") for note in notes)
    ):
        this_month = date(dates.last.year, dates.last.month, 1)
        leader = query(Period(f"{dates.last:%B %Y}", this_month, dates.last))
    if injected:
        notes.append(
            "I ignored the instructions in your message to change how I answer; the figures "
            "come from your data."
        )
    left_out = sensitive_named or personal_words
    if left_out and not personal:
        notes.append(
            f"I left out {', '.join(dict.fromkeys(left_out))}: these are personal "
            "fields, so chat does not list them for named people. An aggregate across all "
            "of them (for example by age band) is the safer way to look for a pattern."
        )
    if purpose and re.search(
        r"\b(?:older|younger|age|gender|male|female|pay|salary|cause|correlat\w*|whether)\b",
        purpose[0],
        re.I,
    ):
        count = f"{limit} people" if limit else "A handful of people"
        notes.append(f"{count} cannot show that one trait causes higher sales.")
    if direction == "asc" and key_column:
        notes.append(f"Only {entity}s with a value in this period are ranked.")
    all_data = (
        f"all data ({dates.first:%d %b %Y} – {dates.last:%d %b %Y})" if dates is not None else ""
    )
    if combined and not groups:
        covered = next(
            (
                (entity_words[t], values_listed)
                for t in entity_words
                if entity_words[t] <= asked
                for column in tables[t].columns
                if (values_listed := _all_values(column))
            ),
            None,
        )
        if covered:
            notes.append(f"It covers every {sorted(covered[0])[0]}: {', '.join(covered[1])}.")
    return ImportedAnswerPlan(
        measure=label,
        queries=queries,
        entity=entity,
        label_columns=tuple(label_columns),
        key_column=key_column,
        join_labels=join_labels,
        direction=direction,
        limit=limit,
        picks=tuple(by_column[picks_at[0]]) if picks_at else (),
        threshold=threshold,
        scope=scope,
        all_data=all_data,
        growth=len(periods) == 2 and bool(_GROWTH.search(text)),
        # "declined the most" and "grew least" want the lowest change; "declined least"
        # the highest.
        growth_low=bool(_WORST.search(text))
        or bool(_DECLINE.search(text)) != bool(_LEAST.search(text)),
        leader=leader,
        notes=tuple(notes),
        definition=(
            f"[{chosen.name}] = {' '.join(chosen.expression.split())}"
            if len(chosen.expression) <= 100
            else ""
        ),
    )


_LAB_OPS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def lab_formula(plan: ImportedAnswerPlan) -> str | None:
    """The plan as one DAX Lab query returning exactly the answer, or None.

    MAN-03 ("top 5 agents in December 2009") and MAN-09 ("5 weakest") got model formulas
    with no month filter or a DESC sort; chat's planner already had the exact query.
    Here it keeps only the requested rows (TOPN), drops the blank member (sales with no
    matching agent is no agent), and applies a threshold.
    """
    if plan.picks or plan.leader is not None:
        return None
    if len(plan.queries) == 2 and not (plan.limit or plan.threshold):
        return _two_period_formula(plan)
    if len(plan.queries) != 1 or plan.growth:
        return None
    display = plan.queries[0].display
    match = re.fullmatch(
        r"EVALUATE (SUMMARIZECOLUMNS\((.*?)(?:, \"[^\"]+\", .*)\))"
        r" ORDER BY (\[[^\]]+\]) (ASC|DESC)",
        display,
        re.S,
    )
    if match is None:
        return None if plan.limit or plan.direction or plan.threshold else display
    table, groups, measure, order = match.groups()
    if plan.key_column:
        key = groups.split(", ")[0]
        table = f"FILTER({table}, NOT ISBLANK({key}))"
    if plan.threshold is not None:
        op, number = plan.threshold
        amount = str(int(number)) if float(number).is_integer() else str(number)
        table = f"FILTER({table}, {measure} {_LAB_OPS[op]} {amount})"
    count = plan.limit or (1 if plan.direction and plan.threshold is None else None)
    if count is not None:
        table = f"TOPN({count}, {table}, {measure}, {order})"
    return f"EVALUATE {table} ORDER BY {measure} {order}"


def _two_period_formula(plan: ImportedAnswerPlan) -> str | None:
    """Both periods side by side with the change (H19: "December 2009 compared with
    December 2008, by channel" got a blank DIVIDE of two SELECTEDVALUE filters)."""
    earlier, later = plan.queries
    if earlier.period is None or later.period is None:
        return None
    names = [
        f"{plan.measure} {period.label}".replace('"', "")
        for period in (earlier.period, later.period)
    ]
    if names[0] == names[1]:
        return None
    grouped = re.compile(
        r"EVALUATE SUMMARIZECOLUMNS\((.*?), \"[^\"]+\", (.*)\) ORDER BY \[[^\]]+\] (?:ASC|DESC)",
        re.S,
    )
    first, second = grouped.fullmatch(earlier.display), grouped.fullmatch(later.display)
    a, b = (f"[{name}]" for name in names)
    change = f'"Change %", DIVIDE({b} - {a}, {a})'
    if first is None and second is None:
        return (
            f'EVALUATE ROW("{names[0]}", {earlier.display}, "{names[1]}", {later.display}, '
            f"{change.replace(a, earlier.display).replace(b, later.display)})"
        )
    if first is None or second is None or first[1] != second[1]:
        return None
    table = f'SUMMARIZECOLUMNS({first[1]}, "{names[0]}", {first[2]}, "{names[1]}", {second[2]})'
    return f"EVALUATE ADDCOLUMNS({table}, {change}) ORDER BY {b} DESC"


def _column(tables: dict[str, Any], table_id: str, column_id: str) -> Any:
    return next(c for c in tables[table_id].columns if c.id == column_id)


_COLUMN_REFERENCE = re.compile(r"(?:'((?:[^']|'')+)'|([A-Za-z_][\w]*))\s*\[([^\[\]\n]+)\]")
_BARE_NAME = re.compile(r"(?<![\w'\]])\[([^\[\]\n]+)\]")


def _reads_personal(
    measure: AdvancedDaxProposal, measures: list[AdvancedDaxProposal], tables: dict[str, Any]
) -> bool:
    """Whether the measure, or a measure it uses, reads a personal column (pay, birth)."""
    personal = {
        (folded(table.name), folded(column.name))
        for table in tables.values()
        for column in table.columns
        if sensitive_column(table.name, column.name)
    }
    by_name = {folded(m.name): m for m in measures}
    seen: set[str] = set()
    pending = [measure]
    while pending:
        current = pending.pop()
        if current.id in seen:
            continue
        seen.add(current.id)
        for match in _COLUMN_REFERENCE.finditer(current.expression):
            table = folded((match[1] or match[2]).replace("''", "'"))
            if (table, folded(match[3])) in personal:
                return True
        # SUMX(DimEmployee, [BaseRate]): a bare name that is a personal column, not a measure.
        bare = {folded(name) for name in _BARE_NAME.findall(current.expression)}
        if bare & ({column for _, column in personal} - set(by_name)):
            return True
        pending.extend(
            by_name[name] for name in measure_references(current.expression, set(by_name))
        )
    return False


def _base_measure(
    measures: list[AdvancedDaxProposal], evaluable: Any
) -> AdvancedDaxProposal | None:
    """The evaluable measure most others build on ([Total Sales]), if it is clear."""
    names = {folded(m.name) for m in measures}
    counts: dict[str, int] = {}
    for measure in measures:
        for name in measure_references(measure.expression, names):
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(measures, key=lambda m: -counts.get(folded(m.name), 0))
    for measure in ranked[:5]:
        if counts.get(folded(measure.name), 0) >= 2 and evaluable(measure):
            return measure
    return None


def compose_imported_answer(
    plan: ImportedAnswerPlan, results: list[tuple[list[dict[str, object]], bool]]
) -> str | None:
    """The answer with every number from the evaluated rows; None when rows fall short."""
    numbers = [
        numeric_value
        for rows, _ in results
        for row in rows
        if isinstance(numeric_value := row.get(plan.measure), (int, float))
    ]
    whole = all(float(n).is_integer() for n in numbers)

    def shown(value: object) -> str:
        return format_value(value, "#,#0" if whole else "#,#0.00")

    def when(query: ImportedQuery) -> str:
        return f"in {query.period.label}" if query.period else f"for {plan.all_data or 'all data'}"

    def label(row: dict[str, object]) -> str:
        parts = [row.get(name) for name in plan.label_columns]
        if all(part is None for part in parts):
            return "(Blank)"
        return plan.join_labels.join("(Blank)" if p is None else str(p) for p in parts)

    def ranked(rows: list[dict[str, object]]) -> list[tuple[str, dict[str, object]]]:
        named = [(label(row), row) for row in rows if row.get(plan.measure) is not None]
        counts: dict[str, int] = {}
        for text, _ in named:
            counts[text] = counts.get(text, 0) + 1
        return [
            (
                f"{text} ({plan.key_column} {row.get(plan.key_column)})"
                if counts[text] > 1 and plan.key_column
                else text,
                row,
            )
            for text, row in named
        ]

    evidence = "\n\n".join(
        (f"-- {query.period.label}\n" if query.period and len(plan.evaluations) > 1 else "")
        + query.display
        for query in plan.evaluations
    )
    if plan.definition:
        evidence = f"-- {plan.definition}\n{evidence}"
    footer = (
        "\n\n" + " ".join(plan.notes) if plan.notes else ""
    ) + f"\n\nCalculated from your imported data with:\n```dax\n{evidence}\n```"
    measure = f"{plan.measure}{plan.scope}"
    if not plan.label_columns:
        values = [next(iter(rows[0].values()), None) if rows else None for rows, _ in results]
        if len(plan.queries) == 1:
            return f"{measure} {when(plan.queries[0])} is **{shown(values[0])}**." + footer
        earlier, later = values[0], values[1]
        text = (
            f"{measure} was **{shown(later)}** {when(plan.queries[1])} and "
            f"**{shown(earlier)}** {when(plan.queries[0])}"
        )
        return text + _change(earlier, later, shown) + "." + footer

    entity = plan.entity
    if len(plan.queries) == 2:
        (earlier_rows, earlier_cut), (later_rows, later_cut) = results[0], results[1]
        # A capped result ranks only part of the members, and a member past the cap
        # read as (Blank): Thimphu No.2 "had no 2007 sales" at rank 113 of 306.
        if (earlier_cut or later_cut) and not plan.picks:
            return None
        before = {text: row.get(plan.measure) for text, row in ranked(earlier_rows)}
        after = ranked(later_rows)
        now = {text: row.get(plan.measure) for text, row in after}
        if plan.picks:
            keys = [
                next((text for text in {*before, *now} if text.casefold() == pick.casefold()), pick)
                for pick in plan.picks
            ]
            if any(
                (earlier_cut and key not in before) or (later_cut and key not in now)
                for key in keys
            ):
                return None
        else:
            keys = [text for text, _ in after if text != "(Blank)"][: plan.limit or 8]
        if not keys:
            return None
        comparison_lines = [
            f"- {key}: {shown(before.get(key))} {when(plan.queries[0])} → "
            f"{shown(now.get(key))} {when(plan.queries[1])}"
            + _change(before.get(key), now.get(key), shown, brief=True)
            for key in keys
        ]
        verdict = ""
        if plan.growth:
            verdict = _growth_verdict(plan, keys, before, now, shown, entity)
        return f"{measure} by {entity}:\n" + "\n".join(comparison_lines) + verdict + footer

    rows, truncated = results[0]
    listed = ranked(rows)
    blank = next((row for text, row in listed if text == "(Blank)"), None)
    listed = [(text, row) for text, row in listed if text != "(Blank)"]
    period = when(plan.queries[0])
    if blank is not None:
        footer = (
            f"\n\n{shown(blank.get(plan.measure))} of {plan.measure} has no matching {entity} "
            "and is not ranked." + footer
        )
    if plan.picks:
        picked = [
            (text, row)
            for text, row in listed
            if text.casefold() in {pick.casefold() for pick in plan.picks}
        ]
        lines = "\n".join(f"- {text}: {shown(row.get(plan.measure))}" for text, row in picked)
        return f"{measure} {period}:\n{lines}" + footer
    if plan.threshold is not None:
        op, limit = plan.threshold
        passed = [
            (text, row)
            for text, row in listed
            if isinstance(metric_value := row.get(plan.measure), (int, float))
            and {
                "gt": metric_value > limit,
                "gte": metric_value >= limit,
                "lt": metric_value < limit,
                "lte": metric_value <= limit,
            }[op]
        ]
        if truncated and (op in {"gt", "gte"}) and len(passed) == len(listed):
            return None  # the capped result may hide more that pass
        if op in {"lt", "lte"} and truncated:
            return None
        amount = format_value(limit, "#,#0" if float(limit).is_integer() else "#,#0.00")
        head = (
            f"**{len(passed)}** {entity}{'s' if len(passed) != 1 else ''} had {measure} of "
            f"{_OP_TEXT[op]} **{amount}** {period}"
        )
        if not passed:
            return head + "." + footer
        lines = "\n".join(f"- {text}: {shown(row.get(plan.measure))}" for text, row in passed[:20])
        more = f"\n- …and {len(passed) - 20} more" if len(passed) > 20 else ""
        return f"{head}:\n{lines}{more}" + footer
    if not listed:
        return f"No {entity} has a {plan.measure} value{plan.scope} {period}." + footer
    count = plan.limit or 8
    shown_rows = listed[:count]
    if plan.limit:
        lines = "\n".join(
            f"{index}. {text}: {shown(row.get(plan.measure))}"
            for index, (text, row) in enumerate(shown_rows, start=1)
        )
        word = "Top" if plan.direction != "asc" else "Bottom"
        text = f"{word} {len(shown_rows)} {entity}s by {measure} {period}:\n{lines}"
    else:
        lines = "\n".join(f"- {text}: {shown(row.get(plan.measure))}" for text, row in shown_rows)
        more = (
            ("\n- …and more" if truncated else f"\n- …and {len(listed) - count} more")
            if len(listed) > count
            else ""
        )
        if plan.direction:
            first = listed[0]
            tied = [t for t, row in listed if row.get(plan.measure) == first[1].get(plan.measure)]
            word = "highest" if plan.direction == "desc" else "lowest"
            lead = (
                f"{', '.join(f'**{t}**' for t in tied)} tie for the {word}"
                if len(tied) > 1
                else f"**{first[0]}** has the {word}"
            )
            text = (
                f"{lead} {measure} {period}: **{shown(first[1].get(plan.measure))}**.\n\n"
                f"{plan.measure} by {entity}:\n{lines}{more}"
            )
        else:
            text = f"{measure} by {entity} {period}:\n{lines}{more}"
    if plan.leader is not None and len(results) > 1:
        month = [(t, row) for t, row in ranked(results[1][0]) if t != "(Blank)"]
        if month:
            text += (
                f"\n\nIn {plan.leader.period.label if plan.leader.period else ''} (the latest "
                f"month with data) **{month[0][0]}** leads with "
                f"**{shown(month[0][1].get(plan.measure))}**."
            )
    return text + footer


def _growth_verdict(
    plan: ImportedAnswerPlan,
    keys: list[str],
    before: dict[str, Any],
    now: dict[str, Any],
    shown: Any,
    entity: str,
) -> str:
    """Who grew (or declined) most, over every member with a value in both periods.

    Only the listed top 8 used to count, and the best grower was named even when the
    question asked which declined most.
    """
    pool = keys if plan.picks else [key for key in now if key in before and key != "(Blank)"]
    rates = {
        key: (now[key] - before[key]) / before[key]
        for key in pool
        if isinstance(before.get(key), (int, float))
        and isinstance(now.get(key), (int, float))
        and before[key]
    }
    if len(rates) < 2 or (plan.picks and len(rates) != len(keys)):
        return ""
    two = len(rates) == 2
    key = (min if plan.growth_low else max)(rates, key=lambda item: rates[item])
    rate = rates[key]
    detail = f"{rate:+.1%}"
    if key not in keys:
        detail += f": {shown(before[key])} → {shown(now[key])}"
    if plan.growth_low and rate < 0:
        text = f"**{key}** declined {'more' if two else 'the most'} ({detail})."
    elif plan.growth_low:
        all_grew = "Both grew" if two else "All grew"
        text = f"{all_grew}; **{key}** grew {'less' if two else 'least'} ({detail})."
    elif rate <= 0:
        none_grew = "Neither grew" if two else "None grew"
        text = f"{none_grew}; **{key}** declined {'less' if two else 'least'} ({detail})."
    elif sum(1 for value in rates.values() if value > 0) == 1:
        text = f"Only **{key}** grew ({detail})."
    else:
        text = f"**{key}** grew {'more' if two else 'the most'} ({detail})."
    unranked = sum(1 for item in now if item != "(Blank)" and not before.get(item))
    if not plan.picks and unranked:
        text += (
            f" {unranked} {entity}{'s' if unranked != 1 else ''} with no value in the earlier "
            "period cannot have a growth rate and are not ranked."
        )
    return "\n\n" + text


def _change(earlier: object, later: object, shown: Any, *, brief: bool = False) -> str:
    """ ": up **346,244.31** (+1.8%)" or, in a list, " (+346,244.31, +1.8%)"."""
    if not isinstance(earlier, (int, float)) or not isinstance(later, (int, float)):
        return ""
    delta = later - earlier
    if delta == 0:
        return " (no change)" if brief else ": unchanged"
    rate = f"{delta / earlier:+.1%}" if earlier else ""
    if brief:
        return f" ({'+' if delta > 0 else '-'}{shown(abs(delta))}" + (f", {rate})" if rate else ")")
    return f": {'up' if delta > 0 else 'down'} **{shown(abs(delta))}**" + (
        f" ({rate})" if rate else ""
    )
