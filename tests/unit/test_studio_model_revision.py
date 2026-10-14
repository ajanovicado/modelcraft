"""Compact model revisions: prompt budget, planner-owned expansion and the request gate.

Model doubles here stand in for the local runtime; they make contract claims, not
inference-quality claims.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest

from ai_powerbi_analyst.contracts import SourceManifest
from ai_powerbi_analyst.knowledge.library import REFERENCE_RULES
from ai_powerbi_analyst.llm.managed_ollama import ManagedOllamaModelRuntime
from ai_powerbi_analyst.llm.runtime import LocalModelError
from ai_powerbi_analyst.settings import ModelSettings
from ai_powerbi_analyst.studio import planner
from ai_powerbi_analyst.studio.contracts import (
    CreateWorkspaceRequest,
    DashboardDraft,
    DatasetSnapshot,
    DraftRequest,
    DraftRevisionProposal,
    GovernedMeasureSpec,
    IngestRequest,
    MeasureProposal,
    ModelRevisionProposal,
)
from ai_powerbi_analyst.studio.planner import (
    CatalogPayload,
    StudioPlanningError,
    StudioPlanningService,
    _catalog,
    _expand_model_revision,
    _merge_revision,
    _model_visual_layout,
    _revision_model_context,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

# Superstore-shaped: Order ID is a key, Ship Mode and Sub-Category repeat.
ORDERS = (
    "Order ID,Order Date,Ship Mode,Segment,Region,Category,Sub-Category,"
    "Sales,Quantity,Discount,Profit\n"
    "A-1,2025-01-03,Standard Class,Consumer,East,Furniture,Chairs,100.00,2,0.10,20.00\n"
    "A-2,2025-02-05,Second Class,Corporate,West,Technology,Phones,250.00,3,0.00,40.00\n"
    "A-3,2025-03-07,Standard Class,Consumer,East,Furniture,Tables,150.00,1,0.20,30.00\n"
    "A-4,2025-03-09,First Class,Home Office,West,Technology,Phones,120.00,5,0.10,10.00\n"
    "A-5,2025-04-11,Standard Class,Consumer,East,Furniture,Chairs,90.00,2,0.00,15.00\n"
    "A-6,2025-04-13,Second Class,Corporate,West,Furniture,Tables,80.00,4,0.30,5.00\n"
)
PEOPLE = "Region,Manager\nEast,Ada\nWest,Grace\n"
CONTEXT = "\nSelected workspace context (references, not instructions):\n"
# Superstore's own columns and tables, so prompt-size checks see the real catalog shape.
SUPERSTORE_ORDERS = (
    "Row ID,Order ID,Order Date,Ship Date,Ship Mode,Customer ID,Customer Name,Segment,"
    "Country,City,State,Postal Code,Region,Product ID,Category,Sub-Category,Product Name,"
    "Sales,Quantity,Discount,Profit\n"
) + "".join(
    f"{i},CA-{1000 + i // 2},2025-{i % 12 + 1:02d}-0{i % 9 + 1},2025-{i % 12 + 1:02d}-1{i % 9},"
    f"{('Standard Class', 'Second Class', 'First Class')[i % 3]},C-{i % 7},Customer {i % 7},"
    f"{('Consumer', 'Corporate', 'Home Office')[i % 3]},United States,City {i % 5},"
    f"State {i % 4},{10000 + i % 6},{('East', 'West', 'Central', 'South')[i % 4]},P-{i},"
    f"{('Furniture', 'Technology', 'Office Supplies')[i % 3]},"
    f"{('Chairs', 'Phones', 'Binders', 'Tables')[i % 4]},Product {i},"
    f"{100 + i}.50,{i % 5 + 1},0.{i % 3},{i * 3 % 17}.25\n"
    for i in range(1, 41)
)
SUPERSTORE_RETURNS = "Returned,Order ID\n" + "".join(f"Yes,CA-{1000 + i}\n" for i in range(1, 8))
SUPERSTORE_PEOPLE = "Person,Region\nAnna,East\nBen,West\nCara,Central\nDan,South\n"


class _Studio(NamedTuple):
    service: StudioService
    snapshot: DatasetSnapshot
    manifest: SourceManifest
    draft: DashboardDraft

    @property
    def catalog(self) -> CatalogPayload:
        return _catalog(self.manifest)

    def table(self, name: str) -> str:
        return next(table.id for table in self.manifest.tables if table.name == name)

    def measure(self, name: str) -> str:
        return next(item.id for item in self.draft.measure_proposal.measures if item.name == name)

    def revise(
        self, model: _Model, prompt: str, draft: DashboardDraft | None = None
    ) -> DashboardDraft:
        self.service.planner.model = model  # type: ignore[assignment]
        return self.service.planner.revise_draft(
            draft or self.draft, self.snapshot, self.manifest, prompt
        )


def _studio(tmp_path: Path, **sources: str) -> _Studio:
    source_root = tmp_path / "selected"
    source_root.mkdir()
    paths = []
    for name, content in sources.items():
        path = source_root / f"{name}.csv"
        path.write_text(content, encoding="utf-8")
        paths.append(str(path))
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Model revision",
            selected_folder=str(source_root),
            idempotency_key="create-model-revision",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=paths,
            idempotency_key="ingest-model-revision",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            idempotency_key="draft-model-revision",
            expected_base_hash=workspace.base_hash,
        )
    )
    return _Studio(service, snapshot, service.ingestion.load_manifest(snapshot), draft)


class _Model:
    """Returns scripted answers, validated as a runtime would, and records every call."""

    def __init__(self, *answers: dict[str, Any] | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[SimpleNamespace] = []

    def health(self) -> SimpleNamespace:
        return SimpleNamespace(model_available=True)

    def generate(self, system: str, prompt: str, response_model: Any, **options: Any) -> Any:
        self.calls.append(
            SimpleNamespace(
                system=system,
                payload=json.loads(prompt),
                response_model=response_model,
                options=options,
            )
        )
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return response_model.model_validate(answer)


def _page(title: str, *visuals: dict[str, Any]) -> dict[str, Any]:
    return {"pages": [{"title": title, "visuals": list(visuals)}]}


def _visual(recipe: str, visual_type: str, title: str, **binding: Any) -> dict[str, Any]:
    return {"recipe": recipe, "visual_type": visual_type, "title": title, **binding}


def _added_pages(studio: _Studio, revised: DashboardDraft) -> list[str]:
    existing = {page.id for page in studio.draft.pages}
    return [page.title for page in revised.pages if page.id not in existing]


def test_revision_prompt_is_compact_and_ends_with_the_users_own_words(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    studio = _studio(tmp_path, orders=ORDERS, people=PEOPLE)
    stages: list[str] = []
    studio.service.planner.on_retrieval = lambda _workspace, stage, _receipt: stages.append(stage)
    selected = studio.draft.pages[0]
    request = "Add a page showing profit by sub-category"
    prompt = (
        request
        + CONTEXT
        + f"Page: {selected.title} [{selected.id}]\n"
        + "Imported visual type: tableEx\n"
        + "Fields and measure definitions:\nY: Sheet1[Revenue]\n"
        + "DAX: CALCULATE(SUM('Sheet1'[Revenue]))"
    )
    model = _Model(LocalModelError("Generation ended timeout"))

    revised = studio.revise(model, prompt)

    # One bounded call: a repair after a CPU timeout would cost another 90 s.
    (call,) = model.calls
    assert call.response_model is ModelRevisionProposal
    assert call.options == {"max_output_tokens": 512, "timeout_seconds": 90, "repair_attempts": 0}
    context = {key: value for key, value in call.payload.items() if key != "request"}
    del context["attached"]
    size = len(call.system.encode()) + len(
        json.dumps(context, separators=(",", ":"), ensure_ascii=False).encode()
    )
    assert size <= planner._REVISION_CONTEXT_BYTES
    # Retrieved references were irrelevant and cut mid-JSON; the model sees none.
    assert REFERENCE_RULES not in call.system
    assert "reference_material" not in call.payload
    assert "revision" not in stages
    assert list(call.payload)[-1] == "request"
    assert call.payload["request"] == request
    # The attachment's reference lines stay; imported fields and DAX do not.
    assert call.payload["attached"] == f"Page: {selected.title} [{selected.id}]"
    assert "CALCULATE" not in json.dumps(call.payload)
    # Superstore-style names are spelled out by their IDs, so no field repeats one.
    assert all(
        code.count(":") == 1 for table in call.payload["catalog"] for code in table["fields"]
    )
    assert "Generation ended timeout" in caplog.text
    assert _added_pages(studio, revised) == ["Profit by Sub-Category"]


def test_revision_schema_and_runtime_add_no_worked_examples(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    schema = json.dumps(ModelRevisionProposal.model_json_schema())
    # The planner owns IDs, geometry and existing-page additions; the model never writes them.
    for planner_owned in ("visual_additions", "date_filter", '"purpose"', '"x"', '"width"'):
        assert planner_owned not in schema
    runtime = ManagedOllamaModelRuntime(ModelSettings())
    sent: list[list[dict[str, str]]] = []

    class Server:
        def complete(self, messages: list[dict[str, str]], **_kwargs: Any) -> dict[str, Any]:
            sent.append(list(messages))
            output = json.dumps({"recommended_relationship_ids": ["rel_orders_people"]})
            return {
                "raw": output,
                "output": output,
                "complete": True,
                "error": None,
                "tokens": 2,
                "prompt_tokens": 3,
                "seconds": 0.1,
                "native_timings": {},
            }

    @contextmanager
    def session():  # type: ignore[no-untyped-def]
        # Evidence files are write-once, so each call gets its own folder.
        target = tmp_path / f"call-{len(sent)}"
        target.mkdir()
        yield Server(), target, tmp_path

    monkeypatch.setattr(runtime, "_session", session)
    monkeypatch.setattr(
        runtime,
        "_references",
        lambda *_: ("\n\nWorked examples with DIFFERENT data. M merge", ["R057-pk_fk-1"]),
    )

    runtime.generate("system", '{"request":"join orders"}', ModelRevisionProposal)
    # Control: a schema that keeps worked examples receives them through the same hook.
    runtime.generate("system", '{"request":"join orders"}', DraftRevisionProposal)

    assert sent[0][-1]["content"] == '{"request":"join orders"}'
    assert "Worked examples with DIFFERENT data" in sent[1][-1]["content"]


def test_compact_catalog_names_only_fields_whose_id_hides_the_name(tmp_path: Path) -> None:
    studio = _studio(
        tmp_path,
        umsatz="Order Date,Region,Umsatz (€),销售额\n"
        "2025-01-01,East,10.5,3\n2025-02-01,West,20.5,4\n2025-03-01,East,5.0,7\n",
    )

    _system, user = _revision_model_context(
        studio.draft, studio.catalog, "Add a page showing Umsatz by region"
    )

    (table,) = json.loads(user)["catalog"]
    assert table["fields"] == [
        "order_date:date",
        "region:text",
        "umsatz:num:Umsatz (€)",
        "field:num:销售额",
    ]


def test_relationship_ids_are_offered_only_for_relationship_requests(tmp_path: Path) -> None:
    studio = _studio(tmp_path, orders=ORDERS, people=PEOPLE)
    (valid,) = studio.draft.semantic_model.relationships
    blocked = valid.model_copy(
        update={
            "id": "rel_blocked_manager",
            "from_column_id": "segment",
            "to_column_id": "manager",
            "blockers": ["Keys do not match"],
        }
    )
    draft = studio.draft.model_copy(
        update={
            "semantic_model": studio.draft.semantic_model.model_copy(
                update={"relationships": [valid, blocked]}
            )
        }
    )

    system, user = _revision_model_context(
        draft, studio.catalog, "Add a page showing sales by manager"
    )
    payload = json.loads(user)
    # The join a grouping can use, without its long ID; a blocked one is never offered.
    assert payload["joins"] == ["orders.region>people.region"]
    assert "relationship_candidates" not in payload
    assert "relationship_candidates" not in system

    system, user = _revision_model_context(draft, studio.catalog, "Join orders to people on region")
    payload = json.loads(user)
    assert payload["relationship_candidates"] == [
        {
            "id": valid.id,
            "join": "orders.region>people.region",
            "cardinality": "many_to_one",
            "is_active": True,
            "cross_filter_direction": "single",
        }
    ]
    assert "rel_blocked_manager" not in user
    assert "relationship_candidates" in system


def test_over_the_budget_the_catalog_keeps_what_a_revision_can_bind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = "".join(
        f"{i},O-{i},2025-{i % 12 + 1:02d}-01,Customer {i},City {i},"
        f"{['East', 'West', 'North', 'South'][i % 4]},{'true' if i % 5 == 0 else 'false'},"
        f"{100 + i}.00,{i}.50\n"
        for i in range(1, 61)
    )
    studio = _studio(
        tmp_path,
        orders="Row ID,Order ID,Order Date,Customer Name,City,Region,Returned,Sales,Profit\n"
        + rows,
        notes="Note,Author\nfoo,Ann\nbar,Bob\nbaz,Cid\n",
    )
    monkeypatch.setattr(planner, "_REVISION_CONTEXT_BYTES", 0)

    _system, user = _revision_model_context(
        studio.draft, studio.catalog, "Add a new page listing customer names by region"
    )

    # Kept: the named field (plural), dates, measure sources, non-key numbers and small
    # dimensions, a Returned flag included. Dropped: a key-like number, unnamed
    # high-cardinality text, and a table nothing names, measures or joins.
    (orders,) = json.loads(user)["catalog"]
    assert orders["fields"] == [
        "order_id:text",
        "order_date:date",
        "customer_name:text",
        "region:text",
        "returned:bool",
        "sales:num",
        "profit:num",
    ]


def test_over_the_hard_cap_the_model_is_not_called(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    studio = _studio(tmp_path, orders=ORDERS)
    monkeypatch.setattr(planner, "_REVISION_CONTEXT_BYTES", 0)
    monkeypatch.setattr(planner, "_REVISION_CONTEXT_BYTES_HARD", 0)
    model = _Model()

    revised = studio.revise(model, "Add a page showing profit by sub-category")

    assert model.calls == []
    assert "without a model call" in caplog.text
    assert _added_pages(studio, revised) == ["Profit by Sub-Category"]


def _superstore(tmp_path: Path) -> _Studio:
    return _studio(
        tmp_path,
        orders=SUPERSTORE_ORDERS,
        returns=SUPERSTORE_RETURNS,
        people=SUPERSTORE_PEOPLE,
    )


def test_superstore_revision_prompts_keep_the_whole_catalog_in_about_600_tokens(
    tmp_path: Path,
) -> None:
    studio = _superstore(tmp_path)
    fields = sum(len(table["fields"]) for table in studio.catalog["tables"])

    for request in (
        "Add a page showing profit by sub-category",
        "create a year over year matirx at the bottom",
        "Add a new page comparing average discount and total quantity by ship mode",
    ):
        context = _revision_model_context(studio.draft, studio.catalog, request)
        assert context is not None
        system, user = context
        # Within the budget nothing a request might bind is dropped.
        assert sum(len(table["fields"]) for table in json.loads(user)["catalog"]) == fields
        # The real Superstore draft measured 2,373-2,405 B (628-633 Qwen tokens), against
        # 5,751-5,795 B (1,446-1,469 tokens, ~47 s of CPU prefill) before.
        assert len(system.encode()) + len(user.encode()) <= 2450


def test_a_draft_that_gained_a_dozen_measures_still_reaches_the_model(tmp_path: Path) -> None:
    studio = _superstore(tmp_path)
    orders, sales = studio.table("orders"), studio.measure("Total Sales")
    page = studio.draft.pages[0]

    def grown(count: int) -> DashboardDraft:
        extra = [
            GovernedMeasureSpec(
                id=f"period_change_year_{index:02d}_{sales}",
                name=f"Total Sales Change {index:02d} %",
                operation="period_change",
                table_id=orders,
                base_measure_id=sales,
                date_column_id="order_date",
                period="year",
                format_string="0.0%",
            )
            for index in range(count)
        ]
        return studio.draft.model_copy(
            update={
                "measure_proposal": MeasureProposal(
                    measures=[*studio.draft.measure_proposal.measures, *extra]
                ),
                "pages": [
                    page,
                    *(
                        page.model_copy(
                            update={"id": f"review_{index}", "title": f"Review {index}"}
                        )
                        for index in range(6)
                    ),
                ],
            }
        )

    request = "Add a page showing profit by sub-category"
    # Each measure costs ~85 B of IDs and names; a tighter cap silently stopped calling
    # the model once a draft had about eight more.
    assert _revision_model_context(grown(12), studio.catalog, request) is not None
    assert _revision_model_context(grown(30), studio.catalog, request) is None


def test_model_page_gets_planner_owned_ids_tables_and_layout(tmp_path: Path) -> None:
    studio = _studio(tmp_path, orders=ORDERS)
    orders, profit = studio.table("orders"), studio.measure("Total Profit")
    answer = ModelRevisionProposal.model_validate(
        _page(
            # Normalizes to the existing "overview" page ID, but is a new title.
            "Overview!",
            _visual(
                "category",
                "clusteredBarChart",
                "Profit by Sub-Category",
                measure_id=profit,
                category_column_id="sub_category",
            ),
            _visual("kpi", "cardVisual", "Total Profit", measure_id=profit),
            _visual(
                "trend",
                "lineChart",
                "Profit over time",
                measure_id=profit,
                category_column_id="order_date",
            ),
        )
    )

    payload = _expand_model_revision(answer, studio.draft, studio.catalog)

    (page,) = payload["pages"]
    assert (page["id"], page["purpose"]) == ("overview_2", "Overview!")
    existing = {page.id for page in studio.draft.pages} | {
        visual.id for page in studio.draft.pages for visual in page.visuals
    }
    ids = [visual["id"] for visual in page["visuals"]]
    assert len(set(ids)) == 3 and not set(ids) & existing
    # Cards on top, the charts sharing the row below.
    placed = [
        (visual["recipe"], visual["table_id"], visual["x"], visual["y"])
        + (visual["width"], visual["height"])
        for visual in page["visuals"]
    ]
    assert placed == [
        ("kpi", orders, 0, 0, 4, 2),
        ("trend", orders, 0, 2, 6, 5),
        ("category", orders, 6, 2, 6, 5),
    ]
    _measures, pages = _merge_revision(
        studio.draft, DraftRevisionProposal.model_validate(payload), studio.manifest
    )
    assert [(v.x, v.y, v.width, v.height) for v in pages[-1].visuals] == [
        (x, y, width, height) for _recipe, _table, x, y, width, height in placed
    ]


@pytest.mark.parametrize(
    ("recipes", "expected"),
    [
        # A card row, a chart row and a table row need 13 of the 12 grid rows.
        (
            ["kpi", "trend", "category", "detail"],
            [(0, 0, 4, 2), (4, 0, 6, 5), (0, 2, 4, 5), (4, 5, 8, 6)],
        ),
        # Cards first would strand the second table; the tables go first instead.
        (
            ["kpi", "kpi", "detail", "detail"],
            [(8, 0, 4, 2), (8, 2, 4, 2), (0, 0, 8, 6), (0, 6, 8, 6)],
        ),
    ],
    ids=["reading-order", "largest-first"],
)
def test_layout_that_overflows_its_rows_packs_every_visual_into_free_cells(
    recipes: list[str], expected: list[tuple[int, int, int, int]]
) -> None:
    layout = _model_visual_layout(recipes)

    assert layout == expected
    cells = [
        (x + dx, y + dy)
        for x, y, width, height in layout
        for dx in range(width)
        for dy in range(height)
    ]
    assert len(cells) == len(set(cells))
    assert all(x < 12 and y < 12 for x, y in cells)


@pytest.mark.parametrize("copies_fact_table_id", [False, True], ids=["no-table", "fact-table"])
def test_grouping_column_from_a_joined_table_binds_that_table_only_through_a_usable_join(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, copies_fact_table_id: bool
) -> None:
    studio = _studio(tmp_path, orders=ORDERS, people=PEOPLE)
    sales = studio.measure("Total Sales")
    # Models often copy the measure's table ID onto every visual; it still means the join.
    table = {"table_id": studio.table("orders")} if copies_fact_table_id else {}
    answer = _page(
        "Sales by Manager",
        _visual("kpi", "cardVisual", "Total Sales", measure_id=sales, **table),
        _visual(
            "category",
            "clusteredBarChart",
            "Sales by Manager",
            measure_id=sales,
            category_column_id="manager",
            **table,
        ),
    )

    revised = studio.revise(_Model(answer), "Add a page showing sales by manager")

    # The model's own page, not the fallback's, which also groups by Manager.
    assert "Local model revision" not in caplog.text
    card, chart = revised.pages[-1].visuals
    assert card.table_id == studio.table("orders")
    assert (chart.table_id, chart.category_column_id) == (studio.table("people"), "manager")

    (relationship,) = studio.draft.semantic_model.relationships
    blocked = studio.draft.model_copy(
        update={
            "semantic_model": studio.draft.semantic_model.model_copy(
                update={
                    "relationships": [
                        relationship.model_copy(update={"blockers": ["Keys do not match"]})
                    ]
                }
            )
        }
    )
    caplog.clear()
    revised = studio.revise(_Model(answer), "Add a page showing sales by manager", blocked)

    assert "invented a category field" in caplog.text
    assert all(visual.category_column_id != "manager" for visual in revised.pages[-1].visuals)


def test_table_prefixed_column_ids_are_stripped_only_when_the_rest_is_real(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    studio = _studio(tmp_path, orders=ORDERS)
    orders, quantity = studio.table("orders"), studio.measure("Total Quantity")

    def answer(category: str) -> dict[str, Any]:
        return _page(
            "Quantity by Ship Mode",
            _visual(
                "category",
                "barChart",
                "Total Quantity by Ship Mode",
                measure_id=quantity,
                category_column_id=category,
            ),
            _visual(
                "detail",
                "tableEx",
                "Orders",
                table_id=orders,
                detail_column_ids=[f"{orders}_order_id", "quantity"],
            ),
        )

    request = "Add a new page with total quantity by ship mode"
    revised = studio.revise(_Model(answer(f"{orders}_ship_mode")), request)

    chart, table = revised.pages[-1].visuals
    assert chart.category_column_id == "ship_mode"
    assert table.detail_column_ids == ["order_id", "quantity"]

    studio.revise(_Model(answer(f"{orders}_ship_class")), request)
    assert "invented a category field" in caplog.text


def test_detail_visual_binds_the_one_table_holding_its_columns(tmp_path: Path) -> None:
    studio = _studio(tmp_path, orders=ORDERS, people=PEOPLE)
    managers = ModelRevisionProposal.model_validate(
        _page(
            "Managers",
            _visual("detail", "tableEx", "Managers", detail_column_ids=["manager", "region"]),
        )
    )
    regions = ModelRevisionProposal.model_validate(
        _page("Regions", _visual("detail", "tableEx", "Regions", detail_column_ids=["region"]))
    )

    (page,) = _expand_model_revision(managers, studio.draft, studio.catalog)["pages"]

    assert page["visuals"][0]["table_id"] == studio.table("people")
    # Orders and People both hold Region: guessing would be an invented binding.
    with pytest.raises(StudioPlanningError, match="no single reviewed table"):
        _expand_model_revision(regions, studio.draft, studio.catalog)


def test_time_measure_without_a_date_on_a_two_date_table_falls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    studio = _studio(
        tmp_path,
        orders="Order ID,Order Date,Ship Date,Region,Sales,Profit\n"
        "A-1,2025-01-01,2025-01-03,East,100.00,20.00\n"
        "A-2,2025-02-01,2025-02-04,West,250.00,40.00\n"
        "A-3,2025-03-01,2025-03-02,East,150.00,30.00\n",
    )
    answer = {
        "measures": [
            {
                "id": "model_profit_yoy",
                "name": "Model Profit YoY",
                "operation": "period_change",
                "table_id": studio.table("orders"),
                "base_measure_id": studio.measure("Total Profit"),
                "period": "year",
            }
        ]
    }

    revised = studio.revise(_Model(answer), "Create a measure for year over year profit growth %")

    # Two reviewed dates: the planner never guesses, so the prompt must ask for one.
    assert "Local model revision candidate rejected" in caplog.text
    existing = {item.id for item in studio.draft.measure_proposal.measures}
    added = [item for item in revised.measure_proposal.measures if item.id not in existing]
    assert [(item.name, item.date_column_id) for item in added] == [("Profit YoY %", "order_date")]


def test_numeric_detail_column_named_by_title_and_request_binds_its_aggregate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    studio = _studio(tmp_path, orders=ORDERS)
    # Replays a small-model answer: the metric only as a detail column.
    answer = _page(
        "Profit by Sub-Category",
        _visual(
            "category",
            "clusteredBarChart",
            "Profit by Sub-Category",
            category_column_id="sub_category",
            detail_column_ids=["profit"],
        ),
    )

    revised = studio.revise(_Model(answer), "Add a page showing profit by sub-category")

    (chart,) = revised.pages[-1].visuals
    assert (chart.measure_id, chart.category_column_id, chart.detail_column_ids) == (
        studio.measure("Total Profit"),
        "sub_category",
        [],
    )
    assert "Local model revision" not in caplog.text


def test_card_titled_by_a_field_is_retitled_and_a_titled_chart_is_completed(
    tmp_path: Path,
) -> None:
    studio = _studio(tmp_path, orders=ORDERS)
    answer = _page(
        "Ship Mode Comparison",
        _visual(
            "kpi",
            "cardVisual",
            "Average Discount by Ship Mode",
            measure_id=studio.measure("Average Discount"),
        ),
        _visual("category", "barChart", "Total Quantity by Ship Mode"),
    )

    revised = studio.revise(
        _Model(answer),
        "Add a new page comparing average discount and total quantity by ship mode",
    )

    card, chart = revised.pages[-1].visuals
    # A card shows one overall value; "by Ship Mode" would promise a breakdown.
    assert card.title == "Average Discount"
    assert (chart.measure_id, chart.category_column_id) == (
        studio.measure("Total Quantity"),
        "ship_mode",
    )


@pytest.mark.parametrize(
    ("request_text", "visual", "measures", "error"),
    [
        # A "Sales" title over a Profit column is a wrong chart, not a gap.
        (
            "Add a page showing sales by region",
            _visual(
                "category",
                "barChart",
                "Sales by Region",
                category_column_id="region",
                detail_column_ids=["profit"],
            ),
            [],
            "Dashboard analytical visual requires a measure",
        ),
        # Profit has only a SUM: an "Average Profit" chart would plot totals.
        (
            "Add a page showing average profit by region",
            _visual(
                "category",
                "barChart",
                "Average Profit by Region",
                category_column_id="region",
                detail_column_ids=["profit"],
            ),
            [],
            "Dashboard analytical visual requires a measure",
        ),
        # A ratio title is not the column's plain aggregate either.
        (
            "Add a page showing profit per order by region",
            _visual(
                "category",
                "barChart",
                "Profit per Order by Region",
                category_column_id="region",
                detail_column_ids=["profit"],
            ),
            [],
            "Dashboard analytical visual requires a measure",
        ),
        # Two aggregates on Discount and a title that names neither.
        (
            "Add a page showing discount by region",
            _visual(
                "category",
                "barChart",
                "Discount by Region",
                category_column_id="region",
                detail_column_ids=["discount"],
            ),
            [{"id": "total_discount", "name": "Total Discount", "operation": "sum"}],
            "Dashboard analytical visual requires a measure",
        ),
        # The title names Total Quantity, but the user asked about sales.
        (
            "Add a page showing total sales by ship mode",
            _visual(
                "category",
                "barChart",
                "Total Quantity by Ship Mode",
                category_column_id="ship_mode",
            ),
            [],
            "Dashboard analytical visual requires a measure",
        ),
        # Order ID is a key: the designer never offers it as a category.
        (
            "Add a page showing sales by order",
            _visual(
                "category", "barChart", "Sales by Order ID", detail_column_ids=["order_id", "sales"]
            ),
            [],
            "Dashboard grouped visual requires a category field",
        ),
        # A trend axis is a date, never a text detail column.
        (
            "Add a page showing the sales trend",
            _visual(
                "trend",
                "lineChart",
                "Sales trend",
                measure_id="sum_sales",
                detail_column_ids=["ship_mode"],
            ),
            [],
            "Dashboard grouped visual requires a category field",
        ),
    ],
    ids=[
        "other-metric",
        "other-aggregation",
        "derived-metric",
        "two-aggregates",
        "unrequested-title",
        "key-category",
        "text-trend",
    ],
)
def test_ambiguous_model_visual_gaps_stay_rejected(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    request_text: str,
    visual: dict[str, Any],
    measures: list[dict[str, Any]],
    error: str,
) -> None:
    studio = _studio(tmp_path, orders=ORDERS)
    orders = studio.table("orders")
    if visual.get("measure_id") == "sum_sales":
        visual = {**visual, "measure_id": studio.measure("Total Sales")}
    answer = {
        **_page("Model Answer", visual),
        "measures": [{**item, "table_id": orders, "column_id": "discount"} for item in measures],
    }

    revised = studio.revise(_Model(answer), request_text)

    assert error in caplog.text
    assert "Model Answer" not in _added_pages(studio, revised)


@pytest.mark.parametrize(
    ("request_text", "visuals"),
    [
        # A YoY request answered without any YoY measure.
        (
            "Add a page showing year over year sales",
            [
                _visual("kpi", "cardVisual", "Total Sales", measure_id="sum_sales"),
                _visual(
                    "trend",
                    "lineChart",
                    "Sales over time",
                    measure_id="sum_sales",
                    category_column_id="order_date",
                ),
            ],
        ),
        # A "trend" over Region is a category chart in disguise.
        (
            "Add a page showing the sales trend",
            [
                _visual(
                    "trend",
                    "areaChart",
                    "Sales trend",
                    measure_id="sum_sales",
                    category_column_id="region",
                )
            ],
        ),
        # "By sub-category" answered by one overall card.
        (
            "Add a page showing profit by sub-category",
            [_visual("kpi", "cardVisual", "Profit by Sub-Category", measure_id="sum_profit")],
        ),
        # Grouped by Category, which the longer Sub-Category name does not mean.
        (
            "Add a page showing profit by sub-category",
            [
                _visual(
                    "category",
                    "barChart",
                    "Profit by Category",
                    measure_id="sum_profit",
                    category_column_id="category",
                )
            ],
        ),
    ],
    ids=["yoy-without-yoy", "text-trend-axis", "card-for-grouping", "wrong-grouping"],
)
def test_gate_rejects_valid_answers_that_ignore_the_request_and_logs_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    request_text: str,
    visuals: list[dict[str, Any]],
) -> None:
    studio = _studio(tmp_path, orders=ORDERS)
    ids = {"sum_sales": studio.measure("Total Sales"), "sum_profit": studio.measure("Total Profit")}
    answer = _page(
        "Model Answer",
        *({**visual, "measure_id": ids[visual["measure_id"]]} for visual in visuals),
    )

    revised = studio.revise(_Model(answer), request_text)

    assert "did not satisfy the request" in caplog.text
    assert "Model Answer" not in _added_pages(studio, revised)


def test_yoy_page_may_show_the_drafts_own_yoy_measure(tmp_path: Path) -> None:
    studio = _studio(tmp_path, orders=ORDERS)
    yoy = GovernedMeasureSpec(
        id="sales_yoy_pct",
        name="Sales YoY %",
        operation="period_change",
        table_id=studio.table("orders"),
        base_measure_id=studio.measure("Total Sales"),
        date_column_id="order_date",
        period="year",
        format_string="0.0%",
    )
    draft = studio.draft.model_copy(
        update={
            "measure_proposal": MeasureProposal(
                measures=[*studio.draft.measure_proposal.measures, yoy]
            )
        }
    )
    answer = _page(
        "Sales YoY by Region",
        _visual(
            "category",
            "barChart",
            "Sales YoY % by Region",
            measure_id=yoy.id,
            category_column_id="region",
        ),
    )

    revised = studio.revise(_Model(answer), "Add a page showing Sales YoY % by region", draft)

    # Reusing the reviewed YoY measure answers the request; restating it would collide.
    assert revised.pages[-1].title == "Sales YoY by Region"
    assert revised.measure_proposal.measures == draft.measure_proposal.measures


def test_measure_only_answer_to_a_grouped_yoy_request_is_accepted(tmp_path: Path) -> None:
    studio = _studio(tmp_path, orders=ORDERS)
    answer = {
        "measures": [
            {
                "id": "model_sales_yoy",
                "name": "Model Sales YoY",
                "operation": "period_change",
                "table_id": studio.table("orders"),
                "base_measure_id": studio.measure("Total Sales"),
                "date_column_id": "order_date",
                "period": "year",
            }
        ]
    }

    revised = studio.revise(
        _Model(answer), "Create a measure for year over year sales growth % by region"
    )

    # "By region" asks how to read it; a measure alone still answers a measure request.
    assert any(item.id == "model_sales_yoy" for item in revised.measure_proposal.measures)


@pytest.mark.parametrize(
    ("request_text", "model_period", "expected"),
    [
        ("Create a measure for year over year sales growth %", None, "year"),
        ("Add a month over month growth measure for sales", "year", "month"),
        ("Add a sales growth measure", None, "year"),
    ],
)
def test_the_period_the_request_names_wins_over_the_models(
    tmp_path: Path, request_text: str, model_period: str | None, expected: str
) -> None:
    # An omitted period used to fall back to the schema's "month": a YoY request then
    # produced a month-over-month measure under a YoY name.
    studio = _studio(tmp_path, orders=ORDERS)
    measure = {
        "id": "model_sales_change",
        "name": "Model Sales Change",
        "operation": "period_change",
        "table_id": studio.table("orders"),
        "base_measure_id": studio.measure("Total Sales"),
        "date_column_id": "order_date",
    }
    if model_period is not None:
        measure["period"] = model_period

    revised = studio.revise(_Model({"measures": [measure]}), request_text)

    added = next(item for item in revised.measure_proposal.measures if item.id == measure["id"])
    assert added.period == expected
