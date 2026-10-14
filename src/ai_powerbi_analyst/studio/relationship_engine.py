"""Deterministic, full-snapshot governance for Power BI relationships.

The language model may explain or rank the resulting candidates, but it does not
decide uniqueness, cardinality, referential integrity, or active filter paths.
Those properties are derived here from the reviewed Parquet snapshots.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import duckdb

from ai_powerbi_analyst.contracts import (
    ColumnProfile,
    RelationshipCandidate,
    SourceKind,
    SourceManifest,
    TableProfile,
)
from ai_powerbi_analyst.powerbi.calendar_table import daily_date_key

_DEFAULT_MEMORY_LIMIT = "512MB"
_DEFAULT_CANDIDATE_LIMIT = 256


class RelationshipGovernanceError(RuntimeError):
    """The reviewed snapshots could not be checked safely."""


@dataclass(frozen=True, slots=True)
class RelationshipGovernanceResult:
    """Governed candidates and non-executable modeling recommendations."""

    relationships: tuple[RelationshipCandidate, ...]
    recommendations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Endpoint:
    table: TableProfile
    column: ColumnProfile
    path: Path


@dataclass(frozen=True, slots=True)
class _ColumnStats:
    row_count: int
    null_count: int
    distinct_count: int

    @property
    def duplicate_rows(self) -> int:
        return max(0, self.row_count - self.null_count - self.distinct_count)

    @property
    def is_unique_key(self) -> bool:
        return self.row_count > 0 and self.null_count == 0 and self.distinct_count == self.row_count

    @property
    def uniqueness_ratio(self) -> float:
        non_null = self.row_count - self.null_count
        return self.distinct_count / non_null if non_null else 0.0


@dataclass(frozen=True, slots=True)
class _IntegrityStats:
    considered_rows: int
    matched_rows: int
    orphan_rows: int
    orphan_keys: int
    matched_keys: int = 0

    @property
    def coverage(self) -> float:
        return self.matched_rows / self.considered_rows if self.considered_rows else 1.0


class RelationshipGovernanceEngine:
    """Propose and validate relationships using bounded-memory DuckDB scans."""

    def __init__(
        self,
        temp_root: str | Path,
        *,
        memory_limit: str = _DEFAULT_MEMORY_LIMIT,
        candidate_limit: int = _DEFAULT_CANDIDATE_LIMIT,
    ) -> None:
        if not re.fullmatch(r"[1-9][0-9]*(?:KB|MB|GB)", memory_limit, re.IGNORECASE):
            raise ValueError("memory_limit must be a positive DuckDB byte-size value")
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        self.temp_root = Path(temp_root).expanduser().resolve(strict=False)
        self.memory_limit = memory_limit.upper()
        self.candidate_limit = candidate_limit

    def propose(self, manifest: SourceManifest) -> RelationshipGovernanceResult:
        """Discover plausible column pairs, then validate their complete snapshots."""

        endpoints = _endpoints(manifest)
        date_dimensions = {
            table.id: column for table in manifest.tables if (column := daily_date_key(table))
        }
        pairs: list[tuple[float, _Endpoint, _Endpoint, bool, float]] = []
        for left_index, left_table in enumerate(manifest.tables):
            for right_table in manifest.tables[left_index + 1 :]:
                for left_column in left_table.columns:
                    for right_column in right_table.columns:
                        name_match = _relationship_name(left_column.name) == _relationship_name(
                            right_column.name
                        )
                        sample_overlap = _sample_overlap(left_column, right_column)
                        if not _plausible_pair(
                            left_table,
                            left_column,
                            right_table,
                            right_column,
                            name_match=name_match,
                            sample_overlap=sample_overlap,
                            date_dimensions=date_dimensions,
                        ):
                            continue
                        # Key stems (CustomerKey ~ CustomerID) and date-dimension
                        # joins are key evidence just like identical names.
                        name_match = (
                            name_match
                            or _key_stem(left_table, left_column)
                            == _key_stem(right_table, right_column)
                            or left_column.data_type.value in _DATE_TYPES
                        )
                        left = endpoints[(left_table.id, left_column.id)]
                        right = endpoints[(right_table.id, right_column.id)]
                        profile_score = (
                            (0.7 if name_match else 0.0)
                            + (0.2 * sample_overlap)
                            + (0.1 if left_column.data_type == right_column.data_type else 0.0)
                        )
                        pairs.append((profile_score, left, right, name_match, sample_overlap))

        pairs.sort(
            key=lambda item: (
                -item[0],
                item[1].table.id,
                item[1].column.id,
                item[2].table.id,
                item[2].column.id,
            )
        )
        limited = pairs[: self.candidate_limit]
        recommendations: list[str] = []
        if len(pairs) > len(limited):
            recommendations.append(
                f"Review column naming: {len(pairs) - len(limited)} lower-ranked relationship "
                "candidates were omitted by the bounded candidate limit."
            )

        with self._connection() as connection:
            governed = [
                self._evaluate_pair(
                    connection,
                    left,
                    right,
                    name_match=name_match,
                    sample_overlap=sample_overlap,
                )
                for _, left, right, name_match, sample_overlap in limited
            ]
        labels = _labels(manifest)
        governed, shared = _drop_fact_to_fact(governed, labels)
        recommendations.extend(shared)
        governed = _role_playing_dates(governed, manifest, date_dimensions)
        governed = _govern_active_paths(governed)
        for relationship in governed:
            if relationship.cardinality == "many_to_many":
                recommendations.append(
                    "Do not activate "
                    f"{labels.column(relationship.from_table_id, relationship.from_column_id)} to "
                    f"{labels.column(relationship.to_table_id, relationship.to_column_id)}; "
                    "create a reviewed "
                    "bridge with one row per key, or choose a verified dimension key."
                )
        return RelationshipGovernanceResult(
            relationships=tuple(sorted(governed, key=lambda item: (-item.confidence, item.id))),
            recommendations=tuple(dict.fromkeys(recommendations)),
        )

    def govern(
        self,
        manifest: SourceManifest,
        relationships: list[RelationshipCandidate],
    ) -> RelationshipGovernanceResult:
        """Revalidate user/model-selected relationships against the current snapshots."""

        endpoints = _endpoints(manifest)
        governed: list[RelationshipCandidate] = []
        with self._connection() as connection:
            for relationship in relationships:
                try:
                    left = endpoints[(relationship.from_table_id, relationship.from_column_id)]
                    right = endpoints[(relationship.to_table_id, relationship.to_column_id)]
                except KeyError as error:
                    raise RelationshipGovernanceError(
                        f"Relationship {relationship.id} references an unknown table or column"
                    ) from error
                if left.table.id == right.table.id:
                    governed.append(
                        relationship.model_copy(
                            update={
                                "blockers": ["Relationships within one table are unsupported"],
                                "rely_on_referential_integrity": False,
                            }
                        )
                    )
                    continue
                checked = self._evaluate_pair(
                    connection,
                    left,
                    right,
                    name_match=(
                        _relationship_name(left.column.name)
                        == _relationship_name(right.column.name)
                    ),
                    sample_overlap=_sample_overlap(left.column, right.column),
                    relationship_id=relationship.id,
                )
                governed.append(
                    checked.model_copy(
                        update={
                            "cross_filter_direction": (
                                "both"
                                if checked.cardinality == "one_to_one"
                                else relationship.cross_filter_direction
                            ),
                            "is_active": relationship.is_active,
                            "review_status": relationship.review_status,
                        }
                    )
                )

        governed = _govern_active_paths(governed)
        recommendations = tuple(
            f"Relationship {item.id} requires a reviewed bridge or a verified unique dimension."
            for item in governed
            if item.cardinality == "many_to_many"
        )
        return RelationshipGovernanceResult(
            relationships=tuple(sorted(governed, key=lambda item: (-item.confidence, item.id))),
            recommendations=recommendations,
        )

    def _connection(self) -> duckdb.DuckDBPyConnection:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect(database=":memory:")
        escaped_temp = str(self.temp_root).replace("'", "''")
        connection.execute(f"SET temp_directory = '{escaped_temp}'")
        connection.execute(f"SET memory_limit = '{self.memory_limit}'")
        connection.execute("SET threads = 2")
        connection.execute("SET preserve_insertion_order = false")
        return connection

    def _evaluate_pair(
        self,
        connection: duckdb.DuckDBPyConnection,
        left: _Endpoint,
        right: _Endpoint,
        *,
        name_match: bool,
        sample_overlap: float,
        relationship_id: str | None = None,
    ) -> RelationshipCandidate:
        left_stats = _column_stats(connection, left)
        right_stats = _column_stats(connection, right)
        from_endpoint, to_endpoint, from_stats, to_stats, cardinality = _normalize_direction(
            left,
            right,
            left_stats,
            right_stats,
        )

        blockers: list[str] = []
        same_type = left.column.data_type == right.column.data_type
        if not same_type:
            blockers.append(
                "Column data types differ: "
                f"{left.column.data_type.value} versus {right.column.data_type.value}"
            )
        if cardinality == "many_to_many":
            blockers.append(
                "Unsupported many-to-many relationship; neither side is a verified unique key"
            )
        if to_stats.null_count:
            blockers.append(f"One-side column contains {to_stats.null_count:,} null row(s)")
        if to_stats.duplicate_rows:
            blockers.append(
                f"One-side column contains {to_stats.duplicate_rows:,} duplicate row(s)"
            )

        integrity = (
            _integrity_stats(connection, from_endpoint, to_endpoint)
            if same_type
            else _IntegrityStats(
                considered_rows=from_stats.row_count - from_stats.null_count,
                matched_rows=0,
                orphan_rows=from_stats.row_count - from_stats.null_count,
                orphan_keys=from_stats.distinct_count,
            )
        )
        if same_type and integrity.considered_rows and integrity.matched_rows == 0:
            blockers.append("No non-null key values match across the full snapshots")
        orphan_warning = None
        if same_type and integrity.orphan_rows:
            finding = (
                f"Referential-integrity check found {integrity.orphan_rows:,} orphan row(s) "
                f"across {integrity.orphan_keys:,} key(s)"
            )
            # Power BI keeps orphan rows under a (Blank) member. A few unmatched keys
            # are a data-quality warning; low coverage means the wrong join, unless
            # the one side is a keyed subset list (Returns of Orders): every one of
            # its keys exists on the many side, so the rest simply have no entry.
            subset_list = (
                name_match
                and cardinality == "many_to_one"
                and 0 < integrity.matched_keys == to_stats.distinct_count
            )
            if integrity.coverage >= 0.9:
                orphan_warning = (
                    f"Warning: {finding}; Power BI groups them under (Blank). "
                    "Fix the source keys or add the missing dimension rows."
                )
            elif subset_list:
                orphan_warning = (
                    f"Subset table: all {to_stats.distinct_count:,} {to_endpoint.table.name} "
                    f"key(s) exist in {from_endpoint.table.name}; its other "
                    f"{integrity.orphan_keys:,} key(s) have no {to_endpoint.table.name} row "
                    "and group under (Blank)."
                )
            else:
                blockers.append(finding)

        evidence = [
            (
                "Full snapshots checked with bounded-memory DuckDB: "
                f"{from_stats.row_count:,} from-row(s), {to_stats.row_count:,} to-row(s)"
                + (
                    "; text keys ignore case and trailing spaces"
                    if left.column.data_type.value == "string"
                    else ""
                )
            ),
            (
                f"Verified cardinality: {cardinality}; one-side distinct/null/duplicate rows: "
                f"{to_stats.distinct_count:,}/{to_stats.null_count:,}/"
                f"{to_stats.duplicate_rows:,}"
            ),
            (
                f"Non-null key coverage: {integrity.coverage:.2%}; "
                f"{integrity.matched_rows:,} matched and {integrity.orphan_rows:,} orphan row(s)"
            ),
            "Key names match or the column joins a date dimension"
            if name_match
            else "Bounded samples overlap",
            f"Bounded sample overlap: {sample_overlap:.0%}",
        ]
        if orphan_warning:
            evidence = [orphan_warning, *_without_overlap(evidence)]
        if cardinality == "one_to_one":
            evidence.append("Power BI one-to-one relationships require Both filter direction")
        if cardinality == "many_to_many":
            evidence.append(
                "Recommendation: use a reviewed distinct-key bridge; no bridge was synthesized"
            )

        confidence = 0.35
        confidence += 0.2 if name_match else 0.0
        confidence += 0.15 * sample_overlap
        confidence += 0.15 if same_type else 0.0
        confidence += 0.15 * integrity.coverage if same_type else 0.0
        if cardinality == "many_to_many":
            confidence = min(confidence, 0.49)
        if blockers:
            confidence = min(confidence, 0.79)
        identifier = relationship_id or _relationship_id(left, right)
        exact_integrity = (
            not blockers
            and from_stats.null_count == 0
            and integrity.orphan_rows == 0
            and to_stats.is_unique_key
        )
        return RelationshipCandidate(
            id=identifier,
            from_table_id=from_endpoint.table.id,
            from_column_id=from_endpoint.column.id,
            to_table_id=to_endpoint.table.id,
            to_column_id=to_endpoint.column.id,
            cardinality=cardinality,
            cross_filter_direction="both" if cardinality == "one_to_one" else "single",
            confidence=round(min(0.99, confidence), 3),
            evidence=evidence,
            blockers=blockers[:6],
            rely_on_referential_integrity=exact_integrity,
        )


def _endpoints(manifest: SourceManifest) -> dict[tuple[str, str], _Endpoint]:
    source_by_id = {source.id: source for source in manifest.sources}
    endpoints: dict[tuple[str, str], _Endpoint] = {}
    for table in manifest.tables:
        source = source_by_id.get(table.source_id)
        if source is None:
            raise RelationshipGovernanceError(f"Table {table.id} has no managed source snapshot")
        if source.kind != SourceKind.PARQUET:
            raise RelationshipGovernanceError(
                "Full relationship governance requires normalized Parquet snapshots"
            )
        path = Path(source.snapshot_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise RelationshipGovernanceError(
                f"Managed snapshot for table {table.id} is not a file"
            )
        for column in table.columns:
            endpoints[(table.id, column.id)] = _Endpoint(table, column, path)
    return endpoints


def _column_stats(
    connection: duckdb.DuckDBPyConnection,
    endpoint: _Endpoint,
) -> _ColumnStats:
    column = _key_expression(endpoint)
    query = (
        f"SELECT COUNT(*)::BIGINT, COUNT(*) FILTER (WHERE {column} IS NULL)::BIGINT, "  # noqa: S608 - quoted catalog identifiers; path parameterized
        f"COUNT(DISTINCT {column})::BIGINT FROM read_parquet(?)"
    )
    try:
        row = connection.execute(query, [str(endpoint.path)]).fetchone()
    except duckdb.Error as error:
        raise RelationshipGovernanceError(
            f"Could not inspect {endpoint.table.id}.{endpoint.column.id}: {error}"
        ) from error
    if row is None:
        raise RelationshipGovernanceError("DuckDB returned no column statistics")
    return _ColumnStats(int(row[0]), int(row[1]), int(row[2]))


def _integrity_stats(
    connection: duckdb.DuckDBPyConnection,
    many: _Endpoint,
    one: _Endpoint,
) -> _IntegrityStats:
    many_column = _key_expression(many, alias="many_side")
    one_column = _key_expression(one)
    query = (
        "WITH one_keys AS ("  # noqa: S608 - quoted catalog identifiers; paths parameterized
        f"SELECT DISTINCT {one_column} AS relationship_key FROM read_parquet(?) "
        f"WHERE {one_column} IS NOT NULL"
        "), checked AS ("
        f"SELECT {many_column} AS relationship_key, "
        "one_keys.relationship_key IS NOT NULL AS matched "
        "FROM read_parquet(?) AS many_side LEFT JOIN one_keys "
        f"ON {many_column} = one_keys.relationship_key "
        f"WHERE {many_column} IS NOT NULL"
        ") SELECT COUNT(*)::BIGINT, "
        "COUNT(*) FILTER (WHERE matched)::BIGINT, "
        "COUNT(*) FILTER (WHERE NOT matched)::BIGINT, "
        "COUNT(DISTINCT relationship_key) FILTER (WHERE NOT matched)::BIGINT, "
        "COUNT(DISTINCT relationship_key) FILTER (WHERE matched)::BIGINT FROM checked"
    )
    try:
        row = connection.execute(query, [str(one.path), str(many.path)]).fetchone()
    except duckdb.Error as error:
        raise RelationshipGovernanceError(
            "Could not perform the full referential-integrity check: " + str(error)
        ) from error
    if row is None:
        raise RelationshipGovernanceError("DuckDB returned no integrity statistics")
    return _IntegrityStats(*(int(value) for value in row))


def _normalize_direction(
    left: _Endpoint,
    right: _Endpoint,
    left_stats: _ColumnStats,
    right_stats: _ColumnStats,
) -> tuple[
    _Endpoint,
    _Endpoint,
    _ColumnStats,
    _ColumnStats,
    Literal["many_to_one", "one_to_one", "many_to_many"],
]:
    left_unique = left_stats.is_unique_key
    right_unique = right_stats.is_unique_key
    if left_unique and right_unique and _owns_key(left) != _owns_key(right):
        # Returns[CustomerID] may happen to be unique in a small fact, but it points
        # at Customers' own key: many-to-one, single direction, not a two-way 1:1.
        if _owns_key(left):
            return right, left, right_stats, left_stats, "many_to_one"
        return left, right, left_stats, right_stats, "many_to_one"
    if left_unique and right_unique:
        cardinality: Literal["many_to_one", "one_to_one", "many_to_many"] = "one_to_one"
        if _endpoint_key(left) <= _endpoint_key(right):
            return left, right, left_stats, right_stats, cardinality
        return right, left, right_stats, left_stats, cardinality
    if left_unique:
        return right, left, right_stats, left_stats, "many_to_one"
    if right_unique:
        return left, right, left_stats, right_stats, "many_to_one"

    left_rank = (left_stats.uniqueness_ratio, -left_stats.null_count, _endpoint_key(left))
    right_rank = (right_stats.uniqueness_ratio, -right_stats.null_count, _endpoint_key(right))
    if left_rank >= right_rank:
        return right, left, right_stats, left_stats, "many_to_many"
    return left, right, left_stats, right_stats, "many_to_many"


def _owns_key(endpoint: _Endpoint) -> bool:
    """Whether the column is its own table's key (Customers[CustomerID], Dates[Date])."""
    date_key = daily_date_key(endpoint.table)
    return (date_key is not None and date_key.id == endpoint.column.id) or _key_stem(
        endpoint.table, endpoint.column
    ) == _table_stem(endpoint.table)


def _govern_active_paths(
    relationships: list[RelationshipCandidate],
) -> list[RelationshipCandidate]:
    accepted_pairs: set[frozenset[str]] = set()
    accepted_edges: list[tuple[str, str, bool]] = []
    ordered = sorted(
        relationships,
        key=lambda item: (
            item.review_status != "approved",
            -item.confidence,
            item.id,
        ),
    )
    result: list[RelationshipCandidate] = []
    for relationship in ordered:
        if (
            relationship.review_status == "rejected"
            or not relationship.is_active
            or relationship.blockers
        ):
            result.append(relationship)
            continue
        pair = frozenset((relationship.from_table_id, relationship.to_table_id))
        blockers = list(relationship.blockers)
        edge = (
            relationship.to_table_id,
            relationship.from_table_id,
            relationship.cross_filter_direction == "both",
        )
        if pair in accepted_pairs:
            blockers.append(
                "Ambiguous active filter path: another relationship already connects these tables"
            )
        elif _ambiguous_paths([*accepted_edges, edge]):
            blockers.extend(
                [
                    "Active relationship would create a cycle",
                    "Active relationship would create an ambiguous filter path",
                ]
            )
        else:
            accepted_pairs.add(pair)
            accepted_edges.append(edge)
        result.append(
            relationship.model_copy(
                update={
                    "blockers": blockers[:6],
                    "rely_on_referential_integrity": (
                        relationship.rely_on_referential_integrity and not blockers
                    ),
                }
            )
        )
    return result


_KEY_TOKENS = {"id", "key", "code", "number", "num", "no", "sk", "identifier"}
_DATE_TYPES = {"date", "datetime"}


def _tokens(name: str) -> list[str]:
    return [
        token.casefold()
        for token in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name.replace("_", " "))
    ]


def _table_stem(table: TableProfile) -> str:
    unprefixed = re.sub(r"^(?:dim|fact|fct|tbl)[_ ]", "", table.name.casefold())
    name = re.sub(r"[^a-z0-9]+", "", unprefixed)
    for suffix, replacement in (("ies", "y"), ("sses", "ss"), ("ses", "s"), ("s", "")):
        if name.endswith(suffix) and len(name) > len(suffix) + 2:
            return name[: -len(suffix)] + replacement
    return name


def _key_stem(table: TableProfile, column: ColumnProfile) -> str:
    """The entity a key column names: CustomerKey, Customer ID, customer_id -> customer.

    A bare "ID" or "Key" names its own table's entity.
    """
    tokens = _tokens(column.name)
    if len(tokens) > 1 and tokens[-1] in _KEY_TOKENS:
        return "".join(tokens[:-1])
    if len(tokens) == 1 and tokens[0] in _KEY_TOKENS:
        return _table_stem(table)
    return re.sub(r"[^a-z0-9]+", "", column.name.casefold())


def key_like_name(name: str) -> bool:
    """Whether a column name reads as a key: CustomerID, Store ID, order_number, Key."""
    tokens = _tokens(name)
    return bool(tokens) and tokens[-1] in _KEY_TOKENS


def _key_like(column: ColumnProfile) -> bool:
    return key_like_name(column.name)


def _plausible_pair(
    left_table: TableProfile,
    left: ColumnProfile,
    right_table: TableProfile,
    right: ColumnProfile,
    *,
    name_match: bool,
    sample_overlap: float,
    date_dimensions: dict[str, ColumnProfile],
) -> bool:
    """Whether two columns could be a key relationship worth a full-snapshot check.

    Measured on a seven-table retail model: pairing small integers by sample overlap
    related Dates[Month] to Customers[CustomerID] and Customers[CustomerID] to
    Sales[SalesLineID]. Integer and date joins therefore need key evidence: a
    matching key name, or a date dimension's unique daily column.
    """
    left_date = left.data_type.value in _DATE_TYPES
    right_date = right.data_type.value in _DATE_TYPES
    if left_date or right_date:
        return (left_date and right_date) and (
            date_dimensions.get(left_table.id) == left
            or date_dimensions.get(right_table.id) == right
        )
    keyed = _key_like(left) or _key_like(right)
    if name_match and (keyed or left.candidate_key or right.candidate_key):
        return True
    left_stem, right_stem = _key_stem(left_table, left), _key_stem(right_table, right)
    if (
        keyed
        and left_stem
        and left_stem == right_stem
        and (_key_like(left) == _key_like(right) or left.data_type == right.data_type)
    ):
        return True
    # Text codes (SKUs, store codes) may be named differently but share values.
    return (
        left.data_type.value == right.data_type.value == "string"
        and (left.candidate_key or right.candidate_key)
        and max(left.distinct_count, right.distinct_count) >= 10
        and sample_overlap >= 0.6
    )


def _drop_fact_to_fact(
    relationships: list[RelationshipCandidate], labels: _Labels
) -> tuple[list[RelationshipCandidate], list[str]]:
    """Remove many-to-many links between tables that already share a dimension.

    Sales[ProductKey] and Returns[ProductKey] both belong to Products; relating
    the two facts directly is the classic modeling mistake.
    """
    dimension_of = {
        (item.from_table_id, item.from_column_id): item.to_table_id
        for item in relationships
        if item.cardinality == "many_to_one" and not item.blockers
    }
    kept: list[RelationshipCandidate] = []
    notes: list[str] = []
    for item in relationships:
        shared = dimension_of.get((item.from_table_id, item.from_column_id))
        if (
            item.cardinality == "many_to_many"
            and shared is not None
            and shared == dimension_of.get((item.to_table_id, item.to_column_id))
        ):
            notes.append(
                f"{labels.table(item.from_table_id)} and {labels.table(item.to_table_id)} "
                f"share {labels.table(shared)}; filter both "
                "through that dimension instead of relating them to each other."
            )
            continue
        kept.append(item)
    return kept, notes


def _role_playing_dates(
    relationships: list[RelationshipCandidate],
    manifest: SourceManifest,
    date_dimensions: dict[str, ColumnProfile],
) -> list[RelationshipCandidate]:
    """One active date relationship per fact; other dates stay inactive (role-playing).

    OrderDate and ShipDate cannot both filter Sales from one Dates table; the
    second is used per measure with USERELATIONSHIP.
    """
    order = {
        (table.id, column.id): index
        for table in manifest.tables
        for index, column in enumerate(table.columns)
    }
    names = {
        (table.id, column.id): column.name for table in manifest.tables for column in table.columns
    }
    groups: dict[tuple[str, str], list[RelationshipCandidate]] = {}
    for item in relationships:
        key = date_dimensions.get(item.to_table_id)
        if (
            key is not None
            and key.id == item.to_column_id
            and item.cardinality == "many_to_one"
            and not item.blockers
        ):
            groups.setdefault((item.from_table_id, item.to_table_id), []).append(item)
    inactive: dict[str, RelationshipCandidate] = {}
    for group in groups.values():

        def rank(item: RelationshipCandidate) -> tuple[int, int]:
            name = names[(item.from_table_id, item.from_column_id)].casefold()
            preferred = (
                0 if "order" in name else 1 if re.search(r"invoice|transaction|sale", name) else 2
            )
            return preferred, order[(item.from_table_id, item.from_column_id)]

        primary, *others = sorted(group, key=rank)
        for item in others:
            labels = _labels(manifest)
            date_ref = labels.column(item.to_table_id, item.to_column_id)
            primary_name = names[(primary.from_table_id, primary.from_column_id)]
            inactive[item.id] = item.model_copy(
                update={
                    "is_active": False,
                    "evidence": [
                        f"Role-playing date: inactive because {primary_name} is the "
                        "active date; use USERELATIONSHIP("
                        f"{labels.column(item.from_table_id, item.from_column_id)}, "
                        f"{date_ref}) in measures",
                        *_without_overlap(item.evidence),
                    ][:6],
                }
            )
    return [inactive.get(item.id, item) for item in relationships]


def _ambiguous_paths(edges: list[tuple[str, str, bool]]) -> bool:
    """Whether any table can filter another along two different relationship paths.

    Filters flow from the one side to the many side (both ways when bidirectional),
    so two facts sharing Products and Customers is not ambiguous even though the
    undirected graph has a loop.
    """
    graph: dict[str, list[tuple[str, int]]] = {}
    for edge_id, (one_side, many_side, both) in enumerate(edges):
        graph.setdefault(one_side, []).append((many_side, edge_id))
        if both:
            graph.setdefault(many_side, []).append((one_side, edge_id))

    def walk(
        node: str,
        reached: dict[str, int],
        visited: frozenset[str],
        used_edges: frozenset[int],
    ) -> bool:
        for target, edge_id in graph.get(node, ()):
            # Both direction on one relationship is valid; returning through
            # a different relationship closes a genuine filter cycle.
            if edge_id in used_edges:
                continue
            if target in visited:
                return True
            reached[target] = reached.get(target, 0) + 1
            if reached[target] > 1 or walk(
                target, reached, visited | {target}, used_edges | {edge_id}
            ):
                return True
        return False

    return any(walk(start, {}, frozenset({start}), frozenset()) for start in graph)


@dataclass(frozen=True, slots=True)
class _Labels:
    tables: dict[str, str]
    columns: dict[tuple[str, str], str]

    def table(self, table_id: str) -> str:
        return self.tables.get(table_id, table_id)

    def column(self, table_id: str, column_id: str) -> str:
        """DAX-style reference, e.g. 'Sales'[ShipDate]."""
        table = self.table(table_id).replace("'", "''")
        return f"'{table}'[{self.columns.get((table_id, column_id), column_id)}]"


def _labels(manifest: SourceManifest) -> _Labels:
    return _Labels(
        tables={table.id: table.name for table in manifest.tables},
        columns={
            (table.id, column.id): column.name
            for table in manifest.tables
            for column in table.columns
        },
    )


def _without_overlap(evidence: list[str]) -> list[str]:
    """Evidence minus the sample-overlap line, making room within the six-item limit."""
    return [item for item in evidence if not item.startswith("Bounded sample overlap")]


def _sample_overlap(left: ColumnProfile, right: ColumnProfile) -> float:
    left_values = {str(value).casefold() for value in left.sample_values}
    right_values = {str(value).casefold() for value in right.sample_values}
    return len(left_values & right_values) / max(1, min(len(left_values), len(right_values)))


def _relationship_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())
    for suffix in ("identifier", "number", "code"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
    return normalized


def _relationship_id(left: _Endpoint, right: _Endpoint) -> str:
    endpoints = sorted((_endpoint_key(left), _endpoint_key(right)))
    raw = "|".join(f"{table}.{column}" for table, column in endpoints)
    readable = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")[:64]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"rel_{readable}_{digest}"


def _endpoint_key(endpoint: _Endpoint) -> tuple[str, str]:
    return endpoint.table.id, endpoint.column.id


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _key_expression(endpoint: _Endpoint, *, alias: str | None = None) -> str:
    """Match Import key equality; retain leading spaces and never mutate source rows."""
    column = _quote_identifier(endpoint.column.name)
    if alias is not None:
        column = f"{alias}.{column}"
    if endpoint.column.data_type.value == "string":
        return f"NULLIF(lower(rtrim({column})), '')"
    return column
