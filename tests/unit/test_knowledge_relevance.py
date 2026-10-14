"""Retrieval cites a reference only when the question is about it (issue I6).

The decoy pages reproduce what the old gate cited for catalog questions on generic words:
"does" + "contain", the user's own table names, "calculate", "Power Query" and "return".
"""

import json
from pathlib import Path

import pytest

from ai_powerbi_analyst.knowledge.archive import compile_collection, sha
from ai_powerbi_analyst.knowledge.library import KnowledgeLibrary, RetrievalReceipt
from ai_powerbi_analyst.studio.contracts import CreateWorkspaceRequest, IngestRequest
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

SUPERSTORE = [
    *("Orders", "Row ID", "Order ID", "Order Date", "Ship Date", "Ship Mode", "Customer ID"),
    *("Customer Name", "Segment", "Region", "Category", "Sub-Category", "Product Name"),
    *("Sales", "Quantity", "Discount", "Profit", "Returns", "Returned", "People", "Person"),
]
CALENDAR = ["Calendar", "Year", "Month", "Quarter", "Sales"]
TENANT = (
    "Tenant settings index",
    "power-bi/admin/service-admin-portal",
    "general",
    [("Sharing", "Tenant settings let people share items connected to orders and apps.")],
)
ARCGIS = (
    "Create ArcGIS maps",
    "power-bi/visuals/power-bi-visualization-arcgis",
    "report_design",
    [
        (
            "Which data does each map show",
            "Each layer does show which field would drive the age distribution and gender "
            "of every customer.",
        )
    ],
)
# Only its heading "Date" anchors it; "shipments" is in its text.
ROLE_PLAYING = (
    "Role-playing dimensions",
    "power-bi/guidance/star-schema-role-playing",
    "modeling",
    [("Date", "Use one copy of the dimension per role, for example order and shipments.")],
)
PAGES = [
    TENANT,
    ARCGIS,
    ROLE_PLAYING,
    (
        "Google BigQuery",
        "power-query/connectors/google-bigquery",
        "general",
        [("Nested fields", "Nested fields which repeat can reduce performance.")],
    ),
    (
        "Financial functions",
        "dax/financial-functions-dax",
        "dax",
        [("Overview", "Financial functions give the rate of return and yield of an investment.")],
    ),
    (
        "Appropriate use of error functions",
        "dax/best-practices/dax-error-functions",
        "dax",
        [("Recommendations", "A Profit Margin measure would use DIVIDE instead of IFERROR.")],
    ),
    (
        "Create key influencers visualizations",
        "power-bi/visuals/power-bi-visualization-influencers",
        "report_design",
        [("How do you calculate segments", "Segments come from running groups of data points.")],
    ),
    (
        "Dealing with errors in Power Query",
        "power-query/dealing-with-errors",
        "power_query_m",
        [("Remove errors", "To remove rows with errors from a query, choose Remove errors.")],
    ),
    (
        "Use variables to improve your DAX formulas",
        "dax/best-practices/dax-variables",
        "dax",
        [
            (
                "Improve performance",
                "The Sales YoY Growth % measure stores the prior year once: VAR Prior = "
                "CALCULATE([Sales], PARALLELPERIOD('Date'[Date], -12, MONTH)) "
                "RETURN DIVIDE([Sales] - Prior, Prior)",
            )
        ],
    ),
    (
        "Set and use date tables in Power BI Desktop",
        "power-bi/transform-model/desktop-date-tables",
        "modeling",
        [("Mark as date table", "A date table has one row per calendar date.")],
    ),
    (
        "DAX glossary",
        "dax/dax-glossary",
        "dax",
        [("Calculated column", "A calculated column is evaluated row by row, unlike a measure.")],
    ),
    (
        "Table.RemoveLastN",
        "powerquery-m/table-removelastn",
        "power_query_m",
        [
            ("Syntax", "Table.RemoveLastN(table as table, optional count as any) as table"),
            ("About", "Gives a table that does not contain the last rows of each table."),
        ],
    ),
    (
        "Table.Contains",
        "powerquery-m/table-contains",
        "power_query_m",
        [
            ("Syntax", "Table.Contains(table as table, record as record) as logical"),
            ("About", "Indicates whether the record appears as a row in the table."),
        ],
    ),
    (
        "Date.Day",
        "powerquery-m/date-day",
        "power_query_m",
        [("About", "Gives the day component of a date, such as orders shipped the same day.")],
    ),
    (
        "Lines.ToBinary",
        "powerquery-m/lines-tobinary",
        "power_query_m",
        [("About", "Converts order lines with a discount into a binary value.")],
    ),
    (
        "ROW function",
        "dax/row-function-dax",
        "dax",
        [
            ("Syntax", "ROW(<name>, <expression>)"),
            ("Remarks", "Gives a table with a single row of sales for each named expression."),
        ],
    ),
    (
        "RATE",
        "dax/rate-function-dax",
        "dax",
        [("Syntax", "RATE(<nper>, <pmt>, <pv>)"), ("Remarks", "The interest rate per period.")],
    ),
    (
        "VALUES",
        "dax/values-function-dax",
        "dax",
        [
            ("Syntax", "VALUES(<TableNameOrColumnName>)"),
            ("Remarks", "Gives a one-column table, plus a blank row for invalid relationships."),
        ],
    ),
    (
        "CONTAINS",
        "dax/contains-function-dax",
        "dax",
        [
            ("Syntax", "CONTAINS(<table>, <columnName>, <value>)"),
            ("Remarks", "Is TRUE when each named column holds the given value."),
        ],
    ),
    (
        "ALL",
        "dax/all-function-dax",
        "dax",
        [("Syntax", "ALL([<table> | <column>])"), ("Remarks", "Clears filters on a table.")],
    ),
    (
        "ALLSELECTED",
        "dax/allselected-function-dax",
        "dax",
        [
            ("Syntax", "ALLSELECTED([<tableName> | <columnName>])"),
            ("Remarks", "Keeps filters from outside the visual."),
        ],
    ),
    (
        "DISTINCT (table)",
        "dax/distinct-function-dax",
        "dax",
        [("Syntax", "DISTINCT(<table>)"), ("Remarks", "Gives the distinct rows of a table.")],
    ),
    (
        "DISTINCT (column)",
        "dax/distinct-column-function-dax",
        "dax",
        [("Syntax", "DISTINCT(<column>)"), ("Remarks", "Gives the distinct values of a column.")],
    ),
    (
        "DISTINCTCOUNT",
        "dax/distinctcount-function-dax",
        "dax",
        [("Syntax", "DISTINCTCOUNT(<column>)"), ("Remarks", "Counts distinct column values.")],
    ),
    (
        "COUNTROWS",
        "dax/countrows-function-dax",
        "dax",
        [("Syntax", "COUNTROWS([<table>])"), ("Remarks", "Counts the rows in a table.")],
    ),
    (
        "SUMMARIZE",
        "dax/summarize-function-dax",
        "dax",
        [
            ("Syntax", "SUMMARIZE(<table>, <groupBy_columnName>)"),
            ("Remarks", "Summarize what a dataset is about by grouping its rows."),
        ],
    ),
    (
        "SWITCH",
        "dax/switch-function-dax",
        "dax",
        [
            ("Syntax", "SWITCH(<expression>, <value>, <result>[, <else>])"),
            ("Remarks", "Use SWITCH(TRUE(), ...) to switch the chart band by price."),
        ],
    ),
    (
        "RELATED",
        "dax/related-function-dax",
        "dax",
        [("Syntax", "RELATED(<column>)"), ("Remarks", "Gives a value from a related table.")],
    ),
    (
        "SUMX",
        "dax/sumx-function-dax",
        "dax",
        [
            ("Syntax", "SUMX(<table>, <expression>)"),
            ("Remarks", "Gives the sum of an expression evaluated for each row of a table."),
        ],
    ),
    (
        "Row-level security (RLS) guidance in Power BI Desktop",
        "power-bi/guidance/rls-guidance",
        "modeling",
        [("Setup", "Roles restrict the rows each user can see; validate every role.")],
    ),
]
FANOUT = (
    "Orders is one row per OrderID with OrderTotal. Lines has multiple rows per OrderID. Is "
    "summing a RELATED OrderTotal calculated column in Lines safe? Give a correct alternative "
    "that preserves header totals. Do not use DISTINCT on monetary values."
)


def _page(title: str, path: str, domain: str, sections: list[tuple[str, str]]) -> dict:
    text = "\n\n".join(heading + "\n" + body for heading, body in sections)
    return {
        "title": title,
        "origin": "https://learn.microsoft.com/en-us/" + path,
        "text": text,
        "text_sha256": sha(text.encode()),
        "sha256": "1" * 64,
        "sections": [{"heading": heading, "text": body} for heading, body in sections],
        "retrieved_at": "2026-09-06T00:00:00+00:00",
        "attribution": "Microsoft Learn",
        "license_note": "Local use only",
        "domain": domain,
    }


def _write_archive(folder: Path, pages) -> None:
    raw = "\n".join(json.dumps(_page(*page)) for page in pages).encode()
    payload = compile_collection(raw, {"archive_sha256": "1" * 64, "member_sha256": "2" * 64})
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "reviewed-archive.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    folder = tmp_path_factory.mktemp("relevance")
    _write_archive(folder, PAGES)
    library = KnowledgeLibrary(folder / "index.sqlite3")
    yield library
    library.close()


def _titles(library: KnowledgeLibrary, query: str, **kwargs) -> list[str]:
    kwargs.setdefault("max_characters", 2400)
    return [r.title for r in library.retrieve(query, **kwargs).references]


@pytest.mark.parametrize(
    "query",
    [
        "How is the People table connected to Orders?",
        "What tables are in this dataset and what does each one contain?",
        "Which fields could I use to analyse shipping performance?",
        "Is there a field for customer age or gender?",
        "What would be a good first chart for understanding profit, and why?",
    ],
)
def test_catalog_questions_cite_no_documentation(library, query):
    receipt = library.retrieve(query, max_characters=1200, context_terms=SUPERSTORE)
    assert receipt.references == []
    assert receipt.footer() == receipt.context() == ""


def test_decoys_are_still_cited_when_asked_about(library):
    # The catalog test above is meaningful only if these pages are retrievable at all.
    assert "Tenant settings index" in _titles(library, "Which tenant settings control sharing?")
    assert "Create ArcGIS maps" in _titles(library, "Show age distribution on ArcGIS maps")


def test_c5_keeps_only_return_rate_recipe(library):
    receipt = library.retrieve(
        "In plain words, what does the Return Rate measure calculate?",
        max_characters=1200,
        context_terms=SUPERSTORE,
    )
    assert [r.id for r in receipt.references] == ["recipe-return-rate"]


@pytest.mark.parametrize(
    "query",
    [
        "Number of orders shipped Same Day",
        "Number of order lines with a discount above 20%",
        "Total sales for each row of the Orders table",
        "What does each table contain?",
    ],
)
def test_unnamed_m_and_function_word_pages_not_cited(library, query):
    titles = _titles(library, query, context_terms=SUPERSTORE)
    assert not {"Date.Day", "Lines.ToBinary", "ROW function", "Table.RemoveLastN"} & set(titles)


def test_named_m_and_row_pages_are_cited(library):
    assert _titles(library, "Explain Table.RemoveLastN")[0] == "Table.RemoveLastN"
    assert "ROW function" in _titles(library, "What does ROW( do in DAX?")
    assert "ROW function" in _titles(library, "Use ROW to build a one-row table")


@pytest.mark.parametrize(
    ("query", "function"),
    [
        ("Summarize what this dataset is about", "SUMMARIZE"),
        ("Which table contains the customer names?", "CONTAINS"),
        ("switch the chart to a bar chart", "SWITCH"),
    ],
)
def test_english_word_function_names_need_code_form(library, query, function):
    assert function not in _titles(library, query, context_terms=SUPERSTORE)


def test_english_word_function_names_in_code_form_are_cited(library):
    assert "SUMMARIZE" in _titles(library, "How do I use SUMMARIZE?")
    assert "SWITCH" in _titles(library, "Write SWITCH(TRUE(), [Sales] > 100, 1) for bands")


def test_filler_words_are_not_evidence(library):
    # ArcGIS shares which/would/each with the question, even in its heading, plus "customer".
    assert _titles(library, "Which data would each customer see?") == []


def test_workspace_names_are_not_evidence_but_concepts_are(library):
    query = "How is the People table connected to Orders?"
    assert "Tenant settings index" in _titles(library, query)
    assert "Tenant settings index" not in _titles(library, query, context_terms=SUPERSTORE)
    # "Order Date" is a workspace name, but "date" is still a modeling concept.
    assert _titles(library, "How do I create a date table?", context_terms=SUPERSTORE) == [
        "Set and use date tables in Power BI Desktop"
    ]
    # Without the phrase "date table", only "date" ties this page to the question.
    question = "Can orders and shipments share one date?"
    assert _titles(library, question, context_terms=SUPERSTORE) == ["Role-playing dimensions"]


def test_two_hits_with_a_heading_anchor_are_enough(library):
    # Two shared words alone are too weak; "date" is also the page's heading.
    assert _titles(library, "Can orders and shipments share one date?") == [
        "Role-playing dimensions"
    ]


@pytest.mark.parametrize("context_terms", [SUPERSTORE, CALENDAR])
def test_useful_dax_references_survive(library, context_terms):
    receipt = library.retrieve(
        "How do I calculate year over year growth?",
        max_characters=1200,
        context_terms=context_terms,
    )
    # Calendar[Year] must not hide the YoY example; lowercase "calculate" is not CALCULATE.
    assert [r.title for r in receipt.references] == ["Use variables to improve your DAX formulas"]
    titles = _titles(library, "When should I use DISTINCTCOUNT instead of COUNTROWS?")
    assert {"DISTINCTCOUNT", "COUNTROWS"} & set(titles)
    assert "DAX glossary" in _titles(
        library, "What is the difference between a measure and a calculated column?"
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What does VALUES(Orders[Region]) return?", {"VALUES"}),
        ('Explain CONTAINS(Orders, Orders[Region], "West")', {"CONTAINS"}),
        ("How do I use ALL and ALLSELECTED?", {"ALL", "ALLSELECTED"}),
        ("What does Table.Contains return?", {"Table.Contains"}),
    ],
)
def test_code_form_names_reach_pool_despite_filler(library, query, expected):
    # values/contains/all are filler or generic words, so only the code form finds these.
    assert set(_titles(library, query, context_terms=SUPERSTORE)) == expected


def test_distinct_table_and_column_pages_are_nameable(library):
    titles = _titles(library, "How does DISTINCT(Orders[Order ID]) work?")
    assert {"DISTINCT (table)", "DISTINCT (column)"} & set(titles)
    assert not any("DISTINCT" in t for t in _titles(library, "How many distinct customers?"))


@pytest.mark.parametrize(
    "query", ["How do I calculate a running total?", "How do I remove duplicates in Power Query?"]
)
def test_calculate_and_power_query_words_are_not_evidence(library, query):
    titles = _titles(library, query, context_terms=SUPERSTORE)
    assert "Create key influencers visualizations" not in titles
    assert "Dealing with errors in Power Query" not in titles


@pytest.mark.parametrize(
    ("query", "cited"),
    [
        ("What does VALUES(T[C]) return?", False),
        ("What does the return value of FILTER mean?", False),
        ("what does the Return Rate measure calculate?", True),
        ("Total profit on orders that were returned", True),
        ("What share of orders were returned?", True),
    ],
)
def test_return_verb_is_not_a_return_rate_cue(query, cited):
    receipt = KnowledgeLibrary().retrieve(query, context_terms=SUPERSTORE)
    assert ("recipe-return-rate" in {r.id for r in receipt.references}) is cited


def test_receipt_accepts_v2_history():
    library = KnowledgeLibrary()
    receipt = library.retrieve("weighted price quantity")
    assert receipt.retriever_version == library.status()["retriever_version"] == "fts5-topic-v3"
    stored = receipt.model_dump(mode="json") | {"retriever_version": "fts5-topic-v2"}
    assert RetrievalReceipt.model_validate(stored).retriever_version == "fts5-topic-v2"


def test_named_pages_diversify(library):
    titles = _titles(library, "What is the difference between VALUES and DISTINCT?")
    assert "VALUES" in titles
    assert sum(t.startswith("DISTINCT (") for t in titles) == 1


@pytest.mark.parametrize("budget", [900, 1200, 2400])
def test_topic_recipe_outranks_pages_named_in_passing(library, budget):
    # "Do not use DISTINCT" names two DISTINCT pages; they must not crowd out the
    # reviewed header-total recipe that the question is about.
    receipt = library.retrieve(FANOUT, max_characters=budget)
    assert receipt.references[0].id == "recipe-join-grain"


def test_recipe_cued_only_by_a_named_function_follows_its_page(library):
    receipt = library.retrieve("What does SUMX do?", max_characters=1200)
    assert [r.title for r in receipt.references] == ["SUMX", "Weighted average"]


def test_lowercase_window_still_cues_the_rolling_recipe():
    receipt = KnowledgeLibrary().retrieve("How do I compute a moving average over a 30 day window?")
    assert "recipe-rolling-days" in {r.id for r in receipt.references}


def test_code_form_name_is_cited_when_other_evidence_fills_the_pool(tmp_path):
    # "all" is in nearly every page and 110 decoys share the other words, so BM25 ranks
    # the ALL page out of the 100-row pool unless a coded name's page joins it directly.
    decoys = [
        (
            f"Matrix visual {i}",
            f"power-bi/visuals/matrix-{i}",
            "report_design",
            [("Totals", f"Remove all filters from the matrix total, layout {i}.")],
        )
        for i in range(110)
    ]
    notes = [
        (
            f"Release notes {i}",
            f"power-bi/fundamentals/release-{i}",
            "general",
            [("Summary", "All changes.")],
        )
        for i in range(150)
    ]
    all_page = (
        "ALL",
        "dax/all-function-dax",
        "dax",
        [("Syntax", "ALL([<table> | <column>])"), ("Remarks", "Clears filters on a table.")],
    )
    _write_archive(tmp_path, [all_page, *decoys, *notes])
    library = KnowledgeLibrary(tmp_path / "index.sqlite3")
    query = "How do I use ALL to remove filters from the total in a matrix visual?"
    assert _titles(library, query) == ["ALL"]
    library.close()


def test_rare_anchored_evidence_ranks_first(tmp_path):
    # Filler pages make gateway/cluster/partition/schedule common without being cited.
    changes = ["The partition schedule was updated.", "The gateway cluster was updated."]
    common = [
        (
            f"Release notes {i}",
            f"power-bi/fundamentals/release-{i}",
            "general",
            [("Changes", changes[i % 2])],
        )
        for i in range(12)
    ]
    _write_archive(
        tmp_path,
        [
            *common,
            (
                "Troubleshoot refresh",
                "power-bi/connect-data/refresh-troubleshooting",
                "general",
                [("Common causes", "A gateway cluster refreshes each partition on a schedule.")],
            ),
            (
                "Request throttling",
                "power-bi/enterprise/service-admin-throttling",
                "general",
                [("Retry behavior", "Throttled requests wait for a backoff before they retry.")],
            ),
        ],
    )
    library = KnowledgeLibrary(tmp_path / "index.sqlite3")
    query = "Why does throttling backoff slow the gateway cluster partition schedule?"
    # Four common words outnumber two rare ones (one in the title): rarity decides.
    assert _titles(library, query) == ["Request throttling", "Troubleshoot refresh"]
    assert _titles(library, query, max_characters=512) == ["Request throttling"]
    library.close()


def test_mirrored_page_is_quoted_once(tmp_path):
    sections = [
        ("Overview", "A KPI visual shows progress toward a measurable goal."),
        ("Formatting", "Format the KPI visual indicator and its trend axis."),
    ]
    _write_archive(
        tmp_path,
        [
            (
                "KPI visuals",
                "power-bi/visuals/power-bi-visualization-kpi",
                "report_design",
                sections,
            ),
            ("KPI visuals", "power-bi/consumer/end-user-kpi", "report_design", sections),
            (
                "Create goal cards",
                "power-bi/create-reports/service-goals-create",
                "report_design",
                [("KPI visuals", "A goal card tracks the same target as a KPI visual.")],
            ),
        ],
    )
    library = KnowledgeLibrary(tmp_path / "index.sqlite3")
    receipt = library.retrieve("How do I build a KPI visual?")
    # The mirror URL repeats the page; its other chunk must not take the second slot.
    assert [r.title for r in receipt.references] == ["KPI visuals", "Create goal cards"]
    assert receipt.footer().count("[KPI visuals]") == 1
    library.close()


def test_plural_workspace_name_leaves_the_singular_as_evidence():
    # A Returns table must not make "return" workspace vocabulary: the curated note on
    # return rates needs both "return" and "rate".
    receipt = KnowledgeLibrary().retrieve(
        "In plain words, what does the Return Rate measure calculate?",
        max_characters=2400,
        context_terms=SUPERSTORE,
    )
    assert [r.id for r in receipt.references] == ["recipe-return-rate", "note-distinct"]


def test_shouted_phrase_does_not_name_a_function_page(library):
    # Upper case marks code, but "ROW LEVEL SECURITY" is a phrase, not the ROW function.
    titles = _titles(library, "ROW LEVEL SECURITY setup", context_terms=SUPERSTORE)
    assert titles[0] == "Row-level security (RLS) guidance in Power BI Desktop"
    assert "ROW function" not in titles


def test_evidence_only_query_still_detects_stale_index(tmp_path):
    old = KnowledgeLibrary(tmp_path / "index.sqlite3")
    _write_archive(tmp_path, [TENANT])
    new = KnowledgeLibrary(tmp_path / "index.sqlite3")
    # Only filler words: no documentation search runs, but the stale check still must.
    with pytest.raises(RuntimeError, match="restart"):
        old.retrieve("What does each one contain?")
    assert new.retrieve("What does each one contain?").references == []
    old.close()
    new.close()


def test_chat_footer_only_for_relevant_references(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    (root / "Orders.csv").write_text(
        "Order ID,Customer Name,Region,Sales,Quantity\nA-1,Ann,East,100,2\nA-2,Bob,West,50,1\n",
        encoding="utf-8",
    )
    (root / "People.csv").write_text("Person,Region\nCy,East\nDi,West\n", encoding="utf-8")
    _write_archive(tmp_path / "state" / "knowledge", [TENANT, ARCGIS])
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Relevance",
            selected_folder=str(root),
            idempotency_key="relevance-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(root / "Orders.csv"), str(root / "People.csv")],
            idempotency_key="relevance-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    captured = {}

    def stream(message, *, system_prompt, history=None, worked_examples=True):
        captured["system"] = system_prompt
        captured["worked_examples"] = worked_examples
        yield "Answer."

    monkeypatch.setattr(service.model, "stream_chat", stream)
    for question, decoy in (
        ("Is there a field for customer age or gender?", "Create ArcGIS maps"),
        ("How is the People table connected to Orders?", "Tenant settings index"),
    ):
        # Without the workspace's names the decoy clears the gate on "customer"/"people".
        assert decoy in [r.title for r in service.knowledge.retrieve(question).references]
        answer = "".join(service.chat(workspace.id, question))
        assert "References supplied" not in answer
        assert "Quoted reference material" not in captured["system"]
        assert captured["worked_examples"] is False
    answer = "".join(service.chat(workspace.id, "Explain a weighted average using quantity"))
    assert "References supplied" in answer
    assert "recipe-weighted" in captured["system"]
    assert captured["worked_examples"] is False
