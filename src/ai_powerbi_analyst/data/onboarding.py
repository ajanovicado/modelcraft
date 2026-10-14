"""Versioned local source snapshots, bounded profiling, and relationship proposals."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from ai_powerbi_analyst.contracts import (
    ColumnDataType,
    ColumnProfile,
    RelationshipCandidate,
    SourceFile,
    SourceKind,
    SourceManifest,
    TableProfile,
)
from ai_powerbi_analyst.integrity import sha256_file

_SUPPORTED_SUFFIXES = {".csv", ".xlsx", ".xlsm"}
_DISTINCT_LIMIT = 100_000
_SAMPLE_LIMIT = 8


@dataclass(frozen=True, slots=True)
class UploadedSource:
    name: str
    content: bytes
    original_path: str | None = None


class DataOnboardingError(RuntimeError):
    """A selected local source could not be snapshotted and profiled safely."""


class _Accumulator:
    def __init__(
        self,
        name: str,
        identifier: str,
        declared_type: ColumnDataType | None = None,
        distinct_limit: int = _DISTINCT_LIMIT,
    ) -> None:
        self.name = name
        self.identifier = identifier
        self.declared_type = declared_type
        self.distinct_limit = distinct_limit
        self.row_count = 0
        self.null_count = 0
        self.samples: list[Any] = []
        self.distinct: set[str] = set()
        self.distinct_exact = True
        self.kinds: set[ColumnDataType] = set()
        self.minimum: Any = None
        self.maximum: Any = None

    def add(self, value: Any) -> None:
        self.row_count += 1
        if _is_null(value):
            self.null_count += 1
            return
        kind = self.declared_type or _classify(value)
        scalar = _coerce_scalar(value, kind)
        self.kinds.add(kind)
        marker = json.dumps(scalar, ensure_ascii=True, sort_keys=True)
        if self.distinct_exact:
            if len(self.distinct) < self.distinct_limit:
                self.distinct.add(marker)
            elif marker not in self.distinct:
                self.distinct_exact = False
        if scalar not in self.samples and len(self.samples) < _SAMPLE_LIMIT:
            self.samples.append(scalar)
        if kind in {
            ColumnDataType.INTEGER,
            ColumnDataType.NUMBER,
            ColumnDataType.DATE,
            ColumnDataType.DATETIME,
        }:
            try:
                if self.minimum is None or scalar < self.minimum:
                    self.minimum = scalar
                if self.maximum is None or scalar > self.maximum:
                    self.maximum = scalar
            except TypeError:
                self.minimum = None
                self.maximum = None

    def profile(self) -> ColumnProfile:
        data_type = self.declared_type or _merge_types(self.kinds)
        distinct_count = len(self.distinct)
        candidate_key = (
            self.row_count > 0
            and self.null_count == 0
            and self.distinct_exact
            and distinct_count == self.row_count
        )
        return ColumnProfile(
            id=self.identifier,
            name=self.name,
            data_type=data_type,
            row_count=self.row_count,
            null_count=self.null_count,
            distinct_count=distinct_count,
            distinct_count_exact=self.distinct_exact,
            candidate_key=candidate_key,
            sample_values=self.samples,
            minimum=self.minimum,
            maximum=self.maximum,
            geographic_role=_geographic_role(self.name),
        )


class DataOnboardingService:
    def __init__(self, state_root: str | Path) -> None:
        self.state_root = Path(state_root).expanduser().resolve(strict=False)
        self.sources_root = self.state_root / "sources"
        self.manifests_root = self.state_root / "manifests"

    @property
    def latest_manifest_path(self) -> Path:
        return self.manifests_root / "latest.json"

    def import_uploads(self, uploads: Iterable[UploadedSource]) -> SourceManifest:
        selected = tuple(uploads)
        if not selected:
            raise DataOnboardingError("Select at least one CSV or Excel file")
        import_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        source_records: list[SourceFile] = []
        table_profiles: list[TableProfile] = []
        for upload in selected:
            suffix = Path(upload.name).suffix.casefold()
            if suffix not in _SUPPORTED_SUFFIXES:
                raise DataOnboardingError(f"Unsupported source type: {upload.name}")
            digest = hashlib.sha256(upload.content).hexdigest()
            safe_name = _safe_filename(Path(upload.name).name)
            snapshot_dir = self.sources_root / digest
            snapshot_path = snapshot_dir / safe_name
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            if snapshot_path.exists():
                if not snapshot_path.is_file() or sha256_file(snapshot_path) != digest:
                    raise DataOnboardingError("Content-addressed source snapshot is inconsistent")
            else:
                temporary = snapshot_dir / f".{safe_name}.{uuid.uuid4().hex}.tmp"
                temporary.write_bytes(upload.content)
                if sha256_file(temporary) != digest:
                    temporary.unlink(missing_ok=True)
                    raise DataOnboardingError("Source snapshot hash verification failed")
                os.replace(temporary, snapshot_path)
            source_id = _identifier(Path(safe_name).stem) + "_" + digest[:8]
            if suffix == ".csv":
                encoding, delimiter, tables = self._profile_csv(snapshot_path, source_id)
                sheets: list[str] = []
                kind = SourceKind.CSV
            else:
                encoding = None
                tables = self._profile_excel(snapshot_path, source_id)
                sheets = [table.sheet or table.name for table in tables]
                kind = SourceKind.EXCEL
            source_records.append(
                SourceFile(
                    id=source_id,
                    original_name=upload.name,
                    original_path=upload.original_path,
                    snapshot_path=str(snapshot_path),
                    sha256=digest,
                    original_sha256=digest,
                    size_bytes=len(upload.content),
                    kind=kind,
                    original_kind=kind,
                    sheets=sheets,
                    encoding=encoding,
                    delimiter=delimiter if suffix == ".csv" else None,
                )
            )
            table_profiles.extend(tables)
        manifest = SourceManifest(
            import_id=import_id,
            imported_at=datetime.now(UTC),
            sources=source_records,
            tables=table_profiles,
        )
        self._persist_manifest(manifest)
        return manifest

    def import_paths(self, paths: Iterable[str | Path]) -> SourceManifest:
        uploads: list[UploadedSource] = []
        for value in paths:
            path = Path(value).expanduser().resolve(strict=True)
            if not path.is_file():
                raise DataOnboardingError(f"Source is not a file: {path}")
            uploads.append(
                UploadedSource(name=path.name, content=path.read_bytes(), original_path=str(path))
            )
        return self.import_uploads(uploads)

    def load_latest(self) -> SourceManifest | None:
        if not self.latest_manifest_path.is_file():
            return None
        try:
            return SourceManifest.model_validate_json(
                self.latest_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise DataOnboardingError("Latest source manifest is invalid") from error

    def relink_source(
        self,
        manifest: SourceManifest,
        source_id: str,
        original_path: str | Path,
    ) -> SourceManifest:
        path = Path(original_path).expanduser().resolve(strict=True)
        source = next((item for item in manifest.sources if item.id == source_id), None)
        if source is None:
            raise DataOnboardingError(f"Unknown source ID: {source_id}")
        if not path.is_file() or path.suffix.casefold() not in _SUPPORTED_SUFFIXES:
            raise DataOnboardingError("Relink target must be a supported local source file")
        if sha256_file(path) != source.sha256:
            raise DataOnboardingError(
                "Relink target content differs from the reviewed snapshot; import it as new data"
            )
        updated = manifest.model_copy(
            update={
                "sources": [
                    item.model_copy(update={"original_path": str(path)})
                    if item.id == source_id
                    else item
                    for item in manifest.sources
                ]
            }
        )
        self.manifests_root.mkdir(parents=True, exist_ok=True)
        relink_root = self.manifests_root / "relinks"
        relink_root.mkdir(parents=True, exist_ok=True)
        evidence = relink_root / f"{manifest.import_id}-{uuid.uuid4().hex}.json"
        payload = updated.model_dump_json(indent=2) + "\n"
        evidence.write_text(payload, encoding="utf-8")
        temporary = self.latest_manifest_path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.latest_manifest_path)
        return updated

    def propose_relationships(self, manifest: SourceManifest) -> list[RelationshipCandidate]:
        if all(source.kind == SourceKind.PARQUET for source in manifest.sources):
            # Imported lazily so the foundational onboarding module stays usable by
            # the legacy CSV/Excel flow without introducing an import cycle.
            from ai_powerbi_analyst.studio.relationship_engine import (
                RelationshipGovernanceEngine,
            )

            result = RelationshipGovernanceEngine(
                self.state_root / "duckdb-spill" / "relationships"
            ).propose(manifest)
            return list(result.relationships)
        candidates: list[RelationshipCandidate] = []
        for left_index, left in enumerate(manifest.tables):
            for right in manifest.tables[left_index + 1 :]:
                candidates.extend(_table_relationships(left, right))
        return sorted(candidates, key=lambda item: (-item.confidence, item.id))

    def _profile_csv(
        self,
        path: Path,
        source_id: str,
    ) -> tuple[str, str, list[TableProfile]]:
        encoding = _detect_encoding(path)
        delimiter = _detect_delimiter(path, encoding)
        try:
            chunks = pd.read_csv(
                path,
                encoding=encoding,
                sep=delimiter,
                chunksize=25_000,
                dtype=object,
                keep_default_na=True,
            )
            profile = _profile_frames(chunks, source_id, Path(path).stem, sheet=None)
        except (OSError, UnicodeError, pd.errors.ParserError, ValueError) as error:
            raise DataOnboardingError(f"Could not read CSV {path.name}: {error}") from error
        return encoding, delimiter, [profile]

    def _profile_excel(self, path: Path, source_id: str) -> list[TableProfile]:
        try:
            workbook = pd.ExcelFile(path, engine="openpyxl")
            profiles: list[TableProfile] = []
            for raw_sheet in workbook.sheet_names:
                sheet = str(raw_sheet)
                frame = pd.read_excel(
                    workbook,
                    sheet_name=sheet,
                    dtype=object,
                    engine="openpyxl",
                )
                if len(frame.columns) == 0:
                    continue
                table_id = f"{source_id}_{_identifier(sheet)}"
                profiles.append(
                    _profile_frames(
                        (frame,),
                        table_id,
                        sheet,
                        sheet=sheet,
                        source_id=source_id,
                    )
                )
            if not profiles:
                raise DataOnboardingError(f"Excel workbook {path.name} has no tabular sheets")
            return profiles
        except DataOnboardingError:
            raise
        except (OSError, ValueError, ImportError) as error:
            raise DataOnboardingError(
                f"Could not read Excel workbook {path.name}: {error}"
            ) from error

    def _persist_manifest(self, manifest: SourceManifest) -> None:
        self.manifests_root.mkdir(parents=True, exist_ok=True)
        payload = manifest.model_dump_json(indent=2) + "\n"
        immutable = self.manifests_root / f"{manifest.import_id}.json"
        if immutable.exists():
            raise DataOnboardingError("Import manifest ID already exists")
        immutable.write_text(payload, encoding="utf-8")
        temporary = self.latest_manifest_path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.latest_manifest_path)


def _profile_frames(
    frames: Iterable[pd.DataFrame],
    table_id: str,
    display_name: str,
    *,
    sheet: str | None,
    source_id: str | None = None,
    declared_types: dict[str, ColumnDataType] | None = None,
) -> TableProfile:
    accumulators: list[_Accumulator] | None = None
    total = 0
    for frame in frames:
        names = _unique_names(str(value) for value in frame.columns)
        if accumulators is None:
            # A table-wide distinct-value budget avoids multiplying the memory cap
            # by every column in wide datasets. Counts become explicit lower bounds.
            distinct_limit = min(_DISTINCT_LIMIT, max(1, 500_000 // len(names)))
            accumulators = []
            used_ids: set[str] = set()
            for column_index, name in enumerate(names):
                base_id = _identifier(name)
                column_id = base_id
                suffix = 2
                while column_id in used_ids:
                    column_id = f"{base_id[:85]}_{suffix}"
                    suffix += 1
                used_ids.add(column_id)
                accumulators.append(
                    _Accumulator(
                        name,
                        column_id,
                        (declared_types or {}).get(
                            str(frame.columns[column_index]), (declared_types or {}).get(name)
                        ),
                        distinct_limit,
                    )
                )
        elif [item.name for item in accumulators] != names:
            raise DataOnboardingError("Source columns changed while it was being profiled")
        total += len(frame)
        for row in frame.itertuples(index=False, name=None):
            for accumulator, value in zip(accumulators, row, strict=True):
                accumulator.add(value)
    if not accumulators:
        raise DataOnboardingError(f"Table {display_name} has no columns")
    return TableProfile(
        id=_identifier(table_id),
        name=display_name,
        source_id=_identifier(source_id or table_id),
        sheet=sheet,
        row_count=total,
        columns=[item.profile() for item in accumulators],
    )


def _table_relationships(left: TableProfile, right: TableProfile) -> list[RelationshipCandidate]:
    matches: list[RelationshipCandidate] = []
    for left_column in left.columns:
        for right_column in right.columns:
            name_match = _relationship_name(left_column.name) == _relationship_name(
                right_column.name
            )
            values_left = {str(value).casefold() for value in left_column.sample_values}
            values_right = {str(value).casefold() for value in right_column.sample_values}
            overlap = len(values_left & values_right) / max(
                1,
                min(len(values_left), len(values_right)),
            )
            if not name_match and overlap < 0.6:
                continue
            blockers: list[str] = []
            if left_column.data_type != right_column.data_type:
                blockers.append("Column data types differ")
            left_key = left_column.candidate_key
            right_key = right_column.candidate_key
            if left_key and right_key:
                cardinality: Literal["many_to_one", "one_to_one", "many_to_many"] = "one_to_one"
            elif left_key or right_key:
                cardinality = "many_to_one"
            else:
                cardinality = "many_to_many"
                blockers.append("Neither side is a verified unique key")
            confidence = min(0.99, 0.45 + (0.35 if name_match else 0) + 0.2 * overlap)
            pair = sorted((left.id, right.id))
            identifier = _identifier(f"rel_{pair[0]}_{left_column.id}_{pair[1]}_{right_column.id}")
            matches.append(
                RelationshipCandidate(
                    id=identifier,
                    from_table_id=left.id,
                    from_column_id=left_column.id,
                    to_table_id=right.id,
                    to_column_id=right_column.id,
                    cardinality=cardinality,
                    confidence=round(confidence, 3),
                    evidence=[
                        "Normalized column names match" if name_match else "Sample values overlap",
                        f"Bounded sample overlap: {overlap:.0%}",
                    ],
                    blockers=blockers,
                )
            )
    return matches


def _detect_encoding(path: Path) -> str:
    with path.open("rb") as stream:
        sample = stream.read(131_072)
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    raise DataOnboardingError(f"Could not determine CSV encoding for {path.name}")


def _detect_delimiter(path: Path, encoding: str) -> str:
    with path.open("r", encoding=encoding, errors="strict") as stream:
        sample = stream.read(131_072)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        return ","


def _safe_filename(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .")
    if not sanitized or sanitized in {".", ".."}:
        raise DataOnboardingError("Source filename is invalid")
    return sanitized[:180]


def _identifier(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not normalized:
        normalized = "field"
    if not normalized[0].isalpha():
        normalized = "f_" + normalized
    return normalized[:96]


def _unique_names(values: Iterable[str]) -> list[str]:
    used: set[str] = set()
    names: list[str] = []
    for index, raw in enumerate(values, start=1):
        base = " ".join(raw.split()) or f"Column {index}"
        name, suffix = base, 2
        while name in used:
            name = f"{base} {suffix}"
            suffix += 1
        used.add(name)
        names.append(name)
    return names


def _json_scalar(value: Any) -> str | int | float | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        item = value.item()
        if item is not value:
            return _json_scalar(item)
    return str(value)


def _coerce_scalar(value: Any, kind: ColumnDataType) -> str | int | float | bool:
    if kind is ColumnDataType.INTEGER:
        return int(value)
    if kind is ColumnDataType.NUMBER:
        return float(value)
    if kind is ColumnDataType.BOOLEAN:
        if isinstance(value, str):
            return value.strip().casefold() in {"true", "yes"}
        return bool(value)
    if kind in {ColumnDataType.DATE, ColumnDataType.DATETIME}:
        parsed = pd.to_datetime(value, errors="raise")
        return str(parsed.isoformat())
    return _json_scalar(value)


def _is_null(value: Any) -> bool:
    try:
        result = pd.isna(value)
        if isinstance(result, bool):
            return result
        if hasattr(result, "item"):
            scalar = result.item()
            return bool(scalar) if isinstance(scalar, bool) else False
        return False
    except (TypeError, ValueError):
        return value is None


def _classify(value: Any) -> ColumnDataType:
    if isinstance(value, bool):
        return ColumnDataType.BOOLEAN
    if isinstance(value, int) and not isinstance(value, bool):
        return ColumnDataType.INTEGER
    if isinstance(value, float):
        return ColumnDataType.NUMBER
    if isinstance(value, datetime):
        return ColumnDataType.DATETIME
    if isinstance(value, (date, pd.Timestamp)):
        return ColumnDataType.DATE
    text = str(value).strip()
    if re.fullmatch(r"[-+]?\d+", text):
        return ColumnDataType.INTEGER
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", text):
        return ColumnDataType.NUMBER
    if text.casefold() in {"true", "false", "yes", "no"}:
        return ColumnDataType.BOOLEAN
    if not re.search(r"[-/:]", text):
        return ColumnDataType.STRING
    parsed = pd.to_datetime(text, errors="coerce")
    if not pd.isna(parsed):
        return ColumnDataType.DATETIME if re.search(r"\d:\d", text) else ColumnDataType.DATE
    return ColumnDataType.STRING


def _merge_types(kinds: set[ColumnDataType]) -> ColumnDataType:
    if not kinds:
        return ColumnDataType.STRING
    if kinds <= {ColumnDataType.INTEGER}:
        return ColumnDataType.INTEGER
    if kinds <= {ColumnDataType.INTEGER, ColumnDataType.NUMBER}:
        return ColumnDataType.NUMBER
    if kinds <= {ColumnDataType.DATE}:
        return ColumnDataType.DATE
    if kinds <= {ColumnDataType.DATE, ColumnDataType.DATETIME}:
        return ColumnDataType.DATETIME
    if len(kinds) == 1:
        return next(iter(kinds))
    return ColumnDataType.STRING


def _geographic_role(
    name: str,
) -> Literal["country", "state", "city", "postal_code"] | None:
    normalized = _identifier(name)
    if normalized in {"country", "country_code", "nation"}:
        return "country"
    if normalized in {"state", "province", "region_code"}:
        return "state"
    if normalized in {"city", "town"}:
        return "city"
    if normalized in {"zip", "zipcode", "postal", "postal_code"}:
        return "postal_code"
    return None


def _relationship_name(value: str) -> str:
    normalized = _identifier(value)
    for suffix in ("_id", "_key", "_code"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
    return normalized.removeprefix("dim_").removeprefix("fact_")
