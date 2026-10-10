import csv
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pymupdf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from readers import timetable_pdf as tp

FIX = PROJECT_ROOT / "tests" / "fixtures" / "timetable.pdf"


def build_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "SkyTeam Timetable", fontsize=16)
    p.insert_text((72, 100), "Covers period: 01 Nov 2018 through 31 Jan 2019", fontsize=12)
    p = doc.new_page()
    y = 72
    for line in [
        "FROM: Hefei, ChinaHFE TO: Taipei, TaiwanTPE Validity Days DepTime ArrTime Flight Aircraft TravelTime",
        "01 Nov - 31 Jan 1234567 08:40 11:10 MU2049 320 2H30M",
        "01 Nov - 31 Jan 1234567 08:40 11:10 CI8028* 320 2H30M Operated by: China Eastern Airlines",
        "FROM: Taipei, TaiwanTPE TO: Hefei, ChinaHFE Validity Days DepTime ArrTime Flight Aircraft TravelTime",
        "03 Nov - 29 Jan 12 456 17:25 20:00 MU2050 320 2H35M",
    ]:
        p.insert_text((72, y), line, fontsize=10)
        y += 16
    p = doc.new_page()
    y = 72
    for line in [
        "01 Nov  -  02 Nov", "        56", "15:40", "09:50+1", "DL9311*", "EQV", "10H10M",
        "Operated by:  KLM Royal Dutch Airlines",
        "01 Nov  -  03 Nov", "        567 15:40", "09:50+1", "KL606", "EQV", "10H10M",
    ]:
        p.insert_text((72, y), line, fontsize=10)
        y += 14
    p = doc.new_page()
    p.insert_text((72, 72), "FROM: Zurich, SwitzerlandZRH TO: Stavanger, NorwaySVG", fontsize=10)
    p.insert_text((72, 100), "Consult your travel agent for details", fontsize=12)
    doc.save(str(path))
    doc.close()


if not FIX.exists():
    build_fixture(FIX)


def _pages() -> list[str]:
    doc = pymupdf.open(FIX)
    texts = [doc[i].get_text("text") for i in range(doc.page_count)]
    doc.close()
    return texts


def test_cover_and_rows():
    texts = _pages()
    cover = tp.parse_cover(texts[0])
    assert cover == ((1, 11, 2018), (31, 1, 2019))
    rows, cov, _errs, _carry = tp.parse_page(texts[1], 2, cover)
    assert len(rows) == 3
    r0 = rows[0]
    assert (r0["flight_number"], r0["origin"], r0["destination"]) == ("MU2049", "HFE", "TPE")
    assert (r0["departure_local"], r0["arrival_local"]) == ("08:40", "11:10")
    assert r0["arrival_day_offset"] is None
    assert r0["days_of_week"] == "1234567"
    assert (r0["valid_from"], r0["valid_to"]) == ("2018-11-01", "2019-01-31")
    assert r0["section_source"] == "page"
    assert rows[1]["codeshare_mark"] and rows[1]["operated_by"] == "China Eastern Airlines"
    assert rows[2]["days_of_week"] == "12456"
    assert cov["type"] == "table"


def test_continuation_carried_and_offset():
    texts = _pages()
    cover = tp.parse_cover(texts[0])
    _r, _c, _e, carry = tp.parse_page(texts[1], 2, cover)
    rows, cov, errs, _out = tp.parse_page(texts[2], 3, cover, carried=carry)
    assert len(rows) == 2 and errs == []
    assert cov["type"] == "continuation" and cov["status"] == "ok"
    r = rows[0]
    assert r["flight_number"] == "DL9311"
    assert r["arrival_day_offset"] == 1
    assert r["days_of_week"] == "56"
    assert (r["origin"], r["destination"]) == ("TPE", "HFE")
    assert r["section_source"] == "carried:2"
    assert r["operated_by"] == "KLM Royal Dutch Airlines"
    assert (r["valid_from"], r["valid_to"]) == ("2018-11-01", "2018-11-02")
    assert rows[1]["operated_by"] is None


def test_consult_page_not_error():
    texts = _pages()
    _r, cov, errs, _o = tp.parse_page(texts[3], 4, ((1, 11, 2018), (31, 1, 2019)))
    assert cov["type"] == "consult" and cov["status"] == "ok" and errs == []


def test_rerun_no_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "PDF_PATH", FIX)
    monkeypatch.setattr(tp, "OUT_DIR", tmp_path)
    monkeypatch.setattr(tp, "CHUNK_PAGES", 2)
    tp.run()
    with open(tmp_path / "schedules.csv", newline="", encoding="utf-8") as f:
        first = len(list(csv.DictReader(f)))
    tp.run()
    with open(tmp_path / "schedules.csv", newline="", encoding="utf-8") as f:
        second = len(list(csv.DictReader(f)))
    assert first == second == 5
    with open(tmp_path / "schedules.jsonl", encoding="utf-8") as f:
        assert sum(1 for _ in f) == 5
    assert len(pq.read_table(tmp_path / "schedules.parquet")) == 5