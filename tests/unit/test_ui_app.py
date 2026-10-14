from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from ai_powerbi_analyst.analytics.executor import AnalyticsExecutionError
from ai_powerbi_analyst.jobs.journal import JobStatus
from ai_powerbi_analyst.orchestration.service import ApplicationOrchestrator

PROJECT_ROOT = Path(__file__).parents[2]


def _configure_mock(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AI_POWERBI_UI_MODE", "mock")
    monkeypatch.setenv("AI_POWERBI_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("AI_POWERBI_STATE_DIR", str(tmp_path / "runtime"))


def test_fabricated_sample_ui_mode_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("AI_POWERBI_UI_MODE", "sample")
    from ai_powerbi_analyst.ui import app as ui_app

    with pytest.raises(RuntimeError, match="must be 'mock' or 'real'"):
        ui_app.main()


def test_primary_navigation_renders_each_functional_view(monkeypatch, tmp_path) -> None:
    _configure_mock(monkeypatch, tmp_path)
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=20)

    assert app.session_state["active_page"] == "conversations"
    assert not app.exception

    expected_views = {
        "nav_workspace": ("workspace", "Project health, report structure"),
        "nav_evidence": ("evidence", "No evidence yet"),
        "nav_settings": ("settings", "Interface preferences"),
        "nav_conversations": ("conversations", "Plan report changes"),
    }
    for button_key, (page, expected_copy) in expected_views.items():
        app.button(key=button_key).click().run(timeout=20)
        assert not app.exception
        assert app.session_state["active_page"] == page
        rendered_html = "\n".join(
            element.value for element in app.markdown if isinstance(element.value, str)
        )
        assert expected_copy in rendered_html


def test_mock_navigation_never_probes_installed_tools_or_model(monkeypatch, tmp_path) -> None:
    _configure_mock(monkeypatch, tmp_path)
    from ai_powerbi_analyst import preflight
    from ai_powerbi_analyst.workspace import runtime

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Fixture UI attempted installed-machine or model discovery")

    monkeypatch.setattr(preflight, "probe_powerbi_desktop", forbidden)
    monkeypatch.setattr(preflight, "collect_environment", forbidden)
    monkeypatch.setattr(preflight, "_powerbi_desktop_running", forbidden)
    monkeypatch.setattr(runtime, "create_model_runtime", forbidden)
    monkeypatch.setattr(runtime.VisualCatalogService, "load", forbidden)
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=15)
    assert not app.exception
    app.button(key="nav_settings").click().run(timeout=15)
    assert not app.exception
    assert any("checks are disabled" in item.value for item in app.info)


def test_ui_preferences_are_saved_and_loaded_from_local_state(monkeypatch, tmp_path) -> None:
    _configure_mock(monkeypatch, tmp_path)
    from ai_powerbi_analyst.ui import app as ui_app

    preferences = {
        "default_page": "workspace",
        "show_project_rail": False,
        "show_activity": False,
    }
    ui_app._save_preferences(preferences)

    assert ui_app._load_preferences() == preferences
    assert (tmp_path / "runtime" / "ui_preferences.json").is_file()


def test_apply_button_completes_one_idempotent_local_job(monkeypatch, tmp_path) -> None:
    _configure_mock(monkeypatch, tmp_path)
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=15)

    app.button(key="apply_report").click().run(timeout=15)

    assert not app.exception
    assert app.session_state["validated"] is True
    assert app.session_state["undo_available"] is True
    assert app.session_state["last_job_id"]


def test_evidence_addition_requires_review_before_apply(monkeypatch, tmp_path) -> None:
    _configure_mock(monkeypatch, tmp_path)
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=15)
    app.button(key="apply_report").click().run(timeout=15)
    report_job_id = app.session_state["last_job_id"]
    app.chat_input(key="follow_up").set_value(
        "Revenue is up, so why did margin fall in the West last quarter?"
    ).run(timeout=20)

    app.button(key="add_evidence").click().run(timeout=15)

    assert not app.exception
    assert app.session_state["last_job_id"] == report_job_id
    assert app.session_state["pending_evidence_review"] is True
    assert "Apply evidence" in [button.label for button in app.button]
    rendered_html = "\n".join(
        element.value for element in app.markdown if isinstance(element.value, str)
    )
    assert "Filters: Region: West" in rendered_html

    app.button(key="apply_evidence").click().run(timeout=15)

    assert not app.exception
    assert app.session_state["validated"] is True
    assert app.session_state["undo_available"] is True
    assert app.session_state["last_job_id"]
    assert app.session_state["pending_evidence_review"] is False


def test_spoken_add_evidence_opens_review_for_the_last_grounded_answer(
    monkeypatch, tmp_path
) -> None:
    _configure_mock(monkeypatch, tmp_path)
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=15)
    app.button(key="apply_report").click().run(timeout=15)
    app.chat_input(key="follow_up").set_value(
        "Revenue is up, so why did margin fall in the West last quarter?"
    ).run(timeout=20)

    app.chat_input(key="follow_up").set_value("Add this evidence to the Margin Drivers page").run(
        timeout=15
    )

    assert not app.exception
    assert app.session_state["pending_evidence_review"] is True
    assert "Apply evidence" in [button.label for button in app.button]


def test_governed_analysis_failure_is_rendered_fail_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_POWERBI_UI_MODE", "mock")
    monkeypatch.setenv("AI_POWERBI_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("AI_POWERBI_STATE_DIR", str(tmp_path / "runtime"))

    def fail_review(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AnalyticsExecutionError("worker exited before returning rows")

    monkeypatch.setattr(ApplicationOrchestrator, "review", fail_review)
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=15)

    app.chat_input(key="follow_up").set_value("Why did margin fall in the West last quarter?").run(
        timeout=15
    )

    assert not app.exception
    assert app.session_state["last_evidence"] is None
    assert app.session_state["grounded"] is False
    assert app.session_state["pending_evidence_review"] is False
    assert "stopped safely" in app.session_state["last_message"]

    refreshed = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    refreshed.run(timeout=15)

    assert not refreshed.exception
    assert refreshed.session_state["last_evidence"] is None
    assert refreshed.session_state["conversation"].last_evidence is None
    assert refreshed.session_state["grounded"] is False


def test_spoken_add_does_not_bypass_unsupported_scenario_evidence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_POWERBI_UI_MODE", "mock")
    monkeypatch.setenv("AI_POWERBI_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("AI_POWERBI_STATE_DIR", str(tmp_path / "runtime"))
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=20)
    app.button(key="apply_report").click().run(timeout=20)
    app.chat_input(key="follow_up").set_value(
        "Under the approved 3% price increase, what happens to next-quarter "
        "Net Sales, volume, and gross profit?"
    ).run(timeout=20)

    app.chat_input(key="follow_up").set_value("Add this evidence to the Margin Drivers page").run(
        timeout=20
    )

    assert not app.exception
    assert app.session_state["pending_evidence_review"] is False
    assert "Apply evidence" not in [button.label for button in app.button]
    assert "not approved for report insertion" in app.session_state["last_message"]


def test_failed_evidence_job_preserves_last_valid_trust_and_undo(monkeypatch, tmp_path) -> None:
    _configure_mock(monkeypatch, tmp_path)
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=15)
    app.button(key="apply_report").click().run(timeout=15)
    app.chat_input(key="follow_up").set_value(
        "Revenue is up, so why did margin fall in the West last quarter?"
    ).run(timeout=20)
    app.button(key="add_evidence").click().run(timeout=15)

    def fail_apply(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            id="failed-evidence-job",
            status=JobStatus.FAILED,
            error="validator rejected the staged evidence",
            result_json=None,
        )

    monkeypatch.setattr(ApplicationOrchestrator, "apply_evidence", fail_apply)
    app.button(key="apply_evidence").click().run(timeout=15)

    assert not app.exception
    assert app.session_state["validated"] is True
    assert app.session_state["undo_available"] is True
    assert app.session_state["pending_evidence_review"] is True
    assert app.session_state["last_message"] == (
        "Evidence was not added. The previous validated report and Undo remain available."
    )


def test_fixture_backed_mock_workspace_renders_without_sample_values(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_POWERBI_UI_MODE", "mock")
    monkeypatch.setenv("AI_POWERBI_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("AI_POWERBI_STATE_DIR", str(tmp_path / "runtime"))
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")

    app.run(timeout=20)

    assert not app.exception
    assert app.session_state["pending_plan"].catalog_version == "1.0.0"
    assert app.session_state["last_evidence"] is None
    assert "Apply to Power BI" in [button.label for button in app.button]


def test_fixture_backed_scenario_replaces_prior_evidence_with_exact_scenario(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AI_POWERBI_UI_MODE", "mock")
    monkeypatch.setenv("AI_POWERBI_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("AI_POWERBI_STATE_DIR", str(tmp_path / "runtime"))
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=20)

    prompt = (
        "Under the approved 3% price increase, what happens to next-quarter "
        "revenue, volume, and gross profit?"
    )
    app.chat_input(key="follow_up").set_value(prompt).run(timeout=20)

    assert not app.exception
    answer = app.session_state["last_evidence"]
    assert answer is not None
    assert "next-quarter price scenario" in answer.conclusion
    assert "mechanical scenario" in " ".join(answer.facts).casefold()
    assert answer.chart.kind == "table"

    rendered_html = "\n".join(
        element.value for element in app.markdown if isinstance(element.value, str)
    )
    assert "scenario-card-grid" in rendered_html
    assert "Net Sales" in rendered_html
    assert "$7,526,098" in rendered_html
    assert "34,482 units" in rendered_html
    assert "$3,157,797" in rendered_html
    assert "−0.8 elasticity" in rendered_html
    assert (
        "Mechanical price scenario anchored to the approved next-quarter forecast; "
        "not a prediction."
    ) in rendered_html
    assert "price_change_pct" not in rendered_html
    assert "baseline_period_start" not in rendered_html
    assert "Data proof" in rendered_html
    assert "Catalog proof" in rendered_html


def test_reviewed_plan_renders_compact_assumptions_and_provenance(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_POWERBI_UI_MODE", "mock")
    monkeypatch.setenv("AI_POWERBI_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("AI_POWERBI_STATE_DIR", str(tmp_path / "runtime"))
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")

    app.run(timeout=20)

    rendered_html = "\n".join(
        element.value for element in app.markdown if isinstance(element.value, str)
    )
    assert "plan-provenance" in rendered_html
    assert "Latest complete quarter" in rendered_html
    assert "Frozen v1.0.0 · six-month" in rendered_html
    assert "+3% · −0.8 elasticity · mechanical" in rendered_html
    assert "Sales Growth" in rendered_html
    assert "Unit Cost" in rendered_html
    assert "Category" in rendered_html


def test_grounded_driver_answer_renders_every_bounded_fact_and_interpretation(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AI_POWERBI_UI_MODE", "mock")
    monkeypatch.setenv("AI_POWERBI_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("AI_POWERBI_STATE_DIR", str(tmp_path / "runtime"))
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=20)

    app.chat_input(key="follow_up").set_value(
        "Revenue is up, so why did margin fall in the West last quarter?"
    ).run(timeout=20)

    assert not app.exception
    answer = app.session_state["last_evidence"]
    assert answer is not None
    assert len(answer.facts) >= 7
    rendered_html = "\n".join(
        element.value for element in app.markdown if isinstance(element.value, str)
    )
    for fact in answer.facts[:8]:
        assert fact in rendered_html
    interpretation = app.session_state["last_interpretation"]
    assert interpretation is not None
    assert interpretation.summary
    assert any("Local interpretation:" in caption.value for caption in app.caption)


def test_new_non_evidence_prompt_clears_grounded_answer_across_refresh(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AI_POWERBI_UI_MODE", "mock")
    monkeypatch.setenv("AI_POWERBI_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("AI_POWERBI_STATE_DIR", str(tmp_path / "runtime"))
    app_path = PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py"
    app = AppTest.from_file(app_path)
    app.run(timeout=20)
    app.chat_input(key="follow_up").set_value(
        "Revenue is up, so why did margin fall in the West last quarter?"
    ).run(timeout=20)
    assert app.session_state["last_evidence"] is not None

    app.chat_input(key="follow_up").set_value("How is sales doing?").run(timeout=20)

    assert not app.exception
    assert app.session_state["last_evidence"] is None
    assert app.session_state["conversation"].last_evidence is None
    assert app.session_state["grounded"] is False

    refreshed = AppTest.from_file(app_path)
    refreshed.run(timeout=20)

    assert not refreshed.exception
    assert refreshed.session_state["last_evidence"] is None
    assert refreshed.session_state["conversation"].last_evidence is None
    assert refreshed.session_state["grounded"] is False


def test_forecast_evidence_shows_point_and_both_approved_interval_levels(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AI_POWERBI_UI_MODE", "mock")
    monkeypatch.setenv("AI_POWERBI_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("AI_POWERBI_STATE_DIR", str(tmp_path / "runtime"))
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=20)

    app.chat_input(key="follow_up").set_value(
        "Show next-quarter forecast revenue with the uncertainty range."
    ).run(timeout=20)

    assert not app.exception
    assert app.dataframe
    columns = list(app.dataframe[0].value.columns)
    assert columns == [
        "Period",
        "Metric",
        "Point forecast",
        "80% low",
        "80% high",
        "95% low",
        "95% high",
        "Approved model",
    ]
    assert any("not calibrated aggregate-quarter" in caption.value for caption in app.caption)


def test_undo_rerenders_trust_state_immediately(monkeypatch, tmp_path) -> None:
    _configure_mock(monkeypatch, tmp_path)
    app = AppTest.from_file(PROJECT_ROOT / "src" / "ai_powerbi_analyst" / "ui" / "app.py")
    app.run(timeout=15)
    app.button(key="apply_report").click().run(timeout=15)

    app.button(key="undo_report").click().run(timeout=15)

    assert not app.exception
    assert app.session_state["undo_available"] is False
    assert app.session_state["validated"] is True
    assert app.session_state["activity"][-1]["title"] == "Restored"
