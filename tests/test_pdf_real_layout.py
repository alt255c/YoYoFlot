"""Regressions from unchanged pages of the supplied SkyTeam source PDF.

The fixture manifest records its original page numbers and full-source SHA-256.
"""

import json
from pathlib import Path

import pyarrow.parquet as pq
import pymupdf
import pytest

from yoyoflot.readers import timetable_pdf as tp


FIXTURE = Path(__file__).parent / "fixtures/timetable_real_pages.pdf"
PAGES = json.loads(FIXTURE.with_suffix(".json").read_text())["source_pages"]
COVER = ((1, 11, 2018), (31, 1, 2019))


def source_page(document, number):
    return document[PAGES.index(number)]


@pytest.mark.parametrize(
    "number,origin,destination,country",
    [
        (26, "ABR", "MSP", "United"),
        (77, "AUH", "DAR", "Tanzania, United"),
        (254, "AJA", "ETZ", "France"),
        (3324, "ATL", "MYR", None),
        (5572, "BEG", "SKP", "Macedonia (FYROM)"),
        (7458, "CAI", "RUN", None),
    ],
)
def test_source_section_variants_are_explicit_routes(number, origin, destination, country):
    with pymupdf.open(FIXTURE) as doc:
        page = source_page(doc, number)
        rows, coverage, errors, carry = tp.parse_pdf_page(page, tp._extract(page), number, COVER)
    assert not errors
    assert coverage["sections"] == 2
    assert coverage["status"] == "ok"
    left = carry["_columns"]["left"]
    assert (left["oiata"], left["diata"]) == (origin, destination)
    assert ((left["dcountry"] or "").strip() or None) == country
    for row in rows:
        assert row["parse_status"] == "ok"
        if row["section_source"].endswith("left"):
            assert (row["origin"], row["destination"]) == (origin, destination)
            assert row["dest_country"] == country


@pytest.mark.parametrize("numbers,expected,forward,reverse", [
    ([5, 6], [92, 8], ("AAL", "AMS"), ("AMS", "AAL")),
    ([26, 27], [92, 100], ("ABR", "MSP"), ("MSP", "ABR")),
    ([13094, 13095, 13096], [68, 72, 60], ("CAN", "YVR"), ("YVR", "CAN")),
])
def test_real_continuations_keep_independent_routes(numbers, expected, forward, reverse):
    carry, negatives = None, []
    with pymupdf.open(FIXTURE) as doc:
        for number, count in zip(numbers, expected):
            page = source_page(doc, number)
            original = tp._extract(page)
            rows, coverage, errors, carry = tp.parse_pdf_page(page, original, number, COVER, carry)
            assert len(rows) == coverage["rows_found"] == coverage["rows_parsed"] == count
            assert not errors
            assert len({r["schedule_id"] for r in rows}) == count
            for row in rows:
                assert (row["origin"], row["destination"]) == (
                    forward if row["section_source"].endswith("left") else reverse
                )
                assert row["raw_line"] in original
                if row["arrival_day_offset"] == -1:
                    negatives.append(row)
                    assert "+-1" in row["raw_line"]
    assert len(negatives) == (60 if numbers[0] == 13094 else 0)


def test_real_spillover_note_and_logo_only_page_are_preserved(tmp_path):
    report = tp.process(FIXTURE, tmp_path / "out", chunk_pages=2)
    assert report["status"] == "complete"
    assert report["rows"] == report["rows_ok"] == 624
    assert report["errors"] == report["extraction_errors"] == 0
    assert report["pages_by_type"]["header_only"] == 1
    assert report["pages_by_type"]["notes"] == 1
    import zipfile
    with zipfile.ZipFile(tmp_path / "out/page_text.zip") as archive:
        note = archive.read(f"page_{PAGES.index(17393) + 1:06d}.txt").decode()
        assert note.strip() == "Operated by:  HOP!"


def test_column_routes_survive_checkpoint_restart(tmp_path, monkeypatch):
    original = tp._extract

    def interrupt(page, ocr=False):
        if page.number == 2:
            raise RuntimeError("interrupted after the two route headers")
        return original(page, ocr)

    monkeypatch.setattr(tp, "_extract", interrupt)
    with pytest.raises(RuntimeError):
        tp.process(FIXTURE, tmp_path / "out", chunk_pages=2)
    saved = json.loads((tmp_path / "out.checkpoint/checkpoint.json").read_text())
    assert saved["carry"]["_columns"]["left"]["oiata"] == "AAL"
    assert saved["carry"]["_columns"]["right"]["oiata"] == "AMS"
    monkeypatch.setattr(tp, "_extract", original)
    assert tp.process(FIXTURE, tmp_path / "out", chunk_pages=2)["status"] == "complete"
    rows = pq.read_table(tmp_path / "out/schedules.parquet").to_pylist()
    continuation = [row for row in rows if row["source_page"] == 3]
    assert [row["origin"] for row in continuation] == ["AAL"] * 4 + ["AMS"] * 4


def test_unreadable_page_taints_each_column_until_its_own_header():
    with pymupdf.open(FIXTURE) as doc:
        page = source_page(doc, 5)
        _, _, _, carry = tp.parse_pdf_page(page, tp._extract(page), 5, COVER)
    carry = tp._unverified_carry(carry, 6)
    row = "01 Nov - 31 Jan 1234567 08:40 11:10 AB123 320 2H30M"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((20, 60), "FROM: A, CountryAAA TO: B, CountryBBB", fontsize=7)
        page.insert_text((20, 90), row, fontsize=7)
        page.insert_text((310, 90), row, fontsize=7)
        rows, coverage, errors, carry = tp.parse_pdf_page(page, page.get_text(), 7, COVER, carry)
    assert rows[0]["parse_status"] == "ok"
    assert (rows[0]["origin"], rows[0]["destination"]) == ("AAA", "BBB")
    assert rows[1]["parse_status"] == "unverified_carry_after_unreadable_page"
    assert coverage["status"] == "excluded"
    assert len(errors) == 1
    assert "_unverified_since" not in carry["_columns"]["left"]
    assert carry["_columns"]["right"]["_unverified_since"] == 6


def test_unsupported_header_cannot_borrow_previous_route():
    text = (
        "FROM: A, CountryAAA TO: B, CountryBBB "
        "01 Nov - 31 Jan 1234567 08:40 11:10 AB123 320 2H30M "
        "FROM: unreadable ??? TO: unknown ??? "
        "01 Nov - 31 Jan 1234567 08:40 11:10 AB456 320 2H30M"
    )
    rows, coverage, errors, carry = tp.parse_page(text, 1, COVER)
    assert rows[0]["origin"] == "AAA"
    assert rows[1]["origin"] is rows[1]["destination"] is None
    assert rows[1]["parse_status"] == "row_without_section"
    assert carry is None
    assert coverage["status"] == "excluded"
    assert {error["reason"] for error in errors} == {"unparsed_section", "row_without_section"}


@pytest.mark.parametrize("body_content", ["image", "vector"])
def test_header_template_does_not_hide_image_or_vector_body(body_content):
    with pymupdf.open(FIXTURE) as doc:
        page = source_page(doc, 3680)
        assert tp._extract(page) == ""
        rect = pymupdf.Rect(50, 300, 100, 320)
        if body_content == "image":
            page.insert_image(rect, stream=b"P6\n2 2\n255\n" + bytes([0, 0, 0]) * 4)
        else:
            page.draw_rect(rect, fill=(0, 0, 0))
        with pytest.raises(tp.PageExtractionError, match="images without text"):
            tp._extract(page)


def test_changed_column_text_order_is_an_explicit_extraction_error():
    with pymupdf.open(FIXTURE) as doc:
        page = source_page(doc, 5)
        with pytest.raises(tp.PageExtractionError, match="complete original extraction order"):
            tp.parse_pdf_page(page, page.get_text() + "missing clipped content", 5, COVER)
