"""Frozen, local reference-grounded Qwen task review; never executes model code."""

# ruff: noqa: E501 -- preserve the frozen evaluation wording verbatim.

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from ai_powerbi_analyst.knowledge import KnowledgeLibrary
from ai_powerbi_analyst.knowledge.library import REFERENCE_RULES
from ai_powerbi_analyst.llm.runtime import OllamaModelRuntime
from ai_powerbi_analyst.settings import ModelSettings

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    (
        "margin",
        "DAX ratio",
        "Reviewed schema: SalesLines[Revenue] and SalesLines[Cost] are numeric at the same grain. "
        "Write a measure Margin Percentage as total revenue minus total cost, divided by total "
        "revenue, returning BLANK when revenue is zero. Preserve current filters. DAX only.",
        "DIVIDE(SUM(SalesLines[Revenue])-SUM(SalesLines[Cost]),SUM(SalesLines[Revenue])); not average row margins.",
    ),
    (
        "weighted",
        "DAX iterator",
        "Reviewed schema: LabReadings[Reading] and LabReadings[SampleCount] are numeric. "
        "Write Weighted Reading, weighting Reading by SampleCount, BLANK for zero total weight. "
        "Use only these exact columns. DAX only.",
        "DIVIDE(SUMX(LabReadings,LabReadings[Reading]*LabReadings[SampleCount]),SUM(LabReadings[SampleCount])).",
    ),
    (
        "lod",
        "DAX fixed grain",
        "Reviewed schema: a single table Tickets with Team, Priority, ClosedDate, ResolutionMinutes; "
        "no related tables. [Total Resolution Minutes] is an existing measure. Write Team Fixed Minutes "
        "preserving explicit Tickets[Team] filters but removing every other Tickets column filter. "
        "Do not hard-code a Team or use an unqualified column. DAX only.",
        "CALCULATE([Total Resolution Minutes],ALLEXCEPT(Tickets,Tickets[Team])).",
    ),
    (
        "scalar",
        "DAX filter context",
        "Reviewed schema: Receipts[Amount], Receipts[Store]. Explain and correct this measure so it "
        "ignores Store: VAR A=SUM(Receipts[Amount]) RETURN CALCULATE(A,REMOVEFILTERS(Receipts[Store])). "
        "Give corrected DAX and one sentence; do not invent fields.",
        "Scalar variable not recalculated; CALCULATE(SUM(Receipts[Amount]),REMOVEFILTERS(Receipts[Store])).",
    ),
    (
        "rolling",
        "DAX rolling window",
        "Reviewed schema: contiguous Calendar[Date] with an approved active one-to-many relationship "
        "to Events[EventDate]. [Event Count] exists. Write Rolling 7 Days ending at the maximum "
        "selected date, including that date and six prior days. Clear Calendar filters only. DAX only.",
        "Filter ALL(Calendar) dates between MAX(Calendar[Date])-6 and end; equivalent date-aware window allowed.",
    ),
    (
        "inactive",
        "DAX relationship activation",
        "Reviewed schema: Calendar[Date] has an active relationship to Dispatch[OrderDate] and an "
        "approved inactive relationship to Dispatch[ShipDate]. [Total Value] exists. Write Shipped Value "
        "using the ship-date relationship. Do not create a table or new relationship. DAX only.",
        "CALCULATE([Total Value],USERELATIONSHIP(Dispatch[ShipDate],Calendar[Date])).",
    ),
    (
        "fanout",
        "Modeling grain",
        "Reviewed profiles: InvoiceHeaders has one row per InvoiceID and numeric Freight; InvoiceLines "
        "has multiple rows per InvoiceID. A merge repeats Freight on every line. Two invoices can "
        "have equal Freight. How should freight be totaled without double counting? Explain in under "
        "80 words, and distinguish a relationship from a materialized merge.",
        "Sum separate header fact at InvoiceID grain; not SUM repeated line Freight or DISTINCT monetary values; relationship is filter propagation not row merge.",
    ),
    (
        "duplicate_keys",
        "Modeling integrity",
        "Reviewed profiles: Customers[CustomerID] has duplicates with conflicting Country values. "
        "Orders[CustomerID] also repeats. Can we approve Customers 1:* Orders now? Propose the next "
        "safe step in under 80 words. There is no Country column in Orders.",
        "No unique one side; investigate identities and reviewed dimension/key mapping; cannot form Country+ID key in Orders or arbitrary deduplicate.",
    ),
    (
        "budget",
        "Modeling mixed grain",
        "Reviewed profiles: Budget has Month, Category, BudgetAmount, one row per month/category. "
        "Actuals has Date, ProductID, Category, SalesAmount, many rows per day/product. Users want "
        "product-level budget versus sales. No allocation rule exists. Recommend a model and explain "
        "what budget should show at product detail, in under 80 words.",
        "Separate facts/common valid dimensions, no row merge fanout; no invented allocation; budget BLANK/suppressed below month/category until reviewed rule.",
    ),
    (
        "nested_join",
        "Power Query join",
        "In Power Query Table.NestedJoin, what happens when joinKind is omitted? Does it immediately "
        "expand the matched rows into the outer table? Is keyEqualityComparers intended for normal "
        "public use? Under 80 words.",
        "Left outer by default; matches nested in new column, separate expansion; comparers internal use only.",
    ),
    (
        "locale",
        "Power Query locale",
        "Reviewed input query Source has a text Amount column containing German numbers such as "
        "1.234,56. Write one Power Query M expression using Table.TransformColumnTypes to convert "
        "Amount to type number with explicit German culture. Do not strip punctuation. M only.",
        'Table.TransformColumnTypes(Source, {{"Amount", type number}}, "de-DE").',
    ),
    (
        "buffer",
        "Power Query performance",
        "Does Table.Buffer always speed up Power Query? Explain its effect on downstream query "
        "folding and distinguish it from Table.StopFolding. Under 80 words.",
        "Can slow down due to buffering cost, prevents downstream folding; StopFolding when only preventing folding without materializing/buffering.",
    ),
]


def main() -> None:
    output = (
        ROOT / ".runtime" / ("qwen-reference-tasks-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    )
    output.mkdir(parents=True)
    library = KnowledgeLibrary(ROOT / ".runtime/studio-dev-state/knowledge/index.sqlite3")
    model = OllamaModelRuntime(ModelSettings(name="qwen3.5:4b-q4_K_M", chat_output_tokens=320))
    system = (
        "You are answering isolated Power BI evaluation questions. Schemas explicitly "
        "marked reviewed in a question are the complete approved schema for that question. "
        "Use only those identifiers. Do not claim execution, approvals or file changes. "
        "Be concise, and ask for missing information instead of inventing it." + REFERENCE_RULES
    )
    try:
        identity = model.capture_attestation().model_dump(mode="json")
        runs = []
        for key, category, question, rubric in CASES:
            receipt = library.retrieve(question, max_characters=2400)
            runs.append(
                {
                    "id": key,
                    "category": category,
                    "question": question,
                    "review_rubric_not_sent_to_model": rubric,
                    "receipt": receipt.model_dump(mode="json"),
                    "system": system + "\nQuoted reference material:\n" + receipt.context(),
                }
            )
        (output / "design.json").write_text(
            json.dumps(
                {
                    "identity": identity,
                    "settings": model.settings.model_dump(),
                    "runs": runs,
                    "limitations": "One grounded attempt per task; no baseline, no repair, no model code execution.",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(str(output), flush=True)
        for run in runs:
            started = time.perf_counter()
            try:
                answer = "".join(model.stream_chat(run["question"], system_prompt=run["system"]))
                result = {"answer": answer, "error": None}
            except Exception as error:
                result = {"answer": "", "error": str(error)}
            result.update(
                {
                    "id": run["id"],
                    "elapsed_seconds": time.perf_counter() - started,
                    "generation": model.last_generation.model_dump()
                    if model.last_generation
                    else None,
                }
            )
            (output / (run["id"] + ".json")).write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
            print(json.dumps(result), flush=True)
    finally:
        model.unload()
        library.close()


if __name__ == "__main__":
    main()
