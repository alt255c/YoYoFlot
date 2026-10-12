import csv
import json
import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pymupdf
import pytest
from yoyoflot.readers import timetable_pdf as tp

FIXTURE = Path(__file__).parent / "fixtures" / "timetable.pdf"


def test_checkpoint_resume_after_real_interruption(tmp_path, monkeypatch):
    out = tmp_path / "out"
    original = tp._extract

    def interrupt(page, ocr=False):
        if page.number == 2:
            raise RuntimeError("simulated crash")
        return original(page, ocr)

    monkeypatch.setattr(tp, "_extract", interrupt)
    with pytest.raises(RuntimeError):
        tp.process(FIXTURE, out, chunk_pages=2)
    assert not out.exists()
    state = json.loads(
        (tmp_path / "out.checkpoint/checkpoint.json").read_text(encoding="utf-8")
    )
    assert state["next_page"] == 2
    monkeypatch.setattr(tp, "_extract", original)
    report = tp.process(FIXTURE, out, chunk_pages=2)
    assert report["status"] == "complete"
    rows = pq.read_table(out / "schedules.parquet").to_pylist()
    assert len(rows) == len({r["schedule_id"] for r in rows}) == 5
    assert rows[3]["section_source"] == "carried:2"


def test_changed_pdf_and_chunk_configuration_no_stale_parts(tmp_path):
    path = tmp_path / "input.pdf"
    shutil.copyfile(FIXTURE, path)
    out = tmp_path / "out"
    tp.process(path, out, chunk_pages=1)
    with pymupdf.open() as doc:
        p = doc.new_page()
        p.insert_text((30, 30), "Covers period: 01 Nov 2018 through 31 Jan 2019")
        doc.save(path)
    report = tp.process(path, out, chunk_pages=3)
    assert report["rows"] == 0
    assert report["pages"] == 1
    assert len(list((tmp_path / "out.checkpoint").glob("part_*.json"))) == 1
    assert len(pq.read_table(out / "schedules.parquet")) == 0


def test_typed_csv_jsonl_parquet_equivalence_and_unknown_period(tmp_path):
    out = tmp_path / "out"
    tp.process(FIXTURE, out, chunk_pages=1)
    table = pq.read_table(out / "schedules.parquet").to_pylist()
    jsonrows = [
        json.loads(line)
        for line in (out / "schedules.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert table == jsonrows
    with (out / "schedules.csv").open(newline="", encoding="utf-8") as f:
        csvrows = list(csv.DictReader(f))
    for a, b in zip(csvrows, jsonrows):
        for key, value in b.items():
            assert a[key] == ("\\N" if value is None else str(value))
    rows, cov, errors, _ = tp.parse_page(
        "FROM: A, CountryAAA TO: B, CountryBBB 01 Nov - 31 Jan 1234567 08:40 11:10 AB123 320 2H30M",
        1,
        None,
    )
    assert rows[0]["valid_from"] is rows[0]["valid_to"] is None


def test_unparsed_candidate_and_unknown_template_block_success(tmp_path):
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((30, 30), "FROM: A, CountryAAA TO: B, CountryBBB")
    p.insert_text((30, 50), "01 Nov - 31 Jan unknown unsupported row")
    path = tmp_path / "bad.pdf"
    doc.save(path)
    doc.close()
    report = tp.process(path, tmp_path / "out")
    assert report["status"] == "incomplete"
    assert report["errors"] > 0
    assert report["excluded_pages"] == 1


def test_image_page_does_not_masquerade_as_blank(tmp_path):
    image = b"P6\n2 2\n255\n" + bytes([255, 255, 255]) * 4
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_image(p.rect, stream=image)
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()
    report = tp.process(path, tmp_path / "out")
    assert report["status"] == "incomplete"
    assert report["pages"] == 1
    assert report["pages_by_type"]["image_without_text"] == 1
    assert report["extraction_errors"] == 1
    assert (tmp_path / "out/problem_pages/page_000001.pdf").is_file()


def test_sample_limit_is_not_full_success(tmp_path):
    report = tp.process(FIXTURE, tmp_path / "out", limit_pages=2)
    assert report["status"] == "incomplete"
    assert report["total_pages"] == 4 and report["pages"] == 2
