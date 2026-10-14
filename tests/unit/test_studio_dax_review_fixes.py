"""DAX Lab completions fire only on requests that state them (review of I2, I3 and I6).

A wrong number that looks plausible is the worst outcome, so when the wording is in doubt
the model's answer or a repair turn wins over a silent rewrite. Superstore-shaped data:
Returns flags orders, People maps each Region to a manager.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.knowledge.library import KnowledgeLibrary
from ai_powerbi_analyst.llm.runtime import LocalModelError
from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    CreateWorkspaceRequest,
    DaxAssistRequest,
    DaxEvaluateRequest,
    DraftRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.dax_assistant import (
    _filter_text,
    _flag_share_formula,
    _in_requested_order,
    _missing_filters,
    _named_share_formula,
    _order_free_words,
    _request_gap,
    _share_part,
    dax_assistant_events,
)
from ai_powerbi_analyst.studio.dax_plan import GroupedDaxPlan
from ai_powerbi_analyst.studio.planner import StudioPlanningService
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

# Sales 700: Consumer 270, West 400, Technology 490 (West 250), Furniture in the East 60
# (Corporate 40). Orders A-2 and A-4 were returned.
ORDERS = (
    "Order ID,Region,Segment,Category,Ship Mode,Sales,Quantity,Discount,Profit\n"
    "A-1,East,Consumer,Technology,Same Day,100,2,0,20\n"
    "A-1,East,Consumer,Furniture,Same Day,20,1,0.3,-5\n"
    "A-2,West,Corporate,Technology,Standard Class,250,3,0,50\n"
    "A-3,West,Consumer,Furniture,Second Class,150,1,0.2,10\n"
    "A-4,Central,Home Office,Technology,Standard Class,80,4,0,8\n"
    "A-5,East,Corporate,Technology,Second Class,60,2,0.1,6\n"
    "A-6,East,Corporate,Furniture,Standard Class,40,1,0,4\n"
)
S = "SUM('Orders'[Sales])"
X = "DISTINCTCOUNT('Orders'[Order ID])"
WEST = "'Orders'[Region] = \"West\""
CONSUMER = "'Orders'[Segment] = \"Consumer\""
TECHNOLOGY = "'Orders'[Category] = \"Technology\""
RETURNED = "'Returns'[Returned] = TRUE()"
SELF_RATIO = f"DIVIDE({S}, {S})"


def _workspace(root: Path, files: dict[str, str], key: str) -> tuple[StudioService, str]:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    service = StudioService(Path(__file__).resolve().parents[2], root / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name=key,
            selected_folder=str(root),
            idempotency_key=f"{key}-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(root / name) for name in files],
            idempotency_key=f"{key}-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            idempotency_key=f"{key}-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    draft = service.draft(service.workspace(workspace.id).active_draft_id)
    service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[item.id for item in draft.semantic_model.relationships],
            idempotency_key=f"{key}-approve",
            expected_base_hash=draft.base_hash,
        ),
    )
    return service, workspace.id


@pytest.fixture(scope="module")
def superstore(tmp_path_factory: pytest.TempPathFactory) -> tuple[StudioService, str]:
    return _workspace(
        tmp_path_factory.mktemp("review-fixes"),
        {
            "Orders.csv": ORDERS,
            "Returns.csv": "Returned,Order ID\nYes,A-2\nYes,A-4\n",
            "People.csv": "Person,Region\nAnn,East\nBob,West\nCy,Central\n",
        },
        "review-fixes",
    )


@pytest.fixture(scope="module")
def snapshot(superstore):
    service, workspace_id = superstore
    return service.snapshot(service.workspace(workspace_id).active_snapshot_id)


def _ask(
    service: StudioService,
    workspace_id: str,
    goal: str,
    answers: list[str],
    *,
    discussion: str = "",
) -> tuple[str, list[str]]:
    """The shipped formula and the model turns (prompt, then each rejection).

    A guarded-model stand-in with one repair turn; answers starting with "{" are plans.
    """
    seen: list[str] = []

    def generate(system, message, response_model, **kwargs):
        # These scripted DAX corrections cover the retained freeform fallback.
        # Decline the separate closed selector rather than feed it expression JSON.
        if "pattern" in response_model.model_fields:
            return response_model(pattern=None)
        seen.append(system)
        for index, answer in enumerate(answers[:2]):
            try:
                if answer.startswith("{"):
                    return response_model.model_validate_json(answer)
                return response_model(expression=answer)
            except ValueError as error:
                seen.append(str(error))
                if index == 1:
                    raise LocalModelError(
                        "Local model failed strict schema/artifact validation: " + str(error)
                    ) from error
        raise AssertionError("no answers")

    service.model = SimpleNamespace(generate=generate, unload=lambda: None)
    events = dax_assistant_events(
        service,
        DaxAssistRequest(
            workspace_id=workspace_id,
            snapshot_id=service.workspace(workspace_id).active_snapshot_id,
            request_id="review-" + str(abs(hash((goal, discussion, *answers)))),
            goal=goal,
            discussion=discussion,
        ),
        Event(),
    )
    text = "".join(e["data"].get("text", "") for e in events if e["event"] == "token")
    return text.split("```dax\n")[1].split("\n```")[0], seen


def _value(service: StudioService, workspace_id: str, expression: str) -> object:
    rows = service.evaluate_dax(
        DaxEvaluateRequest(workspace_id=workspace_id, expression=expression, top_n=20)
    ).rows
    return rows if len(rows) != 1 else next(iter(rows[0].values()))


# --- F1: share wording counts only as the request's noun -------------------------------


NOT_SHARES = [
    # The verb, a negation, or wording that is no share of this request.
    ("Show total sales from the West region to share with my manager", WEST),
    ("Show total sales from the West region so I can share it", WEST),
    ("Show total sales from the Consumer segment rather than its share", CONSUMER),
    ("Show total sales that come from the Consumer segment rather than the share", CONSUMER),
    ("Show total sales from the Consumer segment instead of the percentage of total", CONSUMER),
    ("Show total sales that come from the Consumer segment, not as a share", CONSUMER),
    ("Calculate total sales from the West region, the share comes later", WEST),
    ("Show total sales from the West region for the market share report", WEST),
    ("Show total sales from the West region with a discount above 20% of list price", WEST),
    ("Show sales from the Technology category (a large portion of our revenue)", TECHNOLOGY),
    ("Show total sales from the West region, which has the biggest share", WEST),
]


@pytest.mark.parametrize(("goal", "part"), NOT_SHARES)
def test_a_total_is_never_rewritten_into_a_share(snapshot, goal, part) -> None:
    total = f"CALCULATE({S}, {part})"
    assert _share_part(goal, snapshot) is None, goal
    assert _named_share_formula(goal, total, snapshot) is None, goal
    assert _named_share_formula(goal, f"CALCULATE({SELF_RATIO}, {part})", snapshot) is None
    assert _request_gap(goal, total, snapshot, "", False) is None, goal


def test_the_models_total_ships_when_the_request_is_no_share(superstore) -> None:
    service, workspace_id = superstore
    for goal, part, expected in (
        ("Show total sales from the Consumer segment rather than the share", CONSUMER, 270),
        ("Show total sales from the Consumer segment, not as a share", CONSUMER, 270),
        (
            "Show total sales from the Consumer segment instead of the percentage of total",
            CONSUMER,
            270,
        ),
        ("Show total sales from the West region to share with my manager", WEST, 400),
        ("Show total sales from the West region so I can share it", WEST, 400),
        ("Show total sales from the West region to share with the team", WEST, 400),
        ("Calculate total sales from the West region, the share comes later", WEST, 400),
        ("Show total sales from the West region for the market share report", WEST, 400),
        ("Show total sales from the West region, which has the biggest share", WEST, 400),
    ):
        total = f"CALCULATE({S}, {part})"
        formula, seen = _ask(service, workspace_id, goal, [total, "unused"])
        # Before, both became a share with no repair turn: 0.386 and 0.571.
        assert formula == total and len(seen) == 1, goal
        assert _value(service, workspace_id, formula) == expected


def test_a_column_named_share_is_no_share_wording(tmp_path: Path) -> None:
    service, workspace_id = _workspace(
        tmp_path,
        {
            "Markets.csv": "Market,Region,Market Share,Revenue\n"
            "M1,West,0.2,100\nM2,West,0.3,200\nM3,East,0.1,50\nM4,East,0.1,300\n"
        },
        "market-share",
    )
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    total = "CALCULATE(SUM('Markets'[Market Share]), 'Markets'[Region] = \"West\")"
    for goal in (
        "Show the sum of Market Share for markets from the West region",
        "Total Market Share that comes from the West region",
        "Calculate total Market Share from the West region",
    ):
        assert _share_part(goal, snapshot) is None, goal
        formula, seen = _ask(service, workspace_id, goal, [total, "unused"])
        assert formula == total and len(seen) == 1, goal
        assert _value(service, workspace_id, formula) == pytest.approx(0.5)  # not 0.714


def test_a_single_aggregate_for_a_share_gets_a_repair_turn_not_a_rewrite(superstore) -> None:
    service, workspace_id = superstore
    goal = "Consumer share of sales"
    share = f"DIVIDE(CALCULATE({S}, {CONSUMER}), {S})"
    for plain in (S, f"CALCULATE({S}, {CONSUMER})"):
        formula, seen = _ask(service, workspace_id, goal, [plain, share])
        assert formula == share and len(seen) == 2, plain
        assert "asks for a share" in seen[1] and share in seen[1]
    # The model insists on the total: no answer rather than a rewritten one.
    with pytest.raises(ValueError, match="did not pass local validation"):
        _ask(service, workspace_id, goal, [S, S])
    assert _value(service, workspace_id, share) == pytest.approx(270 / 700)


@pytest.mark.parametrize(
    ("goal", "expected", "value"),
    [
        # D10 and its question form.
        (
            "Share of total sales that comes from the Consumer segment",
            f"DIVIDE(CALCULATE({S}, {CONSUMER}), {S})",
            270 / 700,
        ),
        (
            "What share of total sales comes from the Consumer segment?",
            f"DIVIDE(CALCULATE({S}, {CONSUMER}), {S})",
            270 / 700,
        ),
        ("Consumer share of sales", f"DIVIDE(CALCULATE({S}, {CONSUMER}), {S})", 270 / 700),
        # Another named value in a scope position filters the whole share.
        (
            "What share of Technology sales comes from the West region?",
            f"CALCULATE(DIVIDE(CALCULATE({S}, {WEST}), {S}), {TECHNOLOGY})",
            250 / 490,
        ),
        (
            "What proportion of Furniture sales comes from the Corporate segment in the East "
            "region?",
            # People lists East as well: the same region through the relationship.
            "CALCULATE(DIVIDE(CALCULATE(SUM('Orders'[Sales]), 'Orders'[Segment] = "
            "\"Corporate\"), SUM('Orders'[Sales])), 'Orders'[Region] = \"East\", "
            "'Orders'[Category] = \"Furniture\", 'People'[Region] = \"East\")",
            40 / 60,
        ),
    ],
)
def test_a_constant_ratio_for_an_explicit_share_is_completed(
    superstore, goal, expected, value
) -> None:
    service, workspace_id = superstore
    formula, seen = _ask(service, workspace_id, goal, [SELF_RATIO, "unused"])
    assert formula == expected and len(seen) == 1
    assert _value(service, workspace_id, formula) == pytest.approx(value)


def test_other_share_answers_ship_as_written(superstore) -> None:
    service, workspace_id = superstore
    consumer = f"DIVIDE(CALCULATE({S}, {CONSUMER}), {S})"
    scoped = f"CALCULATE({consumer}, {WEST})"
    # The refinement of an earlier share keeps the model's scoped share.
    for goal in (
        "Now show the same share for the West region only",
        "Apply the West region filter to the same share",
        "Keep the same Consumer share in the West region",
    ):
        formula, seen = _ask(
            service,
            workspace_id,
            goal,
            [scoped, "unused"],
            discussion=f"User: Consumer share of sales\nAssistant: ```dax\n{consumer}\n```",
        )
        assert formula == scoped and len(seen) == 1, goal
        assert _value(service, workspace_id, formula) == pytest.approx(150 / 400)
    # A ratio of two values gets the named filter around the whole ratio.
    margin = "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))"
    formula, seen = _ask(
        service,
        workspace_id,
        "Profit as a percentage of sales for the Consumer segment",
        [margin, "unused"],
    )
    assert formula == f"CALCULATE({margin}, {CONSUMER})" and len(seen) == 1
    assert _value(service, workspace_id, formula) == pytest.approx(25 / 270)


# --- F2: a scoping "from" value is not the part ----------------------------------------


@pytest.mark.parametrize(
    "goal",
    [
        "Calculate the percentage of orders from the West region shipped Same Day",
        "What share of sales from the Consumer segment had a discount above 20%?",
        "Calculate the percentage of orders from the West region that were returned",
        # Explicit wording, but another condition may be the part.
        "What percentage of orders that came from the West region were Same Day?",
        "What share of total sales comes from the Consumer segment above 100?",
        "What share of total sales comes from the West region for returned orders?",
        "Profit as a percentage of total sales for the Consumer segment",
    ],
)
def test_a_share_with_another_condition_is_left_to_the_model(snapshot, goal) -> None:
    assert _share_part(goal, snapshot) is None
    for constant in (SELF_RATIO, f"DIVIDE({X}, {X})"):
        assert _named_share_formula(goal, constant, snapshot) is None


def test_a_scoped_percentage_gets_a_repair_turn_or_ships_the_models_answer(superstore) -> None:
    service, workspace_id = superstore
    same_day = "'Orders'[Ship Mode] = \"Same Day\""
    right = f"CALCULATE(DIVIDE(CALCULATE({X}, {same_day}), {X}), {WEST})"
    # Before: West's share of Same Day orders, with no repair turn.
    for goal in (
        "Calculate the percentage of orders from the West region shipped Same Day",
        "What percentage of orders in the West region shipped Same Day?",
        "Show the percentage of West region orders shipped Same Day",
    ):
        formula, seen = _ask(service, workspace_id, goal, [f"DIVIDE({X}, {X})", right])
        assert formula == right and len(seen) == 2, goal
        assert "always 1" in seen[1] and "DIVIDE(CALCULATE(x, condition), x)" in seen[1]
        # DISTINCTCOUNT on the empty Same Day numerator is BLANK, as in Power BI.
        assert _value(service, workspace_id, formula) is None
    standard = "'Orders'[Ship Mode] = \"Standard Class\""
    standard_rate = f"CALCULATE(DIVIDE(CALCULATE({X}, {standard}), {X}), {WEST})"
    for goal in (
        "Calculate the percentage of orders from the West region shipped Standard Class",
        "What percentage of orders in the West region shipped Standard Class?",
        "Show the percentage of West region orders shipped Standard Class",
    ):
        formula, seen = _ask(service, workspace_id, goal, [f"DIVIDE({X}, {X})", standard_rate])
        assert formula == standard_rate and len(seen) == 2, goal
        assert _value(service, workspace_id, formula) == pytest.approx(1 / 2)
    # A count for a scoped percentage ships as the model wrote it, as before I2; the
    # rewrite gave the West share of returned orders instead of the West return rate.
    count = f"CALCULATE({X}, {RETURNED}, {WEST})"
    goal = "Calculate the percentage of orders from the West region that were returned"
    formula, seen = _ask(service, workspace_id, goal, [count, "unused"])
    assert formula == count and len(seen) == 1


def test_a_flag_only_scopes_an_explicit_part(superstore, snapshot) -> None:
    goal = "What share of returned orders came from the West region?"
    # The flag is not the part here: the constant ratio goes back to the model.
    assert _flag_share_formula(goal, f"DIVIDE({X}, {X})", snapshot) is None
    assert (
        _flag_share_formula("What share of orders were returned?", f"DIVIDE({X}, {X})", snapshot)
        == f"DIVIDE(CALCULATE({X}, {RETURNED}), {X})"
    )
    service, workspace_id = superstore
    right = f"CALCULATE(DIVIDE(CALCULATE({X}, {WEST}), {X}), {RETURNED})"
    formula, seen = _ask(service, workspace_id, goal, [f"DIVIDE({X}, {X})", right])
    assert formula == right and len(seen) == 2
    assert _value(service, workspace_id, formula) == pytest.approx(1 / 2)


@pytest.mark.parametrize(
    "goal",
    [
        "Show total sales on returned orders to share with my manager",
        "Show total sales on returned orders so I can share it",
        "Show total sales on returned orders to share with the team",
    ],
)
def test_share_as_a_verb_is_no_rate_of_a_flag(superstore, goal) -> None:
    service, workspace_id = superstore
    formula, seen = _ask(service, workspace_id, goal, [S, "unused"])
    # The flag filters the total. Before, the verb made it a rate request: no completion,
    # and a repair hint asking for DIVIDE(CALCULATE(x, flag), x).
    assert formula == f"CALCULATE({S}, {RETURNED})" and len(seen) == 1
    assert _value(service, workspace_id, formula) == 330


# --- F3: an order-only rewording is not a repeated formula -----------------------------

BY_REGION = {"table": "Orders", "group_by": ["Region"], "operation": "sum", "value_column": "Sales"}
REGION_DESC = json.dumps(
    {**BY_REGION, "sort_by": [{"column": "Region", "direction": "desc"}], "grand_total": "last"}
)
BY_VALUE = (
    "EVALUATE SUMMARIZECOLUMNS(ROLLUPADDISSUBTOTAL(ROLLUPGROUP('Orders'[Region]), "
    '"IsGrandTotal"), "Total Sales", SUM(\'Orders\'[Sales])) '
    "ORDER BY [IsGrandTotal] ASC, [Total Sales] DESC"
)


@pytest.mark.parametrize(
    ("earlier", "goal"),
    [
        ("Total sales by region", "Total sales by region, highest first"),
        (
            "Total sales by region, highest first",
            "Show total sales by region from highest to lowest",
        ),
        (
            "Total sales by region, highest first",
            "Total sales per region sorted largest to smallest",
        ),
    ],
)
def test_a_reworded_order_follow_up_is_not_a_repeat(superstore, earlier, goal) -> None:
    service, workspace_id = superstore
    discussion = f"User: {earlier}\nAssistant: ```dax\n{BY_VALUE}\n```"
    # The app sorts the plan by value, which reproduces the earlier query: before, that
    # was rejected twice as "the formula for the earlier request".
    formula, seen = _ask(service, workspace_id, goal, [REGION_DESC, "{}"], discussion=discussion)
    assert formula == BY_VALUE and len(seen) == 1


def test_a_copied_formula_for_another_request_is_still_rejected(superstore) -> None:
    service, workspace_id = superstore
    discussion = f"User: Total sales by region\nAssistant: ```dax\n{BY_VALUE}\n```"
    profit = json.dumps({**json.loads(REGION_DESC), "value_column": "Profit"})
    formula, seen = _ask(
        service,
        workspace_id,
        "Total profit by region, highest first",
        [REGION_DESC, profit],
        discussion=discussion,
    )
    assert "earlier request 'Total sales by region'" in seen[1]
    assert formula == BY_VALUE.replace("Sales", "Profit")


def test_order_free_words_keep_the_calculation() -> None:
    same = _order_free_words("Total sales by region")
    for goal in (
        "Total sales by region, highest first",
        "Show total sales by region from highest to lowest",
        "Total sales per region sorted largest to smallest",
        "Total sales for each region in descending order",
        "Total sales by region, A to Z",
    ):
        assert _order_free_words(goal) == same, goal
    for goal in (
        "Total profit by region, highest first",
        "Maximum sales by region, highest first",
        "Total sales by region in 2016, highest first",
    ):
        assert _order_free_words(goal) != same, goal


# --- F4: recipes rank by the whole request ---------------------------------------------

SUPERSTORE_NAMES = [
    *("Orders", "Row ID", "Order ID", "Order Date", "Ship Date", "Ship Mode", "Customer ID"),
    *("Customer Name", "Segment", "Region", "Category", "Sub-Category", "Product Name"),
    *("Sales", "Quantity", "Discount", "Profit", "Returns", "Returned", "People", "Person"),
]
SMALL_NAMES = ["Kund's Orders", "Order ID", "Customer's Name", "Größe", "Returns", "Returned"]


@pytest.mark.parametrize("names", [SUPERSTORE_NAMES, SMALL_NAMES], ids=["superstore", "small"])
def test_the_return_rate_recipe_answers_a_returned_percentage(names) -> None:
    library = KnowledgeLibrary()

    def recipes(goal: str) -> list[str]:
        receipt = library.retrieve(f"DAX {goal}\n", max_characters=900, context_terms=names)
        return [r.id for r in receipt.references if r.kind == "recipe"]

    # Without the workspace's names the documentation query was "percentage" alone, and
    # the profit-margin recipe took the one recipe slot.
    assert recipes("Calculate the percentage of orders that were returned") == [
        "recipe-return-rate"
    ]
    assert recipes("Percentage of orders that were returned in the West region") == [
        "recipe-return-rate"
    ]
    assert recipes("Profit margin as a percentage of sales") == ["recipe-ratio"]


# --- F5: name wording sorts by the grouping field --------------------------------------


def _plan(*sorts: str, **changes: object) -> GroupedDaxPlan:
    sort_by = [dict(zip(("column", "direction"), s.rsplit(" ", 1), strict=True)) for s in sorts]
    return GroupedDaxPlan(**{**BY_REGION, "sort_by": sort_by, **changes})


def _keys(plan: GroupedDaxPlan) -> list[str]:
    return [f"{sort.column} {sort.direction}" for sort in plan.sort_by]


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("Show total sales by region in descending alphabetical order", ["Region desc"]),
        ("Total sales by region, descending by region name", ["Region desc"]),
        ("Total sales by region, ordered descending by region", ["Region desc"]),
        ("Total sales by region, sorted descending by name", ["Region desc"]),
        ("Total sales by region in reverse alphabetical order", ["Region desc"]),
        ("Total sales by region, Z to A", ["Region desc"]),
        ("Total sales by region, A to Z", ["Region asc"]),
        ("Total sales by region, alphabetically", ["Region asc"]),
        ("Total sales by region sorted by region", ["Region asc"]),
    ],
)
def test_name_wording_sorts_by_the_grouping_field(snapshot, goal, expected) -> None:
    # The model sorted by value; the request names the order of the region names.
    assert _keys(_in_requested_order(goal, _plan("value desc"), snapshot)) == expected
    already = _plan(*expected, "value desc")
    assert _in_requested_order(goal, already, snapshot) is already


@pytest.mark.parametrize(
    "goal",
    [
        # A ranking by another field is not an order of the plan's value.
        "Sales by region, lowest discount first",
        "Sales by region, with the most discounted region first",
        "Total sales by region, sorted by discount descending",
        "Total sales by region, highest to lowest quantity",
        # Negated after the cue, or contradicted by the name wording.
        "Show sales by region; highest first is not needed",
        "Sales by region ascending, highest first",
        "Total sales by region, alphabetical, highest first",
    ],
)
def test_other_order_wording_keeps_the_models_order(snapshot, goal) -> None:
    plan = _plan("Region desc")
    assert _in_requested_order(goal, plan, snapshot) is plan


def test_a_ranking_by_the_plans_own_value_still_sorts_it(snapshot) -> None:
    plan = _plan("Region asc", value_column="Profit")
    ordered = _in_requested_order("Profit by region, least profitable first", plan, snapshot)
    assert _keys(ordered) == ["value asc"]


def test_descending_alphabetical_order_ships_names_descending(superstore) -> None:
    service, workspace_id = superstore
    plan = json.dumps({**BY_REGION, "sort_by": [{"column": "value", "direction": "desc"}]})
    goal = "Show total sales by region in descending alphabetical order"
    formula, seen = _ask(service, workspace_id, goal, [plan, "{}"])
    assert formula.endswith(" ORDER BY 'Orders'[Region] DESC") and len(seen) == 1
    # By value it would be West, East, Central.
    assert [row["Region"] for row in _value(service, workspace_id, formula)] == [
        "West",
        "East",
        "Central",
    ]


# --- F6: DAX string literals escape double quotes --------------------------------------


def test_filter_values_with_double_quotes_are_escaped(tmp_path: Path) -> None:
    assert _filter_text("Orders", "Segment", 'Home "Office"') == (
        '\'Orders\'[Segment] = "Home ""Office"""'
    )
    service, workspace_id = _workspace(
        tmp_path,
        {"Orders.csv": 'Segment,Sales\n"Home ""Office""",100\n"Home ""Office""",20\nConsumer,50\n'},
        "quoted-values",
    )
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    goal = 'Total sales for the Home "Office" segment'
    filtered = 'CALCULATE(SUM(\'Orders\'[Sales]), \'Orders\'[Segment] = "Home ""Office""")'
    # The escaped literal counts as the requested filter.
    assert _missing_filters(goal, filtered, snapshot) == []
    # Before, the completion wrote "Home "Office"", which does not parse.
    formula, seen = _ask(service, workspace_id, goal, ["SUM('Orders'[Sales])", "unused"])
    assert formula == filtered and len(seen) == 1
    assert _value(service, workspace_id, formula) == 120
