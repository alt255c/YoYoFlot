"""
Reader for BoardingData.csv source (T03).
Chunked reading with PyArrow, preserving leading zeros and raw values.
All paths are anchored to the project root via __file__.
"""

import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pcsv

# Корень проекта = родитель папки readers/
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Путь к данным привязан к корню проекта, а не к рабочей папке запуска
DEFAULT_DATA_PATH = PROJECT_ROOT / "data" / "BoardingData.csv"

EXPECTED_COLUMNS = [
    "PassengerFirstName", "PassengerSecondName", "PassengerLastName",
    "PassengerSex", "PassengerBirthDate", "PassengerDocument",
    "BookingCode", "TicketNumber", "Baggage", "FlightDate",
    "FlightTime", "FlightNumber", "CodeShare", "Destination",
]

NULL_INDICATORS = ["Not presented", "None", ""]
NULL_TARGET_COLUMNS = [
    "PassengerDocument", "TicketNumber", "BookingCode", "Baggage", "CodeShare",
]


def _invalid_row_handler(row):
    """Повреждённые строки не теряются молча: предупреждение + skip."""
    warnings.warn(
        f"Malformed CSV row skipped: expected {row.expected_columns} columns, "
        f"got {row.actual_columns}: {row.text[:120]!r}"
    )
    return "skip"


def read_boarding_data_chunked(
    file_path: str | Path = DEFAULT_DATA_PATH,
    chunk_size: int = 10000,
) -> Iterator[pa.Table]:
    """
    Чанковое чтение CSV через PyArrow.

    - все колонки читаются как string (ведущие нули сохраняются);
    - пустые строки игнорируются;
    - повреждённые строки -> warning + skip (не молча);
    - к каждой строке добавляются source_file, row_number, origin (= null).
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"BoardingData file not found: {file_path}")

    read_kwargs = {"column_names": EXPECTED_COLUMNS, "skip_rows": 1}
    try:
        read_options = pcsv.ReadOptions(invalid_row_handler=_invalid_row_handler, **read_kwargs)
    except TypeError:  # старый pyarrow без invalid_row_handler
        read_options = pcsv.ReadOptions(**read_kwargs)

    parse_options = pcsv.ParseOptions(delimiter=";")
    convert_options = pcsv.ConvertOptions(
        column_types={col: pa.string() for col in EXPECTED_COLUMNS}
    )

    table = pcsv.read_csv(
        file_path,
        read_options=read_options,
        parse_options=parse_options,
        convert_options=convert_options,
    )

    # Отбрасываем строки, где первая колонка пустая/null (дефектные записи)
    first = table.column("PassengerFirstName")
    mask = pc.or_kleene(
        pc.equal(first, ""),
        pc.is_null(first),
    )
    table = table.filter(pc.invert(mask))

    total_rows = len(table)
    row_offset = 0
    for start in range(0, total_rows, chunk_size):
        chunk = table.slice(start, min(chunk_size, total_rows - start))

        chunk = chunk.append_column(
            "source_file",
            pa.array([file_path.name] * len(chunk), type=pa.string()),
        )
        chunk = chunk.append_column(
            "row_number",
            pa.array(range(row_offset + 1, row_offset + len(chunk) + 1), type=pa.int64()),
        )
        # origin явно отсутствует: не угадывается и не выводится из других полей
        chunk = chunk.append_column(
            "origin",
            pa.nulls(len(chunk), type=pa.string()),
        )
        row_offset += len(chunk)
        yield chunk


def normalize_nulls(table: pa.Table) -> pa.Table:
    """
    Явная нормализация: "Not presented" / "None" / "" -> null
    в колонках NULL_TARGET_COLUMNS. Raw-значения доступны до этого шага.
    """
    for col_name in NULL_TARGET_COLUMNS:
        if col_name not in table.column_names:
            continue
        idx = table.column_names.index(col_name)
        col = table.column(col_name)
        mask = pc.is_in(col, pa.array(NULL_INDICATORS, type=pa.string()))
        col = pc.if_else(mask, pa.nulls(len(col), type=pa.string()), col)
        table = table.set_column(idx, col_name, col)
    return table


def get_boarding_data_summary(file_path: str | Path = DEFAULT_DATA_PATH) -> dict[str, Any]:
    """Source quality summary без единовременной загрузки всего файла в RAM."""
    file_path = Path(file_path)
    total_rows = 0
    null_docs = 0
    null_tickets = 0

    for chunk in read_boarding_data_chunked(file_path, chunk_size=20000):
        chunk = normalize_nulls(chunk)
        total_rows += len(chunk)
        null_docs += chunk.column("PassengerDocument").null_count
        null_tickets += chunk.column("TicketNumber").null_count

    return {
        "source_file": file_path.name,
        "total_processed_rows": total_rows,
        "null_passenger_documents": null_docs,
        "null_ticket_numbers": null_tickets,
        "note": "Booking/boarding observation is NOT proven actual travel. Origin is explicitly NA.",
    }


if __name__ == "__main__":
    summary = get_boarding_data_summary()
    print("BoardingData Summary:")
    for key, value in summary.items():
        print(f"  {key}: {value}")