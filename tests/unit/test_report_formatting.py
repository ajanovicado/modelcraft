import base64

import pytest

from ai_powerbi_analyst.studio.live_report import Compiler, Unsupported, projections
from ai_powerbi_analyst.studio.report_formatting import color, properties, safe_svg, visual_format
from ai_powerbi_analyst.studio.report_import import ImportedReport, ImportedVisual


def lit(v):
    return {"expr": {"Literal": {"Value": v}}}


def measure(name):
    return {
        "expr": {"Measure": {"Expression": {"SourceRef": {"Entity": "Facts"}}, "Property": name}}
    }


def entry(props, metadata=None, identity=None):
    return {
        "properties": props,
        "selector": {
            **({"metadata": metadata} if metadata else {}),
            **({"id": identity} if identity else {}),
        },
    }


def test_card_formatting_uses_saved_alias_and_validates_auxiliary_measures():
    report = ImportedReport(
        id="r",
        workspace_id="w",
        name="R",
        source_path="",
        archived_path="",
        source_hash="",
        base_hash="",
        format="PBIR",
        pages=[],
        measures=[{"TableName": "Facts", "Name": n} for n in ("Amount", "Goal", "Status", "Gauge")],
    )
    alias = "Facts.OldName"
    visual = ImportedVisual(
        id="v",
        title="Amount",
        visual_type="cardVisual",
        definition={
            "visual": {
                "query": {
                    "queryState": {
                        "Data": {
                            "projections": [{"field": measure("Amount")["expr"], "queryRef": alias}]
                        }
                    }
                },
                "objects": {
                    "label": [entry({"text": lit("'Sales'")}, alias)],
                    "image": [entry({"show": lit("true"), "imageUrl": measure("Gauge")}, alias)],
                    "referenceLabel": [entry({"value": measure("Goal")}, alias, "goal")],
                    "referenceLabelTitle": [entry({"titleText": lit("'Target: '")}, alias, "goal")],
                    "referenceLabelDetail": [
                        entry({"detailValue": measure("Status")}, alias, "goal")
                    ],
                },
            }
        },
    )
    compiler = Compiler(report)
    fields = projections(compiler, visual)
    fmt = visual_format(compiler, visual, fields, {("Facts", "Amount"): "$#,0.00"}, [])
    assert fmt["label"] == "Sales" and fmt["imageIndex"] == 1
    assert fmt["references"] == [{"index": 2, "label": "Target: ", "detailIndex": 3}]
    assert fields[0]["format"] == "$#,0.00" and all(f["hidden"] for f in fields[1:])
    visual.definition["visual"]["objects"]["image"][0]["properties"]["imageUrl"] = measure(
        "Unknown"
    )
    with pytest.raises(Unsupported):
        visual_format(compiler, visual, projections(compiler, visual), {}, [])


def test_specific_properties_do_not_leak_between_fields():
    objects = {"value": [entry({"size": 10}), entry({"size": 20}, "A"), entry({"size": 30}, "B")]}
    assert properties(objects, "value", "A")["size"] == 20
    assert properties(objects, "value", "C")["size"] == 10
    assert (
        color(
            {"solid": {"color": {"expr": {"ThemeDataColor": {"ColorId": 0, "Percent": -0.2}}}}},
            ["#ffffff"],
        )
        == "#cccccc"
    )


def test_inert_svg_gradient_retains_measure_graphic():
    data = (
        "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' "
        "width='300' height='30'><!-- note --><linearGradient id='g'>"
        "<stop offset='0%' style='stop-color:#123456;stop-opacity:1'/></linearGradient>"
        "<rect width='200' height='10' fill='url(#g)'/></svg>"
    )
    result = safe_svg(data)
    assert result and b'stop-color="#123456"' in base64.b64decode(result.split(",")[1])


@pytest.mark.parametrize(
    "xml",
    [
        "<svg><script>alert(1)</script></svg>",
        "<svg onload='alert(1)'/>",
        "<svg><image href='https://example.com/a'/></svg>",
        "<!DOCTYPE svg [<!ENTITY x SYSTEM 'file:///secret'>]><svg>&x;</svg>",
        "<svg><rect fill='url(https://example.com/a)'/></svg>",
        "<svg><rect style='background:url(https://example.com/a)'/></svg>",
    ],
)
def test_svg_rejects_active_or_external_content(xml):
    assert safe_svg("data:image/svg+xml;utf8," + xml) is None
