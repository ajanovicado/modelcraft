"""The editing API document names every route and every operation the code has."""

from __future__ import annotations

import re
import typing
from pathlib import Path

from ai_powerbi_analyst.studio import report_edits

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "imported-report-editing-api.md").read_text(encoding="utf-8")
API = (ROOT / "src" / "ai_powerbi_analyst" / "studio" / "api.py").read_text(encoding="utf-8")


def _leaves(annotation: object) -> list[type]:
    args = typing.get_args(annotation)
    if args and typing.get_origin(annotation) is not typing.Literal:
        return [leaf for arg in args for leaf in _leaves(arg)]
    return [annotation] if isinstance(annotation, type) else []


def _shape(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def test_every_imported_report_route_is_in_the_document() -> None:
    routes = re.findall(
        r'@app\.(?:get|post|patch|delete)\("(/v1/workspaces/\{workspace_id\}/'
        r'(?:imported-report[^"]*|open-source-report)|/v1/imported-report/[^"]*)"',
        API,
    )
    # The judgement routes (review, model health, report tests, explain) joined in round 3.
    assert len(routes) >= 29
    documented = _shape(DOC)
    for route in routes:
        tail = _shape(route.removeprefix("/v1/workspaces/{workspace_id}"))
        assert tail in documented, f"{route} is not in imported-report-editing-api.md"


def test_the_chat_answer_routes_are_in_the_document() -> None:
    for route in ("/chat/edit-choice", "/chat/edit-confirm"):
        assert f'"/v1/workspaces/{{workspace_id}}{route}"' in API
        assert route in DOC


def _operation_rows() -> dict[str, str]:
    """The rows of the Operations tables, by every op name their first cell names."""
    section = DOC[DOC.index("## Operations") : DOC.index("## The edit layer as a Python API")]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        if line.startswith("| `"):
            first = line.split(" | ")[0]
            for name in re.findall(r"`([a-z_]+)`", first):
                rows[name] = rows.get(name, "") + line
    return rows


def test_every_operation_is_in_the_document_with_its_fields() -> None:
    classes = [c for c in _leaves(report_edits.ReportOperation) if hasattr(c, "model_fields")]
    # The 103 retained operations include conditional formatting, navigation, layout and model
    # editing; styling operations have been removed.
    assert len(classes) == 103
    rows = _operation_rows()
    for cls in classes:
        name = cls.model_fields["op"].default
        assert f"`{name}`" in DOC, f"operation {name} is not in the document"
        row = rows.get(name)
        assert row is not None, f"operation {name} has no row in the Operations tables"
        missing = [
            field
            for field in cls.model_fields
            if field != "op" and not re.search(rf"\b{field}\b", row)
        ]
        assert missing == [], f"the row of {name} does not name {missing}"
    # The fields the integration brief singles out.
    for field in (
        "cleanup_references",
        "scale",
        "replace_conditional",
        "hidden",
    ):
        assert field in DOC, field


def test_the_document_names_the_edit_layer_entry_points() -> None:
    for name in (
        "apply_report_operations",
        "validate_findings",
        "capture_baseline",
        "HandlerResult",
    ):
        assert name in DOC
        assert hasattr(report_edits, name) or name == "HandlerResult"


def test_the_receipt_sample_names_every_field_the_code_sends() -> None:
    source = (ROOT / "src" / "ai_powerbi_analyst" / "studio" / "imported_editing.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def receipt_payload")
    body = source[start : source.index("\n    def ", start + 1)]
    fields = set(re.findall(r'^\s+"([a-z_]+)":', body, re.MULTILINE))
    assert {"protected", "replaced", "visible_in", "pending_model_operations"} <= fields
    sample = DOC[DOC.index("`EditReceipt`:") : DOC.index("`adopted` is the revision row")]
    for field in fields:
        assert f'"{field}"' in sample, f"{field} is not in the EditReceipt sample"
