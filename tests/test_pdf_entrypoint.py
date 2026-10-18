import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import check_all_data as checker
from yoyoflot.readers import timetable_pdf


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/timetable.pdf"


def test_legacy_file_run_produces_pdf_outputs_from_any_cwd(tmp_path):
    output = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "readers/timetable_pdf.py"),
            "--path",
            str(FIXTURE),
            "--output",
            str(output),
            "--chunk-pages",
            "2",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    assert "Прогресс сохранён" not in result.stdout
    assert "Итог PDF" in result.stdout
    assert "Файлы сохранены" in result.stdout
    assert result.stdout == (output / "summary.txt").read_text(encoding="utf-8")
    report = json.loads((output / "source_report.json").read_text(encoding="utf-8"))
    assert report["rows"] == 5
    assert report["output_path"] == str(output.resolve())
    for name in [
        "schedules.csv",
        "schedules.jsonl",
        "schedules.parquet",
        "page_coverage.csv",
    ]:
        assert (output / name).is_file()


def test_old_checkpoint_format_is_rebuilt(tmp_path):
    output = tmp_path / "out"
    checkpoint = tmp_path / "out.checkpoint"
    checkpoint.mkdir()
    (checkpoint / "checkpoint.json").write_text('{"next_page":999}', encoding="utf-8")
    report = timetable_pdf.process(FIXTURE, output, chunk_pages=2)
    assert report["status"] == "complete"
    assert report["rows"] == 5


def test_pdf_worker_checks_exports_and_creates_review_sample(tmp_path):
    output = tmp_path / "pdf"
    checker.run_child(
        "ingest",
        source="timetable_pdf",
        input=str(FIXTURE),
        output=str(output),
        chunk_pages=2,
        options={},
    )
    result = checker.run_child("pdf", input=str(FIXTURE), output=str(output))
    assert result["exports"] == "equivalent"
    with (output / "audit_sheet.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 5
    assert all(row["manual_match"] == "" for row in rows)
