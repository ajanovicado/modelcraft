"""What Studio can edit in an imported report, and what it hands to Power BI Desktop.

``CAPABILITY_MATRIX`` lists what a "full developer" might ask for: each item is supported
(an operation family in report_edits), planned (a slice that is not built yet) or refused
(with the reason). An out-of-scope request becomes an UNSUPPORTED clause that cites its
entry, states the reason in plain English and gives the steps to do it in Desktop, instead
of being mapped onto an operation that only looks similar.

"Supported" is exactly what has a tool card (edit_tools) whose operation report_edits accepts:
the matrix is derived from the cards the first time it is read. A planned entry becomes
supported when a family's card names it (``ToolCard.capability``) or when most of the card's
examples read as a request for it (its ``pattern``); it keeps its pattern and Desktop steps, so
a request for it is still recognized, and is then read by the planner instead of refused. A
card that fits no entry adds one of its own. Publishing stays refused whatever a card says.

Styling is refused whatever a card says too: Studio does not change colors, fonts, titles, data
labels, legends, axes, borders, backgrounds or themes (the operations that did were removed).
The ``styling`` entries answer such a request with one short sentence (``STYLING_REASON``) and
point at Power BI Desktop's Format pane. A styling entry gives way to a supported feature that
also works with colors or numbers (``defer_to``: conditional formatting, number formats,
analytics lines) and to adding a text box or a shape (``unless``), so "color the Rank column by
value" or "add a red rectangle" are not refused.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .edit_tools import ToolCard

LOGGER = logging.getLogger(__name__)

Status = Literal["supported", "planned", "refused"]


@dataclass(frozen=True)
class Capability:
    key: str
    title: str  # plain name for a person: "Conditional formatting"
    status: Status
    family: str = ""  # supported: the report_edits operations that do it
    reason: str = ""  # planned or refused: why Studio does not do it, in plain English
    desktop_steps: tuple[str, ...] = ()
    pattern: re.Pattern[str] | None = None  # what a request for it looks like
    # A request the pattern matches is still not this entry's when one of these entries'
    # patterns matches it too, or when ``unless`` does (the styling entries give way to
    # conditional formatting and number formats, and to adding a text box or a shape).
    defer_to: tuple[str, ...] = ()
    unless: re.Pattern[str] | None = None


def _p(text: str) -> re.Pattern[str]:
    return re.compile(text, re.I)


def _planned(
    key: str, title: str, pattern: str, steps: tuple[str, ...], reason: str = ""
) -> Capability:
    return Capability(
        key,
        title,
        "planned",
        reason=reason or f"Studio cannot do {title.lower()} yet.",
        desktop_steps=steps,
        pattern=_p(pattern),
    )


def _refused(key: str, title: str, pattern: str, reason: str, steps: tuple[str, ...]) -> Capability:
    return Capability(
        key, title, "refused", reason=reason, desktop_steps=steps, pattern=_p(pattern)
    )


def _supported(key: str, title: str, family: str) -> Capability:
    return Capability(key, title, "supported", family=family)


# --- styling: refused -------------------------------------------------------------------------

STYLING_REASON = (
    "Studio doesn't change styling such as colours, fonts or titles; do it in Power BI Desktop's "
    "Format pane."
)
STYLING_STEPS = (
    "In Power BI Desktop select the visual (or an empty spot of the page, for the page itself) "
    "and open Format in the Visualizations pane.",
    "Change the setting there; the colors and fonts of the whole report are under View > Themes.",
)
# The color words a request uses (edit_text.BASE_COLORS, with its shades), and a hex code.
_COLOR = (
    r"(?:(?:dark|darker|light|lighter|pale|deep|bright|soft|pastel)[\s-]+)?"
    r"(?:red|orange|yellow|gold|green|lime|teal|turquoise|cyan|aqua|blue|navy|purple|violet|"
    r"indigo|lavender|pink|magenta|brown|beige|gr[ae]y|silver|black|white|maroon|crimson|coral|"
    r"salmon|olive|mint)(?![\w-])|#[0-9a-f]{6}(?![0-9a-f])|#[0-9a-f]{3}(?![0-9a-f])"
)
_FONT_NAMES = (
    r"segoe\s+ui|arial|calibri|cambria|candara|comic\s+sans|consolas|constantia|corbel|"
    r"courier(?:\s+new)?|georgia|tahoma|times\s+new\s+roman|trebuchet|verdana"
)
# What a styling request looks like when no other family could read it as something else first:
# a color given to something, a lighter or darker shade, fonts and bold or italic text, a page's
# background or wallpaper, the theme and its palette, borders, shadows, rounded corners,
# transparency, table banding and word wrap, a button's look, the Format pane itself. Data labels,
# legends, axes and titles are read (and refused) by the parser, after the families that use the
# same words for fields ("put Channel on the legend") have had their turn.
_STYLING = (
    rf"\b(?:make|turn|paint|colou?r|recolou?r|repaint|change|set|switch|use|give|fill|shade|"
    rf"replace|swap|convert)\b[^.]{{0,60}}?(?<![\w#-])(?:{_COLOR})|"
    rf"\b(?:should|could|would|must|to|can|will)\s+(?:all\s+)?(?:be|look|turn)\s+"
    rf"(?:a\s+|an\s+|in\s+)?(?:{_COLOR})|"
    rf"\b(?:in|use)\s+(?:{_COLOR})\s+(?:instead|everywhere)\b|"
    r"\b(?:change|set|pick|use|different|new|other|another|better|nicer)\b[^.]{0,30}?\bcolou?rs?\b|"
    r"\bcolou?rs?\s+(?:of|for|on)\s+(?:the\s+|this\s+|that\s+|all\s+)|"
    r"\b(?:darker|lighter|brighter|paler|darken|lighten|brighten)\b|"
    r"\btranspar\w*|\bopacity\b|\bsee[- ]through\b|"
    r"\bfonts?\b|\btypefaces?\b|\bbold(?:er|ed)?\b|\bunbold\b|\bitalic\w*|\bunderlin\w*|"
    r"\b(?:text|font|title|label|legend|header|value)s?\s+sizes?\b|\b\d+(?:\.\d+)?\s*pts?\b|"
    rf"\b(?:use|in|to|with|set\b[^.]{{0,20}}?)\s+(?:{_FONT_NAMES})\b|"
    r"\b(?:page|canvas|report)\s+backgrounds?\b|\bwallpaper\b|"
    r"\bbackgrounds?\s+(?:of|for|on)\s+(?:the\s+|this\s+|every\s+|each\s+|all\s+)?"
    r"(?:pages?|canvas|report)\b|\bbackground\s+(?:images?|pictures?|photos?)\b|"
    r"\bthemes?\b|\bpalettes?\b|\bcolou?r\s+schemes?\b|\bbrand\s+colou?rs?\b|\bdark\s+mode\b|"
    r"\bgrid\s*lines?\b|"
    r"\b(?:rotate|tilt|angle)\b[^.]{0,40}\b(?:axis|axes|labels?)\b|"
    r"\b(?:translate|rewrite|convert)\b[^.]{0,45}\btitles?\b|"
    # The gap between marks inside a chart is formatting, not spacing whole visuals.
    r"\bbars?\b(?!\s+(?:charts?|graphs?|visuals?))[^.]{0,40}\b"
    r"(?:spacing|gaps?|closer|apart|breathing\s+room|thicker|thinner)\b|"
    r"\b(?:gaps?|spacing)\b[^.]{0,30}\b(?:between|around)\s+(?:the\s+)?"
    r"(?:bars|columns)\b|"
    r"\bborders?\b|\b(?:drop\s+)?shadows?\b|\brounded\s+corners?\b|\bround\s+(?:the\s+|its\s+)?"
    r"corners?\b|\bcorner\s+radius\b|\bpadding\b|"
    r"\bband(?:ed|ing)?\s+(?:the\s+)?rows?\b|\bzebra\s+(?:rows?|stripes?|striping)\b|"
    r"\balternat\w*\s+rows?\s+colou?rs?\b|"
    r"\bword\s+wrap\b|\bwrap\s+(?:the\s+)?(?:column\s+|row\s+)?(?:text|headers?|values?)\b|"
    r"\bbuttons?\b[^.]{0,40}\b(?:hover|pressed|fill|outline|style|look)\b|"
    r"\b(?:frames?|outlines?)\s+(?:a?round|about)\b|"
    # a chart's key is its legend ("move the key of Total MTD Sales under the chart")
    r"\b(?:move|put|place|hide|show|remove|drop)\s+(?:the\s+)?(?:chart\s+|colou?r\s+)?key\s+of\b|"
    # a visual's new name is its title ("call the Avg Discount per Transaction chart Discount
    # per sale"); a page, a bookmark, a column, a field or a measure has a name of its own
    r"\b(?:call|rename|retitle)\s+(?:the\s+|this\s+|that\s+|my\s+)?"
    r"(?:(?!(?:pages?|bookmarks?|columns?|fields?|measures?|groups?)\b)[\w'%/&-]+\s+){0,6}?"
    r"(?:charts?|graphs?|visuals?|cards?|slicers?|kpis?|gauges?|matri(?:x|ces))\b|"
    r"\bformat\s+pane\b"
)
# Adding a text box or a shape takes a color and bold text ("add a red rectangle", "add a bold
# text box saying Warning"), and a page's title is a text box ("add a title to the page").
_ADDS_AN_OBJECT = (
    r"\b(?:add|insert|draw|create|put|place|new)\b[^.]{0,40}?\b(?:text\s?box(?:es)?|notes?|"
    r"shapes?|rectangles?|squares?|circles?|ovals?|ellipses?|triangles?|arrows?|chevrons?|"
    r"pentagons?|hexagons?|octagons?|hearts?|pills?|trapezoids?|parallelograms?|speech\s+bubbles?|"
    r"callouts?|lines?(?!\s+(?:charts?|graphs?)))\b"
)
# A number format is set_number_format's even when it names data labels or the axis.
_NUMBER_FORMAT = (
    r"\bin\s+(?:thousands|millions|billions|trillions)\b|\b(?:\d+|no|zero|one|two|three|four)\s+"
    r"decimals?\b|\bdecimal\s+places?\b|\bdisplay\s+units?\b|\bnumber\s+formats?\b|"
    r"\bformat\s+(?:string|code)s?\b|\bthousands?\s+separators?\b"
)
# A color word inside a name or a text is not a styling request ("create a measure called Red
# Flags", "set the alt text to Green bars show growth", "only show the Blue team"); a visual's
# own new title is refused by the parser, which reads it after these families.
_NAMES_OR_TEXT = (
    r"\b(?:called|named|saying|says|titled)\b|\balt\s?text\b|\bdescri(?:be|ption)\b|"
    r"\bfilter\w*|\bonly\s+(?:show|keep)\b|\bexclude\b|\bkeep\s+only\b|\bmeasures?\b|="
)
# The supported entries a styling entry gives way to.
_STYLING_DEFERS = (
    "conditional_formatting",
    "visual_number_formats",
    "analytics_lines",
    "small_multiples",
)
# Words that make a name (a field, a measure, a caption, a page) read like a styling request:
# the parser blanks such names before it looks for one ("add Product Color to the table").
_STYLE_WORDS = re.compile(
    rf"(?<![\w#-])(?:{_COLOR})|\bcolou?rs?\b|\bthemes?\b|\bpalettes?\b|\bfonts?\b|\bbold\b|"
    r"\bitalic\b|\bborders?\b|\bbackgrounds?\b|\bshadows?\b|\bwallpaper\b|\btranspar\w*|"
    r"\bopacity\b|\bpadding\b",
    re.I,
)


def reads_like_styling(name: str) -> bool:
    """Whether a name has a word a styling request uses (a color, "theme", "font"...)."""
    return _STYLE_WORDS.search(name) is not None


def _styling(key: str, title: str, pattern: str) -> Capability:
    """A kind of styling: refused with the one sentence every styling request gets."""
    return Capability(
        key,
        title,
        "refused",
        reason=STYLING_REASON,
        desktop_steps=STYLING_STEPS,
        pattern=_p(pattern),
        defer_to=_STYLING_DEFERS,
        unless=_p(f"{_ADDS_AN_OBJECT}|{_NUMBER_FORMAT}|{_NAMES_OR_TEXT}"),
    )


_LIST: tuple[Capability, ...] = (
    # --- supported: what chat edits today -------------------------------------------------
    _supported("layout", "Move and resize", "set_position"),
    _supported(
        "align_distribute",
        "Align, space evenly, swap and place next to another visual",
        "align_visuals, distribute_visuals, swap_visuals, place_visual",
    ),
    _supported("z_order", "Bring to front and send to back", "arrange"),
    _supported("visibility", "Hide and show a visual", "set_visual_hidden"),
    _supported(
        "visual_lifecycle",
        "Delete, duplicate, add, change type",
        "delete_visual, duplicate_visual, add_visual, change_visual_type",
    ),
    _supported("fields", "Fields and sorting", "set_fields, set_sort"),
    _supported(
        "pages",
        "Add, rename, duplicate, delete, move and hide pages",
        "add_page, rename_page, duplicate_page, delete_page, move_page, set_page_hidden",
    ),
    _supported("page_format", "Page size", "set_page_size"),
    _supported("text_boxes", "Adding a text box", "add_textbox"),
    _supported("shapes", "Adding a shape", "add_shape"),
    _supported(
        "filters",
        "Filters on a visual, a page or the report",
        "set_visual_filter, set_page_filter, set_report_filter, clear_filter",
    ),
    _supported(
        "measures",
        "Create, change, rename and delete measures",
        "create_measure, update_measure, rename_measure, delete_measure",
    ),
    # --- planned slices ---------------------------------------------------------------------
    _planned(
        "conditional_formatting",
        "Conditional formatting",
        r"conditional\s+format|colou?r\s+scales?|\bgradients?\b|data\s+bars?|traffic\s+lights?|"
        r"(?:make|turn|colou?r|highlight|paint|show|mark|flag)\b.{0,50}?"
        r"\b(?:red|green|amber|orange|yellow|blue)\b.{0,50}?"
        r"\b(?:if|when|whenever|based\s+on|depending\s+on|greater\s+than|less\s+than|"
        r"higher\s+than|lower\s+than|more\s+than|exceeds?|(?:above|below|over|under)\s+\d)\b|"
        r"highlight\b.{0,30}\b(?:top|bottom|best|worst|highest|lowest|max|min)\b|"
        r"\bicons?\b.{0,20}\b(?:based\s+on|rules?|when|if)\b|(?:based\s+on|rules?)\b.{0,30}\bicons?\b|"
        r"\b(?:arrows?|traffic|flags?|stars?|ratings?|kpi)\s+icons?\b|\bicon\s+sets?\b|"
        # Arrows next to cell values are icons even without the word "icons". Their
        # thresholds can contain "only above/below", which must not become a filter.
        # Keep the value/column cue and avoid drawing verbs: a page arrow is a shape.
        r"\b(?:show|display|use|apply|add|put|place)\b[^.!?]{0,30}\b"
        r"(?:(?:three|four|five|[345])[\s-]+(?:colou?red[\s-]+|gr[ae]y(?:ed)?[\s-]+)?|"
        r"(?:colou?red|gr[ae]y(?:ed)?)[\s-]+)arrows?\b[^.!?]{0,60}"
        r"\b(?:values?|cells?|columns?|numbers?)\b|"
        r"\b(?:Three(?:Colored|Grayed)|Four(?:Colored|Gray)|Five(?:Colored)?)Arrows\b|"
        # a table column shaded by its values, never the whole visual's background: "shade the
        # Rank column background from white to green", "a heat map on vs PM %", "color the
        # Rank column by value", "the Rank column font from red to green"
        r"\bheat\s*maps?\b|\bshad(?:e|ing)\b[^.]{0,60}\bfrom\s+\S+\s+to\b|"
        r"\b(?:shad(?:e|ing)|colou?r)\b[^.]{0,60}\bby\s+(?:its\s+|their\s+|the\s+)?values?\b|"
        r"\bcolumns?\b(?!\s+charts?)[^.]{0,40}\b(?:background|font|text|colou?rs?)\b[^.]{0,20}"
        r"\bfrom\s+\S+(?:\s+\S+)?\s+to\b|"
        r"\bicons?\s+(?:on|for|in|to)\s+(?:the\s+)?(?!page\b)\w",
        (
            "Select the visual and open Format visual in the Visualizations pane.",
            "Find the element to color (Bars, Values or Cell elements) and choose the fx button "
            "next to Color.",
            "Choose Rules, Gradient or Field value, set the conditions and select OK.",
        ),
    ),
    _planned(
        "textbox_rich_text",
        "Rich text in a text box",
        # Adding a text box is supported (add_textbox); formatting part of the text, lists and
        # links inside it are not.
        r"rich\s+text|bullet(?:s|ed)?\s+list|numbered\s+list|"
        r"\b(?:hyperlink|link)\b.{0,25}\btext\s?box|\btext\s?box\b.{0,25}\b(?:hyperlink|link)\b|"
        r"(?:bold|italicize|underline|colou?r|highlight)\b.{0,20}\b(?:part|word|words)\s+of\s+"
        r"(?:the\s+)?(?:text|note)",
        (
            "Select the text box and click into the text.",
            "Select the words and use the floating toolbar to format them or add a link.",
        ),
    ),
    _planned(
        "images_shapes",
        "Images and logos",
        # Adding or swapping a picture is planned (it needs a file from this computer); moving,
        # resizing or hiding one is not. Shapes are supported (add_shape).
        r"\b(?:add|insert|upload|replace|swap|change|use|put|place|create|draw|include|new|import)"
        # (icons on a column are conditional formatting: "add icons to vs PM MOM%")
        r"\b.{0,25}\b(?:logos?|images?|pictures?|photos?|banners?)\b",
        (
            "On the Insert ribbon choose Image.",
            "Pick the file, then drag it into place. For a page background use Format page > "
            "Canvas background > Image.",
        ),
    ),
    _planned(
        "buttons_actions",
        "Buttons and actions",
        r"\b(?:add|insert|create|make|new|put|place)\b.{0,25}\bbuttons?\b|"
        r"\bbuttons?\b.{0,40}\b(?:goes?\s+to|links?\s+to|navigat\w+|opens?|jumps?\s+to|takes?\s+"
        r"(?:me|you|us|them)\s+to)\b|\bnavigat(?:e|ion|or)\b|link\s+(?:it\s+)?to\s+(?:a\s+)?"
        r"(?:page|url|web)|hyperlink|\bon\s+click\b|when\s+(?:clicked|i\s+click)|\bactions?\b",
        (
            "On the Insert ribbon choose Buttons and pick a style.",
            "Select the button, open Format button and turn on Action, then choose what it does.",
        ),
    ),
    _planned(
        "bookmarks",
        "Bookmarks",
        r"\bbookmarks?\b|selection\s+pane|toggle\s+between\s+(?:views|pages)",
        (
            "On the View ribbon turn on Bookmarks.",
            "Set the page up the way you want it and choose Add in the Bookmarks pane.",
        ),
    ),
    _planned(
        "edit_interactions",
        "Edit interactions",
        r"edit\s+interactions?|\binteractions?\b|cross[- ]?(?:filter|highlight)\w*|"
        # ("turn off the filter" is a filter to remove, not an interaction)
        r"(?:stop|prevent|don'?t|do\s+not|disable|turn\s+off)\b.{0,40}\b(?:filtering|highlighting)\b|"
        r"(?:stop|prevent|don'?t|do\s+not|disable)\b.{0,40}\b(?:filter|highlight)\s+"
        r"(?:the|other|each|all)\b",
        (
            "Select the visual that does the filtering.",
            "On the Format ribbon choose Edit interactions.",
            "On each other visual pick Filter, Highlight or None.",
        ),
    ),
    _planned(
        "drillthrough_tooltip_pages",
        "Drillthrough and tooltip pages",
        # (a hierarchy is a model object: the "model" entry; drilling through one is here)
        r"drill[- ]?through|\btool\s?tips?\b|drill\s+(?:down|up)|"
        r"\b(?:expand|drill)\b.{0,30}\bhierarch(?:y|ies)\b",
        (
            "Add a page and open Format page.",
            "Turn on Allow use as tooltip, or add the fields under Drill through.",
            "Then point a visual's Tooltip setting at that page.",
        ),
    ),
    _planned(
        "sync_slicers",
        "Sync slicers",
        r"sync\w*\s+(?:the\s+|these\s+|those\s+|my\s+)?slicers?|slicers?\s+(?:across|on)\s+"
        r"(?:all\s+|other\s+)?pages|slicer\s+sync",
        (
            "On the View ribbon choose Sync slicers.",
            "Tick the pages the selected slicer should sync to and select Visible where needed.",
        ),
    ),
    _planned(
        "analytics_lines",
        "Analytics lines",
        r"trend\s?lines?|average\s+lines?|reference\s+lines?|constant\s+lines?|target\s+lines?|"
        r"forecasts?|error\s+bars?|median\s+lines?|percentile\s+lines?|analytics\s+pane|"
        r"(?:min|max)(?:imum)?\s+lines?",
        (
            "Select the chart and open the Analytics pane (the magnifying glass icon).",
            "Expand the line you want (Trend line, Average line, Constant line...) and choose "
            "Add line.",
        ),
    ),
    _planned(
        "group_align_distribute",
        "Grouping visuals",
        # Aligning, spacing evenly, swapping and placing are supported (align_visuals and its
        # siblings); grouping, ungrouping and grids are not.
        r"\bgroup\s+(?:these|them|those|the|all|together)\b(?!\s+by)|\bungroup\b|"
        r"\bin\s+a\s+(?:single\s+)?grid\b|\b(?:arrange|lay\s*out|line)\b.{0,30}\bgrid\b",
        (
            "Select the visuals (Ctrl+click each).",
            "On the Format ribbon choose Group to group them (or Ungroup to split a group).",
        ),
    ),
    # Copying one visual's formatting onto another is styling (the format painter was removed).
    _styling(
        "format_painter",
        "Format painter",
        r"format\s+painter|copy\s+(?:the\s+)?(?:[\w'-]+\s+){0,5}?(?:format|formatting|style)|"
        r"(?:same|identical)\s+(?:format|formatting|style)\s+as|match\s+(?:the\s+)?"
        r"(?:format|formatting|style)|apply\s+(?:the\s+)?(?:same\s+)?(?:format|formatting|style)"
        r"\s+(?:to|from)",
    ),
    # A table column's width, alignment and colors are styling;
    # its place, totals and header text are not (move_column, set_table_layout,
    # set_field_display_name).
    _styling(
        "column_formatting",
        "Table column formatting",
        # ("make the Rank column 80 px wide" once resized the whole table to 80 pixels)
        r"column\s+widths?|\bcolumn\b(?!\s+(?:charts?|graphs?))\b.{0,25}\b"
        r"(?:wider|narrower|wide|narrow|width|align\w*)\b|"
        r"(?:right|left|center|centre)[- ]align\w*\s+(?:the\s+)?(?:[\w'-]+\s+){0,4}columns?\b|"
        r"wrap\s+text|freeze\s+columns?|"
        # one column's background or text color, never the whole visual's background
        r"\bcolumns?\b(?!\s+(?:charts?|graphs?))[^.]{0,25}\b(?:background|font\s+colou?r|"
        r"text\s+colou?r|fill)\b",
    ),
    _planned(
        "field_display_names",
        "Field display names",
        r"display\s+names?|rename\s+(?:the\s+)?(?:field|column)\b.{0,50}\b(?:in|on)\s+(?:the\s+)?"
        r"(?:table|chart|visual|matrix|card)|rename\s+(?:the\s+)?(?:\S+\s+){0,3}column\s+"
        r"(?:header|heading)",
        (
            "In the Visualizations pane double-click the field in its well.",
            "Type the new name; it changes only in that visual, not in the model.",
        ),
    ),
    _planned(
        "alt_text",
        "Alt text and tab order",
        r"alt\s?text|alternative\s+text|accessib\w+|screen\s+readers?|tab\s+order",
        (
            "Select the visual and open Format visual > General.",
            "Type the description under Alt text. Use View > Selection pane to check tab order.",
        ),
    ),
    _planned(
        "mobile_layout",
        "Mobile layout",
        r"\bmobile\b|phone\s+layout|tablet\s+layout",
        (
            "On the View ribbon choose Mobile layout.",
            "Drag the visuals you want onto the phone canvas.",
        ),
    ),
    _planned(
        "visual_calculations",
        "Visual calculations",
        # (a measure that does it is a measure: "create a running total measure")
        r"^(?![\s\S]*\bmeasures?\b)[\s\S]*?(?:visual\s+calculations?|running\s+(?:totals?|sums?)|"
        r"moving\s+averages?|rolling\s+averages?|percent\s+of\s+(?:parent|grand\s+total|total))",
        (
            "Select the visual and on the Home ribbon choose New visual calculation.",
            "Pick a template (Running sum, Moving average...) or type the DAX, then press Enter.",
        ),
    ),
    _planned(
        "small_multiples",
        "Small multiples",
        r"small\s+multiples?|\btrellis\b|one\s+(?:chart|graph|panel)\s+(?:per|for\s+each)\b|"
        r"split\s+(?:the\s+|this\s+)?(?:chart|graph|visual)\s+(?:by|into)\b",
        (
            "Select the chart.",
            "In the Visualizations pane drag a field into Small multiples.",
        ),
    ),
    # The visual header's icons are styling.
    _styling(
        "visual_header",
        "Visual header icons",
        r"visual\s+headers?|header\s+icons?|focus\s+mode\s+(?:icon|button)|"
        r"more\s+options\s+(?:icon|menu|button)",
    ),
    # A table's structure (set_table_layout, move_column): read by the planner, not by a pattern.
    Capability(
        "table_layout",
        "Totals, subtotals, layout and column order of a table or matrix",
        "planned",
        reason="Studio cannot change the layout of a table or matrix yet.",
        desktop_steps=(
            "Select the table or matrix and open Format visual.",
            "Use Totals, Subtotals and Layout, or drag the fields into another order in the "
            "Visualizations pane.",
        ),
    ),
    _planned(
        "layer_order",
        "Keeping a visual's layer order",
        r"(?:keep|maintain|hold)\s+(?:the\s+|its\s+)?layer(?:\s+order)?|layer\s+order\s+when\s+clicked",
        (
            "Select the visual and open Format visual > General > Properties > Advanced options.",
            "Turn on Maintain layer order.",
        ),
    ),
    _planned(
        "page_view",
        "Page view (fit to page, fit to width, actual size)",
        r"page\s+view|fit\s+to\s+(?:the\s+)?(?:page|width|screen)|actual\s+size",
        ("On the View ribbon choose Page view and pick Fit to page, Fit to width or Actual size.",),
    ),
    _planned(
        "lock_objects",
        "Locking objects on the canvas",
        r"\block\s+(?:the\s+|all\s+(?:the\s+)?)?(?:objects?|visuals?|layout|everything|canvas|page)|"
        r"(?:prevent|stop|keep)\b.{0,30}\b(?:moving|being\s+moved|resizing)\b",
        (
            "On the View ribbon turn on Lock objects.",
            "It stops visuals from being moved or resized while you edit; readers are not "
            "affected. Turn it off the same way.",
        ),
        reason="Studio cannot lock objects: Power BI Desktop keeps that switch in the view, "
        "not in the report.",
    ),
    _planned(
        "model",
        "Model objects: columns, tables, hierarchies and model settings",
        r"\bhierarch(?:y|ies)\b|"
        r"(?:rename|hide|unhide|delete|remove|call)\s+(?:the\s+)?(?:[\w'-]+\s+){0,3}?"
        r"(?:columns?|tables?)\b[\s\S]{0,40}?\b(?:in|from)\s+the\s+(?:data\s+)?(?:model|data)\b|"
        r"\b(?:hide|unhide|rename|delete|remove|summari[sz]e|categori[sz]e)\b[^.]{0,20}?"
        r"'?[A-Za-z_][\w ]{0,40}'?\s*\[[^\]]+\]|"  # "hide DimStore[StoreKey]"
        r"sort\s+by\s+(?:another\s+|a\s+different\s+)?column|data\s+category|"
        r"summari[sz]e\s+by|don'?t\s+summari[sz]e|mark\s+[\s\S]{0,30}?as\s+(?:a\s+)?date\s+table|"
        r"auto\s+date[\s/]*time|implicit\s+measures|model\s+settings",
        (
            "Open the Data pane (or Model view) in Power BI Desktop.",
            "Right-click the column or table to rename, hide or delete it, or use the Column "
            "tools ribbon for its sort, summarization and category; New hierarchy is on the "
            "same menu.",
        ),
    ),
    _planned(
        "calculated_columns",
        "Calculated columns",
        r"calculated\s+columns?|new\s+columns?\b|computed\s+columns?|create\s+(?:a\s+)?columns?\b",
        (
            "Select the table, then on the Table tools ribbon choose New column.",
            "Type the DAX formula in the formula bar.",
        ),
    ),
    _planned(
        "calculated_tables",
        "Calculated tables",
        r"calculated\s+tables?|\bnew\s+table\b|create\s+(?:a\s+)?(?:new\s+)?table\s+(?:that|which|"
        r"from|with)\b|date\s+table|calendar\s+table",
        (
            "On the Modeling ribbon choose New table.",
            "Type the DAX formula, for example a CALENDAR function for a date table.",
        ),
    ),
    _planned(
        "relationships",
        "Relationships",
        r"relationships?|cardinality|many[- ]to[- ](?:one|many)|one[- ]to[- ](?:many|one)|"
        r"cross[- ]?filter\s+direction|(?:join|link|connect|relate)\b.{0,40}\b"
        r"(?:table|tables|key|keys)\b",
        (
            "Open Model view (the third icon on the left).",
            "Drag the key column of one table onto the matching column of the other, or choose "
            "Manage relationships on the Modeling ribbon.",
        ),
    ),
    _planned(
        "field_parameters",
        "Field parameters",
        r"field\s+parameters?|parameter\s+tables?|what[- ]if\s+parameters?",
        (
            "On the Modeling ribbon choose New parameter, then Fields (or Numeric range).",
            "Pick the fields, then use the parameter table as a slicer.",
        ),
    ),
    _planned(
        "calculation_groups",
        "Calculation groups",
        r"calculation\s+(?:groups?|items?)",
        (
            "Open Model view and choose Calculation group in the Data pane.",
            "Add calculation items with their DAX expressions.",
        ),
    ),
    _planned(
        "rls",
        "Row-level security",
        r"\brls\b|row[- ]level\s+security|security\s+roles?|manage\s+roles|"
        r"(?:restrict|limit)\s+(?:access|who\s+can\s+see)|who\s+can\s+see",
        (
            "On the Modeling ribbon choose Manage roles.",
            "Create a role, pick a table and type the DAX filter, then use View as to test it.",
        ),
    ),
    _planned(
        "visual_number_formats",
        "Number formats inside a visual",
        r"(?:number|currency|percent(?:age)?|decimal|date)\s+format\b.{0,40}\b(?:in|on|for)\s+"
        r"(?:the\s+)?(?:table|matrix|column|visual)|thousand\s+separators?|"
        # ("show Total Sales in the table in thousands" once added the field to the table)
        r"\b(?:table|matrix|column)\b.{0,40}\b(?:in\s+(?:thousands|millions|billions)|"
        r"(?:with\s+)?(?:\d|no|one|two|three|four)\s+decimals?(?:\s+places?)?)\b|"
        r"\b(?:in\s+(?:thousands|millions|billions)|(?:\d|no|one|two|three|four)\s+decimals?)"
        r"\b.{0,40}\b(?:in|on|for)\s+(?:the\s+)?(?:table|matrix|column)\b",
        (
            "Select the field in the Data pane and use the Measure tools (or Column tools) "
            "ribbon to set the format and decimals.",
        ),
    ),
    # --- refused ------------------------------------------------------------------------------
    _refused(
        "sign_in",
        "Signing in to a data source or a service",
        r"\bsign(?:ing)?[\s-]*in\b|\blog(?:ging)?[\s-]*(?:in|on)\b|\bcredentials?\b|"
        r"\bpasswords?\b|\bauthenticat\w*|\boauth\b|\baccess\s+tokens?\b|\bapi\s+keys?\b|"
        r"privacy\s+levels?",
        "Studio never signs in anywhere for you or handles passwords and keys: credentials "
        "stay with you in Power BI Desktop.",
        (
            "In Power BI Desktop choose File > Options and settings > Data source settings.",
            "Select the source, choose Edit Permissions and sign in there.",
        ),
    ),
    _refused(
        "data_gateways",
        "Gateways, scheduled refresh, new data sources and data types",
        r"\bgateways?\b|schedul\w*\s+(?:a\s+|the\s+)?refresh|refresh\w*\s+(?:every|each|daily|"
        r"nightly|weekly|hourly|at\s+\d)|(?:every|each)\s+(?:night|morning|day|hour)\b.{0,20}"
        r"\brefresh|"
        r"(?:connect|point)\b.{0,30}\b(?:database|sql|excel|csv|sharepoint|folder|api|server)\b|"
        r"(?:import|load|get)\s+(?:some\s+|new\s+)?data\b|data\s*sources?|"
        r"change\s+(?:the\s+)?data\s*types?|data\s*types?\s+(?:of|for)\b",
        "Gateways and scheduled refresh live in the Power BI service, and a new source or a "
        "column's data type is set where the data is loaded; this report's original sources "
        "are not reachable from here.",
        (
            "In Power BI Desktop choose Transform data, select the column and pick its type "
            "under Data type (or use Get data for a new source), then choose Close & Apply.",
            "Gateways and scheduled refresh are set on the dataset's settings page in the Power "
            "BI service after publishing.",
        ),
    ),
    _planned(
        "power_query",
        "Power Query (a table's query, shared queries and parameters) and refresh",
        # (changing a table's M query, a parameter or refreshing goes through Power BI Desktop
        # once the model family provides it; sign-in, gateways and data types stay refused)
        r"power\s+query|query\s+editor|transform\s+data|\bm\s+(?:query|code)\b|"
        r"(?:refresh|reload)\s+(?:the\s+)?(?:data|dataset|model|source|sources|\w+\s+table)\b|"
        r"(?:merge|append)\s+queries|unpivot|split\s+(?:the\s+)?column|query\s+parameters?|"
        r"(?:change|edit)\s+(?:the\s+)?(?:source|query)\b",
        (
            "On the Home ribbon choose Transform data to open Power Query Editor.",
            "Make the change there and choose Close & Apply; use Refresh on the Home ribbon to "
            "reload the data.",
        ),
        reason="Power Query belongs to the data model, which only Power BI Desktop can "
        "rebuild. This report's original sources are also not reachable from here.",
    ),
    _refused(
        "custom_visuals",
        "Custom visuals",
        r"custom\s+visuals?|app\s?source|marketplace|get\s+more\s+visuals?|import\s+(?:a\s+)?visual|"
        r"\.pbiviz|third[- ]party\s+visuals?|\b(?:r|python)\s+visuals?\b",
        "Studio does not import custom visuals; each one is code that Power BI Desktop has to "
        "load and trust.",
        (
            "In the Visualizations pane choose the three dots, then Get more visuals.",
            "Pick the visual from AppSource, or Import a visual from a file.",
        ),
    ),
    _refused(
        "publishing",
        "Publishing and sharing",
        r"\bpublish\w*|share\s+(?:this|the|my|it)\b.{0,30}\b(?:report|dashboard|with)|"
        r"power\s*bi\s+(?:service|workspace)|to\s+the\s+(?:service|cloud|web)\b|\bembed\b|"
        r"export\b.{0,30}\b(?:pdf|powerpoint|pptx|excel|csv)|(?:send|email)\s+(?:this|the|it|a)\b"
        r".{0,30}\b(?:report|dashboard)|\bsubscribe\b",
        "Publishing and sharing send the report outside this computer, so Studio leaves that "
        "to you in Power BI Desktop.",
        (
            "Use Save a copy here to write your edited report to a .pbix file, and open it in "
            "Power BI Desktop.",
            "On the Home ribbon choose Publish and pick the workspace.",
        ),
    ),
    # Every other change of how a report looks. Last, so that a request that also asks for
    # something refused for another reason ("publish it with a dark theme") gets that reason.
    _styling(
        "styling",
        "Styling: colors, fonts, titles, labels, legends, axes, borders, backgrounds, themes",
        _STYLING,
    ),
)

# The styling entries: refused whatever a card says, with one sentence (STYLING_REASON).
STYLING_KEYS = frozenset({"styling", "format_painter", "column_formatting", "visual_header"})
# Sending the report outside this computer stays with the person, whatever a card offers.
ALWAYS_REFUSED = (
    frozenset({"publishing", "sign_in", "data_gateways", "custom_visuals"}) | STYLING_KEYS
)
_BY_KEY: dict[str, Capability] = {entry.key: entry for entry in _LIST}
# Titles of the entries a family adds when none of the list above names what it does.
_FAMILY_TITLES = {
    "conditional": "Conditional formatting, number formats and table layout",
    "navigation": "Bookmarks, buttons, drillthrough, tooltips and interactions",
    "extras": "Analytics lines, grouping, mobile layout and other visual features",
    "model": "Model objects: columns, tables, relationships, roles and more",
}


# The entries only a change of the model (through Power BI Desktop) provides: a card that needs
# Desktop is matched among these, a report card among the others ("delete a relationship" is
# no edit of interactions, "create a hierarchy" no drillthrough page).
MODEL_KEYS = frozenset(
    {
        "relationships",
        "rls",
        "power_query",
        "calculated_columns",
        "calculated_tables",
        "field_parameters",
        "calculation_groups",
        "measures",
        "model",
    }
)


def _stem(word: str) -> str:
    """ "bookmarks" is "bookmark", "formatting" is "format" (enough to match op and key names)."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
        if len(word) > 3 and word[-1] == word[-2]:
            word = word[:-1]
    return word


def _words(name: str) -> set[str]:
    return {_stem(w) for w in re.split(r"[\W_]+", name.casefold()) if w}


def card_capability(card: ToolCard) -> str:
    """The matrix key a card provides: its own ``capability``; else the planned entry its
    operation is named after ("add_bookmark" provides "bookmarks"); else the one at least half
    of its summary and examples read as a request for; else its family."""
    if card.capability:
        return card.capability
    allowed = [
        entry
        for entry in _LIST
        if entry.status != "supported"
        and entry.key not in ALWAYS_REFUSED
        and (entry.key in MODEL_KEYS) == card.needs_desktop
    ]
    op_words = _words(card.op)
    for entry in allowed:
        # "visual" says nothing: "set_number_format" provides "visual_number_formats"
        if _words(entry.key) - {"visual"} <= op_words:
            return entry.key
    keys = {entry.key for entry in allowed}
    texts = (card.summary, *(request for request, _ in card.examples))
    votes: Counter[str] = Counter()
    for text in texts:
        for entry in allowed:
            if _claims(entry, text) and entry.key in keys:
                votes[entry.key] += 1
                break
    if votes:
        key, count = votes.most_common(1)[0]
        if 2 * count >= len(texts):
            return key
    return card.family


def derive_matrix(cards: Sequence[ToolCard]) -> dict[str, Capability]:
    """The matrix for these cards (the registered ones): an entry is supported when a card
    provides it, and a card that fits no entry adds one."""
    providing: dict[str, list[ToolCard]] = {}
    for card in cards:
        providing.setdefault(card_capability(card), []).append(card)
    matrix: dict[str, Capability] = {}
    for entry in _LIST:
        found = providing.pop(entry.key, [])
        if not found or entry.key in ALWAYS_REFUSED:
            matrix[entry.key] = entry
            continue
        ops = ", ".join(dict.fromkeys(card.op for card in found))
        # The reason said why Studio could not; the steps stay as help for doing it by hand.
        matrix[entry.key] = replace(entry, status="supported", family=ops, reason="")
    for key, found in providing.items():
        family = found[0].family
        title = _FAMILY_TITLES.get(family) or family.replace("_", " ").capitalize()
        ops = ", ".join(dict.fromkeys(card.op for card in found))
        matrix[key] = Capability(key, title, "supported", family=ops)
    return matrix


def _registered_cards() -> list[ToolCard]:
    from . import edit_tools  # imported here: the family modules it loads may import this one

    known = edit_tools.registered_operations()
    try:
        cards = edit_tools.tool_cards()
    except Exception:  # noqa: BLE001 - a broken family must not take the chat down with it
        LOGGER.exception("Could not read the tool cards; only the built-in ones are used")
        cards = edit_tools.tool_cards((edit_tools.BUILTIN_MODULE,))
    return [card for card in cards if card.op in known]


class CapabilityMatrix(Mapping[str, Capability]):
    """``CAPABILITY_MATRIX``: the entries above with their status taken from the cards, worked
    out on first use (the family modules are imported then, not when this module is)."""

    def __init__(self) -> None:
        self._items: dict[str, Capability] | None = None
        self._lock = threading.RLock()
        self._loading = False

    def _load(self) -> dict[str, Capability]:
        items = self._items
        if items is not None:
            return items
        with self._lock:
            if self._items is not None:
                return self._items
            if self._loading:
                # read again while the cards are being imported (a family module that looks
                # at the matrix when it loads): the entries as written, for that moment only
                return {c.key: c for c in _LIST}
            self._loading = True
            try:
                self._items = derive_matrix(_registered_cards())
            finally:
                self._loading = False
            return self._items

    def refresh(self) -> None:
        """Forget the derived entries (tests that register cards of their own)."""
        with self._lock:
            self._items = None

    def __getitem__(self, key: str) -> Capability:
        return self._load()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())


CAPABILITY_MATRIX = CapabilityMatrix()


# A page's background picture is styling (the styling entry's), not an image to add.
_BACKGROUND_IMAGE = re.compile(
    r"\bbackground\s+(?:images?|pictures?|photos?)\b|\bwallpaper\b", re.I
)


def _claims(entry: Capability, text: str) -> bool:
    """Whether a request is this entry's: its pattern matches, and neither ``unless`` nor an
    entry it defers to does ("color the Rank column by value" is conditional formatting, not
    styling)."""
    if entry.pattern is None or not entry.pattern.search(text):
        return False
    if entry.key == "images_shapes" and _BACKGROUND_IMAGE.search(text):
        return False
    if entry.unless is not None and entry.unless.search(text):
        return False
    return not any(
        (other := _BY_KEY.get(key)) is not None
        and other.pattern is not None
        and other.pattern.search(text) is not None
        for key in entry.defer_to
    )


def is_styling(text: str) -> bool:
    """Whether a request asks to change how the report looks (a color, a font, the theme...),
    which Studio refuses (``STYLING_REASON``)."""
    return any(_claims(_BY_KEY[key], text) for key in STYLING_KEYS)


def styling_gives_way(text: str) -> bool:
    """Whether a request's color or style words belong to something else: a text box or a
    shape added, a number format, a name or a text given (a measure called Red Flags, alt
    text), a filter."""
    unless = _BY_KEY["styling"].unless
    return unless is not None and unless.search(text) is not None


def _first_match(text: str, *, supported: bool) -> Capability | None:
    """The first entry of the list whose pattern the text matches, with its status from the
    matrix: among those Studio does not do (``supported=False``), or among every entry."""
    for entry in _LIST:
        if not _claims(entry, text):
            continue
        found = CAPABILITY_MATRIX[entry.key] if supported else entry
        if supported or entry.status != "supported":
            return found
    return None


def detect_capability(text: str) -> Capability | None:
    """The entry a request asks for by its pattern (the planned and refused ones have one),
    as the matrix has it now: an entry Studio does not do comes first, so "publish it with a
    bookmark" is refused for the publishing even when bookmarks are supported."""
    return detect_unsupported(text) or _first_match(text, supported=True)


def detect_unsupported(text: str) -> Capability | None:
    """The planned or refused capability a request asks for, if any (first match). An entry
    a tool family now provides is not one: ``detect_capability`` finds those. A styling entry
    gives way to the supported features it defers to (``Capability.defer_to``)."""
    for entry in _LIST:
        if not _claims(entry, text):
            continue
        found = CAPABILITY_MATRIX[entry.key]
        if found.status != "supported":
            return found
    return None


def unsupported_keys() -> tuple[str, ...]:
    return tuple(k for k, c in CAPABILITY_MATRIX.items() if c.status != "supported")


# ``UNSUPPORTED_KEYS`` is read through the module's __getattr__ below: the matrix it comes from
# is only known once the cards have been read.
UNSUPPORTED_KEYS: tuple[str, ...]


def __getattr__(name: str) -> tuple[str, ...]:
    if name == "UNSUPPORTED_KEYS":
        return unsupported_keys()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
