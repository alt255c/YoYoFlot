import importlib.util
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pymupdf
import pytest

from yoyoflot.readers import timetable_pdf as tp


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/timetable.pdf"


def legacy():
    path = ROOT / "docs/legacy/timetable_pdf_original.py"
    spec = importlib.util.spec_from_file_location("pdf_original_reference", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_original_parse_fields_match_on_every_fixture_page():
    original = legacy()
    old_carry = new_carry = None
    with pymupdf.open(FIXTURE) as doc:
        cover = original.parse_cover(original.normalize_ws(doc[0].get_text("text")))
        for index, page in enumerate(doc):
            text = page.get_text("text")
            old, _, _, old_carry = original.parse_page(
                text, index + 1, cover, old_carry
            )
            new, _, _, new_carry = tp.parse_page(text, index + 1, cover, new_carry)
            assert old == new
            assert old_carry == new_carry


def test_extract_preserves_original_text_not_block_reordering():
    class Page:
        def get_text(self, mode):
            assert mode == "text"
            return "ORIGINAL ORDER"

    assert tp._extract(Page()) == "ORIGINAL ORDER"


def build_with_image(path):
    image = b"P6\n2 2\n255\n" + bytes([255, 255, 255]) * 4
    with pymupdf.open() as doc:
        doc.new_page().insert_text(
            (30, 30), "Covers period: 01 Nov 2018 through 31 Jan 2019"
        )
        doc.new_page().insert_text((30, 30), "FROM: A, CountryAAA TO: B, CountryBBB")
        doc.new_page().insert_image(pymupdf.Rect(0, 0, 500, 500), stream=image)
        doc.new_page().insert_text(
            (30, 30), "01 Nov - 31 Jan 1234567 08:40 11:10 AB123 320 2H30M"
        )
        doc.save(path)


def test_image_does_not_stop_or_lose_original_continuation(tmp_path):
    path, output = tmp_path / "image.pdf", tmp_path / "out"
    build_with_image(path)
    report = tp.process(path, output, chunk_pages=2)
    assert report["pages"] == 4
    assert report["rows"] == 1
    assert report["status"] == "incomplete"
    row = pq.read_table(output / "schedules.parquet").to_pylist()[0]
    assert row["origin"] == "AAA" and row["destination"] == "BBB"
    assert row["flight_number"] == "AB123"
    assert row["parse_status"] == "unverified_carry_after_unreadable_page"
    with (output / "problem_pages/page_000003.pdf").open("rb") as f:
        with pymupdf.open(stream=f.read(), filetype="pdf") as diagnostic:
            assert len(diagnostic) == 1
    repeat = tp.process(path, output, chunk_pages=2)
    assert repeat["rows"] == 1
    assert pq.read_table(output / "schedules.parquet").to_pylist() == [row]


def test_previous_partial_checkpoint_is_verified_and_reused(tmp_path, monkeypatch):
    output = tmp_path / "out"
    original = tp._extract

    def interrupt(page, ocr=False):
        if page.number == 2:
            raise RuntimeError("interrupt")
        return original(page, ocr)

    monkeypatch.setattr(tp, "_extract", interrupt)
    with pytest.raises(RuntimeError):
        tp.process(FIXTURE, output, chunk_pages=2)
    file = tmp_path / "out.checkpoint/checkpoint.json"
    checkpoint = json.loads(file.read_text(encoding="utf-8"))
    checkpoint["signature"].pop("extraction_mode")
    file.write_text(json.dumps(checkpoint), encoding="utf-8")
    calls = []

    def record(page, ocr=False):
        calls.append(page.number)
        return original(page, ocr)

    monkeypatch.setattr(tp, "_extract", record)
    report = tp.process(FIXTURE, output, chunk_pages=2)
    assert report["rows"] == 5
    assert calls == [2, 3]


def test_missing_ocr_support_is_reported_and_later_pages_are_processed(
    tmp_path, monkeypatch
):
    path = tmp_path / "ocr.pdf"
    build_with_image(path)

    def unavailable(*args, **kwargs):
        raise RuntimeError("missing eng.traineddata")

    monkeypatch.setattr(pymupdf.Page, "get_textpage_ocr", unavailable)
    report = tp.process(path, tmp_path / "out", chunk_pages=2, ocr=True)
    assert report["pages"] == 4
    assert report["rows"] == 1
    assert report["pages_by_type"]["ocr_failed"] == 1
    assert report["status"] == "incomplete"


def test_image_only_cover_does_not_abort_pdf(tmp_path):
    image = b"P6\n2 2\n255\n" + bytes([255, 255, 255]) * 4
    path = tmp_path / "cover.pdf"
    with pymupdf.open() as doc, pymupdf.open(FIXTURE) as fixture:
        doc.new_page().insert_image(pymupdf.Rect(0, 0, 500, 500), stream=image)
        doc.insert_pdf(fixture, from_page=1)
        doc.save(path)
    report = tp.process(path, tmp_path / "out", chunk_pages=2)
    assert report["pages"] == 4
    assert report["rows"] == 5
    assert report["cover_period"] is None
    assert report["status"] == "incomplete"


def write_original_output(output):
    original = legacy()
    rows, carry = [], None
    with pymupdf.open(FIXTURE) as doc:
        cover = original.parse_cover(original.normalize_ws(doc[0].get_text("text")))
        for number, page in enumerate(doc, 1):
            page_rows, _, _, carry = original.parse_page(
                page.get_text("text"), number, cover, carry
            )
            rows.extend(page_rows)
    output.mkdir()
    schema = pa.schema(
        [
            (
                field,
                pa.int64()
                if field in ("source_page", "arrival_day_offset")
                else pa.bool_()
                if field == "codeshare_mark"
                else pa.string(),
            )
            for field in original.FIELDS
        ]
    )
    pq.write_table(
        pa.Table.from_pylist(rows, schema=schema), output / "schedules.parquet"
    )
    (output / "source_report.json").write_text(
        json.dumps({"rows": len(rows), "pages": 4}), encoding="utf-8"
    )


def test_preserves_and_compares_first_successful_output(tmp_path):
    output = tmp_path / "out"
    write_original_output(output)
    report = tp.process(FIXTURE, output, chunk_pages=2)
    assert (tmp_path / "out.baseline_v1/schedules.parquet").is_file()
    comparison = report["baseline_comparison"]
    assert comparison["status"] == "matched"
    assert comparison["old_rows"] == comparison["new_rows"] == 5
    assert comparison["missing_legacy_rows"] == 0
    assert not any(comparison["field_differences"].values())


@pytest.mark.parametrize("change", ["missing", "different_flight"])
def test_reference_comparison_detects_real_regressions(tmp_path, change):
    from yoyoflot.readers.pdf_reference import compare

    output = tmp_path / "out"
    write_original_output(output)
    report = tp.process(FIXTURE, output, chunk_pages=2)
    table = pq.read_table(output / "schedules.parquet")
    rows = table.to_pylist()
    if change == "missing":
        rows.pop(0)
    else:
        rows[0]["flight_number"] = "ZZ9999"
    pq.write_table(
        pa.Table.from_pylist(rows, schema=table.schema), output / "schedules.parquet"
    )
    result = compare(tmp_path / "out.baseline_v1", output, report["sha256"])
    assert result["status"] == "differences"
    if change == "missing":
        assert result["missing_legacy_rows"] == 1
    else:
        assert result["field_differences"]["flight_number"] == 1
