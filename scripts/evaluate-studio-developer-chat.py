"""Real guarded Studio chat smoke checks; semantic review is always required.

Run only the selected cases on a quiet host, for example:
  python scripts/evaluate-studio-developer-chat.py --output-dir .runtime/chat-evals \
      --case decorators --case decorators-example

Every run creates fresh isolated state below the explicit output directory. It never
opens saved workspaces, executes generated code, reseals a runtime, or approves a release.
The prepared prompts and responses are saved alongside the runtime's own evidence,
which records the actual messages after runtime context budgeting.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import time
import uuid
from collections.abc import Generator, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from unittest.mock import patch

from ai_powerbi_analyst.studio.contracts import ChatRequest, CreateWorkspaceRequest, LocalWorkspace
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


@dataclass(frozen=True)
class Case:
    id: str
    prompt: str
    review: str
    follows: str | None = None
    python_syntax: bool = False


CASES = (
    Case(
        "safe-divide",
        "Create a concise Python function called safe_divide(a, b) that returns None when "
        "b is zero and otherwise returns a / b. Include one short usage example in a "
        "fenced Python code block. Do not execute it.",
        "Review zero handling, function behavior, and the usefulness of the example.",
        python_syntax=True,
    ),
    Case(
        "decorators",
        "Explain Python decorators and when to use them. Keep it concise.",
        "Review correctness and concision; watch for unrelated BI advice.",
    ),
    Case(
        "decorators-example",
        "Show an example.",
        "Review whether the example follows the preceding Python decorators explanation.",
        follows="decorators",
        python_syntax=True,
    ),
    Case(
        "sql-joins",
        "Explain SQL INNER JOIN and LEFT JOIN using a tiny example. Keep it concise.",
        "Review SQL row semantics and the example; watch for unrelated BI filter rules.",
    ),
    Case(
        "python-dict",
        'Show a small Python dict example reading payload["customer_id"] and using .get() '
        "for an optional key. Use a fenced Python block with placeholder data; do not execute it.",
        "Review indexing versus optional lookup; placeholders are not workspace fields.",
        python_syntax=True,
    ),
    Case(
        "git-staging",
        "Explain Git staging versus committing and give a small command example. Keep it concise.",
        "Review the distinction and commands; no commands should be claimed as executed.",
    ),
)
CASE_BY_ID = {case.id: case for case in CASES}


def select_cases(selected: Sequence[str]) -> list[Case]:
    """Preserve selection order, adding a follow-up's prerequisite exactly once."""
    result: list[Case] = []
    seen: set[str] = set()

    def add(case_id: str) -> None:
        if case_id not in CASE_BY_ID:
            raise ValueError(f"Unknown case: {case_id}")
        if case_id in seen:
            return
        case = CASE_BY_ID[case_id]
        if case.follows:
            add(case.follows)
        seen.add(case_id)
        result.append(case)

    for case_id in selected or tuple(CASE_BY_ID):
        add(case_id)
    return result


def save_json(path: Path, value: object) -> None:
    """Append evidence without overwriting an earlier run or attempt."""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def create_run_directory(output_dir: Path) -> Path:
    output = output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    run = output / (
        "studio-developer-chat-"
        + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-")
        + uuid.uuid4().hex[:8]
    )
    run.mkdir(exist_ok=False)
    if run.resolve().parent != output:
        raise ValueError("Evaluation state must remain below the explicit output directory")
    return run


def python_syntax_check(response: str) -> dict[str, object]:
    """Parse explicitly labeled Python blocks only; never execute model output."""
    blocks = re.findall(r"(?ms)^```(?:python|py)[ \t]*\r?\n(.*?)^```[ \t]*$", response)
    if not blocks:
        return {"status": "not_checked", "reason": "No explicitly labeled Python code block"}
    errors: list[str] = []
    for index, code in enumerate(blocks, 1):
        try:
            ast.parse(code, filename=f"unexecuted-model-block-{index}.py")
        except SyntaxError as error:
            errors.append(f"Block {index}, line {error.lineno}: {error.msg}")
    return {"status": "failed" if errors else "passed", "blocks": len(blocks), "errors": errors}


def all_events(service: StudioService, workspace_id: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    after = 0
    while page := service.events(workspace_id, after=after):
        rows.extend(page)
        sequence = page[-1]["sequence"]
        if not isinstance(sequence, int) or sequence <= after:
            raise RuntimeError("Audit pagination did not advance")
        after = sequence
    return rows


def evaluate_case(
    service: StudioService, workspace: LocalWorkspace, case: Case, run: Path
) -> dict[str, object]:
    target = run / case.id
    target.mkdir(exist_ok=False)
    before = service.workspace(workspace.id)
    before_events = all_events(service, workspace.id)
    evidence_root = run / "state" / "model-evidence"
    old_evidence = set(evidence_root.rglob("*"))
    request = ChatRequest(
        workspace_id=workspace.id,
        message=case.prompt,
        idempotency_key=f"developer-smoke-{uuid.uuid4().hex}",
        expected_base_hash=before.base_hash,
    )
    save_json(target / "request.json", request.model_dump(mode="json"))
    original_stream = service.model.stream_chat
    prompts: list[str] = []

    def record_stream(
        message: str,
        *,
        system_prompt: str,
        history: list[dict[str, str]] | None = None,
        worked_examples: bool = True,
        max_output_tokens: int | None = None,
    ) -> Iterator[str]:
        name = f"prepared-prompt-{len(prompts) + 1}.json"
        save_json(
            target / name,
            {
                "message": message,
                "system_prompt": system_prompt,
                "history": history or [],
                "worked_examples": worked_examples,
                "max_output_tokens": max_output_tokens,
                "scope": "Service inputs; actual budgeted messages are in runtime evidence",
            },
        )
        prompts.append(name)
        yield from original_stream(
            message,
            system_prompt=system_prompt,
            history=history,
            worked_examples=worked_examples,
            max_output_tokens=max_output_tokens,
        )

    events: list[dict[str, object]] = []
    response: list[str] = []
    error: str | None = None
    status = "interrupted"
    started = time.monotonic()
    cancel = Event()
    stream = service.chat_events(request, cancel)
    try:
        # This instrumentation delegates unchanged to the existing guarded runtime.
        with patch.object(service.model, "stream_chat", record_stream):
            for event in stream:
                events.append(event)
                payload = event.get("data")
                if event.get("event") == "token" and isinstance(payload, dict):
                    text = payload.get("text")
                    if isinstance(text, str):
                        response.append(text)
            status = "completed"
    except Exception as reason:  # noqa: BLE001 - retain failures as evaluation evidence
        error = f"{type(reason).__name__}: {reason}"
        status = "failed"
    finally:
        cancel.set()
        try:
            if isinstance(stream, Generator):
                stream.close()
        finally:
            # A Ctrl+C or close failure also leaves the stream evidence readable.
            text = "".join(response)
            (target / "response.md").write_text(text, encoding="utf-8")
            save_json(target / "stream-events.json", events)
            save_json(
                target / "stream-result.json",
                {"status": status, "error": error, "seconds": round(time.monotonic() - started, 3)},
            )

    after = service.workspace(workspace.id)
    audit = all_events(service, workspace.id)[len(before_events) :]
    mutation_events = [
        event
        for event in audit
        if not str(event["type"]).startswith(
            ("chat.", "rag.", "model.", "assistant.", "knowledge.retrieved")
        )
    ]
    mutation_stream = [
        event
        for event in events
        if event.get("event") in {"draft", "report_action"}
        or str(event.get("event", "")).startswith("edit_")
    ]
    syntax = python_syntax_check(text) if case.python_syntax else {"status": "not_requested"}
    checks = {
        "stream_finished": status == "completed" and error is None,
        "nonempty_response": bool(text.strip()),
        "model_invoked": bool(prompts),
        "workspace_unchanged": before.model_dump() == after.model_dump(),
        "no_report_mutation_events": not mutation_events and not mutation_stream,
        "requested_python_syntax_passed": not case.python_syntax or syntax["status"] == "passed",
    }
    metrics = service.model.last_generation
    result: dict[str, object] = {
        "case": case.id,
        "workspace_id": workspace.id,
        "prompt": case.prompt,
        "status": status,
        "error": error,
        "seconds": round(time.monotonic() - started, 3),
        "checks": checks,
        "automatic_checks_passed": all(checks.values()),
        "python_ast_syntax": syntax,
        "semantic_review_required": True,
        "review": case.review,
        "check_scope": "Stream finished means the API iterator ended; inspect native generation "
        "finish status and review meaning, behavior, relevance, and concision.",
        "workspace_hash_before": before.base_hash,
        "workspace_hash_after": after.base_hash,
        "report_hash_before": None,
        "report_hash_after": None,
        "report_scope": "Empty isolated workspace; no report or draft was loaded",
        "draft_events": [event for event in audit if str(event["type"]).startswith("draft.")],
        "mutation_events": mutation_events,
        "mutation_stream_events": mutation_stream,
        "prepared_prompt_files": prompts,
        "runtime_evidence_files": sorted(
            str(path.relative_to(run))
            for path in set(evidence_root.rglob("*")) - old_evidence
            if path.is_file()
        ),
        "generation_metrics": metrics.model_dump(mode="json") if metrics else None,
    }
    save_json(target / "audit-events.json", audit)
    save_json(target / "result.json", result)
    return result


def run_cases(service: StudioService, cases: Sequence[Case], run: Path) -> list[dict[str, object]]:
    workspaces: dict[str, LocalWorkspace] = {}
    results: list[dict[str, object]] = []
    for case in cases:
        workspace = (
            workspaces[case.follows]
            if case.follows
            else service.create_workspace(
                CreateWorkspaceRequest(
                    name=f"Isolated developer smoke: {case.id}",
                    idempotency_key=f"developer-smoke-create-{uuid.uuid4().hex}",
                    expected_base_hash=EMPTY_HASH,
                )
            )
        )
        workspaces[case.id] = workspace
        print(f"Running {case.id}", flush=True)
        result = evaluate_case(service, workspace, case, run)
        results.append(result)
        print(f"{case.id}: {result['status']}; semantic review required", flush=True)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Explicit parent for fresh isolated runs")
    parser.add_argument(
        "--case",
        action="append",
        choices=tuple(CASE_BY_ID),
        default=[],
        help="Repeat to select cases; defaults to all; follow-ups include prerequisites",
    )
    parser.add_argument(
        "--list-cases", action="store_true", help="List cases without loading a model"
    )
    args = parser.parse_args(argv)
    if args.list_cases:
        for case in CASES:
            print(
                f"{case.id}: {case.prompt}" + (f" [after {case.follows}]" if case.follows else "")
            )
        return 0
    if args.output_dir is None:
        parser.error("--output-dir is required; saved workspace state is never reused")
    cases = select_cases(args.case)
    run = create_run_directory(args.output_dir)
    repo = Path(__file__).resolve().parents[1]
    sources = [
        "config/defaults.yaml",
        "scripts/evaluate-studio-developer-chat.py",
        "src/ai_powerbi_analyst/studio/service.py",
        "src/ai_powerbi_analyst/studio/conversation.py",
    ]
    save_json(
        run / "design.json",
        {
            "created_at": datetime.now(UTC).isoformat(),
            "requested_cases": args.case,
            "resolved_cases": [case.id for case in cases],
            "state_root": str(run / "state"),
            "source_sha256": {
                name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in sources
            },
            "semantic_review_required": True,
            "release_approval": False,
            "limitations": "Syntax is parsed, never executed. No semantic scoring or qualification."
            " Empty isolated data context; imported-model grounding is not tested.",
        },
    )
    print(str(run), flush=True)
    service: StudioService | None = None
    results: list[dict[str, object]] = []
    failure: str | None = None
    cleanup_errors: list[str] = []
    try:
        service = StudioService(repo, run / "state")
        if not service.model.settings.runtime_config_path:
            raise RuntimeError("This smoke runner requires the configured guarded runtime")
        health = service.model.health()
        save_json(run / "health.json", health.model_dump(mode="json"))
        if not health.model_available:
            raise RuntimeError(health.error or "The guarded model is unavailable")
        results = run_cases(service, cases, run)
    except Exception as error:  # noqa: BLE001 - failures remain reviewable without resealing
        failure = f"{type(error).__name__}: {error}"
    finally:
        if service is not None:
            for label, close in (
                ("model unload", service.model.unload),
                ("knowledge close", service.knowledge.close),
            ):
                try:
                    close()
                except Exception as error:  # noqa: BLE001 - still run the remaining cleanup
                    cleanup_errors.append(f"{label}: {type(error).__name__}: {error}")
        save_json(
            run / "summary.json",
            {
                "results": results,
                "error": failure,
                "cleanup_errors": cleanup_errors,
                "automatic_checks_passed": bool(results)
                and failure is None
                and not cleanup_errors
                and all(result["automatic_checks_passed"] for result in results),
                "semantic_review_required": True,
                "release_approval": False,
            },
        )
    print("Evidence saved. Semantic review is required; this does not qualify a model or release.")
    if failure:
        print(f"Run failed: {failure}", flush=True)
    return int(
        bool(failure or cleanup_errors)
        or not results
        or not all(result["automatic_checks_passed"] for result in results)
    )


if __name__ == "__main__":
    raise SystemExit(main())
