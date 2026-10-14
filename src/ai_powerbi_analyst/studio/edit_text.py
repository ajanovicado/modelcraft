"""Words and numbers in an edit request: sentences, clauses, colors, amounts, fonts.

The parser reads a request by slots (a target phrase, a property word and a value) rather
than by grammar, so "make the title blue", "the title should be blue" and "could the title
be blue?" all reach the same slots. Everything here is deterministic and model-free.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import StrEnum
from functools import cache
from typing import Any, cast

from . import pbir_expr as expr

# --- cleaning and tokens -----------------------------------------------------------------

_INVISIBLE = re.compile(r"[​-‏⁠﻿]+")
_QUOTE_MAP = str.maketrans(
    {"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "–": "-"} | {"—": "-", " ": " "}
)


def clean(message: str) -> str:
    """The message with curly quotes and odd spaces made plain, and whitespace collapsed."""
    text = unicodedata.normalize("NFKC", _INVISIBLE.sub("", message)).translate(_QUOTE_MAP)
    return " ".join(text.split())


def _stem(word: str) -> str:
    """Drop a plural s so "agents" finds "Agent" ("sales" and "sale" agree too)."""
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    """Lower-case words of a name; CamelCase splits ("ChannelName" -> channel, name)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", spaced.casefold())]


_QUOTED = re.compile(r"\"([^\"]{1,200})\"|(?<![\w])'([^']{1,200})'(?![\w])")


def quoted(text: str) -> list[tuple[int, int, str]]:
    """(start, end, inner text) of each quoted phrase; an apostrophe inside a word is no quote."""
    return [(m.start(), m.end(), m.group(1) or m.group(2)) for m in _QUOTED.finditer(text)]


def mask_quotes(text: str) -> tuple[str, list[str]]:
    """The text with quoted phrases replaced by \x01<n>\x01 so they survive splitting."""
    saved: list[str] = []

    def hold(match: re.Match[str]) -> str:
        saved.append(match.group(0))
        return f"\x01{len(saved) - 1}\x01"

    return _QUOTED.sub(hold, text), saved


def unmask_quotes(text: str, saved: list[str]) -> str:
    return re.sub(r"\x01(\d+)\x01", lambda m: saved[int(m.group(1))], text)


# --- literal text ---------------------------------------------------------------------------
#
# What a person types as a NEW name or title ("rename the chart to "Delete log"") is data.
# The recognizers read verbs and nouns anywhere in a clause, so a literal that contains
# "delete", "hide" or "undo" would be read as that command. Literals are therefore swapped
# for an inert word before anything is read and put back into the answer at the end.

LITERAL_MARK = re.compile(r"qzlit([a-z]{1,3})qz", re.I)


@cache
def _risky_words() -> re.Pattern[str]:
    """Words that would read as a command if they sat in a name (built on first use, because
    EDIT_VERBS is defined further down)."""
    return re.compile(
        rf"\b(?:{EDIT_VERBS}|undo|redo|restore|revert|reset|discard|kill|zap|ditch|scrap|lose|"
        r"cancel|what\s+changed|what\s+did)\b",
        re.I,
    )


def _letters(number: int) -> str:
    out = ""
    number += 1
    while number:
        number, rest = divmod(number - 1, 26)
        out = chr(97 + rest) + out
    return out


def hold_literal(saved: list[str], literal: str) -> str:
    saved.append(literal)
    return f'"qzlit{_letters(len(saved) - 1)}qz"'


def refers_to_a_name(inner: str, names: Sequence[str]) -> bool:
    """Whether a quoted phrase is (a run of the words of) a name that exists in the report."""
    wanted = tokens(inner)
    if not wanted:
        return False
    needle = " " + " ".join(wanted) + " "
    return any(needle in " " + " ".join(tokens(name)) + " " for name in names)


def mask_quoted_literals(text: str, names: Sequence[str], saved: list[str]) -> str:
    """Quoted text that is not a name in the report and reads like a command is held back.

    A quote that names a visual, page or field stays (it is how a person points at one);
    "green" or "bold" in quotes stays too, because nothing in it can be read as a command.
    """

    def hold(match: re.Match[str]) -> str:
        inner = match.group(1) or match.group(2)
        if not _risky_words().search(inner) or refers_to_a_name(inner, names):
            return match.group(0)
        return hold_literal(saved, inner)

    return _QUOTED.sub(hold, text)


_OBJECT = (
    r"(?:it|them|(?:the\s+|this\s+|that\s+)?(?:[\w'-]+\s+){0,3}?(?:charts?|graphs?|cards?|tables?|"
    r"matri(?:x|ces)|slicers?|visuals?|kpis?|gauges?|maps?|tiles?|pages?|tabs?))"
)
_TAIL_CUES = re.compile(
    r"(?:\b(?:rename|retitle)\b.*?\b(?:to|as)\s+"
    rf"|\b(?:call|name|title|retitle|caption|label)\s+{_OBJECT}\s+(?:to|as)\s+"
    # "title it Sales" is the request itself only at the start of a clause
    rf"|(?:^|[,;]\s*|\b(?:and|then|also|please)\s+)(?:call|name|title|retitle)\s+{_OBJECT}\s+"
    r"|\b(?:titled|called|named)\s+"
    r"|\b(?:title|heading|caption)(?:\s+text)?\s+(?:to|as|=|:|should\s+(?:say|read)|says?|reads?)\s+)",
    re.I,
)
_PREDICATE = re.compile(r"^(?:should|must|needs?|is|are|has|have|be)\b", re.I)
_NEW_MEASURE_CALCULATION = re.compile(
    r"\b(?:create|add|new|make|define|build)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:dax\s+)?"
    r"measure\s+(?:called|named|titled)\s+"
    # (a word after that/which written with a capital is still the name: "Sales that Matter")
    r"(?P<name>\"[^\"]+\"|'[^']+'|(?!')[^\"]+?)\s+(?:that|which)\s+(?-i:(?![A-Z][a-z]))"
    r"(?P<goal>\S.*)$",
    re.I,
)
# What makes the words after that/which a calculation ("is total DiscountAmount divided by
# Total Sales", "counts the orders", "sums SalesAmount"), not a remark ("I can use later",
# "is important", "we need for the board").
_CALCULATION_WORDS = re.compile(
    r"\b(?:sums?|summ(?:ed|ing)|totals?|totall?(?:ed|ing)|counts?|counting|counted|"
    r"averages?|averag(?:ed|ing)|avg|mean|median|min(?:imum)?|max(?:imum)?|divid(?:e|es|ed|ing)|"
    r"over|ratio|share|percent(?:age)?|proportion|times|multipl(?:y|ies|ied|ying)|minus|"
    r"subtract(?:s|ed|ing)?|less|plus|add(?:s|ed|ing)?\s+(?:up|together)|difference|growth|"
    r"change|ytd|mtd|qtd|year[\s-]to[\s-]date|month[\s-]to[\s-]date|running|cumulative|"
    r"distinct|number\s+of|rate|margin|per|rank(?:s|ing)?|compar(?:e|es|ed|ing)|"
    r"previous|prior|last\s+year|same\s+period)\b|[-+*/%]",
    re.I,
)


def describes_calculation(text: str) -> bool:
    """Whether the words say how something is calculated ("sums SalesAmount", "divided by
    Total Sales", "the number of orders"), not only what it is called."""
    return _CALCULATION_WORDS.search(text) is not None


def new_measure_calculation(text: str) -> tuple[str, str] | None:
    """The explicit new measure name and the calculation described after that/which.

    Keeping this boundary shared prevents a description from becoming a literal name
    before parsing, or becoming the name when the local planner omits it. Words after
    that/which that describe no calculation ("that I can use later") are no calculation:
    the request then only names the measure, and asks what it calculates.
    """
    match = _NEW_MEASURE_CALCULATION.search(text)
    if match is None:
        return None
    goal = match.group("goal").strip().rstrip(".!?")
    if not describes_calculation(goal):
        return None
    return match.group("name").strip("\"' "), goal


def mask_tail_literal(sentence: str, names: Sequence[str], saved: list[str]) -> str:
    """The new name after "rename it to", "call it", "titled" when it reads like a command.

    The name runs to the end of the sentence, or to a comma or "and" that starts a request
    of its own ("rename it to Sales and make it green").
    """
    cue = _TAIL_CUES.search(sentence)
    if cue is None:
        return sentence
    tail = sentence[cue.end() :]
    if tail.lstrip().startswith('"qzlit') or not tail.strip() or _PREDICATE.match(tail.lstrip()):
        return sentence
    end = len(tail)
    measure = _NEW_MEASURE_CALCULATION.search(sentence)
    if measure is not None and measure.start("name") == cue.end():
        # A calculation after the name remains readable; only the name is literal text.
        end = measure.end("name") - cue.end()
    elif re.search(
        r"\b(?:create|add|new|make|define|build)\b.*\bmeasure\b",
        sentence[: cue.end()],
        re.I,
    ):
        definition = re.search(
            r"\s*(?:=|:)\s*|\s+(?:as|using|with\s+(?:the\s+)?(?:formula|expression|dax))\s+"
            r"(?=.*[\[(])",
            tail,
            re.I,
        )
        if definition is not None:
            end = definition.start()
    for joint in _SEPARATOR.finditer(tail):
        if _starts_clause(tail[joint.end() :], names):
            end = min(end, joint.start())
            break
    literal = tail[:end].strip().rstrip(".!?").strip()
    if not literal or not _risky_words().search(literal) or refers_to_a_name(literal, names):
        return sentence
    return sentence[: cue.end()] + hold_literal(saved, literal) + tail[end:]


def restore_literals[T](value: T, saved: Sequence[str]) -> T:
    """``value`` with each held-back literal put back (strings and what holds strings)."""
    if not saved:
        return value
    return cast(T, _restore(value, saved))


def _restore(value: Any, saved: Sequence[str]) -> Any:
    if isinstance(value, str):
        if "qz" not in value.casefold():
            return value
        return LITERAL_MARK.sub(lambda m: _held(m, saved), value)
    if isinstance(value, tuple):
        return tuple(_restore(v, saved) for v in value)
    if isinstance(value, list):
        return [_restore(v, saved) for v in value]
    if isinstance(value, dict):
        return {k: _restore(v, saved) for k, v in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        changes = {f.name: _restore(getattr(value, f.name), saved) for f in fields(value)}
        return replace(value, **changes)
    return value


def _held(match: re.Match[str], saved: Sequence[str]) -> str:
    number = 0
    for char in match.group(1).casefold():
        number = number * 26 + (ord(char) - 96)
    index = number - 1
    return saved[index] if 0 <= index < len(saved) else match.group(0)


# --- verbs and objects ---------------------------------------------------------------------

EDIT_VERBS = (
    "make|turn|set|change|switch|convert|translate|update|modify|adjust|edit|use|apply|give|paint|"
    "colou?r|recolou?r|shade|fill|highlight|style|restyle|format|add|insert|create|include|"
    "build|place|put|show|display|enable|disable|remove|delete|drop|erase|clear|hide|unhide|"
    "move|shift|nudge|push|slide|drag|position|reposition|relocate|resize|enlarge|shrink|"
    "widen|narrow|stretch|expand|increase|decrease|reduce|raise|lower|bump|boost|rename|"
    "retitle|call|name|duplicate|copy|clone|swap|exchange|align|center|centre|sort|order|"
    "rank|bring|send|lift|spread|space|distribute|arrange|lay|replace|round|bold|italicize|"
    "underline|stack|fix|tweak|resize|scale|wrap|band|"
    "shorten|lengthen|thicken|thin|brighten|darken|lighten|soften|enlarge|blow|double|"
    "halve|tidy|clean|polish|improve|beautify|modernize|modernise|refresh|spruce|"
    "group|ungroup|lock|unlock|filter|limit|restrict|exclude|keep|only|stop|start|stretch|trim|"
    "line\\s+up|draw|"
    "sync|connect|link|split|merge|pin|freeze|unfreeze|publish|share|export|reload|embed|"
    "schedule|import|expose|hook|left[\\s-]?align|right[\\s-]?align|justify|rotate|tilt|"
    "angle|flip|get\\s+rid\\s+of|take\\s+out|kill|zap|ditch|scrap|lose|trash|toss|nuke|"
    "undo|redo|revert|restore|reset|rollback|roll\\s+back|discard|redefine|rewrite|"
    # "title it Sales" is a request; "title of the card should be bold" is a statement
    "title(?!\\s+(?:of|should|must|is|are|needs?|has|to|font|text|colou?r|size|alignment|bold)\\b)"
)
_VERB_START = re.compile(rf"^(?:{EDIT_VERBS})\b", re.I)

_FILLER = re.compile(
    r"^(?:(?:please|pls|plz|kindly|ok(?:ay)?|now|also|then|and|so|hey|actually|just|next|"
    r"alright|well|hi|hello|thanks|thank you|first|finally|lastly)\b[,.!]?\s*)+",
    re.I,
)
_POLITE = re.compile(
    r"^(?:"
    r"(?:can|could|would|will|may)\s+(?:you|we|i|someone)\s+(?:please\s+)?"
    r"|(?:i|we)(?:'d|\s+would)\s+(?:really\s+)?(?:like|love|prefer)\s+(?:you\s+to\s+|to\s+)?"
    r"|(?:i|we)\s+(?:really\s+)?(?:want|need|wish)\s+(?:you\s+to\s+|to\s+)?"
    r"|let'?s\s+|let\s+us\s+|why\s+(?:don'?t|not)\s+(?:we|you|i)\s+|maybe\s+(?:we\s+|you\s+)?"
    r"(?:could|should|can)\s+|make\s+sure\s+(?:that\s+)?"
    r"|it\s+would\s+be\s+(?:great|nice|good|better|helpful)\s+(?:if\s+you\s+could\s+|to\s+)"
    r"|(?:would\s+it\s+be\s+possible|is\s+it\s+possible|is\s+there\s+a\s+way)\s+to\s+"
    r"|(?:any\s+chance|do\s+you\s+think)\s+(?:you\s+)?(?:can|could)\s+"
    r"|(?:go\s+ahead\s+and|feel\s+free\s+to|try\s+to|you\s+should|you\s+need\s+to|"
    r"you\s+can|you\s+could|please)\s+"
    r")",
    re.I,
)

# What a request must be about to count as an edit of the report, not a data question.
REPORT_NOUNS = re.compile(
    r"\b(?:page|tab|visual|chart|graph|card|kpi|slicer|title|heading|measure|formula|target|"
    r"threshold|matrix|report|dashboard|column|colou?r|format|label|axis|axes|legend|theme|"
    r"palette|font|bookmark|tooltip|filter|relationship|gauge|map|pie|donut|bar|line|"
    r"scatter|treemap|funnel|waterfall|text ?box|image|logo|button|layout|table|text|size|"
    r"gridline|grid line|shadow|corner|border|outline|background|tile|panel|header|number|"
    r"value|figure|series|slice|bubble|width|height|position|it|this|that|them|these|those|"
    r"everything|everywhere|forward|backward|backwards|shape|rectangle|square|circle|oval|"
    r"ellipse|triangle|arrow|hexagon|octagon|pentagon|heart|trapezoid|parallelogram|"
    r"speech bubble|pill)s?\b",
    re.I,
)

# Parts of a visual a request may name instead of the visual itself ("hide the legend").
# (and the parts the tool families added: "remove the frame around the slicer" or "hide the
# totals in the table" names a part, never the whole visual)
PROPERTY_NOUNS = re.compile(
    r"\b(?:legend|titles?|headings?|captions?|labels?|axis|axes|grid\s*lines?|borders?|outlines?|"
    r"shadows?|backgrounds?|headers?|corners?|data\s+labels?|subtitles?|frames?|"
    r"(?<!text\s)(?<!text)box(?:es)?|icons?|(?:sub)?totals?|markers?|zoom\s+sliders?|padding|"
    r"search\s+box(?:es)?|links?|small\s+multiples?|data\s+bars?|banded\s+rows?|"
    r"running\s+(?:totals?|sums?)|moving\s+averages?|tool\s?tips?|alt\s+text|sparklines?|"
    r"(?:reference|average|trend|constant|target|median|min|max)\s+lines?|error\s+bars?|"
    r"(?:conditional\s+)?formatting|gradients?|visual\s+headers?|header\s+icons?)\b",
    re.I,
)
# A column, a field or a measure (not a column chart), or the data model: "hide the EmployeeKey
# column in the model" hides no visual.
FIELD_OR_MODEL = re.compile(
    r"\b(?:columns?(?!\s+(?:charts?|graphs?))|fields?|measures?)\b|"
    r"\b(?:in|from|of)\s+the\s+(?:data\s+)?model\b|\bdata\s+model\b",
    re.I,
)

_QUESTION_START = re.compile(
    r"^(?:what|which|who|whom|whose|where|when|why|how|is|are|was|were|does|do|did|has|have|"
    r"had|should|shall|am|isn't|aren't|doesn't|don't|didn't)\b",
    re.I,
)
_INFO_START = re.compile(
    r"^(?:tell|explain|describe|list|summari[sz]e|compare|calculate|analy[sz]e|walk\s+me|"
    r"help\s+me\s+understand|give\s+me\s+(?:an?\s+)?(?:overview|summary|list)|show\s+me|"
    r"give\s+me\s+the\s+(?:top|bottom|best|worst|total)|thanks?|thank\s+you|hello|hi)\b",
    re.I,
)
# What follows "can you" or "I want to" when the person wants to KNOW something, not change it.
_KNOWING = re.compile(
    r"^(?:tell|explain|describe|list|summari[sz]e|compare|calculate|analy[sz]e|know|understand|"
    r"check|verify|confirm|find\s+out|figure\s+out|learn|say|think|wonder|clarify|remind|recall|"
    r"review|look\s+at|show\s+me|see\s+(?:if|whether|what|which|how|why|who|where|when))\b",
    re.I,
)
_HOW_ABOUT = re.compile(
    r"^(?:how|what)\s+about\s+(?=(?:making|adding|using|moving|changing|"
    r"turning|putting|showing|hiding|removing|setting|giving)\b)",
    re.I,
)
_WHAT_IF = re.compile(
    r"^(?:what\s+if|what\s+would\s+happen\s+if|suppose|supposing|imagine|"
    r"how\s+would\s+(?:it|this|that)\s+look\s+if|what\s+happens\s+(?:if|when))\b|"
    r"\bwhat\s+if\b",
    re.I,
)
_LOOK_LIKE = re.compile(
    r"\b(?:look\s+like|look\s+better|look\s+good|would\s+it\s+look|would\s+that\s+look|"
    r"would\s+.{1,40}\s+look)\b",
    re.I,
)
_DECLARATIVE = re.compile(
    r"^(?P<subject>.+?)\s+(?:should|must|needs?\s+to|has\s+to|have\s+to|ought\s+to)\s+"
    r"(?:not\s+)?(?:be|become|look|show|say|read|have|use|go|move|stay|get|appear|display|"
    r"contain|include|sit|start|end)\b",
    re.I,
)
_NEEDS = re.compile(
    r"^(?P<subject>.+?)\s+(?:needs?|requires?|lacks?)\s+(?!to\b)(?:a|an|the|some|more|no)?\s*\S",
    re.I,
)
_MODAL_WISH = re.compile(
    r"^(?:can|could|would|will)\s+(?:the|this|that|these|those|it|all|both|every|each|my|our)\b"
    r".*\b(?:be|become|look|have|show)\b",
    re.I,
)
_DISLIKE = re.compile(
    r"^i\s+(?:don'?t|do\s+not)\s+like\s+(?:the\s+)?(?P<a>[a-z]+)\b.*?\b"
    r"(?:use|try|switch\s+to|go\s+with|make\s+it)\s+(?:a\s+|an\s+)?(?P<b>[a-z]+)\b",
    re.I,
)
_DISLIKE_THEN = re.compile(
    r"^i\s+(?:don'?t|do\s+not|really\s+don'?t)\s+(?:like|want|need)\s+(?P<subject>[^,;]+?)\s*[,;]\s*"
    r"(?P<rest>(?:please\s+)?(?:make|turn|change|set|use|switch|paint|colou?r|remove|delete|hide|"
    r"drop|replace)\b.+)$",
    re.I,
)
_REMOVAL = re.compile(
    r"^(?P<subject>.+?)\s+(?:should|must|needs?\s+to|has\s+to|ought\s+to|can|could)\s+"
    r"(?:(?:be|get)\s+)?(?P<verb>deleted|removed|dropped|gone|go|erased|scrapped|hidden|hide)\b"
    r"(?:\s+(?:away|now|please))?\s*[.!]?$",
    re.I,
)
_NEGATIVE_ORDER = re.compile(
    r"^(?:please\s+)?(?:don'?t|do\s+not)\s+(?:show|display|include|need|want)\s+(?!me\b|any\s+more\b)",
    re.I,
)
_NEW_THING = re.compile(
    r"^(?:an?\s+)?new\s+(?P<what>measure|page|tab|visual|chart|card|table|slicer|matrix)\b", re.I
)
_GO_AWAY = re.compile(r"^(?:make|let)\s+(?P<subject>.+?)\s+go\s+away\s*[.!]?$", re.I)


def _removal_body(text: str) -> str | None:
    """ "the table should be deleted" -> "delete the table"; "make the chart go away" -> hide."""
    found = _REMOVAL.match(text)
    if found:
        verb = "hide" if found.group("verb").casefold() in {"hidden", "hide"} else "delete"
        return f"{verb} {found.group('subject')}"
    away = _GO_AWAY.match(text)
    return f"hide {away.group('subject')}" if away else None


_COMPLAINT_FIX = {
    "small": "bigger",
    "tiny": "bigger",
    "cramped": "bigger",
    "crowded": "bigger",
    "big": "smaller",
    "large": "smaller",
    "huge": "smaller",
    "wide": "narrower",
    "narrow": "wider",
    "dark": "lighter",
    "light": "darker",
    "pale": "darker",
    "faint": "darker",
    "dull": "brighter",
    "bright": "softer",
}
_NO_START = re.compile(
    r"^(?:no|without)\s+(?:the\s+|a\s+|an\s+|any\s+)?(?=(?:legend|title|labels?|data\s+labels?|"
    r"grid\s*lines?|border|shadow|axis|axes|background)\b)",
    re.I,
)
_COMPLAINT = re.compile(
    r"^(?P<subject>.+?)\s+(?:is|are|looks?|seems?|feels?)\s+(?:a\s+bit\s+|a\s+little\s+|"
    r"way\s+|much\s+|far\s+|really\s+|just\s+)*too\s+"
    r"(?P<adjective>small|tiny|big|large|huge|wide|narrow|dark|light|pale|bright|dull|"
    r"cramped|crowded|busy|thin|thick|plain|faint)\b",
    re.I,
)


class SentenceKind(StrEnum):
    EDIT = "edit"
    QUESTION = "question"
    WHAT_IF = "what_if"
    OTHER = "other"


@dataclass(frozen=True)
class Sentence:
    kind: SentenceKind
    text: str  # the sentence as written
    body: str  # for an edit, the request with the polite lead-in removed


def split_sentences(text: str) -> list[str]:
    """Sentences: a full stop, question or exclamation mark before a space, or a newline."""
    guarded, saved = mask_quotes(text)
    # A colon that introduces a question ends a sentence too: "Set the scene: which agents..."
    parts = re.split(
        r"(?<=[.!?])\s+(?=\S)|\n+|(?<=:)\s+(?=(?:which|what|who|whom|how|why|where|when|is|are|"
        r"do|does|did|can|could|would|will|should)\b)",
        guarded,
        flags=re.I,
    )
    return [unmask_quotes(p, saved).strip() for p in parts if p.strip()]


def classify(sentence: str) -> Sentence:
    """Whether a sentence asks to change the report, asks a question, or is a what-if.

    "Can you make the bars green?" and "could the bars be green?" are requests;
    "what color is the title?" and "would the bars look better green?" are questions, and
    a question never edits anything.
    """
    text = sentence.strip()
    trimmed = _FILLER.sub("", text).strip()
    if _WHAT_IF.search(trimmed):
        return Sentence(SentenceKind.WHAT_IF, text, trimmed)
    if _INFO_START.match(trimmed):
        return Sentence(SentenceKind.OTHER, text, "")
    body = _POLITE.sub("", trimmed, count=1).strip()
    lead_removed = body != trimmed
    if lead_removed and (_KNOWING.match(body) or _QUESTION_START.match(body)):
        # "could you tell me what the title color is?", "I want to know how many agents..."
        return Sentence(SentenceKind.QUESTION, text, "")
    if _LOOK_LIKE.search(trimmed) and not lead_removed and not _VERB_START.match(body):
        return Sentence(SentenceKind.QUESTION, text, "")
    if _NEGATIVE_ORDER.match(trimmed):
        # "don't show the legend" is an order, not a question that starts with "don't"
        return Sentence(SentenceKind.EDIT, text, trimmed)
    away = _GO_AWAY.match(body)
    if away:
        return Sentence(SentenceKind.EDIT, text, f"hide {away.group('subject')}")
    fresh = _NEW_THING.match(body)
    if fresh:
        # "new measure Foo = 1" is "create a new measure Foo = 1"
        return Sentence(SentenceKind.EDIT, text, f"create a new {body[fresh.start('what') :]}")
    how_about = _HOW_ABOUT.match(trimmed)
    if how_about:
        return Sentence(SentenceKind.EDIT, text, trimmed[how_about.end() :])
    if _VERB_START.match(body) and not (
        # "Set the scene:" or "show me ..." are not edits of a report object
        body.casefold().startswith(("show me", "give me"))
    ):
        return Sentence(SentenceKind.EDIT, text, body)
    if _MODAL_WISH.match(trimmed):
        return Sentence(SentenceKind.EDIT, text, trimmed)
    if _QUESTION_START.match(trimmed) or (trimmed.endswith("?") and not lead_removed):
        return Sentence(SentenceKind.QUESTION, text, "")
    removal = None if trimmed.endswith("?") else _removal_body(trimmed)
    if removal is not None:
        # "the table should be deleted", "the legend needs to go"
        return Sentence(SentenceKind.EDIT, text, removal)
    if _DECLARATIVE.match(trimmed) and not trimmed.endswith("?"):
        return Sentence(SentenceKind.EDIT, text, trimmed)
    if _NEEDS.match(trimmed) and REPORT_NOUNS.search(trimmed) and not trimmed.endswith("?"):
        return Sentence(SentenceKind.EDIT, text, trimmed)
    complaints = _complaint_body(trimmed)
    if complaints is not None:
        return Sentence(SentenceKind.EDIT, text, complaints)
    dislike = _DISLIKE.match(trimmed)
    if dislike:
        # "I don't like the blue, use green instead" asks to swap one color for another
        return Sentence(
            SentenceKind.EDIT, text, f"change {dislike.group('a')} to {dislike.group('b')}"
        )
    grumble = _DISLIKE_THEN.match(trimmed)
    if grumble:
        # "I don't like the green bars, make them blue": the request is the part after the comma
        rest, liked = grumble.group("rest"), grumble.group("subject").strip()
        rest = re.sub(r"\b(?:them|it|these|those)\b", liked, rest, count=1, flags=re.I)
        return Sentence(SentenceKind.EDIT, text, rest)
    if _NO_START.match(trimmed):
        # "no legend please", "without a legend"
        return Sentence(SentenceKind.EDIT, text, f"remove {_NO_START.sub('', trimmed, count=1)}")
    if lead_removed and REPORT_NOUNS.search(body):
        # "I'd like the font larger", "I want the bars green", "I want data labels on the bars"
        return Sentence(SentenceKind.EDIT, text, _wish_body(body))
    return Sentence(SentenceKind.OTHER, text, "")


def _complaint_body(text: str) -> str | None:
    """Rewrite complaints as requests: "the title is too small and the table is too wide"
    becomes "make the title bigger, make the table narrower"; None when none is a complaint."""
    pieces = re.split(r"\s*(?:,|;|\band\b)\s+", text, flags=re.I)
    rewritten: list[str] = []
    matched = False
    for piece in pieces:
        found = _COMPLAINT.match(piece)
        fix = _COMPLAINT_FIX.get(found.group("adjective").casefold()) if found else None
        if found and fix:
            rewritten.append(f"make {found.group('subject')} {fix}")
            matched = True
        else:
            rewritten.append(piece)
    return ", ".join(rewritten) if matched else None


_WISH_SEE = re.compile(r"^(?:to\s+)?(?:see|have|get)\s+", re.I)
_WISH_TOGGLE = re.compile(
    r"^(?:(?:a|an|the|some)\s+)?(?:data\s+labels?|labels?|legends?|titles?|grid\s*lines?|"
    r"borders?|shadows?|axis\s+titles?)\b",
    re.I,
)
_WISH_VALUE = re.compile(
    r"\b(?:bigger|larger|smaller|wider|narrower|taller|shorter|bold|italic|darker|lighter|"
    r"transparent|red|orange|yellow|gold|green|lime|teal|turquoise|cyan|aqua|blue|navy|purple|"
    r"violet|pink|magenta|brown|gr[ae]y|silver|black|white|maroon|crimson|coral|olive|indigo|"
    r"lavender|mint|beige)\b|\d|#[0-9a-f]{3}",
    re.I,
)


def _wish_body(body: str) -> str:
    """After "I want": "see data labels" and "data labels on the bars" mean "add data labels"."""
    if _WISH_SEE.match(body):
        return "add " + _WISH_SEE.sub("", body, count=1)
    if _WISH_TOGGLE.match(body) and not _WISH_VALUE.search(body):
        return "add " + body
    return body


# --- clause splitting ------------------------------------------------------------------------

_SEPARATOR = re.compile(
    r"(\s*;\s*|\s*,\s+(?:and\s+then\s+|and\s+also\s+|and\s+|then\s+|also\s+)?|"
    r"\s+(?:and\s+then|and\s+also|as\s+well\s+as|and|then|also|plus)\s+)",
    re.I,
)
_MAKE_LIKE = {"make", "turn", "set", "paint", "color", "colour", "change", "update", "give"}
_ADD_LIKE = {
    "add",
    "insert",
    "create",
    "include",
    "put",
    "place",
    "show",
    "display",
    "enable",
    "hide",
    "remove",
    "delete",
    "drop",
    "clear",
    "disable",
}
_NOUN_START = re.compile(
    r"^(?:(?:a|an|the|another|one\s+more|some|new)\s+)+(?:new\s+)?(?P<noun>[a-z][a-z -]{1,30})",
    re.I,
)
_VALUE_WORD = re.compile(
    r"\b(?:on|off|bigger|larger|smaller|wider|narrower|taller|shorter|bold|italic|darker|lighter|"
    r"transparent|visible|hidden|rounded|red|orange|yellow|gold|green|lime|teal|turquoise|cyan|"
    r"aqua|blue|navy|purple|violet|pink|magenta|brown|gr[ae]y|silver|black|white|maroon|crimson|"
    r"coral|salmon|olive|indigo|lavender|mint|beige|\d+(?:\.\d+)?\s*(?:pt|px|%|k|m)?|#[0-9a-f]{6})\b",
    re.I,
)
_FORMAT_NOUN = re.compile(
    r"\b(?:title|heading|legend|labels?|axis|axes|gridlines?|background|border|outline|shadow|"
    r"font|text|bars?|columns?|lines?|slices?|values?|numbers?|size|corners?|cards?|charts?|"
    r"slicers?|tables?|matri(?:x|ces)|visuals?|graphs?|kpis?|gauges?|maps?|it|them)\b",
    re.I,
)


# "keep the visuals where they are", "don't move anything", "without scaling": said about a page
# resize, it belongs to the resize clause before it and is not a request of its own.
_IN_PLACE_OBJECT = (
    r"(?:around\s+)?(?:the\s+|any\s+of\s+the\s+|my\s+)?"
    r"(?:visuals?|everything|anything|them|it|charts?)\b"
)
KEEP_IN_PLACE = re.compile(
    r"\b(?:(?:keep|leave)\s+(?:all\s+)?(?:the\s+|my\s+)?(?:visuals?|everything|charts?|it\s+all|"
    r"them|all)\s+(?:else\s+)?(?:where\s+(?:they|it)\s+(?:are|is)|as\s+(?:they\s+are|it\s+is|is)|"
    r"in\s+(?:their\s+)?place|the\s+same(?:\s+size)?|unchanged|alone|untouched|"
    r"at\s+(?:their|its)\s+(?:current|same|original)\s+(?:size|position|place)s?)|"
    r"without\s+(?:re)?scal\w+|(?:don'?t|do\s+not|never)\s+(?:re)?scal\w+|no\s+scaling|"
    rf"(?:don'?t|do\s+not|without)\s+(?:mov|resiz|shift|touch)\w*(?:\s+{_IN_PLACE_OBJECT}|\s*$))",
    re.I,
)


# "delete page 2 and clean up the buttons": what happens to the buttons and bookmarks that point at
# the deleted page. Said after a page delete it is part of that delete.
CLEANUP_AFTER_DELETE = re.compile(
    r"\b(?:clean\s*up|clear\s+up|remove|delete|fix|drop|update|get\s+rid\s+of|take\s+out)\s+"
    r"(?:all\s+)?(?:the\s+|any\s+|its\s+|their\s+)?(?:(?:related|matching|linked|dangling|broken|"
    r"leftover|old|navigation)\s+)?(?:buttons?|references?|links?|bookmarks?|navigators?|"
    r"shortcuts?|navigation)\b",
    re.I,
)
_DELETE_PAGE_BEFORE = re.compile(
    r"\b(?:delete|remove|drop|trash|get\s+rid\s+of)\b.*\b(?:page|tab)\b", re.I
)


def _starts_clause(piece: str, names: Sequence[str] = ()) -> bool:
    body = _FILLER.sub("", piece).strip()
    lowered = body.casefold()
    if KEEP_IN_PLACE.match(body):
        return False
    # "Rank" in "add a table with Employee Name, Rank and Sales" is a field, not a verb
    if any(lowered.startswith(n.casefold()) for n in names if len(n) > 2):
        return False
    # "use Arial everywhere and keep my colors": what to do with the visuals' own settings is
    # part of the theme request before it, not a request of its own
    if _KEEP_TAIL.match(body):
        return False
    # "make the value bigger and bold": a lone "bold" is a value of the clause before
    if len(body.split()) <= 2 and lowered.split()[0:1] and lowered.split()[0] in _ADJECTIVE_VERBS:
        return False
    return bool(_VERB_START.match(body)) or bool(_VERB_START.match(_POLITE.sub("", body)))


_ADJECTIVE_VERBS = frozenset({"bold", "italicize", "underline", "round", "thin", "double"})
_KEEP_TAIL = re.compile(
    r"^(?:(?:keep|leave|preserve|protect)\s+(?:all\s+)?(?:my|our|their|those|the\s+visuals?'?s?)\s+"
    r"(?:own\s+)?(?:colou?rs?|fonts?|formatting|settings?|styles?|overrides?)\b|"
    r"(?:don'?t|do\s+not)\s+(?:touch|change|override|reset|clear|remove|overwrite)\b|"
    r"(?:clear|remove|reset|wipe)\s+(?:all\s+)?(?:the\s+|my\s+)?(?:overrides?|custom\s+"
    r"(?:colou?rs?|fonts?)|manual\s+(?:colou?rs?|fonts?))|"
    r"(?:convert|translate|map|migrate|rewrite|carry\s+over)\s+(?:my|the|their)\s+"
    r"(?:old\s+|own\s+|existing\s+|custom\s+)?(?:colou?rs?|fonts?)\b)",
    re.I,
)


def _first_word(text: str) -> str:
    match = re.match(r"[A-Za-z]+", _FILLER.sub("", text).strip())
    return match.group(0).casefold() if match else ""


def split_clauses(body: str, names: Sequence[str] = ()) -> list[str]:
    """Split a request into clauses, but only where each side carries its own action.

    "make the bars green, add data labels and move the table left" is three clauses;
    "rename Sales and Profit" is one, because "Profit" is not an action. A piece with no
    verb of its own but a target and a value ("the title blue") borrows the verb before it.
    ``names`` are captions, fields and measures: a piece that starts with one is not a
    new action even when the name is also a verb ("Rank").
    """
    return [text for text, _ in split_clause_parts(body, names)]


def split_clause_parts(body: str, names: Sequence[str] = ()) -> list[tuple[str, bool]]:
    """The clauses of ``split_clauses``, each with whether it borrowed the verb before it."""
    guarded, saved = mask_quotes(body)
    parts = _SEPARATOR.split(guarded)
    # re.split keeps the separators (odd items); pieces are the even ones.
    pieces = parts[0::2]
    joiners = parts[1::2]
    clauses: list[tuple[str, bool]] = [(pieces[0], False)]
    verb = _first_word(pieces[0])
    for joiner, piece in zip(joiners, pieces[1:], strict=True):
        if not piece.strip():
            continue
        cleanup = CLEANUP_AFTER_DELETE.match(_FILLER.sub("", piece).strip())
        if cleanup and _DELETE_PAGE_BEFORE.search(clauses[-1][0]):
            last, borrowed = clauses[-1]
            clauses[-1] = (f"{last}{joiner}{piece}", borrowed)
            continue
        if re.search(r"\band\b|&", joiner, re.I) and re.search(
            r"\bbetween\b(?![\s\S]*\band\b)", clauses[-1][0], re.I
        ):
            # "a relationship between the store table and the sales table": one pair
            last, borrowed = clauses[-1]
            clauses[-1] = (f"{last}{joiner}{piece}", borrowed)
            continue
        if _starts_clause(piece, names):
            clauses.append((piece, False))
            verb = _first_word(piece)
            continue
        noun = _NOUN_START.match(piece.strip())
        adds = verb in _ADD_LIKE and noun and _FORMAT_NOUN.search(noun.group("noun"))
        makes = (
            verb in _MAKE_LIKE
            and _FORMAT_NOUN.search(piece)
            and _VALUE_WORD.search(piece)
            and re.match(
                r"^(?:the|this|that|these|those|all|both|every|its|their)\b", piece.strip(), re.I
            )
        )
        # "make Total MTD Sales green and Avg MTD Sales by Agents blue": a name with its own value
        own_value = (
            verb in _MAKE_LIKE
            and bool(_COLOR_RE.search(piece))
            and any(piece.strip().casefold().startswith(n.casefold()) for n in names if len(n) > 2)
        )
        if adds or makes or own_value:
            # "the title blue" after "make the bars green": it borrows the verb before it
            clauses.append((f"{verb} {piece.strip()}", True))
        else:
            # the joiner as typed: "Q1 sales, then Q2 sales" is one title, not "Q1 sales and ..."
            last, borrowed = clauses[-1]
            clauses[-1] = (f"{last}{joiner}{piece}", borrowed)
    return [(unmask_quotes(c, saved).strip(), b) for c, b in clauses if c.strip()]


# --- colors --------------------------------------------------------------------------------

BASE_COLORS: dict[str, str] = {
    "red": "#D13438",
    "orange": "#F7630C",
    "yellow": "#FFB900",
    "gold": "#C19C00",
    "green": "#107C10",
    "lime": "#8CBD18",
    "teal": "#038387",
    "turquoise": "#00B7C3",
    "cyan": "#00B7C3",
    "aqua": "#00B7C3",
    "blue": "#0078D4",
    "navy": "#1F3864",
    "purple": "#8764B8",
    "violet": "#8764B8",
    "indigo": "#4B0082",
    "lavender": "#B4A0FF",
    "pink": "#E3008C",
    "magenta": "#B4009E",
    "brown": "#8E562E",
    "beige": "#D8C7A0",
    "gray": "#7A7574",
    "grey": "#7A7574",
    "silver": "#C8C6C4",
    "black": "#000000",
    "white": "#FFFFFF",
    "maroon": "#750B1C",
    "crimson": "#DC143C",
    "coral": "#FF7F50",
    "salmon": "#FA8072",
    "olive": "#6B7A2A",
    "mint": "#8FD6AB",
}
# A modifier shades the base color the way Power BI shades a theme color (pbir_expr.shade).
_MODIFIERS: dict[str, float] = {
    "dark": -0.4,
    "darker": -0.25,
    "lighter": 0.4,
    "deep": -0.25,
    "light": 0.55,
    "pale": 0.7,
    "pastel": 0.65,
    "soft": 0.5,
    "bright": 0.0,
}
_COLOR_WORDS = "|".join(sorted(BASE_COLORS, key=len, reverse=True))
_COLOR_RE = re.compile(
    rf"(?<![\w#-])(?:(?P<mod>{'|'.join(_MODIFIERS)})[\s-]+)?(?P<name>{_COLOR_WORDS})(?![\w-])"
    r"|(?<![\w#])(?P<hex>#[0-9A-Fa-f]{6}|#[0-9A-Fa-f]{3})(?![0-9A-Fa-f\w])",
    re.I,
)


@dataclass(frozen=True)
class ColorMatch:
    start: int
    end: int
    text: str  # as the user wrote it ("dark blue", "#1E90FF")
    hex: str
    name: str  # the base color word ("blue"), "" for a hex code


def _expand_hex(code: str) -> str:
    if len(code) == 4:
        code = "#" + "".join(c * 2 for c in code[1:])
    return code.upper()


def color_hex(name: str, modifier: str | None, palette: dict[str, str] | None = None) -> str:
    """The hex of a named color, from the theme's own names first, then the built-in table."""
    base = (palette or {}).get(name.casefold()) or BASE_COLORS[name.casefold()]
    base = expr.hex_color(base)
    if modifier:
        return expr.shade(base, _MODIFIERS[modifier.casefold()])
    return base


def find_colors(text: str, palette: dict[str, str] | None = None) -> list[ColorMatch]:
    found: list[ColorMatch] = []
    for match in _COLOR_RE.finditer(text):
        if match.group("hex"):
            found.append(
                ColorMatch(
                    match.start(), match.end(), match.group(0), _expand_hex(match.group("hex")), ""
                )
            )
        else:
            name = match.group("name").casefold()
            found.append(
                ColorMatch(
                    match.start(),
                    match.end(),
                    match.group(0),
                    color_hex(name, match.group("mod"), palette),
                    name,
                )
            )
    return found


# --- amounts --------------------------------------------------------------------------------

_AMOUNT = re.compile(
    r"(?<![\w#.:/-])(?P<sign>[+-])?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?![:/]\d)\s*"  # 4:3 and 16:9 are ratios, not amounts
    r"(?P<unit>k\b|m\b|thousand\b|million\b|billion\b|bn\b|b\b|%|percent\b|per\s+cent\b|"
    r"pt\b|pts\b|points?\b|px\b|pixels?\b)?",
    re.I,
)


@dataclass(frozen=True)
class Amount:
    start: int
    end: int
    value: float
    unit: str  # "", "pt", "px", "%", "k", "m", "b"
    signed: bool  # written with an explicit + or -, so a change rather than a value
    text: str


_UNITS = {
    "k": "k",
    "thousand": "k",
    "m": "m",
    "million": "m",
    "b": "b",
    "bn": "b",
    "billion": "b",
    "%": "%",
    "percent": "%",
    "per cent": "%",
    "pt": "pt",
    "pts": "pt",
    "point": "pt",
    "points": "pt",
    "px": "px",
    "pixel": "px",
    "pixels": "px",
}


def find_amounts(text: str) -> list[Amount]:
    found: list[Amount] = []
    for match in _AMOUNT.finditer(text):
        unit = _UNITS.get(re.sub(r"\s+", " ", (match.group("unit") or "").casefold()), "")
        value = float(match.group("num").replace(",", ""))
        signed = bool(match.group("sign"))
        if match.group("sign") == "-":
            value = -value
        found.append(
            Amount(match.start(), match.end(), value, unit, signed, match.group(0).strip())
        )
    return found


_SCALE = {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}


def scaled(amount: Amount) -> float:
    """The number an amount stands for: 120K is 120000."""
    return amount.value * _SCALE.get(amount.unit, 1.0)


def plain_number(value: float) -> str:
    """120000.0 -> "120000", 1.5 -> "1.5"."""
    return str(int(value)) if float(value).is_integer() else repr(round(value, 6))


def short_number(value: float) -> str:
    """120000 -> "120K", 1500000 -> "1.5M", 42 -> "42"."""
    for scale, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(value) >= scale and (value / scale) * 100 % 1 == 0:
            return f"{plain_number(value / scale)}{suffix}"
    return plain_number(value)


# --- fonts ------------------------------------------------------------------------------------

FONTS = (
    "Segoe UI Semibold",
    "Segoe UI Light",
    "Segoe UI Bold",
    "Segoe UI",
    "Arial Black",
    "Arial",
    "Calibri",
    "Cambria",
    "Candara",
    "Comic Sans MS",
    "Consolas",
    "Constantia",
    "Corbel",
    "Courier New",
    "DIN",
    "Georgia",
    "Tahoma",
    "Times New Roman",
    "Trebuchet MS",
    "Verdana",
)
_FONT_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(f) for f in FONTS) + r")(?![\w-])", re.I)


def find_font(text: str) -> str | None:
    match = _FONT_RE.search(text)
    if not match:
        return None
    return next(f for f in FONTS if f.casefold() == match.group(1).casefold())
