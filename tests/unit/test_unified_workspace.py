"""Isolated integration coverage for shared chat context and draft-only undo."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.chat_context import contextual_message
from ai_powerbi_analyst.studio.contracts import (
    AdvancedDaxProposal,
    ChatRequest,
    ConversationContext,
    CreateWorkspaceRequest,
    DraftRequest,
    IngestRequest,
    RestoreDraftRequest,
    ReviseDraftRequest,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningError, StudioPlanningService
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError


class UnifiedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="studio-unified-test-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        sources = root / "sources"
        sources.mkdir()
        source = sources / "orders.csv"
        source.write_text(
            "Order ID,Order Date,Region,Sales,Profit\n"
            "A,2025-01-01,East,100,20\nB,2025-02-01,West,250,40\n"
            "C,2025-03-01,East,150,30\n",
            encoding="utf-8",
        )
        self.service = StudioService(Path(__file__).resolve().parents[2], root / "state")
        self.service.planner = StudioPlanningService(None, self.service.aggregates)
        workspace = self.service.create_workspace(
            CreateWorkspaceRequest(
                name="Unified test",
                selected_folder=str(sources),
                idempotency_key="unified-create",
                expected_base_hash=EMPTY_HASH,
            )
        )
        self.snapshot = self.service.ingest(
            IngestRequest(
                workspace_id=workspace.id,
                item_paths=[str(source)],
                idempotency_key="unified-ingest",
                expected_base_hash=workspace.base_hash,
            )
        )
        self.workspace = self.service.workspace(workspace.id)
        self.draft = self.service.create_draft(
            DraftRequest(
                workspace_id=workspace.id,
                snapshot_id=self.snapshot.id,
                prompt="Sales and profit by Region over time",
                idempotency_key="unified-draft",
                expected_base_hash=self.workspace.base_hash,
            )
        )

    def tearDown(self):
        import gc

        gc.collect()

    def test_context_references_are_strict(self):
        for value in (
            {"page_id": "missing"},
            {"table_id": "foreign"},
            {"visual_id": "detail_table"},
            {"measure_id": "missing"},
            {"relationship_id": "missing"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                contextual_message(
                    "Explain this", ConversationContext(**value), self.draft, self.snapshot
                )
        with self.assertRaises(ValidationError):
            ConversationContext(page_id="detail", shell="do something")

    def test_contextual_visual_addition_and_receipt(self):
        events = list(
            self.service.chat_events(
                ChatRequest(
                    workspace_id=self.workspace.id,
                    message="Add a bar chart of Sales by Region to this page",
                    context=ConversationContext(page_id="detail"),
                    idempotency_key="unified-context-chat",
                    expected_base_hash=self.draft.base_hash,
                ),
                Event(),
            )
        )
        self.assertTrue(any(event["event"] == "draft" for event in events))
        updated = self.service.draft(self.draft.id)
        detail = next(page for page in updated.pages if page.id == "detail")
        self.assertEqual(len(detail.visuals), 2)
        self.assertEqual(len(updated.pages), len(self.draft.pages))
        self.assertEqual(updated.status, "draft")
        self.assertFalse((self.service.state_root / "projects").exists())

    def test_context_does_not_convert_discussion_into_a_write(self):
        captured = []

        def respond(message, **kwargs):
            captured.append(message)
            yield "Suggestion only."

        self.service.model.stream_chat = respond
        list(
            self.service.chat_events(
                ChatRequest(
                    workspace_id=self.workspace.id,
                    message="Explain this page",
                    context=ConversationContext(page_id="detail"),
                ),
                Event(),
            )
        )
        self.assertIn("Detail", captured[0])
        self.assertEqual(self.service.draft(self.draft.id).base_hash, self.draft.base_hash)

    def test_imported_visual_context_reaches_main_conversation(self):
        from ai_powerbi_analyst.studio.report_import import (
            ImportedPage,
            ImportedReport,
            ImportedVisual,
        )

        report = ImportedReport(
            id="imported-test",
            workspace_id=self.workspace.id,
            name="Imported report",
            source_path="test.pbix",
            archived_path="copy.pbix",
            source_hash="a" * 64,
            base_hash="a" * 64,
            format="pbix",
            pages=[
                ImportedPage(
                    id="original-page",
                    title="Original overview",
                    visuals=[
                        ImportedVisual(
                            id="original-card",
                            title="Imported sales",
                            visual_type="cardVisual",
                            bindings=[{"table": "Measures", "name": "Revenue", "kind": "measure"}],
                        ),
                    ],
                )
            ],
            measures=[
                {"TableName": "Measures", "Name": "Revenue", "Expression": "SUM(Orders[Sales])"}
            ],
        )
        workspace = self.service.workspace(self.workspace.id).model_copy(
            update={"imported_report_id": report.id, "active_draft_id": None}
        )
        captured = []

        def respond(message, **kwargs):
            captured.append(message)
            yield "Explanation only."

        self.service.model.stream_chat = respond
        report_before = report.model_dump()
        draft_before = self.service.draft(self.draft.id).base_hash
        with (
            patch.object(self.service, "workspace", return_value=workspace),
            patch.object(self.service, "imported_report", return_value=report),
            patch.object(self.service.editing, "report", return_value=report),
        ):
            events = list(
                self.service.chat_events(
                    ChatRequest(
                        workspace_id=workspace.id,
                        # Definition walkthroughs now answer locally; ordinary discussion
                        # still receives the selected current-head visual and its formula.
                        message="Discuss the business use of this visual",
                        context=ConversationContext(
                            page_id="original-page", visual_id="original-card"
                        ),
                    ),
                    Event(),
                )
            )
        self.assertIn("Imported sales", captured[0])
        self.assertIn("SUM(Orders[Sales])", captured[0])
        self.assertFalse(any(event["event"] == "draft" for event in events))
        # Explicit imported conversion is answered without another LLM call.
        with (
            patch.object(self.service, "workspace", return_value=workspace),
            patch.object(self.service, "imported_report", return_value=report),
            patch.object(self.service.editing, "report", return_value=report),
        ):
            request = ChatRequest(
                workspace_id=workspace.id,
                message="make this a table",
                context=ConversationContext(page_id="original-page", visual_id="original-card"),
                idempotency_key="imported-convert-test",
            )
            events = list(self.service.chat_events(request, Event()))
            # (the report here cannot be edited by the editing service, so chat says what it
            # could not read and that nothing was changed)
            self.assertIn("could not turn", events[0]["data"]["text"])
            self.assertIn("nothing was changed", events[0]["data"]["text"])
            self.assertEqual(events, list(self.service.chat_events(request, Event())))
        self.assertEqual(len(captured), 1)
        self.assertEqual(report.model_dump(), report_before)
        self.assertEqual(self.service.draft(self.draft.id).base_hash, draft_before)

    def revised(self):
        return self.service.revise_draft(
            self.draft.id,
            ReviseDraftRequest(
                workspace_id=self.workspace.id,
                prompt="Add a Profit Margin % formula and a new Margin Analysis page",
                idempotency_key="unified-revise",
                expected_base_hash=self.draft.base_hash,
            ),
        )

    def test_cancelled_action_never_changes_the_draft(self):
        cancel = Event()
        cancel.set()
        list(
            self.service.chat_events(
                ChatRequest(
                    workspace_id=self.workspace.id,
                    message="Add a Sales by Region bar chart to this page",
                    context=ConversationContext(page_id="detail"),
                    idempotency_key="unified-cancel",
                    expected_base_hash=self.draft.base_hash,
                ),
                cancel,
            )
        )
        self.assertEqual(self.service.draft(self.draft.id), self.draft)

    def test_table_and_measure_context_uses_server_names(self):
        table = self.snapshot.tables[0].table
        measure = self.draft.measure_proposal.measures[0]
        resolved = contextual_message(
            "Explain the fields",
            ConversationContext(table_id=table.id, measure_id=measure.id),
            self.draft,
            self.snapshot,
        )
        self.assertIn(f"Table: {table.name}", resolved)
        self.assertIn(f"Measure: {measure.name}", resolved)

    def test_restore_cannot_target_another_workspace(self):
        updated = self.revised()
        other = self.service.create_workspace(
            CreateWorkspaceRequest(
                name="Other workspace",
                selected_folder=self.workspace.selected_folder,
                idempotency_key="unified-other",
                expected_base_hash=EMPTY_HASH,
            )
        )
        with self.assertRaises(StateConflictError):
            self.service.restore_draft(
                self.draft.id,
                RestoreDraftRequest(
                    workspace_id=other.id,
                    restore_hash=self.draft.base_hash,
                    expected_base_hash=updated.base_hash,
                    idempotency_key="unified-foreign",
                ),
            )
        self.assertEqual(self.service.draft(self.draft.id), updated)

    def test_first_report_can_be_created_through_shared_chat(self):
        workspace = self.service.workspace(self.workspace.id)
        self.service.ingest(
            IngestRequest(
                workspace_id=workspace.id,
                item_paths=[str(Path(workspace.selected_folder) / "orders.csv")],
                idempotency_key="unified-new-import",
                expected_base_hash=workspace.base_hash,
            )
        )
        workspace = self.service.workspace(workspace.id)
        self.assertIsNone(workspace.active_draft_id)
        events = list(
            self.service.chat_events(
                ChatRequest(
                    workspace_id=workspace.id,
                    message="Create a sales dashboard with a detail page",
                    idempotency_key="unified-first-chat",
                    expected_base_hash=workspace.base_hash,
                ),
                Event(),
            )
        )
        self.assertTrue(any(item["event"] == "draft" for item in events))
        current = self.service.workspace(workspace.id)
        self.assertEqual(self.service.draft(current.active_draft_id).status, "draft")
        self.assertFalse((self.service.state_root / "projects").exists())

    def test_restore_is_validated_draft_only_and_replayable(self):
        updated = self.revised()
        request = RestoreDraftRequest(
            workspace_id=self.workspace.id,
            restore_hash=self.draft.base_hash,
            expected_base_hash=updated.base_hash,
            idempotency_key="unified-undo",
        )
        restored = self.service.restore_draft(self.draft.id, request)
        self.assertEqual(restored.pages, self.draft.pages)
        self.assertEqual(restored.status, "draft")
        self.assertFalse(restored.semantic_model.approved)
        self.assertEqual(self.service.restore_draft(self.draft.id, request), restored)
        self.assertFalse((self.service.state_root / "projects").exists())

    def test_restore_revokes_advanced_dax_approval(self):
        table = self.snapshot.tables[0].table
        base = self.draft.measure_proposal.measures[0]
        advanced = AdvancedDaxProposal(
            id="west_sales",
            name="West Sales",
            table_id=table.id,
            expression=f"CALCULATE([{base.name}], '{table.name}'[Region] = \"West\")",
            dependencies=[base.id],
            format_string="#,0.00",
            assumptions=["West identifies the reviewed Region value."],
            approved=True,
        )
        previous = self.draft.model_copy(
            update={
                "measure_proposal": self.draft.measure_proposal.model_copy(
                    update={"measures": [*self.draft.measure_proposal.measures, advanced]}
                )
            }
        )
        restored = self.service.planner.restore_draft(
            previous, self.snapshot, self.service.ingestion.load_manifest(self.snapshot)
        )
        self.assertFalse(restored.measure_proposal.measures[-1].approved)
        self.assertFalse(restored.semantic_model.approved)
        self.assertEqual(restored.status, "draft")

    def test_restore_rejects_changed_snapshot(self):
        with self.assertRaises(StudioPlanningError):
            self.service.planner.restore_draft(
                self.draft,
                self.snapshot.model_copy(update={"id": "different-snapshot"}),
                self.service.ingestion.load_manifest(self.snapshot),
            )

    def test_restore_rejects_stale_or_invented_version(self):
        updated = self.revised()
        with self.assertRaises(StateConflictError):
            self.service.restore_draft(
                self.draft.id,
                RestoreDraftRequest(
                    workspace_id=self.workspace.id,
                    restore_hash=self.draft.base_hash,
                    expected_base_hash=self.draft.base_hash,
                    idempotency_key="unified-stale",
                ),
            )
        with self.assertRaises(ValueError):
            self.service.restore_draft(
                self.draft.id,
                RestoreDraftRequest(
                    workspace_id=self.workspace.id,
                    restore_hash="f" * 64,
                    expected_base_hash=updated.base_hash,
                    idempotency_key="unified-invented",
                ),
            )
        self.assertEqual(self.service.draft(self.draft.id), updated)

    def test_restore_endpoint_requires_auth_and_current_workspace(self):
        updated = self.revised()
        client = TestClient(create_app(self.service, "unified-test-token-local"))
        body = dict(
            workspace_id=self.workspace.id,
            restore_hash=self.draft.base_hash,
            expected_base_hash=updated.base_hash,
            idempotency_key="unified-api-undo",
        )
        endpoint = f"/v1/drafts/{self.draft.id}/restore"
        self.assertEqual(client.post(endpoint, json=body).status_code, 401)
        response = client.post(
            endpoint, json=body, headers={"Authorization": "Bearer unified-test-token-local"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "draft")


if __name__ == "__main__":
    unittest.main()
