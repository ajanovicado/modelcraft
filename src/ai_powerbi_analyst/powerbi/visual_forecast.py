"""Encode Desktop's native Forecast transform without computing local forecast values."""

from collections.abc import Mapping

from ai_powerbi_analyst.contracts import TableProfile, VisualPlan


def visual_forecast_objects(
    visual: VisualPlan,
    query_state: dict[str, object],
    tables: Mapping[str, TableProfile],
) -> dict[str, object] | None:
    """Compile one reviewed date trend to the Forecast object Desktop's Analytics pane saves.

    ForecastLength, IgnoreLast and optional MaxSeasonality are numbers of points (Unit 7).
    This configures Desktop's calculation; it adds no forecast rows to Studio's preview.
    """
    settings = visual.forecast
    if settings is None:
        return None
    if visual.visual_type != "lineChart":
        raise ValueError("A native forecast requires a lineChart")
    categories = [binding for binding in visual.bindings if binding.role == "Category"]
    values = [binding for binding in visual.bindings if binding.role == "Y"]
    if len(values) != 1 or values[0].field_kind != "measure":
        raise ValueError("A native forecast requires exactly one measure")
    if any(binding.role == "Series" for binding in visual.bindings):
        raise ValueError("A native forecast cannot have a legend")
    if len(categories) != 1 or categories[0].field_kind != "column":
        raise ValueError("A native forecast requires one reviewed date column")
    category = categories[0]
    table = tables.get(category.table_id)
    column = next((c for c in table.columns if c.id == category.field_id), None) if table else None
    if column is None or column.data_type.value not in {"date", "datetime"}:
        raise ValueError("A native forecast requires one reviewed date column")
    role = query_state.get("Y")
    projections = role.get("projections") if isinstance(role, dict) else None
    if not isinstance(projections, list) or len(projections) != 1:
        raise ValueError("A native forecast requires one compiled measure projection")
    projection = projections[0]
    query_ref = projection.get("queryRef") if isinstance(projection, dict) else None
    if not isinstance(query_ref, str) or not query_ref:
        raise ValueError("A native forecast requires the compiled measure queryRef")

    # Desktop 2.157's AnalyticsAuthoringService saves parameters in this order.
    parameters = [
        {"Literal": {"Value": "7L"}, "Name": "Unit"},
        {"Literal": {"Value": f"{settings.forecast_length}L"}, "Name": "ForecastLength"},
        {"Literal": {"Value": f"{settings.ignore_last}L"}, "Name": "IgnoreLast"},
        {"Literal": {"Value": f"{settings.confidence / 100:g}D"}, "Name": "ConfidenceLevel"},
    ]
    if settings.seasonality is not None:
        parameters.append(
            {"Literal": {"Value": f"{settings.seasonality}L"}, "Name": "MaxSeasonality"}
        )
    return {
        "forecast": [
            {
                "properties": {
                    "transform": {"algorithm": "Forecast", "parameters": parameters},
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "displayName": {"expr": {"Literal": {"Value": "'Forecast 1'"}}},
                },
                "selector": {"metadata": query_ref, "id": "1"},
            }
        ],
        # Scalar is the native categoryAxis enum for Desktop's Continuous option.
        "categoryAxis": [
            {"properties": {"axisType": {"expr": {"Literal": {"Value": "'Scalar'"}}}}}
        ],
    }
