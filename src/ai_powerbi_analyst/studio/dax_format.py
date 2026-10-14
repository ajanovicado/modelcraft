"""DAX FORMAT() for the local engine, en-US, matching Power BI Desktop's output.

Numbers are first rounded to 15 significant digits, as .NET does before applying a
format: Desktop prints FORMAT(0.46757679180887374, "0.###############") as
0.467576791808874. Custom formats follow the VB/.NET rules: up to three sections
(positive;negative;zero), 0 and # digits, a comma for thousands (or, after the last
digit, divide by 1,000), % multiplies by 100, quoted or backslash-escaped literals.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

_NAMED_NUMBER = {
    "general number": None,
    "currency": "$#,##0.00;($#,##0.00)",
    "fixed": "0.00",
    "standard": "#,##0.00",
    "percent": "0.00%",
    "scientific": "0.00E+00",
    "yes/no": None,
    "true/false": None,
    "on/off": None,
}
_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _decimal(value: float) -> Decimal:
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(f"{value:.15g}")


def _general(value: float | int) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    text = f"{value:.15g}"
    if "e" in text:
        mantissa, exponent = text.split("e")
        sign = "-" if exponent.startswith("-") else "+"
        return f"{mantissa}E{sign}{exponent.lstrip('+-').zfill(2)}"
    return text


def _split_sections(fmt: str) -> list[str]:
    sections, current, quoted, escaped = [], [], False, False
    for char in fmt:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == '"':
            current.append(char)
            quoted = not quoted
        elif char == ";" and not quoted:
            sections.append("".join(current))
            current = []
        else:
            current.append(char)
    sections.append("".join(current))
    return sections


def _tokens(section: str) -> list[tuple[str, str]]:
    """(kind, text): kind is 'lit' or 'code' for format characters."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(section):
        char = section[i]
        if char == "\\" and i + 1 < len(section):
            out.append(("lit", section[i + 1]))
            i += 2
        elif char == '"':
            end = section.find('"', i + 1)
            end = len(section) if end < 0 else end
            out.append(("lit", section[i + 1 : end]))
            i = end + 1
        elif char in "0#.,%Ee" and not (
            char in "Ee" and not (i + 1 < len(section) and section[i + 1] in "+-")
        ):
            if char in "Ee":
                out.append(("code", section[i : i + 2]))
                i += 2
                continue
            out.append(("code", char))
            i += 1
        else:
            out.append(("lit", char))
            i += 1
    return out


def _format_section(value: Decimal, section: str, *, negative_section: bool) -> str:
    tokens = _tokens(section)
    codes = "".join(text for kind, text in tokens if kind == "code")
    if "%" in codes:
        value *= 100
    # Scaling commas: commas right before the decimal point (or at the end of the
    # digit run) divide by 1,000 each.
    digit_codes = re.sub(r"[^0#.,]", "", re.split(r"[Ee][+-]", codes)[0])
    integer_part = digit_codes.split(".")[0]
    scale = len(integer_part) - len(integer_part.rstrip(","))
    value /= Decimal(1000) ** scale
    integer_codes = integer_part.rstrip(",")
    thousands = "," in integer_codes
    decimals = digit_codes.split(".")[1] if "." in digit_codes else ""
    decimals = decimals.replace(",", "")
    required = len(decimals) - len(decimals.lstrip("0")) if decimals else 0
    optional = len(decimals) - required
    exponent_code = next((t for k, t in tokens if k == "code" and t[:1] in "Ee"), None)
    exponent = 0
    if exponent_code and value != 0:
        exponent = value.copy_abs().adjusted() - (len(integer_codes.replace(",", "")) - 1)
        value = value.scaleb(-exponent)
    places = required + optional
    rounded = value.copy_abs().quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    integer_text, _, fraction_text = f"{rounded:f}".partition(".")
    fraction_text = fraction_text.ljust(places, "0")[:places]
    # Drop trailing optional zeros.
    keep = len(fraction_text)
    while keep > required and fraction_text[keep - 1] == "0":
        keep -= 1
    fraction_text = fraction_text[:keep]
    min_integer = integer_codes.replace(",", "").count("0")
    if integer_text == "0" and min_integer == 0:
        integer_text = ""
    integer_text = integer_text.zfill(min_integer) if min_integer else integer_text
    if thousands and integer_text:
        integer_text = f"{int(integer_text):,}"
    number = integer_text + ("." + fraction_text if fraction_text else "")
    if not number and "." in digit_codes:
        number = "."
    if exponent_code:
        sign = "-" if exponent < 0 else ("+" if exponent_code[1] == "+" else "")
        number += exponent_code[0] + sign + str(abs(exponent)).zfill(2)
    # Rebuild: literal text in place, the number where the first digit code was.
    out: list[str] = []
    placed = False
    for kind, text in tokens:
        if kind == "lit":
            out.append(text)
        elif text == "%":
            out.append("%")
        elif not placed and text[:1] in "0#.,":
            out.append(number)
            placed = True
    text = "".join(out)
    if value < 0 and not negative_section and rounded != 0:
        text = "-" + text
    return text


def format_number(value: float | int, fmt: str) -> str:
    named = fmt.strip().casefold()
    if named in {"general number", "g", ""}:
        return _general(value)
    if named in {"yes/no", "true/false", "on/off"}:
        words = {"yes/no": ("Yes", "No"), "true/false": ("True", "False"), "on/off": ("On", "Off")}
        return words[named][0] if value else words[named][1]
    fmt = _NAMED_NUMBER.get(named) or fmt
    if named == "percent":
        fmt = "0.00%"
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return _general(value)
    number = _decimal(value)
    sections = _split_sections(fmt)
    if len(sections) == 1:
        return _format_section(number, sections[0], negative_section=False)
    if number < 0 and sections[1] != "":
        shown = _format_section(-number, sections[1], negative_section=True)
    else:
        shown = _format_section(number, sections[0], negative_section=False)
    # A value that rounds to zero under its own section takes the zero section:
    # Desktop formats -0.0000001 with "0.00%;-0.00%;0.00%" as 0.00%.
    if len(sections) >= 3 and sections[2] != "" and not re.search(r"[1-9]", shown):
        return _format_section(Decimal(0), sections[2], negative_section=True)
    return shown


_DATE_TOKEN = re.compile(
    r'"[^"]*"|\\.|yyyy|yy|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|nn|n|ss|s|AM/PM|am/pm|q|.', re.S
)
_NAMED_DATE = {
    "general date": "m/d/yyyy h:nn:ss AM/PM",
    "long date": "dddd, mmmm d, yyyy",
    "medium date": "dd-mmm-yy",
    "short date": "m/d/yyyy",
    "long time": "h:nn:ss AM/PM",
    "medium time": "hh:nn AM/PM",
    "short time": "hh:nn",
}


def format_date(value: datetime | date, fmt: str) -> str:
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    named = fmt.strip().casefold()
    if named in {"", "general date"}:
        if value.hour == value.minute == value.second == 0:
            fmt = "m/d/yyyy"
        else:
            fmt = _NAMED_DATE["general date"]
    else:
        fmt = _NAMED_DATE.get(named, fmt)
    tokens = _DATE_TOKEN.findall(fmt)
    out: list[str] = []
    for index, token in enumerate(tokens):
        lower = token.lower()
        if token.startswith('"'):
            out.append(token[1:-1])
        elif token.startswith("\\"):
            out.append(token[1:])
        elif lower == "yyyy":
            out.append(f"{value.year:04d}")
        elif lower == "yy":
            out.append(f"{value.year % 100:02d}")
        elif lower in {"mmmm", "mmm", "mm", "m"}:
            # m after h (or before s) means minutes, as in VB.
            previous = "".join(tokens[:index]).lower().rstrip(": ")
            minutes = previous.endswith(("h", "hh")) or "".join(tokens[index + 1 :]).lower().lstrip(
                ": "
            ).startswith("s")
            if minutes and lower in {"mm", "m"}:
                out.append(f"{value.minute:02d}" if lower == "mm" else str(value.minute))
            elif lower == "mmmm":
                out.append(_MONTHS[value.month - 1])
            elif lower == "mmm":
                out.append(_MONTHS[value.month - 1][:3])
            elif lower == "mm":
                out.append(f"{value.month:02d}")
            else:
                out.append(str(value.month))
        elif lower == "dddd":
            out.append(_DAYS[value.weekday()])
        elif lower == "ddd":
            out.append(_DAYS[value.weekday()][:3])
        elif lower == "dd":
            out.append(f"{value.day:02d}")
        elif lower == "d":
            out.append(str(value.day))
        elif lower in {"hh", "h"}:
            twelve = "am/pm" in fmt.lower()
            hour = value.hour % 12 or 12 if twelve else value.hour
            out.append(f"{hour:02d}" if lower == "hh" else str(hour))
        elif lower in {"nn", "n"}:
            out.append(f"{value.minute:02d}" if lower == "nn" else str(value.minute))
        elif lower in {"ss", "s"}:
            out.append(f"{value.second:02d}" if lower == "ss" else str(value.second))
        elif lower == "am/pm":
            text = "AM" if value.hour < 12 else "PM"
            out.append(text if token.isupper() else text.lower())
        elif lower == "q":
            out.append(str((value.month - 1) // 3 + 1))
        else:
            out.append(token)
    return "".join(out)


def format_value(value: object, fmt: str) -> str:
    """FORMAT(value, fmt) in en-US. BLANK formats as an empty string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        named = fmt.strip().casefold()
        if named in {"yes/no", "true/false", "on/off"}:
            return format_number(int(value), fmt)
        return "True" if value else "False"
    if isinstance(value, datetime | date):
        return format_date(value, fmt)
    if isinstance(value, int | float):
        return format_number(value, fmt)
    return str(value)
