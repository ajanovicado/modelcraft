r"""Run a sample of the planner cases against the REAL local model (Qwen 4B, CPU).

    .venv\Scripts\python.exe evals\imported_edit\planner_live.py [--sample 40 | --all]
        [--only ID ...] [--out PATH.json] [--config RUNTIME_CONFIG.json] [--timeout 60]

It goes through the same guarded runtime the Studio service uses (``llm.factory.
create_model_runtime`` with ``config/defaults.yaml``), so the prompt budget, the grammar built
from each call's JSON schema, the timeouts and the model evidence are the production ones. Close
Studio (the sidecar) first: the runtime serves one guarded request at a time and holds a
cross-process lock while it runs. Nothing is edited: every plan is checked by applying its
operations to an in-memory copy of the structure twin, and Power BI Desktop is never started.

The sample (``planner_eval.sample_live``) is what the chat really sends the model: plan cases
the parser leaves unread, and a quarter of requests nothing may be planned for (no_plan,
not_edit, unsupported); cases the parser reads alone only top it up. A sample moves whenever
cases are added or the parser changes, so ``--all`` runs, unsampled, every plan case left to
the model and every negative case (``planner_eval.all_live``). For each case the planner
gets the parts of the message the case lists (``pieces``, else the whole message), exactly as
the chat hands it unreadable text, and one choose call (plus a draft call for a formula it has
to write). Reported, overall and split by whether the parser reads the message alone
(``left_to_the_model`` is the number that matters), and per case:

- tool accuracy: gold steps whose tool the plan chose, in order;
- slot accuracy: gold slot values found in the planned steps (visuals by id, fields by name,
  colors by hue, numbers within 0.05, texts ignoring case, drafted formulas by what they
  compute: ``planner_eval.formula_problems``);
- right plans: every tool and slot right, no step or value nobody asked for (colors the build
  adds included), no broken formula, valid on the twin (a ``model_asks`` case is right only
  when the planner asks: a question or a choice, never a failure);
- unverified: plans right as far as they could be checked that were not applied to the twin
  (``no_apply``) or whose drafted formula the gold gives no spec for, apart from right plans;
- steps and values nobody asked for, summed; wrong formulas; negatives planned anyway;
- failed calls (timeout, schema, busy) and plans with no usable step;
- latency: seconds per request (all its calls) and per call, p50 and p95;
- prompt bytes: the choose call's system + user prompt (the budget is 3000).

The report goes to ``--out`` (default ``evidence/planner-live-<time>.json`` next to this file)
and is rewritten after every case, so a stopped run keeps what it measured. Formula bodies
are hashed in that report; full local runtime diagnostics stay in ignored
``.runtime/planner-live-evidence/<time>``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import planner_eval  # noqa: E402  (a sibling module, found through the path above)
import run  # noqa: E402

from ai_powerbi_analyst.studio import edit_planner as planner  # noqa: E402
from ai_powerbi_analyst.studio.chat_context import prompt_text  # noqa: E402
from ai_powerbi_analyst.studio.imported_chat_edits import route_message  # noqa: E402


class TimedModel:
    """The guarded runtime with every call timed and every failure kept (then re-raised, so the
    planner reacts as it does in the chat)."""

    def __init__(self, model: Any, seconds: int) -> None:
        self.model = model
        self.seconds = seconds
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[Any],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: int | None = None,
        repair_attempts: int | None = None,
    ) -> Any:
        started = time.monotonic()
        row: dict[str, Any] = {
            "kind": "draft" if issubclass(response_model, planner.DraftAnswer) else "choose",
            "prompt_bytes": len(system_prompt.encode()) + len(user_prompt.encode()),
        }
        try:
            answer = self.model.generate(
                system_prompt,
                user_prompt,
                response_model,
                max_output_tokens=max_output_tokens,
                timeout_seconds=min(timeout_seconds or self.seconds, self.seconds),
                repair_attempts=repair_attempts,
            )
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {str(error)[:300]}"
            raise
        finally:
            row["seconds"] = round(time.monotonic() - started, 2)
            self.calls.append(row)
        row["answer"] = answer.model_dump(mode="json")
        return answer


def _percentile(values: list[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)], 2)


def _settings(config: str | None) -> Any:
    from ai_powerbi_analyst.settings import load_settings

    settings = load_settings(REPO / "config" / "defaults.yaml").model
    if config:
        settings = settings.model_copy(update={"runtime_config_path": config})
    return settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sample", type=int, default=40, help="how many plan cases (default 40)")
    parser.add_argument(
        "--all", action="store_true",
        help="no sample: every plan case left to the model and every negative case",
    )
    parser.add_argument("--only", nargs="*", default=None, help="run only these case ids")
    parser.add_argument("--out", type=Path, default=None, help="the JSON report")
    parser.add_argument("--config", default=None, help="another runtime config (optional)")
    parser.add_argument("--timeout", type=int, default=planner.MODEL_SECONDS)
    args = parser.parse_args(argv)

    from ai_powerbi_analyst.llm.factory import create_model_runtime

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or HERE / "evidence" / f"planner-live-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    settings = _settings(args.config)
    # Full runtime answers are local diagnostics; shareable eval evidence is hash-only.
    runtime = create_model_runtime(settings, REPO / ".runtime" / "planner-live-evidence" / stamp)
    health = runtime.health()
    if not getattr(health, "model_available", False):
        print(f"The local model is not available: {getattr(health, 'error', health)}")
        return 2

    cards = planner.available_cards()
    cases = planner_eval.load_planner_cases()
    if args.only:
        chosen = [c for c in cases if c["id"] in set(args.only)]
    elif args.all:
        chosen = planner_eval.all_live(cases, cards)
    else:
        chosen = planner_eval.sample_live(cases, args.sample, cards)
    checker = run.OperationChecker()
    rows: list[dict[str, Any]] = []
    try:
        for number, case in enumerate(chosen, 1):
            rows.append(_run_case(case, runtime, cards, checker, args.timeout))
            row = rows[-1]
            print(
                f"[{number}/{len(chosen)}] {case['id']} ({case['expect']['kind']}): "
                f"{row['outcome']} {[s['op'] for s in row['steps']]} ok={row['ok']} "
                f"{row['seconds']}s",
                flush=True,
            )
            # after every case: a crash or a stopped run keeps what was measured so far
            _write(out, _report(stamp, settings, rows, len(chosen)))
    finally:
        unload = getattr(runtime, "unload", None)
        if callable(unload):
            unload()
    report = _report(stamp, settings, rows, len(chosen))
    _write(out, report)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    print(f"Report: {out}")
    return 0


def _run_case(
    case: dict[str, Any], runtime: Any, cards: Any, checker: Any, timeout: int
) -> dict[str, Any]:
    """One case through the planner with the real model, scored. A plan case scores its tools
    and slots; a request nothing may be planned for (no_plan, not_edit, unsupported) is right
    only when no step comes of it."""
    context = planner_eval.context_of(case)
    routed = route_message(case["message"], context)
    model = TimedModel(runtime, timeout)
    started = time.monotonic()
    made = planner.EditPlanner(context, model, cards=cards)
    outcome = made.plan(planner_eval.pieces_of(case))
    seconds = round(time.monotonic() - started, 2)
    kind = case["expect"]["kind"]
    row: dict[str, Any] = {
        "id": case["id"],
        "message": case["message"],
        "expected": kind,
        "deterministic_alone": routed is not None and not routed.result.unparsed,
        "outcome": outcome.kind,
        "reason": outcome.reason,
        "notes": list(outcome.notes),
        "steps": [
            {"op": s.op, "operation": s.operation, "summary": s.summary, "notes": list(s.notes)}
            for s in outcome.steps
        ],
        "clause": outcome.clause.question if outcome.clause else "",
        "seconds": seconds,
        "calls": model.calls,
    }
    row["unverified"] = []
    if kind == "plan":
        scored = planner_eval.score_outcome(case, outcome, cards, context, checker)
        row["scores"] = scored
        row["ok"] = planner_eval.plan_is_right(scored)
        row["unverified"] = list(scored["unverified"])
        if case.get("model_asks"):
            # only a question or a choice asks; a failure or a plan does not
            row["ok"] = planner_eval.asks_back(outcome)
            row["unverified"] = []
    else:
        row["scores"] = {"planned_steps": len(outcome.steps)}
        row["ok"] = not outcome.steps
    return row


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``right_plans`` counts only verified right plans; a plan that is right as far as it
    could be checked but was not applied to the twin (no_apply) or whose drafted formula the
    gold does not specify is counted under ``unverified`` instead."""
    plans = [r for r in rows if r["expected"] == "plan"]
    negatives = [r for r in rows if r["expected"] != "plan"]
    unverified = [r for r in plans if r.get("unverified")]
    tools = sum(r["scores"]["tools"] for r in plans)
    tools_ok = sum(r["scores"]["tools_ok"] for r in plans)
    slots = sum(r["scores"]["slots"] for r in plans)
    slots_ok = sum(r["scores"]["slots_ok"] for r in plans)
    calls = [c for r in rows for c in r["calls"]]
    request_seconds = [r["seconds"] for r in rows]
    call_seconds = [c["seconds"] for c in calls]
    prompt_bytes = [c["prompt_bytes"] for c in calls if c["kind"] == "choose"]
    return {
        "cases": len(rows),
        "plan_cases": len(plans),
        "gold_steps": tools,
        "tool_accuracy": round(tools_ok / tools, 4) if tools else 0,
        "slot_accuracy": round(slots_ok / slots, 4) if slots else 0,
        "right_plans": sum(1 for r in plans if r["ok"] and not r.get("unverified")),
        "unverified": {
            "plan_cases": len(unverified),
            "right_as_far_as_checked": sum(1 for r in unverified if r["ok"]),
            "ids": [r["id"] for r in unverified],
        },
        "steps_nobody_asked_for": sum(r["scores"]["extra_steps"] for r in plans),
        "values_nobody_asked_for": sum(r["scores"]["unasked"] for r in plans),
        "invalid_operations": sum(r["scores"]["invalid"] for r in plans),
        "wrong_formulas": sum(r["scores"].get("wrong_formulas", 0) for r in plans),
        "no_plan_outcomes": sum(1 for r in plans if r["outcome"] != "plan"),
        "negatives": len(negatives),
        "negatives_planned_anyway": sum(1 for r in negatives if not r["ok"]),
        "failed_calls": sum(1 for c in calls if "error" in c),
        "latency_s": {
            "request_p50": _percentile(request_seconds, 0.5),
            "request_p95": _percentile(request_seconds, 0.95),
            "call_p50": _percentile(call_seconds, 0.5),
            "call_p95": _percentile(call_seconds, 0.95),
        },
        "prompt_bytes": {
            "p50": statistics.median(prompt_bytes) if prompt_bytes else 0,
            "max": max(prompt_bytes) if prompt_bytes else 0,
            "budget": planner.PROMPT_BUDGET,
        },
    }


def _report(stamp: str, settings: Any, rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    """The whole report, the aggregate split by whether the parser reads the message alone
    (those the chat never sends to the model: a model that reads them too says little)."""
    everything = _aggregate(rows)
    return {
        "run_at": stamp,
        "model": getattr(settings, "name", ""),
        "planned_cases": total,
        "done": len(rows),
        **everything,
        # (kept for the older readers of this report)
        "exact_plans": everything["right_plans"],
        "invalid_outputs": {
            "failed_calls": everything["failed_calls"],
            "no_usable_plan": everything["no_plan_outcomes"],
        },
        "left_to_the_model": _aggregate([r for r in rows if not r["deterministic_alone"]]),
        "read_by_the_parser_alone": _aggregate([r for r in rows if r["deterministic_alone"]]),
        "rows": rows,
    }


def _write(out: Path, report: dict[str, Any]) -> None:
    temporary = out.with_suffix(".partial")
    temporary.write_text(
        json.dumps(_shareable(report), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(out)


def _shareable(report: dict[str, Any]) -> dict[str, Any]:
    """Score full operations in memory, but never publish formula bodies or their summaries."""
    bodies: dict[str, dict[str, Any]] = {}

    def digest(value: str) -> dict[str, Any]:
        return {
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "length": len(value),
        }

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key in {"expression", "filter", "format_string_expression"}
                    and isinstance(item, str)
                    and item.strip()
                ):
                    bodies[item] = digest(item)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(report)
    for row in report.get("rows", []):
        for kind in ("dax", "m"):
            body = planner.typed_expression(row.get("message", ""), kind)
            if body:
                collect({"expression": body})
    replacements = sorted(bodies, key=len, reverse=True)

    def redact(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {name: redact(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [redact(item, key) for item in value]
        if isinstance(value, str):
            if key in {"reason", "error", "notes"} and value:
                # A parser error can quote only part of a formula, without any code marker.
                return digest(value)
            if key == "message" and prompt_text(value) != value:
                return {"text": prompt_text(value), **digest(value)}
            if key in {"expression", "filter", "format_string_expression"} and value in bodies:
                return bodies[value]
            for body in replacements:
                if len(body.strip()) < 4:
                    if key == "summary" and ("expression" in value or "formula" in value):
                        return digest(value)
                    continue
                marker = f"[formula sha256:{bodies[body]['sha256']}]"
                value = value.replace(body, marker).replace(" ".join(body.split()), marker)
            return value
        return value

    return cast(dict[str, Any], redact(report))


if __name__ == "__main__":
    raise SystemExit(main())
