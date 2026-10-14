"""Full DAX expression and query grammar for the local engine (``dax_engine``).

The lab's own evaluator (``dax.py``) accepts a small governed subset on purpose. An
imported report's measures use the whole language (VAR blocks, iterators, time
intelligence, table constructors), so the engine parses DAX as Power BI does:
operators by precedence, ``'Table'[Column]`` and ``[Measure]`` references, and
``DEFINE``/``EVALUATE``/``ORDER BY`` queries. Parsing never evaluates anything.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field


class DaxParseError(ValueError):
    """The text is not valid DAX."""


# ---------------------------------------------------------------- syntax tree


@dataclass(frozen=True)
class Num:
    value: float | int


@dataclass(frozen=True)
class Str:
    value: str


@dataclass(frozen=True)
class ColRef:
    """``'Table'[Column]``, ``Table[Column]``, ``''[Value]`` or a bare ``[Name]``.

    ``table`` is None for a bare reference: a measure, or a column of the current row.
    """

    table: str | None
    column: str


@dataclass(frozen=True)
class Name:
    """An unquoted identifier: a variable, or a table name."""

    name: str
    quoted: bool = False


@dataclass(frozen=True)
class Call:
    name: str  # upper case
    args: tuple[Node, ...]


@dataclass(frozen=True)
class Binary:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Unary:
    op: str  # "-", "+" or "NOT"
    operand: Node


@dataclass(frozen=True)
class TableCtor:
    rows: tuple[tuple[Node, ...], ...]


@dataclass(frozen=True)
class VarBlock:
    variables: tuple[tuple[str, Node], ...]
    body: Node


@dataclass(frozen=True)
class Keyword:
    """ASC/DESC/SKIP/DENSE and interval names used as bare arguments."""

    word: str  # upper case


Node = Num | Str | ColRef | Name | Call | Binary | Unary | TableCtor | VarBlock | Keyword


@dataclass
class QueryMeasure:
    table: str
    name: str
    expression: Node


@dataclass
class Evaluate:
    table: Node
    order_by: list[tuple[Node, bool]] = field(default_factory=list)  # (expr, ascending)


@dataclass
class Query:
    measures: list[QueryMeasure] = field(default_factory=list)
    variables: list[tuple[str, Node]] = field(default_factory=list)
    evaluates: list[Evaluate] = field(default_factory=list)


# ------------------------------------------------------------------- tokens

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|//[^\n]*|--[^\n]*|/\*.*?\*/)
  | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<table>'(?:[^']|'')*')
  | (?P<bracket>\[(?:[^\]]|\]\])*\])
  | (?P<ident>[A-Za-z_À-￿][\w.À-￿]*)
  | (?P<op>&&|\|\||<>|<=|>=|==|[=<>+\-*/^&(),{};])
    """,
    re.S | re.X,
)

_KEYWORDS = {"ASC", "DESC", "SKIP", "DENSE"}
_INTERVALS = {"DAY", "MONTH", "QUARTER", "YEAR", "WEEK", "HOUR", "MINUTE", "SECOND"}
_BOOLEAN_WORDS = {"TRUE": True, "FALSE": False}


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    position: int


def tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise DaxParseError(f"Unexpected character {text[position]!r} at {position}")
        kind = match.lastgroup or ""
        if kind != "ws":
            tokens.append(_Token(kind, match.group(), position))
        position = match.end()
    return tokens


def _unquote_table(text: str) -> str:
    return text[1:-1].replace("''", "'")


def _unbracket(text: str) -> str:
    return text[1:-1].replace("]]", "]")


# ------------------------------------------------------------------- parser


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = tokenize(text)
        self.index = 0

    # token helpers
    def peek(self, offset: int = 0) -> _Token | None:
        at = self.index + offset
        return self.tokens[at] if at < len(self.tokens) else None

    def at(self, text: str, offset: int = 0) -> bool:
        token = self.peek(offset)
        return token is not None and token.kind in {"op", "ident"} and token.text.upper() == text

    def take(self, text: str | None = None) -> _Token:
        token = self.peek()
        if token is None:
            raise DaxParseError(f"Expected {text or 'more text'} at the end of the formula")
        if text is not None and token.text.upper() != text:
            raise DaxParseError(f"Expected {text!r}, found {token.text!r}")
        self.index += 1
        return token

    def done(self) -> bool:
        return self.index >= len(self.tokens)

    # expressions, lowest precedence first
    def expression(self) -> Node:
        if self.at("VAR"):
            return self.var_block()
        return self.or_expr()

    def var_block(self) -> Node:
        variables: list[tuple[str, Node]] = []
        while self.at("VAR"):
            self.take("VAR")
            name = self.take()
            if name.kind != "ident":
                raise DaxParseError(f"Expected a variable name, found {name.text!r}")
            self.take("=")
            variables.append((name.text, self.expression()))
        self.take("RETURN")
        return VarBlock(tuple(variables), self.expression())

    def or_expr(self) -> Node:
        node = self.and_expr()
        while self.at("||"):
            self.take()
            node = Binary("||", node, self.and_expr())
        return node

    def and_expr(self) -> Node:
        node = self.not_expr()
        while self.at("&&"):
            self.take()
            node = Binary("&&", node, self.not_expr())
        return node

    def not_expr(self) -> Node:
        # NOT without parentheses is the operator; NOT( ... ) is also the function form,
        # which parses the same way.
        if self.at("NOT") and not self.at("(", 1):
            self.take()
            return Unary("NOT", self.not_expr())
        return self.comparison()

    def comparison(self) -> Node:
        node = self.concat()
        while True:
            token = self.peek()
            if token is None:
                return node
            text = token.text.upper()
            if token.kind == "op" and text in {"=", "==", "<>", "<", ">", "<=", ">="}:
                self.take()
                node = Binary(text, node, self.concat())
            elif token.kind == "ident" and text == "IN":
                self.take()
                node = Binary("IN", node, self.concat())
            elif token.kind == "ident" and text == "NOT" and self.at("IN", 1):
                self.take()
                self.take()
                node = Unary("NOT", Binary("IN", node, self.concat()))
            else:
                return node

    def concat(self) -> Node:
        node = self.additive()
        while self.at("&"):
            self.take()
            node = Binary("&", node, self.additive())
        return node

    def additive(self) -> Node:
        node = self.multiplicative()
        while self.at("+") or self.at("-"):
            op = self.take().text
            node = Binary(op, node, self.multiplicative())
        return node

    def multiplicative(self) -> Node:
        node = self.unary()
        while self.at("*") or self.at("/"):
            op = self.take().text
            node = Binary(op, node, self.unary())
        return node

    def unary(self) -> Node:
        if self.at("-") or self.at("+"):
            op = self.take().text
            return Unary(op, self.unary())
        return self.power()

    def power(self) -> Node:
        node = self.primary()
        if self.at("^"):
            self.take()
            return Binary("^", node, self.unary())
        return node

    def primary(self) -> Node:
        token = self.take()
        if token.kind == "num":
            text = token.text
            is_int = re.fullmatch(r"\d+", text) is not None
            return Num(int(text) if is_int else float(text))
        if token.kind == "str":
            return Str(token.text[1:-1].replace('""', '"'))
        if token.kind == "bracket":
            return ColRef(None, _unbracket(token.text))
        if token.kind == "table":
            name = _unquote_table(token.text)
            nxt = self.peek()
            if nxt is not None and nxt.kind == "bracket":
                self.take()
                return ColRef(name, _unbracket(nxt.text))
            return Name(name, quoted=True)
        if token.kind == "op" and token.text == "(":
            node = self.expression()
            if self.at(","):
                # A row of a table constructor written without braces is not DAX.
                raise DaxParseError("Unexpected ',' inside parentheses")
            self.take(")")
            return node
        if token.kind == "op" and token.text == "{":
            return self.table_ctor()
        if token.kind == "ident":
            upper = token.text.upper()
            nxt = self.peek()
            if nxt is not None and nxt.kind == "op" and nxt.text == "(":
                self.take("(")
                args: list[Node] = []
                if not self.at(")"):
                    args.append(self.argument())
                    while self.at(","):
                        self.take()
                        args.append(self.argument())
                self.take(")")
                return Call(upper, tuple(args))
            if nxt is not None and nxt.kind == "bracket":
                self.take()
                return ColRef(token.text, _unbracket(nxt.text))
            if upper in _BOOLEAN_WORDS:
                return Call(upper, ())
            if upper in _KEYWORDS or upper in _INTERVALS:
                return Keyword(upper)
            return Name(token.text)
        raise DaxParseError(f"Unexpected {token.text!r}")

    def argument(self) -> Node:
        # Empty arguments are allowed in DAX (RANKX(t, e, , DESC)).
        if self.at(",") or self.at(")"):
            return Name("")
        return self.expression()

    def table_ctor(self) -> Node:
        rows: list[tuple[Node, ...]] = []
        if self.at("}"):
            self.take()
            return TableCtor(())
        while True:
            if self.at("(") and self._tuple_ahead():
                self.take("(")
                values = [self.expression()]
                while self.at(","):
                    self.take()
                    values.append(self.expression())
                self.take(")")
                rows.append(tuple(values))
            else:
                rows.append((self.expression(),))
            if self.at(","):
                self.take()
                continue
            self.take("}")
            return TableCtor(tuple(rows))

    def _tuple_ahead(self) -> bool:
        """Whether '(' opens a (a, b) row rather than a parenthesized expression."""
        depth = 0
        for token in self.tokens[self.index :]:
            if token.kind != "op":
                continue
            if token.text in "({":
                depth += 1
            elif token.text in ")}":
                depth -= 1
                if depth == 0:
                    return False
            elif token.text == "," and depth == 1:
                return True
        return False

    # queries
    def query(self) -> Query:
        query = Query()
        if self.at("DEFINE"):
            self.take()
            while self.at("MEASURE") or self.at("VAR"):
                if self.take().text.upper() == "MEASURE":
                    ref = self.primary()
                    if not isinstance(ref, ColRef) or ref.table is None:
                        raise DaxParseError("DEFINE MEASURE needs 'Table'[Measure name]")
                    self.take("=")
                    query.measures.append(QueryMeasure(ref.table, ref.column, self.expression()))
                else:
                    name = self.take().text
                    self.take("=")
                    query.variables.append((name, self.expression()))
        while self.at("EVALUATE"):
            self.take()
            evaluate = Evaluate(self.expression())
            if self.at("ORDER") and self.at("BY", 1):
                self.take()
                self.take()
                while True:
                    expr = self.expression()
                    ascending = True
                    if self.at("ASC") or self.at("DESC"):
                        ascending = self.take().text.upper() == "ASC"
                    evaluate.order_by.append((expr, ascending))
                    if not self.at(","):
                        break
                    self.take()
            query.evaluates.append(evaluate)
        if not query.evaluates:
            raise DaxParseError("A query needs EVALUATE")
        return query


def parse_expression(text: str) -> Node:
    """One DAX expression, such as a measure's formula."""
    parser = _Parser(text)
    if parser.done():
        raise DaxParseError("The formula is empty")
    node = parser.expression()
    if not parser.done():
        extra = parser.peek()
        raise DaxParseError(f"Unexpected {extra.text if extra else ''!r} after the formula")
    return node


def parse_query(text: str) -> Query:
    """A DAX query: optional DEFINE, one or more EVALUATE with ORDER BY."""
    parser = _Parser(text)
    query = parser.query()
    if not parser.done():
        extra = parser.peek()
        raise DaxParseError(f"Unexpected {extra.text if extra else ''!r} after the query")
    return query


def is_query(text: str) -> bool:
    return re.match(r"\s*(?:DEFINE|EVALUATE)\b", text, re.I) is not None


def _quote_table(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _bracket(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def to_dax(node: Node) -> str:
    """DAX text for a syntax tree; parse_expression(to_dax(n)) gives n back."""
    if isinstance(node, Num):
        return repr(node.value)
    if isinstance(node, Str):
        return '"' + node.value.replace('"', '""') + '"'
    if isinstance(node, ColRef):
        prefix = "" if node.table is None else _quote_table(node.table)
        return prefix + _bracket(node.column)
    if isinstance(node, Name):
        return _quote_table(node.name) if node.quoted else node.name
    if isinstance(node, Keyword):
        return node.word
    if isinstance(node, Call):
        return f"{node.name}({', '.join(to_dax(a) for a in node.args)})"
    if isinstance(node, Binary):
        return f"({to_dax(node.left)} {node.op} {to_dax(node.right)})"
    if isinstance(node, Unary):
        return (
            f"NOT ({to_dax(node.operand)})"
            if node.op == "NOT"
            else f"{node.op}({to_dax(node.operand)})"
        )
    if isinstance(node, TableCtor):
        rows = [
            to_dax(row[0]) if len(row) == 1 else "(" + ", ".join(to_dax(v) for v in row) + ")"
            for row in node.rows
        ]
        return "{" + ", ".join(rows) + "}"
    if isinstance(node, VarBlock):
        parts = [f"VAR {name} = {to_dax(value)}" for name, value in node.variables]
        return " ".join([*parts, "RETURN", to_dax(node.body)])
    raise DaxParseError(f"Cannot print {node!r}")


def replace_nodes(node: Node, change: Callable[[Node], Node | None]) -> Node:
    """A copy of the tree where change(n) replaces every n it returns a node for."""
    replaced = change(node)
    if replaced is not None:
        return replaced
    if isinstance(node, Call):
        return Call(node.name, tuple(replace_nodes(a, change) for a in node.args))
    if isinstance(node, Binary):
        return Binary(node.op, replace_nodes(node.left, change), replace_nodes(node.right, change))
    if isinstance(node, Unary):
        return Unary(node.op, replace_nodes(node.operand, change))
    if isinstance(node, TableCtor):
        return TableCtor(tuple(tuple(replace_nodes(v, change) for v in row) for row in node.rows))
    if isinstance(node, VarBlock):
        return VarBlock(
            tuple((name, replace_nodes(value, change)) for name, value in node.variables),
            replace_nodes(node.body, change),
        )
    return node
