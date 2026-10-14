"""Differential test of Studio's DAX engine against Power BI Desktop.

Open the imported report's working copy in Power BI Desktop first (Report tab > Open in
Power BI), then:

    python evals/dax_engine_differential/run.py measures <workspace-id> out.jsonl
    python evals/dax_engine_differential/run.py queries  <workspace-id> out.jsonl patterns.json
    python evals/dax_engine_differential/run.py page     <workspace-id> out.jsonl

``measures`` computes every model measure under nine breakdowns in one query per measure
(total, year, month, channel/employee-like columns found in the model are not assumed:
the breakdowns are the report's own group columns listed below via --group). ``queries``
runs a JSON list of DAX queries. ``page`` compares each page's live-report query. Rows
are matched on their text columns and numbers compared within a relative 1e-7: sums of
many floats differ in the tenth digit between the two engines.

Measured on the Agents Performance report (2026-09-27): the page matched 172/172 rows,
102 measures x 9 breakdowns matched except three RANKX ties (Desktop's float noise ranks
a sole agent second), and 76 of 85 query patterns matched with the other 9 failing in both.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from ai_powerbi_analyst.studio.dax_engine import run_query  # noqa: E402
from ai_powerbi_analyst.studio.live_report import LiveReportRequest, compile_page  # noqa: E402
from ai_powerbi_analyst.studio.report_engine import report_model  # noqa: E402
from ai_powerbi_analyst.studio.service import StudioService  # noqa: E402


def normalize(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    text = str(value)
    if text.endswith(" 12:00:00 AM") and "/" in text:
        return text.replace(" 12:00:00 AM", "")
    if hasattr(value, "month") and hasattr(value, "year"):
        return f"{value.month}/{value.day}/{value.year}"  # type: ignore[union-attr]
    try:
        number = float(text)
    except ValueError:
        return text
    return text if number != number or abs(number) == float("inf") else number


def rows_of(columns: list[str], rows: list) -> list[tuple]:
    return [
        tuple(normalize(r.get(c) if isinstance(r, dict) else r[i]) for i, c in enumerate(columns))
        for r in rows
    ]


def same(desktop: list[tuple], local: list[tuple]) -> bool:
    if len(desktop) != len(local):
        return False

    def grouped(rows: list[tuple]) -> dict:
        out = defaultdict(list)
        for row in rows:
            label = tuple(v for v in row if not isinstance(v, float))
            out[label].append(sorted(v for v in row if isinstance(v, float)))
        return {k: sorted(v) for k, v in out.items()}

    a, b = grouped(desktop), grouped(local)
    if a.keys() != b.keys():
        return False
    for key in a:
        for x, y in zip(a[key], b[key], strict=True):
            if len(x) != len(y) or any(
                abs(p - q) > max(1e-7 * max(abs(p), abs(q)), 1e-6)
                for p, q in zip(x, y, strict=True)
            ):
                return False
    return True


class Harness:
    def __init__(self, workspace_id: str, state: Path) -> None:
        self.service = StudioService(REPO, state)
        self.workspace = self.service.workspace(workspace_id)
        self.report = self.service.imported_report(workspace_id)
        self.model = report_model(
            self.report, self.service.snapshot(self.workspace.active_snapshot_id or "")
        )
        self.desktop = self.service.native_report_engine()

    def compare(self, query: str) -> dict:
        out: dict = {"query": query}
        for attempt in range(40):
            try:
                payload = self.desktop.query(self.report, query, 5000)
                out["desktop"] = rows_of(payload["columns"], payload["rows"])
                break
            except ValueError as error:
                if "Another Power BI preview" in str(error) and attempt < 39:
                    time.sleep(0.5)
                    continue
                out["desktop_error"] = str(error)[:400]
                break
        try:
            result = run_query(self.model, query, time_budget=180)
            out["local"] = rows_of(result.columns, result.rows)
        except Exception as error:  # noqa: BLE001 - every engine failure is a result here
            out["local_error"] = f"{type(error).__name__}: {str(error)[:400]}"
        if "desktop_error" in out or "local_error" in out:
            both = "desktop_error" in out and "local_error" in out
            out["verdict"] = "both_error" if both else "error_mismatch"
        else:
            out["verdict"] = "match" if same(out["desktop"], out["local"]) else "MISMATCH"
        return out


def measure_queries(harness: Harness, groups: list[str]) -> list[tuple[str, str]]:
    parts = [("total", 'ROW("v", {m})', "BLANK()")]
    for group in groups:
        parts.append((group, f'SUMMARIZECOLUMNS({group}, "v", {{m}})', f'{group} & ""'))
    items = []
    for measure in harness.model.measures.values():
        ref = "[" + measure.name.replace("]", "]]") + "]"
        pieces = [
            f'SELECTCOLUMNS({table.replace("{m}", ref)}, "t", "{name}", "k", {key}, "v", [v])'
            for name, table, key in parts
        ]
        items.append((measure.name, "EVALUATE UNION(" + ", ".join(pieces) + ")"))
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["measures", "queries", "page"])
    parser.add_argument("workspace")
    parser.add_argument("out")
    parser.add_argument("queries", nargs="?")
    parser.add_argument("--state", default=str(REPO / ".runtime" / "studio-dev-state"))
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        help="a group column such as 'Date'[Year]; repeat for each breakdown",
    )
    args = parser.parse_args()
    harness = Harness(args.workspace, Path(args.state))
    if args.mode == "measures":
        items = measure_queries(harness, args.group)
    elif args.mode == "queries":
        items = [(f"Q{i:03d}", q) for i, q in enumerate(json.loads(Path(args.queries).read_text()))]
    else:
        items = []
        for page in harness.report.pages:
            _, query = compile_page(harness.report, page.id, LiveReportRequest())
            if query:
                items.append((page.title, query))
    counts: dict[str, int] = defaultdict(int)
    with open(args.out, "w", encoding="utf-8") as handle:
        for label, query in items:
            result = harness.compare(query)
            result["label"] = label
            counts[result["verdict"]] += 1
            handle.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
            print(f"{result['verdict']:15s} {label}", flush=True)
    print("SUMMARY", dict(counts))


if __name__ == "__main__":
    main()
