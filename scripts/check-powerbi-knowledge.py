"""Offline retrieval regression/latency check, not an LLM or DAX accuracy benchmark."""

import argparse
import json
import statistics
import time
from pathlib import Path

import psutil

from ai_powerbi_analyst.knowledge import KnowledgeLibrary

CASES = [
    ("Table.NestedJoin default join kind", "table-nestedjoin"),
    ("DIVIDE alternate result constant", "divide-function-dax"),
    ("SUMX weighted average price quantity", "sumx-function-dax"),
    ("CALCULATE context transition", "calculate-function-dax"),
    ("USERELATIONSHIP inactive ship date", "userelationship-function-dax"),
    ("ALLEXCEPT fixed grain Region filters", "allexcept-function-dax"),
    ("star schema dimension fact relationship", "star-schema"),
    ("many to many relationship bridge duplicate keys", "relationships-many-to-many"),
    ("Table.TransformColumnTypes culture locale", "table-transformcolumntypes"),
    ("Date.FromText culture format", "date-fromtext"),
    ("Table.Buffer performance query folding", "table-buffer"),
    ("row level security bidirectional relationships", "rls-guidance"),
    ("report accessibility keyboard alt text", "desktop-accessibility-creating-reports"),
    ("rolling seven days DATESINPERIOD", "datesinperiod-function-dax"),
    ("TREATAS unrelated table filter", "treatas-function-dax"),
    ("SELECTEDVALUE multiple distinct values", "selectedvalue-function-dax"),
    ("List.Distinct equality criteria", "list-distinct"),
    ("Table.ExpandTableColumn nested columns", "table-expandtablecolumn"),
    ("query folding indicators", "step-folding-indicators"),
    ("report drillthrough filters", "desktop-drillthrough"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    process = psutil.Process()
    initial_rss = process.memory_info().rss
    start = time.perf_counter()
    library = KnowledgeLibrary(args.state_root / "knowledge/index.sqlite3")
    startup = time.perf_counter() - start
    rows = []
    latencies = []
    for query, expected in CASES:
        timings = []
        for _ in range(3):
            start = time.perf_counter()
            receipt = library.retrieve(query, max_characters=2400)
            timings.append((time.perf_counter() - start) * 1000)
        latencies.extend(timings)
        rows.append(
            {
                "query": query,
                "expected_url_suffix": expected,
                "hit": any(r.source_url.endswith(expected) for r in receipt.references),
                "milliseconds": timings,
                "receipt": receipt.model_dump(mode="json"),
            }
        )
    report = {
        "kind": "retrieval-regression-not-model-accuracy",
        "status": library.status(),
        "startup_seconds": startup,
        "rss_increase_mib": (process.memory_info().rss - initial_rss) / 1024**2,
        "median_ms": statistics.median(latencies),
        "max_ms": max(latencies),
        "p95_ms": sorted(latencies)[int(len(latencies) * 0.95) - 1],
        "hits": sum(r["hit"] for r in rows),
        "cases": len(rows),
        "results": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    print("Misses:", [r["query"] for r in rows if not r["hit"]])
    library.close()
    if report["hits"] != len(rows) or any(r["receipt"]["context_characters"] > 2400 for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
