"""Chat answers about an imported report's cards, computed by Studio's DAX engine.

MAN-01 and MAN-08 (2026-09-27 battery) asked what a card shows: the model guessed what
the measure meant and gave no number. MAN-10 asked to change the 100K target to 120K
and how many agents would make it: it got a clarification request. Now the card's own
query answers, and a what-if recalculates the card's formula with the new threshold.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_imported_report_chat import _ask, _import

from ai_powerbi_analyst.studio.card_answers import card_answer, cards, find_card, what_if_answer
from ai_powerbi_analyst.studio.report_engine import report_model


@pytest.fixture
def imported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, workspace_id = _import(tmp_path, monkeypatch)
    # The fixture's archive has no decodable model: number formats and theme default.
    monkeypatch.setattr("ai_powerbi_analyst.studio.live_report.model_formats", lambda *_: {})
    monkeypatch.setattr("ai_powerbi_analyst.studio.live_report.page_style", lambda *_: ({}, {}))
    report = service.imported_report(workspace_id)
    model = report_model(
        report, service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    )
    return service, workspace_id, report, model


def test_a_card_question_is_answered_from_the_cards_own_figures(imported) -> None:
    service, workspace_id, report, model = imported
    text = _ask(service, workspace_id, "What does the 100K card show right now?")
    # December 2009 month to date: Ana 125,000 (two stores); Bo 60,000; the blank agent 7.
    assert "shows **1** for December 2009, month to date (through 31 Dec 2009)" in text
    assert (
        "the number of DimEmployee[EmployeeKey] values where [MTD Total Sales] is at least 100,000"
        in text
    )
    assert "- **MTD Total Sales**: [Total Sales] month to date" in text
    assert "FactSales → DimStore → DimEmployee" in text
    assert "Calculated by Studio's DAX engine" in text
    # Quoted by its measure name, and asked of the selected card, it is the same card.
    assert card_answer(
        "What is the 'Number of Employees selling more than 100K MTD' card showing?", report, model
    ).startswith("**Number of Employees selling more than 100K MTD** shows **1**")
    assert card_answer("What does this show?", report, model, visual_id="v0") is not None


def test_a_what_if_recalculates_the_card_with_the_new_threshold(imported) -> None:
    service, workspace_id, report, model = imported
    text = _ask(
        service,
        workspace_id,
        "Change the 100K target on that card to 50K. How many agents would make it?",
    )
    # The what-if is answered from the formula. The edit itself is never applied without a
    # confirmation; this fixture has no Power BI Desktop, so chat says why it cannot offer it.
    assert "would show **2**" in text and "instead of 1" in text
    assert "Newly included: Bo Chen." in text
    # The what-if's own "The report has not changed" footer gives way to what chat says next.
    assert "Nothing was changed." in text and "The report has not changed." not in text
    assert "needs Power BI Desktop, which Studio cannot use right now" in text
    lowered = what_if_answer("what if the 100K bar were 130K instead?", report, model)
    assert lowered is not None and "would show **0**" not in lowered  # blank, not zero
    assert "Dropping out: Ana Lopez." in lowered
    assert "The report has not changed." in lowered


@pytest.mark.parametrize(
    "message",
    [
        "Which agents hit the 100K target this month?",
        "Write DAX that shows Total Sales by year",
        "How are sales this month compared to last month?",
        "what does MTD mean?",
    ],
)
def test_other_questions_are_not_card_questions(imported, message: str) -> None:
    _, _, report, _ = imported
    assert find_card(message, report) is None


def test_cards_list_the_measures_a_card_names(imported) -> None:
    _, _, report, _ = imported
    (card,) = cards(report)
    assert card.measure == "Number of Employees selling more than 100K MTD"
    assert card.label == card.measure  # no label text in the fixture: the measure's name


def test_the_data_model_walkthrough_is_read_from_the_model(imported) -> None:
    """DEV-10: the model left out FactSales -> DimChannel and guessed what DateAutoTemplate
    was for; the walkthrough lists what the model holds."""
    service, workspace_id, _, _ = imported
    text = _ask(
        service,
        workspace_id,
        "Walk me through the data model: how are the tables related, and why is "
        "Top-Bottom-N not related to anything?",
    )
    for line in (
        "- FactSales[StoreKey] → DimStore[StoreKey]: many-to-one, single direction",
        "- FactSales[channelKey] → DimChannel[ChannelKey]: many-to-one, single direction",
        "- FactSales → DimStore → DimEmployee: rows of FactSales reach DimEmployee only "
        "through DimStore[EmployeeKey] (2 of the 3 DimEmployee rows are linked from DimStore).",
        "- FactSales has 1 row whose StoreKey is missing from DimStore (99), so Power BI adds "
        "a (Blank) DimStore member",
        "- DateAutoTemplate (731 rows; Year 2008–2009): the same columns and rows as Date, "
        "a copy of it. No measure or visual uses it.",
    ):
        assert line in text
    assert (
        "- Top-Bottom-N (30 rows; Top-Bottom-N 1–30) — a parameter table: measures read its "
        "selection with SELECTEDVALUE: [Top-Bottom-N Value]; default 10 when not exactly one"
    ) in text
    assert "The report is read-only here" in text
