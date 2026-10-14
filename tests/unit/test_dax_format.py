"""FORMAT() as Power BI Desktop prints it (en-US), for the local engine.

Each expected string here is Desktop's own output (2026-09-27): cards and tables of the
imported dashboard show these texts, and the local page had to match them exactly.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ai_powerbi_analyst.studio.dax_format import format_value


@pytest.mark.parametrize(
    ("value", "fmt", "shown"),
    [
        (-0.0321, "▲ 0.0%; ▼ -0.0%", " ▼ -3.2%"),
        (0.0321, "▲ 0.0%; ▼ -0.0%", "▲ 3.2%"),
        (1234567.891, "#,##0.00", "1,234,567.89"),
        (0.5, "0%", "50%"),
        (12345.678, "0.###############", "12345.678"),
        (0.46757679180887374, "0.###############", "0.467576791808874"),  # 15 digits
        (19, "0.###############", "19"),
        (-0.0000001, "0.00%;-0.00%;0.00%", "0.00%"),  # rounds to zero: third section
        (0.4676, "0.00%;-0.00%;0.00%", "46.76%"),
        (-1234.5, "$#,##0.00;($#,##0.00)", "($1,234.50)"),
        (1234567, "#,##0,K", "1,235K"),
        (0.5, "Percent", "50.00%"),
        (1234.5, "Standard", "1,234.50"),
        (73514.796, "$#,0.00", "$73,514.80"),
        (None, "0.00", ""),
    ],
)
def test_numbers(value, fmt: str, shown: str) -> None:
    assert format_value(value, fmt) == shown


def test_dates() -> None:
    day = datetime(2009, 12, 31)
    assert format_value(day, "mmm yyyy") == "Dec 2009"
    assert format_value(datetime(2009, 1, 5), "dd/mm/yyyy") == "05/01/2009"
    assert format_value(datetime(2009, 1, 5), "mmmm yyyy") == "January 2009"
    assert format_value(day, "") == "12/31/2009"
    assert format_value(datetime(2009, 12, 31, 14, 5), "hh:nn AM/PM") == "02:05 PM"
