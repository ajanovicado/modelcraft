"""DAX Lab Explain answers in prose, and the lab retrieves with the user's vocabulary.

Measured (candidates-20260924 X1/X2): explain mode reused the formula prompt ("Return
JSON with one expression field"), so Qwen 4B answered {"expression", "explanation"} and
Gemma put a ```json block restating the formula ahead of its prose. Both reached the UI,
the event log and the next turn's discussion.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_powerbi_analyst.knowledge.library import Reference, RetrievalReceipt
from ai_powerbi_analyst.llm.contract_ollama import ContractOllamaModelRuntime
from ai_powerbi_analyst.llm.runtime import LocalModelError
from ai_powerbi_analyst.settings import ModelSettings
from ai_powerbi_analyst.studio import dax_assistant
from ai_powerbi_analyst.studio.contracts import (
    CreateWorkspaceRequest,
    DaxAssistRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.dax_assistant import (
    _explanation_prose,
    _explanation_stream,
    dax_assistant_events,
)
from ai_powerbi_analyst.studio.report_import import ImportedPage, ImportedReport
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

GOAL = "Explain this formula and its filter context briefly."  # the Explain button
X1 = "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))"
X2 = "CALCULATE(SUM('Orders'[Sales]), 'Returns'[Returned] = TRUE())"
PROSE = "This formula sums Sales for returned orders."
OTHER = "SUM('Orders'[Profit])"

# Recorded replies, byte for byte: Qwen 4B X2 (both runs) and Gemma E2B X1/X2.
QWEN_X2_PROSE = (
    "This formula calculates the total sales from orders that have been returned, "
    "using a filter on the Returns table to identify them."
)
QWEN_X2 = json.dumps({"expression": X2, "explanation": QWEN_X2_PROSE}, indent=2)
GEMMA_X1_PROSE = (
    "This formula calculates the ratio of total profit to total sales from the 'Orders' "
    "table. It uses the `DIVIDE` function to safely divide the sum of the 'Profit' column "
    "by the sum of the 'Sales' column. The result represents the profit margin as a "
    "fraction."
)
GEMMA_X2_PROSE = (
    "This formula calculates the total sum of the 'Sales' column from the 'Orders' table. "
    "It does this while applying a filter that restricts the calculation only to those "
    "rows in the 'Returns' table where the 'Returned' column is true. In essence, it "
    "finds the total sales amount associated with orders that have corresponding return "
    "records marked as returned."
)


def _fenced(expression: str, prose: str) -> str:
    return "```json\n" + json.dumps({"expression": expression}, indent=2) + "\n```\n\n" + prose


@pytest.fixture
def lab(tmp_path: Path) -> tuple[StudioService, str]:
    """Superstore-shaped: Orders, and Returns flagging some of them."""
    (tmp_path / "Orders.csv").write_text(
        "Order ID,Region,Sales,Profit\nA-1,East,100,10\nA-2,West,250,-5\nA-3,West,150,30\n",
        encoding="utf-8",
    )
    (tmp_path / "Returns.csv").write_text("Returned,Order ID\nYes,A-2\n", encoding="utf-8")
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Explain",
            selected_folder=str(tmp_path),
            idempotency_key="explain-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(tmp_path / name) for name in ("Orders.csv", "Returns.csv")],
            idempotency_key="explain-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    return service, workspace.id


def _request(service: StudioService, workspace_id: str, **fields: object) -> DaxAssistRequest:
    return DaxAssistRequest(
        workspace_id=workspace_id,
        snapshot_id=service.workspace(workspace_id).active_snapshot_id,
        request_id="explain-" + uuid4().hex,
        **{"goal": GOAL, "mode": "explain", **fields},
    )


def _answered(service: StudioService, workspace_id: str, request_id: str) -> dict:
    return next(
        e["payload"]
        for e in reversed(service.events(workspace_id))
        if e["type"] == "dax.assistant_answered" and e["payload"]["request_id"] == request_id
    )


def _tokens(events: list[dict]) -> str:
    return "".join(e["data"]["text"] for e in events if e["event"] == "token")


def _explain(
    service: StudioService, workspace_id: str, expression: str, output: str, size: int = 3
) -> tuple[str, dict, dict]:
    """Tokens, answered event and model call for an explain turn that replays ``output``."""
    calls: list[dict] = []

    def stream_chat(message, **kwargs):
        calls.append({"message": message, **kwargs})
        for start in range(0, len(output), size):
            yield output[start : start + size]

    service.model = SimpleNamespace(stream_chat=stream_chat)
    request = _request(service, workspace_id, expression=expression)
    events = list(dax_assistant_events(service, request, Event()))
    return _tokens(events), _answered(service, workspace_id, request.request_id), calls[0]


def test_explain_prompt_asks_for_prose_not_a_json_formula(lab) -> None:
    service, workspace_id = lab
    _, answered, call = _explain(service, workspace_id, X2, PROSE)
    system, message = call["system_prompt"], call["message"]
    assert "Return JSON" not in system + message
    assert "expression field" not in system + message
    assert "plain English" in system and "plain English" in message
    assert "DAX formula to explain:\n```dax\n" + X2 in message
    assert "to refine" not in message
    # The schema context stays; the formula-writing contract and references do not.
    assert "DAX schema (use these table and column names):" in system
    assert '{"table": "Orders"' in system and '{"table": "Returns"' in system
    assert "Preview supports" not in system and "Quoted references" not in system
    assert call["worked_examples"] is False and call["history"] == []
    assert answered["reference_context_supplied"] is False and answered["citations"] == []


def test_explain_json_object_reply_renders_its_explanation(lab) -> None:
    service, workspace_id = lab
    text, answered, _ = _explain(service, workspace_id, X2, QWEN_X2)
    assert text == answered["response"] == QWEN_X2_PROSE
    assert answered["status"] == "completed"


@pytest.mark.parametrize(
    ("expression", "prose"), [(X1, GEMMA_X1_PROSE), (X2, GEMMA_X2_PROSE)], ids=["x1", "x2"]
)
def test_explain_drops_a_leading_block_that_restates_the_formula(lab, expression, prose) -> None:
    service, workspace_id = lab
    text, answered, _ = _explain(service, workspace_id, expression, _fenced(expression, prose))
    assert text == answered["response"] == prose


@pytest.mark.parametrize(
    "raw",
    [
        f"```dax\n{OTHER}\n```\nUse Profit instead.",
        '{"expression": "SUM(x)", "explanation": "This formula ad',  # cut at the token cap
    ],
    ids=["another-formula", "truncated-json"],
)
def test_explain_keeps_blocks_that_do_not_restate_the_formula(lab, raw) -> None:
    service, workspace_id = lab
    text, answered, _ = _explain(service, workspace_id, X2, raw)
    assert text == answered["response"] == raw


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"expression": X2.replace("TRUE()", "TRUE"), "explanation": PROSE}),
        json.dumps(
            {
                "expression": "CALCULATE(SUM(Orders[Sales]), Returns[Returned] = TRUE())",
                "explanation": PROSE,
            }
        ),
        json.dumps({"expression": "Returned Sales = " + X2, "explanation": PROSE}, indent=2),
        json.dumps({"measure_name": "Returned Sales", "expression": X2, "explanation": PROSE}),
    ],
    ids=["true-without-parentheses", "unquoted-tables", "measure-name", "extra-key"],
)
def test_near_miss_restatements_render_prose(lab, raw) -> None:
    service, workspace_id = lab
    text, answered, _ = _explain(service, workspace_id, X2, raw)
    assert text == answered["response"] == PROSE


def test_a_different_formula_in_a_json_reply_follows_as_a_dax_block(lab) -> None:
    service, workspace_id = lab
    raw = json.dumps({"expression": OTHER, "explanation": PROSE}, indent=2)
    text, answered, _ = _explain(service, workspace_id, X2, raw)
    assert (
        text
        == answered["response"]
        == (f"{PROSE}\n\nThe reply also proposed this formula:\n```dax\n{OTHER}\n```")
    )
    assert '{"expression"' not in text and '"expression":' not in text


def test_explain_prose_still_streams_from_the_first_chunk(lab) -> None:
    service, workspace_id = lab

    def stream_chat(message, **kwargs):
        yield "  This"
        raise AssertionError("the first token must not wait for a second chunk")

    service.model = SimpleNamespace(stream_chat=stream_chat)
    stream = dax_assistant_events(service, _request(service, workspace_id, expression=X2), Event())
    assert next(stream)["event"] == "status"
    assert next(stream)["event"] == "assistant"
    assert next(stream)["data"]["text"] == "  This"
    stream.close()


def test_cancel_while_holding_a_json_reply_records_cancelled(lab) -> None:
    service, workspace_id = lab
    cancel = Event()
    closed = []

    def stream_chat(message, **kwargs):
        try:
            yield '{"explanation": "par'
            cancel.set()
            yield 'tial"}'
        finally:
            closed.append(True)

    service.model = SimpleNamespace(stream_chat=stream_chat)
    request = _request(service, workspace_id, expression=X2)
    events = list(dax_assistant_events(service, request, cancel))
    assert [e["event"] for e in events] == ["status", "assistant"]
    assert closed == [True]
    answered = _answered(service, workspace_id, request.request_id)
    # The held reply is kept raw as evidence; replay shows only completed turns.
    assert answered["status"] == "cancelled"
    assert answered["response"] == '{"explanation": "partial"}'


def test_a_stream_error_while_holding_keeps_the_raw_reply_as_evidence(lab) -> None:
    service, workspace_id = lab
    seen: list[dict] = []

    def stream_chat(message, **kwargs):
        yield '{"explanation": "par'
        raise LocalModelError("Local model response did not finish: length")

    service.model = SimpleNamespace(stream_chat=stream_chat)
    request = _request(service, workspace_id, expression=X2)
    with pytest.raises(LocalModelError, match="did not finish"):
        for event in dax_assistant_events(service, request, Event()):
            seen.append(event)
    assert [e["event"] for e in seen] == ["status", "assistant"]
    answered = _answered(service, workspace_id, request.request_id)
    assert answered["status"] == "interrupted"
    assert answered["response"] == '{"explanation": "par'


def test_imported_report_explain_lists_original_measures_as_plain_lines(lab, monkeypatch) -> None:
    service, workspace_id = lab
    report = ImportedReport(
        id="imported-explain",
        workspace_id=workspace_id,
        name="Imported report",
        source_path="test.pbix",
        archived_path="copy.pbix",
        source_hash="a" * 64,
        base_hash="a" * 64,
        format="pbix",
        pages=[ImportedPage(id="page", title="Overview")],
        measures=[{"TableName": "Measures", "Name": "Revenue", "Expression": "SUM(Orders[Sales])"}],
    )
    workspace = service.workspace(workspace_id).model_copy(update={"imported_report_id": report.id})
    monkeypatch.setattr(service, "workspace", lambda _id: workspace)
    monkeypatch.setattr(service, "imported_report", lambda _id: report)
    # Never probe a Power BI Desktop that happens to run on the test machine.
    monkeypatch.setattr(dax_assistant, "_desktop_check", lambda *_args: "closed")
    _, _, call = _explain(service, workspace_id, "DIVIDE([Revenue], SUM('Orders'[Profit]))", PROSE)
    system = call["system_prompt"]
    assert "Original measures the formula may reference:\n[Revenue] = SUM(Orders[Sales])" in system
    assert '"expression"' not in system

    # Formula prompts list the measures the request names as plain lines too.
    systems: list[str] = []

    def generate(system, message, response_model, **kwargs):
        systems.append(system)
        raise LocalModelError("stopped before the Desktop engine")

    service.model = SimpleNamespace(generate=generate, unload=lambda: None)
    request = _request(service, workspace_id, goal="Calculate Revenue for West", mode="generate")
    with pytest.raises(LocalModelError):
        list(dax_assistant_events(service, request, Event()))
    assert "\nMeasures (reuse by [Name]):\n[Revenue] = SUM(Orders[Sales])" in systems[0]
    assert '"expression"' not in systems[0]


def test_explain_through_the_real_runtime_leaves_out_worked_examples(
    lab, monkeypatch, tmp_path
) -> None:
    """dax_assistant and the runtime chain must agree on the keyword, or Explain breaks."""
    service, workspace_id = lab
    runtime = ContractOllamaModelRuntime(ModelSettings())
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    sent: list[list[dict]] = []
    retrieved: list[str] = []

    class Server:
        def complete(self, messages, **kwargs):
            sent.append([dict(message) for message in messages])
            for start in range(0, len(QWEN_X2), 5):
                kwargs["on_text"](QWEN_X2[start : start + 5])
            return dict(
                raw=QWEN_X2,
                output=QWEN_X2,
                complete=True,
                error=None,
                finish_reason="stop",
                tokens=60,
                prompt_tokens=400,
                seconds=1.0,
                native_timings={},
            )

        def stop(self):
            return None

    @contextmanager
    def session():
        yield Server(), evidence, evidence

    def references(_root, text):
        retrieved.append(text)
        return "\n\nWorked examples with DIFFERENT data. Chores(ChoreId integer)", ["R-1"]

    monkeypatch.setattr(runtime, "_session", session)
    monkeypatch.setattr(runtime, "_references", references)
    service.model = runtime
    request = _request(service, workspace_id, expression=X2)
    events = list(dax_assistant_events(service, request, Event()))
    assert retrieved == []
    assert "Worked examples" not in json.dumps(sent)
    answered = _answered(service, workspace_id, request.request_id)
    assert _tokens(events) == answered["response"] == QWEN_X2_PROSE


def _receipt(service: StudioService, *references: Reference) -> RetrievalReceipt:
    return RetrievalReceipt(
        captured_at="2026-09-25T00:00:00Z",
        corpus_hash=service.knowledge.corpus_hash,
        query_hash="0" * 64,
        mode="hybrid",
        references=list(references),
        context_characters=0,
    )


REFERENCE = Reference(
    id="ref-calculate",
    title="CALCULATE",
    kind="documentation",
    source_url="https://learn.microsoft.com/en-us/dax/calculate-function-dax",
    revision="test",
    text="CALCULATE evaluates an expression in a modified filter context.",
    sha256="c" * 64,
    verification="reference_reviewed",
)


def test_lab_retrieval_treats_workspace_names_as_vocabulary(lab, monkeypatch) -> None:
    service, workspace_id = lab
    calls: list[dict] = []
    found: list[RetrievalReceipt] = [_receipt(service, REFERENCE)]

    def retrieve(query, **kwargs):
        calls.append(kwargs)
        return found[0]

    monkeypatch.setattr(service.knowledge, "retrieve", retrieve)
    systems: list[str] = []

    def generate(system, message, response_model, **kwargs):
        systems.append(system)
        if "calculation_request" in response_model.model_fields:
            return response_model(calculation_request=None, answer="It sums on demand.")
        return response_model(expression="SUM('Orders'[Sales])")

    service.model = SimpleNamespace(generate=generate, unload=lambda: None)
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    names = [
        name
        for item in snapshot.tables
        for name in (item.table.name, *(column.name for column in item.table.columns))
    ]
    assert set(names) == {"Orders", "Order ID", "Region", "Sales", "Profit", "Returns", "Returned"}

    generated = _request(service, workspace_id, goal="Calculate total sales", mode="generate")
    list(dax_assistant_events(service, generated, Event()))
    assert calls[-1]["context_terms"] == names
    assert systems[-1].startswith("Write DAX using only the supplied schema. Return JSON")
    assert "Quoted references: " in systems[-1]
    answered = _answered(service, workspace_id, generated.request_id)
    assert answered["citations"] == ["ref-calculate"]
    assert answered["reference_context_supplied"] is True

    advice = _request(service, workspace_id, goal="What does a measure do?", mode="auto")
    list(dax_assistant_events(service, advice, Event()))
    assert calls[-1]["context_terms"] == names
    assert _answered(service, workspace_id, advice.request_id)["mode"] == "advice"

    # Nothing retrieved: nothing was quoted, so none is claimed.
    found[0] = _receipt(service)
    again = _request(service, workspace_id, goal="Calculate total profit", mode="generate")
    list(dax_assistant_events(service, again, Event()))
    assert _answered(service, workspace_id, again.request_id)["reference_context_supplied"] is False
    assert len(calls) == 3


def test_explain_never_retrieves_and_records_an_empty_receipt(lab, monkeypatch) -> None:
    service, workspace_id = lab
    calls: list[str] = []
    monkeypatch.setattr(service.knowledge, "retrieve", lambda query, **_: calls.append(query))
    _explain(service, workspace_id, X2, PROSE)
    assert calls == []
    receipts = [
        e["payload"] for e in service.events(workspace_id) if e["type"] == "knowledge.retrieved"
    ]
    assert len(receipts) == 1 and receipts[0]["stage"] == "dax-assistant"
    receipt = RetrievalReceipt.model_validate(
        {k: v for k, v in receipts[0].items() if k != "stage"}
    )
    assert receipt.references == [] and receipt.corpus_hash == service.knowledge.corpus_hash


# _explanation_prose and _explanation_stream on their own. Every case must come out the
# same whole or in chunks of any size.
CLEANED = {
    "json-explanation": (json.dumps({"expression": X2, "explanation": PROSE}, indent=2), PROSE),
    "json-answer-only": (json.dumps({"answer": PROSE}), PROSE),
    "json-escapes": (
        json.dumps({"explanation": 'It keeps "Returned" rows—only.'}),
        'It keeps "Returned" rows—only.',
    ),
    "json-fence-restating": (_fenced(X2, PROSE), PROSE),
    "crlf-fence": (f"```json\r\n{json.dumps({'expression': X2})}\r\n```\r\n\r\n{PROSE}", PROSE),
    "dax-fence-restating": (f"```dax\n{X2}\n```\n\n{PROSE}", PROSE),
    "other-layout": (
        "```dax\nCALCULATE(\n  SUM('Orders'[Sales]),\n  'Returns'[Returned] = TRUE()\n)\n```\n"
        + PROSE,
        PROSE,
    ),
    "bare-fence-restating": (f"```\n{X2}\n```\n{PROSE}", PROSE),
    "fenced-json-explanation": (
        f"```json\n{json.dumps({'expression': X2, 'explanation': PROSE})}\n```",
        PROSE,
    ),
    "json-then-prose": (json.dumps({"expression": X2}) + "\n\n" + PROSE, PROSE),
    "unknown-key": (json.dumps({"explanation": PROSE, "confidence": 0.9}), PROSE),
    "different-formula": (
        json.dumps({"expression": OTHER, "explanation": PROSE}),
        f"{PROSE}\n\nThe reply also proposed this formula:\n```dax\n{OTHER}\n```",
    ),
}
KEPT = {
    "prose": PROSE,
    "leading-whitespace": "\n\n  " + PROSE,
    "inline-code-opener": "`CALCULATE` sums Sales.",
    "block-with-another-formula": f"```dax\n{OTHER}\n```\n\n{PROSE}",
    "restatement-without-prose": json.dumps({"expression": X2}),
    "fenced-restatement-without-prose": f"```json\n{json.dumps({'expression': X2})}\n```",
    "malformed-json": '{"expression": "CALCULATE(SUM(\'Orders\'[Sales]), explanation: nope',
    "truncated-json": '{\n  "expression": "' + X2 + '",\n  "explanation": "This formula calc',
    "json-array": json.dumps([PROSE]),
    "explanation-not-a-string": json.dumps({"explanation": [PROSE]}),
    "unclosed-fence": f"```json\n{json.dumps({'expression': X2})}\n\n{PROSE}",
    "other-language": f"```python\nprint(1)\n```\n{PROSE}",
    "brace-led-prose": "{Sales} is summed only for returned orders.",
    "whitespace-only": "  \n ",
}
CASES = [pytest.param(text, want, id=name) for name, (text, want) in CLEANED.items()] + [
    pytest.param(text, text, id="kept-" + name) for name, text in KEPT.items()
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_explanation_prose(text: str, expected: str) -> None:
    assert _explanation_prose(text, X2) == expected


@pytest.mark.parametrize("size", [1, 3, 10_000])
@pytest.mark.parametrize(("text", "expected"), CASES)
def test_explanation_stream_cleans_the_same_in_any_chunking(
    text: str, expected: str, size: int
) -> None:
    evidence: list[str] = []
    chunks = [text[start : start + size] for start in range(0, len(text), size)]
    assert "".join(_explanation_stream(iter(chunks), X2, Event(), evidence)) == expected
    assert evidence == []


def test_without_a_supplied_formula_every_formula_field_is_shown() -> None:
    raw = json.dumps({"expression": X2, "explanation": PROSE})
    assert _explanation_prose(raw, "") == (
        f"{PROSE}\n\nThe reply also proposed this formula:\n```dax\n{X2}\n```"
    )
    assert _explanation_prose(json.dumps({"explanation": PROSE}), "  ") == PROSE


def test_prose_is_yielded_as_soon_as_its_first_character_arrives() -> None:
    pulled: list[str] = []

    def source():
        for piece in ["\n", "`", "CALCULATE`", " sums", " Sales."]:
            pulled.append(piece)
            yield piece

    stream = _explanation_stream(source(), X2, Event(), [])
    assert next(stream) == "\n`CALCULATE`"
    assert len(pulled) == 3
    assert list(stream) == [" sums", " Sales."]


def test_cancel_while_holding_stops_pulling_and_keeps_the_raw_reply() -> None:
    cancel = Event()
    pulled: list[str] = []

    def source():
        for piece in ['{"expla', 'nation": "', "partial", '"}']:
            pulled.append(piece)
            if len(pulled) == 2:
                cancel.set()
            yield piece

    evidence: list[str] = []
    assert list(_explanation_stream(source(), X2, cancel, evidence)) == []
    assert pulled == ['{"expla', 'nation": "']
    assert evidence == ['{"explanation": "']
