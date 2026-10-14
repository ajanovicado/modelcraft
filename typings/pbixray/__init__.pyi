"""Public PBIXRay 0.15.5 surface used by the read-only extractor.

The private metadata adapter is dynamically discovered for version compatibility.
"""

from collections.abc import Iterator
from types import TracebackType
from typing import Any

from pandas import DataFrame

class PBIXRay:
    _metadata: Any
    tables: list[str]
    relationships: DataFrame
    dax_tables: DataFrame
    dax_columns: DataFrame
    dax_measures: DataFrame
    schema: DataFrame
    def __init__(
        self, file_path: str, *, on_disk: bool = ..., temp_dir: str | None = ...
    ) -> None: ...
    def __enter__(self) -> PBIXRay: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
    def iter_table(
        self, table_name: str, *, chunk_size: int = ..., strings_as_categorical: bool = ...
    ) -> Iterator[DataFrame]: ...
