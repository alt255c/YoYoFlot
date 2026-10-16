"""
Tests for BoardingData CSV reader (T03).
Пути привязаны к корню проекта через __file__ — работают из PyCharm, терминала и CI.
"""

import sys
from pathlib import Path

import pyarrow as pa

# Корень проекта = родитель папки tests/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from readers.boarding_csv import (
    get_boarding_data_summary,
    normalize_nulls,
    read_boarding_data_chunked,
)

# АБСОЛЮТНЫЙ путь к фикстуре: не зависит от рабочей папки запуска
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "boarding_data_test.csv"


def test_fixture_exists():
    assert FIXTURE_PATH.is_file(), f"Fixture not found: {FIXTURE_PATH}"


def test_chunked_reading():
    """3 строки при chunk_size=2 -> ровно 2 чанка; ведущие нули сохранены."""
    chunks = list(read_boarding_data_chunked(FIXTURE_PATH, chunk_size=2))
    assert len(chunks) == 2, f"Expected 2 chunks, got {len(chunks)}"

    combined = pa.concat_tables(chunks)
    assert combined.column("PassengerDocument")[0].as_py() == "0012 345678"
    assert combined.column("TicketNumber")[0].as_py() == "000987654321"


def test_origin_is_null():
    chunks = list(read_boarding_data_chunked(FIXTURE_PATH, chunk_size=10))
    combined = pa.concat_tables(chunks)
    assert "origin" in combined.column_names
    assert combined.column("origin").null_count == len(combined)


def test_provenance_columns():
    chunks = list(read_boarding_data_chunked(FIXTURE_PATH, chunk_size=10))
    combined = pa.concat_tables(chunks)

    assert combined.column("source_file")[0].as_py() == "boarding_data_test.csv"
    assert [v.as_py() for v in combined.column("row_number")] == [1, 2, 3]


def test_normalize_nulls():
    chunks = list(read_boarding_data_chunked(FIXTURE_PATH, chunk_size=10))
    normalized = normalize_nulls(pa.concat_tables(chunks))

    # "Not presented" -> null
    assert normalized.column("PassengerDocument")[1].as_py() is None
    # "None" в Baggage -> null
    assert normalized.column("Baggage")[1].as_py() is None
    # нормальное значение не тронуто
    assert normalized.column("PassengerDocument")[0].as_py() == "0012 345678"


def test_raw_whitespace_preserved():
    """Raw-значения сохраняются как есть (пробелы из фикстуры в кавычках)."""
    chunks = list(read_boarding_data_chunked(FIXTURE_PATH, chunk_size=10))
    combined = pa.concat_tables(chunks)
    assert combined.column("PassengerDocument")[2].as_py() == "  9988 776655  "


def test_quality_summary():
    summary = get_boarding_data_summary(FIXTURE_PATH)
    assert summary["total_processed_rows"] == 3
    assert summary["null_passenger_documents"] == 1
    assert summary["null_ticket_numbers"] == 1
    assert "NOT proven actual travel" in summary["note"]


if __name__ == "__main__":
    # Диагностика путей + запуск тестов без pytest
    import os
    print("cwd:", os.getcwd())
    print("PROJECT_ROOT:", PROJECT_ROOT)
    print("FIXTURE_PATH:", FIXTURE_PATH, "exists:", FIXTURE_PATH.is_file())

    for fn in (
        test_fixture_exists,
        test_chunked_reading,
        test_origin_is_null,
        test_provenance_columns,
        test_normalize_nulls,
        test_raw_whitespace_preserved,
        test_quality_summary,
    ):
        fn()
        print(f"✓ {fn.__name__} passed")
    print("\nAll tests passed!")