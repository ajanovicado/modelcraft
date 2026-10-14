import pytest

from ai_powerbi_analyst.studio.chat_context import (
    contextual_message,
    imported_visual_conversion_reply,
)
from ai_powerbi_analyst.studio.contracts import ConversationContext
from ai_powerbi_analyst.studio.report_import import ImportedPage, ImportedReport, ImportedVisual


@pytest.fixture
def report():
    return ImportedReport(
        id="report-local",
        workspace_id="workspace-local",
        name="Performance",
        source_path="report.pbix",
        archived_path="copy.pbix",
        source_hash="a" * 64,
        base_hash="a" * 64,
        format="pbix",
        pages=[
            ImportedPage(
                id="page-one",
                title="Overview",
                visuals=[
                    ImportedVisual(
                        id="card-one",
                        title="Positive change",
                        visual_type="cardVisual",
                        bindings=[
                            {
                                "kind": "measure",
                                "table": "Measures",
                                "name": "Positive %",
                                "role": "Data",
                            }
                        ],
                    )
                ],
            ),
            ImportedPage(id="page-two", title="Other"),
        ],
        measures=[
            {
                "TableName": "Measures",
                "Name": "Positive %",
                "Expression": "DIVIDE([Positive employees], [Total employees])",
            },
            {"TableName": "Other", "Name": "Positive %", "Expression": "WRONG TABLE"},
        ],
    )


def test_imported_visual_resolves_original_bindings_without_a_draft(report):
    resolved = contextual_message(
        "Explain this visual",
        ConversationContext(page_id="page-one", visual_id="card-one"),
        None,
        None,
        report,
    )
    assert "Positive change" in resolved
    assert "cardVisual" in resolved
    assert "Measures[Positive %]" in resolved
    assert "DIVIDE([Positive employees], [Total employees])" in resolved
    assert "WRONG TABLE" not in resolved
    assert "Values and current slicer/filter selections are not included" in resolved


def test_imported_conversion_distinguishes_existing_table_from_unsupported_edit(report):
    context = ConversationContext(page_id="page-one", visual_id="card-one")
    # Chat changes the type itself when it can read the request (imported_chat_edits); this reply
    # is what is said when it could not, and says nothing was changed.
    unread = imported_visual_conversion_reply("make this a table", context, report)
    assert unread is not None and "could not turn" in unread and "nothing was changed" in unread
    assert "cannot yet change" not in unread
    report.pages[0].visuals[0] = (
        report.pages[0].visuals[0].model_copy(update={"visual_type": "tableEx"})
    )
    assert "already a table" in imported_visual_conversion_reply(
        "make this a table", context, report
    )
    assert imported_visual_conversion_reply("Explain this table", context, report) is None
    assert imported_visual_conversion_reply("make this a table", None, report) is None


@pytest.mark.parametrize(
    "page,visual",
    [
        ("foreign-page", "card-one"),
        ("page-two", "card-one"),
        ("page-one", "foreign-visual"),
        (None, "card-one"),
    ],
)
def test_imported_attachment_rejects_stale_or_foreign_identifiers(report, page, visual):
    with pytest.raises(ValueError):
        contextual_message(
            "Explain",
            ConversationContext(page_id=page, visual_id=visual),
            None,
            None,
            report,
        )


def test_imported_attachment_bounds_large_measure_definitions(report):
    report.measures[0]["Expression"] = "x" * 100_000
    resolved = contextual_message(
        "Explain",
        ConversationContext(page_id="page-one", visual_id="card-one"),
        None,
        None,
        report,
    )
    assert len(resolved) < 4500
