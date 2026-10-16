"""Backward-compatible import for the canonical T05 reader.

The production implementation lives under ``yoyoflot.readers``. Keeping this
module as a thin adapter prevents the legacy PyCharm import from drifting into
a second, non-streaming implementation.
"""

from pathlib import Path
from collections.abc import Iterator
from typing import Any
import warnings
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pcsv
from yoyoflot.readers.boarding_csv import EXPECTED_COLUMNS, read

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_PATH = PROJECT_ROOT / "data" / "BoardingData.csv"
NULL_INDICATORS = ["Not presented", "None", ""]
NULL_TARGET_COLUMNS = ["PassengerDocument", "TicketNumber", "BookingCode", "Baggage", "CodeShare"]

def _invalid_row_handler(row):
    warnings.warn(f"Malformed CSV row skipped: expected {row.expected_columns} columns, got {row.actual_columns}: {row.text[:120]!r}")
    return "skip"

def read_boarding_data_chunked(file_path: str | Path = DEFAULT_DATA_PATH, chunk_size: int = 10000) -> Iterator[pa.Table]:
    """Legacy PyArrow API, retaining string identifiers and source metadata."""
    file_path = Path(file_path)
    if not file_path.exists(): raise FileNotFoundError(f"BoardingData file not found: {file_path}")
    try: ro = pcsv.ReadOptions(column_names=EXPECTED_COLUMNS, skip_rows=1, invalid_row_handler=_invalid_row_handler)
    except TypeError: ro = pcsv.ReadOptions(column_names=EXPECTED_COLUMNS, skip_rows=1)
    table = pcsv.read_csv(file_path, read_options=ro, parse_options=pcsv.ParseOptions(delimiter=";"), convert_options=pcsv.ConvertOptions(column_types={c: pa.string() for c in EXPECTED_COLUMNS}))
    good = pc.or_kleene(pc.not_equal(table.column("PassengerFirstName"), ""), pc.is_null(table.column("PassengerFirstName")))
    table = table.filter(good)
    for start in range(0, len(table), chunk_size):
        chunk = table.slice(start, chunk_size)
        chunk = chunk.append_column("source_file", pa.array([file_path.name] * len(chunk), type=pa.string()))
        chunk = chunk.append_column("row_number", pa.array(range(start + 1, start + len(chunk) + 1), type=pa.int64()))
        yield chunk.append_column("origin", pa.nulls(len(chunk), type=pa.string()))

def normalize_nulls(table: pa.Table) -> pa.Table:
    for name in NULL_TARGET_COLUMNS:
        if name in table.column_names:
            i = table.column_names.index(name)
            col = table.column(name)
            table = table.set_column(i, name, pc.if_else(pc.is_in(col, pa.array(NULL_INDICATORS)), pa.nulls(len(col), type=pa.string()), col))
    return table

def get_boarding_data_summary(file_path: str | Path = DEFAULT_DATA_PATH) -> dict[str, Any]:
    total = docs = tickets = 0
    for chunk in read_boarding_data_chunked(file_path, 20000):
        chunk = normalize_nulls(chunk); total += len(chunk)
        docs += chunk.column("PassengerDocument").null_count; tickets += chunk.column("TicketNumber").null_count
    return {"source_file": Path(file_path).name, "total_processed_rows": total, "null_passenger_documents": docs, "null_ticket_numbers": tickets, "note": "Booking/boarding observation is NOT proven actual travel. Origin is explicitly NA."}

__all__ = ["EXPECTED_COLUMNS", "read", "read_boarding_data_chunked", "normalize_nulls", "get_boarding_data_summary"]
