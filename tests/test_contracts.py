import json

import pyarrow.parquet as pq
import pytest
from yoyoflot.contracts import SCHEMAS, TableBatch
from yoyoflot.io import ingest
from yoyoflot.provenance import stable_id
from yoyoflot.readers import boarding_csv


def test_empty_schemas_and_strict_identifiers():
    for name, schema in SCHEMAS.items():
        assert TableBatch(name, []).to_arrow().schema == schema
    with pytest.raises(TypeError):
        TableBatch(
            "identifiers",
            [
                dict(
                    source_id="s",
                    source_record_id="r",
                    locator="l",
                    identifier_id="i",
                    observation_id="o",
                    kind="document",
                    value=123,
                )
            ],
        ).to_arrow()
    with pytest.raises(ValueError):
        TableBatch("person_observations", [dict(source_id="s")]).to_arrow()


def test_stable_locator_id():
    assert stable_id("s", "v", "line:2", "person") == stable_id(
        "s", "v", "line:2", "person"
    )
    assert stable_id("s", "v2", "line:2", "person") != stable_id(
        "s", "v", "line:2", "person"
    )


def test_failure_never_replaces_valid_output(tmp_path):
    path = tmp_path / "input"
    path.write_text("x", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep").write_text("complete", encoding="utf-8")

    def broken(*args, **kwargs):
        yield TableBatch("person_observations", [])
        raise ValueError("broken source")

    with pytest.raises(ValueError):
        ingest(broken, path, "s", output)
    assert (output / "keep").read_text(encoding="utf-8") == "complete"
    report = next(tmp_path.glob("*.partial/source_report.json"))
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "failed"


def test_empty_source_and_roundtrip_identifier(tmp_path):
    import csv

    path = tmp_path / "input.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(boarding_csv.EXPECTED_COLUMNS)
    out = tmp_path / "empty"
    ingest(boarding_csv.read, path, "boarding_csv", out)
    for name, schema in SCHEMAS.items():
        assert pq.read_table(out / (name + ".parquet")).schema == schema
    row = {k: "" for k in boarding_csv.EXPECTED_COLUMNS}
    row.update(PassengerFirstName="X", PassengerDocument="000123")
    with path.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter=";").writerow(
            [row[k] for k in boarding_csv.EXPECTED_COLUMNS]
        )
    ingest(boarding_csv.read, path, "boarding_csv", out)
    assert pq.read_table(out / "identifiers.parquet")["value"].to_pylist() == ["000123"]
