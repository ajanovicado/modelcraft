from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from ai_powerbi_analyst.contracts import ProposedDaxMeasure, RelationshipCandidate
from ai_powerbi_analyst.data.onboarding import DataOnboardingService, UploadedSource
from ai_powerbi_analyst.powerbi import semantic_model as semantic_model_module
from ai_powerbi_analyst.powerbi.semantic_model import (
    SemanticProjectBuilder,
    SemanticProjectError,
)
from ai_powerbi_analyst.preflight import ToolProbe
from ai_powerbi_analyst.workspace.planner import DashboardPlanningService

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def unavailable_external_validators(monkeypatch: pytest.MonkeyPatch) -> None:
    """These local builder tests must not discover host tools or download schemas."""
    monkeypatch.setattr(
        semantic_model_module, "probe_pbi_cli", lambda: ToolProbe(name="pbi", available=False)
    )
    monkeypatch.setattr(
        semantic_model_module, "probe_tool", lambda name: ToolProbe(name=name, available=False)
    )


@pytest.mark.parametrize("direction", ["both", "single"])
def test_fresh_one_to_one_export_preserves_cardinality_and_rejects_single(tmp_path, direction):
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(name="devices.csv", content=b"serial_id,cost\n31,8\n47,12\n82,15\n"),
            UploadedSource(name="warranties.csv", content=b"serial_id,years\n31,1\n47,2\n82,3\n"),
        ]
    )
    relationship = RelationshipCandidate(
        id="device_warranty",
        from_table_id=next(table.id for table in manifest.tables if table.name == "devices"),
        from_column_id="serial_id",
        to_table_id=next(table.id for table in manifest.tables if table.name == "warranties"),
        to_column_id="serial_id",
        cardinality="one_to_one",
        cross_filter_direction=direction,
        confidence=1,
        evidence=["Fresh unique keys"],
        review_status="approved",
    )
    plan = DashboardPlanningService(None).plan("Build a device dashboard", manifest, [relationship])
    plan = plan.model_copy(
        update={
            "approved": True,
            "semantic_model": plan.semantic_model.model_copy(update={"approved": True}),
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures/powerbi/client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )
    if direction == "single":
        with pytest.raises(SemanticProjectError, match="require Both"):
            builder.build("Device Model", manifest, plan)
        assert not (tmp_path / "projects/device_model").exists()
    else:
        pbip, _ = builder.build("Device Model", manifest, plan)
        text = next(pbip.parent.glob("*.SemanticModel/definition/relationships.tmdl")).read_text()
        assert "fromCardinality: one" in text and "toCardinality: one" in text
        assert "crossFilteringBehavior: bothDirections" in text


def test_long_desktop_export_paths_are_rejected_before_copy(tmp_path):
    from ai_powerbi_analyst.powerbi.semantic_model import validate_desktop_paths

    project = tmp_path / "project"
    project.mkdir()
    (project / "visual.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SemanticProjectError, match="too long"):
        validate_desktop_paths(project, destination_root=tmp_path / ("x" * 200))


def test_failed_receipt_after_promotion_restores_exact_previous_project(tmp_path, monkeypatch):
    from ai_powerbi_analyst.powerbi.transaction import hash_tree

    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Region,Sales\nEast,100\nWest,200\n")]
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures/powerbi/client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )
    plan = _approved_plan(manifest)
    pbip, _ = builder.build("QA", manifest, plan)
    before = hash_tree(pbip.parent)
    metadata = (builder.evidence_root / "qa-validation-receipt.json").read_bytes()
    original_write = Path.write_text

    def fail_receipt(path, *args, **kwargs):
        if path.parent == builder.evidence_root:
            raise OSError("injected full disk during receipt")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_receipt)
    with pytest.raises(OSError, match="injected"):
        builder.build("QA", manifest, plan)
    assert hash_tree(pbip.parent) == before
    assert (builder.evidence_root / "qa-validation-receipt.json").read_bytes() == metadata
    assert not (builder.undo_root / "qa.json").exists()


def test_workspace_project_keys_do_not_collide_for_the_same_display_name(tmp_path):
    from ai_powerbi_analyst.powerbi.transaction import hash_tree

    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Region,Sales\nEast,100\nWest,200\n")]
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures/powerbi/client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )
    plan = _approved_plan(manifest)
    first, _ = builder.build("Data", manifest, plan, project_key="data_workspace_one")
    before = hash_tree(first.parent)
    second, _ = builder.build("Data", manifest, plan, project_key="data_workspace_two")
    assert first.parent != second.parent
    assert hash_tree(first.parent) == before
    assert (
        json.loads(next(second.parent.glob("*.Report/.platform")).read_text())["metadata"][
            "displayName"
        ]
        == "Data"
    )


def _approved_plan(manifest):
    plan = DashboardPlanningService(None).plan("Build a sales dashboard", manifest, [])
    return plan.model_copy(
        update={
            "approved": True,
            "semantic_model": plan.semantic_model.model_copy(update={"approved": True}),
        }
    )


def test_builder_creates_valid_pbip_with_tmdl_and_pbir(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="sales.csv",
                content=b"Date,Region,Sales\n2026-01-01,East,100\n2026-02-01,West,150\n",
            )
        ]
    )
    plan = _approved_plan(manifest)
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    pbip, receipt = builder.build("Sales Dashboard", manifest, plan)

    assert receipt.passed
    assert set(receipt.validators) == {
        "local-pbip-structure",
        "source-content-hash",
        "semantic-bindings",
        "local-tmdl-model-index",
    }
    assert pbip.is_file()
    project = pbip.parent
    assert next(project.glob("*.SemanticModel/definition/tables/*.tmdl")).is_file()
    assert len(list(project.glob("*.Report/definition/pages/*/visuals/*/visual.json"))) == 4
    model = next(project.glob("*.SemanticModel/definition/model.tmdl")).read_text(encoding="utf-8")
    assert "\nref table sales\n" in model
    assert "\n\tref table" not in model
    report_platform = json.loads(
        next(project.glob("*.Report/.platform")).read_text(encoding="utf-8")
    )
    model_platform = json.loads(
        next(project.glob("*.SemanticModel/.platform")).read_text(encoding="utf-8")
    )
    assert report_platform["metadata"]["displayName"] == "Sales Dashboard"
    assert model_platform["metadata"]["displayName"] == "Sales Dashboard"
    assert report_platform["config"]["logicalId"] == str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "ai-powerbi-analyst:sales_dashboard:report",
        )
    )
    assert model_platform["config"]["logicalId"] == str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "ai-powerbi-analyst:sales_dashboard:semanticmodel",
        )
    )


def test_numeric_identifiers_do_not_summarize(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="sales.csv",
                content=(
                    b"Row ID,Postal Code,StoreKey,Paid,Sales\n1,42420,7,1,100\n2,90036,8,0,150\n"
                ),
            )
        ]
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    pbip, receipt = builder.build("Identifiers", manifest, _approved_plan(manifest))

    assert receipt.passed
    table_tmdl = next(pbip.parent.glob("*.SemanticModel/definition/tables/sales.tmdl")).read_text(
        encoding="utf-8"
    )
    for name in ("'Row ID'", "'Postal Code'", "StoreKey"):
        assert f"\tcolumn {name}\n\t\tdataType: int64\n\t\tsummarizeBy: none" in table_tmdl
    for name in ("Paid", "Sales"):
        assert f"\tcolumn {name}\n\t\tdataType: int64\n\t\tsummarizeBy: none" not in table_tmdl


def test_builder_applies_reviewed_table_name_visibility_and_summarization(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="sales.csv",
                content=(b"Date,Region,Sales\n2026-01-01,East,100\n2026-02-01,West,150\n"),
            )
        ]
    )
    plan = _approved_plan(manifest)
    source_table = manifest.tables[0]
    reviewed_table = plan.semantic_model.tables[0].model_copy(
        update={
            "display_name": "Sales Facts",
            "hidden_column_ids": ["region"],
        }
    )
    advanced = ProposedDaxMeasure(
        id="average_reviewed_sales",
        name="Average Reviewed Sales",
        table_id=source_table.id,
        expression="AVERAGE('sales'[Sales])",
        approved=True,
    )
    row_count = ProposedDaxMeasure(
        id="reviewed_sales_rows",
        name="Reviewed Sales Rows",
        table_id=source_table.id,
        expression="COUNTROWS('sales')",
        approved=True,
    )
    plan = plan.model_copy(
        update={
            "semantic_model": plan.semantic_model.model_copy(
                update={
                    "tables": [reviewed_table],
                    "measures": [*plan.semantic_model.measures, advanced, row_count],
                }
            )
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    pbip, receipt = builder.build("Reviewed semantic names", manifest, plan)

    assert receipt.passed
    project = pbip.parent
    table_tmdl = next(project.glob("*.SemanticModel/definition/tables/sales.tmdl")).read_text(
        encoding="utf-8"
    )
    assert "table 'Sales Facts'" in table_tmdl
    assert "partition 'Sales Facts' = m" in table_tmdl
    assert "SUM('Sales Facts'[Sales])" in table_tmdl
    assert "AVERAGE('Sales Facts'[Sales])" in table_tmdl
    assert "COUNTROWS('Sales Facts')" in table_tmdl
    assert (
        "\tcolumn Region\n"
        "\t\tdataType: string\n"
        "\t\tisHidden\n"
        "\t\tsummarizeBy: none\n"
        "\t\tsourceColumn: Region"
    ) in table_tmdl
    assert (
        "\tcolumn Date\n\t\tdataType: dateTime\n\t\tsummarizeBy: none\n\t\tsourceColumn: Date"
    ) in table_tmdl
    assert ("\tcolumn Sales\n\t\tdataType: int64\n\t\tsummarizeBy: none") not in table_tmdl
    model_tmdl = next(project.glob("*.SemanticModel/definition/model.tmdl")).read_text(
        encoding="utf-8"
    )
    assert "\nref table 'Sales Facts'\n" in model_tmdl
    report_definition = "\n".join(
        path.read_text(encoding="utf-8")
        for path in project.glob("*.Report/definition/pages/*/visuals/*/visual.json")
    )
    assert '"Entity": "Sales Facts"' in report_definition
    assert '"queryRef": "Sales Facts.' in report_definition
    assert f'"Entity": "{source_table.name}"' not in report_definition


def test_builder_uses_distinct_calendar_name_after_reviewed_table_rename(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="sales.csv",
                content=b"Date,Sales\n2026-01-01,100\n2026-02-01,150\n",
            )
        ]
    )
    plan = _approved_plan(manifest)
    assert plan.semantic_model.create_calendar
    reviewed_table = plan.semantic_model.tables[0].model_copy(update={"display_name": "Calendar"})
    plan = plan.model_copy(
        update={
            "semantic_model": plan.semantic_model.model_copy(update={"tables": [reviewed_table]})
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    pbip, receipt = builder.build("Calendar display collision", manifest, plan)

    assert receipt.passed
    definition = next(pbip.parent.glob("*.SemanticModel/definition"))
    model_tmdl = (definition / "model.tmdl").read_text(encoding="utf-8")
    assert "\nref table Calendar\n" in model_tmdl
    assert "\nref table 'Calendar 2'\n" in model_tmdl
    relationship_tmdl = (definition / "relationships.tmdl").read_text(encoding="utf-8")
    assert "fromColumn: Calendar.Date" in relationship_tmdl
    assert "toColumn: 'Calendar 2'.Date" in relationship_tmdl


def test_builder_rejects_case_insensitive_duplicate_reviewed_table_names(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(name="alpha.csv", content=b"Value\n10\n"),
            UploadedSource(name="beta.csv", content=b"Value\n20\n"),
        ]
    )
    plan = _approved_plan(manifest)
    duplicate_names = [
        item.model_copy(update={"display_name": name})
        for item, name in zip(plan.semantic_model.tables, ["Facts", "facts"], strict=True)
    ]
    plan = plan.model_copy(
        update={
            "semantic_model": plan.semantic_model.model_copy(update={"tables": duplicate_names})
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    with pytest.raises(SemanticProjectError, match="display names must be unique"):
        builder.build("Duplicate table names", manifest, plan)


def test_builder_rejects_unknown_reviewed_hidden_column(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Sales\n100\n")]
    )
    plan = _approved_plan(manifest)
    invalid_table = plan.semantic_model.tables[0].model_copy(
        update={"hidden_column_ids": ["invented_column"]}
    )
    plan = plan.model_copy(
        update={
            "semantic_model": plan.semantic_model.model_copy(update={"tables": [invalid_table]})
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    with pytest.raises(SemanticProjectError, match="hides unknown column"):
        builder.build("Invalid hidden column", manifest, plan)


def test_builder_rejects_multiline_reviewed_table_name(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Sales\n100\n")]
    )
    plan = _approved_plan(manifest)
    invalid_table = plan.semantic_model.tables[0].model_copy(
        update={"display_name": "Sales\nInjected"}
    )
    plan = plan.model_copy(
        update={
            "semantic_model": plan.semantic_model.model_copy(update={"tables": [invalid_table]})
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    with pytest.raises(SemanticProjectError, match="control character"):
        builder.build("Invalid table name", manifest, plan)


def test_builder_remaps_advanced_dax_table_names_once_when_names_swap(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(name="alpha.csv", content=b"Value\n10\n"),
            UploadedSource(name="beta.csv", content=b"Value\n20\n"),
        ]
    )
    plan = _approved_plan(manifest)
    tables_by_name = {table.name: table for table in manifest.tables}
    advanced = ProposedDaxMeasure(
        id="combined_value",
        name="Combined Value",
        table_id=tables_by_name["alpha"].id,
        expression="SUM('alpha'[Value]) + SUM('beta'[Value])",
        approved=True,
    )
    renamed_tables = [
        item.model_copy(
            update={
                "display_name": ("beta" if item.table_id == tables_by_name["alpha"].id else "alpha")
            }
        )
        for item in plan.semantic_model.tables
    ]
    plan = plan.model_copy(
        update={
            "semantic_model": plan.semantic_model.model_copy(
                update={
                    "tables": renamed_tables,
                    "measures": [*plan.semantic_model.measures, advanced],
                }
            )
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    pbip, receipt = builder.build("Swapped table names", manifest, plan)

    assert receipt.passed
    alpha_tmdl = next(pbip.parent.glob("*.SemanticModel/definition/tables/alpha.tmdl")).read_text(
        encoding="utf-8"
    )
    assert "SUM(beta[Value]) + SUM(alpha[Value])" in alpha_tmdl


def test_builder_rejects_indented_tmdl_object_references(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Sales\n100\n")]
    )
    plan = _approved_plan(manifest)
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )
    pbip, _ = builder.build("Invalid reference indentation", manifest, plan)
    model_path = next(pbip.parent.glob("*.SemanticModel/definition/model.tmdl"))
    model_path.write_text(
        model_path.read_text(encoding="utf-8").replace("\nref table ", "\n\tref table "),
        encoding="utf-8",
    )

    receipt = builder.validate(pbip.parent, manifest, plan)

    assert not receipt.passed
    assert any("references must be root-level" in issue for issue in receipt.issues)


def test_builder_requires_explicit_plan_approval(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Sales\n100\n")]
    )
    plan = DashboardPlanningService(None).plan("Build a dashboard", manifest, [])
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    with pytest.raises(SemanticProjectError, match="Approve"):
        builder.build("Sales Dashboard", manifest, plan)


def test_builder_accepts_separately_approved_catalog_bound_dax(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Sales\n100\n150\n")]
    )
    plan = _approved_plan(manifest)
    advanced = ProposedDaxMeasure(
        id="approved_sales_average",
        name="Approved Sales Average",
        expression="AVERAGE('sales'[Sales])",
        approved=True,
    )
    plan = plan.model_copy(
        update={
            "semantic_model": plan.semantic_model.model_copy(
                update={"measures": [*plan.semantic_model.measures, advanced]}
            )
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    pbip, receipt = builder.build("Advanced DAX", manifest, plan)

    assert receipt.passed
    table = next(pbip.parent.glob("*.SemanticModel/definition/tables/*.tmdl"))
    assert "Approved Sales Average" in table.read_text(encoding="utf-8")


def test_builder_rejects_hallucinated_advanced_dax_function(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Sales\n100\n150\n")]
    )
    plan = _approved_plan(manifest)
    advanced = ProposedDaxMeasure(
        id="fake_sales_measure",
        name="Fake Sales Measure",
        expression="NOT_A_REAL_DAX_FUNCTION('sales'[Sales])",
        approved=True,
    )
    plan = plan.model_copy(
        update={
            "semantic_model": plan.semantic_model.model_copy(
                update={"measures": [*plan.semantic_model.measures, advanced]}
            )
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    with pytest.raises(SemanticProjectError, match="unsupported DAX function"):
        builder.build("Hallucinated DAX", manifest, plan)


def test_builder_rejects_circular_advanced_dax_dependencies(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Sales\n100\n")]
    )
    plan = _approved_plan(manifest)
    first = ProposedDaxMeasure(
        id="measure_a",
        name="Measure A",
        expression="[Measure B]",
        dependencies=["measure_b"],
        approved=True,
    )
    second = ProposedDaxMeasure(
        id="measure_b",
        name="Measure B",
        expression="[Measure A]",
        dependencies=["measure_a"],
        approved=True,
    )
    plan = plan.model_copy(
        update={
            "semantic_model": plan.semantic_model.model_copy(
                update={"measures": [*plan.semantic_model.measures, first, second]}
            )
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    with pytest.raises(SemanticProjectError, match="circular"):
        builder.build("Circular DAX", manifest, plan)


@pytest.mark.parametrize(
    "updates",
    [
        {"name": "Sales\n\tmeasure injected = 1"},
        {"name": "Sales\x00"},
        {"format_string": "0.0\n\tmeasure injected = 1"},
        {"format_string": "0.0\r\n\tmeasure injected = 1"},
    ],
)
def test_builder_rejects_tmdl_scalar_injection_even_after_internal_copy(tmp_path, updates) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [UploadedSource(name="sales.csv", content=b"Sales\n100\n150\n")]
    )
    plan = _approved_plan(manifest)
    first, *remaining = plan.semantic_model.measures
    plan = plan.model_copy(
        update={
            "semantic_model": plan.semantic_model.model_copy(
                update={"measures": [first.model_copy(update=updates), *remaining]}
            )
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )
    with pytest.raises(SemanticProjectError, match="single-line"):
        builder.build("Injection", manifest, plan)
    assert not list((tmp_path / "projects").glob("**/*.pbip"))


def test_builder_writes_only_reviewed_nondefault_relationship_properties(tmp_path) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="customers.csv",
                content=b"customer_id,name\n1,A\n2,B\n",
            ),
            UploadedSource(
                name="orders.csv",
                content=b"customer_id,amount\n1,10\n1,20\n2,30\n",
            ),
        ]
    )
    relationship = RelationshipCandidate(
        id="orders_to_customers",
        from_table_id=next(table.id for table in manifest.tables if table.name == "orders"),
        from_column_id="customer_id",
        to_table_id=next(table.id for table in manifest.tables if table.name == "customers"),
        to_column_id="customer_id",
        cardinality="many_to_one",
        cross_filter_direction="both",
        is_active=False,
        rely_on_referential_integrity=True,
        confidence=1,
        evidence=["Full snapshots checked"],
        review_status="approved",
    )
    plan = DashboardPlanningService(None).plan("Build a joined dashboard", manifest, [relationship])
    plan = plan.model_copy(
        update={
            "approved": True,
            "semantic_model": plan.semantic_model.model_copy(
                update={
                    "approved": True,
                    "tables": [
                        item.model_copy(
                            update={
                                "display_name": (
                                    "Orders Fact"
                                    if item.table_id == relationship.from_table_id
                                    else "Customer Dimension"
                                )
                            }
                        )
                        for item in plan.semantic_model.tables
                    ],
                }
            ),
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    pbip, receipt = builder.build("Relationship Properties", manifest, plan)

    assert receipt.passed
    relationship_tmdl = next(
        pbip.parent.glob("*.SemanticModel/definition/relationships.tmdl")
    ).read_text(encoding="utf-8")
    assert "fromColumn: 'Orders Fact'.customer_id" in relationship_tmdl
    assert "toColumn: 'Customer Dimension'.customer_id" in relationship_tmdl
    assert "crossFilteringBehavior: bothDirections" in relationship_tmdl
    assert "isActive: false" in relationship_tmdl
    assert "relyOnReferentialIntegrity: true" in relationship_tmdl
