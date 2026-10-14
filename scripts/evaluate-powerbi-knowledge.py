"""Read-only local A/B evaluation. Does not execute generated DAX or mutate reports.

Uses six frozen fixtures: three previous cases and three unseen variations. Prompts
and retrieved contexts are saved before inference. Run --thinking to additionally
test two reasoning-enabled cases. Raw outputs remain in a local-only runtime folder.
"""

from __future__ import annotations

# ruff: noqa: E501 -- frozen evaluation prompts stay verbatim for reproducibility.
import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

import psutil

from ai_powerbi_analyst.knowledge import KnowledgeLibrary
from ai_powerbi_analyst.knowledge.library import REFERENCE_RULES
from ai_powerbi_analyst.powerbi.dax_validation import dax_argument_errors, unsupported_dax_functions

ROOT = Path(__file__).resolve().parents[1]
MODEL = "qwen3.5:4b-q4_K_M"
SYSTEM = "You are a Power BI DAX and modeling expert. Answer in at most80 words plus a formula. Use supplied names exactly. Do not claim execution or changes."
CASES = [
    {
        "id": "weighted",
        "question": "Lines has UnitPrice and Quantity. Write Weighted Price, weighting UnitPrice by Quantity and safely handling zero total quantity. Return DAX only.",
        "expected": "DIVIDE(SUMX(Lines,Lines[UnitPrice]*Lines[Quantity]),SUM(Lines[Quantity]))",
    },
    {
        "id": "lod",
        "question": "Single table Orders with Region,Category,Sales; no related tables. [Total Sales] exists. Write Region Fixed Sales preserving explicit Orders[Region] filters and removing every other Orders filter. Return DAX only.",
        "expected": "CALCULATE([Total Sales],ALLEXCEPT(Orders,Orders[Region]))",
    },
    {
        "id": "fanout",
        "question": "Orders is one row per OrderID with OrderTotal. Lines has multiple rows per OrderID. Is summing a RELATED OrderTotal calculated column in Lines safe? Give a correct alternative that preserves header totals. Do not use DISTINCT on monetary values.",
        "expected": "Reject duplicate header sum; aggregate separate Orders[OrderTotal] at header grain or once perOrderID.",
    },
    {
        "id": "heldout_weighted",
        "question": "Inspections has Reading and SampleCount. Write Weighted Reading with SampleCount as weight, and BLANK for zero total weight. Return DAX only; use these exact names.",
        "expected": "DIVIDE(SUMX(Inspections,Inspections[Reading]*Inspections[SampleCount]),SUM(Inspections[SampleCount]))",
    },
    {
        "id": "heldout_lod",
        "question": "Visits is a single table with Clinic,Practitioner,VisitDate,Charge. [Total Charge] exists. Write Clinic Fixed Charge preserving explicit Clinic filters but ignoring Practitioner,VisitDate and every other Visits column filter. Do not hard-code clinic names. Return DAX only.",
        "expected": "CALCULATE([Total Charge],ALLEXCEPT(Visits,Visits[Clinic]))",
    },
    {
        "id": "heldout_grain",
        "question": "InvoiceHeader has one row per InvoiceID with Freight. InvoiceLines has multiple rows per invoice. A merge repeats Freight per line. How should a model total freight without double-counting, including invoices that have equal freight amounts?",
        "expected": "Keep header fact/uniqueInvoiceID grain and sum there; no SUM of duplicated line freight or DISTINCT monetaryamount.",
    },
]


def post(body):
    with urlopen(
        Request(
            "http://127.0.0.1:11434/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=300,
    ) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument(
        "--resume", type=Path, help="Resume saved design, skipping completed answers"
    )
    args = parser.parse_args()
    output = (
        ROOT / ".runtime" / ("knowledge-evaluation-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    )
    if args.resume:
        output = args.resume.resolve()
        if not output.is_relative_to((ROOT / ".runtime").resolve()):
            raise ValueError("Evaluation output must stay in the local runtime directory")
    output.mkdir(parents=True, exist_ok=bool(args.resume))
    library = KnowledgeLibrary(ROOT / ".runtime/studio-dev-state/knowledge/index.sqlite3")
    runs = []
    for mode in ("baseline", "docs", "hybrid"):
        for case in CASES:
            receipt = library.retrieve(
                case["question"], mode=mode if mode != "baseline" else "docs", max_characters=2400
            )
            runs.append(
                {
                    "mode": mode,
                    "case": case,
                    "context": receipt.context() if mode != "baseline" else "",
                    "thinking": False,
                }
            )
    if args.thinking:
        for case in CASES[:2]:
            runs.append(
                {
                    "mode": "hybrid_thinking",
                    "case": case,
                    "context": library.retrieve(case["question"], max_characters=2400).context(),
                    "thinking": True,
                }
            )
    if args.resume:
        saved = json.loads((output / "design.json").read_text(encoding="utf-8"))
        if saved["model"] != MODEL or saved["system"] != SYSTEM:
            raise ValueError("Frozen evaluation identity changed")
        runs = saved["runs"]
    else:
        (output / "design.json").write_text(
            json.dumps(
                {
                    "system": SYSTEM,
                    "model": MODEL,
                    "corpus_hash": library.corpus_hash,
                    "runs": runs,
                    "context_tokens": 4096,
                    "max_output_tokens": 320,
                    "thinking_output_budget": 768,
                    "temperature": 0,
                    "limitation": "Single attempt; formula review not DAX execution; different condition prompt lengths; thinking gets a larger total generation budget.",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    print(str(output), flush=True)
    try:
        for run in runs:
            name = run["mode"] + "-" + run["case"]["id"]
            if (output / (name + ".json")).exists():
                continue
            if psutil.virtual_memory().available < 2 * 1024**3:
                raise RuntimeError("Insufficient memory headroom")
            started = time.perf_counter()
            system = SYSTEM + (REFERENCE_RULES if run["context"] else "")
            message = run["case"]["question"] + (
                "\nQuoted reference material:\n" + run["context"] if run["context"] else ""
            )
            raw = post(
                {
                    "model": MODEL,
                    "stream": False,
                    "think": run["thinking"],
                    "keep_alive": "15m",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": message},
                    ],
                    "options": {
                        "num_ctx": 4096,
                        "num_predict": 768 if run["thinking"] else 320,
                        "temperature": 0,
                        "seed": 42,
                        "num_thread": 4,
                        "num_gpu": 0,
                    },
                }
            )
            result = {**run, "raw": raw, "wall_seconds": time.perf_counter() - started}
            answer = raw.get("message", {}).get("content", "")
            result["static_argument_errors"] = dax_argument_errors(answer)
            result["static_unknown_functions"] = sorted(unsupported_dax_functions(answer))
            name = run["mode"] + "-" + run["case"]["id"]
            (output / (name + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(f"{name}: {raw.get('done_reason')} {result['wall_seconds']:.1f}s", flush=True)
    finally:
        with urlopen(
            Request(
                "http://127.0.0.1:11434/api/generate",
                data=json.dumps({"model": MODEL, "keep_alive": 0}).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=60,
        ):
            pass
    print("COMPLETE: no report changes; Qwen unloaded.", flush=True)


if __name__ == "__main__":
    main()
