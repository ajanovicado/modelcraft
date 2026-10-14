"""Filter operations of the imported-report editor (report_filters.py).

The goldens are the shapes Power BI Desktop writes. The only filter Desktop itself wrote in
the Agents report is the advanced measure filter carried by the structure twin; the rest are
written from Desktop's query language and are listed as unverified in VERIFIED_VARIANTS.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ai_powerbi_analyst.studio import report_filters as rf
from ai_powerbi_analyst.studio.pbir_document import ROOT, DocumentError, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage

TWIN = Path(__file__).resolve().parents[2] / "fixtures" / "imported" / "structure-twin.pbix"
PAGE = "ReportSection8454c812b60b8bb83b99"
VISUAL = "e82e02f93055d6b0b544"
REAL_NAME = "Filterff831044ab8c98051120"

TEXT, INTEGER, DOUBLE, DATETIME, DECIMAL, BOOLEAN = 2, 6, 8, 9, 10, 11


class FakeModel:
    """Tables with typed columns and measures, resolving names case-insensitively."""

    def __init__(self) -> None:
        self.columns: dict[str, dict[str, str | int | None]] = {
            "Sales": {
                "Country": TEXT,
                "Year": INTEGER,
                "YearText": TEXT,
                "Amount": DECIMAL,
                "Price": DOUBLE,
                "OrderDate": DATETIME,
                "Active": BOOLEAN,
                "Mystery": None,
            },
            "DimStore": {"StoreName": TEXT},
        }
        self.measures: dict[str, tuple[str, str | int | None]] = {
            "Rank Filtering Dynamics": ("# Measures", INTEGER),
            "Total Sales": ("# Measures", None),
        }

    def resolve(self, ref: Any) -> tuple[str, str, str]:
        wanted = ref.name.casefold()
        for name, (table, _) in self.measures.items():
            if name.casefold() == wanted:
                return "measure", table, name
        tables = [
            t for t in self.columns if ref.table is None or t.casefold() == ref.table.casefold()
        ]
        for table in tables:
            for column in self.columns[table]:
                if column.casefold() == wanted:
                    return "column", table, column
        raise DocumentError(f"The model has no field {ref.name!r}")

    def data_type(self, table: str, name: str) -> str | int | None:
        if name in self.measures:
            return self.measures[name][1]
        return self.columns[table][name]


def seeded(*first: str) -> Callable[[], str]:
    """A name factory: the given names first, then Filter000...1, Filter000...2."""
    given = list(first)
    counter: Iterator[int] = iter(range(1, 1000))

    def factory() -> str:
        return given.pop(0) if given else f"Filter{next(counter):020x}"

    return factory


def field(name: str, **extra: Any) -> rf.FieldRef:
    return rf.FieldRef(name=name, **extra)


def column(source: dict[str, str], prop: str) -> dict[str, Any]:
    return {"Column": {"Expression": {"SourceRef": source}, "Property": prop}}


def literal(value: str) -> dict[str, Any]:
    return {"Literal": {"Value": value}}


def build(spec: Any, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("name_factory", seeded("Filter" + "a" * 20))
    return rf.build_filter(spec, FakeModel(), set(), **kwargs)


@pytest.fixture
def document() -> PbirDocument:
    if not TWIN.is_file():
        pytest.skip("the structure twin fixture is not present")
    return PbirDocument(PbixPackage(TWIN))


A20 = "Filter" + "a" * 20


# --- goldens ---------------------------------------------------------------------------------


def test_categorical_in_on_a_text_column_quotes_values_and_escapes_quotes() -> None:
    built = build(
        rf.CategoricalFilter(field=field("Country", table="Sales"), values=["Canada", "O'Neil"])
    )
    assert built == {
        "name": A20,
        "field": column({"Entity": "Sales"}, "Country"),
        "type": "Categorical",
        "filter": {
            "Version": 2,
            "From": [{"Name": "s", "Entity": "Sales", "Type": 0}],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [column({"Source": "s"}, "Country")],
                            "Values": [[literal("'Canada'")], [literal("'O''Neil'")]],
                        }
                    }
                }
            ],
        },
        "howCreated": "User",
    }


def test_categorical_not_in_on_an_integer_column_writes_typed_numbers() -> None:
    built = build(
        rf.CategoricalFilter(field=field("Year"), values=[2014, "2015", 2014.0], exclude=True)
    )
    assert built["filter"]["Where"] == [
        {
            "Condition": {
                "Not": {
                    "Expression": {
                        "In": {
                            "Expressions": [column({"Source": "s"}, "Year")],
                            "Values": [[literal("2014L")], [literal("2015L")]],
                        }
                    }
                }
            }
        }
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [("YearText", "'2014'"), ("Year", "2014L"), ("Amount", "2014M"), ("Price", "2014D")],
)
def test_a_literal_is_typed_by_the_column_not_by_the_chat_value(name: str, expected: str) -> None:
    built = build(rf.CategoricalFilter(field=field(name), values=[2014]))
    values = built["filter"]["Where"][0]["Condition"]["In"]["Values"]
    assert values == [[literal(expected)]]


def test_categorical_blank_and_boolean_and_date_values() -> None:
    blank = build(rf.CategoricalFilter(field=field("Country"), values=[None, "x"]))
    assert blank["filter"]["Where"][0]["Condition"]["In"]["Values"] == [
        [literal("null")],
        [literal("'x'")],
    ]
    flag = build(rf.CategoricalFilter(field=field("Active"), values=["yes"]))
    assert flag["filter"]["Where"][0]["Condition"]["In"]["Values"] == [[literal("true")]]
    date = build(rf.CategoricalFilter(field=field("OrderDate"), values=["2019-01-01"]))
    assert date["filter"]["Where"][0]["Condition"]["In"]["Values"] == [
        [literal("datetime'2019-01-01T00:00:00'")]
    ]


def test_advanced_and_on_a_decimal_column() -> None:
    built = build(
        rf.AdvancedFilter(
            field=field("Amount"),
            conditions=[
                rf.Condition(operator="greater_or_equal", value=5),
                rf.Condition(operator="less_than", value="10.5"),
            ],
        )
    )
    left = column({"Source": "s"}, "Amount")
    assert built["type"] == "Advanced"
    assert built["field"] == column({"Entity": "Sales"}, "Amount")
    assert built["filter"]["Where"] == [
        {
            "Condition": {
                "And": {
                    "Left": {
                        "Comparison": {
                            "ComparisonKind": 2,
                            "Left": left,
                            "Right": literal("5M"),
                        }
                    },
                    "Right": {
                        "Comparison": {
                            "ComparisonKind": 3,
                            "Left": left,
                            "Right": literal("10.5M"),
                        }
                    },
                }
            }
        }
    ]


def test_advanced_or_with_text_conditions_and_blank() -> None:
    built = build(
        rf.AdvancedFilter(
            field=field("Country"),
            logic="or",
            conditions=[
                rf.Condition(operator="starts_with", value="Ca"),
                rf.Condition(operator="not_contains", value="x"),
            ],
        )
    )
    left = column({"Source": "s"}, "Country")
    assert built["filter"]["Where"] == [
        {
            "Condition": {
                "Or": {
                    "Left": {"StartsWith": {"Left": left, "Right": literal("'Ca'")}},
                    "Right": {
                        "Not": {"Expression": {"Contains": {"Left": left, "Right": literal("'x'")}}}
                    },
                }
            }
        }
    ]
    blank = build(
        rf.AdvancedFilter(field=field("Country"), conditions=[rf.Condition(operator="is_blank")])
    )
    not_blank = build(
        rf.AdvancedFilter(
            field=field("Country"), conditions=[rf.Condition(operator="is_not_blank")]
        )
    )
    is_blank = {"Comparison": {"ComparisonKind": 0, "Left": left, "Right": literal("null")}}
    assert blank["filter"]["Where"] == [{"Condition": is_blank}]
    assert not_blank["filter"]["Where"] == [{"Condition": {"Not": {"Expression": is_blank}}}]


def test_advanced_is_not_and_a_date_comparison() -> None:
    built = build(
        rf.AdvancedFilter(
            field=field("OrderDate"),
            conditions=[rf.Condition(operator="is_not", value="2019-01-01")],
        )
    )
    assert built["filter"]["Where"] == [
        {
            "Condition": {
                "Not": {
                    "Expression": {
                        "Comparison": {
                            "ComparisonKind": 0,
                            "Left": column({"Source": "s"}, "OrderDate"),
                            "Right": literal("datetime'2019-01-01T00:00:00'"),
                        }
                    }
                }
            }
        }
    ]


def test_advanced_measure_filter_matches_the_one_desktop_wrote(document: PbirDocument) -> None:
    """The Agents report's own filter: Rank Filtering Dynamics = 1L on a measure."""
    real = document.visual(VISUAL)[1]["filterConfig"]["filters"][0]
    built = build(
        rf.AdvancedFilter(
            field=field("Rank Filtering Dynamics"),
            conditions=[rf.Condition(operator="is", value=1)],
        )
    )
    assert built["field"] == real["field"]
    assert built["type"] == real["type"] == "Advanced"
    assert built["howCreated"] == real["howCreated"] == "User"
    assert built["filter"]["Version"] == real["filter"]["Version"] == 2
    assert built["filter"]["Where"] == real["filter"]["Where"]
    # Desktop left a stale, unused alias in the real From list; ours lists only what it uses.
    assert built["filter"]["From"] == [{"Name": "#", "Entity": "# Measures", "Type": 0}]
    assert rf.VERIFIED_VARIANTS["advanced_comparison_measure"] is True


def test_top_n_by_a_measure_uses_a_subquery() -> None:
    built = build(
        rf.TopNFilter(field=field("Country"), n=10, by=field("Total Sales")),
    )
    country = column({"Source": "s"}, "Country")
    tables = [
        {"Name": "s", "Entity": "Sales", "Type": 0},
        {"Name": "#", "Entity": "# Measures", "Type": 0},
    ]
    assert built["type"] == "TopN"
    assert built["field"] == column({"Entity": "Sales"}, "Country")
    assert built["filter"] == {
        "Version": 2,
        "From": [
            {
                "Name": "subquery",
                "Expression": {
                    "Subquery": {
                        "Query": {
                            "Version": 2,
                            "From": tables,
                            "Select": [{**country, "Name": "field"}],
                            "OrderBy": [
                                {
                                    "Direction": 2,
                                    "Expression": {
                                        "Measure": {
                                            "Expression": {"SourceRef": {"Source": "#"}},
                                            "Property": "Total Sales",
                                        }
                                    },
                                }
                            ],
                            "Top": 10,
                        }
                    }
                },
                "Type": 2,
            },
            *tables,
        ],
        "Where": [
            {
                "Condition": {
                    "In": {
                        "Expressions": [country],
                        "Table": {"SourceRef": {"Source": "subquery"}},
                    }
                }
            }
        ],
    }


def test_bottom_n_by_a_total_of_a_column() -> None:
    built = build(
        rf.TopNFilter(
            field=field("Country"),
            n=3,
            by=field("Price", aggregation="Average"),
            direction="bottom",
        )
    )
    query = built["filter"]["From"][0]["Expression"]["Subquery"]["Query"]
    assert query["OrderBy"] == [
        {
            "Direction": 1,
            "Expression": {
                "Aggregation": {"Expression": column({"Source": "s"}, "Price"), "Function": 1}
            },
        }
    ]
    assert query["Top"] == 3
    assert [entry["Name"] for entry in built["filter"]["From"]] == ["subquery", "s"]


def _between(built: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = built["filter"]["Where"][0]["Condition"]["Between"]
    return result


def _span(amounts: list[tuple[int, int]], unit: int) -> dict[str, Any]:
    inner: dict[str, Any] = {"Now": {}}
    for amount, step in amounts:
        inner = {"DateAdd": {"Expression": inner, "Amount": amount, "TimeUnit": step}}
    return {"DateSpan": {"Expression": inner, "TimeUnit": unit}}


def test_relative_date_last_30_days() -> None:
    built = build(
        rf.RelativeDateFilter(field=field("OrderDate"), unit="days", direction="last", count=30)
    )
    assert built["type"] == "RelativeDate"
    assert built["filter"]["From"] == [{"Name": "s", "Entity": "Sales", "Type": 0}]
    assert _between(built) == {
        "Expression": column({"Source": "s"}, "OrderDate"),
        "LowerBound": _span([(-30, 0)], 0),
        "UpperBound": _span([(-1, 0)], 0),
    }


def test_relative_date_variants_shift_whole_units() -> None:
    including = build(
        rf.RelativeDateFilter(
            field=field("OrderDate"), unit="days", direction="last", count=7, include_today=True
        )
    )
    assert _between(including)["LowerBound"] == _span([(-6, 0)], 0)
    assert _between(including)["UpperBound"] == _span([], 0)
    months = build(
        rf.RelativeDateFilter(
            field=field("OrderDate"), unit="calendar_months", direction="last", count=2
        )
    )
    assert _between(months)["LowerBound"] == _span([(-2, 2)], 2)
    assert _between(months)["UpperBound"] == _span([(-1, 2)], 2)
    this_year = build(
        rf.RelativeDateFilter(field=field("OrderDate"), unit="calendar_years", direction="this")
    )
    # TimeUnit 3 is a year; 4 is a decade in the semantic query schema.
    assert _between(this_year)["LowerBound"] == _span([], 3)
    assert _between(this_year)["UpperBound"] == _span([], 3)
    last_years = build(
        rf.RelativeDateFilter(field=field("OrderDate"), unit="years", direction="last", count=2)
    )
    assert _between(last_years)["LowerBound"] == _span([(-2, 3)], 0)
    assert _between(last_years)["UpperBound"] == _span([(-1, 0)], 0)
    assert rf.describe_filter(last_years) == "OrderDate is in the last 2 years"
    rolling = build(
        rf.RelativeDateFilter(
            field=field("OrderDate"), unit="weeks", direction="next", count=2, include_today=True
        )
    )
    assert _between(rolling)["LowerBound"] == _span([], 0)
    assert _between(rolling)["UpperBound"] == _span([(-1, 0), (2, 1)], 0)


def test_lock_and_hide_flags_are_written_only_when_set() -> None:
    spec = rf.CategoricalFilter(field=field("Country"), values=["x"])
    plain = build(spec)
    assert "isLockedInViewMode" not in plain
    assert "isHiddenInViewMode" not in plain
    flagged = build(spec, locked=True, hidden=True)
    assert flagged["isLockedInViewMode"] is True
    assert flagged["isHiddenInViewMode"] is True
    assert rf.describe_filter(flagged) == "Country is x (locked, hidden)"


# --- Microsoft validator rules --------------------------------------------------------------


def _source_refs(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        if isinstance(node.get("SourceRef"), dict):
            yield node["SourceRef"]
        else:
            for value in node.values():
                yield from _source_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _source_refs(item)


def _validator_problems(filter_obj: dict[str, Any]) -> list[str]:
    """PBIR_FILTER_ENTITY_IN_WHERE and PBIR_FILTER_SOURCE_UNKNOWN as the CLI checks them."""
    body = filter_obj["filter"]
    aliases = {entry["Name"] for entry in body["From"]}
    problems = []
    for ref in _source_refs(body["Where"]):
        if "Entity" in ref and not ref.get("Source"):
            problems.append("PBIR_FILTER_ENTITY_IN_WHERE")
        elif ref.get("Source") not in aliases:
            problems.append("PBIR_FILTER_SOURCE_UNKNOWN")
    return problems


ALL_SPECS: list[
    rf.CategoricalFilter | rf.AdvancedFilter | rf.TopNFilter | rf.RelativeDateFilter
] = [
    rf.CategoricalFilter(field=field("Country"), values=["a", "b"]),
    rf.CategoricalFilter(field=field("Year"), values=[1], exclude=True),
    rf.AdvancedFilter(
        field=field("Amount"),
        conditions=[
            rf.Condition(operator="greater_than", value=1),
            rf.Condition(operator="is_blank"),
        ],
        logic="or",
    ),
    rf.AdvancedFilter(
        field=field("Total Sales"), conditions=[rf.Condition(operator="less_than", value=5)]
    ),
    rf.AdvancedFilter(
        field=field("Amount", aggregation="Sum"),
        conditions=[rf.Condition(operator="less_than", value=5)],
    ),
    rf.TopNFilter(field=field("Country"), n=5, by=field("Total Sales")),
    rf.TopNFilter(field=field("StoreName"), n=5, by=field("Total Sales"), direction="bottom"),
    rf.RelativeDateFilter(field=field("OrderDate"), unit="months", direction="last", count=3),
]


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.kind)
def test_every_variant_follows_the_validator_rules(spec: Any) -> None:
    built = build(spec)
    assert built["filter"]["Version"] == 2
    assert re.fullmatch(r"Filter[0-9a-f]{20}", built["name"])
    assert built["howCreated"] == "User"
    assert _validator_problems(built) == []
    # Only the top-level field names the table; Where reaches it through the alias.
    assert next(iter(_source_refs(built["field"]))).keys() == {"Entity"}


def test_aggregated_field_is_written_as_desktop_writes_a_total() -> None:
    built = build(
        rf.AdvancedFilter(
            field=field("Amount", aggregation="Sum"),
            conditions=[rf.Condition(operator="greater_than", value=100)],
        )
    )
    assert built["field"] == {
        "Aggregation": {"Expression": column({"Entity": "Sales"}, "Amount"), "Function": 0}
    }
    where = built["filter"]["Where"][0]["Condition"]["Comparison"]
    assert where["Left"] == {
        "Aggregation": {"Expression": column({"Source": "s"}, "Amount"), "Function": 0}
    }
    assert where["Right"] == literal("100M")
    assert rf.describe_filter(built) == "Sum of Amount is greater than 100"


# --- errors in plain English --------------------------------------------------------------------


def test_a_value_that_does_not_fit_the_column_type_names_the_field_and_type() -> None:
    with pytest.raises(DocumentError, match=r"Year is a whole number; '20x9' is not"):
        build(rf.CategoricalFilter(field=field("Year"), values=["20x9"]))
    with pytest.raises(DocumentError, match=r"Year is a whole number; 2014\.5 is not"):
        build(rf.CategoricalFilter(field=field("Year"), values=[2014.5]))
    with pytest.raises(DocumentError, match=r"OrderDate is a date; 'soon' is not"):
        build(rf.CategoricalFilter(field=field("OrderDate"), values=["soon"]))
    with pytest.raises(DocumentError, match=r"Active is true or false; 'maybe' is not"):
        build(rf.CategoricalFilter(field=field("Active"), values=["maybe"]))


def test_conditions_must_suit_the_field() -> None:
    with pytest.raises(DocumentError, match="not text"):
        build(
            rf.AdvancedFilter(
                field=field("Year"), conditions=[rf.Condition(operator="contains", value="1")]
            )
        )
    with pytest.raises(DocumentError, match="cannot be compared with greater or less than"):
        build(
            rf.AdvancedFilter(
                field=field("Country"),
                conditions=[rf.Condition(operator="greater_than", value="a")],
            )
        )
    with pytest.raises(DocumentError, match="Give a value"):
        build(rf.AdvancedFilter(field=field("Year"), conditions=[rf.Condition(operator="is")]))
    with pytest.raises(DocumentError, match="Amount is a fixed decimal number; 'lots' is not"):
        build(
            rf.AdvancedFilter(
                field=field("Amount"), conditions=[rf.Condition(operator="less_than", value="lots")]
            )
        )


def test_field_problems_are_explained() -> None:
    with pytest.raises(DocumentError, match="use a condition"):
        build(rf.CategoricalFilter(field=field("Total Sales"), values=["x"]))
    with pytest.raises(DocumentError, match="cannot tell what kind of values Mystery holds"):
        build(rf.CategoricalFilter(field=field("Mystery"), values=["x"]))
    with pytest.raises(DocumentError, match="not a date column"):
        build(rf.RelativeDateFilter(field=field("Country"), unit="days", direction="last"))
    with pytest.raises(DocumentError, match="'This' needs days or a calendar"):
        build(rf.RelativeDateFilter(field=field("OrderDate"), unit="months", direction="this"))
    with pytest.raises(DocumentError, match="Choose how to total Price"):
        build(rf.TopNFilter(field=field("Country"), n=3, by=field("Price")))
    with pytest.raises(DocumentError, match="already totals"):
        build(
            rf.TopNFilter(field=field("Country"), n=3, by=field("Total Sales", aggregation="Sum"))
        )
    with pytest.raises(DocumentError, match="cannot be totalled"):
        build(
            rf.AdvancedFilter(
                field=field("Country", aggregation="Sum"),
                conditions=[rf.Condition(operator="is", value=1)],
            )
        )
    with pytest.raises(DocumentError, match="no field"):
        build(rf.CategoricalFilter(field=field("Nope"), values=["x"]))


def test_a_measure_of_unknown_type_takes_a_number() -> None:
    built = build(
        rf.AdvancedFilter(
            field=field("Total Sales"),
            conditions=[rf.Condition(operator="greater_than", value="1,500.5")],
        )
    )
    assert built["filter"]["Where"][0]["Condition"]["Comparison"]["Right"] == literal("1500.5D")
    with pytest.raises(DocumentError, match="takes a number"):
        build(
            rf.AdvancedFilter(
                field=field("Total Sales"), conditions=[rf.Condition(operator="is", value="abc")]
            )
        )


def test_specs_reject_out_of_range_input() -> None:
    with pytest.raises(ValidationError):
        rf.TopNFilter(field=field("Country"), n=0, by=field("Total Sales"))
    with pytest.raises(ValidationError):
        rf.TopNFilter(field=field("Country"), n=1001, by=field("Total Sales"))
    with pytest.raises(ValidationError):
        rf.AdvancedFilter(
            field=field("Amount"),
            conditions=[rf.Condition(operator="is", value=1)] * 3,
        )
    with pytest.raises(ValidationError):
        rf.CategoricalFilter(field=field("Country"), values=[])
    with pytest.raises(ValidationError):
        rf.SetPageFilter.model_validate(
            {"op": "set_page_filter", "page": "P", "filter": {"kind": "categorical"}, "extra": 1}
        )


def test_operations_parse_from_json_with_a_discriminated_filter() -> None:
    op = rf.SetVisualFilter.model_validate(
        {
            "op": "set_visual_filter",
            "visual": "v1",
            "locked": True,
            "filter": {
                "kind": "top_n",
                "field": {"name": "Country"},
                "n": 5,
                "by": {"name": "Total Sales"},
            },
        }
    )
    assert isinstance(op.filter, rf.TopNFilter)
    assert op.filter.direction == "top"
    assert op.locked is True
    operations = rf.FILTER_OPERATIONS
    assert operations[0] is rf.SetPageFilter
    assert operations[1:] == (rf.SetVisualFilter, rf.SetReportFilter, rf.ClearFilter)


# --- names ----------------------------------------------------------------------------------------


def test_default_names_are_filter_plus_20_lowercase_hex() -> None:
    spec = rf.CategoricalFilter(field=field("Country"), values=["x"])
    names = {rf.build_filter(spec, FakeModel(), set())["name"] for _ in range(20)}
    assert len(names) == 20
    assert all(re.fullmatch(r"Filter[0-9a-f]{20}", name) for name in names)


def test_a_name_in_use_is_skipped() -> None:
    spec = rf.CategoricalFilter(field=field("Country"), values=["x"])
    built = rf.build_filter(
        spec, FakeModel(), {A20}, name_factory=seeded(A20, A20, "Filter" + "b" * 20)
    )
    assert built["name"] == "Filter" + "b" * 20


def test_collect_names_covers_every_scope_and_bookmarks(document: PbirDocument) -> None:
    page = document.pages[PAGE]
    page.data["filterConfig"] = {"filters": [{"name": "Filter" + "1" * 20}]}
    document.report["filterConfig"] = {"filters": [{"name": "Filter" + "2" * 20}]}
    document.package.write_json(
        ROOT + "bookmarks/b1.bookmark.json",
        {
            "name": "b1",
            "explorationState": {
                "sections": {PAGE: {"filters": {"byExpr": [{"name": "Filter" + "3" * 20}]}}}
            },
        },
    )
    assert rf.collect_filter_names(document) == {
        REAL_NAME,
        "Filter" + "1" * 20,
        "Filter" + "2" * 20,
        "Filter" + "3" * 20,
    }


# --- applying -----------------------------------------------------------------------------


def _country_filter(*values: str) -> rf.CategoricalFilter:
    return rf.CategoricalFilter(field=field("StoreName", table="DimStore"), values=list(values))


def test_page_filter_is_added_touched_and_written(document: PbirDocument) -> None:
    summary = rf.apply_filter_operation(
        document,
        rf.SetPageFilter(
            page="ReportSection8454c812b60b8bb83b99", filter=_country_filter("A", "B")
        ),
        FakeModel(),
        name_factory=seeded(A20),
    )
    page = document.pages[PAGE]
    assert summary == f"Added a filter on the {page.display_name} page: StoreName is one of A, B"
    assert [f["name"] for f in page.data["filterConfig"]["filters"]] == [A20]
    assert document.commit() == [f"{ROOT}pages/{PAGE}/page.json"]
    assert (
        document.package.read_json(f"{ROOT}pages/{PAGE}/page.json")["filterConfig"]["filters"][0][
            "name"
        ]
        == A20
    )


def test_page_is_found_by_its_display_name(document: PbirDocument) -> None:
    display = document.pages[PAGE].display_name
    rf.apply_filter_operation(
        document,
        rf.SetPageFilter(page=display.upper(), filter=_country_filter("A")),
        FakeModel(),
    )
    assert "filterConfig" in document.pages[PAGE].data


def test_report_filter_goes_into_report_json(document: PbirDocument) -> None:
    summary = rf.apply_filter_operation(
        document,
        rf.SetReportFilter(filter=_country_filter("A"), locked=True),
        FakeModel(),
        name_factory=seeded(A20),
    )
    assert summary == "Added a filter on the whole report: StoreName is A (locked)"
    filters = document.report["filterConfig"]["filters"]
    assert filters[0]["name"] == A20
    assert filters[0]["isLockedInViewMode"] is True
    assert document.commit() == [f"{ROOT}report.json"]


def test_visual_filter_on_the_same_field_replaces_and_keeps_the_name(
    document: PbirDocument,
) -> None:
    op = rf.SetVisualFilter(
        visual=VISUAL,
        filter=rf.AdvancedFilter(
            field=field("Rank Filtering Dynamics"),
            conditions=[rf.Condition(operator="less_or_equal", value=5)],
        ),
    )
    summary = rf.apply_filter_operation(document, op, FakeModel(), name_factory=seeded(A20))
    filters = document.visual(VISUAL)[1]["filterConfig"]["filters"]
    assert len(filters) == 1
    assert filters[0]["name"] == REAL_NAME
    assert filters[0]["filter"]["Where"][0]["Condition"]["Comparison"]["ComparisonKind"] == 4
    assert filters[0]["filter"]["Where"][0]["Condition"]["Comparison"]["Right"] == literal("5L")
    assert summary.startswith("Replaced a filter on ")
    assert summary.endswith("Rank Filtering Dynamics is less than or equal to 5")
    assert A20 not in rf.collect_filter_names(document)


def test_a_new_field_is_appended_next_to_the_existing_filter(document: PbirDocument) -> None:
    rf.apply_filter_operation(
        document,
        rf.SetVisualFilter(visual=VISUAL, filter=_country_filter("A")),
        FakeModel(),
        name_factory=seeded(A20),
    )
    filters = document.visual(VISUAL)[1]["filterConfig"]["filters"]
    assert [f["name"] for f in filters] == [REAL_NAME, A20]
    assert document.commit() == [f"{ROOT}pages/{PAGE}/visuals/{VISUAL}/visual.json"]


def test_setting_the_same_field_twice_never_duplicates(document: PbirDocument) -> None:
    for values in (["A"], ["B", "C"]):
        rf.apply_filter_operation(
            document, rf.SetPageFilter(page=PAGE, filter=_country_filter(*values)), FakeModel()
        )
    filters = document.pages[PAGE].data["filterConfig"]["filters"]
    assert len(filters) == 1
    assert rf.summarize_filters(document.pages[PAGE].data) == ["StoreName is one of B, C"]


def test_names_stay_unique_across_scopes_and_a_batch(document: PbirDocument) -> None:
    names = rf.collect_filter_names(document)
    fresh = seeded(REAL_NAME, A20, A20, "Filter" + "c" * 20)
    model = FakeModel()
    rf.apply_filter_operation(
        document,
        rf.SetPageFilter(page=PAGE, filter=_country_filter("A")),
        model,
        names,
        name_factory=fresh,
    )
    rf.apply_filter_operation(
        document, rf.SetReportFilter(filter=_country_filter("B")), model, names, name_factory=fresh
    )
    page_name = document.pages[PAGE].data["filterConfig"]["filters"][0]["name"]
    report_name = document.report["filterConfig"]["filters"][0]["name"]
    assert page_name == A20
    assert report_name == "Filter" + "c" * 20
    assert names == {REAL_NAME, A20, "Filter" + "c" * 20}
    assert rf.collect_filter_names(document) == names


def test_a_bookmarked_filter_name_is_not_reused(document: PbirDocument) -> None:
    document.package.write_json(
        ROOT + "bookmarks/b1.bookmark.json",
        {"explorationState": {"filters": [{"name": A20}]}},
    )
    rf.apply_filter_operation(
        document,
        rf.SetReportFilter(filter=_country_filter("A")),
        FakeModel(),
        name_factory=seeded(A20, "Filter" + "d" * 20),
    )
    assert document.report["filterConfig"]["filters"][0]["name"] == "Filter" + "d" * 20


def test_a_failed_operation_leaves_the_document_unchanged(document: PbirDocument) -> None:
    before = repr(document.report), repr(document.pages[PAGE].data)
    with pytest.raises(DocumentError):
        rf.apply_filter_operation(
            document,
            rf.SetPageFilter(
                page=PAGE, filter=rf.CategoricalFilter(field=field("Year"), values=["20x9"])
            ),
            FakeModel(),
        )
    with pytest.raises(DocumentError, match="no page named"):
        rf.apply_filter_operation(
            document, rf.SetPageFilter(page="Nowhere", filter=_country_filter("A")), FakeModel()
        )
    assert (repr(document.report), repr(document.pages[PAGE].data)) == before
    assert document.commit() == []


def test_visual_targets_are_checked(document: PbirDocument) -> None:
    with pytest.raises(DocumentError, match="no visual"):
        rf.apply_filter_operation(
            document, rf.SetVisualFilter(visual="nope", filter=_country_filter("A")), FakeModel()
        )
    document.visual(VISUAL)[1]["visual"]["visualType"] = "textbox"
    with pytest.raises(DocumentError, match="cannot be filtered"):
        rf.apply_filter_operation(
            document, rf.SetVisualFilter(visual=VISUAL, filter=_country_filter("A")), FakeModel()
        )
    with pytest.raises(DocumentError, match="not a filter operation"):
        rf.apply_filter_operation(document, object(), FakeModel())


def test_clear_one_filter_by_field_keeps_the_others(document: PbirDocument) -> None:
    rf.apply_filter_operation(
        document,
        rf.SetVisualFilter(visual=VISUAL, filter=_country_filter("A")),
        FakeModel(),
        name_factory=seeded(A20),
    )
    summary = rf.apply_filter_operation(
        document,
        rf.ClearFilter(scope="visual", target=VISUAL, field=field("storename")),
        FakeModel(),
    )
    assert summary.startswith("Removed the filter on StoreName from ")
    filters = document.visual(VISUAL)[1]["filterConfig"]["filters"]
    assert [f["name"] for f in filters] == [REAL_NAME]


def test_clear_all_removes_the_empty_filter_config(document: PbirDocument) -> None:
    summary = rf.apply_filter_operation(
        document, rf.ClearFilter(scope="visual", target=VISUAL), FakeModel()
    )
    assert summary.startswith("Removed the filter from ")
    assert "filterConfig" not in document.visual(VISUAL)[1]
    with pytest.raises(DocumentError, match="There are no filters on"):
        rf.apply_filter_operation(
            document, rf.ClearFilter(scope="visual", target=VISUAL), FakeModel()
        )


def test_clear_keeps_other_filter_config_keys(document: PbirDocument) -> None:
    document.report["filterConfig"] = {
        "filters": [{"name": A20, "field": column({"Entity": "DimStore"}, "StoreName")}],
        "filterSortOrder": "Custom",
    }
    summary = rf.apply_filter_operation(
        document,
        rf.ClearFilter(scope="report", field=field("StoreName")),
        FakeModel(),
    )
    assert summary == "Removed the filter on StoreName from the whole report"
    assert document.report["filterConfig"] == {"filterSortOrder": "Custom"}


def test_clear_names_a_missing_filter_and_a_missing_target(document: PbirDocument) -> None:
    with pytest.raises(DocumentError, match="There is no filter on Country on the .* page"):
        rf.apply_filter_operation(
            document, rf.ClearFilter(scope="page", target=PAGE, field=field("Country")), FakeModel()
        )
    with pytest.raises(DocumentError, match="Say which page"):
        rf.apply_filter_operation(document, rf.ClearFilter(scope="page"), FakeModel())
    with pytest.raises(DocumentError, match="Say which visual"):
        rf.apply_filter_operation(document, rf.ClearFilter(scope="visual"), FakeModel())


def test_clear_all_page_filters_counts_them(document: PbirDocument) -> None:
    model = FakeModel()
    rf.apply_filter_operation(
        document, rf.SetPageFilter(page=PAGE, filter=_country_filter("A")), model
    )
    rf.apply_filter_operation(
        document,
        rf.SetPageFilter(
            page=PAGE,
            filter=rf.AdvancedFilter(
                field=field("Year"), conditions=[rf.Condition(operator="is", value=2020)]
            ),
        ),
        model,
    )
    summary = rf.apply_filter_operation(document, rf.ClearFilter(scope="page", target=PAGE), model)
    assert summary.startswith("Removed all 2 filters from the ")
    assert "filterConfig" not in document.pages[PAGE].data


# --- reading filters back -----------------------------------------------------------------


def test_summaries_of_the_real_filter_and_every_variant(document: PbirDocument) -> None:
    assert rf.summarize_filters(document.visual(VISUAL)[1]) == ["Rank Filtering Dynamics is 1"]
    assert rf.summarize_filters({}) == []
    assert rf.summarize_filters({"filterConfig": {}}) == []
    texts = {
        "categorical one": (
            rf.CategoricalFilter(field=field("Country"), values=["a"]),
            "Country is a",
        ),
        "categorical excluded one": (
            rf.CategoricalFilter(field=field("Country"), values=["a"], exclude=True),
            "Country is not a",
        ),
        "many": (
            rf.CategoricalFilter(field=field("Year"), values=list(range(1, 9)), exclude=True),
            "Year is not one of 1, 2, 3, 4, 5, 6 and 2 more",
        ),
        "blank value": (
            rf.CategoricalFilter(field=field("Country"), values=[None, "a"]),
            "Country is one of (Blank), a",
        ),
        "range": (
            rf.AdvancedFilter(
                field=field("Year"),
                conditions=[
                    rf.Condition(operator="greater_than", value=2010),
                    rf.Condition(operator="less_or_equal", value=2020),
                ],
            ),
            "Year is greater than 2010 and is less than or equal to 2020",
        ),
        "text or blank": (
            rf.AdvancedFilter(
                field=field("Country"),
                logic="or",
                conditions=[
                    rf.Condition(operator="not_starts_with", value="A"),
                    rf.Condition(operator="is_not_blank"),
                ],
            ),
            "Country does not start with A or is not blank",
        ),
        "date": (
            rf.AdvancedFilter(
                field=field("OrderDate"),
                conditions=[rf.Condition(operator="greater_or_equal", value="2019-05-01")],
            ),
            "OrderDate is greater than or equal to 2019-05-01",
        ),
        "top": (
            rf.TopNFilter(field=field("Country"), n=10, by=field("Total Sales")),
            "Top 10 Country by Total Sales",
        ),
        "bottom": (
            rf.TopNFilter(
                field=field("Country"),
                n=3,
                by=field("Price", aggregation="Sum"),
                direction="bottom",
            ),
            "Bottom 3 Country by Sum of Price",
        ),
        "days": (
            rf.RelativeDateFilter(
                field=field("OrderDate"), unit="days", direction="last", count=30
            ),
            "OrderDate is in the last 30 days",
        ),
        "one week": (
            rf.RelativeDateFilter(
                field=field("OrderDate"),
                unit="calendar_weeks",
                direction="last",
                count=2,
                include_today=True,
            ),
            "OrderDate is in the last 2 calendar weeks, including this week",
        ),
        "next": (
            rf.RelativeDateFilter(
                field=field("OrderDate"), unit="months", direction="next", count=2
            ),
            "OrderDate is in the next 2 months",
        ),
        "this": (
            rf.RelativeDateFilter(
                field=field("OrderDate"), unit="calendar_months", direction="this"
            ),
            "OrderDate is in this calendar month",
        ),
        "today": (
            rf.RelativeDateFilter(field=field("OrderDate"), unit="days", direction="this"),
            "OrderDate is today",
        ),
    }
    for label, (spec, expected) in texts.items():
        assert rf.describe_filter(build(spec)) == expected, label


def test_a_filter_studio_does_not_write_is_described_safely() -> None:
    odd = {"name": "x", "type": "Tuple", "field": column({"Entity": "T"}, "C"), "filter": {}}
    assert rf.describe_filter(odd) == "C has a Tuple filter that Studio cannot describe"
    passthrough = {"field": {"HierarchyLevel": {}}, "type": "Passthrough", "filter": {}}
    assert (
        rf.describe_filter(passthrough)
        == "a field has a Passthrough filter that Studio cannot describe"
    )
    assert rf.describe_filter({**odd, "type": "Advanced"}) == (
        "C has an Advanced filter that Studio cannot describe"
    )
    between = {
        "type": "RelativeDate",
        "field": column({"Entity": "T"}, "D"),
        "filter": {"Where": [{"Condition": {"Between": {"Expression": {}}}}]},
    }
    assert rf.describe_filter(between) == "D is in a relative date range"


def test_summaries_read_desktops_own_list_and_between_shapes() -> None:
    desktop = {
        "type": "Categorical",
        "field": column({"Entity": "T"}, "C"),
        "filter": {
            "Version": 2,
            "From": [{"Name": "t", "Entity": "T", "Type": 0}],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [column({"Source": "t"}, "C")],
                            "Values": [[literal("'O''Neil'")], [literal("1.5M")], [literal("2L")]],
                        }
                    }
                }
            ],
        },
    }
    assert rf.describe_filter(desktop) == "C is one of O'Neil, 1.5, 2"


# --- what the local canvas can draw -------------------------------------------------------


def test_the_canvas_reason_matches_what_the_local_engine_can_evaluate(
    document: PbirDocument,
) -> None:
    real = document.visual(VISUAL)[1]["filterConfig"]["filters"][0]
    assert rf.filter_unsupported_reason(real) is None
    reasons = {
        "top": rf.filter_unsupported_reason(build(ALL_SPECS[5])),
        "bottom": rf.filter_unsupported_reason(build(ALL_SPECS[6])),
        "date": rf.filter_unsupported_reason(build(ALL_SPECS[7])),
        "aggregate": rf.filter_unsupported_reason(build(ALL_SPECS[4])),
        "text": rf.filter_unsupported_reason(
            build(
                rf.AdvancedFilter(
                    field=field("Country"),
                    conditions=[rf.Condition(operator="contains", value="a")],
                )
            )
        ),
        "decimal": rf.filter_unsupported_reason(build(ALL_SPECS[2])),
        "datetime": rf.filter_unsupported_reason(
            build(rf.CategoricalFilter(field=field("OrderDate"), values=["2019-01-01"]))
        ),
    }
    assert reasons["top"] == "A top or bottom N filter is drawn by Power BI, not by Studio's canvas"
    assert reasons["bottom"] == reasons["top"]
    assert reasons["date"] == "A relative date filter is drawn by Power BI, not by Studio's canvas"
    assert reasons["aggregate"] == (
        "A filter on a total or a hierarchy is drawn by Power BI, not by Studio's canvas"
    )
    assert reasons["text"] == (
        "A 'contains' or 'starts with' filter is drawn by Power BI, not by Studio's canvas"
    )
    assert reasons["decimal"] == (
        "A filter on dates or fixed decimals is drawn by Power BI, not by Studio's canvas"
    )
    assert reasons["datetime"] == reasons["decimal"]


@pytest.mark.parametrize(
    "spec",
    [
        rf.CategoricalFilter(field=field("Country"), values=["a", "b"]),
        rf.CategoricalFilter(field=field("Year"), values=[1, 2], exclude=True),
        rf.CategoricalFilter(field=field("Country"), values=[None]),
        rf.AdvancedFilter(
            field=field("Year"),
            conditions=[
                rf.Condition(operator="greater_than", value=1),
                rf.Condition(operator="less_than", value=9),
            ],
        ),
        rf.AdvancedFilter(field=field("Country"), conditions=[rf.Condition(operator="is_blank")]),
        rf.AdvancedFilter(
            field=field("Rank Filtering Dynamics"),
            conditions=[rf.Condition(operator="is", value=1)],
        ),
    ],
)
def test_filters_the_local_engine_can_draw_report_no_reason(spec: Any) -> None:
    assert rf.filter_unsupported_reason(build(spec)) is None


def test_the_canvas_reason_ignores_a_filter_without_a_body_and_limits_depth() -> None:
    assert rf.filter_unsupported_reason({"name": "x", "field": {}}) is None
    node: dict[str, Any] = {
        "Comparison": {
            "ComparisonKind": 0,
            "Left": column({"Source": "s"}, "C"),
            "Right": literal("1L"),
        }
    }
    for _ in range(14):
        node = {"Not": {"Expression": node}}
    deep = {"type": "Advanced", "filter": {"Where": [{"Condition": node}]}}
    assert (
        rf.filter_unsupported_reason(deep) == "This filter is too deeply nested for Studio's canvas"
    )
    bad_kind = {
        "type": "Advanced",
        "filter": {
            "Where": [
                {
                    "Condition": {
                        "Comparison": {
                            "ComparisonKind": 9,
                            "Left": column({"Source": "s"}, "C"),
                            "Right": literal("1L"),
                        }
                    }
                }
            ]
        },
    }
    assert rf.filter_unsupported_reason(bad_kind) == "Studio's canvas cannot apply this comparison"


def _in_filter(count: int) -> dict[str, Any]:
    return build(
        rf.CategoricalFilter(field=field("Country"), values=[f"c{i}" for i in range(count)])
    )


def test_the_canvas_takes_at_most_100_values_in_one_list() -> None:
    # live_report.Compiler.condition refuses an IN list of more than 100 rows.
    assert rf.filter_unsupported_reason(_in_filter(100)) is None
    assert rf.filter_unsupported_reason(_in_filter(101)) == (
        "A filter with more than 100 values is drawn by Power BI, not by Studio's canvas"
    )
    assert rf.filter_unsupported_reason(_in_filter(200)) is not None


def test_a_negated_comparison_and_an_unset_filter_card_read_naturally() -> None:
    negated = {
        "type": "Advanced",
        "field": column({"Entity": "T"}, "A"),
        "filter": {
            "Where": [
                {
                    "Condition": {
                        "Not": {
                            "Expression": {
                                "Comparison": {
                                    "ComparisonKind": 1,
                                    "Left": column({"Source": "t"}, "A"),
                                    "Right": literal("5L"),
                                }
                            }
                        }
                    }
                }
            ]
        },
    }
    assert rf.describe_filter(negated) == "A is not greater than 5"
    card = {"name": "Filterx", "field": column({"Entity": "T"}, "Country"), "type": "Categorical"}
    assert rf.describe_filter(card) == "Country (no values selected)"
    level = {
        "type": "Categorical",
        "field": {"HierarchyLevel": {"Level": "Year", "Expression": {}}},
        "filter": {
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [{"HierarchyLevel": {"Level": "Year"}}],
                            "Values": [[literal("2020L")]],
                        }
                    }
                }
            ]
        },
    }
    assert rf.describe_filter(level) == "Year is 2020"


@pytest.mark.parametrize(
    "config",
    [None, [], {"filters": None}, {"filters": {}}, {"filters": "x"}],
)
def test_a_malformed_filter_config_never_escapes_as_a_type_error(
    document: PbirDocument, config: Any
) -> None:
    document.report["filterConfig"] = config
    op = rf.SetReportFilter(filter=_country_filter("A"))
    if config is None or config == {"filters": None}:
        rf.apply_filter_operation(document, op, FakeModel(), name_factory=seeded(A20))
        assert [f["name"] for f in document.report["filterConfig"]["filters"]] == [A20]
        return
    with pytest.raises(DocumentError, match="not in a form Studio can edit"):
        rf.apply_filter_operation(document, op, FakeModel())
    with pytest.raises(DocumentError, match="not in a form Studio can edit"):
        rf.apply_filter_operation(
            document, rf.ClearFilter(scope="report", field=field("Country")), FakeModel()
        )


def test_a_whole_number_beyond_64_bits_is_refused_and_the_edge_is_kept() -> None:
    edge = build(rf.CategoricalFilter(field=field("Year"), values=[2**63 - 1, -(2**63)]))
    assert edge["filter"]["Where"][0]["Condition"]["In"]["Values"] == [
        [literal("9223372036854775807L")],
        [literal("-9223372036854775808L")],
    ]
    for value in (2**63, 2**70, 1e20):
        with pytest.raises(DocumentError, match=r"too large for Year, a whole number"):
            build(rf.CategoricalFilter(field=field("Year"), values=[value]))
    # A measure of unknown type falls back to a decimal literal instead of failing.
    big = build(
        rf.AdvancedFilter(
            field=field("Total Sales"), conditions=[rf.Condition(operator="is", value=2**70)]
        )
    )
    right = big["filter"]["Where"][0]["Condition"]["Comparison"]["Right"]
    assert right["Literal"]["Value"].endswith("D")


def test_replacing_a_filter_keeps_its_card_formatting_and_lock(document: PbirDocument) -> None:
    old = document.visual(VISUAL)[1]["filterConfig"]["filters"][0]
    old["displayName"] = "Top rank"
    old["objects"] = {"general": [{"properties": {"x": 1}}]}
    old["isLockedInViewMode"] = True
    op = rf.SetVisualFilter(
        visual=VISUAL,
        filter=rf.AdvancedFilter(
            field=field("Rank Filtering Dynamics"),
            conditions=[rf.Condition(operator="less_or_equal", value=5)],
        ),
    )
    summary = rf.apply_filter_operation(document, op, FakeModel(), name_factory=seeded(A20))
    new = document.visual(VISUAL)[1]["filterConfig"]["filters"][0]
    assert new["name"] == REAL_NAME
    assert new["displayName"] == "Top rank"
    assert new["objects"] == {"general": [{"properties": {"x": 1}}]}
    assert new["isLockedInViewMode"] is True
    assert new["filter"]["Where"][0]["Condition"]["Comparison"]["ComparisonKind"] == 4
    assert summary.endswith("is less than or equal to 5 (locked)")


# --- variants -----------------------------------------------------------------------------


def test_spec_variants_name_what_needs_a_desktop_golden() -> None:
    model = FakeModel()
    measure = rf.AdvancedFilter(
        field=field("Rank Filtering Dynamics"), conditions=[rf.Condition(operator="is", value=1)]
    )
    assert rf.spec_variants(measure, model) == ["advanced_comparison_measure"]
    assert all(rf.VERIFIED_VARIANTS[key] for key in rf.spec_variants(measure, model))
    column_range = rf.AdvancedFilter(
        field=field("Year"),
        logic="or",
        conditions=[
            rf.Condition(operator="is_not", value=1),
            rf.Condition(operator="is_blank"),
        ],
    )
    assert rf.spec_variants(column_range, model) == [
        "advanced_or",
        "advanced_comparison_column",
        "advanced_not",
        "advanced_blank",
    ]
    assert rf.spec_variants(
        rf.CategoricalFilter(field=field("Country"), values=["a"], exclude=True), model, locked=True
    ) == ["categorical_not_in", "locked_in_view_mode"]
    assert rf.spec_variants(ALL_SPECS[5], model) == ["top_n"]
    assert rf.spec_variants(ALL_SPECS[6], model, hidden=True) == [
        "bottom_n",
        "hidden_in_view_mode",
    ]
    assert rf.spec_variants(ALL_SPECS[7], model) == ["relative_date_rolling"]
    assert rf.spec_variants(
        rf.RelativeDateFilter(field=field("OrderDate"), unit="days", direction="last"), model
    ) == ["relative_date_days"]
    assert rf.spec_variants(
        rf.RelativeDateFilter(field=field("OrderDate"), unit="calendar_years", direction="this"),
        model,
    ) == ["relative_date_calendar"]
    assert rf.spec_variants(
        rf.AdvancedFilter(
            field=field("OrderDate"), conditions=[rf.Condition(operator="is", value="2020-01-01")]
        ),
        model,
    ) == ["advanced_date_comparison"]
    assert rf.spec_variants(
        rf.AdvancedFilter(
            field=field("Country"), conditions=[rf.Condition(operator="not_contains", value="a")]
        ),
        model,
    ) == ["advanced_text", "advanced_not"]
    assert set(rf.VERIFIED_VARIANTS) >= {
        "categorical_in",
        "top_n",
        "relative_date_calendar",
        "advanced_aggregated_field",
    }
    # Every variant was checked in Desktop on 2026-09-28 (see the comment on VERIFIED_VARIANTS).
    assert all(rf.VERIFIED_VARIANTS.values())


def test_pandas_dtypes_from_the_model_info_are_understood() -> None:
    class PandasModel(FakeModel):
        def data_type(self, table: str, name: str) -> str | int | None:
            return {"Year": "int64", "Price": "float64", "OrderDate": "datetime64[ns]"}.get(
                name, "object"
            )

    model = PandasModel()
    built = rf.build_filter(rf.CategoricalFilter(field=field("Year"), values=[2014]), model, set())
    assert built["filter"]["Where"][0]["Condition"]["In"]["Values"] == [[literal("2014L")]]
    built = rf.build_filter(
        rf.CategoricalFilter(field=field("Country"), values=[2014]), model, set()
    )
    assert built["filter"]["Where"][0]["Condition"]["In"]["Values"] == [[literal("'2014'")]]


def test_alias_collisions_get_a_number() -> None:
    model = FakeModel()
    model.columns["Sales2"] = {"Region": TEXT}
    model.columns["Store"] = {"Name": TEXT}
    model.measures["Store Total"] = ("Sales", INTEGER)
    built = rf.build_filter(
        rf.TopNFilter(field=field("Name", table="Store"), n=2, by=field("Store Total")),
        model,
        set(),
    )
    aliases = {entry["Entity"]: entry["Name"] for entry in built["filter"]["From"][1:]}
    assert aliases == {"Store": "s", "Sales": "s1"}
    assert _validator_problems(built) == []


# --- module boundaries --------------------------------------------------------------------


def test_the_module_does_not_import_report_edits() -> None:
    source = Path(rf.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("report_edits" in name for name in imported)
    assert "\r" not in source
