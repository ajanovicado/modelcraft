from __future__ import annotations

from ai_powerbi_analyst.ui.theme import APP_CSS


def test_semantic_headings_override_streamlit_display_typography() -> None:
    assert "h2.plan-heading" in APP_CSS
    assert "h2.answer-question" in APP_CSS
    assert "h2.right-heading" in APP_CSS
    assert "font-size: 17px !important" in APP_CSS
    assert "font-size: 14px !important" in APP_CSS
    assert "font-size: 15px !important" in APP_CSS


def test_compatibility_heading_and_plan_provenance_have_compact_layout_rules() -> None:
    assert "white-space: nowrap" in APP_CSS
    assert ".plan-provenance" in APP_CSS
    assert ".scenario-card-grid" in APP_CSS


def test_chat_input_uses_governed_neutral_and_focus_borders() -> None:
    assert '[data-testid="stChatInput"] > div' in APP_CSS
    assert '[data-testid="stChatInput"]:focus-within > div' in APP_CSS
    assert "border-color: #aab4c4 !important" in APP_CSS
    assert "border-color: var(--blue) !important" in APP_CSS
