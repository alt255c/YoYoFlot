"""Tests for the canonical T05 BoardingData CSV reader."""

import csv
from pathlib import Path

from yoyoflot.readers import boarding_csv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "boarding_data_test.csv"


def collect(path):
    tables = {}
    for batch in boarding_csv.read(path, batch_size=2):
        tables.setdefault(batch.table_name, []).extend(batch.records)
    return tables


def test_fixture_exists():
    assert FIXTURE_PATH.is_file()


def test_reader_preserves_strings_locators_and_raw_values():
    data = collect(FIXTURE_PATH)

    people = data["person_observations"]
    identifiers = data["identifiers"]
    travels = data["travel_observations"]

    assert len(people) == len(travels) == 3
    assert people[0]["birth_date"] == "1990-01-01"
    assert people[0]["locator"] == "line:2-2"
    assert {row["value"] for row in identifiers if row["kind"] == "document"} == {
        "0012 345678",
        "  9988 776655  ",
    }
    assert travels[0]["ticket_number"] == "000987654321"
    assert travels[0]["origin_raw"] is None
    assert travels[0]["raw_json"]


def test_missing_values_and_invalid_dates_are_explicit(tmp_path):
    path = tmp_path / "boarding.csv"
    row = dict.fromkeys(boarding_csv.EXPECTED_COLUMNS, "")
    row.update(
        PassengerFirstName="ANNA",
        PassengerDocument="000123",
        TicketNumber="0001",
        FlightDate="2017-02-30",
        FlightTime="23:30",
        Destination="BBB",
    )
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(boarding_csv.EXPECTED_COLUMNS)
        writer.writerow([row[column] for column in boarding_csv.EXPECTED_COLUMNS])
        writer.writerow([])
        writer.writerow(["wrong", "count"])

    data = collect(path)
    assert len(data["person_observations"]) == 1
    assert data["travel_observations"][0]["departure_local"] is None
    assert {row["reason"] for row in data["errors"]} == {
        "blank_row",
        "column_count:2",
        "invalid_departure_date",
    }


def test_legacy_import_points_to_canonical_reader():
    from readers import boarding_csv as legacy

    assert legacy.read is boarding_csv.read
    assert legacy.EXPECTED_COLUMNS == boarding_csv.EXPECTED_COLUMNS
