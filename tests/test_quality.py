import json

import pyarrow as pa
import pyarrow.parquet as pq

from yoyoflot.normalize.canonical import SCHEMAS
from yoyoflot.quality import audit


def _row(table, **values):
    row = {field.name: None for field in SCHEMAS[table]}
    row.update(source_id="fixture", source_record_id=f"{table}-source", locator=f"{table}/1")
    row.update(values)
    key = {"observations": "observation_id", "identifiers": "identifier_id", "travel_observations": "travel_id", "schedules": "schedule_id", "errors": "error_id"}.get(table)
    if key: row[key] = f"{table}-1"
    return row


def _write_dataset(path, include_observation_provenance=True):
    rows = {
        "observations": [_row("observations", observation_id="observations-1", name_raw="Ivan", name_comparison="ivan")],
        "identifiers": [_row("identifiers", identifier_id="identifiers-1", observation_id="observations-1", kind="loyalty_card", value="123")],
        "travel_observations": [_row("travel_observations", travel_id="travel_observations-1", observation_id="observations-1", loyalty_identifier_id="identifiers-1")],
        "schedules": [], "errors": [],
        "provenance": [
            _row("provenance", entity_table="observations", entity_id="observations-1", source_record_id="observations-source", transformation="reader:1.0"),
            _row("provenance", entity_table="identifiers", entity_id="identifiers-1", source_record_id="identifiers-source", transformation="reader:1.0"),
            _row("provenance", entity_table="travel_observations", entity_id="travel_observations-1", source_record_id="travel_observations-source", transformation="reader:1.0"),
        ],
    }
    if not include_observation_provenance:
        rows["provenance"] = rows["provenance"][1:]
    for name, values in rows.items():
        pq.write_table(pa.Table.from_pylist(values, schema=SCHEMAS[name]), path / f"{name}.parquet")
    (path / "canonical_report.json").write_text(json.dumps({"status": "complete", "counts": {name: len(values) for name, values in rows.items()}}), encoding="utf-8")


def test_quality_passes_and_writes_review_sample(tmp_path):
    _write_dataset(tmp_path)
    report = audit(tmp_path, sample_size=2)
    assert report["status"] == "passed"
    assert report["provenance"]["missing_by_table"]["observations"] == 0
    assert (tmp_path / "quality.json").is_file()
    assert len((tmp_path / "quality_review_sample.csv").read_text(encoding="utf-8").splitlines()) == 2


def test_quality_blocks_missing_provenance(tmp_path):
    _write_dataset(tmp_path, include_observation_provenance=False)
    report = audit(tmp_path)
    assert report["status"] == "failed"
    assert "missing_provenance:observations" in report["blockers"]


def test_provenance_uses_the_whole_composite_key(tmp_path):
    _write_dataset(tmp_path)
    people = pq.read_table(tmp_path / "observations.parquet").to_pylist()
    people.append({**people[0], "observation_id": "observations-2", "source_record_id": "source-2"})
    links = pq.read_table(tmp_path / "provenance.parquet").to_pylist()
    links.append(_row("provenance", entity_table="observations", entity_id="observations-2", source_record_id="source-2"))
    for name, rows in (("observations", people), ("provenance", links)):
        pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMAS[name]), tmp_path / f"{name}.parquet")
    report_path = tmp_path / "canonical_report.json"
    upstream = json.loads(report_path.read_text())
    upstream["counts"].update(observations=2, provenance=4)
    report_path.write_text(json.dumps(upstream))
    report = audit(tmp_path)
    assert report["status"] == "passed"
    assert report["duplicate_or_null_keys"]["provenance"] == 0


def test_duplicate_full_provenance_key_is_still_a_blocker(tmp_path):
    _write_dataset(tmp_path)
    links = pq.read_table(tmp_path / "provenance.parquet").to_pylist()
    links.append(dict(links[0]))
    pq.write_table(pa.Table.from_pylist(links, schema=SCHEMAS["provenance"]), tmp_path / "provenance.parquet")
    report = audit(tmp_path)
    assert report["status"] == "failed"
    assert report["duplicate_or_null_keys"]["provenance"] == 1
    assert "duplicate_provenance_link" in report["blockers"]
