"""The model's facts never reach the local model's prompts.

The report keeps Power Query text (file paths, "Enter data" rows), role filters and the full
DAX of the model's objects for the model workbench (``model_facts.stored`` and the full facts
beside the history). A sentinel string planted in each of them must not appear in any prompt
chat, the edit planner or the DAX assistant builds. The report's own measure rows are what
chat and the DAX assistant read by design, so the sentinel is not planted there. Power Query
(M) code typed into chat is replaced where it stands; the rest of the message is kept.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ai_powerbi_analyst.studio import model_facts, report_import
from ai_powerbi_analyst.studio.chat_context import m_prompt_text

SENTINEL = "ZZ-SENTINEL-7f3a"
FACTS: dict[str, Any] = {
    "version": 1,
    "model": {"culture": "en-US"},
    "tables": [
        {"name": "Orders", "source": "m", "calculated": False, "lineageTag": "t-o"},
        {"name": "Time", "source": "calculationGroup", "calculated": False,
         "calculationGroup": True, "lineageTag": "t-t"},
    ],
    "columns": [
        {"table": "Orders", "name": "Region", "kind": "data", "dataType": 2},
        {"table": "Orders", "name": "Sales", "kind": "data", "dataType": 8},
    ],
    "measures": [
        {"table": "Orders", "name": "Total", "dataType": 8,
         "expression": f"SUM(Orders[Sales]) /* {SENTINEL} measure */"},
    ],
    "relationships": [],
    "roles": [{"name": "EU", "tables": [{"table": "Orders", "filter": f'"{SENTINEL} role"'}]}],
    "calculationGroups": [
        {"table": "Time", "items": [{"name": "YTD", "expression": f"/* {SENTINEL} item */ 1"}],
         "selectionExpressions": []},
    ],
    "partitions": [
        {"table": "Orders", "name": "Orders", "source": "m", "mode": "import",
         "expression": f'let Source = Csv.Document(File.Contents("C:\\{SENTINEL}.csv")) in Source'},
    ],
    "expressions": [
        {"name": "Folder", "kind": "m", "parameter": True,
         "expression": f'"C:\\{SENTINEL}" meta [IsParameterQuery=true]'},
    ],
}  # fmt: skip


def extraction(_source: Path, destination: Path) -> dict[str, Any]:
    pd.DataFrame({"Region": ["East", "West"], "Sales": [100.0, 250.0]}).to_parquet(
        destination / "table-0.parquet", index=False
    )
    return {
        "tables": [{"name": "Orders", "file": "table-0.parquet", "rows": 2}],
        "schema": model_facts.schema_rows(FACTS),
        "measures": [{"TableName": "Orders", "Name": "Total", "Expression": "SUM(Orders[Sales])"}],
        "relationships": [],
        "facts": FACTS,
    }


def pbix(path: Path, *, title: str = "P") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Report/definition/pages/pages.json", json.dumps({"pageOrder": ["p"]}))
        archive.writestr(
            "Report/definition/pages/p/page.json", json.dumps({"name": "p", "displayName": title})
        )
        archive.writestr("DataModel", b"model-bytes")
    return path


def test_no_prompt_builder_reads_the_models_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_powerbi_analyst.studio.dax_assistant import _imported_context
    from ai_powerbi_analyst.studio.edit_context import EditContext
    from ai_powerbi_analyst.studio.edit_planner import TooLarge, build_prompt
    from ai_powerbi_analyst.studio.edit_tools import tool_cards
    from ai_powerbi_analyst.studio.report_import import ImportReportRequest, imported_relationships
    from ai_powerbi_analyst.studio.service import StudioService

    monkeypatch.setattr(report_import, "extract_model", extraction)
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.import_existing_report(
        ImportReportRequest(path=str(pbix(tmp_path / "Orders.pbix")), idempotency_key="privacy-01")
    )
    report = service.imported_report(workspace.id)
    # The facts are where the workbench reads them: stored with the report (cut, but the
    # sentinels are near the start) and in full beside the history.
    assert SENTINEL in json.dumps(report.model)
    assert SENTINEL in json.dumps(service.editing.full_model_facts(workspace.id))
    assert SENTINEL in json.dumps(service.imported_report_payload(workspace.id)["model"])
    snapshot = service.snapshot(workspace.active_snapshot_id or "")
    prompts: list[str] = []
    for question in ("Explain Total", "What does the Orders query read?", "Show the roles"):
        prompts.append(service._chat_context(workspace, question))
        prompts.append(
            _imported_context(
                snapshot,
                report.measures,
                imported_relationships(report, snapshot),
                question,
                3_000,
                goal=question,
                expression="[Total]",
                mode="explain",
            )
        )
    context = EditContext.from_report(report)
    cards = tool_cards()
    for text in (
        "rename the Orders query",
        "change the power query of Orders",
        "edit Total",
        f'set the query to let Source = "{SENTINEL}" in Source',
    ):
        try:
            prompt = build_prompt(text, context, cards)
        except TooLarge:
            continue
        prompts.append(prompt.user + json.dumps(prompt.schema))
    assert prompts and all(SENTINEL not in prompt for prompt in prompts)


@pytest.mark.parametrize(
    "text",
    [
        "What is 10 + 5?",
        "Make the chart 10 pixels wider",
        "Rename the chart to \x60Sales (EUR)\x60",
        "Rename the chart to KPI (top)",
        "Format the Profit measure (in millions)",
        "Use [Total] in the table",
        "Use \x60Orders[Sales]\x60 in the table",
        "Rename the measure to Sales (EUR)",
        "Format the Profit measure's number format (in millions)",
        "Format the Profit measure's number format (0.0%)",
        "Show query fields (including hidden ones)",
        "Create a return rate: units returned / units sold",
        "Return rate across FactSales",
        "evaluate sales by region",
        "Is sales = cost + profit?",
        "let me know what is in Orders",
    ],
)
def test_prompt_privacy_keeps_names_and_ordinary_requests(text: str) -> None:
    assert m_prompt_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        f'query: let Source = "{SENTINEL}" in Source',
        f'query: let #"Source Step" = "{SENTINEL}" in #"Source Step"',
    ],
)
def test_prompt_privacy_withholds_typed_power_query(text: str) -> None:
    safe = m_prompt_text(text)
    assert SENTINEL not in safe
    assert m_prompt_text(safe) == safe


def test_prompt_privacy_keeps_a_second_plan_piece_after_a_typed_formula() -> None:
    from ai_powerbi_analyst.studio.edit_context import EditContext, PageEntry, VisualEntry
    from ai_powerbi_analyst.studio.edit_planner import build_prompt
    from ai_powerbi_analyst.studio.edit_tool_cards import TOOL_CARDS

    context = EditContext(
        pages=[PageEntry("p", "Sales", 0, 1280, 720)],
        visuals=[VisualEntry("v", "p", "Sales", "columnChart", "Sales", (), 0, 0, 500, 300)],
        measures=[],
        tables={"Orders": ["Sales"]},
        active_page="p",
    )
    pieces = [
        f'create measure X = SUM(Orders[Sales]) + LEN("{SENTINEL}")',
        "move the Sales chart to x=100",
    ]
    combined = " ".join(f"{number}) {piece}." for number, piece in enumerate(pieces, 1))
    cards = tuple(card for card in TOOL_CARDS if card.op in {"create_measure", "set_position"})
    prompt = build_prompt(combined, context, cards)
    request = prompt.user.split("\n", 1)[0]
    # The local model may see the user's own DAX; a typed formula must not cut off the next piece.
    assert "2) move the Sales chart to x=100" in request


def test_imported_chat_withholds_typed_power_query_but_keeps_dax_and_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.report_import import ImportReportRequest
    from ai_powerbi_analyst.studio.service import StudioService

    monkeypatch.setattr(report_import, "extract_model", extraction)
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.import_existing_report(
        ImportReportRequest(path=str(pbix(tmp_path / "Orders.pbix")), idempotency_key="privacy-02")
    )
    captures: list[dict[str, Any]] = []

    def stream(message: str, **options: Any) -> Any:
        captures.append({"message": message, **options})
        yield "The model has Total and Orders[Sales]."

    service.model = SimpleNamespace(stream_chat=stream)  # type: ignore[assignment]
    m_code = f'let Source = Csv.Document(File.Contents("C:\\{SENTINEL}.csv")) in Source'
    service.store.add_message(workspace.id, "user", f"my query is {m_code}, is it right?")
    service.store.add_message(workspace.id, "assistant", "It reads one CSV file.")
    raw = (
        f"The query {m_code} feeds Total. Why is Total = SUM(Orders[Sales]) "
        "blank for 2024, and how do I fix it?"
    )
    list(service.chat(workspace.id, raw, selected_context=raw))
    assert captures
    sent = json.dumps(captures)
    assert SENTINEL not in sent and "Csv.Document" not in sent
    # Only the M span is replaced: the user's DAX and the rest of the request reach the
    # local model.
    assert "Total = SUM(Orders[Sales]) blank for 2024, and how do I fix it?" in sent
    assert "[Power Query code kept locally] feeds Total" in sent
    assert "[Power Query code kept locally], is it right?" in sent
    assert any(SENTINEL in str(row) for row in service.store.messages(workspace.id))


@pytest.mark.parametrize(
    "message,answer",
    [
        (
            "Explain Python decorators and when to use them. Keep it concise.",
            "A decorator wraps a function.",
        ),
        (
            "Create a SQL query for a general example",
            "SELECT [customer_id] FROM [dbo].[Customers];",
        ),
        ("Create a Python function for a general example", 'return payload["customer_id"]'),
    ],
)
def test_general_developer_discussion_omits_imported_bi_context_and_bracket_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, answer: str
) -> None:
    from threading import Event

    from ai_powerbi_analyst.studio.contracts import ChatRequest, ConversationContext
    from ai_powerbi_analyst.studio.report_import import ImportReportRequest
    from ai_powerbi_analyst.studio.service import StudioService

    monkeypatch.setattr(report_import, "extract_model", extraction)
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.import_existing_report(
        ImportReportRequest(
            path=str(pbix(tmp_path / "Orders.pbix", title="Scorecard")),
            idempotency_key="technical-import",
        )
    )
    captured: dict[str, Any] = {}

    def stream(message: str, **options: Any) -> Any:
        captured.update(message=message, **options)
        yield answer

    def no_context(*_args: Any, **_kwargs: Any) -> str:
        pytest.fail("General technical discussion must not load an unrelated imported schema")

    monkeypatch.setattr(service.model, "stream_chat", stream)
    monkeypatch.setattr(service, "_chat_context", no_context)
    events = service.chat_events(
        ChatRequest(
            workspace_id=workspace.id,
            message=message,
            context=ConversationContext(page_id="p"),
            idempotency_key="technical-chat",
        ),
        Event(),
    )
    reply = "".join(item["data"]["text"] for item in events if item["event"] == "token")
    assert reply == answer
    assert captured["message"] == message
    assert captured["max_output_tokens"] == 768
    prompt = captured["system_prompt"]
    assert "general technical knowledge" in prompt
    assert "Clearly label hypothetical names and values used in examples" in prompt
    assert "Do not present examples as facts about the user's workspace" in prompt
    assert "Power BI" not in prompt
    assert "DAX" not in prompt
    assert "Orders[Sales]" not in prompt
    assert "Scorecard" not in prompt
    assert "imported PBIX" not in prompt
    assert "data_dates" not in prompt
    assert "Follow joins" not in prompt
    assert "Modeling:" not in prompt
    assert SENTINEL not in json.dumps(captured)
    assert service.workspace(workspace.id) == workspace


def test_power_bi_developer_answer_still_flags_unknown_model_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_powerbi_analyst.studio.report_import import ImportReportRequest
    from ai_powerbi_analyst.studio.service import StudioService

    monkeypatch.setattr(report_import, "extract_model", extraction)
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.import_existing_report(
        ImportReportRequest(path=str(pbix(tmp_path / "Orders.pbix")), idempotency_key="bi-import")
    )

    def stream(message: str, **options: Any) -> Any:
        assert "imported PBIX" in options["system_prompt"]
        yield 'EVALUATE ROW("Example", SUM(Missing[Field]))'

    monkeypatch.setattr(service.model, "stream_chat", stream)
    reply = "".join(service.chat(workspace.id, "Create a DAX query for this Power BI report"))
    assert "not found in this report's model" in reply
    assert "Missing[Field]" in reply


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f'Why does let Source = "{SENTINEL}" in Source fail? The rest stays.',
            "Why does [Power Query code kept locally] fail? The rest stays.",
        ),
        (
            f'A: let a = let b = "{SENTINEL} in x" in b in a. B: keep this',
            "A: [Power Query code kept locally]. B: keep this",
        ),
        (
            f'```m\nlet Source = "{SENTINEL}"\nin Source\n```\nThen show sales by region',
            "[Power Query code kept locally]\nThen show sales by region",
        ),
        (
            f'Half a query: let Source = "{SENTINEL}", Next = 1\n\nShow sales by region',
            "Half a query: [Power Query code kept locally]\n\nShow sales by region",
        ),
    ],
)
def test_m_prompt_text_replaces_only_the_power_query_span(text: str, expected: str) -> None:
    assert m_prompt_text(text) == expected
    assert m_prompt_text(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "let me know what is in Orders",
        "What is the return rate by region?",
        "Show sales IN the West region",
        "The measure returns 0 for 2024, why?",
        "Make [Total Sales] year to date: CALCULATE([Total Sales], DATESYTD('Date'[Date]))",
        "```dax\nVAR x = SUM(Orders[Sales]) RETURN x\n```\nWhy is it blank?",
        'Is sales = cost + profit? See "https://learn.microsoft.com/dax" for help.',
    ],
)
def test_m_prompt_text_keeps_dax_and_ordinary_requests(text: str) -> None:
    assert m_prompt_text(text) == text


def test_m_prompt_text_is_linear_on_long_input() -> None:
    import time

    for unit in ('"a" & ', "1 - -1 + ", "let x = 1 in ", 'let y = "', "[x] + "):
        text = unit * (20_000 // len(unit))
        started = time.perf_counter()
        m_prompt_text(text)
        assert time.perf_counter() - started < 0.5, unit
