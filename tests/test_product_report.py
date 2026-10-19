"""Export a real completed ProductStore snapshot built from tiny unit fixtures.

These authored fixture rows are test inputs only, never dataset fallbacks.
"""
import csv
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from yoyoflot.cli import main
from yoyoflot.product import report
from yoyoflot.product.build import run as build_product
from yoyoflot.product.store import ProductStore
from yoyoflot.provenance import fingerprint
from yoyoflot.resolve.common import SCHEMAS
from tests.test_source_product import ROOT, config, tables


def _csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.fixture(scope="module")
def product_for_report(tmp_path_factory):
    root = tmp_path_factory.mktemp("report-source")
    source = tables(root)
    persons = pq.read_table(source / "persons.parquet").to_pylist()
    persons[0]["display_name"] = "Иванова Мария, Тест"
    persons[1]["display_name"] = '=HYPERLINK("https://example.invalid")'
    persons[2]["display_name"] = " \t+Сидоров"
    segments = pq.read_table(source / "segments.parquet").to_pylist()
    # A real segment in the fixture lacks airport coordinates. It still belongs
    # in the profile/export, and must not receive a guessed location.
    segments[0]["origin_airport_id"] = "test:unknown"
    segments[0]["origin_iata"] = "ZZZ"
    links = pq.read_table(source / "segment_links.parquet").to_pylist()
    for link in links:
        link.update(event_kind="reported", status_raw="Неизвестно")
    for name, rows in [("persons", persons), ("segments", segments), ("segment_links", links)]:
        pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMAS[name]), source / f"{name}.parquet")
    manifest = json.loads((source / "resolution_manifest.json").read_text(encoding="utf-8"))
    manifest["files"] = {name: fingerprint(source / name) for name in manifest["files"]}
    (source / "resolution_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    target = root / "product"
    build_product(source, target, config(root), ROOT / "data/reference/airports.csv")
    return target


def test_export_preserves_frozen_scores_profiles_and_source_evidence(product_for_report, tmp_path):
    product = product_for_report
    before = {p.relative_to(product).as_posix(): fingerprint(p) for p in product.rglob("*") if p.is_file()}
    output = tmp_path / "report"
    result = report.run(product, output)
    store = ProductStore(product)
    assert result["source_provenance"] == store.manifest["source_provenance"]
    profiles = json.loads((output / "profiles.json").read_text(encoding="utf-8"))
    assert profiles["run_id"] == result["run_id"] == store.manifest["run_id"]
    assert profiles["profiles"] == [store.profile(row["person_id"]) for row in store.top()]
    assert result["counts"] == {"persons": 3, "segments": 24, "top50": 3,
                                "exported_factors": 9, "exported_segments": 24, "source_evidence_rows": 24}
    factors = _csv(output / "factors.csv")
    flights = _csv(output / "flights.csv")
    assert len(factors) == 9 and len(flights) == 24
    for p in profiles["profiles"]:
        by_id = {factor["criterion_id"]: factor for factor in p["factors"]}
        person_factors = [row for row in factors if row["person_id"] == p["person"]["person_id"]]
        assert sum(float(row["contribution"]) for row in person_factors) == p["person"]["rule_score"]
        for row in person_factors:
            original = by_id[row["criterion_id"]]
            ids = json.loads(row["evidence_segment_ids_json"])
            assert ids == original["evidence_segment_ids"]
            assert float(row["threshold"]) == original["threshold"]
            assert float(row["weight"]) == original["weight"]
            assert row["status"] == original["status"]
            linked = json.loads(row["source_evidence_json"])
            assert {link["segment_id"] for link in linked} == set(ids)
            assert all(link["person_id"] == row["person_id"] for link in linked)
    for row in flights:
        links = json.loads(row["source_evidence_json"])
        assert all(link["person_id"] == row["person_id"] and link["segment_id"] == row["segment_id"] for link in links)
        assert links[0]["locator"].startswith("row:")
    manifest = json.loads((output / "report_manifest.json").read_text(encoding="utf-8"))
    assert manifest["request"]["source_manifest_sha256"] == fingerprint(product / "run_manifest.json")
    assert manifest["request"]["source_files"] == store.manifest["files"]
    assert all(fingerprint(output / name) == checksum for name, checksum in manifest["files"].items())
    assert before == {p.relative_to(product).as_posix(): fingerprint(p) for p in product.rglob("*") if p.is_file()}


def test_missing_coordinates_are_retained_with_status(product_for_report, tmp_path):
    output = tmp_path / "report"
    summary = report.run(product_for_report, output)
    unknown = next(row for row in _csv(output / "flights.csv") if row["segment_id"] == "person_000_0")
    assert unknown["origin_airport_id"] == "test:unknown" and unknown["origin_iata"] == "ZZZ"
    assert unknown["origin_latitude"] == unknown["origin_longitude"] == ""
    assert unknown["destination_latitude"] != "" and unknown["destination_longitude"] != ""
    assert unknown["is_plottable"] == "False" and "Неизвестны координаты" in unknown["unplottable_reason_ru"]
    assert unknown["departure_date"] == "2017-01-01" and unknown["departure_precision"] == "date"
    assert unknown["participation_status"] == "reported_only" and unknown["identity_status"] == "accepted"
    assert summary["coverage"]["top50"]["unplottable_segments"] == 1


def test_unicode_csv_and_formula_protection_preserve_raw_json(product_for_report, tmp_path):
    output = tmp_path / "report"
    report.run(product_for_report, output)
    raw = {p["person"]["person_id"]: p["person"]["display_name"]
           for p in json.loads((output / "profiles.json").read_text(encoding="utf-8"))["profiles"]}
    assert raw["person_001"].startswith("=HYPERLINK")
    for filename in ["top50.csv", "factors.csv", "flights.csv"]:
        assert (output / filename).read_bytes().startswith(b"\xef\xbb\xbf")
        for row in _csv(output / filename):
            pid = row["person_id"]
            expected = raw[pid] if pid == "person_000" else "'" + raw[pid]
            assert row["display_name"] == expected
    assert "Иванова Мария, Тест" in (output / "report.md").read_text(encoding="utf-8")


def test_identical_rerun_is_read_only_and_new_path_is_byte_identical(product_for_report, tmp_path):
    target = tmp_path / "report"
    first = report.run(product_for_report, target)
    before = {p.name: (fingerprint(p), p.stat().st_mtime_ns) for p in target.iterdir()}
    assert report.run(product_for_report, target) == first
    assert before == {p.name: (fingerprint(p), p.stat().st_mtime_ns) for p in target.iterdir()}
    second = tmp_path / "report-copy"
    assert report.run(product_for_report, second) == first
    assert {p.name: fingerprint(p) for p in target.iterdir()} == {p.name: fingerprint(p) for p in second.iterdir()}


def test_existing_foreign_or_damaged_output_is_never_overwritten(product_for_report, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("do not replace", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        report.run(product_for_report, foreign)
    assert (foreign / "notes.txt").read_text() == "do not replace"
    target = tmp_path / "report"
    report.run(product_for_report, target)
    (target / "top50.csv").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        report.run(product_for_report, target)
    assert (target / "top50.csv").read_text() == "tampered"
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize("relative", ["self", "child", "parent"])
def test_output_cannot_overlap_product_snapshot(product_for_report, relative):
    target = {"self": product_for_report, "child": product_for_report / "report", "parent": product_for_report.parent}[relative]
    before = fingerprint(product_for_report / "run_manifest.json")
    with pytest.raises(ValueError, match="overlap"):
        report.run(product_for_report, target)
    assert fingerprint(product_for_report / "run_manifest.json") == before
    assert not (product_for_report / "report").exists()


def test_report_cli_writes_all_files_and_shows_compact_top(product_for_report, tmp_path, capsys):
    target = tmp_path / "cli-report"
    assert main(["report", "--product", str(product_for_report), "--output", str(target)]) == 0
    output = capsys.readouterr().out
    assert "Топ: 3" in output and "Доступно" in output and "person_000" in output
    assert {p.name for p in target.iterdir()} == report.PAYLOAD_FILES | {"report_manifest.json"}
    assert main(["report", "--product", str(product_for_report), "--output", str(target), "-q"]) == 0
    assert capsys.readouterr().out == ""


def test_empty_product_exports_headers_and_honest_empty_summary(tmp_path):
    source = tables(tmp_path, people=0)
    product = tmp_path / "product"
    build_product(source, product, config(tmp_path), ROOT / "data/reference/airports.csv")
    target = tmp_path / "report"
    result = report.run(product, target)
    assert result["counts"]["top50"] == result["counts"]["exported_segments"] == 0
    assert result["top50"] == []
    assert all(_csv(target / name) == [] for name in ["top50.csv", "factors.csv", "flights.csv"])
    assert "Подходящих профилей нет" in (target / "report.md").read_text(encoding="utf-8")


def test_partial_profile_retains_unavailable_factor_and_undated_segment(tmp_path):
    source = tables(tmp_path)
    segments = pq.read_table(source / "segments.parquet").to_pylist()
    segments[0]["departure_date"] = None
    pq.write_table(pa.Table.from_pylist(segments, schema=SCHEMAS["segments"]), source / "segments.parquet")
    manifest = json.loads((source / "resolution_manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["segments.parquet"] = fingerprint(source / "segments.parquet")
    (source / "resolution_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    product = tmp_path / "product"
    build_product(source, product, config(tmp_path), ROOT / "data/reference/airports.csv")
    target = tmp_path / "report"
    result = report.run(product, target)
    person = next(row for row in _csv(target / "top50.csv") if row["person_id"] == "person_000")
    factor = next(row for row in _csv(target / "factors.csv")
                  if row["person_id"] == "person_000" and row["criterion_id"] == "M02")
    assert person["available_criteria_count"] == "2" and person["total_criteria_count"] == "3"
    assert factor["status"] == "unavailable" and factor["value"] == ""
    assert float(factor["contribution"]) == 0 and factor["availability_reason"]
    assert json.loads(factor["evidence_segment_ids_json"]) == []
    assert json.loads(factor["source_evidence_json"]) == []
    undated = next(row for row in _csv(target / "flights.csv") if row["segment_id"] == "person_000_0")
    assert undated["departure_date"] == "" and undated["departure_label_ru"] == "Дата неизвестна"
    assert result["counts"]["exported_segments"] == 24
    assert result["coverage"]["top50"]["undated_segments"] == 1
    assert result["coverage"]["top50"]["factor_statuses"]["unavailable"] == 1


def test_symbolic_link_output_preserves_destination(product_for_report, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("do not replace", encoding="utf-8")
    target = tmp_path / "report"
    target.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        report.run(product_for_report, target)
    assert target.is_symlink() and (foreign / "notes.txt").read_text() == "do not replace"


def test_interruption_cleans_staging_and_keeps_snapshot(product_for_report, tmp_path, monkeypatch):
    before = fingerprint(product_for_report / "run_manifest.json")
    target = tmp_path / "report"
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(report, "_write_csv", interrupt)
    with pytest.raises(KeyboardInterrupt):
        report.run(product_for_report, target)
    assert not target.exists() and not list(tmp_path.glob("*.partial"))
    assert fingerprint(product_for_report / "run_manifest.json") == before
