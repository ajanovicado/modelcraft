"""Runner mechanics use fake inference; generated text is never executed."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ai_powerbi_analyst.studio.service import StudioService


def _runner():
    path = Path(__file__).resolve().parents[2] / "scripts/evaluate-studio-developer-chat.py"
    spec = importlib.util.spec_from_file_location("developer_chat_smoke_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_case_selection_adds_a_followup_prerequisite_once_and_rejects_unknown():
    runner = _runner()
    assert [case.id for case in runner.select_cases(["decorators-example", "decorators"])] == [
        "decorators",
        "decorators-example",
    ]
    with pytest.raises(ValueError, match="Unknown case"):
        runner.select_cases(["not-a-case"])
    with pytest.raises(SystemExit) as error:
        runner.main(["--case", "decorators"])
    assert error.value.code == 2


def test_followup_evidence_is_isolated_retains_history_and_never_executes_code(
    tmp_path, monkeypatch
):
    runner = _runner()
    sentinel = tmp_path / "studio.sqlite3"
    sentinel.write_text("Saved workspace state must not be opened", encoding="utf-8")
    run = runner.create_run_directory(tmp_path)
    next_run = runner.create_run_directory(tmp_path)
    assert run != next_run and run.parent == next_run.parent == tmp_path.resolve()
    service = StudioService(Path(__file__).resolve().parents[2], run / "state")
    calls = []

    def answer(message, **kwargs):
        calls.append((message, kwargs))
        yield (
            "A decorator wraps a function."
            if len(calls) == 1
            else ("```python\nraise RuntimeError('Do not execute model output')\n```")
        )

    monkeypatch.setattr(service.model, "stream_chat", answer)
    try:
        results = runner.run_cases(service, runner.select_cases(["decorators-example"]), run)
        assert all(result["automatic_checks_passed"] for result in results)
        assert all(result["semantic_review_required"] for result in results)
        assert results[0]["workspace_id"] == results[1]["workspace_id"]
        assert results[1]["python_ast_syntax"]["status"] == "passed"
        assert calls[1][1]["history"][-2]["content"] == runner.CASE_BY_ID["decorators"].prompt
        assert calls[1][1]["history"][-1]["content"] == "A decorator wraps a function."
        evidence = json.loads((run / "decorators-example/prepared-prompt-1.json").read_text())
        assert evidence["message"] == "Show an example."
        assert (run / "decorators-example/response.md").read_text().startswith("```python")
        assert sentinel.read_text() == "Saved workspace state must not be opened"
        assert service.store.path == run / "state/studio.sqlite3"
    finally:
        service.model.unload()
        service.knowledge.close()


def test_python_syntax_is_narrow_and_mutation_evidence_fails_automatic_checks(
    tmp_path, monkeypatch
):
    runner = _runner()
    assert runner.python_syntax_check("Plain explanation")["status"] == "not_checked"
    assert runner.python_syntax_check("```python\ndef broken(\n```")["status"] == "failed"
    run = runner.create_run_directory(tmp_path)
    service = StudioService(Path(__file__).resolve().parents[2], run / "state")

    def unexpected_write(message, **kwargs):
        workspace = service.list_workspaces()[0]
        service.store.event(workspace.id, "draft.changed", {"unexpected": True})
        yield "An explanation."

    monkeypatch.setattr(service.model, "stream_chat", unexpected_write)
    try:
        result = runner.run_cases(service, runner.select_cases(["decorators"]), run)[0]
        assert result["checks"]["workspace_unchanged"] is True
        assert result["checks"]["no_report_mutation_events"] is False
        assert result["automatic_checks_passed"] is False
        assert result["draft_events"][0]["type"] == "draft.changed"
    finally:
        service.model.unload()
        service.knowledge.close()


def test_requested_python_code_cannot_pass_with_a_prose_only_response(tmp_path, monkeypatch):
    runner = _runner()
    run = runner.create_run_directory(tmp_path)
    service = StudioService(Path(__file__).resolve().parents[2], run / "state")

    def prose_only(message, **kwargs):
        yield "Check whether the denominator is zero before dividing."

    monkeypatch.setattr(service.model, "stream_chat", prose_only)
    try:
        result = runner.run_cases(service, runner.select_cases(["safe-divide"]), run)[0]
        assert result["checks"]["nonempty_response"] is True
        assert result["checks"]["requested_python_syntax_passed"] is False
        assert result["python_ast_syntax"]["status"] == "not_checked"
        assert result["automatic_checks_passed"] is False
        assert result["semantic_review_required"] is True
    finally:
        service.model.unload()
        service.knowledge.close()
