"""Conservative static validation for approved, AI-written DAX expressions.

The cold PBIP validators validate project structure, but they do not evaluate every
measure. Advanced DAX therefore passes through this reviewed function catalog before
it can be stored or compiled. The catalog intentionally favors a false rejection over
committing a hallucinated or unsupported function name.
"""

from __future__ import annotations

import re

_DAX_FUNCTION_PATTERN = re.compile(r"(?<![A-Za-z0-9_.])(?P<name>[A-Za-z][A-Za-z0-9_.]*)\s*\(")

# Common aggregation, filter/context, relationship, time-intelligence, table,
# ranking, text, information, mathematical, statistical, and financial functions.
# External/environmental functions are deliberately absent.
_SUPPORTED_DAX_FUNCTIONS = frozenset(
    {
        "ABS",
        "ACOS",
        "ACOSH",
        "ACOT",
        "ACOTH",
        "ADDCOLUMNS",
        "ALL",
        "ALLCROSSFILTERED",
        "ALLEXCEPT",
        "ALLNOBLANKROW",
        "ALLSELECTED",
        "AND",
        "APPROXIMATEDISTINCTCOUNT",
        "ASIN",
        "ASINH",
        "ATAN",
        "ATANH",
        "AVERAGE",
        "AVERAGEA",
        "AVERAGEX",
        "BLANK",
        "CALCULATE",
        "CALCULATETABLE",
        "CALENDAR",
        "CALENDARAUTO",
        "CEILING",
        "COALESCE",
        "COMBIN",
        "COMBINA",
        "COMBINEVALUES",
        "CONCATENATE",
        "CONCATENATEX",
        "CONTAINS",
        "CONTAINSROW",
        "CONTAINSSTRING",
        "CONTAINSSTRINGEXACT",
        "COS",
        "COSH",
        "COT",
        "COTH",
        "COUNT",
        "COUNTA",
        "COUNTAX",
        "COUNTBLANK",
        "COUNTROWS",
        "COUNTX",
        "CROSSFILTER",
        "CROSSJOIN",
        "CURRENCY",
        "CURRENTGROUP",
        "DATE",
        "DATEADD",
        "DATEDIFF",
        "DATESBETWEEN",
        "DATESINPERIOD",
        "DATESMTD",
        "DATESQTD",
        "DATESYTD",
        "DATEVALUE",
        "DAY",
        "DEGREES",
        "DISTINCT",
        "DISTINCTCOUNT",
        "DISTINCTCOUNTNOBLANK",
        "DIVIDE",
        "EARLIER",
        "EARLIEST",
        "EDATE",
        "ENDOFMONTH",
        "ENDOFQUARTER",
        "ENDOFYEAR",
        "EOMONTH",
        "EVEN",
        "EXACT",
        "EXCEPT",
        "EXP",
        "FACT",
        "FALSE",
        "FILTER",
        "FIND",
        "FIRSTDATE",
        "FIRSTNONBLANK",
        "FIRSTNONBLANKVALUE",
        "FIXED",
        "FLOOR",
        "FORMAT",
        "GCD",
        "GENERATE",
        "GENERATEALL",
        "GENERATESERIES",
        "GEOMEAN",
        "GEOMEANX",
        "GROUPBY",
        "HASONEFILTER",
        "HASONEVALUE",
        "HOUR",
        "IF",
        "IF.EAGER",
        "IFERROR",
        "INDEX",
        "INT",
        "INTERSECT",
        "IRR",
        "ISBLANK",
        "ISCROSSFILTERED",
        "ISEMPTY",
        "ISERROR",
        "ISEVEN",
        "ISFILTERED",
        "ISINSCOPE",
        "ISLOGICAL",
        "ISNONTEXT",
        "ISNUMBER",
        "ISODD",
        "ISONORAFTER",
        "ISSELECTEDMEASURE",
        "ISSUBTOTAL",
        "ISTEXT",
        "ISO.CEILING",
        "KEEPFILTERS",
        "LASTDATE",
        "LASTNONBLANK",
        "LASTNONBLANKVALUE",
        "LCM",
        "LEFT",
        "LEN",
        "LN",
        "LOG",
        "LOG10",
        "LOOKUPVALUE",
        "LOWER",
        "MATCHBY",
        "MAX",
        "MAXA",
        "MAXX",
        "MEDIAN",
        "MEDIANX",
        "MID",
        "MIN",
        "MINA",
        "MINUTE",
        "MINX",
        "MOD",
        "MONTH",
        "MROUND",
        "NATURALINNERJOIN",
        "NATURALLEFTOUTERJOIN",
        "NETWORKDAYS",
        "NEXTDAY",
        "NEXTMONTH",
        "NEXTQUARTER",
        "NEXTYEAR",
        "NOT",
        "NOW",
        "NPV",
        "ODD",
        "OFFSET",
        "OR",
        "ORDERBY",
        "PARALLELPERIOD",
        "PARTITIONBY",
        "PATH",
        "PATHCONTAINS",
        "PATHITEM",
        "PATHITEMREVERSE",
        "PATHLENGTH",
        "PERCENTILE.EXC",
        "PERCENTILE.INC",
        "PERCENTILEX.EXC",
        "PERCENTILEX.INC",
        "PI",
        "PMT",
        "POWER",
        "PREVIOUSDAY",
        "PREVIOUSMONTH",
        "PREVIOUSQUARTER",
        "PREVIOUSYEAR",
        "PRODUCT",
        "PRODUCTX",
        "QUARTER",
        "QUOTIENT",
        "RADIANS",
        "RANK",
        "RANKX",
        "RELATED",
        "RELATEDTABLE",
        "REMOVEFILTERS",
        "REPLACE",
        "REPT",
        "RIGHT",
        "ROUND",
        "ROUNDDOWN",
        "ROUNDUP",
        "ROW",
        "ROWNUMBER",
        "SAMEPERIODLASTYEAR",
        "SEARCH",
        "SECOND",
        "SELECTCOLUMNS",
        "SELECTEDMEASURE",
        "SELECTEDMEASUREFORMATSTRING",
        "SELECTEDMEASURENAME",
        "SELECTEDVALUE",
        "SIGN",
        "SIN",
        "SINH",
        "SQRT",
        "SQRTPI",
        "STARTOFMONTH",
        "STARTOFQUARTER",
        "STARTOFYEAR",
        "STDEV.P",
        "STDEV.S",
        "STDEVX.P",
        "STDEVX.S",
        "SUBSTITUTE",
        "SUBSTITUTEWITHINDEX",
        "SUM",
        "SUMMARIZE",
        "SUMMARIZECOLUMNS",
        "SUMX",
        "SWITCH",
        "TAN",
        "TANH",
        "TIME",
        "TIMEVALUE",
        "TODAY",
        "TOPN",
        "TOTALMTD",
        "TOTALQTD",
        "TOTALYTD",
        "TREATAS",
        "TRIM",
        "TRUE",
        "TRUNC",
        "UNICHAR",
        "UNICODE",
        "UNION",
        "UPPER",
        "USERELATIONSHIP",
        "UTCNOW",
        "UTCTODAY",
        "VALUE",
        "VALUES",
        "VAR.P",
        "VAR.S",
        "VARX.P",
        "VARX.S",
        "WEEKDAY",
        "WEEKNUM",
        "WINDOW",
        "XIRR",
        "XNPV",
        "YEAR",
        "YEARFRAC",
    }
)


def dax_function_names(expression: str) -> set[str]:
    """Return normalized function-call names, ignoring DAX string literals."""
    scrubbed = re.sub(r'"(?:[^"]|"")*"', '""', expression)
    return {match.group("name").upper() for match in _DAX_FUNCTION_PATTERN.finditer(scrubbed)}


def unsupported_dax_functions(expression: str) -> set[str]:
    """Return function names outside the reviewed local compiler catalog."""
    return dax_function_names(expression) - _SUPPORTED_DAX_FUNCTIONS


def dax_variable_errors(expression: str) -> list[str]:
    """Reject known invalid VAR assignments without parsing native DAX constructs.

    Measure definition headers may use ``:=``; VAR declarations require ``=``.
    Identifiers, strings and comments cannot introduce an executable declaration.
    This deliberately leaves the variable body and native function support alone.
    """
    tokens = r'/\*.*?\*/|//[^\n]*|--[^\n]*|"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'|\[(?:[^\]]|\]\])*\]'
    masked = re.sub(tokens, lambda match: " " * len(match.group()), expression, flags=re.S)
    errors = []
    for match in re.finditer(r"(?<![\w.])VAR\s+([A-Za-z_][A-Za-z_0-9]*)\s*(:=|==)", masked, re.I):
        errors.append(
            f"VAR {match.group(1)} requires '=' for its assignment, not '{match.group(2)}'."
        )
    return errors


def requested_dax_constructs(request: str) -> list[str]:
    """Extract explicit implementation requirements, not incidental mentions.

    Planning and validation share this contract so an instruction such as "use
    SUMX" is visible to the model before the same requirement is enforced.
    """
    requested: set[str] = set()
    clauses = re.finditer(
        r"\b(?:use|using|with)\s+(?:both\s+|the\s+)?"
        r"(?P<names>(?:VAR|SUMX)(?:\s*(?:,|and|&)\s*(?:VAR|SUMX))*)\b",
        request,
        re.I,
    )
    for match in clauses:
        prefix = request[max(0, match.start() - 16) : match.start()]
        if re.search(r"\b(?:not|never|avoid|without|don't)\s*$", prefix, re.I):
            continue
        requested.update(re.findall(r"\b(?:VAR|SUMX)\b", match["names"].upper()))
    return sorted(requested)


def dax_requested_construct_errors(request: str, expression: str) -> list[str]:
    """Preserve explicit positive requests to use VAR/SUMX in proposal generation.

    This is a request contract, not a restriction on the general DAX language.
    Only an explicit use/using/with clause naming these constructs activates it.
    """
    requested = set(requested_dax_constructs(request))
    if not requested:
        return []
    tokens = r'/\*.*?\*/|//[^\n]*|--[^\n]*|"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'|\[(?:[^\]]|\]\])*\]'
    masked = re.sub(tokens, lambda match: " " * len(match.group()), expression, flags=re.S)
    actual = set()
    if re.search(r"(?<![\w.])VAR\s+[A-Za-z_][A-Za-z_0-9]*\s*=(?!=)", masked, re.I):
        actual.add("VAR")
    if re.search(r"\bSUMX\s*\(", masked, re.I):
        actual.add("SUMX")
    missing = requested - actual
    if not missing:
        return []
    return [
        "Missing explicitly requested DAX constructs: "
        + ", ".join(sorted(missing))
        + ". Requested: "
        + ", ".join(sorted(requested))
        + "; actual: "
        + (", ".join(sorted(actual)) or "none")
        + "."
    ]


def dax_argument_errors(expression: str) -> list[str]:
    """Reject known expression boundaries, VAR syntax and column aggregate arguments.

    The masked scan ignores strings, identifiers and comments when matching calls,
    while argument validation uses the original column reference. MIN/MAX are not
    included: their two-scalar overloads are valid.
    """
    token_pattern = (
        r'/\*.*?\*/|//[^\n]*|--[^\n]*|"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'|\[(?:[^\]]|\]\])*\]'  # noqa: S105 - regex pattern, not a credential
    )
    # Remove comments without mistaking comment markers inside names or strings for comments.
    without_comments = re.sub(
        token_pattern,
        lambda m: " " * len(m.group()) if m.group().startswith(("/*", "//", "--")) else m.group(),
        expression,
        flags=re.S,
    )
    masked = re.sub(
        token_pattern,
        lambda m: " " * len(m.group()),
        expression,
        flags=re.S,
    )
    column = re.compile(r"(?:(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_ ]*)\s*)?\[(?:[^\]]|\]\])+\]")
    errors = dax_variable_errors(expression)
    for match in re.finditer(r"\)\s*([A-Za-z][A-Za-z0-9_.]*)\s*\(", masked):
        name = match.group(1).upper()
        # RETURN ends a VAR expression; IN is a binary operator whose right-hand
        # table expression may be parenthesized. Neither is an adjacent function.
        if name in {"RETURN", "IN"}:
            continue
        errors.append(
            f"DAX call {name} cannot follow a closed expression without an operator or "
            "argument separator. Put a table filter inside the iterator's table expression "
            "or a CALCULATE argument."
        )
    for match in re.finditer(
        r"\b(SUM|AVERAGE|COUNT|COUNTA|DISTINCTCOUNT|DISTINCTCOUNTNOBLANK|MINA|MAXA)\s*\(",
        masked,
        re.I,
    ):
        depth = 1
        end = match.end()
        while end < len(masked) and depth:
            depth += (masked[end] == "(") - (masked[end] == ")")
            end += 1
        if depth:
            continue  # Existing delimiter validator owns malformed expressions.
        argument = without_comments[match.end() : end - 1].strip()
        if not column.fullmatch(argument):
            errors.append(
                f"{match.group(1).upper()} requires one column argument, not an expression. "
                "For a row-wise expression use an appropriate iterator such as SUMX."
            )
    return errors
