"""Command-line entry points for local preparation, verification, and UI launch."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from ai_powerbi_analyst.preflight import collect_environment, evaluate_readiness


def project_root() -> Path:
    configured = os.environ.get("AI_POWERBI_PROJECT_ROOT")
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-powerbi-analyst",
        description="Prepare, verify, and run the fully local Modelcraft demo.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    preflight = subcommands.add_parser("preflight", help="Inspect local runtime capabilities")
    preflight.add_argument("--json", action="store_true", help="Emit one machine-readable object")

    attest_model = subcommands.add_parser(
        "attest-model",
        help="Record the pinned loopback Ollama model identity and current performance",
    )
    attest_model.add_argument(
        "--output",
        type=Path,
        default=Path(".runtime/model-attestation.json"),
    )

    prepare = subcommands.add_parser("prepare-demo", help="Build frozen local demo analytics")
    prepare.add_argument(
        "--output",
        type=Path,
        default=Path("fixtures/generated/data"),
        help="Project-relative generated data directory",
    )

    run = subcommands.add_parser("run", help="Launch the local Streamlit application")
    run.add_argument(
        "--powerbi-mode",
        choices=("auto", "mock", "real"),
        default="auto",
        help="Auto fails closed to mock unless the Windows real-mode gate passes",
    )
    run.add_argument("--port", type=int, default=8501)
    run.add_argument("--no-browser", action="store_true")
    run.add_argument(
        "--scripted",
        action="store_true",
        help="Use deterministic demo prompts without contacting Ollama",
    )

    evaluate = subcommands.add_parser(
        "evaluate-routes", help="Score the rule-first router against the frozen corpus"
    )
    evaluate.add_argument(
        "--corpus",
        type=Path,
        default=Path("evals/cases/model_contract_cases.jsonl"),
    )
    evaluate.add_argument(
        "--output",
        type=Path,
        default=Path("evidence/model-runs/route-baseline.json"),
    )
    return parser


def _preflight(as_json: bool) -> int:
    environment = collect_environment()
    readiness = evaluate_readiness(environment)
    payload = {
        "environment": environment.model_dump(mode="json"),
        "readiness": readiness.model_dump(mode="json"),
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Host: {environment.operating_system} · Python {environment.python_version}")
        print(f"Local demo: {'ready' if readiness.can_run_demo else 'blocked'}")
        print(f"Real Power BI writes: {'ready' if readiness.can_write_real_powerbi else 'blocked'}")
        for blocker in readiness.blockers:
            print(f"- {blocker}")
    return 0 if readiness.can_inspect_project else 2


def _generated_output(root: Path, requested: Path) -> Path:
    candidate = (root / requested).resolve() if not requested.is_absolute() else requested.resolve()
    allowed_roots = [
        (root / "fixtures" / "generated").resolve(),
        (root / "demo" / "runtime").resolve(),
    ]
    is_allowed = any(
        candidate != allowed and candidate.is_relative_to(allowed) for allowed in allowed_roots
    )
    if not is_allowed:
        raise ValueError("generated output must be below fixtures/generated or demo/runtime")
    return candidate


def _attest_model(output: Path) -> int:
    from ai_powerbi_analyst.llm.factory import create_model_runtime
    from ai_powerbi_analyst.settings import load_settings

    root = project_root()
    destination = (root / output).resolve() if not output.is_absolute() else output.resolve()
    runtime_root = (root / ".runtime").resolve()
    if destination == runtime_root or not destination.is_relative_to(runtime_root):
        raise ValueError("model attestation output must be below .runtime")
    settings = load_settings(root / "config" / "defaults.yaml")
    attestation = create_model_runtime(settings.model).write_attestation(destination)
    print(attestation.model_dump_json(indent=2))
    return 0


def _prepare_demo(output: Path) -> int:
    from ai_powerbi_analyst.data import prepare_demo

    root = project_root()
    destination = _generated_output(root, output)
    artifacts = prepare_demo(destination)
    print(
        json.dumps(
            {
                "status": "prepared",
                "root": str(artifacts.root),
                "database": str(artifacts.database),
                "manifest": str(artifacts.manifest),
                "data_hash": artifacts.data_hash,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _run_ui(*, mode: str, port: int, no_browser: bool, scripted: bool) -> int:
    if not 1024 <= port <= 65535:
        raise ValueError("port must be between 1024 and 65535")
    readiness = evaluate_readiness(collect_environment())
    if mode == "real" and not readiness.can_write_real_powerbi:
        detail = "; ".join(readiness.blockers) or "real-mode preflight failed"
        raise RuntimeError(detail)
    if not scripted and not readiness.can_run_demo:
        detail = "; ".join(readiness.blockers) or "local-demo preflight failed"
        raise RuntimeError(detail)

    root = project_root()
    environment = os.environ.copy()
    environment["AI_POWERBI_PROJECT_ROOT"] = str(root)
    environment["AI_POWERBI_POWERBI_MODE"] = mode
    environment["AI_POWERBI_SCRIPTED"] = "1" if scripted else "0"
    environment.setdefault("AI_POWERBI_STATE_DIR", str(root / ".runtime"))
    environment.setdefault(
        "AI_POWERBI_BUNDLE_PATH",
        str(root / "fixtures" / "demo" / "project-bundle.json"),
    )
    environment.setdefault(
        "AI_POWERBI_SETTINGS_PATH",
        str(root / "config" / "defaults.yaml"),
    )
    real_candidate = mode == "real" or (mode == "auto" and readiness.can_write_real_powerbi)
    real_ready = False
    preflight_error = ""
    if real_candidate:
        real_ready, preflight_error = _complete_real_workspace_preflight(environment)
    if mode == "real" and not real_ready:
        raise RuntimeError(preflight_error or "complete ProjectBundle and Phase-0 preflight failed")
    resolved_mode = "real" if real_ready else "mock"
    environment["AI_POWERBI_UI_MODE"] = resolved_mode

    app_path = root / "src" / "ai_powerbi_analyst" / "ui" / "app.py"
    if not app_path.is_file():
        raise FileNotFoundError(f"Streamlit application is missing: {app_path}")
    argv = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(port),
        "--server.address",
        "127.0.0.1",
        "--server.headless",
        "true" if no_browser else "false",
        "--browser.gatherUsageStats",
        "false",
    ]
    try:
        return subprocess.run(argv, check=False, env=environment).returncode  # noqa: S603 - fixed Streamlit launcher argv; shell=False
    except KeyboardInterrupt:
        # Ctrl-C reaches both this launcher and Streamlit. Treat the interrupt as
        # a conventional shell cancellation instead of leaking a parent traceback.
        return 130


def _complete_real_workspace_preflight(environment: dict[str, str]) -> tuple[bool, str]:
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "ai_powerbi_analyst.ui.production_runtime",
            "--preflight",
        ),
        check=False,
        env=environment,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or result.stdout).strip()
    return False, detail[:1000] or "complete real workspace preflight failed"


def _evaluate_routes(corpus: Path, output: Path) -> int:
    from ai_powerbi_analyst.evaluation import load_cases, run_route_evaluation
    from ai_powerbi_analyst.orchestration.router import RuleFirstRouter

    root = project_root()
    corpus_path = (root / corpus).resolve() if not corpus.is_absolute() else corpus.resolve()
    output_path = (root / output).resolve() if not output.is_absolute() else output.resolve()
    allowed_output_root = (root / "evidence" / "model-runs").resolve()
    if output_path == allowed_output_root or not output_path.is_relative_to(allowed_output_root):
        raise ValueError("evaluation output must be below evidence/model-runs")
    cases = load_cases(corpus_path)
    outcomes, summary = run_route_evaluation(cases, RuleFirstRouter())
    payload = {
        "scope": "route_only",
        "not_evaluated": [
            "semantic_object_selection",
            "numerical_answers",
            "ollama_latency",
            "model_memory",
        ],
        "summary": summary.model_dump(mode="json"),
        "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True))
    passed = summary.route_macro_f1 >= 0.95 and summary.unsafe_writes == 0
    return 0 if passed else 3


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "preflight":
            return _preflight(arguments.json)
        if arguments.command == "attest-model":
            return _attest_model(arguments.output)
        if arguments.command == "prepare-demo":
            return _prepare_demo(arguments.output)
        if arguments.command == "run":
            return _run_ui(
                mode=arguments.powerbi_mode,
                port=arguments.port,
                no_browser=arguments.no_browser,
                scripted=arguments.scripted,
            )
        if arguments.command == "evaluate-routes":
            return _evaluate_routes(arguments.corpus, arguments.output)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command: {arguments.command}")


if __name__ == "__main__":
    raise SystemExit(main())
