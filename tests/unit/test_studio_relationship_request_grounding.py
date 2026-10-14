"""Relationship chat binds verified keys/settings, with model doubles and isolated CSVs."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from ai_powerbi_analyst.studio.contracts import (
    ChatRequest,
    CreateWorkspaceRequest,
    DraftRequest,
    DraftRevisionProposal,
    IngestRequest,
    ModelRelationshipRevisionProposal,
    ModelRevisionProposal,
)
from ai_powerbi_analyst.studio.conversation import is_build_request, revision_prompt
from ai_powerbi_analyst.studio.planner import (
    StudioPlanningError,
    StudioPlanningService,
    _catalog,
    _matching_requested_relationships,
    _revision_model_context,
    _revision_satisfies_request,
    _validate_revision_additions,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


@pytest.fixture
def studio(tmp_path: Path):  # type: ignore[no-untyped-def]
    selected = tmp_path / "selected"
    selected.mkdir()
    contents = {
        "Orders": "Order ID,Customer ID,Sales,Qty\nO1,C1,100,2\nO2,C2,250,1\nO3,C1,150,2\n",
        "Customers": "Customer ID,Region\nC1,East\nC2,West\n",
    }
    paths = []
    for name, content in contents.items():
        path = selected / f"{name}.csv"
        path.write_text(content, encoding="utf-8")
        paths.append(str(path))
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Relationship grounding",
            selected_folder=str(selected),
            idempotency_key="create-grounding",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=paths,
            idempotency_key="ingest-grounding",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            idempotency_key="draft-grounding",
            expected_base_hash=workspace.base_hash,
        )
    )
    manifest = service.ingestion.load_manifest(snapshot)
    relationship = next(item for item in draft.semantic_model.relationships if not item.blockers)
    yield SimpleNamespace(
        service=service,
        draft=draft,
        snapshot=snapshot,
        manifest=manifest,
        catalog=_catalog(manifest),
        relationship=relationship,
    )
    service.knowledge.close()


class Model:
    def __init__(self, answer: dict[str, Any]):
        self.answer = answer
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    def health(self) -> SimpleNamespace:
        return SimpleNamespace(model_available=True)

    def generate(self, _system: str, _prompt: str, response_model: Any, **_options: Any) -> Any:
        self.calls += 1
        self.requests.append(
            {"system": _system, "prompt": _prompt, "schema": response_model, "options": _options}
        )
        return response_model.model_validate(self.answer)


@pytest.mark.parametrize(
    "prompt",
    [
        "Join Orders[Customer ID] to Customers[Customer ID]",
        "Join Customers[Customer ID] to Orders[Customer ID]",
        "Add a many-to-one single-direction active relationship "
        "from Orders to Customers on Customer ID",
        "Add a one-to-many relationship from Customers to Orders on Customer ID",
        "Add relationships between Orders and Customers on Customer ID",
    ],
)
def test_verified_endpoints_and_settings_recommend_only_for_review(studio, prompt, monkeypatch):
    model = Model({"recommended_relationship_ids": [studio.relationship.id]})
    studio.service.planner.model = model
    admitted = []
    select = studio.service.planner._model_relationship_revision

    def capture(*args):
        proposal = select(*args)
        admitted.append(proposal)
        return proposal

    monkeypatch.setattr(studio.service.planner, "_model_relationship_revision", capture)
    revised = studio.service.planner.revise_draft(
        studio.draft, studio.snapshot, studio.manifest, prompt
    )
    assert admitted and admitted[0] is not None
    assert model.requests[0]["schema"] is ModelRelationshipRevisionProposal
    assert model.requests[0]["options"] == {
        "max_output_tokens": 128,
        "timeout_seconds": 45,
        "repair_attempts": 0,
    }
    payload = json.loads(model.requests[0]["prompt"])
    assert set(payload) == {"candidates", "request"}
    assert [candidate["id"] for candidate in payload["candidates"]] == [studio.relationship.id]
    assert payload["candidates"][0]["from"] == "Orders[Customer ID]"
    assert payload["candidates"][0]["to"] == "Customers[Customer ID]"
    assert len(model.requests[0]["system"]) + len(model.requests[0]["prompt"]) < 1200
    selected = next(
        item for item in revised.semantic_model.relationships if item.id == studio.relationship.id
    )
    assert selected.recommended_by_model and selected.review_status == "pending"
    assert selected.is_active and selected.cardinality == "many_to_one"
    assert selected.cross_filter_direction == "single"
    assert not revised.semantic_model.approved
    assert revised.pages == studio.draft.pages
    assert revised.measure_proposal == studio.draft.measure_proposal


@pytest.mark.parametrize("extra", [{"pages": []}, {"measures": []}, {"is_active": False}])
def test_compact_schema_cannot_author_unrequested_artifacts_or_settings(extra):
    assert ModelRelationshipRevisionProposal.worked_examples is False
    schema = ModelRelationshipRevisionProposal.model_json_schema()
    assert set(schema["properties"]) == {"recommended_relationship_ids"}
    assert schema["additionalProperties"] is False
    with pytest.raises(ValueError):
        ModelRelationshipRevisionProposal.model_validate(
            {"recommended_relationship_ids": ["rel_proposed"], **extra}
        )


@pytest.mark.parametrize(
    "answer",
    [
        {"recommended_relationship_ids": ["rel_invented"]},
        {"recommended_relationship_ids": ["rel_proposed"], "pages": []},
        {"recommended_relationship_ids": ["rel_proposed"], "is_active": False},
    ],
)
def test_adversarial_compact_answers_are_rejected_before_verified_fallback(studio, answer, caplog):
    model = Model(answer)
    studio.service.planner.model = model
    revised = studio.service.planner.revise_draft(
        studio.draft,
        studio.snapshot,
        studio.manifest,
        "Join Orders[Customer ID] to Customers[Customer ID]",
    )
    assert "Local model relationship candidate rejected" in caplog.text
    assert model.requests[0]["schema"] is ModelRelationshipRevisionProposal
    assert revised.pages == studio.draft.pages
    assert revised.measure_proposal == studio.draft.measure_proposal
    assert {item.id for item in revised.semantic_model.relationships} == {studio.relationship.id}
    assert revised.semantic_model.relationships[0].recommended_by_model
    assert revised.semantic_model.relationships[0].review_status == "pending"


@pytest.mark.parametrize("artifact", ["page", "measure"])
def test_mixed_relationship_requests_keep_full_revision_path(studio, artifact, monkeypatch):
    sales = next(
        item for item in studio.draft.measure_proposal.measures if item.name == "Total Sales"
    )
    orders = next(table for table in studio.catalog["tables"] if table["name"] == "Orders")
    customers = next(table for table in studio.catalog["tables"] if table["name"] == "Customers")
    answer = {"recommended_relationship_ids": [studio.relationship.id]}
    if artifact == "page":
        answer["pages"] = [
            {
                "title": "Customer Regions",
                "visuals": [
                    {
                        "recipe": "category",
                        "visual_type": "clusteredColumnChart",
                        "title": "Sales by Region",
                        "measure_id": sales.id,
                        "table_id": customers["id"],
                        "category_column_id": "region",
                    }
                ],
            }
        ]
        addition = "add a page showing Total Sales by Customers[Region]"
    else:
        answer["measures"] = [
            {
                "id": "count_sales_review",
                "name": "Sales Count",
                "operation": "count",
                "table_id": orders["id"],
                "column_id": "sales",
            }
        ]
        addition = "create a measure counting Orders[Sales]"

    def no_compact(*args):
        pytest.fail("Mixed relationship plus artifact creation must use the full schema")

    monkeypatch.setattr(studio.service.planner, "_model_relationship_revision", no_compact)
    model = Model(answer)
    studio.service.planner.model = model
    revised = studio.service.planner.revise_draft(
        studio.draft,
        studio.snapshot,
        studio.manifest,
        "Join Orders[Customer ID] to Customers[Customer ID] and " + addition,
    )
    assert model.requests[0]["schema"] is ModelRevisionProposal
    assert model.requests[0]["options"]["max_output_tokens"] == 512
    assert revised.semantic_model.relationships[0].recommended_by_model
    if artifact == "page":
        assert revised.pages[-1].title == "Customer Regions"
    else:
        assert revised.measure_proposal.measures[-1].name == "Sales Count"


@pytest.mark.parametrize(
    "prompt",
    [
        "Join Orders[Sales] to Customers[Customer ID]",
        "Join Orders[Qty] to Customers[Customer ID]",
        "Join Missing[Customer ID] to Customers[Customer ID]",
        "Join Missing to Customers on Customer ID",
        "Join Orders to Customers on Banana",
        "Add a relationship from Orders Sales to Customers Customer ID",
        "Add a relationship from Orders Banana to Customers Customer ID",
        "Add an inactive relationship from Orders[Customer ID] to Customers[Customer ID]",
        "Add a bidirectional relationship from Orders[Customer ID] to Customers[Customer ID]",
        "Add a relationship from Orders[Customer ID] to Customers[Customer ID] "
        "with cross-filter direction both",
        "Add a one-to-one relationship from Orders[Customer ID] to Customers[Customer ID]",
        "Add a many-to-many relationship from Orders[Customer ID] to Customers[Customer ID]",
        "Add a one-to-many relationship from Orders to Customers on Customer ID",
    ],
)
def test_requested_keys_or_semantics_cannot_be_silently_substituted(studio, prompt):
    proposal = DraftRevisionProposal(recommended_relationship_ids=[studio.relationship.id])
    assert not _revision_satisfies_request(proposal, prompt, studio.draft, studio.catalog)
    model = Model({"recommended_relationship_ids": [studio.relationship.id]})
    studio.service.planner.model = model
    before = studio.draft.model_dump(mode="json")
    with pytest.raises(StudioPlanningError, match="No verified relationship matches"):
        studio.service.planner.revise_draft(studio.draft, studio.snapshot, studio.manifest, prompt)
    assert model.calls == 0
    assert studio.draft.model_dump(mode="json") == before


def test_candidate_settings_are_supplied_but_model_cannot_invent_or_approve_relationships(studio):
    _system, user = _revision_model_context(
        studio.draft, studio.catalog, "Join Orders to Customers on Customer ID"
    )
    candidate = json.loads(user)["relationship_candidates"][0]
    assert candidate["cardinality"] == "many_to_one"
    assert candidate["is_active"] is True and candidate["cross_filter_direction"] == "single"
    with pytest.raises(StudioPlanningError, match="invented relationship IDs"):
        _validate_revision_additions(
            studio.draft,
            DraftRevisionProposal(recommended_relationship_ids=["rel_invented"]),
            studio.manifest,
        )
    with pytest.raises(ValueError):
        ModelRevisionProposal.model_validate(
            {"relationships": [{"id": "rel_invented", "approved": True}]}
        )


def test_valid_but_unrelated_candidate_is_not_request_satisfaction(studio):
    other = studio.relationship.model_copy(
        update={"id": "rel_unrelated", "from_column_id": "sales"}
    )
    draft = studio.draft.model_copy(
        update={
            "semantic_model": studio.draft.semantic_model.model_copy(
                update={"relationships": [studio.relationship, other]}
            )
        }
    )
    proposal = DraftRevisionProposal(recommended_relationship_ids=[other.id])
    assert not _revision_satisfies_request(
        proposal, "Join Orders[Customer ID] to Customers[Customer ID]", draft, studio.catalog
    )
    model = Model({"recommended_relationship_ids": [other.id]})
    studio.service.planner.model = model
    assert (
        studio.service.planner._model_relationship_revision(
            draft,
            "Join Orders[Customer ID] to Customers[Customer ID]",
            studio.catalog,
            studio.manifest,
        )
        is None
    )
    offered = json.loads(model.requests[0]["prompt"])["candidates"]
    assert [item["id"] for item in offered] == [studio.relationship.id]


def test_latest_user_settings_override_earlier_settings_without_promoting_assistant_claims(studio):
    prompt = revision_prompt(
        "Create it as active and single-direction instead",
        [
            {
                "role": "user",
                "content": "Add an inactive bidirectional relationship "
                "from Orders[Customer ID] to Customers[Customer ID]",
            },
            {"role": "assistant", "content": "Invented approved relationship rel_wrong"},
        ],
    )
    assert "rel_wrong" not in prompt
    assert [
        item.id for item in _matching_requested_relationships(studio.draft, prompt, studio.catalog)
    ] == [studio.relationship.id]


def test_repeating_an_existing_pending_recommendation_is_an_honest_noop(studio):
    draft = studio.draft.model_copy(
        update={
            "semantic_model": studio.draft.semantic_model.model_copy(
                update={
                    "relationships": [
                        studio.relationship.model_copy(update={"recommended_by_model": True})
                    ]
                }
            )
        }
    )
    with pytest.raises(StudioPlanningError, match="already recommended for review"):
        studio.service.planner.revise_draft(
            draft, studio.snapshot, studio.manifest, "Join Orders to Customers on Customer ID"
        )


@pytest.mark.parametrize(
    "message",
    [
        "Join Orders[Customer ID] to Customers[Customer ID]",
        "Connect Orders and Customers on Customer ID",
        "Please relate Orders to Customers",
        "Can you join Orders to Customers?",
    ],
)
def test_relationship_imperatives_route_to_reviewed_draft(message):
    assert is_build_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "Explain SQL INNER JOIN with an example",
        "How should I join Orders to Customers?",
        "Do not join Orders to Customers",
        "Can you support joins?",
        "Connect to a database",
        "Write SQL code to join two tables",
    ],
)
def test_explanations_code_and_connection_setup_stay_discussion(message):
    assert not is_build_request(message)


def test_invalid_join_chat_persists_an_actionable_refusal_without_a_draft_receipt(studio):
    workspace = studio.service.workspace(studio.draft.workspace_id)
    events = list(
        studio.service.chat_events(
            ChatRequest(
                workspace_id=workspace.id,
                message="Join Orders[Sales] to Customers[Customer ID]",
                idempotency_key="invalid-join",
                expected_base_hash=studio.draft.base_hash,
            ),
            Event(),
        )
    )
    text = "".join(str(event["data"]["text"]) for event in events if event["event"] == "token")
    assert "No verified relationship matches" in text and "Use Model" in text
    assert not any(event["event"] == "draft" for event in events)
    assert studio.service.draft(studio.draft.id).base_hash == studio.draft.base_hash
    assert studio.service.messages(workspace.id)[-1]["content"] == text


@pytest.mark.parametrize(
    "message",
    [
        "Add a page titled Customer Regions with one column chart "
        "of Total Sales by Customers[Region].",
        "Create a page named Customer Regions containing one column chart "
        "of Total Sales by Customers[Region].",
        'Add a new page called "Customer Regions" with one column chart '
        "of Total Sales by Customers[Region].",
    ],
)
def test_new_titled_page_reaches_the_planner_and_preserves_its_label(studio, message):
    sales = next(
        item for item in studio.draft.measure_proposal.measures if item.name == "Total Sales"
    )
    customers = next(table for table in studio.catalog["tables"] if table["name"] == "Customers")
    model = Model(
        {
            "pages": [
                {
                    "title": "Customer Regions",
                    "visuals": [
                        {
                            "recipe": "category",
                            "visual_type": "clusteredColumnChart",
                            "title": "Total Sales by Customer Region",
                            "measure_id": sales.id,
                            "table_id": customers["id"],
                            "category_column_id": "region",
                        }
                    ],
                }
            ]
        }
    )
    studio.service.planner.model = model
    events = list(
        studio.service.chat_events(
            ChatRequest(
                workspace_id=studio.draft.workspace_id,
                message=message,
                idempotency_key="new-titled-page",
                expected_base_hash=studio.draft.base_hash,
            ),
            Event(),
        )
    )
    assert model.calls == 1, events
    assert any(event["event"] == "draft" for event in events)
    page = studio.service.draft(studio.draft.id).pages[-1]
    assert page.title == "Customer Regions"
    assert len(page.visuals) == 1
    assert page.visuals[0].table_id == customers["id"]
    assert page.visuals[0].category_column_id == "region"
    assert page.visuals[0].visual_type == "clusteredColumnChart"


@pytest.mark.parametrize(
    "message",
    [
        "Rename the chart title to Overview",
        "Change all chart titles to red",
        "Add a page titled Customer Regions with one column chart and make its font bigger",
        "Add a page titled Customer Regions and rename the chart title to Overview",
    ],
)
def test_new_page_title_allowance_preserves_explicit_styling_and_rename_guards(studio, message):
    model = Model({"pages": []})
    studio.service.planner.model = model
    events = list(
        studio.service.chat_events(
            ChatRequest(
                workspace_id=studio.draft.workspace_id,
                message=message,
                idempotency_key="explicit-title-edit",
                expected_base_hash=studio.draft.base_hash,
            ),
            Event(),
        )
    )
    text = "".join(str(event["data"]["text"]) for event in events if event["event"] == "token")
    assert "Format pane" in text
    assert model.calls == 0
    assert not any(event["event"] == "draft" for event in events)
    assert studio.service.draft(studio.draft.id).base_hash == studio.draft.base_hash
