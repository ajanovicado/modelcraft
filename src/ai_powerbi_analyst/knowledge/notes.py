"""Original compact reference notes linked to Microsoft documentation.

These summaries ship with Studio; they are not a mirror of Microsoft Learn pages.
Full pages may be explicitly downloaded into a user's local, non-packaged cache.
"""

# ruff: noqa: E501
NOTES = [
    (
        "divide",
        "DIVIDE: ratios and zero denominators",
        "divide-function-dax",
        "DIVIDE(numerator, denominator, alternateResult) safely divides scalar expressions. "
        "Its default zero-denominator result is BLANK. An alternate result must be a constant. "
        "A profit margin normally divides aggregated profit by aggregated sales; averaging row "
        "percentages measures something else. Numeric results need computation, not prose estimates.",
    ),
    (
        "sumx",
        "SUMX: row-wise weighted expressions",
        "sumx-function-dax",
        "SUMX(table, expression) evaluates an expression for each table row and sums the results. "
        "Use it when aggregating products such as quantity times price. SUM(column) cannot accept "
        "a multiplication expression. A weighted mean divides the sum of value times weight by "
        "the sum of weights; preserve the requested filter context.",
    ),
    (
        "allexcept",
        "ALLEXCEPT: preserving a grouping filter",
        "allexcept-function-dax",
        "ALLEXCEPT requires a base table followed by base columns from that table. In CALCULATE "
        "it removes filters from that table except the specified columns. Use only when this is "
        "the requested filter behavior. ALL removes the retained grouping too; ALLSELECTED has "
        "selection-sensitive behavior and is not equivalent. Related-table behavior must be analyzed.",
    ),
    (
        "calculate",
        "CALCULATE and filter context",
        "calculate-function-dax",
        "CALCULATE evaluates its first expression under modified filter context. A Boolean filter "
        "argument references columns from one table, not columns from unrelated tables. A new "
        "column filter does not automatically remove filters on other columns. Filter modifiers "
        "such as REMOVEFILTERS must explicitly implement the desired scope.",
    ),
    (
        "variables",
        "Scalar variables do not re-evaluate",
        "best-practices/dax-variables",
        "A scalar variable holds the value evaluated where it was defined. Passing that variable "
        "to CALCULATE after modifying filters does not re-run the original aggregation. Put the "
        "aggregation or a measure reference inside CALCULATE when it must use the new context.",
    ),
    (
        "removefilters",
        "REMOVEFILTERS: explicit filter removal",
        "removefilters-function-dax",
        "REMOVEFILTERS can clear a table's filters or filters on specified columns. It is a "
        "filter modifier, not a table result to iterate. Do not combine a table argument and a "
        "column argument as if they were a valid same-column list. Choose exactly the scope "
        "requested and leave unrelated filters intact.",
    ),
    (
        "userelationship",
        "USERELATIONSHIP and shipping dates",
        "userelationship-function-dax",
        "USERELATIONSHIP(column1, column2) refers to the endpoints of an existing relationship. "
        "Inside CALCULATE it can activate an inactive ShipDate relationship and supersede the "
        "competing OrderDate relationship for that calculation. It does not create a missing "
        "relationship. Verify endpoints, cardinality and security limitations in the actual model.",
    ),
    (
        "rolling",
        "Rolling date intervals",
        "datesinperiod-function-dax",
        "DATESINPERIOD returns a date interval for use in a calculation. For a manually constructed "
        "N-day inclusive window ending at a selected date, the lower endpoint is end minus N-1 "
        "days, and the upper endpoint is end. A reviewed date table and relationship are prerequisites. "
        "Clear conflicting date filters explicitly when required; do not remove Region filters.",
    ),
    (
        "distinct",
        "DISTINCTCOUNT and order-level return rates",
        "distinctcount-function-dax",
        "DISTINCTCOUNT counts distinct column values, not table rows; BLANK needs an explicit "
        "business rule. Order-line facts require distinct order counts for an order return rate. "
        "Deduplicate return IDs logically and verify that numerator and denominator use the same "
        "population, date semantics and scope.",
    ),
    (
        "queries",
        "DAX query validation",
        "dax-queries",
        "DAX queries can evaluate table expressions and define query-scoped measures. Actual engine "
        "execution is distinct from checking JSON, identifiers or PBIP structure. Validate generated "
        "measures in an isolated context with reviewed test cases; do not promote a successful "
        "syntax check to proof of numerical or filter correctness.",
    ),
    (
        "relationships",
        "Relationship keys and direction",
        "https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-relationships-understand",
        "A one-to-many relationship needs a unique key on its one side and compatible column "
        "types. Inspect duplicates, unmatched keys and blank handling before proposing cardinality. "
        "A normal dimension-to-fact filter flows from the one side to the many side. "
        "A relationship is not a SQL merge that materializes duplicated rows.",
    ),
    (
        "star",
        "Star schemas, composite keys and join grain",
        "https://learn.microsoft.com/en-us/power-bi/guidance/star-schema",
        "Dimensions describe entities and facts record observations at a consistent grain. Keep "
        "header measures from being summed repeatedly after merging with lines. Customer composite "
        "keys must preserve the identity of the entire pair; use a shared surrogate mapping or "
        "collision-safe encoding. Review conflicts instead of blindly removing duplicate rows.",
    ),
    (
        "budget",
        "Higher-grain budgets and shared dimensions",
        "https://learn.microsoft.com/en-us/power-bi/guidance/relationships-many-to-many",
        "Facts at different grains require carefully designed common dimensions and measure "
        "semantics. A monthly category budget has no product/day allocation by default. "
        "Suppress unsupported detail or return BLANK until an allocation rule is approved. "
        "Repeating a budget for every lower-grain row and summing it causes double-counting. "
        "Do not invent a Products-to-Calendar relationship without a real key.",
    ),
]
