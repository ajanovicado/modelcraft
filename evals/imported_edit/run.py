"""Evaluate the chat edit parser on the cases in cases.jsonl (no model, no Power BI Desktop).

    .venv/Scripts/python.exe evals/imported_edit/run.py [--cases PATH] [--out REPORT.json]
                                                        [--only ID ...] [--verbose]
    .venv/Scripts/python.exe evals/imported_edit/run.py --planner stub|noisy|coverage [--out ...]

``--planner`` evaluates the planner cases (planner_cases.jsonl, run through planner_eval.py):
``stub`` gives the planner a model that answers with each case's gold choice, which tests the
tool retrieval and the slot plumbing (gates: retrieval recall@6 >= 95%, every gold choice
becomes a valid operation that applies to the twin, no step or value nobody asked for);
``noisy`` gives it a model that also makes something up in each answer (a value the request
does not give, a step more, another visual or page than the one named: gate, none of it is
kept); ``coverage`` measures how many of those requests the deterministic tier reads with no
model (gates: no edit on a request that must not edit, and no wrong operation, on/off or
number on a request it reads alone). Without ``--planner`` the parser gates below apply.

Every case is one message on one context (contexts.py: the committed structure twin of the
Agents Performance dashboard, or the 5-page synthetic report) with the clauses the parser
must return. The runner plays the part of the chat service: it parses the message and,
for a clause the deterministic tier cannot read, would call the 4B model. Here the model is
a stub that raises, so a case that needs it is counted, not answered.

Ship gates (design review P30), each a hard failure with exit code 1:
- coverage: at least 90% of the cases are handled with no model call;
- wrong targets: 0. An edit that lands on a visual the case did not expect, or any edit on a
  case that must not edit;
- unsupported accuracy: 100%. Every out-of-scope request is UNSUPPORTED and cites the right
  capability of CAPABILITY_MATRIX.
Also gated: every operation must validate against report_edits, and, when the twin is
present, apply to it without an error; and every case must come out as written (a case
that is not met is a change of behavior somebody has to look at). Latency of the
deterministic tier is recorded.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import contexts  # noqa: E402  (a sibling module, found through the path above)

from ai_powerbi_analyst.studio.edit_context import EditContext, hue_family  # noqa: E402
from ai_powerbi_analyst.studio.edit_intents import parse_edit_message  # noqa: E402
from ai_powerbi_analyst.studio.edit_types import Clause, ParseKind, ParseResult  # noqa: E402

COVERAGE_GATE = 0.90
COLOR_KEYS = {"color", "font_color", "background", "border_color"}
# operation settings that hold a visual's id; a case may write the first 8 characters of one
ID_KEYS = ("visual", "a", "b", "relative_to")


class ModelCalled(AssertionError):
    """The deterministic tier handed a clause to the model."""


class ForbiddenModel:
    """The chat model, as the service would call it for a clause the parser could not read."""

    def stream_chat(self, message: str, *, system_prompt: str, history: Any = None) -> Any:
        raise ModelCalled(message)


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """The cases of one file, or of every ``cases*.jsonl`` next to this script: ``cases.jsonl``
    holds the first families (visuals, pages, measures, undo, styling refusals) and
    ``cases_ops.jsonl`` the operations added later (layout, text boxes, shapes, filters)."""
    files = [path] if path is not None else sorted(HERE.glob("cases*.jsonl"))
    found: list[dict[str, Any]] = []
    for file in files:
        lines = file.read_text(encoding="utf-8").splitlines()
        found += [json.loads(line) for line in lines if line]
    ids = [case["id"] for case in found]
    assert len(ids) == len(set(ids)), "two cases share an id"
    return found


def route(message: str, context: EditContext, model: ForbiddenModel) -> tuple[ParseResult, bool]:
    """(the parse, whether the model was needed) the way the service routes a message."""
    result = parse_edit_message(message, context)
    if result.unparsed:
        try:
            model.stream_chat(" ".join(result.unparsed), system_prompt="")
        except ModelCalled:
            return result, True
    return result, False


# --- matching ----------------------------------------------------------------------------------


def _same(key: str, expected: Any, actual: Any) -> bool:
    if key.endswith("~"):
        base = key[:-1]
        if base == "data_colors":
            return (
                isinstance(actual, list)
                and [hue_family(c) for c in actual[: len(expected)]] == expected
            )
        return isinstance(actual, str) and hue_family(actual) == expected
    if key in ID_KEYS and isinstance(actual, str):
        return actual.startswith(str(expected))
    if key == "visuals" and isinstance(actual, list) and isinstance(expected, list):
        pairs = zip(expected, actual, strict=False)
        return len(actual) == len(expected) and all(
            isinstance(a, str) and a.startswith(str(e)) for e, a in pairs
        )
    if isinstance(expected, dict):
        return isinstance(actual, dict) and _subset(expected, actual)
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_same("", e, a) for e, a in zip(expected, actual, strict=True))
        )
    if isinstance(expected, float) and isinstance(actual, int | float):
        return math.isclose(expected, float(actual), abs_tol=0.011)
    return bool(expected == actual)


def _subset(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key, value in expected.items():
        source = key[:-1] if key.endswith("~") else key
        if source not in actual or not _same(key, value, actual[source]):
            return False
    return True


def op_visual_ids(made: dict[str, Any]) -> set[str]:
    """The visuals an operation touches, as the first 8 characters of their ids: the one it
    changes and, for the layout operations, the ones it lines up, swaps or goes next to."""
    ids = {str(made[key])[:8] for key in ID_KEYS if key in made}
    ids |= {str(v)[:8] for v in made.get("visuals", [])}
    return ids


def _visual_ids(clause: Clause) -> set[str]:
    ids = {v[:8] for t in clause.targets for v in t.visual_ids}
    for op in clause.ops:
        ids |= op_visual_ids(op)
    return ids


def compare(case: dict[str, Any], result: ParseResult, called_model: bool) -> dict[str, Any]:
    """Problems found in one case: mismatches, wrong targets, false edits, unsupported misses."""
    problems: list[str] = []
    wrong: list[str] = []
    expected = case["clauses"]
    if bool(case.get("needs_model")) != called_model:
        problems.append(f"needs_model expected {bool(case.get('needs_model'))}, got {called_model}")
    if "remainder" in case and case["remainder"] != result.remainder:
        problems.append(f"remainder {result.remainder!r} != {case['remainder']!r}")
    if len(result.clauses) != len(expected):
        problems.append(f"{len(result.clauses)} clauses, expected {len(expected)}")
    allowed: set[str] = set()
    for item in expected:
        allowed |= set(item.get("targets", []))
        for op in item.get("ops", []):
            allowed |= op_visual_ids(op)
    for index, actual in enumerate(result.clauses):
        want = expected[index] if index < len(expected) else None
        edits = actual.kind in {ParseKind.APPLY, ParseKind.MODEL_EDIT} and bool(actual.ops)
        if want is None:
            if edits:
                wrong.append(f"clause {index}: an edit nobody asked for")
            continue
        if actual.kind.value != want["kind"]:
            problems.append(f"clause {index}: {actual.kind.value}, expected {want['kind']}")
            if edits:
                wrong.append(f"clause {index}: {actual.kind.value} where {want['kind']} was due")
            continue
        if "ops" in want:
            if len(actual.ops) != len(want["ops"]):
                problems.append(
                    f"clause {index}: {len(actual.ops)} ops, expected {len(want['ops'])}"
                )
            for n, (exp_op, got_op) in enumerate(zip(want["ops"], actual.ops, strict=False)):
                if not _subset(exp_op, got_op):
                    problems.append(f"clause {index} op {n}: {got_op} != {exp_op}")
        if actual.kind is ParseKind.APPLY:
            got = _visual_ids(actual)
            want_ids = set(want.get("targets", []))
            for op in want.get("ops", []):
                want_ids |= op_visual_ids(op)
            if got != want_ids and not got <= allowed:
                wrong.append(f"clause {index}: targets {sorted(got)} not in {sorted(want_ids)}")
            elif got != want_ids:
                problems.append(
                    f"clause {index}: targets {sorted(got)}, expected {sorted(want_ids)}"
                )
        if actual.kind is ParseKind.CLARIFY:
            _check_clarify(index, want, actual, problems)
        if actual.kind is ParseKind.UNSUPPORTED and want.get("capability") not in {
            None,
            actual.capability,
        }:
            problems.append(
                f"clause {index}: capability {actual.capability}, expected {want['capability']}"
            )
        if actual.kind is ParseKind.MODEL_EDIT and bool(want.get("what_if")) != (
            actual.what_if is not None
        ):
            problems.append(f"clause {index}: what_if presence differs")
    oos = "oos" in case.get("tags", [])
    if oos and not all(c.kind is ParseKind.UNSUPPORTED for c in result.clauses):
        problems.append("out-of-scope request not answered with UNSUPPORTED")
    return {"problems": problems, "wrong": wrong, "oos": oos}


def _check_clarify(index: int, want: dict[str, Any], actual: Clause, problems: list[str]) -> None:
    if len(actual.choices) < int(want.get("choices_min", 0)):
        problems.append(f"clause {index}: {len(actual.choices)} choices")
    if "candidates" in want:
        got = sorted(
            c.fill["visual"][:8]
            for c in actual.choices
            if "visual" in c.fill and "," not in c.fill["visual"]
        )
        if got != want["candidates"]:
            problems.append(f"clause {index}: candidates {got}, expected {want['candidates']}")
    if "parts" in want:
        got_parts = [c.fill["part"] for c in actual.choices if "part" in c.fill]
        if got_parts != want["parts"]:
            problems.append(f"clause {index}: parts {got_parts}, expected {want['parts']}")


# --- operations against report_edits -------------------------------------------------------------


MAX_ANSWERS = 3  # how many questions in a row one batch may ask before it is a finding


class OperationChecker:
    """Validates every emitted operation and applies it to the twin (when the twin is there).

    The edit layer may stop a batch to ask (``NeedsChoice``, ``NeedsConfirmation``): that is a
    valid outcome, not an error, as long as it asks a real question and every answer it offers
    applies cleanly once the answer is filled in (``ops_with_answer``, the way the chat does it).
    Each question is counted in ``asked``."""

    def __init__(self) -> None:
        from pydantic import TypeAdapter

        from ai_powerbi_analyst.studio.report_edits import ReportOperation

        self.adapter: Any = TypeAdapter(ReportOperation)
        self.report = contexts.agents_report()
        self.available = contexts.TWIN.exists()
        self.asked: dict[str, int] = {}

    def check(self, clause: Clause, context_id: str) -> list[str]:
        """Problems with the operations a clause carries, or that any of its choices carries."""
        problems = self.check_ops(clause.ops, context_id)
        for choice in clause.choices:
            problems += self.check_ops(choice.ops, context_id)
        return problems

    def check_ops(self, made: tuple[dict[str, Any], ...], context_id: str) -> list[str]:
        if not made:
            return []
        try:
            for op in made:
                self.adapter.validate_python(op)
        except Exception as error:  # noqa: BLE001 - any validation failure is a finding
            return [f"invalid operation: {str(error)[:160]}"]
        # Only the twin's own context can be applied to the twin.
        if not self.available or context_id != "agents":
            return []
        return self.apply(self.with_filters_to_clear(list(made)), 0)

    @staticmethod
    def with_filters_to_clear(made: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The batch with a filter set in front of each ``clear_filter``: the twin has no
        filters, and a clear with nothing to clear is rightly refused, so the check gives it
        the filter the person is taken to have (on the field they named, else on Year)."""
        prepared: list[dict[str, Any]] = []
        for op in made:
            if op.get("op") == "clear_filter":
                field = op.get("field") or {"table": "Date", "name": "Year"}
                spec = {"kind": "categorical", "field": field, "values": [2019]}
                if field["name"] != "Year":
                    spec["values"] = ["Online"]
                scope, target = op["scope"], op.get("target")
                if scope == "report":
                    prepared.append({"op": "set_report_filter", "filter": spec})
                elif scope == "page":
                    prepared.append({"op": "set_page_filter", "page": target, "filter": spec})
                else:
                    prepared.append({"op": "set_visual_filter", "visual": target, "filter": spec})
            prepared.append(op)
        return prepared

    def apply(self, made: list[dict[str, Any]], depth: int) -> list[str]:
        from ai_powerbi_analyst.studio.edit_ops import ops_with_answer
        from ai_powerbi_analyst.studio.pbir_document import DocumentError, PbirDocument
        from ai_powerbi_analyst.studio.pbix_package import PbixPackage
        from ai_powerbi_analyst.studio.report_edits import (
            ModelInfo,
            NeedsChoice,
            NeedsConfirmation,
            apply_operations,
            parse_operations,
        )

        document = PbirDocument(PbixPackage(contexts.TWIN))
        model = ModelInfo.from_report(self.report.tables, self.report.measures)
        try:
            apply_operations(document, parse_operations(made), model)
        except (NeedsChoice, NeedsConfirmation) as ask:
            if depth >= MAX_ANSWERS:
                return [f"the operations keep asking after {MAX_ANSWERS} answers: {ask}"]
            if isinstance(ask, NeedsChoice):
                self.asked["choice"] = self.asked.get("choice", 0) + 1
                answers: list[str | dict[str, Any]] = [dict(o.fill) for o in ask.options]
                if not str(ask.question).strip() or len(answers) < 2:
                    return [f"asks without a question or without options: {ask!r}"]
            else:
                self.asked[ask.kind] = self.asked.get(ask.kind, 0) + 1
                # yes; a conditional format can also be kept and the rest changed
                answers = [ask.kind]
                if ask.kind == "replace_conditional":
                    answers.append("keep_conditional")
            problems: list[str] = []
            for answer in answers:
                try:
                    answered = ops_with_answer(made, answer, ask.step)
                except ValueError as error:
                    if answer == "keep_conditional":
                        continue  # the other way out, which a conditional-format step lacks
                    problems.append(f"the answer {answer!r} cannot be filled in: {error}")
                    continue
                problems += self.apply(answered, depth + 1)
            return problems
        except DocumentError as error:
            return [f"the operations do not apply to the report: {error}"]
        return []


# --- the run -------------------------------------------------------------------------------------


def now_supported(case: dict[str, Any]) -> str | None:
    """The capability a refusal case asks for when a tool family has since made it supported:
    the request is then the tool cards' and the planner's (planner_cases.jsonl scores it), not
    a refusal, so this file's expectation no longer applies."""
    from ai_powerbi_analyst.studio.edit_capabilities import CAPABILITY_MATRIX

    for clause in case["clauses"]:
        key = clause.get("capability")
        refused = clause["kind"] == "unsupported" and key in CAPABILITY_MATRIX
        if refused and CAPABILITY_MATRIX[key].status == "supported":
            return str(key)
    return None


def evaluate(cases: list[dict[str, Any]], only: set[str] | None = None) -> dict[str, Any]:
    model = ForbiddenModel()
    checker = OperationChecker()
    timings: list[float] = []
    findings: list[dict[str, Any]] = []
    covered = wrong_targets = false_edits = invalid = 0
    oos_total = oos_ok = 0
    chosen = [c for c in cases if not only or c["id"] in only]
    moved = {c["id"]: key for c in chosen if (key := now_supported(c)) is not None}
    selected = [c for c in chosen if c["id"] not in moved]
    for case in selected:
        context = contexts.context_for(case["context"], dict(case.get("options", {})))
        started = time.perf_counter()
        result, called_model = route(case["message"], context, model)
        timings.append((time.perf_counter() - started) * 1000)
        covered += 0 if called_model else 1
        found = compare(case, result, called_model)
        op_problems = [
            p for clause in result.clauses for p in checker.check(clause, case["context"])
        ]
        invalid += len(op_problems)
        found["problems"] += op_problems
        if found["wrong"]:
            wrong_targets += 1
            if "negative" in case.get("tags", []):
                false_edits += 1
        if found["oos"]:
            oos_total += 1
            oos_ok += (
                0 if any("out-of-scope" in p or "capability" in p for p in found["problems"]) else 1
            )
        if found["problems"] or found["wrong"]:
            findings.append({"id": case["id"], "message": case["message"], **found})
    timings.sort()
    total = len(selected)
    report: dict[str, Any] = {
        "cases": total,
        "coverage": covered / total if total else 0.0,
        "handled_without_model": covered,
        "wrong_target_cases": wrong_targets,
        "false_edits": false_edits,
        "unsupported_cases": oos_total,
        "unsupported_accuracy": (oos_ok / oos_total) if oos_total else 1.0,
        "invalid_operations": invalid,
        "mismatches": len([f for f in findings if f["problems"]]),
        "moved_to_planner": moved,
        "latency_ms": {
            "p50": round(statistics.median(timings), 3) if timings else 0.0,
            "p95": round(timings[min(len(timings) - 1, math.ceil(0.95 * len(timings)) - 1)], 3)
            if timings
            else 0.0,
            "max": round(timings[-1], 3) if timings else 0.0,
        },
        "findings": findings,
    }
    report["gates"] = {
        "coverage>=90%": report["coverage"] >= COVERAGE_GATE,
        "wrong_targets==0": wrong_targets == 0,
        "unsupported==100%": report["unsupported_accuracy"] == 1.0,
        "operations_valid": invalid == 0,
        "cases_match": report["mismatches"] == 0,
    }
    report["passed"] = all(report["gates"].values())
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cases", type=Path, default=None, help="one case file (default: all)")
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--only", nargs="*", default=None, help="run only these case ids")
    parser.add_argument("--verbose", action="store_true", help="print every finding")
    parser.add_argument(
        "--planner",
        choices=("stub", "noisy", "coverage"),
        default=None,
        help="evaluate the planner cases instead (planner_cases.jsonl)",
    )
    args = parser.parse_args(argv)
    only = set(args.only) if args.only else None
    if args.planner:
        import planner_eval  # noqa: PLC0415 - a sibling module, only for the planner modes

        cases = planner_eval.load_planner_cases(args.cases)
        if args.planner == "coverage":
            report = planner_eval.evaluate_coverage(cases, only)
        else:
            report = planner_eval.evaluate_stub(cases, only, noisy=args.planner == "noisy")
    else:
        report = evaluate(load_cases(args.cases), only)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    summary = {k: v for k, v in report.items() if k != "findings"}
    print(json.dumps(summary, indent=2))
    if args.verbose or not report["passed"]:
        for finding in report["findings"]:
            print(f"- {finding['id']} {finding['message']!r}", file=sys.stderr)
            for line in [*finding["wrong"], *finding["problems"]]:
                print(f"    {line}", file=sys.stderr)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
