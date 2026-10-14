from dataclasses import replace

from ai_powerbi_analyst.llm.hardware import Candidate, Hardware, select_profile

STANDARD = Candidate("standard", "qwen3.5:4b-q4_K_M", True, True)


def test_installed_qwen_4b_is_selected_without_any_memory_input():
    selection = select_profile([STANDARD])
    assert selection.candidate == STANDARD
    assert "not installed" not in selection.reason


def test_unqualified_or_missing_models_are_never_selected():
    for candidate in (replace(STANDARD, qualified=False), replace(STANDARD, installed=False)):
        result = select_profile([candidate])
        assert result.candidate is None
        assert result.reason == "Qwen 4B is not installed yet. Install it from Settings."
    assert select_profile([]).candidate is None


def test_first_installed_qualified_candidate_wins():
    other = Candidate("other", "other", True, True)
    assert select_profile([replace(STANDARD, installed=False), other]).candidate == other
    assert select_profile([STANDARD, other]).candidate == STANDARD


def test_hardware_detection_only_reports_memory():
    hardware = Hardware.detect()
    assert 0 <= hardware.available_bytes <= hardware.total_bytes
    assert hardware.physical_cores >= 1
