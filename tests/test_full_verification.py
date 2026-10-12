import csv
import hashlib
import json

import pytest

import check_all_data as checker


def test_balance_does_not_accept_missing_source_rows():
    census = {"boarding_csv": {"valid_width": 20}}
    with pytest.raises(ValueError, match="Баланс"):
        checker.check_balance(
            "boarding_csv",
            {"person_observations": 19, "travel_observations": 20},
            census,
        )


def test_coverage_checks_sheet_names_not_only_counts(tmp_path):
    inventory = tmp_path / "inventory.csv"
    with inventory.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["workbook", "sheets", "sheet_names_sha256"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "workbook": "book.xlsx",
                "sheets": 1,
                "sheet_names_sha256": hashlib.sha256(
                    json.dumps(["Correct"]).encode()
                ).hexdigest(),
            }
        )
    with (tmp_path / "workbook_coverage.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "workbook",
                "sheet",
                "status",
                "header_version",
                "input_rows",
                "accepted_rows",
                "errors",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "workbook": "book.xlsx",
                "sheet": "Wrong",
                "status": "ok",
                "header_version": "boarding_card_v1",
                "input_rows": 1,
                "accepted_rows": 1,
                "errors": 0,
            }
        )
    with pytest.raises(ValueError, match="Имена"):
        checker.check_workbook_coverage(tmp_path, inventory)


def test_pdf_exports_are_checked_record_by_record(tmp_path):
    import pymupdf
    from yoyoflot.readers import timetable_pdf

    pdf = tmp_path / "tiny.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text(
            (40, 40), "Covers period: 01 Nov 2018 through 31 Jan 2019"
        )
        document.save(pdf)
    output = tmp_path / "out"
    report = timetable_pdf.process(pdf, output)
    assert checker.check_pdf(output, pdf, report)["exports"] == "equivalent"
    (output / "schedules.jsonl").write_text('{"unexpected":"row"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Количество"):
        checker.check_pdf(output, pdf, report)
