"""Reviewed patterns, not evidence of successful execution in a user's semantic model.

Formulas use illustrative names and must be bound to the approved workspace catalog.
Independent numerical checks live in tests/unit/test_powerbi_knowledge.py.
"""

# ruff: noqa: E501
RECIPES = [
    (
        "ratio",
        "Ratio and profit margin",
        "margin ratio profit divide percentage zero denominator",
        "Compute a ratio of aggregates, not the average of row percentages. "
        "DAX pattern: DIVIDE(SUM('Fact'[Profit]), SUM('Fact'[Sales])). "
        "DIVIDE defaults to BLANK for zero denominator. Let the analytical engine calculate the "
        "displayed result; do not mentally estimate it. Required bindings: numeric numerator and "
        "denominator at compatible grain. Keep current filters unless explicitly asked otherwise.",
        "divide-function-dax",
    ),
    (
        "weighted",
        "Weighted average",
        "weighted average price quantity sumx weight",
        "DAX pattern: DIVIDE(SUMX('Lines', 'Lines'[UnitPrice] * 'Lines'[Quantity]), "
        "SUM('Lines'[Quantity])). SUM accepts a column, not a multiplied-column expression. "
        "SUMX evaluates the product per row. Bind both numeric columns in the same table; clarify "
        "the treatment of missing or negative weights. Zero denominator returns BLANK by default. "
        "Compute the numeric answer through a bounded aggregate, not model arithmetic.",
        "sumx-function-dax",
    ),
    (
        "fixed-grain",
        "Explicit single-table fixed grain (LOD)",
        "lod fixed regional region category level detail allexcept preserve ignore filters",
        "Prerequisite: explicit filters on a grouping column in a single source table. "
        "Pattern: CALCULATE([Base Measure], ALLEXCEPT('Fact', 'Fact'[Group])). "
        "This preserves filters on Group and removes other Fact filters. Never hard-code an example "
        "group value. Region=MAX(Region) does not remove Category. ALL(Fact) removes the group too; "
        "ALLSELECTED is different. Ask which filters to keep/remove when unclear. Related dimensions "
        "and expanded-table behavior need separate analysis; this is NOT universal Tableau FIXED. "
        "Test with and without explicit group filters, other slicers, multiple groups and totals.",
        "allexcept-function-dax",
    ),
    (
        "scalar-context",
        "Scalar variables and CALCULATE",
        "variable var scalar calculate removefilters all region",
        "A scalar VAR evaluated before CALCULATE is not recalculated in its new filter context. "
        "Wrong: VAR S=SUM('Fact'[Sales]) RETURN CALCULATE(S,REMOVEFILTERS('Fact'[Region])). "
        "Correct pattern: CALCULATE(SUM('Fact'[Sales]),REMOVEFILTERS('Fact'[Region])). "
        "Alternatively call an existing measure inside CALCULATE. Do not invent ALLSELECT; "
        "ALLSELECTED is real but retains selection semantics and is not a universal replacement.",
        "best-practices/dax-variables",
    ),
    (
        "rolling-days",
        "Inclusive rolling-day window",
        "rolling trailing window days date inclusive seven",
        "For N inclusive calendar days ending at the selected maximum date: "
        "VAR WindowEnd=MAX('Date'[Date]) RETURN CALCULATE([Base Measure], "
        "FILTER(ALL('Date'), 'Date'[Date]>=WindowEnd-(N-1) && 'Date'[Date]<=WindowEnd)). "
        "N must be an approved positive integer, not a literal placeholder in output. "
        "Requires a contiguous date table and correct active date relationship. Clears Date-table "
        "filters, not unrelated dimension filters. Subtract days, not months. DATESINCLUDE does not "
        "exist. Check exact inclusive endpoints and partial-history behavior.",
        "calculate-function-dax",
    ),
    (
        "inactive-date",
        "Use an existing inactive date relationship",
        "ship shipped delivery inactive date relationship userelationship",
        "Pattern: CALCULATE([Base Measure], USERELATIONSHIP('Fact'[ShipDate], 'Date'[Date])). "
        "Both columns must be endpoints of an existing reviewed relationship. This engages the "
        "ShipDate relationship for the measure instead of the competing active OrderDate relationship. "
        "ALLSELECTED does not activate a relationship. Do not invent Dates if the catalog says Date. "
        "The model may propose a missing relationship but cannot create or approve it itself.",
        "userelationship-function-dax",
    ),
    (
        "join-grain",
        "Avoid duplicated header totals",
        "join merge fanout order header lines duplicate total grain",
        "A header-to-lines merge repeats header totals per line. Summing a RELATED header-total "
        "column on lines has the same problem. Preserve separate header and line facts with reviewed "
        "keys and sum header measures at header grain; or preaggregate once per OrderID. Never use "
        "DISTINCT on monetary values: different orders may have equal amounts. Verify both row counts "
        "and sums before/after a proposed merge. Relationships are not SQL row-materializing joins.",
        "https://learn.microsoft.com/en-us/power-bi/guidance/star-schema",
    ),
    (
        "keys",
        "Relationship and composite-key review",
        "customer customers unique null orphan composite country key cardinality joins",
        "One side must have unique keys. Resolve conflicting identities using a reviewed rule, not "
        "arbitrary deduplication or invented IDs. Explicitly handle blank and unmatched keys. For a "
        "composite key, prefer one shared surrogate mapping of the actual pair; a raw separator can "
        "collide and removing hyphens changes identities. Validate uniqueness and referential coverage "
        "before Customers 1:* Orders, with single-direction filtering Customers to Orders. Never "
        "validate a key based only on names or model confidence; use deterministic data evidence.",
        "https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-relationships-understand",
    ),
    (
        "return-rate",
        "Order-level return rate",
        "return returned returns rate distinct order grain",
        "Order return rate = distinct returned orders in scope divided by distinct orders in scope. "
        "Duplicate order lines and duplicate return records are not extra orders. A reviewed unique "
        "Order dimension with a HasReturn attribute can filter the order-line fact. Alternatively "
        "design verified paths to both facts; default single-direction dimension relationships do "
        "not make a Returns fact filter the Orders fact. Define numerator coverage, unmatched returns "
        "and denominator filter behavior. Do not use COUNTROWS(Orders) for order-line data.",
        "distinctcount-function-dax",
    ),
    (
        "mixed-grain",
        "Monthly budgets versus daily sales",
        "budget monthly category product daily allocation grain facts",
        "Keep separate facts and valid common Month/Category dimensions or equivalent reviewed "
        "higher-grain design. Do not join a monthly category budget to every daily product row and "
        "sum it. No relationship between Products and Calendar exists without a valid key. Without "
        "a reviewed allocation rule, return BLANK or disallow budget detail below its grain; repeating "
        "the whole budget for every product is not allocation. Budget measures must reference Budget, "
        "not a convenient Sales field. Validate month/category totals and lower-grain suppression.",
        "https://learn.microsoft.com/en-us/power-bi/guidance/relationships-many-to-many",
    ),
]
