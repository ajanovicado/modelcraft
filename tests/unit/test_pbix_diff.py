"""Semantic diff and the survival check, on hand-built packages."""

from __future__ import annotations

import copy
import zipfile
from pathlib import Path
from typing import Any

import pytest
from pbix_factory import (
    DEFINITION,
    content_types,
    dump,
    make_pbix,
    report_parts,
    schema,
    visual,
)

from ai_powerbi_analyst.studio.pbix_diff import (
    MISSING,
    Change,
    SemanticDiff,
    semantic_diff,
    survival,
)
from ai_powerbi_analyst.studio.pbix_package import PbixPackage

V1 = DEFINITION + "pages/p1/visuals/v1/visual.json"
V2 = DEFINITION + "pages/p1/visuals/v2/visual.json"
PAGE1 = DEFINITION + "pages/p1/page.json"
REPORT = DEFINITION + "report.json"
IMAGE = "Report/StaticResources/RegisteredResources/logo.png"


def _pbix(tmp_path: Path, name: str, parts: dict[str, bytes], **kwargs: Any) -> Path:
    return make_pbix(tmp_path / name, parts, **kwargs)


def _basic(**overrides: bytes) -> dict[str, bytes]:
    parts = report_parts([("p1", "Overview", [visual("v1", "Sales by region", x=10.5)])])
    parts.update(overrides)
    return parts


def _report_with_image() -> dict[str, Any]:
    return {
        "$schema": schema("report", "3.3.0"),
        "themeCollection": {"baseTheme": {"name": "CY24SU02", "type": "SharedResources"}},
        "resourcePackages": [
            {
                "name": "SharedResources",
                "type": "SharedResources",
                "items": [
                    {"name": "CY24SU02", "path": "BaseThemes/CY24SU02.json", "type": "BaseTheme"}
                ],
            },
            {
                "name": "RegisteredResources",
                "type": "RegisteredResources",
                "items": [{"name": "logo.png", "path": "logo.png", "type": "Image"}],
            },
        ],
    }


def _with_image(visuals: list[dict[str, Any]] | None = None) -> dict[str, bytes]:
    parts = report_parts(
        [("p1", "Overview", visuals or [visual("v1", "Sales by region")])],
        report=_report_with_image(),
        resources={
            IMAGE: b"\x89PNG-image",
            "Report/StaticResources/SharedResources/BaseThemes/CY24SU02.json": dump({"name": "b"}),
        },
    )
    return parts


def _edit(parts: dict[str, bytes], part: str, change: Any) -> dict[str, bytes]:
    """A copy of ``parts`` with one JSON part changed by ``change(document)``."""
    import json

    document = json.loads(parts[part].decode("utf-8"))
    change(document)
    return {**parts, part: dump(document)}


# --- semantic_diff: what is NOT a difference -------------------------------------------------


def test_identical_packages_have_an_empty_diff(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _basic())
    b = _pbix(tmp_path, "b.pbix", _basic())
    result = semantic_diff(a, b)
    assert result.empty
    assert result == SemanticDiff((), (), (), (), ())
    assert result.summary() == []


def test_key_order_whitespace_and_encoding_are_not_differences(tmp_path: Path) -> None:
    parts = _basic()
    document = {"b": [1, {"y": 2, "x": 1}], "a": {"k": "café"}}
    left = {**parts, V1: dump(document)}
    right_forms = {
        "reordered": dump({"a": {"k": "café"}, "b": [1, {"x": 1, "y": 2}]}, sort_keys=True),
        "indented": dump(document, indent=4),
        "bom": dump(document, bom=True),
        "utf16": dump(document, utf16=True),
    }
    a = _pbix(tmp_path, "a.pbix", left)
    for label, data in right_forms.items():
        b = _pbix(tmp_path, f"{label}.pbix", {**parts, V1: data})
        assert semantic_diff(a, b).empty, label


def test_float_spelling_is_not_a_difference(tmp_path: Path) -> None:
    def parts(width: float, literal: str) -> dict[str, bytes]:
        item = visual("v1", "Sales by region", width=width)
        item["visual"]["objects"] = {
            "labels": [{"properties": {"fontSize": {"expr": {"Literal": {"Value": literal}}}}}]
        }
        return _basic(**{V1: dump(item)})

    python_style = _pbix(tmp_path, "a.pbix", parts(966.1417322834645, "966.1417322834645D"))
    desktop_style = _pbix(tmp_path, "b.pbix", parts(966.14173228346453, "966.14173228346453D"))
    assert semantic_diff(python_style, desktop_style).empty
    # A different suffix is a different literal type; that is a real change.
    long_style = _pbix(tmp_path, "c.pbix", parts(966.1417322834645, "966L"))
    changed = semantic_diff(python_style, long_style).changed
    assert [c.path for c in changed] == [
        "/visual/objects/labels/0/properties/fontSize/expr/Literal/Value"
    ]
    # Integers and floats of the same value compare equal; different values do not.
    assert semantic_diff(
        _pbix(tmp_path, "d.pbix", parts(16, "16D")), _pbix(tmp_path, "e.pbix", parts(16.0, "16D"))
    ).empty
    assert not semantic_diff(
        _pbix(tmp_path, "f.pbix", parts(16, "16D")), _pbix(tmp_path, "g.pbix", parts(17, "16D"))
    ).empty


def test_a_schema_version_bump_is_not_a_difference(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _basic())
    bumped = _edit(
        _basic(), V1, lambda d: d.update({"$schema": schema("visualContainer", "2.13.0")})
    )
    assert semantic_diff(a, _pbix(tmp_path, "b.pbix", bumped)).empty
    renamed = _edit(_basic(), V1, lambda d: d.update({"$schema": schema("page", "2.12.0")}))
    result = semantic_diff(a, _pbix(tmp_path, "c.pbix", renamed))
    assert [c.path for c in result.changed] == ["/$schema"]


def test_security_bindings_and_content_type_order_are_not_differences(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _basic(), security=True)
    b = _pbix(tmp_path, "b.pbix", _basic(), security=False)
    reordered = content_types(defaults=("dax", "png", "json"), overrides=("DataModel", "Version"))
    c = _pbix(tmp_path, "c.pbix", _basic(), security=False, types=reordered)
    assert semantic_diff(a, b).empty
    assert semantic_diff(b, a).empty
    assert semantic_diff(a, c).empty


def test_different_security_bindings_bytes_are_not_a_difference(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _basic())
    package = PbixPackage(a)
    out = tmp_path / "out.pbix"
    package.write(out)  # drops SecurityBindings
    assert semantic_diff(a, out).empty


def test_a_json_part_in_a_package_object_is_compared_without_writing_it(tmp_path: Path) -> None:
    source = _pbix(tmp_path, "a.pbix", _basic())
    package = PbixPackage(source)
    package.write_json(V1, {"name": "v1"})
    result = semantic_diff(source, package)
    assert not result.empty
    assert semantic_diff(package, source).changed[0].before == result.changed[0].after


# --- semantic_diff: what IS a difference -------------------------------------------------------


def test_changes_carry_json_pointers_and_before_after_values(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _basic())
    edited = _edit(
        _basic(),
        V1,
        lambda d: (
            d["position"].update(x=99),
            d["visual"].update(visualType="tableEx"),
            d["visual"].pop("query"),
            d.update(isHidden=True),
        ),
    )
    result = semantic_diff(a, _pbix(tmp_path, "b.pbix", edited))
    assert not result.empty
    assert result.added == () and result.removed == () and result.binary_changed == ()
    by_path = {c.path: c for c in result.changed}
    assert set(by_path) == {"/position/x", "/visual/visualType", "/visual/query", "/isHidden"}
    assert all(c.part == V1 for c in result.changed)
    assert (by_path["/position/x"].before, by_path["/position/x"].after) == (10.5, 99)
    assert by_path["/visual/visualType"].after == "tableEx"
    assert by_path["/visual/query"].after is MISSING
    assert by_path["/visual/query"].before["queryState"]["Values"]
    assert by_path["/isHidden"].before is MISSING and by_path["/isHidden"].after is True
    assert repr(MISSING) == "<missing>" and not MISSING


def test_list_order_matters_and_length_changes_are_reported(tmp_path: Path) -> None:
    def parts(order: list[str]) -> dict[str, bytes]:
        return _edit(_basic(), V1, lambda d: d.update(tags=order))

    a = _pbix(tmp_path, "a.pbix", parts(["a", "b"]))
    swapped = semantic_diff(a, _pbix(tmp_path, "b.pbix", parts(["b", "a"])))
    assert [(c.path, c.before, c.after) for c in swapped.changed] == [
        ("/tags/0", "a", "b"),
        ("/tags/1", "b", "a"),
    ]
    longer = semantic_diff(a, _pbix(tmp_path, "c.pbix", parts(["a", "b", "c"])))
    assert [(c.path, c.before, c.after) for c in longer.changed] == [("/tags/2", MISSING, "c")]
    shorter = semantic_diff(_pbix(tmp_path, "d.pbix", parts(["a", "b", "c"])), a)
    assert [(c.path, c.before, c.after) for c in shorter.changed] == [("/tags/2", "c", MISSING)]


def test_pointer_segments_escape_slash_and_tilde(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _edit(_basic(), V1, lambda d: d.update({"a/b": 1, "c~d": 1})))
    b = _pbix(tmp_path, "b.pbix", _edit(_basic(), V1, lambda d: d.update({"a/b": 2, "c~d": 2})))
    assert sorted(c.path for c in semantic_diff(a, b).changed) == ["/a~1b", "/c~0d"]


def test_type_changes_and_null_are_differences(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _edit(_basic(), V1, lambda d: d.update(x=1, y=True, z=None)))
    b = _pbix(tmp_path, "b.pbix", _edit(_basic(), V1, lambda d: d.update(x=True, y=1, z={})))
    assert sorted(c.path for c in semantic_diff(a, b).changed) == ["/x", "/y", "/z"]


def test_added_and_removed_parts(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _basic())
    extra = {**_basic(), V2: dump(visual("v2", "Profit")), IMAGE: b"PNG"}
    b = _pbix(tmp_path, "b.pbix", extra)
    forward = semantic_diff(a, b)
    assert forward.added == (IMAGE, V2)
    assert forward.removed == () and forward.changed == () and forward.binary_changed == ()
    backward = semantic_diff(b, a)
    assert backward.removed == (IMAGE, V2) and backward.added == ()


def test_binary_parts_compare_by_crc_and_size_without_reading_the_data_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    same_size_a, same_size_b = b"A" * 500, b"B" * 500
    a = _pbix(tmp_path, "a.pbix", _basic(Settings=b"one"), data_model=same_size_a)
    b = _pbix(tmp_path, "b.pbix", _basic(Settings=b"two"), data_model=same_size_b)
    c = _pbix(tmp_path, "c.pbix", _basic(Settings=b"one"), data_model=same_size_a)
    touched: list[str] = []
    real_open = zipfile.ZipFile.open

    def spy(self: zipfile.ZipFile, name: Any, *args: Any, **kwargs: Any) -> Any:
        touched.append(getattr(name, "filename", name))
        return real_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    assert semantic_diff(a, c).empty
    result = semantic_diff(a, b)
    assert result.binary_changed == ("Settings", "DataModel")  # in archive order
    assert result.changed == ()
    assert not result.empty  # Settings differs
    assert "DataModel" not in touched


def test_a_rewritten_data_model_alone_is_not_a_difference_in_the_report(tmp_path: Path) -> None:
    """Desktop rewrites the DataModel bytes on every save, so a save that changed nothing in
    the report must still be recognised as the same report (adoption, no-op detection)."""
    a = _pbix(tmp_path, "a.pbix", _basic(), data_model=b"A" * 500)
    b = _pbix(tmp_path, "b.pbix", _basic(), data_model=b"B" * 700)
    result = semantic_diff(a, b)
    assert result.empty
    assert result.binary_changed == ("DataModel",)
    assert result.model_rewritten
    assert result.model_changes == ()
    assert result.summary() == []


def test_compare_model_reports_measure_format_and_relationship_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_powerbi_analyst.studio import pbix_diff

    a = _pbix(tmp_path, "a.pbix", _basic(), data_model=b"A" * 500)
    b = _pbix(tmp_path, "b.pbix", _basic(), data_model=b"B" * 700)
    models = {
        "a.pbix": {
            "measures": [
                {"TableName": "Sales", "Name": "Total", "Expression": "SUM(x)"},
                {"TableName": "Sales", "Name": "Old", "Expression": "1"},
            ],
            "formats": [{"TableName": "Sales", "Name": "Total", "FormatString": "0"}],
            "relationships": [{"From": "a", "To": "b"}],
        },
        "b.pbix": {
            "measures": [
                {"TableName": "Sales", "Name": "Total", "Expression": "SUM(y)"},
                {"TableName": "Sales", "Name": "New", "Expression": "2"},
            ],
            "formats": [{"TableName": "Sales", "Name": "Total", "FormatString": "0.0"}],
            "relationships": [{"From": "a", "To": "c"}],
        },
    }
    monkeypatch.setattr(
        pbix_diff, "_model_snapshot", lambda package: models[Path(package.source).name]
    )
    result = semantic_diff(a, b, compare_model=True)
    assert not result.empty
    assert result.model_changes == (
        "Format of measure 'Sales[Total]' changed",
        "Measure 'Sales[New]' was added",
        "Measure 'Sales[Old]' was removed",
        "Measure 'Sales[Total]' changed",
        "The relationships changed",
    )
    assert result.summary() == list(result.model_changes)
    # The same models (whatever the bytes) are the same report.
    models["b.pbix"] = models["a.pbix"]
    assert semantic_diff(a, b, compare_model=True).empty


def test_compare_model_treats_an_unreadable_model_as_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_powerbi_analyst.studio import pbix_diff

    a = _pbix(tmp_path, "a.pbix", _basic(), data_model=b"A" * 500)
    b = _pbix(tmp_path, "b.pbix", _basic(), data_model=b"B" * 700)
    monkeypatch.setattr(pbix_diff, "_model_snapshot", lambda package: None)
    result = semantic_diff(a, b, compare_model=True)
    assert not result.empty and result.summary() == ["The data model changed"]
    # A real reader failure on a fake DataModel is the same answer, not an exception.
    monkeypatch.undo()
    assert semantic_diff(a, b, compare_model=True).summary() == ["The data model changed"]


def test_odd_report_part_shapes_never_block_the_comparison(tmp_path: Path) -> None:
    """A part with valid JSON of an unexpected shape is named by its file name, not a crash."""
    odd = {"name": "v1", "visual": {"visualType": "x", "query": None}, "position": {"x": 1}}
    third = {"queryState": {"Y": {"projections": ["a", None, {"nativeQueryRef": "Q"}]}}}
    for shape, title in ((None, "x v1"), ([], "x v1"), (third, "Q")):
        first = dict(odd, visual={"visualType": "x", "query": shape})
        second = dict(first, position={"x": 2})
        a = _pbix(tmp_path, "a.pbix", _basic(**{V1: dump(first)}))
        b = _pbix(tmp_path, "b.pbix", _basic(**{V1: dump(second)}))
        assert semantic_diff(b, a).summary() == [f"Visual '{title}': position or size changed"]
        assert survival(b, a) == [
            f"Visual '{title}': position or size was not kept (Studio wrote 2, Desktop saved 1)"
        ]
    literal_title = {
        "name": "v1",
        "visual": {
            "visualType": "x",
            "visualContainerObjects": {"title": [{"properties": {"text": {"Literal": "s"}}}]},
        },
        "position": {"x": 1},
    }
    a = _pbix(tmp_path, "c.pbix", _basic(**{V1: dump(literal_title)}))
    b = _pbix(tmp_path, "d.pbix", _basic(**{V1: dump(dict(literal_title, position={"x": 2}))}))
    assert semantic_diff(a, b).summary() == ["Visual 'x v1': position or size changed"]
    assert survival(b, a) == [
        "Visual 'x v1': position or size was not kept (Studio wrote 2, Desktop saved 1)"
    ]


def test_a_different_content_type_for_the_same_extension_is_reported(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _basic())
    other = content_types().replace(
        b'Extension="png" ContentType=""', b'Extension="png" ContentType="image/png"'
    )
    b = _pbix(tmp_path, "b.pbix", _basic(), types=other)
    assert semantic_diff(a, b).binary_changed == ("[Content_Types].xml",)


def test_a_new_extension_default_alone_is_not_a_difference(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _basic())
    with_webp = {**_basic(), "Report/StaticResources/RegisteredResources/x.webp": b"RIFF"}
    b = _pbix(
        tmp_path, "b.pbix", with_webp, types=content_types(defaults=("png", "json", "dax", "webp"))
    )
    result = semantic_diff(a, b)
    assert result.added == ("Report/StaticResources/RegisteredResources/x.webp",)
    assert result.binary_changed == ()


def test_unparseable_report_json_falls_back_to_a_byte_comparison(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _basic(**{DEFINITION + "broken.json": b"{oops"}))
    b = _pbix(tmp_path, "b.pbix", _basic(**{DEFINITION + "broken.json": b"{oops!"}))
    same = _pbix(tmp_path, "c.pbix", _basic(**{DEFINITION + "broken.json": b"{oops"}))
    assert semantic_diff(a, b).binary_changed == (DEFINITION + "broken.json",)
    assert semantic_diff(a, same).empty


# --- summary --------------------------------------------------------------------------------


def _diff_of(tmp_path: Path, before: dict[str, bytes], after: dict[str, bytes]) -> SemanticDiff:
    return semantic_diff(_pbix(tmp_path, "a.pbix", before), _pbix(tmp_path, "b.pbix", after))


def test_summary_names_the_visual_and_the_property(tmp_path: Path) -> None:
    def retitle(d: dict[str, Any]) -> None:
        d["visual"]["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"][
            "Value"
        ] = "'Revenue'"

    result = _diff_of(tmp_path, _basic(), _edit(_basic(), V1, retitle))
    assert result.summary() == ["Visual 'Sales by region': title changed"]


def test_summary_groups_properties_per_visual_and_reads_like_power_bi(tmp_path: Path) -> None:
    def dressed(d: dict[str, Any]) -> None:
        d["visual"]["objects"] = {
            "dataPoint": [{"properties": {"a": 1}}],
            "legend": [{"properties": {}}],
        }
        d["visual"]["visualContainerObjects"]["background"] = [{"properties": {"a": 1}}]

    def many(d: dict[str, Any]) -> None:
        d["position"]["x"] = 500
        d["position"]["width"] = 300
        d["visual"]["objects"]["dataPoint"][0]["properties"]["a"] = 2
        d["visual"]["objects"]["legend"][0]["properties"]["show"] = False
        d["visual"]["visualContainerObjects"]["background"][0]["properties"]["a"] = 2
        d["filterConfig"] = {"filters": []}
        d["isHidden"] = True

    before = _edit(_basic(), V1, dressed)
    result = _diff_of(tmp_path, before, _edit(before, V1, many))
    assert result.summary() == [
        "Visual 'Sales by region': position or size, background, "
        "data colors formatting, legend formatting, filters, visibility changed"
    ]
    # A whole new formatting block is named by what is inside it.
    assert _diff_of(tmp_path, _basic(), before).summary() == [
        "Visual 'Sales by region': background, data colors formatting, legend formatting changed"
    ]


def test_summary_of_page_and_report_changes(tmp_path: Path) -> None:
    edited = _edit(_basic(), PAGE1, lambda d: d.update(displayName="Home", width=1920))
    edited = _edit(edited, DEFINITION + "pages/pages.json", lambda d: d.update(activePageName="x"))
    edited = _edit(edited, REPORT, lambda d: d.update(themeCollection={"baseTheme": {"name": "N"}}))
    result = _diff_of(tmp_path, _basic(), edited)
    assert result.summary() == [
        "The report: theme changed",
        "The page list: active page changed",
        "Page 'Overview': name, size changed",
    ]


def test_summary_of_added_and_removed_parts(tmp_path: Path) -> None:
    two_pages = report_parts(
        [
            ("p1", "Overview", [visual("v1", "Sales by region", x=10.5)]),
            ("p2", "Details", [visual("v2", "Profit"), visual("v3", "Cost")]),
        ]
    )
    result = _diff_of(tmp_path, _basic(), two_pages)
    assert result.summary() == [
        "The page list: page order changed",
        "Page 'Details' added (with 2 visuals)",
    ]
    back = semantic_diff(tmp_path / "b.pbix", tmp_path / "a.pbix")
    assert back.summary() == [
        "The page list: page order changed",
        "Page 'Details' removed (with 2 visuals)",
    ]
    single = {**_basic(), V2: dump(visual("v2", "Profit"))}
    assert _diff_of(tmp_path, _basic(), single).summary() == ["Visual 'Profit' added"]


def test_summary_says_which_page_only_when_there_are_several(tmp_path: Path) -> None:
    before = report_parts([("p1", "One", [visual("v1", "A")]), ("p2", "Two", [visual("v2", "B")])])
    after = _edit(
        before,
        DEFINITION + "pages/p2/visuals/v2/visual.json",
        lambda d: d["position"].update(x=5),
    )
    assert _diff_of(tmp_path, before, after).summary() == [
        "Visual 'B' on page 'Two': position or size changed"
    ]


def test_summary_of_binary_parts(tmp_path: Path) -> None:
    a = _pbix(tmp_path, "a.pbix", _basic(Settings=b"1"), data_model=b"x" * 10)
    b = _pbix(tmp_path, "b.pbix", _basic(Settings=b"2"), data_model=b"y" * 10)
    assert semantic_diff(a, b).summary() == ["'Settings' changed"]


def test_summary_is_limited(tmp_path: Path) -> None:
    visuals = [visual(f"v{i}", f"Chart {i}", x=i) for i in range(30)]
    before = report_parts([("p1", "Overview", visuals)])
    after = {
        **before,
        **{
            f"{DEFINITION}pages/p1/visuals/v{i}/visual.json": dump(
                visual(f"v{i}", f"Chart {i}", x=i + 1000)
            )
            for i in range(30)
        },
    }
    result = _diff_of(tmp_path, before, after)
    lines = result.summary()
    assert len(lines) == 20
    assert lines[0] == "Visual 'Chart 0': position or size changed"
    assert lines[-1] == "...and 11 more changes"
    assert len(result.summary(limit=5)) == 5
    assert len(result.summary(limit=100)) == 30
    assert result.summary(limit=1) == ["...and 30 more changes"]


# --- survival --------------------------------------------------------------------------------


def _survival(
    tmp_path: Path, written: dict[str, bytes], saved: dict[str, bytes], **kwargs: Any
) -> list[str]:
    baseline = kwargs.pop("baseline", None)
    base_path = _pbix(tmp_path, "base.pbix", baseline) if baseline is not None else None
    return survival(
        _pbix(tmp_path, "written.pbix", written),
        _pbix(tmp_path, "saved.pbix", saved, security=True),
        base_path,
    )


def test_an_identical_save_keeps_everything(tmp_path: Path) -> None:
    assert _survival(tmp_path, _with_image(), _with_image()) == []


def test_desktop_respelling_floats_and_reordering_keys_is_not_a_loss(tmp_path: Path) -> None:
    written = visual("v1", "Sales by region", x=966.1417322834645)
    written["visual"]["objects"] = {
        "labels": [{"properties": {"fontSize": {"expr": {"Literal": {"Value": "13.5D"}}}}}]
    }
    saved = copy.deepcopy(written)
    saved["position"]["x"] = 966.14173228346453
    saved["visual"]["objects"]["labels"][0]["properties"]["fontSize"]["expr"]["Literal"][
        "Value"
    ] = "13.50000000000000D"
    saved = dict(reversed(list(saved.items())))
    result = _survival(
        tmp_path, _with_image([written]), {**_with_image([written]), V1: dump(saved)}
    )
    assert result == []


def test_properties_desktop_adds_are_not_reported(tmp_path: Path) -> None:
    written = _with_image()
    saved = _edit(written, V1, lambda d: (d.update(extra={"a": 1}), d["position"].update(w=1)))
    saved = _edit(saved, PAGE1, lambda d: d.update(visibility="AlwaysVisible"))
    saved[DEFINITION + "pages/p1/visuals/v9/visual.json"] = dump(visual("v9", "New"))
    assert _survival(tmp_path, written, saved) == []


def test_schema_bumps_security_bindings_and_base_themes_are_not_reported(tmp_path: Path) -> None:
    written = _with_image()
    saved = _edit(written, V1, lambda d: d.update({"$schema": schema("visualContainer", "2.20.0")}))
    saved = _edit(saved, REPORT, lambda d: d.update({"$schema": schema("report", "3.9.0")}))
    saved["Report/StaticResources/SharedResources/BaseThemes/CY24SU02.json"] = dump(
        {"name": "newer"}
    )
    assert _survival(tmp_path, written, saved) == []


def test_d_and_l_number_literals_of_the_same_value_are_interchangeable(tmp_path: Path) -> None:
    def parts(literal: str) -> dict[str, bytes]:
        item = visual("v1", "Sales by region")
        item["visual"]["objects"] = {
            "labels": [{"properties": {"fontSize": {"expr": {"Literal": {"Value": literal}}}}}]
        }
        return _with_image([item])

    assert _survival(tmp_path, parts("6D"), parts("6L")) == []
    assert _survival(tmp_path, parts("6D"), parts("7D")) == [
        "Visual 'Sales by region': data labels formatting was not kept "
        "(Studio wrote 6D, Desktop saved 7D)"
    ]
    assert _survival(tmp_path, parts("6D"), parts("6M")) != []


def test_a_changed_value_is_reported_with_both_values(tmp_path: Path) -> None:
    written = _with_image()

    def retitle(d: dict[str, Any]) -> None:
        d["visual"]["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"][
            "Value"
        ] = "'Something else'"

    assert _survival(tmp_path, written, _edit(written, V1, retitle)) == [
        "Visual 'Sales by region': title was not kept "
        "(Studio wrote 'Sales by region', Desktop saved 'Something else')"
    ]


def test_a_dropped_property_is_reported_once_per_property(tmp_path: Path) -> None:
    def with_objects(d: dict[str, Any]) -> None:
        d["visual"]["objects"] = {
            "dataPoint": [{"properties": {"fill": 1}}, {"properties": {"fill": 2}}],
            "legend": [{"properties": {"show": False}}],
        }

    written = _edit(_with_image(), V1, with_objects)
    saved = _edit(written, V1, lambda d: d["visual"]["objects"].pop("dataPoint"))
    assert _survival(tmp_path, written, saved) == [
        "Visual 'Sales by region': data colors formatting was not kept"
    ]


def test_empty_containers_desktop_omits_are_not_reported(tmp_path: Path) -> None:
    written = _edit(_with_image(), V1, lambda d: d["visual"].update(objects={}, extra=[]))
    assert _survival(tmp_path, written, _with_image()) == []


def test_a_visual_missing_from_the_saved_file_is_reported(tmp_path: Path) -> None:
    written = _with_image([visual("v1", "Sales by region"), visual("v2", "Profit")])
    saved = _with_image([visual("v1", "Sales by region")])
    assert _survival(tmp_path, written, saved) == [
        "Visual 'Profit' was removed by Power BI Desktop"
    ]


def test_a_dropped_registered_resource_is_reported_once(tmp_path: Path) -> None:
    written = _with_image()
    saved = dict(written)
    del saved[IMAGE]
    saved[REPORT] = dump(
        {**_report_with_image(), "resourcePackages": [_report_with_image()["resourcePackages"][0]]}
    )
    assert _survival(tmp_path, written, saved) == [
        "Power BI Desktop dropped the image 'logo.png' the edit registered"
    ]


def test_a_dropped_resource_file_with_its_item_kept_is_reported(tmp_path: Path) -> None:
    written = _with_image()
    saved = dict(written)
    del saved[IMAGE]
    assert _survival(tmp_path, written, saved) == ["Power BI Desktop dropped the file 'logo.png'"]


def test_a_registered_theme_item_dropped_by_desktop_is_reported(tmp_path: Path) -> None:
    report = _report_with_image()
    report["resourcePackages"][1]["items"].append(
        {"name": "Mine.json", "path": "Mine.json", "type": "CustomTheme"}
    )
    theme_part = "Report/StaticResources/RegisteredResources/Mine.json"
    written = {**_with_image(), REPORT: dump(report), theme_part: dump({"name": "Mine"})}
    saved = {k: v for k, v in _with_image().items()}
    assert _survival(tmp_path, written, saved) == [
        "Power BI Desktop dropped the theme 'Mine.json' the edit registered"
    ]


def test_scalar_list_order_is_enforced_but_object_lists_may_be_reordered(tmp_path: Path) -> None:
    written = _with_image()
    pages = DEFINITION + "pages/pages.json"
    written = _edit(written, pages, lambda d: d.update(pageOrder=["p1", "p2", "p3"]))
    swapped = _edit(written, pages, lambda d: d.update(pageOrder=["p1", "p3", "p2"]))
    assert _survival(tmp_path, written, swapped) == [
        "The page list: the order of page order was not kept"
    ]
    assert (
        _survival(
            tmp_path,
            written,
            _edit(written, pages, lambda d: d.update(pageOrder=["p1", "p0", "p2", "p3"])),
        )
        == []
    )
    missing = _edit(written, pages, lambda d: d.update(pageOrder=["p1", "p2"]))
    assert _survival(tmp_path, written, missing) == ["The page list: page order was not kept"]

    filters = [{"name": "F1", "type": "A"}, {"name": "F2", "type": "B"}]
    with_filters = _edit(_with_image(), V1, lambda d: d.update(filterConfig={"filters": filters}))
    reordered = _edit(
        with_filters, V1, lambda d: d.update(filterConfig={"filters": list(reversed(filters))})
    )
    assert _survival(tmp_path, with_filters, reordered) == []
    dropped = _edit(with_filters, V1, lambda d: d.update(filterConfig={"filters": filters[:1]}))
    assert _survival(tmp_path, with_filters, dropped) == [
        "Visual 'Sales by region': filters were not kept"
    ]


def test_a_resource_desktop_kept_under_its_name_but_repathed_is_reported(tmp_path: Path) -> None:
    report = _report_with_image()
    report["resourcePackages"][1]["items"].append(
        {"name": "Mine.json", "path": "Mine.json", "type": "CustomTheme"}
    )
    theme_part = "Report/StaticResources/RegisteredResources/Mine.json"
    written = {**_with_image(), REPORT: dump(report), theme_part: dump({"name": "Mine"})}
    moved = _report_with_image()
    moved["resourcePackages"][1]["items"].append(
        {"name": "Mine.json", "path": "Other/Mine.json", "type": "CustomTheme"}
    )
    retyped = _report_with_image()
    retyped["resourcePackages"][1]["items"].append(
        {"name": "Mine.json", "path": "Mine.json", "type": "Image"}
    )
    assert _survival(tmp_path, written, {**written, REPORT: dump(moved)}) == [
        "Power BI Desktop changed the theme 'Mine.json' the edit registered "
        "(it is now a theme at 'Mine.json')"
    ]
    assert _survival(tmp_path, written, {**written, REPORT: dump(retyped)}) == [
        "Power BI Desktop changed the theme 'Mine.json' the edit registered "
        "(it is now an image at 'Mine.json')"
    ]
    # An item the edit did not touch is not the edit's to answer for.
    assert _survival(tmp_path, written, {**written, REPORT: dump(moved)}, baseline=written) == []


def test_a_dropped_formatting_block_is_named_by_what_it_held(tmp_path: Path) -> None:
    written = _with_image()
    saved = _edit(written, V1, lambda d: d["visual"].pop("visualContainerObjects"))
    assert _survival(tmp_path, written, saved) == ["Visual 'Sales by region': title was not kept"]


def test_a_formatting_block_added_to_a_visual_is_named_by_what_it_holds(tmp_path: Path) -> None:
    def title_block() -> dict[str, Any]:
        return {"title": [{"properties": {"text": {"expr": {"Literal": {"Value": "'Sales'"}}}}}]}

    bare = _with_image()

    def strip(d: dict[str, Any]) -> None:
        d["visual"].pop("visualContainerObjects", None)

    before = _edit(bare, V1, strip)

    def dress(d: dict[str, Any]) -> None:
        d["visual"]["visualContainerObjects"] = title_block() | {
            "background": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}}}]
        }

    after = _edit(before, V1, dress)
    forward = _diff_of(tmp_path, before, after)
    assert forward.summary() == ["Visual 'Total': title, background changed"]
    assert _diff_of(tmp_path, after, before).summary() == [
        "Visual 'Sales': title, background changed"
    ]


def test_a_baseline_blames_only_the_edit(tmp_path: Path) -> None:
    # The baseline had an unused image that Desktop garbage-collects on every save.
    baseline = _with_image()
    written = _edit(baseline, V1, lambda d: d["position"].update(x=99))
    saved = dict(written)
    del saved[IMAGE]
    saved[REPORT] = dump(
        {**_report_with_image(), "resourcePackages": [_report_with_image()["resourcePackages"][0]]}
    )
    assert _survival(tmp_path, written, saved) != []
    assert _survival(tmp_path, written, saved, baseline=baseline) == []
    # ...but the edit itself still has to survive.
    lost = _edit(saved, V1, lambda d: d["position"].update(x=1))
    assert _survival(tmp_path, written, lost, baseline=baseline) == [
        "Visual 'Sales by region': position or size was not kept (Studio wrote 99, Desktop saved 1)"
    ]


def test_a_baseline_exempts_untouched_values_desktop_rewrote(tmp_path: Path) -> None:
    baseline = _with_image()
    written = _edit(baseline, V1, lambda d: d.update(isHidden=True))
    saved = _edit(written, PAGE1, lambda d: d.update(displayName="Desktop renamed it"))
    assert _survival(tmp_path, written, saved) != []
    assert _survival(tmp_path, written, saved, baseline=baseline) == []


def test_survival_accepts_package_objects(tmp_path: Path) -> None:
    source = _pbix(tmp_path, "in.pbix", _with_image())
    package = PbixPackage(source)
    package.write_json(V1, visual("v1", "Sales by region", x=77))
    saved = _pbix(tmp_path, "saved.pbix", _with_image())
    assert survival(package, saved) == [
        "Visual 'Sales by region': position or size was not kept (Studio wrote 77, Desktop saved 0)"
    ]


def test_change_is_a_plain_value_object() -> None:
    change = Change("Report/definition/report.json", "/a", 1, 2)
    assert change == Change("Report/definition/report.json", "/a", 1, 2)
    with pytest.raises(AttributeError):
        change.part = "x"  # type: ignore[misc]
