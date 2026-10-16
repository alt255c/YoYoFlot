import json
from pathlib import Path
from zipfile import ZipFile

import pyarrow.parquet as pq
import pymupdf
import pytest

import check_all_data as checker
from yoyoflot.readers import timetable_pdf as tp

FIXTURE = Path(__file__).parent / 'fixtures' / 'timetable.pdf'


def test_finished_pdf_reuses_exports_and_keeps_all_original_text(tmp_path, monkeypatch):
    out = tmp_path / 'out'
    first = tp.process(FIXTURE, out, chunk_pages=2)
    exported = out / 'schedules.csv'
    modified = exported.stat().st_mtime_ns
    with ZipFile(out / 'page_text.zip') as archive, pymupdf.open(FIXTURE) as doc:
        assert len(archive.namelist()) == len(doc)
        for number, page in enumerate(doc, 1):
            assert archive.read(f'page_{number:06d}.txt').decode('utf-8') == page.get_text('text')
    def unexpected(*args, **kwargs):
        raise AssertionError('Completed checkpoint must not extract pages again')
    monkeypatch.setattr(tp, '_extract', unexpected)
    assert tp.process(FIXTURE, out, chunk_pages=2) == json.loads(json.dumps(first))
    assert exported.stat().st_mtime_ns == modified
    assert 'готовая выгрузка: да' in (out / 'summary.txt').read_text(encoding='utf-8')
    # A changed export is rebuilt from the checkpoint, without extracting PDF again.
    exported.write_text('broken', encoding='utf-8')
    tp.process(FIXTURE, out, chunk_pages=2)
    assert checker.check_pdf(out, FIXTURE, first)['exports'] == 'equivalent'


def test_review_pages_do_not_hide_export_corruption(tmp_path):
    out = tmp_path / 'out'
    pdf = tmp_path / 'scan.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_image(page.rect, stream=b'P6\n2 2\n255\n' + bytes([255]) * 12)
        doc.save(pdf)
    report = tp.process(pdf, out)
    result = checker.check_pdf(out, pdf, report)
    assert result['coverage_status'] == 'needs_review'
    assert result['pages_requiring_review'] == 1
    assert result['exports'] == 'equivalent'
    (out / 'schedules.jsonl').write_text('{"unexpected":1}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='Количество'):
        checker.check_pdf(out, pdf, report)


def test_source_ids_and_raw_offsets_are_preserved(tmp_path):
    out = tmp_path / 'out'
    tp.process(FIXTURE, out)
    with pymupdf.open(FIXTURE) as doc:
        originals = [p.get_text('text') for p in doc]
    for row in pq.read_table(out / 'schedules.parquet').to_pylist():
        raw = json.loads(row['raw_json'])
        assert raw['page_text_archive'] == 'page_text.zip'
        assert row['raw_line'] in originals[row['source_page'] - 1]
